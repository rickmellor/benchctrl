"""The PDU41002 driver against its simulator, plus the fixtures it was built from.

This is the hardware-free half of the driver's coverage. Everything here runs
against :py:class:`SimulatedPDU41002` over a pty, holding the *production*
driver — so the CLI engine, the login handshake, the parsers and the policy
checks are all the real ones.

Three things make this file different from the other per-driver test suites:

1. **This device switches mains.** So the tests that matter most are the ones
   asserting what the driver *cannot* do: no aggregate target is expressible,
   no read method is classified as a mutator, and the allowlist refuses before
   any bytes leave the process.
2. **The parsers are pinned to captured bytes, not to the simulator.** A
   simulator built from the same misreading as the driver agrees with it
   (``AGENTS.md``, and ``sim/qr10x.py``'s docstring records what that cost
   last time). ``TestFixtureParsing`` therefore feeds the driver's module-level
   parsers with literal excerpts from ``tests/fixtures/pdu41002/``, which are
   verbatim device output. If the simulator and the fixtures ever disagree, the
   fixtures win.
3. **The naming test is a safety test.** ``agent/dispatch.py`` derives which
   methods need a writer claim purely from name prefixes, with no
   driver-declared override. A read that accidentally matched a mutator prefix
   would make metering require a claim; a switching method that *didn't* match
   would be remotely callable **without** one. ``TestDispatchClassification``
   is the guard on that, and it is the reason the method names are what they
   are.
"""

from __future__ import annotations

import pytest

from benchctrl.drivers.cyberpower_pdu41002 import (
    PDU41002AuthError,
    PDU41002CommandError,
    PDU41002Info,
    PDU41002PolicyError,
    PDU41002ProtocolError,
    PDU41002Status,
    PDU41002ValueError,
)


@pytest.fixture()
def pdu():
    """A production driver bound to a simulated PDU over a pty.

    Goes through :py:func:`make_pdu41002` rather than constructing the driver
    directly, so the sim-mode registration path is exercised on every test in
    this file rather than only in the one test that names it.
    """
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002()
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture()
def narrow_pdu():
    """A driver allowed to switch outlets 2 and 3 only.

    Separate from ``pdu`` because ``make_pdu41002`` defaults to every outlet in
    sim mode; the allowlist cannot be tested against a fixture that permits
    everything.
    """
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3))
    try:
        yield driver
    finally:
        driver.close()


# ---------------------------------------------------------------------------
# open() — transport selection and credentials
# ---------------------------------------------------------------------------


class TestOpen:
    def test_exactly_one_transport_is_required(self):
        """Both ``port`` and ``host`` is an error, not a silent preference.

        A driver that quietly picked one would make "which wire carried that
        mains switch" unanswerable from a run log.
        """
        with pytest.raises(PDU41002ValueError, match="exactly one"):
            CyberPowerPDU41002 = _driver_class()
            CyberPowerPDU41002.open(
                port="/dev/null", host="pdu-benchctrl", allowed_outlets=()
            )

    def test_neither_transport_is_also_an_error(self):
        """The same check catches the omission, not just the collision."""
        with pytest.raises(PDU41002ValueError, match="exactly one"):
            _driver_class().open(allowed_outlets=())

    def test_allowed_outlets_has_no_default(self):
        """Mandatory, not ``Optional`` with an "all" fallback.

        An allowlist with a permissive default is a denylist wearing a hat: a
        config typo would silently widen switching scope on the one device that
        can drop mains. ``TypeError`` here is the signature refusing, before
        any driver code runs.
        """
        with pytest.raises(TypeError):
            _driver_class().open(port="/dev/null")

    def test_a_missing_password_names_the_env_var(self):
        """Fails at ``open()``, naming the variable — not later as a timeout.

        Resolution happens before the link is opened, so this raises without
        touching ``/dev/null`` or waiting for a login prompt that will never
        arrive. Env is injected rather than monkeypatched so the test cannot
        pass because the developer happens to have the real password exported.
        """
        with pytest.raises(PDU41002AuthError, match="BENCHCTRL_PDU_PASSWORD"):
            _driver_class().open(
                port="/dev/null", allowed_outlets=(1,), env={}
            )

    def test_the_password_is_read_from_the_environment(self):
        """The supported production path: the secret lives in the environment
        of the process that talks to the device, never in config or on the
        wire."""
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        sim = SimulatedPDU41002(password="from-env")
        sim.start()
        try:
            pdu = _driver_class().open(
                port=sim.port,
                allowed_outlets=(1,),
                env={"BENCHCTRL_PDU_PASSWORD": "from-env"},
            )
            try:
                assert pdu.is_open
                assert sim.login_log == [("admin", True)]
            finally:
                pdu.close()
        finally:
            sim.close()

    def test_a_wrong_password_raises_auth_error(self, monkeypatch):
        """And it takes the whole login budget to say so, on purpose.

        The device answers a wrong password and a still-closing session with
        *identical* bytes, so the driver cannot classify — it retries until the
        budget runs out and only then concludes "credential". Scaled down here
        so the test costs a second rather than the real 75 s.
        """
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        monkeypatch.setattr(_driver_class(), "_LOGIN_TIMEOUT_S", 2.0)
        monkeypatch.setattr(_driver_class(), "_LOGIN_RETRY_S", 0.1)

        sim = SimulatedPDU41002(password="correct")
        sim.start()
        try:
            with pytest.raises(PDU41002AuthError, match="BENCHCTRL_PDU_PASSWORD"):
                _driver_class().open(
                    port=sim.port, allowed_outlets=(1,), password="wrong"
                )
            # More than one attempt: the driver has to retry, because on
            # hardware this same response also means "still busy".
            assert sim.refusal_count > 1
        finally:
            sim.close()

    def test_a_busy_device_refusing_a_correct_password_is_retried(self, monkeypatch):
        """The bug this shape hid: a *correct* password, refused, then accepted.

        Measured on firmware 1.3.4 — a serial login within ~15 s of a previous
        session closing prints dots, then "Login Failed", then nothing. It is
        byte-identical to a wrong password, and it clears on its own.

        The driver used to fall through to "the device did not answer" here,
        because no re-prompt follows the refusal, and so never retried. A bench
        that had just switched transports therefore could not reopen its own
        PDU. This asserts the recovery *and* that the recovery took a second
        attempt, since a driver that got lucky on the first would pass on the
        first assertion alone.
        """
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        monkeypatch.setattr(_driver_class(), "_LOGIN_RETRY_S", 0.1)

        sim = SimulatedPDU41002()
        sim.start()
        try:
            sim.refuse_next_logins = 2
            driver = _driver_class().open(
                port=sim.port, allowed_outlets=(1,), password=sim.password
            )
            try:
                assert driver.read_identity().model == "PDU41002"
                assert sim.refusal_count == 2
                # Every attempt used the right credential; the refusals were
                # the device being busy, not the password being wrong.
                assert [ok for _u, ok in sim.login_log] == [True, True, True]
            finally:
                driver.close()
        finally:
            sim.close()

    def test_the_transport_is_recorded(self, pdu):
        assert pdu.transport == "serial"

    def test_panic_outlets_must_be_a_subset_of_allowed(self):
        """A governor that may cut an outlet nobody authorised for switching is
        a hole in the allowlist, reachable only on trip — i.e. exactly when
        nobody is watching."""
        from benchctrl.sim.factories import make_pdu41002

        with pytest.raises(PDU41002ValueError, match="panic_outlets"):
            make_pdu41002(allowed_outlets=(1, 2), panic_outlets=(3,))

    def test_panic_outlets_defaults_to_empty(self, pdu):
        """Opt-in. The default posture is "do not move the contactors"."""
        assert pdu.panic_outlets == frozenset()


# ---------------------------------------------------------------------------
# Credential hygiene
# ---------------------------------------------------------------------------


class TestTheSecretDoesNotLeak:
    def test_the_password_is_absent_from_the_driver_repr(self):
        from benchctrl.sim.factories import make_pdu41002

        driver = make_pdu41002(sim={"password": "sentinel-pw-42"})
        try:
            assert "sentinel-pw-42" not in repr(driver)
        finally:
            driver.close()

    def test_the_password_is_absent_from_the_link_repr(self):
        from benchctrl.sim.factories import make_pdu41002

        driver = make_pdu41002(sim={"password": "sentinel-pw-42"})
        try:
            assert "sentinel-pw-42" not in repr(driver._link)
        finally:
            driver.close()

    def test_the_simulator_never_logs_the_password(self):
        """``login_log`` records ``(user, accepted)`` deliberately.

        A simulator that logged the password would put it in every failing
        test's output, which is how secrets end up in CI logs.
        """
        from benchctrl.sim.factories import make_pdu41002

        driver = make_pdu41002(sim={"password": "sentinel-pw-42"})
        try:
            assert driver._benchctrl_sim.login_log == [("admin", True)]
            assert "sentinel-pw-42" not in repr(driver._benchctrl_sim.login_log)
        finally:
            driver.close()

    def test_config_masks_a_password_in_open_kwargs(self):
        """``DeviceConfig.to_dict()`` is what gets written back to a saved
        config file, and it emitted ``open`` verbatim before this change.

        The supported path is the env var, so this is belt-and-braces — but a
        password that reaches config should not also survive a round trip into
        a file on disk.
        """
        from benchctrl.config import MASKED, DeviceConfig

        cfg = DeviceConfig(
            open={"host": "pdu-benchctrl", "password": "sentinel-pw-42"}
        )
        emitted = cfg.to_dict()
        assert emitted["open"]["password"] == MASKED
        assert "sentinel-pw-42" not in repr(emitted)
        # Non-secret keys are untouched — masking must not eat the config.
        assert emitted["open"]["host"] == "pdu-benchctrl"

    def test_masking_leaves_a_none_password_alone(self):
        """``None`` is not a secret, and turning it into ``"***"`` would make a
        config that meant "read the env var" round-trip into one that means
        "the password is literally three asterisks"."""
        from benchctrl.config import DeviceConfig

        cfg = DeviceConfig(open={"password": None})
        assert cfg.to_dict()["open"]["password"] is None


# ---------------------------------------------------------------------------
# Reads
# ---------------------------------------------------------------------------


class TestReads:
    def test_identity(self, pdu):
        info = pdu.read_identity()
        assert isinstance(info, PDU41002Info)
        assert info.model == "PDU41002"
        assert info.firmware_version
        assert info.mac_address

    def test_device_status(self, pdu):
        status = pdu.read_device_status()
        assert isinstance(status, PDU41002Status)
        assert status.voltage_V == pytest.approx(121.7)
        assert status.frequency_Hz == pytest.approx(60.0)

    def test_power_factor_is_none_at_zero_load(self, pdu):
        """The device prints ``----``, not ``0``.

        Coercing that to 0.0 would be a plausible wrong number: a power factor
        of zero means a purely reactive load, which is a real and different
        condition from "nothing is drawing current".
        """
        assert pdu.read_device_status().power_factor is None

    def test_scalar_measurements_agree_with_the_status_read(self, pdu):
        status = pdu.read_device_status()
        assert pdu.measure_load_A() == pytest.approx(status.load_A)
        assert pdu.measure_voltage_V() == pytest.approx(status.voltage_V)
        assert pdu.measure_frequency_Hz() == pytest.approx(status.frequency_Hz)

    def test_outlet_states_covers_every_outlet(self, pdu):
        states = pdu.outlet_states()
        assert sorted(states) == list(range(1, 9))
        assert all(isinstance(v, bool) for v in states.values())

    def test_one_outlet_state_agrees_with_the_table(self, pdu):
        """The single-index read and the table read must not diverge — they are
        different CLI verbs (``oltsta index N show`` vs ``oltsta show``) with
        different output layouts, so agreement is evidence both parsers are
        right rather than one being consistently wrong."""
        table = pdu.outlet_states()
        for index, expected in table.items():
            assert pdu.outlet_state(index) is expected

    def test_outlet_names(self, pdu):
        assert pdu.outlet_name(1)

    def test_outlet_config_reports_the_configured_delays(self, pdu):
        """The delays are *configurable*, which is why read-back verification
        has to budget them rather than using a fixed settling time."""
        cfg = pdu.read_outlet_config()
        assert sorted(cfg) == list(range(1, 9))
        assert cfg[1].on_delay_s == 3
        assert cfg[1].off_delay_s == 3
        assert cfg[1].reboot_duration_s == 5

    def test_outlet_config_is_cached_until_refreshed(self, pdu):
        """Cached because it is read on every verified switch, and an extra
        round trip per switch on a 9600 baud console is not free."""
        sim = pdu._benchctrl_sim
        pdu.read_outlet_config()
        before = len([c for c in sim.command_log if c.startswith("oltcfg")])
        pdu.read_outlet_config()
        assert len([c for c in sim.command_log if c.startswith("oltcfg")]) == before
        pdu.read_outlet_config(refresh=True)
        assert len([c for c in sim.command_log if c.startswith("oltcfg")]) > before

    def test_reads_are_repeatable(self, pdu):
        """Ten consecutive round trips with no resync in between.

        A CLI engine that left a byte in the buffer would drift, and the
        symptom would be an off-by-one response — the Nth read answering the
        (N-1)th command — which is far worse than a clean failure.
        """
        first = pdu.read_identity()
        for _ in range(9):
            assert pdu.read_identity() == first


# ---------------------------------------------------------------------------
# Index coercion — the structural half of "no aggregate targeting"
# ---------------------------------------------------------------------------


class TestIndexCoercion:
    @pytest.mark.parametrize("bad", ["all", "b1", "b2", "1", "guest 1"])
    def test_a_string_index_is_rejected(self, pdu, bad):
        """``all``, ``b1`` and ``b2`` are real, accepted device targets, and
        ``oltctrl index all act off`` de-powers the whole bench in one line.

        No signature in this driver accepts a string, so those targets are
        not expressible rather than merely discouraged.
        """
        with pytest.raises(PDU41002ValueError):
            pdu.outlet_state(bad)

    @pytest.mark.parametrize("bad", [(1, 2), [1, 2], {1, 2}])
    def test_a_collection_index_is_rejected(self, pdu, bad):
        with pytest.raises(PDU41002ValueError):
            pdu.outlet_state(bad)

    def test_bool_is_rejected_before_int(self, pdu):
        """``bool`` is a subclass of ``int``, so ``isinstance(True, int)`` is
        true and ``outlet_state(True)`` would silently mean outlet 1.

        On a device that switches mains, "the flag I passed was interpreted as
        an outlet number" is not an acceptable failure mode. The bool check has
        to come *first*, which is a property of ordering that a plain type
        check cannot express — hence the explicit test.
        """
        with pytest.raises(PDU41002ValueError, match="bool"):
            pdu.outlet_state(True)
        with pytest.raises(PDU41002ValueError, match="bool"):
            pdu.outlet_state(False)

    @pytest.mark.parametrize("bad", [0, -1, 9, 99])
    def test_out_of_range_is_rejected_client_side(self, pdu, bad):
        """Rejected before any bytes are written, so a typo cannot reach the
        device at all."""
        sim = pdu._benchctrl_sim
        before = list(sim.command_log)
        with pytest.raises(PDU41002ValueError):
            pdu.outlet_state(bad)
        assert sim.command_log == before, "a rejected index still sent bytes"

    def test_a_float_index_is_rejected(self, pdu):
        with pytest.raises(PDU41002ValueError):
            pdu.outlet_state(1.0)


# ---------------------------------------------------------------------------
# The allowlist
# ---------------------------------------------------------------------------


class TestAllowlist:
    def test_the_allowlist_is_reported_as_configured(self, narrow_pdu):
        assert narrow_pdu.allowed_outlets == frozenset({2, 3})

    def test_reading_is_not_gated_by_the_allowlist(self, narrow_pdu):
        """Deliberate: the allowlist governs *switching*, not observation.

        Refusing to read outlet 1 because it may not be switched would make
        the driver unable to report the state of the bench it is protecting,
        and would push operators toward a wider allowlist to get visibility —
        the opposite of the intent.
        """
        assert isinstance(narrow_pdu.outlet_state(1), bool)
        assert sorted(narrow_pdu.outlet_states()) == list(range(1, 9))

    def test_the_policy_check_refuses_a_disallowed_outlet(self, narrow_pdu):
        """Exercises the enforcement path directly, since no public mutator
        exists yet in this release.

        Testing it now rather than with the first switching method means the
        refusal is proven *before* anything can act on it, instead of the
        allowlist arriving untested alongside the code it guards.
        """
        with pytest.raises(PDU41002PolicyError, match="not in allowed_outlets"):
            narrow_pdu._require_allowed(1)

    def test_the_policy_check_permits_an_allowed_outlet(self, narrow_pdu):
        narrow_pdu._require_allowed(2)

    def test_an_empty_allowlist_permits_no_switching(self):
        """The most restrictive configuration has to be expressible — it is the
        right one for a PDU that is only being metered."""
        from benchctrl.sim.factories import make_pdu41002

        driver = make_pdu41002(allowed_outlets=())
        try:
            assert driver.allowed_outlets == frozenset()
            for index in range(1, 9):
                with pytest.raises(PDU41002PolicyError):
                    driver._require_allowed(index)
        finally:
            driver.close()

    def test_the_allowlist_cannot_be_widened_through_the_property(self, pdu):
        """``frozenset``, so a caller cannot ``.add()`` an outlet into scope."""
        assert isinstance(pdu.allowed_outlets, frozenset)
        assert isinstance(pdu.panic_outlets, frozenset)


# ---------------------------------------------------------------------------
# What the driver never sends
# ---------------------------------------------------------------------------


class TestForbiddenCommands:
    """The command log is the evidence. These assertions are about the bytes
    that reach the device, not about the driver's intentions."""

    def test_no_read_ever_emits_an_aggregate_or_trap_verb(self, pdu):
        """Exercises the whole read surface, then inspects everything sent.

        ``menumode`` is a one-way trap: it switches the session to a menu
        interface and returning to the CLI needs a full logout/login, so every
        parser would then fail while the link still looked healthy.
        ``console telnet enable`` silently *disables SSH*, killing the network
        transport from underneath a run.

        ``oltcfg index all show`` is the one legitimate use of ``all`` in the
        driver — it is a read of every outlet's delays, not a control verb — so
        the assertion is scoped to ``oltctrl``, where ``all`` would switch.
        """
        pdu.read_identity()
        pdu.read_device_status()
        pdu.outlet_states()
        pdu.outlet_state(1)
        pdu.outlet_name(1)
        pdu.read_outlet_config()
        pdu.measure_load_A()
        pdu.measure_voltage_V()
        pdu.measure_frequency_Hz()

        log = pdu._benchctrl_sim.command_log
        assert log, "the read surface sent nothing — the test proves nothing"
        for sent in log:
            lowered = sent.lower()
            assert "menumode" not in lowered
            assert "telnet" not in lowered
            assert "guest" not in lowered
            assert "coldsta" not in lowered
            if lowered.startswith("oltctrl"):
                for aggregate in (" all", " b1", " b2"):
                    assert aggregate not in lowered

    def test_no_read_emits_a_control_verb_at_all(self, pdu):
        """This release cannot switch anything, and that is worth asserting
        rather than assuming: a read implemented via a control verb (say,
        ``oltctrl index N act cancel`` to observe a pending command) would
        quietly make the read surface a write surface."""
        pdu.read_identity()
        pdu.outlet_states()
        pdu.read_outlet_config()
        assert not [c for c in pdu._benchctrl_sim.command_log if "oltctrl" in c]

    def test_the_driver_sends_no_interrupt_character(self, pdu):
        r"""``\x03`` is **not** an interrupt on this CLI.

        Measured on hardware: it is echoed and consumed as part of the command
        (producing ``Command not found`` at a constant column regardless of
        command length), and it does not clear a dirty input line either. A
        bare CR does. Prefixing commands with it broke every single command in
        the driver's first end-to-end run, and the simulator caught it by
        reproducing the hardware rather than agreeing with the driver.
        """
        pdu.read_identity()
        pdu.outlet_states()
        for sent in pdu._benchctrl_sim.command_log:
            assert "\x03" not in sent


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class TestErrorShapes:
    @pytest.mark.parametrize("shape", ["command", "index", "parameter"])
    def test_each_of_the_three_shapes_raises(self, pdu, shape):
        """Three shapes, not the two the manual documents.

        Keying detection on the caret alone misses the index shape (no caret,
        no ``Error :``); keying on ``Error :`` alone misses it too. Both
        signals are needed, and each shape gets its own case here so a
        regression names which one broke.
        """
        pdu._benchctrl_sim.inject_error(shape)
        with pytest.raises(PDU41002CommandError):
            pdu.read_identity()

    def test_the_error_carries_the_offending_command(self, pdu):
        """Without the command, a caret column is meaningless — and the caret
        is the only thing the device tells you about *where* it failed."""
        pdu._benchctrl_sim.inject_error("command")
        with pytest.raises(PDU41002CommandError) as exc:
            pdu.read_identity()
        assert exc.value.command == "sys show"

    def test_the_session_survives_an_error(self, pdu):
        """The unknown-verb shape carries a ~30-line verb dump.

        A read that stopped at the first blank line would leave most of that
        dump in the buffer, and the *next* command would parse the tail of the
        previous error. So recovering cleanly after an error is a test of
        prompt-terminated reading, not of error handling.
        """
        pdu._benchctrl_sim.inject_error("command")
        with pytest.raises(PDU41002CommandError):
            pdu.read_identity()
        assert pdu.read_identity().model == "PDU41002"

    def test_the_session_survives_several_errors_in_a_row(self, pdu):
        for shape in ("command", "index", "parameter"):
            pdu._benchctrl_sim.inject_error(shape)
            with pytest.raises(PDU41002CommandError):
                pdu.read_device_status()
        assert pdu.read_device_status().frequency_Hz == pytest.approx(60.0)

    def test_every_exception_descends_from_the_driver_base(self):
        """So ``except PDU41002Error`` is a complete catch, and the dual
        inheritance on the Value/Timeout ones keeps ``except ValueError`` /
        ``except TimeoutError`` working for callers that never heard of this
        driver."""
        from benchctrl.drivers.cyberpower_pdu41002 import (
            PDU41002ConnectionError,
            PDU41002Error,
            PDU41002ProtocolError,
            PDU41002SessionError,
            PDU41002TimeoutError,
        )

        for cls in (
            PDU41002ConnectionError,
            PDU41002ProtocolError,
            PDU41002TimeoutError,
            PDU41002ValueError,
            PDU41002AuthError,
            PDU41002SessionError,
            PDU41002PolicyError,
            PDU41002CommandError,
        ):
            assert issubclass(cls, PDU41002Error)
        assert issubclass(PDU41002ValueError, ValueError)
        assert issubclass(PDU41002TimeoutError, TimeoutError)
        assert issubclass(PDU41002PolicyError, PDU41002Error)


# ---------------------------------------------------------------------------
# Session handling
# ---------------------------------------------------------------------------


class TestSession:
    def test_an_idle_logout_mid_sequence_is_recovered(self, pdu):
        """The nastiest failure mode this device has.

        After the idle timeout, the CLI is back at the login prompt — and it
        consumes the next command **as a username**, answering with a password
        prompt rather than an error. So a switch command would be silently
        swallowed while the operator believed it ran.

        Driven by ``force_logout()`` rather than a wall-clock wait, so the test
        is deterministic and takes no three minutes.
        """
        assert pdu.read_identity().model == "PDU41002"
        pdu._benchctrl_sim.force_logout()
        assert pdu._benchctrl_sim.authenticated is False
        assert pdu.read_identity().model == "PDU41002"
        assert pdu._benchctrl_sim.authenticated is True

    def test_the_swallowed_command_is_visible_in_the_login_log(self, pdu):
        """Proves it re-authenticated, and shows *exactly* how the hazard bites.

        The log reads ``[("admin", True), ("sys show", False), ("admin", True)]``
        — the middle entry is the driver's own in-flight command being consumed
        as a **username** by a logged-out CLI. That is the failure this whole
        code path exists for: the device answers a command with a password
        prompt rather than an error, so a switch would be silently swallowed
        while the operator believed it ran.

        Asserting the middle entry, not just the two logins, is what makes this
        a test of the mechanism rather than of the simulator having stayed
        logged in.
        """
        pdu.read_identity()
        pdu._benchctrl_sim.force_logout()
        pdu.read_identity()
        assert pdu._benchctrl_sim.login_log == [
            ("admin", True),
            ("sys show", False),
            ("admin", True),
        ]

    def test_a_device_that_never_answers_is_not_reported_as_a_bad_password(
        self, monkeypatch
    ):
        """A silent device and a rejected credential are different diagnoses.

        Both look the same from here — the prompt does not arrive — and they
        send an operator to opposite places: one to the device, one to a
        password that was correct all along. The driver reported the silent case
        as ``PDU41002AuthError`` until this test existed, which is the same
        misdiagnosis the single-session hangup causes, and it was found on
        hardware rather than here: a login that started late got a post-password
        read too short to ever see the prompt, and the failure named the
        environment variable.

        ``login_log`` is what makes this a test of the distinction rather than
        of the timeout: the credential the device stayed silent about was the
        *right* one, so nothing about it justified an auth error.
        """
        from benchctrl.drivers.cyberpower_pdu41002 import (
            CyberPowerPDU41002,
            PDU41002TimeoutError,
        )
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        # Scaled down, not stubbed out: the *real* budget is 30 s + a 10 s
        # floor, and waiting that out on every run is what gets a test deleted.
        # Both are scaled by the same factor so the relationship under test —
        # the floor outliving the budget — is preserved.
        monkeypatch.setattr(CyberPowerPDU41002, "_LOGIN_TIMEOUT_S", 3.0)
        monkeypatch.setattr(CyberPowerPDU41002, "_AUTH_WAIT_S", 1.0)

        sim = SimulatedPDU41002()
        sim.start()
        try:
            sim.stall_next_auth = True
            with pytest.raises(PDU41002TimeoutError) as exc:
                CyberPowerPDU41002.open(
                    port=sim.port,
                    allowed_outlets=(1,),
                    password=sim.password,
                )
            assert sim.login_log == [("admin", True)], (
                "the password the device went silent about was correct"
            )
            # Naming the variable here is precisely the wrong advice.
            assert "BENCHCTRL_PDU_PASSWORD" not in str(exc.value)
            assert "did not answer" in str(exc.value)
        finally:
            sim.close()

    @pytest.mark.parametrize("wording", ["received_disconnect", "connection_to"])
    def test_an_ssh_idle_logout_is_a_connection_error_not_a_timeout(
        self, pdu, wording
    ):
        """The network half of the idle timeout, where recovery differs.

        Serial and ssh do not fail the same way and must not be reported the
        same way. Serial keeps the port and lands on ``Login Name :``, so
        ``_cmd`` re-authenticates in place (the test above). Over ssh the device
        closes the connection at ~180 s and the client process exits — there is
        nothing left to re-authenticate on, and the only thing that works is
        opening again.

        Until the client's disconnect notice was recognised, this surfaced as
        "no prompt after 'sys show' within 12.0s": a timeout, which reads as a
        slow device and invites a retry that can never succeed. Found by the
        hardware suite, where the 130 bytes of ssh notice matched no marker.

        **Parametrised over both of ssh's wordings, and that is the whole
        point.** Fixing the timeout with only the first one left the second
        matching ``_HANGUP_MARKERS``, so an idled-out session was reported as
        ``PDU41002SessionError``: "another session is logged in — send 'exit' on
        it". Doubly wrong, because nothing else was logged in and that advice
        cannot work on a link that no longer exists. One wording passed here
        while the other failed on hardware, which is exactly the gap a
        single-shape test leaves open.

        The message is asserted, not just the type, because the recovery
        instruction is the entire difference between the two cases.
        """
        from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ConnectionError

        assert pdu.read_identity().model == "PDU41002"
        pdu._benchctrl_sim.drop_ssh_session(wording=wording)

        with pytest.raises(PDU41002ConnectionError) as exc:
            pdu.read_identity()
        assert "reopen" in str(exc.value)
        # And it must not be reported as the device being slow...
        assert "no prompt" not in str(exc.value)
        # ...nor as a session someone else is holding, whose advice ("send
        # 'exit' on it") is unfollowable here.
        assert "exit" not in str(exc.value)

    def test_close_sends_exit(self, pdu):
        """Required, not polite.

        This PDU allows one CLI session at a time **across all transports**,
        and it does not drop that session when the connection closes. A driver
        that merely closed its port would leave the device occupied, and every
        later SSH attempt would die *after* a successful login — a symptom that
        reads as a wrong password.
        """
        pdu.read_identity()
        pdu.close()
        assert "exit" in pdu._benchctrl_sim.command_log

    def test_close_is_idempotent(self, pdu):
        pdu.close()
        pdu.close()
        assert pdu.is_open is False

    def test_close_does_not_change_outlet_state(self, pdu):
        """Unlike every other driver in the tree, whose ``close()`` disables
        outputs. Here "safe" is *don't move the contactors*: cutting mains on
        teardown would de-power a DUT mid-measurement."""
        before = pdu.outlet_states()
        pdu.close()
        sim = pdu._benchctrl_sim
        assert {i: sim.outlet_state[i] for i in before} == before

    def test_the_context_manager_closes_but_does_not_switch(self):
        from benchctrl.sim.factories import make_pdu41002

        driver = make_pdu41002()
        sim = driver._benchctrl_sim
        with driver as pdu:
            before = pdu.outlet_states()
        assert driver.is_open is False
        assert "exit" in sim.command_log
        assert {i: sim.outlet_state[i] for i in before} == before

    def test_a_command_after_close_raises_rather_than_hanging(self, pdu):
        from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ConnectionError

        pdu.close()
        with pytest.raises(PDU41002ConnectionError):
            pdu.read_identity()


# ---------------------------------------------------------------------------
# Dispatch classification — the guard on the method names
# ---------------------------------------------------------------------------


class TestDispatchClassification:
    """``agent/dispatch.py`` decides which methods need a writer claim purely
    from name prefixes, with **no driver-declared override**.

    That makes method naming a safety constraint on this driver rather than a
    style question, and this class is the guard on it. It is the single most
    important test in the file.
    """

    @pytest.fixture()
    def surface(self, pdu):
        from benchctrl.agent.dispatch import introspect

        return introspect(pdu, "cyberpower_pdu41002")

    @pytest.mark.parametrize(
        "name",
        [
            "read_identity",
            "read_device_status",
            "read_outlet_config",
            "outlet_state",
            "outlet_states",
            "outlet_name",
            "measure_load_A",
            "measure_voltage_V",
            "measure_frequency_Hz",
        ],
    )
    def test_no_read_requires_a_writer_claim(self, surface, name):
        """A read misclassified as a mutator would make *metering* need a
        writer claim, so a monitoring client could not watch a bench it does
        not control.

        Note ``read_outlet_config`` starts with ``read_``, which is **not** a
        mutator prefix — ``write_`` is. Easy to misread, hence the explicit
        case.
        """
        assert name in surface.methods, f"{name} is not remotely reachable"
        assert name not in surface.mutators, f"{name} wrongly needs a writer claim"

    def test_every_reachable_method_is_classified_deliberately(self, surface):
        """Catches a *new* method arriving without anyone thinking about its
        prefix — the actual failure mode, since nobody forgets to classify a
        method they are consciously naming.

        If this fails, decide whether the new method mutates and add it to the
        appropriate list here. Do not delete the assertion.
        """
        expected_reads = {
            "read_identity",
            "read_device_status",
            "read_outlet_config",
            "outlet_state",
            "outlet_states",
            "outlet_name",
            "measure_load_A",
            "measure_voltage_V",
            "measure_frequency_Hz",
        }
        expected_mutators = {
            "set_outlet_state",
            "reset_outlet",
            "clear_outlet_command",
        }
        unclassified = set(surface.methods) - expected_reads - expected_mutators
        assert not unclassified, (
            f"new method(s) {sorted(unclassified)} — is each a read or a "
            f"mutator? Mutators must carry an existing dispatch prefix "
            f"(set_/reset/clear_/trigger), or they are remotely callable "
            f"without a writer claim."
        )

    def test_every_switching_method_requires_a_writer_claim(self, surface):
        """Exact equality, not ``issubset``.

        The set is pinned in both directions on purpose. A missing entry means a
        contactor can be moved without a writer claim; an *extra* entry means a
        method that can move a contactor arrived without being reviewed as such.
        Either way this test is the one that should fail.
        """
        assert surface.mutators == frozenset(
            {"set_outlet_state", "reset_outlet", "clear_outlet_command"}
        )

    def test_the_switching_methods_are_actually_reachable(self, surface):
        """A mutator absent from ``methods`` is not protected — it is simply
        unreachable, which would make the test above pass for the wrong
        reason."""
        for name in ("set_outlet_state", "reset_outlet", "clear_outlet_command"):
            assert name in surface.methods

    def test_the_rejected_switching_names_would_have_bypassed_the_claim(self):
        """Pins the naming decision itself, not the current surface.

        ``outlet_on()`` reads naturally and would be **remotely callable
        without a writer claim** — mains switching bypassing the claim gate.
        ``set_outlet_state()`` matches ``set_``. Keeping this after switching
        landed is the point: it documents why the natural name was rejected, so
        a later "cleanup" renaming toward it fails here first.
        """
        from benchctrl.agent.dispatch import _MUTATOR_PREFIXES

        def is_mutator(name: str) -> bool:
            return any(name.startswith(p) for p in _MUTATOR_PREFIXES)

        assert is_mutator("set_outlet_state")
        assert is_mutator("reset_outlet")
        assert is_mutator("clear_outlet_command")
        assert not is_mutator("outlet_on"), (
            "outlet_on would switch mains without a writer claim"
        )
        assert not is_mutator("outlet_off")
        # And the reads must stay reads, which is why "outlet_" was never
        # added to the prefix list.
        assert not is_mutator("outlet_state")
        assert not is_mutator("outlet_states")


# ---------------------------------------------------------------------------
# Switching — the methods that move real contactors
# ---------------------------------------------------------------------------


class TestSwitching:
    """Stage 2. Every test here would move mains on hardware.

    The simulator honours the same per-outlet delay the device reports through
    ``oltcfg`` (3 s as shipped), so these exercise the *derived* read-back
    budget rather than a hardcoded sleep.
    """

    def test_switching_an_outlet_off_reports_the_read_back_state(self, pdu):
        assert pdu.outlet_state(4) is True
        assert pdu.set_outlet_state(4, False) is False
        assert pdu.outlet_state(4) is False

    def test_switching_an_outlet_on_reports_the_read_back_state(self, pdu):
        pdu.set_outlet_state(4, False)
        assert pdu.set_outlet_state(4, True) is True
        assert pdu.outlet_state(4) is True

    def test_the_emitted_command_is_a_single_outlet_oltctrl(self, pdu):
        pdu.set_outlet_state(5, False)
        sent = [c for c in pdu._benchctrl_sim.command_log if c.startswith("oltctrl")]
        assert sent == ["oltctrl index 5 act off"]

    def test_switching_returns_the_devices_answer_not_the_request(self, pdu):
        """The distinction the whole design turns on.

        ``oltctrl`` acknowledges nothing — its response is byte-identical
        whether or not the contactor moved — so a method that returned its own
        argument would report success for a switch that never happened. Here the
        simulator is told to ignore the switch, and the call must fail rather
        than echo the request back.
        """
        sim = pdu._benchctrl_sim
        sim.outlet_state[6] = True
        sim.ignore_switches = True
        with pytest.raises(PDU41002ProtocolError) as excinfo:
            pdu.set_outlet_state(6, False)
        assert "still reads" in str(excinfo.value)

    def test_a_lying_device_is_caught_rather_than_trusted(self, pdu):
        """The same guarantee stated from the operator's side: if the outlet
        does not reach the requested state, the caller learns it. Silence here
        would mean an operator believing mains was cut when it was not."""
        sim = pdu._benchctrl_sim
        sim.ignore_switches = True
        with pytest.raises(PDU41002ProtocolError):
            pdu.set_outlet_state(2, False)
        assert sim.outlet_state[2] is True

    def test_verify_false_skips_the_read_back_and_says_so(self, pdu, caplog):
        """``verify=False`` is supported but must be loud: the returned value is
        the *request*, and nothing has confirmed it."""
        import logging

        sim = pdu._benchctrl_sim
        sim.ignore_switches = True
        with caplog.at_level(logging.WARNING):
            assert pdu.set_outlet_state(3, False, verify=False) is False
        # The outlet never moved, and the call still returned the request —
        # which is exactly why the warning has to be there.
        assert sim.outlet_state[3] is True
        assert any("unconfirmed" in r.getMessage() for r in caplog.records)

    def test_no_read_back_happens_when_verify_is_false(self, pdu):
        """Not just cosmetic: the point of ``verify=False`` is to skip the
        traffic, so a version that still polled would be misleading about
        cost as well as about certainty."""
        pdu.read_outlet_config()  # warm the cache so it is not counted below
        sim = pdu._benchctrl_sim
        sim.command_log.clear()
        pdu.set_outlet_state(3, True, verify=False)
        assert not [c for c in sim.command_log if c.startswith("oltsta")]

    def test_delayed_switching_uses_the_delay_verbs(self, pdu):
        pdu.set_outlet_state(7, False, delayed=True)
        sent = [c for c in pdu._benchctrl_sim.command_log if c.startswith("oltctrl")]
        assert sent == ["oltctrl index 7 act delayoff"]

    def test_a_non_bool_on_argument_is_refused(self, pdu):
        """``set_outlet_state(3, "off")`` is truthy, so a permissive signature
        would **energise** an outlet the caller was trying to cut. That is the
        worst available failure: the argument names the intent and the device
        does the opposite."""
        with pytest.raises(PDU41002ValueError):
            pdu.set_outlet_state(3, "off")
        assert not [
            c for c in pdu._benchctrl_sim.command_log if c.startswith("oltctrl")
        ]

    @pytest.mark.parametrize("bad", [1, 0, None, "on"])
    def test_only_a_real_bool_energises_or_cuts(self, pdu, bad):
        """``1`` and ``0`` are rejected too. They read as "obviously on/off",
        but accepting them means accepting ``"on"`` is a mistake away."""
        with pytest.raises(PDU41002ValueError):
            pdu.set_outlet_state(3, bad)

    def test_reset_outlet_emits_reboot_and_claims_nothing(self, pdu):
        """``reset_outlet`` returns ``None`` deliberately: the outlet ends where
        it started, so no read-back could prove the cut happened."""
        assert pdu.reset_outlet(4) is None
        sent = [c for c in pdu._benchctrl_sim.command_log if c.startswith("oltctrl")]
        assert sent == ["oltctrl index 4 act reboot"]

    def test_a_reboot_actually_cuts_power_before_restoring_it(self, pdu):
        """The transient off is the *point* of a reboot, and it is the thing a
        read-back cannot see afterwards.

        This is why the simulator queues multiple pending transitions per
        outlet: with one slot per outlet the off was overwritten by the on, so a
        reboot became indistinguishable from doing nothing — and this assertion
        would have passed against a simulator that never cut the outlet.
        """
        sim = pdu._benchctrl_sim
        assert sim.outlet_state[4] is True
        pdu.reset_outlet(4)

        # Step the sim's clock past the off delay but not the reboot duration.
        sim.on_tick(sim.elapsed_s + sim.off_delay_s + 0.1)
        assert sim.outlet_state[4] is False, "reboot never cut the outlet"

        # ...and past the reboot duration, where it comes back on its own.
        sim.on_tick(sim.elapsed_s + sim.off_delay_s + sim.reboot_duration_s + 0.1)
        assert sim.outlet_state[4] is True, "reboot never restored the outlet"

    def test_clear_outlet_command_cancels_a_pending_switch(self, pdu):
        sim = pdu._benchctrl_sim
        assert sim.outlet_state[5] is True
        pdu.set_outlet_state(5, False, delayed=True, verify=False)
        pdu.clear_outlet_command(5)
        # Well past the delay: the cancelled switch must never land.
        sim.on_tick(sim.elapsed_s + sim.off_delay_s + 5.0)
        assert sim.outlet_state[5] is True
        sent = [c for c in sim.command_log if c.startswith("oltctrl")]
        assert sent[-1] == "oltctrl index 5 act cancel"

    def test_cancelling_without_the_cancel_would_have_switched(self, pdu):
        """The control for the test above. Without it, ``clear_outlet_command``
        could be a no-op and the cancellation test would still pass, because
        nothing would prove the pending switch was ever going to fire."""
        sim = pdu._benchctrl_sim
        pdu.set_outlet_state(5, False, delayed=True, verify=False)
        sim.on_tick(sim.elapsed_s + sim.off_delay_s + 5.0)
        assert sim.outlet_state[5] is False

    def test_the_verify_budget_comes_from_the_devices_configured_delay(self, pdu):
        """Derived, not hardcoded — the delay is operator-configurable per
        outlet, and the capture notes record that a budget sized from the
        observed contactor settle (~1.5 s) flakes on a unit whose ``td_on`` was
        raised."""
        sim = pdu._benchctrl_sim
        sim.on_delay_s = 11
        sim.off_delay_s = 17
        pdu.read_outlet_config(refresh=True)
        margin = type(pdu)._VERIFY_MARGIN_S
        assert pdu._verify_budget_s(1, True) == pytest.approx(11 + margin)
        assert pdu._verify_budget_s(1, False) == pytest.approx(17 + margin)

    def test_a_longer_configured_delay_is_waited_out(self, pdu):
        """The budget is not merely reported — a switch that settles late still
        succeeds, which is the behaviour the derivation exists for."""
        sim = pdu._benchctrl_sim
        sim.off_delay_s = 1
        pdu.read_outlet_config(refresh=True)
        assert pdu.set_outlet_state(6, False) is False

    def test_an_unreadable_config_still_allows_the_switch(self, pdu):
        """The budget is advisory. Failing the switch because an *advisory*
        read failed would be worse than using the margin alone — the command
        has already been emitted by then, so refusing would report failure for
        a switch that did happen."""
        margin = type(pdu)._VERIFY_MARGIN_S

        def boom(*a, **k):
            raise PDU41002ProtocolError("oltcfg unreadable")

        pdu.read_outlet_config = boom
        assert pdu._verify_budget_s(1, True) == pytest.approx(margin)


class TestSwitchingAllowlist:
    """The allowlist is the primary safety control, and it applies to *every*
    mutator. One unguarded method would make it decorative."""

    @pytest.mark.parametrize(
        "call",
        [
            lambda p: p.set_outlet_state(5, False),
            lambda p: p.set_outlet_state(5, True),
            lambda p: p.reset_outlet(5),
            lambda p: p.clear_outlet_command(5),
        ],
        ids=["set_off", "set_on", "reset", "clear"],
    )
    def test_every_mutator_refuses_an_outlet_outside_the_allowlist(
        self, narrow_pdu, call
    ):
        with pytest.raises(PDU41002PolicyError):
            call(narrow_pdu)
        assert not [
            c
            for c in narrow_pdu._benchctrl_sim.command_log
            if c.startswith("oltctrl")
        ], "a refused call still reached the device"

    def test_an_allowed_outlet_still_switches(self, narrow_pdu):
        """Otherwise the refusals above would pass against a driver that
        refused everything."""
        assert narrow_pdu.set_outlet_state(2, False) is False

    def test_the_refusal_happens_before_any_byte_is_written(self, narrow_pdu):
        """Ordering matters: a policy check *after* the write would refuse the
        caller while the contactor had already moved."""
        sim = narrow_pdu._benchctrl_sim
        sim.command_log.clear()
        with pytest.raises(PDU41002PolicyError):
            narrow_pdu.set_outlet_state(8, False)
        assert sim.command_log == []
        assert sim.outlet_state[8] is True


class TestForbiddenSwitchingCommands:
    """The structural guarantees, tested against the emitted bytes and against
    the guard directly."""

    def test_no_mutator_can_emit_an_aggregate_target(self, pdu):
        """Exercises every switching path, then inspects everything sent.

        ``oltctrl index all act off`` is one well-formed line that de-powers the
        entire unit, and the device answers it with the same blank re-prompt a
        legitimate switch gets.
        """
        pdu.set_outlet_state(1, False)
        pdu.set_outlet_state(1, True)
        pdu.set_outlet_state(2, False, delayed=True, verify=False)
        pdu.clear_outlet_command(2)
        pdu.reset_outlet(3)

        sent = [c for c in pdu._benchctrl_sim.command_log if c.startswith("oltctrl")]
        assert sent, "no switching commands were emitted — the test proves nothing"
        for command in sent:
            lowered = command.lower()
            for forbidden in (" all", " b1", " b2", "guest", "menumode"):
                assert forbidden not in lowered, f"{command!r} contains {forbidden!r}"

    def test_every_emitted_switch_matches_the_whitelist(self, pdu):
        """The whitelist is what the driver actually applies, so assert the
        emitted bytes against it rather than against a second hand-written
        pattern that could drift from it."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _SAFE_OLTCTRL_RE

        pdu.set_outlet_state(1, False)
        pdu.reset_outlet(1)
        pdu.clear_outlet_command(1)
        for command in pdu._benchctrl_sim.command_log:
            if command.startswith("oltctrl"):
                assert _SAFE_OLTCTRL_RE.match(command), f"{command!r} escaped the guard"

    @pytest.mark.parametrize(
        "command",
        [
            "oltctrl index all act off",
            "oltctrl index b1 act off",
            "oltctrl index b2 act off",
            "oltctrl index 1 act off guest 1",
            "oltctrl guest 1 index 1 act off",
            "menumode",
            "console telnet enable",
            "oltctrl index 1 act off; oltctrl index 2 act off",
            "oltctrl index 1 act",
            "oltctrl index 1 act bogus",
            "oltctrl index -1 act off",
            "oltctrl index 1 act off\roltctrl index all act off",
            " oltctrl index 1 act off",
            "OLTCTRL INDEX ALL ACT OFF",
        ],
    )
    def test_the_guard_rejects_what_the_methods_cannot_construct(self, command):
        """Tests the guard directly, with strings the driver's own methods
        cannot produce.

        That is the whole reason ``_assert_safe_command`` is a module-level
        function. Driving it only through the methods would prove the argument
        validation works — which is already tested — and prove nothing about
        whether the guard catches a *rendering* bug, which is the failure it
        exists for.
        """
        from benchctrl.drivers.cyberpower_pdu41002.driver import _assert_safe_command

        with pytest.raises(PDU41002ValueError):
            _assert_safe_command(command)

    @pytest.mark.parametrize(
        "action", ["on", "off", "reboot", "delayon", "delayoff", "delayreboot", "cancel"]
    )
    def test_the_guard_admits_every_legitimate_single_outlet_action(self, action):
        """The other half. A guard that rejected everything would pass every
        test above while making the driver useless."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _assert_safe_command

        _assert_safe_command(f"oltctrl index 8 act {action}")

    def test_a_rendering_bug_is_caught_by_the_guard(self, pdu, monkeypatch):
        """The concrete scenario the guard is for: validation passed, and the
        command was then rendered wrongly anyway.

        Simulated by corrupting the action map, which is how a real edit would
        break it. The write must not happen.
        """
        from benchctrl.drivers.cyberpower_pdu41002 import driver as drv

        monkeypatch.setitem(drv._SWITCH_ACTIONS, (False, False), "off guest 1")
        sim = pdu._benchctrl_sim
        sim.command_log.clear()
        with pytest.raises(PDU41002ValueError):
            pdu.set_outlet_state(1, False)
        assert sim.command_log == []


# ---------------------------------------------------------------------------
# Fixture parsing — the parsers against captured device bytes
# ---------------------------------------------------------------------------


class TestFixtureParsing:
    """Feeds the module-level parsers literal excerpts of real device output.

    This is the check the simulator cannot perform: the simulator and the
    driver were written by the same reader, so their agreement proves only
    self-consistency. These bytes came off the wire.
    """

    def test_the_fixtures_are_present_and_labelled(self):
        """Provenance is the whole point. A fixture without a firmware version
        and a capture date cannot be re-verified after a firmware update."""
        import pathlib

        root = pathlib.Path(__file__).parent / "fixtures" / "pdu41002"
        for name in ("serial_reads.txt", "ssh_reads.txt", "errors.txt", "README.md"):
            assert (root / name).is_file(), f"missing fixture {name}"
        assert "1.3.4" in (root / "serial_reads.txt").read_text()

    def test_sys_show_parses(self):
        from benchctrl.drivers.cyberpower_pdu41002.driver import _parse_colon_fields

        body = (
            "Name              : PDU-BENCHCTRL\r\n"
            "Location          : Office\r\n"
            "Contact           : Rick\r\n"
            "Model             : PDU41002\r\n"
            "Hardware Version  : 1.2\r\n"
            "Firmware Version  : 1.3.4\r\n"
            "MAC Address       : 00-0C-15-42-29-41\r\n"
        )
        fields = _parse_colon_fields(body)
        assert fields["Model"] == "PDU41002"
        assert fields["Firmware Version"] == "1.3.4"
        assert fields["MAC Address"] == "00-0C-15-42-29-41"

    def test_the_load_triplet_parses(self):
        """``Device Load : 0.00 A/ 0 W/ 0 VA`` — three numbers, three units,
        one line, inconsistent spacing."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _parse_load_triplet

        assert _parse_load_triplet("0.00 A/ 0 W/ 0 VA") == (0.0, 0.0, 0.0)
        assert _parse_load_triplet("1.25 A/ 150 W/ 152 VA") == (1.25, 150.0, 152.0)

    def test_the_outlet_table_parses(self):
        """Names may contain single spaces, and the state column is separated
        by *two or more* — so splitting on whitespace would fold a two-word
        outlet name into the state column."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _parse_outlet_table

        body = (
            "  Index  Name                                 State\r\n"
            "  -----  -----------------------------------  -----\r\n"
            "    1  Outlet1                              On\r\n"
            "    2  DUT power rail                       Off\r\n"
        )
        rows = _parse_outlet_table(body)
        # The raw status token, not a bool: conversion is _parse_on_off's job, so
        # an unexpected third state ("Pending") reaches a parser that can name it
        # rather than being silently coerced to False here.
        assert rows == [(1, "Outlet1", "On"), (2, "DUT power rail", "Off")]

        from benchctrl.drivers.cyberpower_pdu41002.driver import _parse_on_off

        assert _parse_on_off(rows[0][2], "outlet 1") is True
        assert _parse_on_off(rows[1][2], "outlet 2") is False
        # A ProtocolError, not a ValueError: the *device* said something
        # unparseable, which is not the caller's mistake. A third outlet state
        # must surface as "I do not understand this unit" rather than being
        # coerced to Off, which would report mains absent when it may not be.
        from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ProtocolError

        with pytest.raises(PDU41002ProtocolError):
            _parse_on_off("Pending", "outlet 1")

    def test_the_config_table_parses(self):
        from benchctrl.drivers.cyberpower_pdu41002.driver import _parse_config_table

        body = (
            "    1  Outlet1     3 s        3 s       5 s\r\n"
            "    2  Outlet2     10 s       20 s      30 s\r\n"
        )
        rows = _parse_config_table(body)
        assert rows == [
            (1, "Outlet1", 3, 3, 5),
            (2, "Outlet2", 10, 20, 30),
        ]

    def test_a_bare_lf_cr_line_ending_is_handled(self):
        r"""Caret lines are introduced by a bare ``\n\r``, not ``\r\n``.

        Splitting on ``\r\n`` alone mis-parses both the caret lines and
        ``snmpv3 show``'s rows, which is why the engine parses bytes rather
        than trusting ``splitlines()``.
        """
        from benchctrl.drivers.cyberpower_pdu41002.driver import _lines

        assert _lines("a\n\r             ^\n\rb") == ["a", "             ^", "b"]
        # Leading whitespace is preserved deliberately: on a caret line the
        # column *is* the payload — it is the only thing the device says about
        # where the command failed. Stripping here would discard it.
        assert _lines("a\r\nb") == ["a", "b"]
        assert _lines("a\rb") == ["a", "b"]

    def test_the_unknown_verb_error_is_detected(self):
        from benchctrl.drivers.cyberpower_pdu41002.driver import _raise_for_error

        body = "bogusverb\n\r             ^\n\rError : Command not found\r\n"
        with pytest.raises(PDU41002CommandError) as exc:
            _raise_for_error("bogusverb", body, 8)
        assert "Command not found" in str(exc.value)

    def test_the_index_range_error_is_detected_without_a_caret(self):
        """The shape that has neither a caret nor an ``Error :`` prefix.

        This is the one a caret-only detector misses entirely — and it would
        miss it *silently*, returning the error text as if it were data.
        """
        from benchctrl.drivers.cyberpower_pdu41002.driver import _raise_for_error

        body = "Index number must be 1 to 8\r\n"
        with pytest.raises(PDU41002CommandError):
            _raise_for_error("oltsta index 9 show", body, 8)

    def test_the_parameter_error_is_detected(self):
        from benchctrl.drivers.cyberpower_pdu41002.driver import _raise_for_error

        body = (
            "oltctrl index 1 act bogus\n\r                      ^\n\r"
            "Error : Parameter Error\r\n"
            "Usage: oltctrl index <1..8> act <on|off|reboot>\r\n"
        )
        with pytest.raises(PDU41002CommandError) as exc:
            _raise_for_error("oltctrl index 1 act bogus", body, 8)
        assert "Parameter Error" in str(exc.value)

    def test_a_clean_response_raises_nothing(self):
        """The other half of the error test: a detector that fired on normal
        output would make every read fail, which is at least loud — but one
        that fires on *some* normal output is a flake."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _raise_for_error

        _raise_for_error("sys show", "Model : PDU41002\r\nName : PDU\r\n", 8)

    def test_the_device_stated_outlet_count_is_cross_checked(self, caplog):
        """The index-range error is the device's own statement of how many
        outlets it has. If it disagrees with the configured count, the
        configuration is wrong — worth a warning rather than being discarded,
        because the alternative is switching an outlet that does not exist on a
        unit somebody swapped."""
        import logging

        from benchctrl.drivers.cyberpower_pdu41002.driver import _raise_for_error

        with caplog.at_level(logging.WARNING), pytest.raises(PDU41002CommandError):
            _raise_for_error("oltsta index 9 show", "Index number must be 1 to 16\r\n", 8)
        assert any("16" in r.getMessage() for r in caplog.records), (
            "outlet-count disagreement was not logged"
        )

    def test_the_prompt_keeps_its_trailing_space(self):
        """Recorded in the fixture README, and worth pinning in code too.

        The device sends ``b"CyberPower > "`` **with** a trailing space, but the
        checked-in fixture files lost it to trailing-whitespace stripping on
        save. Anyone "fixing" ``PROMPT`` to match the files breaks every read,
        so this test fails first.
        """
        from benchctrl.drivers.cyberpower_pdu41002.driver import PROMPT

        assert PROMPT == "CyberPower > "
        assert PROMPT.endswith(" ")

    def test_serial_echo_is_stripped_but_ssh_has_none_to_strip(self):
        """The largest textual difference between the two transports, and the
        reason echo handling is conditional on the link rather than assumed."""
        from benchctrl.drivers.cyberpower_pdu41002.driver import _strip_echo

        assert _strip_echo("sys show\r\nModel : PDU41002\r\n", "sys show",
                           echoes=True).lstrip().startswith("Model")
        assert _strip_echo("Model : PDU41002\r\n", "sys show",
                           echoes=False).lstrip().startswith("Model")


# ---------------------------------------------------------------------------
# Simulator fidelity
# ---------------------------------------------------------------------------


class TestSimulatorFidelity:
    def test_the_simulator_treats_ctrl_c_as_part_of_the_command(self):
        r"""The simulator's most valuable property: it is faithful where the
        driver was *wrong*.

        Hardware has no interrupt character — ``\x03`` is echoed and consumed
        as part of the command. The simulator does the same, which is how the
        driver's ``\x03`` prefix was caught rather than shipped. A permissive
        simulator that had stripped it would have agreed with the broken
        driver.
        """
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        sim = SimulatedPDU41002()
        sim.start()
        try:
            driver = _driver_class().open(
                port=sim.port, allowed_outlets=(1,), password=sim.password
            )
            try:
                driver._link.write(b"\x03sys show\r")
                driver._read_until((PDU_PROMPT,), timeout=3.0)

                # The control byte was not swallowed and not treated as an
                # interrupt: it arrived as the first character of the command.
                assert sim.command_log[-1] == "\x03sys show"

                # And a bare CR is what recovers — the actual resync, measured
                # on hardware. \x03 does not clear a dirty line either.
                driver._buf = ""
                driver._resync()
                assert driver.read_identity().model == "PDU41002"
                assert sim.command_log[-1] == "sys show"
            finally:
                driver.close()
        finally:
            sim.close()

    def test_the_simulator_rejects_an_unknown_error_shape(self):
        """A typo'd shape name in a test must fail the test, not silently
        inject nothing and leave the assertion passing for the wrong reason."""
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        with pytest.raises(ValueError):
            SimulatedPDU41002().inject_error("nonsense")

    def test_holding_the_session_blocks_a_second_login(self):
        """Models the device's single-session behaviour.

        On hardware the *incumbent* wins: the second login completes — banner
        and all — and is then hung up. That ordering is what makes the failure
        look like bad credentials, so the simulator has to reproduce it rather
        than simply refusing the password.
        """
        from benchctrl.drivers.cyberpower_pdu41002 import PDU41002SessionError
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        sim = SimulatedPDU41002()
        sim.start()
        try:
            sim.hold_session()
            with pytest.raises(PDU41002SessionError):
                _driver_class().open(
                    port=sim.port, allowed_outlets=(1,), password=sim.password
                )
            assert sim.hangup_count >= 1
        finally:
            sim.close()

    def test_releasing_the_session_lets_a_login_through(self):
        """The other half — otherwise the test above would pass against a
        simulator that simply never accepted a login."""
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        sim = SimulatedPDU41002()
        sim.start()
        try:
            sim.hold_session()
            sim.release_session()
            driver = _driver_class().open(
                port=sim.port, allowed_outlets=(1,), password=sim.password
            )
            try:
                assert driver.read_identity().model == "PDU41002"
            finally:
                driver.close()
        finally:
            sim.close()


# ---------------------------------------------------------------------------
# Package surface
# ---------------------------------------------------------------------------


class TestPackageSurface:
    def test_the_package_exports_what_the_docs_use(self):
        import benchctrl.drivers.cyberpower_pdu41002 as pkg

        for name in (
            "CyberPowerPDU41002",
            "PDU41002Info",
            "PDU41002Status",
            "OutletConfig",
            "PDU41002Error",
            "PDU41002ConnectionError",
            "PDU41002CommandError",
            "PDU41002ProtocolError",
            "PDU41002TimeoutError",
            "PDU41002ValueError",
            "PDU41002AuthError",
            "PDU41002SessionError",
            "PDU41002PolicyError",
        ):
            assert hasattr(pkg, name), f"{name} is not exported"

    def test_the_package_imports_without_a_serial_port(self):
        """Import must not touch hardware — the MCP server imports every driver
        at startup, on a host that may have none of them attached."""
        import importlib

        importlib.reload(
            importlib.import_module("benchctrl.drivers.cyberpower_pdu41002")
        )

    def test_the_dataclasses_are_frozen(self):
        """So a caller cannot edit a reading after the fact and have it look
        like something the device said."""
        import dataclasses

        from benchctrl.drivers.cyberpower_pdu41002 import OutletConfig

        for cls in (PDU41002Info, PDU41002Status, OutletConfig):
            assert dataclasses.fields(cls)
            assert cls.__dataclass_params__.frozen

    def test_to_dict_round_trips_the_readings(self, pdu):
        """``to_dict()`` is what the MCP tools return, so it has to carry every
        field rather than a convenient subset."""
        import dataclasses

        info = pdu.read_identity()
        status = pdu.read_device_status()
        assert set(info.to_dict()) == {f.name for f in dataclasses.fields(info)}
        assert set(status.to_dict()) == {f.name for f in dataclasses.fields(status)}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _driver_class():
    from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

    return CyberPowerPDU41002


from benchctrl.drivers.cyberpower_pdu41002.driver import PROMPT as PDU_PROMPT  # noqa: E402

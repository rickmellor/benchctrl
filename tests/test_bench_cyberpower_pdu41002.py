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

    def test_a_wrong_password_raises_auth_error(self):
        from benchctrl.sim.pdu41002 import SimulatedPDU41002

        sim = SimulatedPDU41002(password="correct")
        sim.start()
        try:
            with pytest.raises(PDU41002AuthError):
                _driver_class().open(
                    port=sim.port, allowed_outlets=(1,), password="wrong"
                )
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
        expected_mutators: set[str] = set()  # this release switches nothing
        unclassified = set(surface.methods) - expected_reads - expected_mutators
        assert not unclassified, (
            f"new method(s) {sorted(unclassified)} — is each a read or a "
            f"mutator? Mutators must carry an existing dispatch prefix "
            f"(set_/reset/clear_/trigger), or they are remotely callable "
            f"without a writer claim."
        )

    def test_this_release_exposes_no_mutators_at_all(self, surface):
        """The reads-only claim, asserted rather than described.

        When switching lands this becomes ``== {"set_outlet_state",
        "reset_outlet", "clear_outlet_command"}``. Until then, any mutator
        appearing here is a method that can move a contactor and was not
        reviewed as such.
        """
        assert surface.mutators == frozenset()

    def test_a_future_switching_name_would_be_classified_as_a_mutator(self):
        """Pins the naming decision itself, not the current surface.

        ``outlet_on()`` reads naturally and would be **remotely callable
        without a writer claim** — mains switching bypassing the claim gate.
        ``set_outlet_state()`` matches ``set_``. This test is what makes that
        difference visible to whoever adds switching next.
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

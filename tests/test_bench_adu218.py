"""The ADU218 driver against its simulator, plus the guards that keep it safe.

**Read this before adding a test here.** Every capture under
``tests/fixtures/adu218/`` was taken from real hardware, and where the vendor
manual disagrees with a capture, the capture wins. Several assertions below cite
a fixture by name; those are the ones that must not be "corrected" to match the
manual.

The simulator subclasses the *production* USBDEVFS link and overrides only the
ioctl (:py:mod:`benchctrl.sim.adu218`), so these tests exercise real framing,
the mandatory ``0x01`` report id, the desync check, ``drain()``, the command
whitelist and the width table. What they do **not** exercise is the ioctl layer
itself — that is deliberate and explained in ``tests/test_usbfs_adu218.py``.

Two shapes of past defect drive most of what is here:

- **A guard nothing distinguishes from another guard.** The relay range (0..7)
  and the input-line range (0..3) are different, so a single shared validator
  would accept ``RPA5``. There are tests that only the *correct* validator
  passes, not tests that any validator passes.
- **A capability that works locally and is invisible remotely.** Registration
  is spread over seven files and four of them fail *silently*. Those are pinned
  in ``tests/test_remote_ontrak_adu218.py``, not here.
"""

from __future__ import annotations

from unittest import mock

import pytest

from benchctrl.drivers.ontrak_adu218 import driver as driver_module
from benchctrl.drivers.ontrak_adu218.driver import (
    ADU218ConnectionError,
    ADU218Info,
    ADU218PolicyError,
    ADU218ProtocolError,
    ADU218TimeoutError,
    ADU218ValueError,
    OntrakADU218,
    command_spec,
)
from benchctrl.sim.adu218 import SimulatedADU218, SimulatedAdu218Link


@pytest.fixture()
def adu():
    """A production driver on a simulated link, watchdog disarmed at open."""
    link = SimulatedAdu218Link()
    device = OntrakADU218.open(link=link)
    try:
        yield device
    finally:
        device.close()


@pytest.fixture()
def model(adu):
    """The device model behind the driver, for asserting and for driving inputs."""
    return adu._link.device_model


# ---------------------------------------------------------------------------
# The command whitelist and the response table
# ---------------------------------------------------------------------------


class TestCommandTable:
    """The table is the only thing that knows which commands answer.

    It cannot be derived at runtime: silence is the device's sole error signal,
    so an unanswered command is indistinguishable from an unknown one.
    """

    def test_the_response_widths_match_the_hardware_capture(self):
        """Every width comes from ``reads.txt``, not from the manual.

        The manual gives an explicit width for some commands and only an
        *example* for ``RPKn``, ``RPyn``, ``DB`` and ``WD``. An example is not a
        specification, and a width that is wrong by one turns a desync into a
        plausible value instead of an exception.
        """
        expected = {
            "PK": 3,
            "RPK0": 1,
            "PA": 2,
            "PB": 2,
            "RPA": 4,
            "RPB": 4,
            "RPA0": 1,
            "RPB3": 1,
            "PI": 3,
            "RE0": 5,
            "RC7": 5,
            "DB": 1,
            "WD": 1,
        }
        for command, width in expected.items():
            spec = command_spec(command)
            assert spec.responsive, f"{command} must be declared responsive"
            assert spec.width == width, f"{command} width"

    def test_rk_is_write_only_despite_starting_with_r(self):
        """The trap that makes a mnemonic-derived rule wrong.

        Every other ``R``-prefixed command answers. ``RKn`` does not, and it is
        the most-called command on the device — so an inferred rule would wait
        the full timeout on every single de-energise, and a driver built that
        way looks broken only under load.
        """
        assert command_spec("RK0").responsive is False
        assert command_spec("RK7").responsive is False
        # Contrast, so this test cannot pass by declaring everything write-only.
        assert command_spec("RPK0").responsive is True

    def test_writes_are_all_declared_silent(self):
        for command in ("SK0", "SK7", "RK3", "MK000", "MK255", "DB0", "DB2", "WD0", "WD3"):
            spec = command_spec(command)
            assert spec.responsive is False, command
            assert spec.width is None, command

    @pytest.mark.parametrize(
        "command",
        [
            "RI",  # the manual's summary table's name for PI; silent on hardware
            "RPK8",  # relay index out of range
            "RPA4",  # input line out of range -- the off-by-four
            "RE8",
            "DB3",  # three de-bounce settings, not the web page's four
            "WD4",
            "MK256",  # a byte's worth is 255
            "MK300",  # the aliasing hazard: undocumented, so never sent
            "MK9999",
            "MK",  # no argument
            "RPK",  # no index
            "menumode",  # not this device's verb, but a good junk case
            "",
            "XYZ",
        ],
    )
    def test_the_whitelist_refuses_everything_it_should(self, command):
        """A rendered command that matches no entry never reaches the device.

        This is the second of two independent guards. The first is the argument
        range check, which produces a useful message; this one catches a bad
        *rendering* — a format-string slip a value check cannot see.
        """
        with pytest.raises(ADU218ValueError):
            command_spec(command)

    def test_mk_accepts_exactly_the_byte_range_zero_padded(self):
        """``MKddd`` takes three digits. 0..255 in, 256..999 out."""
        for value in (0, 1, 99, 100, 199, 200, 249, 250, 255):
            assert command_spec(f"MK{value:03d}").what == "whole relay port"
        for value in (256, 260, 299, 300, 999):
            with pytest.raises(ADU218ValueError):
                command_spec(f"MK{value:03d}")

    def test_no_accepted_command_exceeds_the_payload_limit(self):
        """Seven bytes, because byte 0 of the 8-byte report is the report id.

        An eighth payload byte is silently dropped by the device, which is
        indistinguishable from the command being unknown. So the check is on
        what the table *accepts*, enumerated — not on how the patterns are
        written, which is what makes it a property rather than a restatement.
        """
        import re
        from itertools import product

        from benchctrl.drivers.ontrak_adu218.usbfs import MAX_COMMAND_LEN

        # Enumerate what each pattern accepts: its literal alpha prefix plus up
        # to four trailing characters, drawn from every character any argument
        # position can hold. Four is one more than the widest argument in the
        # table (``MKddd``'s three digits), so every pattern is offered at least
        # one string longer than it should accept -- an over-long acceptance is
        # *generated* here, not merely permitted.
        accepted = set()
        for spec in driver_module._COMMAND_SPECS:
            prefix = re.match(r"\^([A-Z]*)", spec.pattern.pattern).group(1)
            for length in range(5):
                for tail in product("0123456789AB", repeat=length):
                    candidate = prefix + "".join(tail)
                    if spec.pattern.fullmatch(candidate):
                        accepted.add(candidate)
                        assert len(candidate) <= MAX_COMMAND_LEN, candidate

        # The enumeration has to have found something, or the assertion above
        # is vacuous -- the shape where rc=0 means "nothing ran".
        assert {"PK", "RPK0", "SK7", "RK0", "MK255", "PA", "RPB", "RPA3",
                "PI", "RE0", "RC7", "DB", "DB2", "WD", "WD3"} <= accepted

    def test_every_whitelisted_command_is_reachable_from_the_sdk(self):
        """No entry in the table is a capability the driver cannot actually use.

        The whitelist is read two ways elsewhere -- as a safety gate (nothing
        unlisted is emitted) and as the response-width table -- and both of those
        are satisfied by an entry no method ever sends. That asymmetry is what
        this closes, and it caught a real one: ``P[AB]`` was whitelisted, given a
        hardware-measured width, modelled by the simulator and documented in a
        ``docs/drivers.md`` table row, while no public method emitted it. The
        per-port nibble read was, in effect, documented as present and absent.

        Driving the surface rather than reading the source, because a grep for
        the command strings would pass on a method that renders one and never
        gets called.
        """
        link = SimulatedAdu218Link()
        adu = OntrakADU218.open(link=link)
        sent: list[str] = []
        original = adu._send
        adu._send = lambda command: (sent.append(command), original(command))[1]
        try:
            adu.read_identity()
            for index in range(8):
                adu.relay_state(index)
                adu.set_relay_state(index, True)
                adu.set_relay_state(index, False)
                adu.read_counter(index)
                adu.clear_counter(index)
            adu.relay_states()
            adu.relay_mask()
            adu.set_relay_port(0)
            adu.reset_relays()
            for port in ("A", "B"):
                adu.input_port_mask(port)
                for line in range(4):
                    adu.input_state(port, line)
            adu.input_states()
            adu.input_mask()
            adu.read_debounce()
            adu.read_debounce_ms()
            for setting in (0, 1, 2):
                adu.set_debounce(setting)
            adu.read_watchdog()
            adu.read_watchdog_tripped()
            _ = adu.watchdog_setting
            # Ascending then back to 0, so the surface is left disarmed.
            for setting in (1, 2, 3, 0):
                adu.set_watchdog(setting)
        finally:
            adu._send = original
            adu.close()

        # Guard against the vacuous pass: if the exercise above sent nothing,
        # every "unreached" check below would be trivially satisfied.
        assert len(set(sent)) >= 60, f"only {len(set(sent))} distinct commands sent"

        unreached = [
            spec.what
            for spec in driver_module._COMMAND_SPECS
            if not any(spec.pattern.match(command) for command in sent)
        ]
        assert unreached == [], (
            f"whitelisted but unreachable from any public method: {unreached}. "
            f"Either add the method or drop the entry -- a command the driver "
            f"admits, measures a width for and simulates, but cannot send, "
            f"reads as a supported capability that is not one."
        )


# ---------------------------------------------------------------------------
# Argument validation: two ranges, never one
# ---------------------------------------------------------------------------


class TestValidation:
    def test_relay_and_input_ranges_are_enforced_separately(self, adu):
        """The off-by-four. A shared validator passes both of these.

        Relay 7 is valid and input line 7 is not. A single 0..7 check would
        accept ``RPA7``, the device would answer with silence, and the operator
        would see a timeout three layers from the bad argument that caused it.
        """
        assert adu.relay_state(7) is False  # valid: relays are 0..7
        with pytest.raises(ADU218ValueError, match="0..3"):
            adu.input_state("A", 7)  # invalid: input lines are 0..3
        with pytest.raises(ADU218ValueError, match="0..3"):
            adu.input_state("A", 4)  # the boundary RPA4, silent on hardware
        assert adu.input_state("A", 3) is False  # RPA3 answers on hardware

    def test_counters_use_the_relay_range_not_the_input_range(self, adu):
        """Eight counters, one per input — but indexed 0..7, not 0..3.

        A third range, and the one most likely to be conflated with the input
        line range since the counters count *inputs*.
        """
        assert adu.read_counter(7) == 0
        with pytest.raises(ADU218ValueError):
            adu.read_counter(8)

    @pytest.mark.parametrize(
        "call,args",
        [
            ("relay_state", (True,)),
            ("read_counter", (False,)),
            ("clear_counter", (True,)),
            ("set_debounce", (True,)),
            ("set_watchdog", (True,)),
            ("set_relay_port", (True,)),
        ],
    )
    def test_bool_is_rejected_before_int(self, adu, call, args):
        """``bool`` is an ``int`` subclass, so ``relay_state(True)`` would
        silently mean relay 1 — and ``set_watchdog(True)`` would arm a
        one-second deadman on a bench nobody expected to hold it."""
        with pytest.raises(ADU218ValueError, match="bool"):
            getattr(adu, call)(*args)

    def test_the_on_argument_must_be_a_real_bool(self, adu):
        """``set_relay_state(0, 1)`` is refused rather than guessed.

        A truthy string or a 0/1 is exactly how a caller ends up energising the
        opposite of what they meant, and this is the one argument where being
        wrong closes a circuit.
        """
        with pytest.raises(ADU218ValueError, match="bool"):
            adu.set_relay_state(0, 1)
        with pytest.raises(ADU218ValueError, match="bool"):
            adu.set_relay_state(0, "off")

    def test_input_ports_are_a_and_b_in_either_case(self, adu):
        assert adu.input_state("a", 0) is False
        assert adu.input_state(" B ", 0) is False
        with pytest.raises(ADU218ValueError):
            adu.input_state("C", 0)
        with pytest.raises(ADU218ValueError):
            adu.input_state(0, 0)

    def test_the_port_letter_is_rejected_by_the_argument_check_not_the_whitelist(
        self, adu, model
    ):
        """Two guards catch ``input_state("C", 0)`` and they are not
        interchangeable — the test above passes with *either* one, because both
        raise :py:class:`ADU218ValueError`. Deleting the port check outright
        survived that test.

        The distinction is which one fires. The argument check names the
        argument, which is what an operator can act on; the whitelist names a
        rendered command they never wrote. So assert the message *and* that the
        rejection happened before the wire, since only the argument check
        guarantees that ordering.
        """
        before = len(model.command_log)
        with pytest.raises(ADU218ValueError, match="must be one of"):
            adu.input_state("C", 0)
        assert model.command_log[before:] == []
        # And the control: the whitelist message is a different string, so a
        # match on "must be one of" cannot be satisfied by the fallback guard.
        with pytest.raises(ADU218ValueError, match="whitelist"):
            driver_module.command_spec("RPC0")

    def test_an_oversize_mask_is_rejected_by_the_range_check_not_the_whitelist(
        self, adu, model
    ):
        """Same shape as the port letter, and the stakes are higher: ``MK300``
        exceeds a byte and the device's aliasing behaviour is undocumented, so a
        value that got as far as being *rendered* is a value that might have
        energised relays nobody named.

        The range check must be what stops it. The whitelist is the backstop for
        a mis-*rendering*, which a value check cannot see; keying this test on
        the whitelist message would have proved the range check was gone.
        """
        before = len(model.command_log)
        with pytest.raises(ADU218ValueError, match="three-digit byte"):
            adu.set_relay_port(300)
        assert model.command_log[before:] == []
        with pytest.raises(ADU218ValueError, match="whitelist"):
            driver_module.command_spec("MK300")


# ---------------------------------------------------------------------------
# Relay control
# ---------------------------------------------------------------------------


class TestRelays:
    def test_a_switch_returns_the_verified_read_back(self, adu, model):
        """Not ``None``. The device does not acknowledge a write at all, so the
        return value is the only confirmation and discarding it has to be an
        explicit choice."""
        assert adu.set_relay_state(2, True) is True
        assert model.relay_state(2) is True
        assert adu.set_relay_state(2, False) is False
        assert model.relay_state(2) is False

    @pytest.mark.parametrize("index", range(8))
    def test_every_relay_switches_individually(self, adu, model, index):
        """All eight, one at a time — not just as part of a port mask.

        Relays 6 and 7 previously appeared **only** inside ``MKddd`` mask tests,
        so an ``SKn``/``RKn`` command builder that was wrong at the top of the
        range (an off-by-one, or a table with six entries) would have been
        caught by nothing: the mask path builds a different command entirely and
        would keep passing.

        The neighbour assertions are the point. Asserting only that relay 6
        closed cannot distinguish "closed 6" from "closed 6 and 7", which is the
        exact failure a shift-by-one command builder produces.
        """
        assert adu.set_relay_state(index, True) is True
        assert model.relay_state(index) is True
        assert adu.relay_state(index) is True
        assert adu.relay_mask() == 1 << index, "exactly one relay, and the right one"

        for other in range(8):
            if other != index:
                assert model.relay_state(other) is False, (
                    f"switching relay {index} also moved relay {other}"
                )

        assert adu.set_relay_state(index, False) is False
        assert model.relay_state(index) is False
        assert adu.relay_mask() == 0

    def test_the_relays_are_addressed_by_a_distinct_command_each(self, adu, model):
        """The command actually put on the wire, per relay.

        ``relay_mask()`` proves the *outcome* is right; this proves the driver
        got there by naming the relay rather than by any path that happens to
        agree. ``SKn``/``RKn`` are write-only and unacknowledged, so the command
        log is the only witness to what was sent.
        """
        model.command_log.clear()
        for index in range(8):
            adu.set_relay_state(index, True, verify=False)
        sent = [c for c in model.command_log if c.startswith(("SK", "RK"))]
        assert sent == [f"SK{i}" for i in range(8)]

        model.command_log.clear()
        for index in range(8):
            adu.set_relay_state(index, False, verify=False)
        sent = [c for c in model.command_log if c.startswith(("SK", "RK"))]
        assert sent == [f"RK{i}" for i in range(8)]

    def test_relay_commands_are_absolute_not_toggling(self, adu, model):
        """Established on hardware by the DMM witness, which recorded three
        *holds* among nine commands: ``SK0,SK0`` left the relay closed rather
        than reverting it. The plan had only assumed this."""
        adu.set_relay_state(0, True)
        adu.set_relay_state(0, True)
        assert model.relay_state(0) is True
        adu.set_relay_state(0, False)
        adu.set_relay_state(0, False)
        assert model.relay_state(0) is False

    def test_a_lying_device_is_caught_by_verification(self, adu, monkeypatch):
        """The read-back exists to catch a command the device accepted
        silently and did not act on — which, on a device with no error reply,
        is otherwise invisible."""
        monkeypatch.setattr(type(adu), "relay_state", lambda self, index: False)
        with pytest.raises(ADU218ProtocolError, match="did not act on it"):
            adu.set_relay_state(1, True)

    def test_verify_false_returns_the_commanded_value_not_a_measurement(self, adu, model):
        """A real downgrade, and the docstring says so. Pinned because the two
        return values are the same type and only their *meaning* differs."""
        model.relays = 0

        def swallow(command):
            pass  # a device that accepts and ignores

        original = model.handle
        model.handle = lambda command: None
        try:
            assert adu.set_relay_state(4, True, verify=False) is True
        finally:
            model.handle = original
        assert model.relay_state(4) is False  # nothing actually happened

    def test_the_whole_port_moves_in_one_transition(self, adu, model):
        """``MKddd`` is one simultaneous change. Per-relay writes pass through
        intermediate combinations, which may be electrically meaningful."""
        assert adu.set_relay_port(0b10100101) == 0b10100101
        assert model.relays == 0b10100101
        assert [c for c in model.command_log if c.startswith("MK")] == ["MK165"]

    def test_relay_states_is_one_round_trip(self, adu, model):
        """Eight ``RPKn`` reads would be eight instants, so a state changing
        mid-sweep yields a mixture that never existed. ``PK`` is one sample."""
        model.relays = 0b00110011
        before = len(model.command_log)
        states = adu.relay_states()
        emitted = model.command_log[before:]
        assert emitted == ["PK"]
        assert states == {0: True, 1: True, 2: False, 3: False,
                          4: True, 5: True, 6: False, 7: False}

    def test_reset_relays_reaches_the_safe_state(self, adu, model):
        model.relays = 0xFF
        assert adu.reset_relays() == 0
        assert model.relays == 0
        assert "MK000" in model.command_log

    def test_a_lying_device_is_caught_when_the_whole_port_is_written(self, adu, model):
        """``set_relay_port`` verifies too, and this is not the same code path as
        :py:meth:`set_relay_state`'s check — a mutation removing *only* this one
        survived every other test in this file, including the single-relay lying
        test right above. ``MKddd`` is the more dangerous write of the two, so a
        silently-ignored one is the worse thing to leave unverified."""
        model.relays = 0
        original = model.handle
        # Accepts MK and does nothing; every other command still works, so the
        # read-back succeeds and disagrees, rather than timing out.
        model.handle = lambda command: (
            None if command.startswith("MK") else original(command)
        )
        try:
            with pytest.raises(ADU218ProtocolError, match="did not act"):
                adu.set_relay_port(0b00000110)
        finally:
            model.handle = original
        assert model.relays == 0

    def test_reset_relays_verifies_that_the_safe_state_was_actually_reached(
        self, adu, model
    ):
        """The most important read-back on the device, and separately mutable
        from the other two: this is the call an operator makes to *believe* the
        bench is de-energised. Reporting 0 for a port that is still at 0xFF is
        the one wrong answer that gets someone hurt, so a device that swallows
        ``MK000`` must raise rather than return the safe-looking value."""
        model.relays = 0xFF
        original = model.handle
        swallowed = []

        def swallow_mk000(command):
            if command == "MK000":
                # Recorded here rather than read from command_log: the stub
                # replaces handle(), which is also where the log is appended.
                swallowed.append(command)
                return None
            return original(command)

        model.handle = swallow_mk000
        try:
            with pytest.raises(ADU218ProtocolError, match="still energised"):
                adu.reset_relays()
        finally:
            model.handle = original
        # And the control: the exception is about the *device*, not the driver
        # failing to send. MK000 really was written, and the relays really are
        # still energised — which is precisely the state that must not be
        # reported as safe.
        assert swallowed == ["MK000"]
        assert model.relays == 0xFF


class TestAllowlist:
    def test_an_unlisted_relay_cannot_be_energised(self, model=None):
        link = SimulatedAdu218Link()
        adu = OntrakADU218.open(link=link, allowed_relays=(0, 1))
        try:
            with pytest.raises(ADU218PolicyError, match="not in allowed_relays"):
                adu.set_relay_state(5, True)
            assert "SK5" not in link.device_model.command_log
        finally:
            adu.close()

    def test_an_unlisted_relay_can_always_be_de_energised(self):
        """Asymmetric on purpose. The allowlist prevents unintended
        *energising*; a rule that also blocked de-energising would make a
        narrower policy the more dangerous one."""
        link = SimulatedAdu218Link(relays=0b00100000)
        adu = OntrakADU218.open(link=link, allowed_relays=(0, 1))
        try:
            assert adu.set_relay_state(5, False) is False
            assert link.device_model.relay_state(5) is False
        finally:
            adu.close()

    def test_reset_relays_bypasses_the_allowlist(self):
        """The only method that does, so the safe state stays reachable on
        exactly the benches most carefully configured."""
        link = SimulatedAdu218Link(relays=0xFF)
        adu = OntrakADU218.open(link=link, allowed_relays=(0,))
        try:
            assert adu.reset_relays() == 0
        finally:
            adu.close()

    def test_the_mask_is_checked_whole_not_diffed(self):
        """A mask naming a disallowed relay is refused even when that relay's
        requested value matches its current one.

        "No change requested" depends on a read that could be stale, and a
        policy that holds only when the device agrees is not a policy.
        """
        link = SimulatedAdu218Link(relays=0b00100000)
        adu = OntrakADU218.open(link=link, allowed_relays=(0, 1))
        try:
            # Relay 5 is already energised, and the mask keeps it that way.
            with pytest.raises(ADU218PolicyError, match=r"relays \[5\]"):
                adu.set_relay_port(0b00100001)
        finally:
            adu.close()

    def test_a_mask_of_only_allowed_relays_is_permitted(self):
        """The corroborating half: the refusal above must not be the only
        outcome the allowlist can produce."""
        link = SimulatedAdu218Link()
        adu = OntrakADU218.open(link=link, allowed_relays=(0, 1))
        try:
            assert adu.set_relay_port(0b00000011) == 0b00000011
        finally:
            adu.close()

    def test_the_allowlist_defaults_to_every_relay(self, adu):
        """A deliberate difference from the mains PDU, whose allowlist is
        mandatory. These are 1 A signal SSRs on instrument leads and the
        operator's stated policy is that they toggle freely, with the hardware
        watchdog as the per-test interlock."""
        assert adu.allowed_relays == frozenset(range(8))

    def test_the_allowlist_cannot_be_widened_after_open(self, adu):
        """``frozenset``, so a caller cannot ``.add()`` a relay into scope."""
        assert isinstance(adu.allowed_relays, frozenset)
        with pytest.raises(AttributeError):
            adu.allowed_relays.add(3)


# ---------------------------------------------------------------------------
# Digital inputs and counters
# ---------------------------------------------------------------------------


class TestInputs:
    def test_input_states_reverses_the_msb_first_reply(self, adu, model):
        """``RPy`` answers MSB first, so its leftmost character is line **3**.

        Indexing the reply string directly is an off-by-three that reads
        correctly for the all-zero case every unwired bench produces — so this
        asserts an *asymmetric* pattern, which is the only kind that can fail.
        """
        model.set_input("A", 0, True)
        model.set_input("B", 3, True)
        states = adu.input_states()
        assert states["A"] == (True, False, False, False)
        assert states["B"] == (False, False, False, True)

    def test_the_input_mask_puts_port_a_in_the_low_nibble(self, adu, model):
        model.set_input("A", 1, True)
        assert adu.input_mask() == 0b00000010
        model.set_input("B", 0, True)
        assert adu.input_mask() == 0b00010010

    def test_one_line_agrees_with_the_whole_port(self, adu, model):
        """Cross-check between two independent commands (``RPyn`` and ``RPy``).
        A symmetric error in the driver's decoding would pass either alone."""
        model.set_input("B", 2, True)
        assert adu.input_state("B", 2) is True
        assert adu.input_states()["B"][2] is True
        assert adu.input_state("B", 1) is False

    def test_the_port_nibble_needs_no_reordering(self, adu, model):
        """``Py`` is LSB-weighted decimal, unlike ``RPy``'s MSB-first text.

        Asserted asymmetrically on purpose: line 0 set and line 3 clear on port
        A, then the mirror on port B. A driver that reversed this one the way
        ``input_states`` correctly reverses ``RPy`` would return 8 rather than 1,
        and a symmetric fixture could not tell the two apart.
        """
        model.set_input("A", 0, True)
        model.set_input("B", 3, True)
        assert adu.input_port_mask("A") == 0b0001
        assert adu.input_port_mask("B") == 0b1000

    def test_the_port_nibble_is_one_port_only(self, adu, model):
        """``Py`` reports its own port and says nothing about the other one.

        The distinction from ``PI`` that justifies a separate method: masking
        ``PI`` down to a nibble would give the same number here, but only
        because both ports are read in the same round trip. This asserts port A
        is unmoved by port B's lines, which a "cheaper PI" implementation would
        fail.
        """
        model.set_input("B", 0, True)
        model.set_input("B", 1, True)
        assert adu.input_port_mask("A") == 0
        assert adu.input_port_mask("B") == 0b0011
        # ...and PI carries both, with port B in the high nibble.
        assert adu.input_mask() == 0b00110000

    def test_the_port_nibble_rejects_a_bad_port(self, adu):
        with pytest.raises(ADU218ValueError):
            adu.input_port_mask("C")


class TestCounters:
    def test_a_rising_edge_is_counted(self, adu, model):
        model.set_input("A", 0, True)
        model.set_input("A", 0, False)
        model.set_input("A", 0, True)
        assert adu.read_counter(0) == 2

    def test_reading_a_counter_does_not_clear_it(self, adu, model):
        model.set_input("A", 2, True)
        assert adu.read_counter(2) == 1
        assert adu.read_counter(2) == 1
        assert "RC2" not in model.command_log

    def test_clearing_returns_the_value_it_destroyed(self, adu, model):
        """``RCn`` is the only responsive command that mutates state, so the
        returned count is the only copy — which is why it is returned rather
        than discarded, and why it must never be auto-retried."""
        model.set_input("B", 1, True)
        model.set_input("B", 1, False)
        model.set_input("B", 1, True)
        index = 1 + 4  # port B line 1
        assert adu.clear_counter(index) == 2
        assert adu.read_counter(index) == 0

    def test_a_counter_wraps_at_65535_rather_than_growing(self, adu, model):
        """16 bits, then rollover to 00000 (manual §6c).

        The wrap matters to callers, not just to the wire: the only safe way to
        use these counters is to difference successive reads, and a consumer
        that subtracts naively gets a large negative number exactly once per
        65536 events. Asserting the boundary is what makes that documentable.
        """
        model.counters[0] = driver_module.COUNTER_MAX
        assert adu.read_counter(0) == 65535

        model.set_input("A", 0, True)  # one more rising edge
        assert adu.read_counter(0) == 0, "must roll over, not saturate or overflow"

        model.set_input("A", 0, False)
        model.set_input("A", 0, True)
        assert adu.read_counter(0) == 1, "counting continues past the wrap"

    def test_the_wrap_is_why_a_naive_difference_goes_negative(self, adu, model):
        """The failure mode the wrap causes, stated as a test so nobody has to
        rediscover it. ``after - before`` is negative across a rollover; the
        correct form adds the modulo back."""
        model.counters[3] = 65530
        before = adu.read_counter(3)
        for _ in range(10):
            model.set_input("A", 3, True)
            model.set_input("A", 3, False)
        after = adu.read_counter(3)

        assert before == 65530
        assert after == 4
        assert after - before < 0, "the naive difference is negative here"
        assert (after - before) % 65536 == 10, "the modulo form recovers the count"

    def test_a_count_above_the_16_bit_maximum_is_rejected_not_returned(self, adu):
        """The device cannot report more than 65535 in five digits, so a larger
        value means the reply was corrupted or misframed — a queued response
        from a *different* command being read as this one is the observed
        failure mode on this device. Returning it would launder a framing bug
        into a plausible measurement.
        """
        with (
            mock.patch.object(type(adu), "_send", autospec=True, return_value="99999"),
            pytest.raises(ADU218ProtocolError, match="above the 65535"),
        ):
            adu.read_counter(0)

    def test_a_non_numeric_counter_reply_is_rejected(self, adu):
        """The other half of the same guard: silence is this device's only error
        signal, so a reply that is present but not a number is a framing fault
        rather than a value."""
        with (
            mock.patch.object(type(adu), "_send", autospec=True, return_value="00O23"),
            pytest.raises(ADU218ProtocolError, match="not a decimal number"),
        ):
            adu.read_counter(0)

    def test_the_driver_never_retries_a_destructive_read(self, adu, monkeypatch):
        """A lost reply after the device has already cleared loses the count
        permanently, and a retry would report 0 — indistinguishable from "no
        events". So the timeout must propagate, not be papered over.
        """
        calls = []

        def timeout_once(*args, **kwargs):
            calls.append(1)
            raise ADU218TimeoutError("simulated lost reply")

        monkeypatch.setattr(type(adu), "_send_int", timeout_once)
        with pytest.raises(ADU218TimeoutError):
            adu.clear_counter(0)
        assert len(calls) == 1, "clear_counter must not retry"


class TestDebounce:
    def test_the_setting_round_trips(self, adu):
        assert adu.set_debounce(2) == 2
        assert adu.read_debounce() == 2
        assert adu.set_debounce(0) == 0

    def test_there_are_three_settings_not_four(self, adu):
        """Ontrak's web page lists a fourth (``NONE``). The manual bounds ``n``
        to 0..2, the captures show 0/1/2, and the same four-option string
        appears on the ADU208 and ADU228 pages — shared boilerplate."""
        assert driver_module.DEBOUNCE_SETTINGS == (0, 1, 2)
        with pytest.raises(ADU218ValueError):
            adu.set_debounce(3)

    def test_a_higher_setting_is_a_shorter_filter(self, adu):
        """The mapping is inverted, and that is the whole point of ``DEBOUNCE_MS``.

        Manual §6c: ``0 = 10ms, 1 = 1ms (Default), 2 = 100us``. So the intuitive
        readings are both wrong — 0 is not "off", it is the *longest* filter,
        and 2 does not filter hardest, it filters least. An operator chasing
        maximum contact de-bounce would pick 2 and get 100 µs.

        Asserted as a strict ordering rather than three equalities so that a
        future edit cannot keep the values and quietly re-sort them.
        """
        ms = driver_module.DEBOUNCE_MS
        assert ms == {0: 10.0, 1: 1.0, 2: 0.1}
        assert ms[0] > ms[1] > ms[2], "setting number and filter width must be inverted"
        assert set(ms) == set(driver_module.DEBOUNCE_SETTINGS)

    def test_read_debounce_ms_reports_the_width_not_the_setting(self, adu):
        """The two reads must not be interchangeable, or the inverted mapping
        would be invisible at the call site."""
        assert adu.set_debounce(0) == 0
        assert adu.read_debounce() == 0
        assert adu.read_debounce_ms() == 10.0, "setting 0 is the 10 ms filter"

        assert adu.set_debounce(2) == 2
        assert adu.read_debounce() == 2
        assert adu.read_debounce_ms() == 0.1, "setting 2 is the 100 us filter"

    def test_the_simulator_does_not_default_it_to_zero(self):
        """The hardware reported ``DB`` = 1 out of the box (``reads.txt``).

        A sim defaulting every setting to 0 could not catch a driver that
        returned a hardcoded 0 instead of reading the device.
        """
        link = SimulatedAdu218Link()
        adu = OntrakADU218.open(link=link)
        try:
            assert adu.read_debounce() == 1
        finally:
            adu.close()


# ---------------------------------------------------------------------------
# The watchdog — the interlock, and the most dangerous thing to get wrong
# ---------------------------------------------------------------------------


class TestWatchdog:
    def test_open_disarms_it_by_default(self, model):
        """A fresh process has no expectation to compare ``WD`` against, so
        the first read after a restart is ambiguous *by construction*. Writing
        a known value replaces an inherited unknown."""
        assert "WD0" in model.command_log
        assert model.watchdog == 0

    def test_open_can_be_asked_to_preserve_an_inherited_setting(self):
        """The escape hatch, for inspecting what a previous session left."""
        link = SimulatedAdu218Link(watchdog=2)
        adu = OntrakADU218.open(link=link, disarm_watchdog=False)
        try:
            assert "WD0" not in link.device_model.command_log
            assert adu.read_watchdog() == 2
            # ...and the driver-held expectation is 0, which is the ambiguity
            # disarming exists to avoid. Asserted so the cost is visible.
            assert adu.watchdog_setting == 0
        finally:
            adu.close()

    def test_setting_it_also_arms_it(self, adu, model):
        """``WDn`` does both — there is no separate arm step, so a nonzero
        value takes effect immediately."""
        assert adu.set_watchdog(3) == 3
        assert model.watchdog == 3

    def test_it_de_energises_every_relay_on_silence(self, adu, model):
        """The whole point of the interlock, on a synthetic clock.

        No software is in this decision path on hardware — which is why the
        driver leaves it armed at ``close()``: releasing the device *is* the
        silence it exists to detect.
        """
        adu.set_relay_port(0b00001111)
        adu.set_watchdog(1)
        model.advance(1.5)
        assert model.relays == 0
        assert model.watchdog_trips == 1

    def test_any_command_refeeds_the_timer(self, adu, model):
        """Measured on hardware, and the reason there is no keep-alive thread.

        Ninety seconds pass here with a ten-second watchdog armed and the relay
        never drops, because a state read keeps the deadman fed. A background
        feeder would guarantee this while the failure it guards against was
        happening.
        """
        adu.set_watchdog(2)
        adu.set_relay_state(0, True)
        for _ in range(10):
            model.advance(9.0)
            adu.relay_states()
        assert model.relay_state(0) is True
        assert model.watchdog_trips == 0
        # And it does fire once the polling stops, so the test above is not
        # passing because the watchdog was never armed.
        model.advance(11.0)
        assert model.relays == 0
        assert model.watchdog_trips == 1

    def test_an_invalid_command_also_refeeds_it(self, adu, model):
        """Which is worse than a valid one refeeding it, and is why the feed
        must live on the control path rather than in a health check."""
        adu.set_watchdog(2)
        for _ in range(5):
            model.advance(9.0)
            model.handle("XYZ")  # silent, unknown, and still a refeed
        assert model.watchdog == 2
        assert model.watchdog_trips == 0

    def test_a_trip_is_visible_only_against_the_held_expectation(self, adu, model):
        """``WD`` self-clearing to 0 is the only trace a timeout leaves, and 0
        is also what "never enabled" looks like. The comparison is the test."""
        adu.set_watchdog(1)
        assert adu.watchdog_setting == 1
        model.advance(1.5)
        assert adu.read_watchdog_tripped() is True
        # Latched once: the expectation is cleared, so it does not re-report.
        assert adu.read_watchdog_tripped() is False

    def test_a_disarmed_watchdog_never_reports_a_trip(self, adu, model):
        """Without this, ``read_watchdog_tripped()`` would return ``True`` for
        every session that never armed it — 0 == 0."""
        assert adu.watchdog_setting == 0
        assert adu.read_watchdog_tripped() is False
        model.advance(3600.0)
        assert adu.read_watchdog_tripped() is False

    def test_the_documented_timeouts_are_the_measured_ones(self):
        """``WD1``'s 1 s was established by bisecting the silence window on
        hardware — (0.90, 1.10] s. An earlier capture's "3.7 s" was observation
        latency, not a trip time."""
        assert driver_module.WATCHDOG_TIMEOUT_S == {0: None, 1: 1.0, 2: 10.0, 3: 60.0}

    def test_the_driver_offers_no_keep_alive(self):
        """A background feeder would keep the watchdog fed precisely while the
        thing it protects against was happening — the inert-interlock shape. So
        no such method exists, and this asserts the absence."""
        names = [n for n in dir(OntrakADU218) if not n.startswith("_")]
        for forbidden in ("feed", "kick", "pet", "keepalive", "keep_alive", "poke"):
            assert not any(forbidden in n.lower() for n in names), forbidden


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


class TestLifecycle:
    def test_is_open_means_the_link_not_a_contact(self, adu):
        """Framework meaning, matching ``agent/registry.py``, and the opposite
        sign to a relay's "open". The two senses coexist in this driver, so the
        assertion is that they stay distinct."""
        adu.set_relay_state(0, True)
        assert adu.is_open is True  # the link
        assert adu.relay_state(0) is True  # energised, i.e. contact CLOSED
        adu.close()
        assert adu.is_open is False

    def test_no_relay_facing_name_borrows_the_word_open(self):
        """A naming rule, enforced. ``is_open`` is the link; anything about a
        relay says energised. A method named ``close_relay()`` would also be
        remotely callable with no writer claim, since dispatch derives mutators
        from prefixes."""
        for name in dir(OntrakADU218):
            if name.startswith("_") or name in ("open", "close", "is_open"):
                continue
            lowered = name.lower()
            for word in ("open", "close", "opened", "closed"):
                assert word not in lowered, (
                    f"{name} borrows '{word}', which points the opposite way "
                    f"for a relay than it does for a link"
                )

    def test_close_does_not_de_energise(self, model):
        """A relay left conducting stays conducting. A teardown that silently
        dropped contacts would make every ``with`` block a bench event, and the
        driver cannot know whether an energised relay is holding something that
        must not be interrupted."""
        link = SimulatedAdu218Link()
        with OntrakADU218.open(link=link) as adu:
            adu.set_relay_state(3, True)
        assert link.device_model.relay_state(3) is True
        assert "MK000" not in link.device_model.command_log

    def test_close_does_not_disarm_the_watchdog(self):
        """Sharper than the above: if ``WD`` is armed, releasing the device *is*
        the silence it exists to detect. Disarming here would defeat exactly the
        case it was armed for."""
        link = SimulatedAdu218Link()
        adu = OntrakADU218.open(link=link)
        adu.set_watchdog(3)
        adu.close()
        assert link.device_model.watchdog == 3

    def test_close_is_idempotent(self, adu):
        adu.close()
        adu.close()
        assert adu.is_open is False

    def test_open_reports_relays_it_found_energised(self, caplog):
        """Power-on relay state is undocumented and USB suspend holds outputs
        in their last state, so ``open()`` reads and reports rather than
        assuming — and it drives ``MK000`` only when asked."""
        link = SimulatedAdu218Link(relays=0b00000110)
        with caplog.at_level("WARNING"):
            adu = OntrakADU218.open(link=link)
        try:
            assert "[1, 2]" in caplog.text
            assert link.device_model.relays == 0b00000110  # left as found
            assert "MK000" not in link.device_model.command_log
        finally:
            adu.close()

    def test_identity_needs_no_round_trip(self, adu, model):
        """This device has no identity command. The values come from the
        enumeration the kernel already did, so identity cannot fail mid-session
        and costs nothing."""
        before = len(model.command_log)
        info = adu.read_identity()
        assert model.command_log[before:] == []
        assert isinstance(info, ADU218Info)
        assert info.model == "ADU218"
        assert info.vendor_id == 0x0A07
        assert info.product_id == 0x00DA

    def test_identity_reports_no_firmware_field(self, adu):
        """``bcdDevice`` is 0000 on this unit, so there is no version to
        report. An always-``None`` field would invite callers to read its
        absence as "old firmware"; inventing one from the product string would
        be a guess wearing a measurement's name."""
        keys = adu.read_identity().to_dict()
        assert not any("firmware" in k or "version" in k for k in keys)

    def test_identity_before_open_raises_rather_than_guessing(self):
        adu = OntrakADU218(link=SimulatedAdu218Link())
        with pytest.raises(ADU218ConnectionError):
            adu.read_identity()


# ---------------------------------------------------------------------------
# Silence, and the framing it cannot be distinguished from
# ---------------------------------------------------------------------------


class TestSilence:
    def test_a_responsive_command_that_answers_nothing_raises(self, adu, model):
        """It does not return ``None``. On a device whose only error signal is
        silence, a ``None`` callers must remember to check is exactly the shape
        that becomes an unnoticed wrong answer."""
        model.handle = lambda command: None  # accepts everything, answers nothing
        with pytest.raises(ADU218TimeoutError, match="declared responsive"):
            adu.relay_states()

    def test_the_timeout_message_names_the_ambiguity(self, adu, model):
        """The exception cannot say *why* the device was silent, and pretending
        otherwise would be the misdiagnosis this device invites."""
        model.handle = lambda command: None
        with pytest.raises(ADU218TimeoutError) as exc:
            adu.read_debounce()
        assert "unknown command" in str(exc.value)

    def test_a_reply_of_the_wrong_width_is_a_protocol_error(self, adu, model):
        """A desynced reply is a plausible value otherwise. ``PK`` returns 3
        characters; a 1-character answer belongs to some other command."""
        model.replies.append("0")
        model.handle = lambda command: None
        with pytest.raises(ADU218ProtocolError, match="desynchronised"):
            adu.relay_states()

    def test_a_non_numeric_payload_is_a_protocol_error(self, adu, model):
        original = model._dispatch
        model._dispatch = lambda command: "abc" if command == "PK" else original(command)
        with pytest.raises(ADU218ProtocolError, match="not a decimal"):
            adu.relay_states()

    def test_the_session_survives_a_silence(self, adu, model):
        """The mitigating half of the headline finding (``errors.txt``): unlike
        the SDM4065A, an unanswered command does not poison the session. The
        next valid command answers normally."""
        model.handle("ZZZ")
        model.handle("RPK9")
        assert adu.relay_states() == dict.fromkeys(range(8), False)

    def test_an_unread_reply_would_be_the_next_query_s_answer(self, adu, model):
        """The failure ``drain()`` exists to prevent, demonstrated.

        Replies queue on the endpoint rather than overwriting a slot, so a
        skipped read returns the *previous* command's answer to the *next*
        query — a silently wrong value, not an exception. This asserts the
        simulator reproduces it, which is what makes the drain test meaningful.
        """
        model.relays = 0b00000001
        model.handle("PK")  # queue a reply nobody reads
        model.relays = 0b11111111
        # The next read gets the STALE 001, not the current 255.
        assert adu._link.read() == "001"

    def test_open_drains_a_reply_left_by_a_previous_process(self):
        link = SimulatedAdu218Link()
        link.device_model.replies.append("099")
        adu = OntrakADU218.open(link=link)
        try:
            assert link.device_model.replies == []
            assert adu.relay_states()[0] is False
        finally:
            adu.close()


# ---------------------------------------------------------------------------
# The dispatch gate — the guard on the naming decision
# ---------------------------------------------------------------------------


class TestDispatchGate:
    """``agent/dispatch.py`` derives which calls need a writer claim purely
    from name prefixes, walking the class with no driver-declared override.

    So a method named ``close_relay()`` would be remotely callable with **no
    claim**. These are the tests that fail when a rename quietly removes the
    gate from relay control.
    """

    def test_every_write_needs_the_writer_claim(self, adu):
        from benchctrl.agent.dispatch import introspect

        surface = introspect(adu, "ontrak_adu218")
        for name in (
            "set_relay_state",
            "set_relay_port",
            "reset_relays",
            "set_debounce",
            "set_watchdog",
            "clear_counter",
        ):
            assert name in surface.mutators, f"{name} is remotely callable with no claim"

    def test_no_read_requires_the_writer_claim(self, adu):
        """The other direction. Adding a prefix to ``_MUTATOR_PREFIXES`` to
        catch a write would also capture reads, making observation a
        privileged operation."""
        from benchctrl.agent.dispatch import introspect

        surface = introspect(adu, "ontrak_adu218")
        for name in (
            "relay_state",
            "relay_states",
            "relay_mask",
            "input_state",
            "input_states",
            "input_mask",
            "read_counter",
            "read_counters",
            "read_debounce",
            "read_debounce_ms",
            "read_watchdog",
            "read_watchdog_tripped",
            "read_identity",
        ):
            assert name in surface.methods, f"{name} is not remotely callable"
            assert name not in surface.mutators, f"{name} should not need a claim"

    def test_clear_counter_needs_a_claim_despite_being_a_read(self, adu):
        """``RCn`` reads *and* clears. It is the only responsive command that
        mutates, and its ``clear_`` prefix is what makes the gate catch it — a
        name like ``read_and_clear_counter`` would not."""
        from benchctrl.agent.dispatch import introspect

        assert "clear_counter" in introspect(adu, "ontrak_adu218").mutators

    def test_the_arming_table_does_not_see_relay_switching(self, adu):
        """Closing a signal relay is not arming an output, and treating it as
        one would start a governor countdown on every switch — a second, weaker
        deadman layered over the device's own hardware watchdog.

        Verified as an intersection rather than assumed, so a rename cannot
        quietly create the overlap.
        """
        from benchctrl.agent.dispatch import introspect
        from benchctrl.agent.safety import _ARMING_CALLS

        surface = introspect(adu, "ontrak_adu218")
        assert set(_ARMING_CALLS) & set(surface.methods) == set()

    def test_the_default_safe_state_is_inert_on_this_device(self, adu, model):
        """Documented, not accidental. The device's hardware watchdog already
        de-energises the relays when the agent stops talking — a mechanism that
        works when this function cannot run at all."""
        from benchctrl.agent.safety import default_safe_state

        adu.set_relay_port(0b00001111)
        before = list(model.command_log)
        default_safe_state(adu)
        assert model.command_log == before
        assert model.relays == 0b00001111

    def test_it_is_not_treated_as_a_switched_pdu(self):
        """``SWITCHED_PDU_KEYS`` gates run-engine outlet setpoints and means
        "switches mains". These are 1 A SSRs on instrument leads."""
        from benchctrl.agent.registry import SWITCHED_PDU_KEYS

        assert "ontrak_adu218" not in SWITCHED_PDU_KEYS

    def test_it_earns_a_rail_slot_rather_than_the_mains_panel(self):
        """The other half of the same distinction: the FUI's ``PDU_KEYS`` get a
        fixed mains panel, and this device belongs on the instrument rail."""
        from benchctrl.dashboards.fui.view import INSTRUMENTS, PDU_KEYS

        assert "ontrak_adu218" not in PDU_KEYS
        assert any(spec["key"] == "ontrak_adu218" for spec in INSTRUMENTS)


# ---------------------------------------------------------------------------
# The simulator itself
# ---------------------------------------------------------------------------


class TestSimulatorFidelity:
    """A sim built from the same misreading as the driver agrees with it, and
    the pair passes every test while both are wrong (``sim/qr10x.py`` records
    the real cost). These pin the sim against the hardware captures instead.
    """

    def test_every_reply_width_matches_the_reads_capture(self):
        """Read from ``reads.txt`` at test time rather than transcribed, so the
        sim is checked against the artefact rather than against my memory of
        it."""
        import pathlib
        import re

        text = pathlib.Path("tests/fixtures/adu218/reads.txt").read_text()
        captured = {}
        for line in text.splitlines():
            match = re.match(r"^(\w+)\s+-> b'.*?'\s+payload='(.*?)'\s+len=(\d+)", line)
            if match:
                captured[match.group(1)] = int(match.group(3))
        assert captured, "the capture parsed to nothing; the format changed"

        model = SimulatedADU218()
        for command, width in captured.items():
            model.handle(command)
            assert model.replies, f"{command} answered nothing but hardware answered"
            assert len(model.replies.pop(0)) == width, f"{command} width"

    def test_the_commands_hardware_ignored_are_ignored_here(self):
        """From ``errors.txt`` and ``reads.txt``. ``RI`` is the interesting one:
        the manual's summary table names it, and only ``PI`` answers."""
        model = SimulatedADU218()
        for command in ("RI", "XYZ", "RPK8", "RPK9", "RPA4", "RPB4", "RE8",
                        "RPK", "MK", "MK9999", "DB9", "RK0", "MK000", "DB1"):
            model.handle(command)
            assert model.replies == [], f"{command} answered but hardware was silent"

    def test_the_report_prefix_must_be_exactly_one(self):
        """From ``framing.txt``: bare ASCII, ``0x00`` and ``0x02`` were all
        measured silent. A sim accepting "any non-ASCII lead byte" would let a
        driver ship with the wrong prefix."""
        import ctypes

        from benchctrl.drivers.ontrak_adu218.usbfs import EP_OUT, PACKET_SIZE

        link = SimulatedAdu218Link()
        link.open()
        for prefix in (0x00, 0x02, ord("P")):
            buffer = (ctypes.c_ubyte * PACKET_SIZE)()
            buffer[0] = prefix
            buffer[1] = ord("P")
            buffer[2] = ord("K")
            link._transfer(EP_OUT, buffer, 200)
            assert link.device_model.replies == [], f"prefix {prefix:#04x} answered"
        # And the control: 0x01 does answer, so this is not passing because
        # nothing works.
        link.write("PK")
        assert link.device_model.replies == ["000"]

    def test_the_sim_is_the_production_link_with_one_override(self):
        """The whole reason framing is trustworthy here. If this becomes a
        standalone stand-in, these tests stop covering the shipping code."""
        from benchctrl.drivers.ontrak_adu218.usbfs import Adu218UsbfsLink

        assert issubclass(SimulatedAdu218Link, Adu218UsbfsLink)
        overridden = {
            name
            for name, value in vars(SimulatedAdu218Link).items()
            if not name.startswith("__") and name in vars(Adu218UsbfsLink)
        }
        assert overridden <= {"_transfer", "open", "close", "is_open", "__repr__"}, (
            f"the sim overrides production behaviour beyond the ioctl seam: "
            f"{sorted(overridden)}"
        )

    def test_the_manual_clock_is_the_default(self):
        """Wall-clock watchdog tests are races. A sim that used real time would
        make the ladder untestable and the tests flaky in CI."""
        model = SimulatedADU218()
        model.advance(0.0)  # does not raise
        import time

        real = SimulatedADU218(clock=time.monotonic)
        with pytest.raises(RuntimeError, match="manual clock"):
            real.advance(1.0)

    def test_the_sim_serial_is_obviously_synthetic(self):
        """A sim claiming the bench unit's serial (``E02246``) makes captured
        logs impossible to attribute."""
        assert "SIM" in SimulatedADU218().serial
        assert "E02246" not in SimulatedADU218().serial

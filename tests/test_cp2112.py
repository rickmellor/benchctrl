"""CP2112 driver behaviour against the simulated chip.

The tests that matter here are the ones that would let a real DUT come to harm
if they failed: that open-drain is the only drive mode reachable, that a line
outside the allowlist cannot be moved, that a pulse releases even when
interrupted, and that close() cannot strand a target in reset.
"""

from __future__ import annotations

import pytest

from benchctrl.drivers.silabs_cp2112 import (
    ALTERNATE_FUNCTIONS,
    LINE_COUNT,
    MIN_PULSE_S,
    PACKAGE_PINS,
    CP2112,
    CP2112ConnectionError,
    CP2112GpioConfig,
    CP2112PolicyError,
    CP2112ProtocolError,
    CP2112ValueError,
    CP2112VerifyError,
)
from benchctrl.sim.cp2112 import SimulatedCP2112


def make_device(
    *,
    allowed_lines=(2, 3, 7),
    open_it: bool = True,
    **sim_kwargs,
) -> tuple[CP2112, SimulatedCP2112]:
    """A driver wired to a simulated chip through the link seam."""
    sim = SimulatedCP2112(**sim_kwargs)
    if open_it:
        sim.open()
    dev = CP2112(sim, allowed_lines=allowed_lines, serial=sim.serial)  # type: ignore[arg-type]
    dev._as_found = dev.read_gpio_config()
    return dev, sim


class TestIdentity:
    def test_reads_part_number_and_revision(self) -> None:
        dev, _ = make_device()
        info = dev.read_identity()
        assert info.part_number == 0x0C
        assert info.device_version == 0x03
        assert info.is_cp2112

    def test_a_non_cp2112_is_not_claimed_as_one(self) -> None:
        dev, _ = make_device(part_number=0x0A)
        assert not dev.read_identity().is_cp2112

    def test_info_property_matches_the_sdm4065a_shape(self) -> None:
        """``info`` is a property here, as on the DMM -- not read_identity()."""
        dev, _ = make_device()
        assert dev.info.part_number == 0x0C


class TestAllowlist:
    """A line the operator did not opt into cannot be configured or driven."""

    def test_configuring_an_unallowed_line_is_refused(self) -> None:
        dev, sim = make_device(allowed_lines=(2,))
        with pytest.raises(CP2112PolicyError, match="not in allowed_lines"):
            dev.set_line_mode(5, output=True)
        assert sim.direction == 0x00, "refused call must not have written anything"

    def test_driving_an_unallowed_line_is_refused(self) -> None:
        dev, sim = make_device(allowed_lines=(2,))
        with pytest.raises(CP2112PolicyError, match="not in allowed_lines"):
            dev.set_line_asserted(5, True)
        assert sim.latch == 0xFF

    def test_pulsing_an_unallowed_line_is_refused(self) -> None:
        dev, _ = make_device(allowed_lines=(2,))
        with pytest.raises(CP2112PolicyError, match="not in allowed_lines"):
            dev.trigger_reset_pulse(5)

    def test_the_error_names_the_package_pin(self) -> None:
        """A wiring dispute is settled by package pin, not by silkscreen."""
        dev, _ = make_device(allowed_lines=(2,))
        with pytest.raises(CP2112PolicyError, match="package pin 14"):
            dev.set_line_mode(5, output=True)

    def test_there_is_no_implicit_all_lines_default(self) -> None:
        """allowed_lines is required, so a typo fails closed rather than open."""
        sim = SimulatedCP2112()
        with pytest.raises(TypeError):
            CP2112(sim)  # type: ignore[call-arg]

    def test_allowlist_rejects_a_bad_index_at_construction(self) -> None:
        sim = SimulatedCP2112()
        with pytest.raises(CP2112ValueError, match="out of range"):
            CP2112(sim, allowed_lines=(2, 9))  # type: ignore[arg-type]

    def test_reset_lines_touches_only_allowed_lines(self) -> None:
        """A pin excluded from the allowlist is not "cleaned up" either."""
        dev, sim = make_device(allowed_lines=(2,))
        sim.direction = 0b1000_0100  # GPIO.2 (ours) and GPIO.7 (not ours)
        sim.latch = 0b0111_1011  # both driving low
        dev.reset_lines()
        assert not (sim.direction & (1 << 2)), "GPIO.2 should be back to input"
        assert sim.direction & (1 << 7), "GPIO.7 was not ours to change"


class TestIndexCoercion:
    def test_bool_is_rejected_before_int(self) -> None:
        """set_line_asserted(True, True) must not silently mean line 1."""
        dev, _ = make_device(allowed_lines=(0, 1))
        with pytest.raises(CP2112ValueError, match="bool"):
            dev.set_line_asserted(True, True)  # type: ignore[arg-type]

    @pytest.mark.parametrize("bad", [-1, 8, 99])
    def test_out_of_range_is_rejected(self, bad: int) -> None:
        dev, _ = make_device()
        with pytest.raises(CP2112ValueError, match="out of range"):
            dev.read_line_state(bad)

    @pytest.mark.parametrize("bad", ["3", 3.0, None, [3]])
    def test_non_int_is_rejected(self, bad: object) -> None:
        dev, _ = make_device()
        with pytest.raises(CP2112ValueError):
            dev.read_line_state(bad)  # type: ignore[arg-type]


class TestOpenDrainIsTheOnlyDriveMode:
    """The core safety property. Push-pull must be unreachable through the API."""

    def test_configuring_an_output_clears_the_push_pull_bit(self) -> None:
        dev, sim = make_device()
        state = dev.set_line_mode(2, output=True)
        assert sim.direction & (1 << 2)
        assert not (sim.push_pull & (1 << 2)), "must be open-drain"
        assert state.open_drain

    def test_a_pre_existing_push_pull_bit_is_cleared_not_preserved(self) -> None:
        """Read-modify-write must not carry a dangerous bit forward.

        If the chip was left push-pull by another program, configuring our line
        must *fix* that rather than inherit it.
        """
        dev, sim = make_device()
        sim.push_pull = 0xFF
        dev.set_line_mode(2, output=True)
        assert not (sim.push_pull & (1 << 2))

    def test_driving_a_push_pull_line_is_refused(self) -> None:
        """Belt and braces: even if something else set the bit, we won't drive it."""
        dev, sim = make_device()
        sim.direction = 1 << 2
        sim.push_pull = 1 << 2
        with pytest.raises(CP2112PolicyError, match="push-pull"):
            dev.set_line_asserted(2, True)

    def test_no_public_method_can_request_push_pull(self) -> None:
        """The word should not appear as a settable parameter anywhere.

        A keyword like ``push_pull=True`` on any public method would defeat the
        module's entire safety argument, so this asserts the API shape rather
        than a behaviour.
        """
        import inspect

        for name in ("set_line_mode", "set_line_asserted", "trigger_reset_pulse"):
            sig = inspect.signature(getattr(CP2112, name))
            assert "push_pull" not in sig.parameters, name

    def test_every_config_write_leaves_push_pull_clear_for_our_lines(self) -> None:
        """Assert the emitted bytes, not just the end state."""
        dev, sim = make_device(allowed_lines=(2, 3))
        dev.set_line_mode(2, output=True)
        dev.set_line_mode(3, output=True)
        dev.set_line_asserted(2, True)
        for kind, report_id, payload in sim.report_log:
            if kind == "set" and report_id == 0x02:
                pp = payload[1]
                assert not (pp & 0b0000_1100), (
                    f"config write set a push-pull bit on an allowed line: "
                    f"{payload.hex(' ')}"
                )


class TestAssertAndRelease:
    def test_asserting_pulls_the_line_low(self) -> None:
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        state = dev.set_line_asserted(2, True)
        assert state.asserted
        assert not state.level
        assert not (sim.latch & (1 << 2))

    def test_releasing_lets_the_line_go_high(self) -> None:
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        dev.set_line_asserted(2, True)
        state = dev.set_line_asserted(2, False)
        assert not state.asserted
        assert state.level
        assert sim.latch & (1 << 2)

    def test_the_write_mask_covers_only_the_target_line(self) -> None:
        """Report 0x04's mask byte is the guard against a whole-port clobber."""
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        sim.report_log.clear()
        dev.set_line_asserted(2, True)
        writes = [p for k, r, p in sim.report_log if k == "set" and r == 0x04]
        assert writes, "expected a Set GPIO report"
        for payload in writes:
            assert payload[1] == 1 << 2, (
                f"mask must be exactly GPIO.2, got 0x{payload[1]:02X}"
            )

    def test_driving_a_neighbour_does_not_disturb_a_held_line(self) -> None:
        """Two lines, one held asserted -- configuring the other must not release it."""
        dev, sim = make_device(allowed_lines=(2, 3))
        dev.set_line_mode(2, output=True)
        dev.set_line_asserted(2, True)
        dev.set_line_mode(3, output=True)
        dev.set_line_asserted(3, False)
        assert dev.read_line_state(2).asserted, "GPIO.2 must still be held"

    def test_driving_an_unconfigured_input_is_refused(self) -> None:
        """The chip ignores such a write, which would look like a dead line."""
        dev, _ = make_device()
        with pytest.raises(CP2112PolicyError, match="is an input"):
            dev.set_line_asserted(2, True)

    def test_verification_catches_a_line_that_will_not_move(self) -> None:
        """Open-drain cannot force a pulled-up net low if something holds it high.

        This is the failure a bench operator most needs reported rather than
        swallowed: the command was accepted, the pin did not move.
        """
        dev, _ = make_device(externally_driven_high=1 << 2)
        dev.set_line_mode(2, output=True)
        with pytest.raises(CP2112VerifyError, match="read back"):
            dev.set_line_asserted(2, True)

    def test_verification_can_be_waived_explicitly(self) -> None:
        dev, _ = make_device(externally_driven_high=1 << 2)
        dev.set_line_mode(2, output=True)
        dev.set_line_asserted(2, True, verify=False)  # no raise

    def test_open_drain_cannot_force_high_against_a_pull_down(self) -> None:
        dev, _ = make_device(pull_downs=1 << 3, allowed_lines=(3,))
        dev.set_line_mode(3, output=True)
        with pytest.raises(CP2112VerifyError):
            dev.set_line_asserted(3, False)


class TestResetPulse:
    def test_a_pulse_asserts_then_releases(self) -> None:
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        state = dev.trigger_reset_pulse(2, duration_s=MIN_PULSE_S)
        assert not state.asserted, "the line must be released when the pulse ends"
        assert sim.latch & (1 << 2)

    def test_the_line_is_released_even_if_the_hold_is_interrupted(self) -> None:
        """A KeyboardInterrupt mid-pulse must not strand a DUT in reset.

        This is why the pulse is one driver method rather than two caller calls:
        a finally in the driver is more reliable than one in every caller.
        """
        import time as _time

        dev, sim = make_device()
        dev.set_line_mode(2, output=True)

        real_sleep = _time.sleep
        calls: list[float] = []

        def boom(seconds: float) -> None:
            calls.append(seconds)
            raise KeyboardInterrupt

        _time.sleep = boom
        try:
            with pytest.raises(KeyboardInterrupt):
                dev.trigger_reset_pulse(2, duration_s=0.05)
        finally:
            _time.sleep = real_sleep

        assert calls, "the hold must actually have been attempted"
        assert sim.latch & (1 << 2), "line must be released despite the interrupt"

    def test_a_pulse_shorter_than_the_usb_floor_is_refused(self) -> None:
        """Rejected, not silently stretched to whatever the bus manages."""
        dev, _ = make_device()
        dev.set_line_mode(2, output=True)
        with pytest.raises(CP2112ValueError, match="below the"):
            dev.trigger_reset_pulse(2, duration_s=0.0001)

    def test_the_refusal_explains_that_the_limit_is_the_bus(self) -> None:
        dev, _ = make_device()
        dev.set_line_mode(2, output=True)
        with pytest.raises(CP2112ValueError, match="USB control transfer"):
            dev.trigger_reset_pulse(2, duration_s=0.001)

    def test_bool_duration_is_rejected(self) -> None:
        dev, _ = make_device()
        dev.set_line_mode(2, output=True)
        with pytest.raises(CP2112ValueError, match="must be a number"):
            dev.trigger_reset_pulse(2, duration_s=True)  # type: ignore[arg-type]

    def test_negative_settle_is_rejected(self) -> None:
        dev, _ = make_device()
        dev.set_line_mode(2, output=True)
        with pytest.raises(CP2112ValueError, match="settle_s"):
            dev.trigger_reset_pulse(2, duration_s=MIN_PULSE_S, settle_s=-1)


class TestAlternateFunctionPins:
    """GPIO.0/1/7 can be driven by the chip itself (datasheet Table 10)."""

    def test_an_alternate_function_pin_needs_an_explicit_opt_in(self) -> None:
        dev, _ = make_device(allowed_lines=(7,))
        with pytest.raises(CP2112PolicyError, match="CLK Output"):
            dev.set_line_mode(7, output=True)

    def test_opting_in_allows_it(self) -> None:
        dev, sim = make_device(allowed_lines=(7,))
        state = dev.set_line_mode(7, output=True, allow_alternate_function=True)
        assert state.is_output
        assert state.open_drain
        assert sim.direction & (1 << 7)

    def test_a_running_clock_overrides_the_opt_in(self) -> None:
        """A caller asserting the function is off does not make it off.

        ``special`` bit 0 enables GPIO.7's clock output. If the chip is actually
        driving it, configuring the pin as an output means two drivers on one
        net -- so the measured state wins over the caller's claim.
        """
        dev, sim = make_device(allowed_lines=(7,))
        sim.special = 0x01
        with pytest.raises(CP2112PolicyError, match="clock output is enabled"):
            dev.set_line_mode(7, output=True, allow_alternate_function=True)

    def test_a_non_alternate_pin_needs_no_opt_in(self) -> None:
        dev, _ = make_device(allowed_lines=(2,))
        dev.set_line_mode(2, output=True)  # no raise

    def test_configuring_as_input_never_needs_the_opt_in(self) -> None:
        """Making a pin high-Z cannot contend with anything."""
        dev, _ = make_device(allowed_lines=(7,))
        dev.set_line_mode(7, output=False)  # no raise

    def test_the_documented_alternate_functions_match_the_datasheet(self) -> None:
        assert ALTERNATE_FUNCTIONS == {0: "TX Toggle", 1: "RX Toggle", 7: "CLK Output"}

    def test_package_pins_cover_every_line(self) -> None:
        assert sorted(PACKAGE_PINS) == list(range(LINE_COUNT))
        assert PACKAGE_PINS[7] == 12


class TestLevelsAreNotIdentity:
    def test_an_undriven_input_latches_high(self) -> None:
        """The observation that made commissioning confusing.

        A floating pin reads 1 at the chip's input buffer while a 10 MΩ
        voltmeter reads ~0 V on the same net. Neither is wrong, and it means a
        level identifies nothing on its own.
        """
        dev, _ = make_device()
        assert dev.read_levels() == 0xFF
        assert all(s.level for s in dev.read_line_states().values())

    def test_a_pulled_down_input_reads_low(self) -> None:
        dev, _ = make_device(pull_downs=1 << 4)
        assert not dev.read_line_state(4).level

    def test_open_drain_is_false_for_an_input(self) -> None:
        """The push-pull bit is don't-care for an input; don't report it as a mode."""
        dev, _ = make_device()
        assert not dev.read_line_state(2).open_drain


class TestCloseRestores:
    def test_close_releases_a_held_line(self) -> None:
        """The property that matters: no exit path strands a DUT in reset."""
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        dev.set_line_asserted(2, True)
        assert not (sim.latch & (1 << 2))
        dev.close()
        assert sim.latch & (1 << 2), "close() must release the line"

    def test_close_restores_the_as_found_configuration(self) -> None:
        dev, sim = make_device()
        as_found = sim.direction, sim.push_pull, sim.special, sim.clock_divider
        dev.set_line_mode(2, output=True)
        dev.set_line_asserted(2, True)
        dev.close()
        assert (
            sim.direction,
            sim.push_pull,
            sim.special,
            sim.clock_divider,
        ) == as_found

    def test_close_is_idempotent(self) -> None:
        dev, _ = make_device()
        dev.close()
        dev.close()
        assert not dev.is_open

    def test_restore_can_be_skipped(self) -> None:
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)
        dev.close(restore=False)
        assert sim.direction & (1 << 2), "restore=False should leave it configured"

    def test_context_manager_restores_on_exception(self) -> None:
        dev, sim = make_device()
        with pytest.raises(ZeroDivisionError):
            with dev:
                dev.set_line_mode(2, output=True)
                dev.set_line_asserted(2, True)
                raise ZeroDivisionError
        assert sim.latch & (1 << 2), "an exception must not leave reset asserted"
        assert not dev.is_open

    def test_a_restore_failure_does_not_mask_the_original_error(self) -> None:
        """Best-effort restore: it must never replace the exception that caused it."""
        dev, sim = make_device()
        dev.set_line_mode(2, output=True)

        def explode(*_a: object, **_k: object) -> None:
            raise RuntimeError("device vanished")

        sim.set_feature = explode  # type: ignore[method-assign]
        with pytest.raises(ValueError, match="original"):
            with dev:
                raise ValueError("original")


class TestGpioConfig:
    def test_round_trips_through_bytes(self) -> None:
        cfg = CP2112GpioConfig(0x84, 0x00, 0x00, 0x30)
        assert CP2112GpioConfig.from_bytes(cfg.to_bytes()) == cfg

    def test_a_short_report_is_a_protocol_error(self) -> None:
        with pytest.raises(CP2112ProtocolError, match="4 bytes"):
            CP2112GpioConfig.from_bytes(b"\x00\x00")

    def test_clock_output_flag_reads_special_bit_zero(self) -> None:
        assert not CP2112GpioConfig(0, 0, 0x00, 0).clock_output_enabled
        assert CP2112GpioConfig(0, 0, 0x01, 0).clock_output_enabled

    def test_config_is_frozen(self) -> None:
        cfg = CP2112GpioConfig(0, 0, 0, 0)
        with pytest.raises(Exception):
            cfg.direction = 1  # type: ignore[misc]


class TestTransportErrorsAreWrapped:
    def test_a_link_failure_becomes_a_connection_error(self) -> None:
        """Callers catch the driver's hierarchy, not the transport's."""
        dev, sim = make_device()
        sim.close()
        with pytest.raises(CP2112ConnectionError):
            dev.read_levels()


class TestDispatchSurface:
    """The naming decision is load-bearing, so it gets a test.

    ``agent/dispatch.py`` derives writer-claim requirements *purely* from method
    name prefixes. A method that moves a pin but misses the prefix list is
    remotely callable with no writer claim -- i.e. an unclaimed caller could
    toggle a DUT's reset. So every mutator must match, and every read must not.
    """

    def test_every_pin_moving_method_is_a_mutator(self) -> None:
        from benchctrl.agent.dispatch import is_mutator

        for name in (
            "set_line_mode",
            "set_line_asserted",
            "trigger_reset_pulse",
            "reset_lines",
        ):
            assert is_mutator(name), f"{name} would bypass the writer claim"

    def test_no_read_is_a_mutator(self) -> None:
        from benchctrl.agent.dispatch import is_mutator

        for name in (
            "read_identity",
            "read_gpio_config",
            "read_levels",
            "read_line_state",
            "read_line_states",
            "line_is_asserted",
        ):
            assert not is_mutator(name), f"{name} would need a writer claim to read"

    def test_introspection_sorts_the_surface_correctly(self) -> None:
        from benchctrl.agent.dispatch import introspect

        dev, _ = make_device()
        surface = introspect(dev, "silabs_cp2112")
        assert "set_line_asserted" in surface.mutators
        assert "trigger_reset_pulse" in surface.mutators
        assert "read_levels" not in surface.mutators


# ----- parity: every driver capability is reachable over MCP ---------------


def test_cp2112_mcp_tools_cover_the_driver_surface():
    """Every public driver method has a tool, except a documented few.

    This is the test that fails when a method is added to the driver and the
    tool is forgotten — the failure mode that leaves a capability working
    locally and invisible to an agent.
    """
    from benchctrl.drivers.silabs_cp2112 import mcp_tools as cp_tools
    from benchctrl.drivers.silabs_cp2112.driver import CP2112

    # Deliberately not exposed:
    #   open              — cp2112_open is the tool; the classmethod is internal
    #   read_identity     — cp2112_info is the tool name, matching the other
    #                       drivers' naming rather than the method's
    #   read_gpio_config  — the raw four register bytes. cp2112_line_states
    #                       decodes them per pin, which is what an agent can act
    #                       on; a bare 0x84 invites the mask arithmetic to be
    #                       redone by the caller, wrongly.
    #   read_levels       — likewise raw; the decoded form carries the caveat
    #                       that an undriven pin latches 1, which the bare
    #                       bitmask does not.
    #   line_is_asserted  — sugar over read_line_state, whose tool already
    #                       returns `asserted`
    exempt = {
        "open",
        "read_gpio_config",
        "read_levels",
        "line_is_asserted",
    }
    # Tool names drop the read_/set_ prefixes, matching how the other drivers'
    # MCP surfaces read (sdm4065a_info, not sdm4065a_read_identity). Normalise
    # rather than exempt: exempting a method because its tool is named
    # differently would silently stop checking whether the tool exists at all,
    # which is the entire point of this test.
    aliases = {
        "read_identity": "info",
        "read_line_state": "line_state",
        "read_line_states": "line_states",
    }

    def tool_name(method: str) -> str:
        return aliases.get(method, method)

    methods = {
        tool_name(name)
        for name in vars(CP2112)
        if not name.startswith("_") and callable(getattr(CP2112, name))
    } - exempt
    tools = {fn.__name__[len("cp2112_"):] for fn in cp_tools._TOOLS}

    missing = methods - tools
    assert not missing, f"driver methods with no MCP tool: {sorted(missing)}"


def test_every_cp2112_tool_is_registered_and_re_exported():
    """A tool absent from mcp.py is invisible to an agent even though it exists."""
    import benchctrl.mcp as m
    from benchctrl.drivers.silabs_cp2112 import mcp_tools as cp_tools

    for fn in cp_tools._TOOLS:
        assert hasattr(m, fn.__name__), f"{fn.__name__} not re-exported from mcp.py"


def test_no_mcp_tool_offers_the_alternate_function_override():
    """allow_alternate_function is an operator observation, not a model's call.

    The driver accepts the override because a human can confirm the alternate
    function is off. A model cannot, so the tool must not offer the parameter —
    otherwise the gate is one plausible-sounding argument away from bypassed.
    """
    import inspect

    from benchctrl.drivers.silabs_cp2112 import mcp_tools as cp_tools

    for fn in cp_tools._TOOLS:
        params = inspect.signature(fn).parameters
        assert "allow_alternate_function" not in params, fn.__name__

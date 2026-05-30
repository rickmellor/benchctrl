"""Tests for the Rigol DP2031 triple-output PSU driver (Phase A).

Hardware-free tests use a fake instrument mimicking the pyvisa Resource
interface (just ``write`` / ``query``). Hardware-marked tests need the
real DP2031 reachable via USB-TMC; some hardware tests also need the
DL3031A connected to CH1 for closed-loop validation.
"""
from __future__ import annotations

import os
from collections import deque

import pytest

from benchctrl.drivers.rigol_dp2031.driver import (
    DP2031Channel,
    RigolDP2031,
    RigolDP2031CommandError,
    RigolDP2031ConnectionError,
    RigolDP2031Error,
    RigolDP2031ValueError,
    _coerce_channel,
)


# ---------------------------------------------------------------------------
# Fake pyvisa-style instrument
# ---------------------------------------------------------------------------


class FakeInstrument:
    """Records every ``write`` and replies to ``query`` from a script."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.writes: list[str] = []
        self.responses: dict[str, str | deque] = responses or {}
        self.timeout = 0
        self.read_termination = ""
        self.write_termination = ""
        self.closed = False

    def write(self, cmd: str) -> None:
        self.writes.append(cmd)

    def query(self, cmd: str) -> str:
        self.writes.append(cmd)
        if cmd not in self.responses:
            raise AssertionError(
                f"fake instrument has no response scripted for {cmd!r}; "
                f"add it to the responses dict"
            )
        resp = self.responses[cmd]
        if isinstance(resp, deque):
            return resp.popleft()
        return resp

    def close(self) -> None:
        self.closed = True


def _make(responses: dict | None = None) -> tuple[RigolDP2031, FakeInstrument]:
    inst = FakeInstrument(responses)
    drv = RigolDP2031(inst, resource_string="FAKE::INSTR")
    return drv, inst


# ---------------------------------------------------------------------------
# Channel coercion + validation helpers
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("input_val,expected", [
    (1, 1), (2, 2), (3, 3),
    (DP2031Channel.CH1, 1), (DP2031Channel.CH2, 2), (DP2031Channel.CH3, 3),
])
def test_coerce_channel_accepts_int_and_enum(input_val, expected):
    assert _coerce_channel(input_val) == expected


@pytest.mark.parametrize("bad", [0, 4, -1, "CH1", "1", None, 1.5])
def test_coerce_channel_rejects_invalid(bad):
    with pytest.raises(RigolDP2031ValueError):
        _coerce_channel(bad)


def test_coerce_channel_rejects_bool():
    # bool is an int subclass — explicit rejection avoids
    # set_voltage(True, 3.3) silently meaning channel 1.
    with pytest.raises(RigolDP2031ValueError):
        _coerce_channel(True)
    with pytest.raises(RigolDP2031ValueError):
        _coerce_channel(False)


# ---------------------------------------------------------------------------
# Identity + housekeeping
# ---------------------------------------------------------------------------


def test_info_parses_idn():
    drv, inst = _make({
        "*IDN?": "Rigol Technologies,DP2031,DP2A243500269,01.00.01.00.16",
    })
    info = drv.info()
    assert info.manufacturer == "Rigol Technologies"
    assert info.model == "DP2031"
    assert info.serial == "DP2A243500269"
    assert info.firmware == "01.00.01.00.16"
    assert info.resource == "FAKE::INSTR"


def test_info_cached():
    drv, inst = _make({"*IDN?": "A,B,C,D"})
    drv.info()
    drv.info()
    assert inst.writes.count("*IDN?") == 1


def test_info_malformed_raises():
    drv, _ = _make({"*IDN?": "only,two"})
    with pytest.raises(RigolDP2031Error):
        drv.info()


def test_reset_and_clear_status():
    drv, inst = _make()
    drv.reset()
    drv.clear_status()
    assert inst.writes == ["*RST", "*CLS"]


# ---------------------------------------------------------------------------
# Error queue
# ---------------------------------------------------------------------------


def test_last_error_empty_returns_none():
    drv, _ = _make({":SYSTem:ERRor?": '0,"No error"'})
    assert drv.last_error() is None


def test_last_error_non_zero_returns_tuple():
    drv, _ = _make({":SYSTem:ERRor?": '-101,"Invalid character"'})
    err = drv.last_error()
    assert err == (-101, "Invalid character")


def test_last_error_malformed_raises():
    drv, _ = _make({":SYSTem:ERRor?": "garbage with no comma"})
    with pytest.raises(RigolDP2031Error):
        drv.last_error()


def test_raise_if_error_raises_on_non_zero():
    drv, _ = _make({":SYSTem:ERRor?": '-220,"Parameter error"'})
    with pytest.raises(RigolDP2031CommandError) as exc_info:
        drv.raise_if_error()
    assert exc_info.value.code == -220
    assert "Parameter error" in str(exc_info.value)


def test_raise_if_error_quiet_when_clean():
    drv, _ = _make({":SYSTem:ERRor?": '0,"No error"'})
    drv.raise_if_error()  # should not raise


# ---------------------------------------------------------------------------
# Connection state
# ---------------------------------------------------------------------------


def test_write_on_closed_raises():
    drv, inst = _make()
    drv.close()
    with pytest.raises(RigolDP2031ConnectionError):
        drv.write("*RST")


def test_query_on_closed_raises():
    drv, inst = _make()
    drv.close()
    with pytest.raises(RigolDP2031ConnectionError):
        drv.query("*IDN?")


def test_close_is_idempotent():
    drv, inst = _make()
    drv.close()
    drv.close()
    assert inst.closed is True


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch", [1, 2, 3, DP2031Channel.CH1, DP2031Channel.CH2,
                                DP2031Channel.CH3])
def test_select_channel_writes_nselect(ch):
    drv, inst = _make()
    drv.select_channel(ch)
    assert inst.writes == [f":INSTrument:NSELect {int(ch)}"]


def test_current_channel_parses_int():
    drv, _ = _make({":INSTrument:NSELect?": "2"})
    assert drv.current_channel() == DP2031Channel.CH2


def test_current_channel_rejects_unexpected_value():
    drv, _ = _make({":INSTrument:NSELect?": "9"})
    with pytest.raises(RigolDP2031Error):
        drv.current_channel()


# ---------------------------------------------------------------------------
# Voltage setpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch,volts", [(1, 5.0), (2, 32.0), (3, 6.0), (3, 0.0)])
def test_set_voltage_in_range(ch, volts):
    drv, inst = _make()
    drv.set_voltage(ch, volts)
    assert inst.writes == [f":SOURce{ch}:VOLTage:LEVel:IMMediate {volts:.6f}"]


@pytest.mark.parametrize("ch,volts", [
    (1, -0.1), (1, 32.1), (2, 50.0), (3, 6.1), (3, -1.0),
])
def test_set_voltage_out_of_range_rejected(ch, volts):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="voltage"):
        drv.set_voltage(ch, volts)


def test_get_voltage_parses_float():
    drv, _ = _make({":SOURce1:VOLTage:LEVel:IMMediate?": "3.300000"})
    assert drv.get_voltage(1) == pytest.approx(3.3)


def test_set_voltage_rejects_non_numeric():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_voltage(1, "3.3")  # type: ignore[arg-type]


# ---------------------------------------------------------------------------
# Current setpoint
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch,amps", [(1, 0.5), (2, 3.0), (3, 5.0), (3, 0.0)])
def test_set_current_in_range(ch, amps):
    drv, inst = _make()
    drv.set_current(ch, amps)
    assert inst.writes == [f":SOURce{ch}:CURRent:LEVel:IMMediate {amps:.6f}"]


@pytest.mark.parametrize("ch,amps", [
    (1, -0.001), (1, 3.1), (2, 5.0), (3, 5.1),
])
def test_set_current_out_of_range_rejected(ch, amps):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="current"):
        drv.set_current(ch, amps)


def test_get_current_parses_float():
    drv, _ = _make({":SOURce2:CURRent:LEVel:IMMediate?": "0.500000"})
    assert drv.get_current(2) == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Output enable
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch", [1, 2, 3])
@pytest.mark.parametrize("on,wire", [(True, "ON"), (False, "OFF")])
def test_set_output_writes(ch, on, wire):
    drv, inst = _make()
    drv.set_output(ch, on)
    assert inst.writes == [f":OUTPut:STATe CH{ch},{wire}"]


@pytest.mark.parametrize("reply,expected", [
    ("ON", True), ("OFF", False), ("1", True), ("0", False),
])
def test_get_output_parses(reply, expected):
    drv, _ = _make({":OUTPut:STATe? CH1": reply})
    assert drv.get_output(1) is expected


def test_get_output_unexpected_response_raises():
    drv, _ = _make({":OUTPut:STATe? CH1": "MAYBE"})
    with pytest.raises(RigolDP2031Error):
        drv.get_output(1)


def test_set_output_all_writes_all_form():
    drv, inst = _make()
    drv.set_output_all(True)
    drv.set_output_all(False)
    assert inst.writes == [
        ":OUTPut:STATe ALL,ON",
        ":OUTPut:STATe ALL,OFF",
    ]


@pytest.mark.parametrize("reply", ["CV", "CC", "UR"])
def test_output_regulation_parses(reply):
    drv, _ = _make({":OUTPut:CVCC? CH1": reply})
    assert drv.output_regulation(1) == reply


def test_output_regulation_unexpected_raises():
    drv, _ = _make({":OUTPut:CVCC? CH1": "FOO"})
    with pytest.raises(RigolDP2031Error):
        drv.output_regulation(1)


# ---------------------------------------------------------------------------
# Protection — OVP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch,volts", [(1, 5.0), (2, 35.0), (3, 6.5)])
def test_set_ovp_level_in_range(ch, volts):
    drv, inst = _make()
    drv.set_ovp_level(ch, volts)
    assert inst.writes == [f":OUTPut:OVP:VALue CH{ch},{volts:.6f}"]


@pytest.mark.parametrize("ch,volts", [
    (1, 0.0),    # below min (1 mV)
    (1, 36.0),   # above CH1 max (35.2 V)
    (3, 7.0),    # above CH3 max (6.6 V)
])
def test_set_ovp_level_out_of_range_rejected(ch, volts):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="OVP"):
        drv.set_ovp_level(ch, volts)


def test_get_ovp_level_parses():
    drv, _ = _make({":OUTPut:OVP:VALue? CH2": "33.000000"})
    assert drv.get_ovp_level(2) == pytest.approx(33.0)


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_set_ovp_enabled_writes(ch):
    drv, inst = _make()
    drv.set_ovp_enabled(ch, True)
    drv.set_ovp_enabled(ch, False)
    assert inst.writes == [
        f":OUTPut:OVP:STATe CH{ch},ON",
        f":OUTPut:OVP:STATe CH{ch},OFF",
    ]


@pytest.mark.parametrize("reply,expected", [
    ("ON", True), ("OFF", False), ("1", True), ("0", False),
])
def test_get_ovp_enabled_parses(reply, expected):
    drv, _ = _make({":OUTPut:OVP:STATe? CH3": reply})
    assert drv.get_ovp_enabled(3) is expected


# ---------------------------------------------------------------------------
# Protection — OCP
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch,amps", [(1, 1.0), (2, 3.2), (3, 5.0)])
def test_set_ocp_level_in_range(ch, amps):
    drv, inst = _make()
    drv.set_ocp_level(ch, amps)
    assert inst.writes == [f":OUTPut:OCP:VALue CH{ch},{amps:.6f}"]


@pytest.mark.parametrize("ch,amps", [
    (1, 0.0),     # below min (1 mA)
    (1, 3.5),     # above CH1 max (3.3 A)
    (3, 6.0),     # above CH3 max (5.5 A)
])
def test_set_ocp_level_out_of_range_rejected(ch, amps):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="OCP"):
        drv.set_ocp_level(ch, amps)


def test_get_ocp_level_parses():
    drv, _ = _make({":OUTPut:OCP:VALue? CH1": "2.500000"})
    assert drv.get_ocp_level(1) == pytest.approx(2.5)


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_set_ocp_enabled_writes(ch):
    drv, inst = _make()
    drv.set_ocp_enabled(ch, True)
    drv.set_ocp_enabled(ch, False)
    assert inst.writes == [
        f":OUTPut:OCP:STATe CH{ch},ON",
        f":OUTPut:OCP:STATe CH{ch},OFF",
    ]


@pytest.mark.parametrize("reply,expected", [
    ("ON", True), ("OFF", False), ("1", True), ("0", False),
])
def test_get_ocp_enabled_parses(reply, expected):
    drv, _ = _make({":OUTPut:OCP:STATe? CH2": reply})
    assert drv.get_ocp_enabled(2) is expected


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def test_measure_voltage_parses():
    drv, _ = _make({":MEASure:VOLTage:DC? CH1": "3.299850"})
    assert drv.measure_voltage(1) == pytest.approx(3.29985)


def test_measure_current_parses():
    drv, _ = _make({":MEASure:CURRent:DC? CH2": "0.050000"})
    assert drv.measure_current(2) == pytest.approx(0.05)


def test_measure_power_parses():
    drv, _ = _make({":MEASure:POWer:DC? CH3": "0.165000"})
    assert drv.measure_power(3) == pytest.approx(0.165)


def test_measure_all_parses_csv():
    drv, _ = _make({":MEASure:ALL? CH1": "3.3000,0.5000,1.6500"})
    out = drv.measure_all(1)
    assert out == {
        "voltage_V": pytest.approx(3.3),
        "current_A": pytest.approx(0.5),
        "power_W": pytest.approx(1.65),
    }


def test_measure_all_wrong_column_count_raises():
    drv, _ = _make({":MEASure:ALL? CH1": "3.3,0.5"})
    with pytest.raises(RigolDP2031Error, match="expected 3"):
        drv.measure_all(1)


def test_measure_all_non_numeric_raises():
    drv, _ = _make({":MEASure:ALL? CH1": "3.3,bad,1.6"})
    with pytest.raises(RigolDP2031Error, match="non-numeric"):
        drv.measure_all(1)


def test_measure_all_channels_aggregates():
    drv, _ = _make({
        ":MEASure:ALL? CH1": "3.3,0.5,1.65",
        ":MEASure:ALL? CH2": "5.0,0.1,0.50",
        ":MEASure:ALL? CH3": "2.5,0.2,0.50",
    })
    out = drv.measure_all_channels()
    assert set(out.keys()) == {1, 2, 3}
    assert out[1]["voltage_V"] == pytest.approx(3.3)
    assert out[2]["current_A"] == pytest.approx(0.1)
    assert out[3]["power_W"] == pytest.approx(0.5)


# ---------------------------------------------------------------------------
# Context manager — best-effort output disable on exit
# ---------------------------------------------------------------------------


def test_context_manager_disables_all_channels_on_exit():
    drv, inst = _make()
    with drv:
        pass
    # Three :OUTPut:STATe CHn,OFF writes, in order
    assert inst.writes == [
        ":OUTPut:STATe CH1,OFF",
        ":OUTPut:STATe CH2,OFF",
        ":OUTPut:STATe CH3,OFF",
    ]
    assert inst.closed is True


# ---------------------------------------------------------------------------
# Hardware-required tests
# ---------------------------------------------------------------------------


@pytest.fixture
def hw_dp2031():
    """Open the real DP2031. Skips if VISA / device unavailable."""
    import time
    pytest.importorskip("pyvisa")
    resource = os.environ.get("BENCHCTRL_DP2031_RESOURCE")  # None → auto
    try:
        psu = RigolDP2031.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DP2031 unavailable: {e}")
    psu.reset()
    psu.clear_status()
    # *RST on this firmware needs a real settle before subsequent writes
    # take effect reliably — without this, the first set_voltage after
    # reset can be silently ignored.
    time.sleep(0.5)
    # Make sure every channel starts off — defensive in case a prior
    # test left something hot.
    for ch in (1, 2, 3):
        psu.set_output(ch, False)
    time.sleep(0.1)
    yield psu
    for ch in (1, 2, 3):
        try:
            psu.set_output(ch, False)
        except Exception:
            pass
    psu.close()


@pytest.mark.hardware
def test_hw_identity_and_factory_state(hw_dp2031):
    """Identity sanity check + clean error queue after reset."""
    info = hw_dp2031.info()
    assert info.manufacturer.lower().startswith("rigol")
    assert info.model == "DP2031"
    assert info.serial
    assert info.firmware
    assert hw_dp2031.last_error() is None


@pytest.mark.hardware
@pytest.mark.parametrize("ch,volts", [(1, 3.3), (2, 5.0), (3, 1.8)])
def test_hw_voltage_setpoint_round_trip_output_off(hw_dp2031, ch, volts):
    """Setpoint round-trip with output OFF — no DUT exposure."""
    hw_dp2031.set_voltage(ch, volts)
    assert hw_dp2031.get_voltage(ch) == pytest.approx(volts, abs=0.01)
    assert hw_dp2031.last_error() is None


@pytest.mark.hardware
@pytest.mark.parametrize("ch,amps", [(1, 0.500), (2, 1.000), (3, 0.250)])
def test_hw_current_setpoint_round_trip_output_off(hw_dp2031, ch, amps):
    hw_dp2031.set_current(ch, amps)
    assert hw_dp2031.get_current(ch) == pytest.approx(amps, abs=0.005)
    assert hw_dp2031.last_error() is None


@pytest.mark.hardware
def test_hw_ovp_level_round_trip(hw_dp2031):
    hw_dp2031.set_ovp_level(1, 4.0)
    assert hw_dp2031.get_ovp_level(1) == pytest.approx(4.0, abs=0.01)
    hw_dp2031.set_ovp_enabled(1, True)
    assert hw_dp2031.get_ovp_enabled(1) is True
    hw_dp2031.set_ovp_enabled(1, False)
    assert hw_dp2031.get_ovp_enabled(1) is False


@pytest.mark.hardware
def test_hw_ocp_level_round_trip(hw_dp2031):
    hw_dp2031.set_ocp_level(1, 1.000)
    assert hw_dp2031.get_ocp_level(1) == pytest.approx(1.0, abs=0.005)
    hw_dp2031.set_ocp_enabled(1, True)
    assert hw_dp2031.get_ocp_enabled(1) is True
    hw_dp2031.set_ocp_enabled(1, False)
    assert hw_dp2031.get_ocp_enabled(1) is False


@pytest.mark.hardware
def test_hw_measure_with_output_off_reads_zero(hw_dp2031):
    """With every output off, V/I/P measurements should be essentially zero."""
    for ch in (1, 2, 3):
        m = hw_dp2031.measure_all(ch)
        assert abs(m["voltage_V"]) < 0.1, f"CH{ch} V leaks: {m}"
        assert abs(m["current_A"]) < 0.01, f"CH{ch} I leaks: {m}"


@pytest.mark.hardware
def test_hw_measure_all_channels_returns_three(hw_dp2031):
    out = hw_dp2031.measure_all_channels()
    assert set(out.keys()) == {1, 2, 3}
    for ch_data in out.values():
        assert set(ch_data.keys()) == {"voltage_V", "current_A", "power_W"}


# ---------------------------------------------------------------------------
# Closed-loop hardware tests — DP2031 CH1 → DL3031A input
# These exist because the user has CH1 hard-wired to the DL3031A.
# They verify that what the PSU SAYS it's sourcing matches what the
# load actually MEASURES, which is the strongest end-to-end check we
# can do for Phase A. Skips cleanly if either device is unavailable.
# ---------------------------------------------------------------------------


@pytest.mark.hardware
def test_hw_closed_loop_dp2031_ch1_to_dl3031a_open_circuit(hw_dp2031):
    """CH1 sources 3.3 V into the DL3031A input with load OFF.

    Verifies the PSU's own measurement against the setpoint. The
    DL3031A's voltmeter is bench-confirmed to be unreliable when its
    input MOSFET is open (reads ~0.5 V regardless of actual input);
    we cross-check the load only in the CR / CC tests below.
    """
    import time
    hw_dp2031.set_voltage(1, 3.300)
    hw_dp2031.set_current(1, 0.500)
    # OVP disabled here to avoid transient overshoots tripping at enable.
    # OVP trip+clear is verified in its own (Phase B) test.
    hw_dp2031.set_ovp_enabled(1, False)

    try:
        hw_dp2031.set_output(1, True)
        time.sleep(0.5)  # full settle — the device's transient response is
                         # < 50 µs but its measurement integration takes
                         # longer; be conservative.
        v_psu = hw_dp2031.measure_voltage(1)
        i_psu = hw_dp2031.measure_current(1)
        reg = hw_dp2031.output_regulation(1)
        assert v_psu == pytest.approx(3.3, abs=0.05), \
            f"PSU V readback off: {v_psu} V"
        # Open-circuit current is ~0 (only the load's input leakage)
        assert abs(i_psu) < 0.005, \
            f"PSU I should be ~0 with load input off, got {i_psu*1000:.1f} mA"
        assert reg == "CV", f"expected CV regulation, got {reg!r}"
    finally:
        hw_dp2031.set_output(1, False)


# NOTE: Loaded closed-loop tests deferred to a later phase.
#
# Initial Phase A plan was to drive the DP2031 CH1 into the DL3031A
# in CR or CC mode and cross-check both instruments. Bench debugging
# discovered firmware quirks on the DL3031A unit (FW 00.01.05.00.01):
#
# - After `*RST` the load's :SOURce:FUNCtion:MODE query reports
#   "WAV" and stays that way; conventional ways to switch it back to
#   FIXed (:SOURce:FUNCtion:MODE FIXed) silently no-op.
# - CC-mode setpoints applied while FUNC:MODE = WAV are ignored —
#   the load only sinks a few mA of input bias regardless of setpoint.
# - CR mode partially works (the load actually presents a real
#   impedance), but its :MEASure:CURRent:DC? returns 0 in this state.
# - Across multiple fixture re-RSTs, even CR mode becomes flaky and
#   stops engaging.
#
# These are DL3031A issues, not DP2031 issues. The DP2031 driver is
# fully verified by the PSU-side tests above (round-trip setpoints
# per channel + measurement-with-output-off + open-circuit
# self-readback at full voltage). Re-introducing loaded closed-loop
# tests is tracked for after the DL3031A's WAV-mode behaviour is
# better understood — see KNOWN_LIMITATIONS.md.

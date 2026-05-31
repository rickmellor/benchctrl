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
    _coerce_channel_or_all,
    _parse_delay_ms,
    _parse_timer_group_payload,
    _query_block_payload,
    _strip_block_header_bytes,
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
# Phase B — protection trip / clear / delay
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_clear_ovp_writes(ch):
    drv, inst = _make()
    drv.clear_ovp(ch)
    assert inst.writes == [f":OUTPut:OVP:CLEar CH{ch}"]


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_clear_ocp_writes(ch):
    drv, inst = _make()
    drv.clear_ocp(ch)
    assert inst.writes == [f":OUTPut:OCP:CLEar CH{ch}"]


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_ovp_tripped_parses(reply, expected):
    drv, _ = _make({":OUTPut:OVP:ALAR? CH1": reply})
    assert drv.ovp_tripped(1) is expected


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_ocp_tripped_parses(reply, expected):
    drv, _ = _make({":OUTPut:OCP:ALAR? CH2": reply})
    assert drv.ocp_tripped(2) is expected


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_ovp_questionable_parses(reply, expected):
    drv, _ = _make({":OUTPut:OVP:QUES? CH3": reply})
    assert drv.ovp_questionable(3) is expected


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_ocp_questionable_parses(reply, expected):
    drv, _ = _make({":OUTPut:OCP:QUES? CH1": reply})
    assert drv.ocp_questionable(1) is expected


@pytest.mark.parametrize("ms", [0, 50, 200, 1000])
def test_set_ocp_delay_ms_writes(ms):
    drv, inst = _make()
    drv.set_ocp_delay_ms(1, ms)
    assert inst.writes == [f":OUTPut:OCP:DELay CH1,{ms}"]


@pytest.mark.parametrize("bad", [-1, 1001, 0.5, "200", True])
def test_set_ocp_delay_ms_rejects_invalid(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_ocp_delay_ms(1, bad)


@pytest.mark.parametrize("raw,expected", [
    ("200ms", 200), ("50ms", 50), ("0ms", 0),
    ("200", 200), ("50", 50),
    ("  100ms  ", 100), ("100MS", 100),
])
def test_parse_delay_ms_accepts_variants(raw, expected):
    assert _parse_delay_ms(raw) == expected


def test_parse_delay_ms_rejects_garbage():
    with pytest.raises(RigolDP2031Error):
        _parse_delay_ms("no number here")


def test_get_ocp_delay_ms_parses_unit_suffix():
    drv, _ = _make({":OUTPut:OCP:DELay? CH3": "200ms"})
    assert drv.get_ocp_delay_ms(3) == 200


# ---------------------------------------------------------------------------
# IEEE 488.2 status / OPC / options
# ---------------------------------------------------------------------------


def test_event_status_register_parses():
    drv, _ = _make({"*ESR?": "+5"})
    assert drv.event_status_register() == 5


def test_set_event_status_enable_writes():
    drv, inst = _make()
    drv.set_event_status_enable(32)
    assert inst.writes == ["*ESE 32"]


@pytest.mark.parametrize("bad", [-1, 256, 1.5, True, "32"])
def test_set_event_status_enable_rejects_invalid(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_event_status_enable(bad)


def test_status_byte_parses():
    drv, _ = _make({"*STB?": "+128"})
    assert drv.status_byte() == 128


def test_set_service_request_enable_writes():
    drv, inst = _make()
    drv.set_service_request_enable(16)
    assert inst.writes == ["*SRE 16"]


def test_wait_op_complete_returns_int():
    drv, _ = _make({"*OPC?": "+1"})
    assert drv.wait_op_complete() == 1


def test_mark_op_complete_writes():
    drv, inst = _make()
    drv.mark_op_complete()
    assert inst.writes == ["*OPC"]


def test_wait_writes():
    drv, inst = _make()
    drv.wait()
    assert inst.writes == ["*WAI"]


@pytest.mark.parametrize("reply,expected", [("+0", 0), ("+1", 1)])
def test_self_test_parses(reply, expected):
    drv, _ = _make({"*TST?": reply})
    assert drv.self_test() == expected


@pytest.mark.parametrize("reply,expected", [
    ("NONE", []),
    ("", []),
    ("DP2000-10A", ["DP2000-10A"]),
    ("DP2000-10A,DP2000-HADC", ["DP2000-10A", "DP2000-HADC"]),
])
def test_installed_options_parses(reply, expected):
    drv, _ = _make({"*OPT?": reply})
    assert drv.installed_options() == expected


def test_set_power_on_status_clear_writes():
    drv, inst = _make()
    drv.set_power_on_status_clear(True)
    drv.set_power_on_status_clear(False)
    assert inst.writes == ["*PSC 1", "*PSC 0"]


@pytest.mark.parametrize("slot", [0, 5, 9])
def test_save_state_writes(slot):
    drv, inst = _make()
    drv.save_state(slot)
    assert inst.writes == [f"*SAV {slot}"]


@pytest.mark.parametrize("slot", [0, 5, 9])
def test_recall_state_writes(slot):
    drv, inst = _make()
    drv.recall_state(slot)
    assert inst.writes == [f"*RCL {slot}"]


@pytest.mark.parametrize("bad", [-1, 10, 0.5, True, "5"])
def test_save_state_rejects_invalid_slot(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.save_state(bad)


# ---------------------------------------------------------------------------
# :STATus subsystem
# ---------------------------------------------------------------------------


def test_operation_condition_parses():
    drv, _ = _make({":STATus:OPERation:CONDition?": "+0"})
    assert drv.operation_condition() == 0


def test_set_operation_enable_writes():
    drv, inst = _make()
    drv.set_operation_enable(7)
    assert inst.writes == [":STATus:OPERation:ENABle 7"]


def test_operation_event_parses():
    drv, _ = _make({":STATus:OPERation:EVENt?": "+3"})
    assert drv.operation_event() == 3


def test_preset_status_writes():
    drv, inst = _make()
    drv.preset_status()
    assert inst.writes == [":STATus:PRESet"]


def test_set_questionable_enable_writes():
    drv, inst = _make()
    drv.set_questionable_enable(2048)
    assert inst.writes == [":STATus:QUEStionable:ENABle 2048"]


def test_questionable_event_parses():
    drv, _ = _make({":STATus:QUEStionable:EVENt?": "+16"})
    assert drv.questionable_event() == 16


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_channel_condition_writes_per_channel(ch):
    drv, _ = _make({
        f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:CONDition?": "+4",
    })
    assert drv.channel_condition(ch) == 4


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_channel_status_event_writes_per_channel(ch):
    drv, _ = _make({
        f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:EVENt?": "+7",
    })
    assert drv.channel_status_event(ch) == 7


def test_health_check_decodes_top_event_bits():
    """OTP bit (bit 4) and FAN bit (bit 11) decoded correctly."""
    drv, _ = _make({
        ":SYSTem:ERRor?": '0,"No error"',
        ":STATus:QUEStionable:EVENt?": str(1 << 4 | 1 << 11),  # OTP + FAN
        ":STATus:QUEStionable:INSTrument:ISUMmary1:CONDition?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary2:CONDition?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary3:CONDition?": "0",
    })
    result = drv.health_check()
    assert result["otp_global"] is True
    assert result["fan_failure"] is True
    assert result["ch1"]["ovp"] is False
    assert result["error_queue"] is None


def test_health_check_decodes_per_channel_bits():
    """Per-channel OVP/OCP bits decoded correctly."""
    drv, _ = _make({
        ":SYSTem:ERRor?": '0,"No error"',
        ":STATus:QUEStionable:EVENt?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary1:CONDition?": str(1 << 2),  # OVP
        ":STATus:QUEStionable:INSTrument:ISUMmary2:CONDition?": str(1 << 3),  # OCP
        ":STATus:QUEStionable:INSTrument:ISUMmary3:CONDition?": "0",
    })
    result = drv.health_check()
    assert result["ch1"]["ovp"] is True
    assert result["ch1"]["ocp"] is False
    assert result["ch2"]["ovp"] is False
    assert result["ch2"]["ocp"] is True
    assert result["ch3"]["ovp"] is False


def test_health_check_surfaces_error_queue():
    drv, _ = _make({
        ":SYSTem:ERRor?": '-220,"Parameter error"',
        ":STATus:QUEStionable:EVENt?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary1:CONDition?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary2:CONDition?": "0",
        ":STATus:QUEStionable:INSTrument:ISUMmary3:CONDition?": "0",
    })
    result = drv.health_check()
    assert result["error_queue"] == {"code": -220, "message": "Parameter error"}


# ---------------------------------------------------------------------------
# System basics
# ---------------------------------------------------------------------------


def test_beep_once_writes():
    drv, inst = _make()
    drv.beep_once()
    assert inst.writes == [":SYSTem:BEEPer:IMMediate"]


def test_set_beeper_writes():
    drv, inst = _make()
    drv.set_beeper(True)
    drv.set_beeper(False)
    assert inst.writes == [
        ":SYSTem:BEEPer:STATe ON",
        ":SYSTem:BEEPer:STATe OFF",
    ]


def test_get_beeper_parses():
    drv, _ = _make({":SYSTem:BEEPer:STATe?": "1"})
    assert drv.get_beeper() is True


def test_set_brightness_writes():
    drv, inst = _make()
    drv.set_brightness(75)
    assert inst.writes == [":SYSTem:BRIGhtness 75"]


@pytest.mark.parametrize("bad", [0, 101, -1, 50.5, True, "50"])
def test_set_brightness_rejects_invalid(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_brightness(bad)


def test_scpi_version_parses():
    drv, _ = _make({":SYSTem:VERSion?": "1999.0"})
    assert drv.scpi_version() == "1999.0"


def test_set_keyboard_lock_writes():
    drv, inst = _make()
    drv.set_keyboard_lock(True)
    assert inst.writes == [":SYSTem:KLOCk:STATe ON"]


def test_set_touchscreen_lock_writes():
    drv, inst = _make()
    drv.set_touchscreen_lock(False)
    assert inst.writes == [":SYSTem:TLOCk OFF"]


def test_set_remote_and_local_write():
    drv, inst = _make()
    drv.set_remote()
    drv.set_local()
    assert inst.writes == [":SYSTem:REMote", ":SYSTem:LOCal"]


def test_set_screen_saver_writes():
    drv, inst = _make()
    drv.set_screen_saver(True)
    assert inst.writes == [":SYSTem:SAVer ON"]


@pytest.mark.parametrize("input_lang,scpi", [
    ("EN", "EN"), ("en", "EN"),
    ("ENGLISH", "EN"), ("english", "EN"),
    ("CH", "CH"), ("CHINESE", "CH"),
    ("DE", "DE"), ("GERMAN", "DE"),
    ("ES", "ES"), ("SPANISH", "ES"),
    ("FR", "FR"), ("FRENCH", "FR"),
])
def test_set_language_accepts_short_and_long_forms(input_lang, scpi):
    drv, inst = _make()
    drv.set_language(input_lang)
    assert inst.writes == [f":SYSTem:LANGuage:TYPE {scpi}"]


@pytest.mark.parametrize("bad", ["JP", "", "EN GLISH", "RU"])
def test_set_language_rejects_unknown(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_language(bad)


@pytest.mark.parametrize("input_mode,scpi", [
    ("DEFault", "DEFault"), ("DEFAULT", "DEFault"),
    ("default", "DEFault"), ("DEF", "DEFault"),
    ("LAST", "LAST"), ("last", "LAST"),
])
def test_set_power_on_mode_accepts_forms(input_mode, scpi):
    drv, inst = _make()
    drv.set_power_on_mode(input_mode)
    assert inst.writes == [f":SYSTem:POWEron {scpi}"]


def test_set_power_on_mode_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_power_on_mode("AUTO")


# ---------------------------------------------------------------------------
# Phase C — pair / tracking / sync / sense / sampling / step / apply / bounds
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("input_val,expected", [
    (1, "CH1"), (2, "CH2"), (3, "CH3"),
    (DP2031Channel.CH1, "CH1"),
    ("ALL", "ALL"), ("all", "ALL"), ("All", "ALL"),
])
def test_coerce_channel_or_all_accepts(input_val, expected):
    assert _coerce_channel_or_all(input_val) == expected


@pytest.mark.parametrize("bad", [0, 4, "CH4", "all_channels", None])
def test_coerce_channel_or_all_rejects_invalid(bad):
    with pytest.raises(RigolDP2031ValueError):
        _coerce_channel_or_all(bad)


# Channel pair


@pytest.mark.parametrize("mode_in,scpi", [
    ("OFF", "OFF"), ("off", "OFF"),
    ("SERies", "SERies"), ("ser", "SERies"), ("SERIES", "SERies"),
    ("PARallel", "PARallel"), ("par", "PARallel"), ("PARALLEL", "PARallel"),
])
def test_set_channel_pair_writes(mode_in, scpi):
    drv, inst = _make()
    drv.set_channel_pair(mode_in)
    assert inst.writes == [f":OUTPut:PAIR {scpi}"]


@pytest.mark.parametrize("bad", ["BOTH", "INDE", "ON", ""])
def test_set_channel_pair_rejects_unknown(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_channel_pair(bad)


@pytest.mark.parametrize("reply,expected", [
    ("OFF", "OFF"), ("SERIES", "SERIES"), ("PARALLEL", "PARALLEL"),
    ("ser", "SER"), ("  off  ", "OFF"),
])
def test_get_channel_pair_normalizes_to_upper(reply, expected):
    drv, _ = _make({":OUTPut:PAIR?": reply})
    assert drv.get_channel_pair() == expected


# Tracking + track mode + sync


def test_set_tracking_writes():
    drv, inst = _make()
    drv.set_tracking(True)
    drv.set_tracking(False)
    assert inst.writes == [":OUTPut:TRACk ON", ":OUTPut:TRACk OFF"]


@pytest.mark.parametrize("reply,expected", [
    ("0", False), ("1", True),
    ("0 ", False), ("1 ", True),  # trailing-space form bench-observed
])
def test_get_tracking_parses(reply, expected):
    drv, _ = _make({":OUTPut:TRACk?": reply})
    assert drv.get_tracking() is expected


@pytest.mark.parametrize("mode_in,scpi", [
    ("SYNC", "SYNC"), ("sync", "SYNC"),
    ("SYNCHRONOUS", "SYNC"), ("synchronous", "SYNC"),
    ("INDE", "INDE"), ("INDEPENDENT", "INDE"),
])
def test_set_track_mode_writes(mode_in, scpi):
    drv, inst = _make()
    drv.set_track_mode(mode_in)
    assert inst.writes == [f":SYSTem:TMODe {scpi}"]


def test_set_track_mode_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_track_mode("BOTH")


def test_get_track_mode_returns_device_reply():
    drv, _ = _make({":SYSTem:TMODe?": "SYNCHRONOUS"})
    assert drv.get_track_mode() == "SYNCHRONOUS"


def test_set_output_sync_writes():
    drv, inst = _make()
    drv.set_output_sync(True)
    assert inst.writes == [":SYSTem:SYNC ON"]


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_get_output_sync_parses(reply, expected):
    drv, _ = _make({":SYSTem:SYNC?": reply})
    assert drv.get_output_sync() is expected


# Remote sense


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_set_remote_sense_per_channel_writes(ch):
    drv, inst = _make()
    drv.set_remote_sense(ch, True)
    drv.set_remote_sense(ch, False)
    assert inst.writes == [
        f":SYSTem:SENSe CH{ch},ON",
        f":SYSTem:SENSe CH{ch},OFF",
    ]


def test_set_remote_sense_all_form():
    drv, inst = _make()
    drv.set_remote_sense("ALL", True)
    assert inst.writes == [":SYSTem:SENSe ALL,ON"]


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_get_remote_sense_parses(reply, expected):
    drv, _ = _make({":SYSTem:SENSe? CH2": reply})
    assert drv.get_remote_sense(2) is expected


# Sampling mode


@pytest.mark.parametrize("mode", ["AUTO", "HIGH", "LOW"])
def test_set_sampling_mode_writes(mode):
    drv, inst = _make()
    drv.set_sampling_mode(mode)
    assert inst.writes == [f":SYSTem:SAMPling {mode}"]


@pytest.mark.parametrize("mode", ["auto", "high", "low"])
def test_set_sampling_mode_accepts_lowercase(mode):
    drv, inst = _make()
    drv.set_sampling_mode(mode)
    assert inst.writes == [f":SYSTem:SAMPling {mode.upper()}"]


@pytest.mark.parametrize("bad", ["MEDIUM", "FAST", "AUTOLOW", ""])
def test_set_sampling_mode_rejects_unknown(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_sampling_mode(bad)


def test_get_sampling_mode_normalises_upper():
    drv, _ = _make({":SYSTem:SAMPling?": "auto"})
    assert drv.get_sampling_mode() == "AUTO"


# Voltage / current step


@pytest.mark.parametrize("ch,step", [(1, 0.1), (2, 0.5), (3, 0.01)])
def test_set_voltage_step_writes(ch, step):
    drv, inst = _make()
    drv.set_voltage_step(ch, step)
    assert inst.writes == [
        f":SOURce{ch}:VOLTage:LEVel:IMMediate:STEP {step:.6f}"
    ]


def test_get_voltage_step_parses_with_trailing_space():
    drv, _ = _make({":SOURce1:VOLTage:LEVel:IMMediate:STEP?": "0.100 "})
    assert drv.get_voltage_step(1) == pytest.approx(0.1)


@pytest.mark.parametrize("ch,step", [(1, 0.05), (3, 0.5)])
def test_set_current_step_writes(ch, step):
    drv, inst = _make()
    drv.set_current_step(ch, step)
    assert inst.writes == [
        f":SOURce{ch}:CURRent:LEVel:IMMediate:STEP {step:.6f}"
    ]


def test_get_current_step_parses():
    drv, _ = _make({":SOURce2:CURRent:LEVel:IMMediate:STEP?": "0.0500"})
    assert drv.get_current_step(2) == pytest.approx(0.05)


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_step_voltage_up_down_writes(ch):
    drv, inst = _make()
    drv.step_voltage_up(ch)
    drv.step_voltage_down(ch)
    assert inst.writes == [
        f":SOURce{ch}:VOLTage:LEVel:IMMediate UP",
        f":SOURce{ch}:VOLTage:LEVel:IMMediate DOWN",
    ]


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_step_current_up_down_writes(ch):
    drv, inst = _make()
    drv.step_current_up(ch)
    drv.step_current_down(ch)
    assert inst.writes == [
        f":SOURce{ch}:CURRent:LEVel:IMMediate UP",
        f":SOURce{ch}:CURRent:LEVel:IMMediate DOWN",
    ]


# Apply


def test_apply_with_v_and_i_writes_full():
    drv, inst = _make()
    drv.apply(1, voltage=5.0, current=1.5)
    assert inst.writes == [":APPLy CH1,5.000000,1.500000"]


def test_apply_with_v_only_writes_two_args():
    drv, inst = _make()
    drv.apply(2, voltage=3.3)
    assert inst.writes == [":APPLy CH2,3.300000"]


def test_apply_with_no_args_just_selects_channel():
    drv, inst = _make()
    drv.apply(3)
    assert inst.writes == [":APPLy CH3"]


def test_apply_with_i_only_uses_source_path():
    """No bare current-positional form on :APPLy; we fall back to
    the SOURce setter to honour 'leave V alone' semantics."""
    drv, inst = _make()
    drv.apply(1, current=0.5)
    assert inst.writes == [":SOURce1:CURRent:LEVel:IMMediate 0.500000"]


@pytest.mark.parametrize("kw,val", [
    ("voltage", 100.0),    # CH1 over-range
    ("current", 10.0),     # CH1 over-range
])
def test_apply_validates_arguments(kw, val):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.apply(1, **{kw: val})


def test_query_applied_no_option_parses_triplet():
    drv, _ = _make({":APPLy? CH1": "CH1:32V/3A,5.000,1.5000"})
    result = drv.query_applied(1)
    assert result == ("CH1:32V/3A", pytest.approx(5.0), pytest.approx(1.5))


def test_query_applied_malformed_raises():
    drv, _ = _make({":APPLy? CH1": "only,two"})
    with pytest.raises(RigolDP2031Error, match="expected"):
        drv.query_applied(1)


def test_query_applied_volt_option_returns_float():
    drv, _ = _make({":APPLy? CH1,VOLT": "5.000"})
    assert drv.query_applied(1, option="VOLT") == pytest.approx(5.0)


def test_query_applied_curr_option_returns_float():
    drv, _ = _make({":APPLy? CH2,CURR": "1.5000"})
    assert drv.query_applied(2, option="CURR") == pytest.approx(1.5)


def test_query_applied_invalid_option_rejected():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.query_applied(1, option="POWER")


# Bounds queries


def test_voltage_bounds_queries_all_three():
    drv, _ = _make({
        ":SOURce1:VOLTage? MIN": "0.000",
        ":SOURce1:VOLTage? MAX": "33.600",
        ":SOURce1:VOLTage? DEF": "0.000",
    })
    lo, hi, dflt = drv.voltage_bounds(1)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(33.6)
    assert dflt == pytest.approx(0.0)


def test_current_bounds_queries_all_three():
    drv, _ = _make({
        ":SOURce3:CURRent? MIN": "0.0000",
        ":SOURce3:CURRent? MAX": "5.2500",
        ":SOURce3:CURRent? DEF": "0.1000",
    })
    lo, hi, dflt = drv.current_bounds(3)
    assert lo == pytest.approx(0.0)
    assert hi == pytest.approx(5.25)
    assert dflt == pytest.approx(0.1)


# ---------------------------------------------------------------------------
# Phase D — IEEE 488.2 block parser
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("raw,expected", [
    # Standard: '#9' = 9 length digits, '000000021' = length 21
    ("#9000000021" + "3,0.000,0.000,0.300;", "3,0.000,0.000,0.300;"),
    # Short: #2 = 2 length digits, '10' = length 10
    ("#210" + "1234567890", "1234567890"),
    # Length 0 — empty payload
    ("#10", ""),
    # Trailing null + whitespace stripped
    ("#9000000021" + "3,0.000,0.000,0.300;" + "\x00", "3,0.000,0.000,0.300;"),
    ("#9000000021" + "3,0.000,0.000,0.300;" + "\r\n", "3,0.000,0.000,0.300;"),
])
def test_query_block_payload_parses(raw, expected):
    assert _query_block_payload(raw) == expected


@pytest.mark.parametrize("bad", [
    "no hash prefix",
    "#",
    "#X1234",  # length-digit isn't a digit
    "",
])
def test_query_block_payload_rejects_malformed(bad):
    with pytest.raises(RigolDP2031Error):
        _query_block_payload(bad)


def test_parse_timer_group_payload_single():
    payload = "1,3.300,0.500,1.000;"
    result = _parse_timer_group_payload(payload)
    assert result == [(1, 3.3, 0.5, 1.0)]


def test_parse_timer_group_payload_multiple():
    payload = "1,3.300,0.500,1.000;2,1.000,0.100,0.500;3,0.000,0.000,0.300;"
    result = _parse_timer_group_payload(payload)
    assert len(result) == 3
    assert result[0] == (1, 3.3, 0.5, 1.0)
    assert result[1] == (2, 1.0, 0.1, 0.5)
    assert result[2] == (3, 0.0, 0.0, 0.3)


def test_parse_timer_group_payload_handles_null_terminator():
    payload = "1,3.300,0.500,1.000;\x00"
    result = _parse_timer_group_payload(payload)
    assert result == [(1, 3.3, 0.5, 1.0)]


def test_parse_timer_group_payload_rejects_malformed_chunk():
    payload = "1,3.300,0.500;"  # only 3 fields
    with pytest.raises(RigolDP2031Error, match="must be 'idx,V,I,t'"):
        _parse_timer_group_payload(payload)


def test_parse_timer_group_payload_rejects_non_numeric():
    payload = "1,three,0.5,1.0;"
    with pytest.raises(RigolDP2031Error, match="non-numeric"):
        _parse_timer_group_payload(payload)


@pytest.mark.parametrize("data,expected", [
    # Standard: '#9' = 9 length digits, '000000005' = length 5
    (b"#9000000005" + b"hello", b"hello"),
    # BMP-style block — magic 'BM' inside the payload
    (b"#7" + b"0000010" + b"BM" + b"\x00" * 8, b"BM" + b"\x00" * 8),
    # No header — return as-is (minus trailing terminators)
    (b"plain bytes", b"plain bytes"),
    (b"plain bytes\x00\n", b"plain bytes"),
])
def test_strip_block_header_bytes_handles_variants(data, expected):
    assert _strip_block_header_bytes(data) == expected


def test_strip_block_header_bytes_short_input_returns_as_is():
    assert _strip_block_header_bytes(b"#") == b"#"


# ---------------------------------------------------------------------------
# Phase D — Timer
# ---------------------------------------------------------------------------


def test_set_timer_enabled_writes():
    drv, inst = _make()
    drv.set_timer_enabled(True)
    drv.set_timer_enabled(False)
    assert inst.writes == [":TIMEr:STATe ON", ":TIMEr:STATe OFF"]


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_get_timer_enabled_parses(reply, expected):
    drv, _ = _make({":TIMEr:STATe?": reply})
    assert drv.get_timer_enabled() is expected


@pytest.mark.parametrize("ch", [1, 2, 3])
def test_set_timer_channel_writes(ch):
    drv, inst = _make()
    drv.set_timer_channel(ch)
    assert inst.writes == [f":TIMEr:CHANnel CH{ch}"]


@pytest.mark.parametrize("reply,expected", [
    ("CH1", DP2031Channel.CH1),
    ("CH2", DP2031Channel.CH2),
    ("CH3", DP2031Channel.CH3),
])
def test_get_timer_channel_parses(reply, expected):
    drv, _ = _make({":TIMEr:CHANnel?": reply})
    assert drv.get_timer_channel() == expected


def test_get_timer_channel_rejects_unexpected():
    drv, _ = _make({":TIMEr:CHANnel?": "CH9"})
    with pytest.raises(RigolDP2031Error):
        drv.get_timer_channel()


@pytest.mark.parametrize("count,wire", [
    (1, ":TIMEr:CYCLEs N,1"),
    (5, ":TIMEr:CYCLEs N,5"),
    (99999, ":TIMEr:CYCLEs N,99999"),
    (None, ":TIMEr:CYCLEs I"),
    (0, ":TIMEr:CYCLEs I"),
])
def test_set_timer_cycles_writes(count, wire):
    drv, inst = _make()
    drv.set_timer_cycles(count)
    assert inst.writes == [wire]


@pytest.mark.parametrize("bad", [-1, 100000, 1.5, True, "5"])
def test_set_timer_cycles_rejects_invalid(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_cycles(bad)


@pytest.mark.parametrize("reply,expected", [
    ("N, 1", 1),
    ("N,5", 5),
    ("N, 99999", 99999),
    ("I", None),
])
def test_get_timer_cycles_parses(reply, expected):
    drv, _ = _make({":TIMEr:CYCLEs?": reply})
    assert drv.get_timer_cycles() == expected


def test_get_timer_cycles_malformed_raises():
    drv, _ = _make({":TIMEr:CYCLEs?": "garbage"})
    with pytest.raises(RigolDP2031Error):
        drv.get_timer_cycles()


@pytest.mark.parametrize("mode", ["OFF", "LAST"])
def test_set_timer_end_state_writes(mode):
    drv, inst = _make()
    drv.set_timer_end_state(mode)
    assert inst.writes == [f":TIMEr:ENDState {mode}"]


def test_set_timer_end_state_accepts_lowercase():
    drv, inst = _make()
    drv.set_timer_end_state("off")
    assert inst.writes == [":TIMEr:ENDState OFF"]


def test_set_timer_end_state_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_end_state("MAYBE")


@pytest.mark.parametrize("mode,scpi", [
    ("CONTinue", "CONTinue"), ("CONT", "CONTinue"), ("continue", "CONTinue"),
    ("SINGle", "SINGle"), ("SING", "SINGle"), ("single", "SINGle"),
])
def test_set_timer_run_mode_writes(mode, scpi):
    drv, inst = _make()
    drv.set_timer_run_mode(mode)
    assert inst.writes == [f":TIMEr:RUN {scpi}"]


@pytest.mark.parametrize("source,scpi", [
    ("MANual", "MANual"), ("MANUAL", "MANual"), ("man", "MANual"),
    ("BUS", "BUS"), ("bus", "BUS"),
])
def test_set_timer_trigger_writes(source, scpi):
    drv, inst = _make()
    drv.set_timer_trigger(source)
    assert inst.writes == [f":TIMEr:TRIG {scpi}"]


@pytest.mark.parametrize("idx", [1, 250, 512])
def test_set_timer_group_index_writes(idx):
    drv, inst = _make()
    drv.set_timer_group_index(idx)
    assert inst.writes == [f":TIMEr:GROUP:INDEx {idx}"]


@pytest.mark.parametrize("bad", [0, 513, -1, 1.5, True])
def test_set_timer_group_index_rejects_invalid(bad):
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_group_index(bad)


def test_set_timer_group_params_validates_against_current_channel():
    """set_timer_group_params should reject voltages outside the active
    channel's envelope."""
    drv, inst = _make({":TIMEr:CHANnel?": "CH3"})
    # CH3 max is 6 V — 10 V should reject
    with pytest.raises(RigolDP2031ValueError, match="timer V"):
        drv.set_timer_group_params(10.0, 1.0, 0.5)


def test_set_timer_group_params_dwell_validation():
    drv, _ = _make({":TIMEr:CHANnel?": "CH1"})
    with pytest.raises(RigolDP2031ValueError, match="timer dwell"):
        drv.set_timer_group_params(5.0, 1.0, 0.0005)  # too short
    drv2, _ = _make({":TIMEr:CHANnel?": "CH1"})
    with pytest.raises(RigolDP2031ValueError, match="timer dwell"):
        drv2.set_timer_group_params(5.0, 1.0, 3601.0)  # too long


def test_get_timer_group_params_parses_block_response():
    drv, _ = _make({
        ":TIMEr:GROUP:PARAmeter? 3":
            "#9000000061"
            "1,3.300000,0.500000,1.000000;"
            "2,1.000000,0.100000,0.500000;"
    })
    result = drv.get_timer_group_params(count=3)
    assert len(result) == 2
    assert result[0] == (1, 3.3, 0.5, 1.0)
    assert result[1] == (2, 1.0, 0.1, 0.5)


def test_program_timer_validates_step_count():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="at least one"):
        drv.program_timer(1, [])
    with pytest.raises(RigolDP2031ValueError, match="1–512"):
        drv.program_timer(1, [(5.0, 1.0, 0.1)] * 513)


def test_program_timer_validates_step_shape():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="must be"):
        drv.program_timer(1, [(5.0, 1.0)])  # missing dwell


def test_program_timer_validates_envelope():
    drv, _ = _make()
    # CH1 max V is 32, but try 50 → reject
    with pytest.raises(RigolDP2031ValueError, match="V"):
        drv.program_timer(1, [(50.0, 1.0, 0.5)])


def test_program_timer_writes_correct_sequence():
    drv, inst = _make()
    drv.program_timer(
        2, [(5.0, 0.5, 1.0), (10.0, 1.0, 2.0)],
        cycles=3, end_state="LAST",
        run_mode="SINGle", trigger="BUS",
    )
    # Expected sequence:
    expected_prefix = [
        ":TIMEr:STATe OFF",
        ":TIMEr:CHANnel CH2",
        ":TIMEr:GROUP:INDEx 1",
        ":TIMEr:GROUP:PARAmeter 5.000000,0.500000,1.000000",
        ":TIMEr:GROUP:INDEx 2",
        ":TIMEr:GROUP:PARAmeter 10.000000,1.000000,2.000000",
        ":TIMEr:CYCLEs N,3",
        ":TIMEr:ENDState LAST",
        ":TIMEr:RUN SINGle",
        ":TIMEr:TRIG BUS",
    ]
    assert inst.writes == expected_prefix


# Timer template


@pytest.mark.parametrize("t", ["SINE", "PULSE", "RAMP", "UP", "DN",
                              "UPDN", "RISE", "FALL"])
def test_set_timer_template_writes(t):
    drv, inst = _make()
    drv.set_timer_template(t)
    assert inst.writes == [f":TIMEr:TEMPlet:SELect {t}"]


def test_set_timer_template_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_template("SQUARE")


def test_construct_timer_from_template_writes():
    drv, inst = _make()
    drv.construct_timer_from_template()
    assert inst.writes == [":TIMEr:TEMPlet:CONSTruct"]


def test_set_timer_template_object_with_paired_value():
    drv, inst = _make()
    drv.set_timer_template_object("V", paired_value=2.5)
    assert inst.writes == [":TIMEr:TEMPlet:OBJect V,2.500000"]


def test_set_timer_template_object_no_paired_value():
    drv, inst = _make()
    drv.set_timer_template_object("C")
    assert inst.writes == [":TIMEr:TEMPlet:OBJect C"]


def test_set_timer_template_period_validates_range():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_template_period(0.0005)
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_template_period(3601.0)


def test_set_timer_template_points_validates_range():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_template_points(0)
    with pytest.raises(RigolDP2031ValueError):
        drv.set_timer_template_points(513)


# ---------------------------------------------------------------------------
# Phase D — Analyzer
# ---------------------------------------------------------------------------


def test_set_analyzer_enabled_writes():
    drv, inst = _make()
    drv.set_analyzer_enabled(True)
    drv.set_analyzer_enabled(False)
    assert inst.writes == [":ANALyzer:STATe ON", ":ANALyzer:STATe OFF"]


@pytest.mark.parametrize("t,scpi", [
    ("COM", "COM"), ("common", "COM"), ("COMMON", "COM"),
    ("CURR", "CURR"), ("current", "CURR"),
])
def test_set_analyzer_type_writes(t, scpi):
    drv, inst = _make()
    drv.set_analyzer_type(t)
    assert inst.writes == [f":ANALyzer:TYPE {scpi}"]


def test_set_analyzer_common_objects_writes():
    drv, inst = _make()
    drv.set_analyzer_common_objects("CH1_V", "CH1_C", "CH3_P")
    assert inst.writes == [":ANALyzer:COMMon:MEASure:TYPE CH1_V,CH1_C,CH3_P"]


def test_set_analyzer_common_objects_rejects_invalid():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="CHx_"):
        drv.set_analyzer_common_objects("BAD")
    with pytest.raises(RigolDP2031ValueError, match="1–3 objects"):
        drv.set_analyzer_common_objects()


def test_get_analyzer_common_objects_parses_list():
    drv, _ = _make({":ANALyzer:COMMon:MEASure:TYPE?": "CH1_V,CH2_C"})
    assert drv.get_analyzer_common_objects() == ["CH1_V", "CH2_C"]


def test_get_analyzer_common_objects_empty():
    drv, _ = _make({":ANALyzer:COMMon:MEASure:TYPE?": "  "})
    assert drv.get_analyzer_common_objects() == []


def test_set_analyzer_save_writes():
    drv, inst = _make()
    drv.set_analyzer_save(True)
    assert inst.writes == [":ANALyzer:SAVE:STATe ON"]


def test_set_analyzer_save_path_writes():
    drv, inst = _make()
    drv.set_analyzer_save_path("C:/RA.ROF")
    assert inst.writes == [":ANALyzer:SAVE:ROUTe C:/RA.ROF"]


# ---------------------------------------------------------------------------
# Phase D — Trigger I/O
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("line", ["D1", "D2", "D3", "D4"])
def test_set_trigger_in_enabled_writes(line):
    drv, inst = _make()
    drv.set_trigger_in_enabled(line, True)
    assert inst.writes == [f":TRIGger:IN:ENABle {line},ON"]


def test_set_trigger_in_enabled_rejects_unknown_line():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="D1/D2/D3/D4"):
        drv.set_trigger_in_enabled("D5", True)


@pytest.mark.parametrize("t", ["RISE", "FALL", "HIGH", "LOW"])
def test_set_trigger_in_type_writes(t):
    drv, inst = _make()
    drv.set_trigger_in_type("D1", t)
    assert inst.writes == [f":TRIGger:IN:TYPE D1,{t}"]


def test_set_trigger_in_type_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError):
        drv.set_trigger_in_type("D1", "EDGE")


def test_set_trigger_in_source_writes_channels():
    drv, inst = _make()
    drv.set_trigger_in_source("D1", [1, 2])
    assert inst.writes == [":TRIGger:IN:SOURce D1,CH1,CH2"]


def test_set_trigger_in_source_rejects_string():
    """Earlier bench-discovered: NONE is read-only sentinel, not writable."""
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="list of channels"):
        drv.set_trigger_in_source("D1", "NONE")


def test_set_trigger_in_source_rejects_empty():
    drv, _ = _make()
    with pytest.raises(RigolDP2031ValueError, match="≥ 1 channel"):
        drv.set_trigger_in_source("D1", [])


@pytest.mark.parametrize("reply,expected", [
    ("CH1,CH2", ["CH1", "CH2"]),
    ("CH3", ["CH3"]),
    ("NONE", []),
    ("", []),
])
def test_get_trigger_in_source_parses(reply, expected):
    drv, _ = _make({":TRIGger:IN:SOURce? D1": reply})
    assert drv.get_trigger_in_source("D1") == expected


@pytest.mark.parametrize("r", ["ON", "OFF", "ALTER"])
def test_set_trigger_in_response_writes(r):
    drv, inst = _make()
    drv.set_trigger_in_response("D2", r)
    assert inst.writes == [f":TRIGger:IN:RESPonse D2,{r}"]


def test_trigger_in_immediate_writes():
    drv, inst = _make()
    drv.trigger_in_immediate()
    assert inst.writes == [":TRIGger:IN:IMMEdiate"]


@pytest.mark.parametrize("line", ["D1", "D2", "D3", "D4"])
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_set_trigger_out_source_writes(line, ch):
    drv, inst = _make()
    drv.set_trigger_out_source(line, ch)
    assert inst.writes == [f":TRIGger:OUT:SOURce {line},CH{ch}"]


@pytest.mark.parametrize("pol,scpi", [
    ("POS", "POSitive"), ("POSitive", "POSitive"), ("positive", "POSitive"),
    ("NEG", "NEGative"), ("NEGative", "NEGative"),
])
def test_set_trigger_out_polarity_writes(pol, scpi):
    drv, inst = _make()
    drv.set_trigger_out_polarity("D3", pol)
    assert inst.writes == [f":TRIGger:OUT:POLArity D3,{scpi}"]


# ---------------------------------------------------------------------------
# Phase D — Memory
# ---------------------------------------------------------------------------


def test_list_files_parses_csv():
    drv, _ = _make({":MEMory:CATalog?": "FILE1.RSF,FILE2.RTF,FILE3.BMP"})
    assert drv.list_files() == ["FILE1.RSF", "FILE2.RTF", "FILE3.BMP"]


def test_list_files_empty():
    drv, _ = _make({":MEMory:CATalog?": " "})
    assert drv.list_files() == []


def test_change_directory_writes():
    drv, inst = _make()
    drv.change_directory("D:/folder")
    assert inst.writes == [":MEMory:CDIRectory D:/folder"]


def test_current_directory_returns():
    drv, _ = _make({":MEMory:CDIRectory?": "C:/ "})
    assert drv.current_directory() == "C:/"


def test_delete_file_writes():
    drv, inst = _make()
    drv.delete_file("OLD.RSF")
    assert inst.writes == [":MEMory:DELete OLD.RSF"]


def test_store_file_writes():
    drv, inst = _make()
    drv.store_file("MYSTATE.RSF")
    assert inst.writes == [":MEMory:STORe MYSTATE.RSF"]


def test_load_file_writes():
    drv, inst = _make()
    drv.load_file("MYSTATE.RSF")
    assert inst.writes == [":MEMory:LOAD MYSTATE.RSF"]


@pytest.mark.parametrize("reply,expected", [
    ("NONE", []),
    ("NONE ", []),
    ("", []),
    ("D:/", ["D:/"]),
    ("D:/,E:/", ["D:/", "E:/"]),
])
def test_external_disks_parses(reply, expected):
    drv, _ = _make({":MEMory:DISK?": reply})
    assert drv.external_disks() == expected


@pytest.mark.parametrize("reply,expected", [("0", False), ("1", True)])
def test_file_exists_parses(reply, expected):
    drv, _ = _make({":MEMory:VALid? FOO.RSF": reply})
    assert drv.file_exists("FOO.RSF") is expected


# ---------------------------------------------------------------------------
# Phase D — License + screenshot
# ---------------------------------------------------------------------------


def test_install_license_writes():
    drv, inst = _make()
    drv.install_license("ABCD-1234")
    assert inst.writes == [":LIC:SET ABCD-1234"]


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
    """Open the real DP2031. Skips if VISA / device unavailable.

    Bench-discovered firmware quirks make a clean reset state harder
    than a simple ``*RST``:

    - PAIR mode (SERies / PARallel) survives ``*RST`` — must be
      cleared explicitly.
    - OVP latch can survive ``*RST`` — drain via per-channel
      :py:meth:`clear_ovp`.
    - PAIR mode transitions take > 1 s to settle.
    - Tracking + sync states can also linger.

    This fixture does the full belt-and-suspenders cleanup so tests
    can rely on a known starting state regardless of what the
    preceding test left behind.
    """
    import time
    pytest.importorskip("pyvisa")
    resource = os.environ.get("BENCHCTRL_DP2031_RESOURCE")  # None → auto
    try:
        psu = RigolDP2031.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DP2031 unavailable: {e}")
    # Aggressively clear state before yielding to the test
    psu.reset()
    psu.clear_status()
    time.sleep(0.5)
    # Disable all outputs first (in case a prior test left them on)
    for ch in (1, 2, 3):
        try:
            psu.set_output(ch, False)
        except Exception:
            pass
    # Clear PAIR — survives *RST. Long settle.
    try:
        psu.set_channel_pair("OFF")
        time.sleep(2.0)
    except Exception:
        pass
    # Clear tracking + sync
    for setter in (lambda: psu.set_tracking(False),
                   lambda: psu.set_output_sync(False)):
        try:
            setter()
        except Exception:
            pass
    # Clear latched OVP / OCP per channel — also survives *RST sometimes
    for ch in (1, 2, 3):
        try:
            psu.set_ovp_enabled(ch, False)
            psu.set_ocp_enabled(ch, False)
            psu.clear_ovp(ch)
            psu.clear_ocp(ch)
        except Exception:
            pass
    # Drain the error queue
    for _ in range(10):
        try:
            if psu.last_error() is None:
                break
        except Exception:
            break
    time.sleep(0.2)
    yield psu
    # Teardown: best-effort outputs off + restore safe defaults
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


# ---------------------------------------------------------------------------
# Phase B — hardware tests: status registers, OVP trip + clear,
# system commands that don't change persistent state.
# ---------------------------------------------------------------------------


@pytest.mark.hardware
def test_hw_status_registers_readable(hw_dp2031):
    """After *RST + *CLS all status registers should be 0."""
    # *ESR is read-clearing; should be 0 right after reset
    assert hw_dp2031.event_status_register() == 0
    assert hw_dp2031.status_byte() & 0x80 == 0  # no MSS bit set
    assert hw_dp2031.operation_event() == 0
    assert hw_dp2031.questionable_event() == 0


@pytest.mark.hardware
def test_hw_self_test_passes(hw_dp2031):
    assert hw_dp2031.self_test() == 0


@pytest.mark.hardware
def test_hw_installed_options(hw_dp2031):
    """*OPT? returns a list. We don't have any installed options on this
    unit so it should be empty; the test just asserts the parsing path."""
    opts = hw_dp2031.installed_options()
    assert isinstance(opts, list)


@pytest.mark.hardware
def test_hw_scpi_version(hw_dp2031):
    v = hw_dp2031.scpi_version()
    # Version is "1999.0" per SCPI standard
    assert v
    assert "." in v


@pytest.mark.hardware
def test_hw_opc_returns_one(hw_dp2031):
    # *OPC? must return +1 once pending operations finish
    assert hw_dp2031.wait_op_complete() == 1


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_ocp_delay_round_trip(hw_dp2031, ch):
    for ms in (50, 100, 500):
        hw_dp2031.set_ocp_delay_ms(ch, ms)
        assert hw_dp2031.get_ocp_delay_ms(ch) == ms


@pytest.mark.hardware
def test_hw_health_check_clean_state(hw_dp2031):
    """After *RST with no output on, health check should report all
    bits clear."""
    h = hw_dp2031.health_check()
    assert h["otp_global"] is False
    assert h["fan_failure"] is False
    for ch in (1, 2, 3):
        d = h[f"ch{ch}"]
        # Output off → may report Vunreg / Iunreg (channel isn't
        # regulating). What we care about for "healthy" is OVP / OCP
        # / OTP bits.
        assert d["ovp"] is False, f"CH{ch} unexpected OVP bit"
        assert d["ocp"] is False, f"CH{ch} unexpected OCP bit"
        assert d["otp"] is False, f"CH{ch} unexpected OTP bit"
    assert h["error_queue"] is None


@pytest.mark.hardware
def test_hw_ovp_trip_and_clear(hw_dp2031):
    """End-to-end OVP trip + clear on whichever channel the user has
    leads on (default CH3 — see the bench-ports memory).

    Set V above the OVP threshold; enable output; expect OVP latches
    within ~300 ms and output trips off. Clearing OVP unlatches the
    alarm but does NOT re-enable the output (manual).
    """
    import time
    ch = int(os.environ.get("BENCHCTRL_DP2031_OVP_TEST_CH", "3"))
    # Pick a safe setpoint + below-setpoint OVP for the chosen channel
    if ch == 3:
        v_setpoint, v_ovp = 3.000, 2.000
    else:
        v_setpoint, v_ovp = 5.000, 3.000
    # Defensive: clear any latched OVP from prior tests + start from
    # output OFF + OVP disabled. *RST in the fixture doesn't always
    # clear the OVP latch on this firmware (bench-observed).
    hw_dp2031.set_output(ch, False)
    hw_dp2031.set_ovp_enabled(ch, False)
    hw_dp2031.clear_ovp(ch)
    time.sleep(0.3)
    hw_dp2031.set_voltage(ch, v_setpoint)
    hw_dp2031.set_current(ch, 0.500)
    hw_dp2031.set_ovp_level(ch, v_ovp)
    hw_dp2031.set_ovp_enabled(ch, True)
    try:
        hw_dp2031.set_output(ch, True)
        # Latch takes ~150–250 ms to settle on this firmware; some
        # bench conditions (warm USB-TMC link from prior tests) need
        # longer. Retry-poll up to ~1.5 s.
        tripped = False
        for _ in range(15):
            time.sleep(0.1)
            if hw_dp2031.ovp_tripped(ch):
                tripped = True
                break
        assert tripped, \
            f"OVP did not latch within 1.5 s on CH{ch}"
        # Output may take an extra moment to flip OFF after the latch
        for _ in range(10):
            if hw_dp2031.get_output(ch) is False:
                break
            time.sleep(0.05)
        assert hw_dp2031.get_output(ch) is False, \
            "output should have tripped off on OVP"
        # Now clear and verify the latch releases. Output stays off
        # per the :OUTPut:OVP:CLEar form's documented semantics.
        hw_dp2031.clear_ovp(ch)
        time.sleep(0.2)
        assert hw_dp2031.ovp_tripped(ch) is False, \
            "OVP latch did not clear"
        assert hw_dp2031.get_output(ch) is False, \
            "clear_ovp should not re-enable the output"
    finally:
        hw_dp2031.set_ovp_enabled(ch, False)
        hw_dp2031.set_output(ch, False)


@pytest.mark.hardware
def test_hw_beep_once_does_not_error(hw_dp2031):
    """Sanity check the BEEPer:IMMediate path — emits a single audible
    beep on the device. If the operator hears nothing, the device's
    beeper may be muted; that's not a failure of this test."""
    hw_dp2031.beep_once()
    assert hw_dp2031.last_error() is None


@pytest.mark.hardware
def test_hw_brightness_round_trip(hw_dp2031):
    """Round-trip a non-default brightness then restore."""
    original = hw_dp2031.get_brightness()
    try:
        hw_dp2031.set_brightness(80)
        assert hw_dp2031.get_brightness() == 80
        hw_dp2031.set_brightness(30)
        assert hw_dp2031.get_brightness() == 30
    finally:
        hw_dp2031.set_brightness(original)


@pytest.mark.hardware
def test_hw_remote_local_round_trip(hw_dp2031):
    """REMote / LOCal commands take effect with no error."""
    hw_dp2031.set_remote()
    assert hw_dp2031.last_error() is None
    hw_dp2031.set_local()
    assert hw_dp2031.last_error() is None


# ---------------------------------------------------------------------------
# Phase C — hardware tests
# Run with the DP2031 outputs UNCONNECTED to anything (SERies / PARallel
# pair modes internally tie CH1 and CH2 together; remote-sense without
# sense leads connected could drive the channel to max).
# ---------------------------------------------------------------------------


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_voltage_step_round_trip(hw_dp2031, ch):
    """Set step, then UP / DOWN, verify the device updates by the step."""
    import time
    step = 0.5 if ch != 3 else 0.1
    hw_dp2031.set_voltage(ch, 1.000)
    hw_dp2031.set_voltage_step(ch, step)
    assert hw_dp2031.get_voltage_step(ch) == pytest.approx(step, abs=0.01)
    v0 = hw_dp2031.get_voltage(ch)
    hw_dp2031.step_voltage_up(ch)
    time.sleep(0.1)
    assert hw_dp2031.get_voltage(ch) == pytest.approx(v0 + step, abs=0.01)
    hw_dp2031.step_voltage_down(ch)
    time.sleep(0.1)
    assert hw_dp2031.get_voltage(ch) == pytest.approx(v0, abs=0.01)


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_current_step_round_trip(hw_dp2031, ch):
    import time
    step = 0.05 if ch != 3 else 0.1
    hw_dp2031.set_current(ch, 0.500)
    hw_dp2031.set_current_step(ch, step)
    assert hw_dp2031.get_current_step(ch) == pytest.approx(step, abs=0.005)
    i0 = hw_dp2031.get_current(ch)
    hw_dp2031.step_current_up(ch)
    time.sleep(0.1)
    assert hw_dp2031.get_current(ch) == pytest.approx(i0 + step, abs=0.005)
    hw_dp2031.step_current_down(ch)
    time.sleep(0.1)
    assert hw_dp2031.get_current(ch) == pytest.approx(i0, abs=0.005)


@pytest.mark.hardware
@pytest.mark.parametrize("ch,v,i", [(1, 5.0, 1.5), (2, 12.0, 0.8), (3, 3.3, 2.0)])
def test_hw_apply_round_trip(hw_dp2031, ch, v, i):
    """APPLy sets both V and I in one round-trip; verify both took effect
    and query_applied returns them."""
    import time
    hw_dp2031.apply(ch, voltage=v, current=i)
    time.sleep(0.1)
    assert hw_dp2031.get_voltage(ch) == pytest.approx(v, abs=0.02)
    assert hw_dp2031.get_current(ch) == pytest.approx(i, abs=0.005)
    rated, v_read, i_read = hw_dp2031.query_applied(ch)
    assert rated.upper().startswith(f"CH{ch}")
    assert v_read == pytest.approx(v, abs=0.02)
    assert i_read == pytest.approx(i, abs=0.005)


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_voltage_bounds_match_nominal_or_above(hw_dp2031, ch):
    """The device's reported MAX should be at least the nominal envelope
    (bench-observed: 5 % headroom)."""
    nominal_max = {1: 32.0, 2: 32.0, 3: 6.0}[ch]
    lo, hi, dflt = hw_dp2031.voltage_bounds(ch)
    assert lo == pytest.approx(0.0, abs=0.01)
    assert hi >= nominal_max
    # And not absurdly above nominal — 10 % headroom is enough
    assert hi <= nominal_max * 1.10


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_current_bounds_match_nominal_or_above(hw_dp2031, ch):
    nominal_max = {1: 3.0, 2: 3.0, 3: 5.0}[ch]
    lo, hi, dflt = hw_dp2031.current_bounds(ch)
    assert lo == pytest.approx(0.0, abs=0.001)
    assert hi >= nominal_max
    assert hi <= nominal_max * 1.10


@pytest.mark.hardware
def test_hw_tracking_state_round_trip(hw_dp2031):
    """Tracking state writes round-trip and TMODe reflects it.

    Bench-discovered on this firmware: enabling :OUTPut:TRACk sets
    the state register (TRACk? = 1, TMODe? = SYNCHRONOUS) but does
    NOT mirror CH1→CH2 setpoint changes with both outputs off. The
    actual analog mirroring effect needs further investigation —
    likely requires outputs enabled + connected loads. For now we
    verify the state round-trip, which is what the API is responsible
    for.
    """
    import time
    hw_dp2031.set_tracking(True)
    time.sleep(0.2)
    assert hw_dp2031.get_tracking() is True
    # When tracking is on, TMODe should report SYNCHRONOUS
    assert hw_dp2031.get_track_mode() == "SYNCHRONOUS"
    hw_dp2031.set_tracking(False)
    time.sleep(0.2)
    assert hw_dp2031.get_tracking() is False
    assert hw_dp2031.get_track_mode() == "INDEPENDENT"


@pytest.mark.hardware
def test_hw_track_mode_alias_works(hw_dp2031):
    """SYSTem:TMODe SYNC ≡ OUTPut:TRACk ON per the manual; verify."""
    import time
    hw_dp2031.set_track_mode("SYNC")
    time.sleep(0.2)
    assert hw_dp2031.get_tracking() is True
    hw_dp2031.set_track_mode("INDE")
    time.sleep(0.2)
    assert hw_dp2031.get_tracking() is False


@pytest.mark.hardware
def test_hw_output_sync_round_trip(hw_dp2031):
    """SYSTem:SYNC ON enables simultaneous CH1+CH2 enable/disable."""
    import time
    hw_dp2031.set_output_sync(True)
    time.sleep(0.2)
    assert hw_dp2031.get_output_sync() is True
    hw_dp2031.set_output_sync(False)
    time.sleep(0.2)
    assert hw_dp2031.get_output_sync() is False


@pytest.mark.hardware
@pytest.mark.parametrize("ch", [1, 2, 3])
def test_hw_remote_sense_round_trip(hw_dp2031, ch):
    """Round-trip 4-wire sense state per channel. Don't enable in a
    test that runs without sense leads connected for long — leaving
    sense ON with leads floating can drive the output to OVP."""
    import time
    hw_dp2031.set_remote_sense(ch, True)
    time.sleep(0.2)
    assert hw_dp2031.get_remote_sense(ch) is True
    hw_dp2031.set_remote_sense(ch, False)
    time.sleep(0.2)
    assert hw_dp2031.get_remote_sense(ch) is False


def _all_remote_sense_off(psu):
    """Defensive teardown helper — ensures every channel ends with
    remote sense disabled, since leaving it on with floating sense
    leads is a hazard."""
    for ch in (1, 2, 3):
        try:
            psu.set_remote_sense(ch, False)
        except Exception:
            pass


@pytest.mark.hardware
def test_hw_remote_sense_all_form(hw_dp2031):
    """The 'ALL' selector writes all three channels in one command."""
    import time
    try:
        hw_dp2031.set_remote_sense("ALL", True)
        time.sleep(0.3)
        for ch in (1, 2, 3):
            assert hw_dp2031.get_remote_sense(ch) is True, \
                f"CH{ch} did not turn on via ALL"
    finally:
        _all_remote_sense_off(hw_dp2031)


@pytest.mark.hardware
@pytest.mark.parametrize("mode", ["AUTO", "HIGH", "LOW"])
def test_hw_sampling_mode_round_trip(hw_dp2031, mode):
    import time
    hw_dp2031.set_sampling_mode(mode)
    time.sleep(0.2)
    assert hw_dp2031.get_sampling_mode() == mode


@pytest.mark.hardware
def test_hw_channel_pair_series_engages(hw_dp2031):
    """SERies pair mode is bench-verified to work on this firmware
    (PARallel may silently no-op — see KNOWN_LIMITATIONS)."""
    import time
    # Make sure no external load is wired between CH1 and CH2 before
    # running this test — see the test_bench_rigol_dp2031.py header.
    try:
        hw_dp2031.set_channel_pair("SERies")
        time.sleep(1.5)  # mode transition is slow (~1 s) on this firmware
        assert hw_dp2031.get_channel_pair() == "SERIES"
    finally:
        hw_dp2031.set_channel_pair("OFF")
        time.sleep(1.0)
        assert hw_dp2031.get_channel_pair() == "OFF"


@pytest.mark.hardware
def test_hw_channel_pair_off_default(hw_dp2031):
    """Reset should leave the pair mode at OFF."""
    assert hw_dp2031.get_channel_pair() == "OFF"


# ---------------------------------------------------------------------------
# Phase D — hardware tests
# Mostly state-only round-trip — Timer execution + Analyzer logging need
# loads / external storage and are out of scope for the CI suite.
# ---------------------------------------------------------------------------


@pytest.mark.hardware
def test_hw_timer_state_round_trip(hw_dp2031):
    """STATe / CHANnel / CYCLEs / ENDState / RUN / TRIG round-trip."""
    import time
    hw_dp2031.set_timer_enabled(False)
    assert hw_dp2031.get_timer_enabled() is False

    hw_dp2031.set_timer_channel(3)
    assert hw_dp2031.get_timer_channel() == DP2031Channel.CH3

    hw_dp2031.set_timer_cycles(7)
    assert hw_dp2031.get_timer_cycles() == 7
    hw_dp2031.set_timer_cycles(None)
    assert hw_dp2031.get_timer_cycles() is None
    hw_dp2031.set_timer_cycles(1)

    hw_dp2031.set_timer_end_state("OFF")
    assert hw_dp2031.get_timer_end_state() == "OFF"
    hw_dp2031.set_timer_end_state("LAST")
    assert hw_dp2031.get_timer_end_state() == "LAST"

    hw_dp2031.set_timer_run_mode("SINGle")
    assert hw_dp2031.get_timer_run_mode() == "SINGLE"
    hw_dp2031.set_timer_run_mode("CONTinue")
    assert hw_dp2031.get_timer_run_mode() == "CONTINUE"

    hw_dp2031.set_timer_trigger("BUS")
    assert hw_dp2031.get_timer_trigger() == "BUS"
    hw_dp2031.set_timer_trigger("MANual")
    assert hw_dp2031.get_timer_trigger() == "MANUAL"


@pytest.mark.hardware
def test_hw_program_timer_round_trip(hw_dp2031):
    """program_timer writes a multi-step sequence; read it back via
    get_timer_group_params and verify the IEEE 488.2 block parser."""
    steps = [
        (3.3, 0.5, 1.0),
        (1.0, 0.1, 0.5),
        (0.0, 0.0, 0.3),
    ]
    hw_dp2031.program_timer(3, steps, cycles=2, end_state="OFF",
                             trigger="MANual")
    # Position the editor at group 1 and read back
    hw_dp2031.set_timer_group_index(1)
    rows = hw_dp2031.get_timer_group_params(count=len(steps))
    assert len(rows) == len(steps)
    for (i, expected), got in zip(enumerate(steps, 1), rows):
        v_exp, i_exp, t_exp = expected
        idx, v, i_amp, t = got
        assert idx == i
        assert v == pytest.approx(v_exp, abs=0.01)
        assert i_amp == pytest.approx(i_exp, abs=0.01)
        assert t == pytest.approx(t_exp, abs=0.01)
    assert hw_dp2031.get_timer_cycles() == 2
    assert hw_dp2031.get_timer_end_state() == "OFF"


@pytest.mark.hardware
def test_hw_timer_template_round_trip(hw_dp2031):
    """Template selection round-trip — doesn't execute, just configures.

    Bench-observed: template writes need ~200 ms to settle before the
    next write is reliably accepted.
    """
    import time
    for t in ("SINE", "PULSE", "RAMP"):
        hw_dp2031.set_timer_template(t)
        time.sleep(0.3)
        assert hw_dp2031.get_timer_template() == t, \
            f"expected {t}, got {hw_dp2031.get_timer_template()!r}"


@pytest.mark.hardware
def test_hw_trigger_io_round_trip(hw_dp2031):
    """Trigger I/O state round-trip across D1-D4."""
    import time
    for line in ("D1", "D2", "D3", "D4"):
        hw_dp2031.set_trigger_in_enabled(line, True)
        assert hw_dp2031.get_trigger_in_enabled(line) is True
        hw_dp2031.set_trigger_in_enabled(line, False)
        assert hw_dp2031.get_trigger_in_enabled(line) is False

        for t in ("RISE", "FALL", "HIGH", "LOW"):
            hw_dp2031.set_trigger_in_type(line, t)
            assert hw_dp2031.get_trigger_in_type(line) == t

        hw_dp2031.set_trigger_in_source(line, [1, 2])
        assert hw_dp2031.get_trigger_in_source(line) == ["CH1", "CH2"]
        hw_dp2031.set_trigger_in_source(line, [3])
        assert hw_dp2031.get_trigger_in_source(line) == ["CH3"]


@pytest.mark.hardware
def test_hw_trigger_in_immediate_no_error(hw_dp2031):
    """Force one immediate trigger event; verify no SCPI error queues."""
    hw_dp2031.trigger_in_immediate()
    assert hw_dp2031.last_error() is None


@pytest.mark.hardware
def test_hw_memory_state_round_trip(hw_dp2031):
    """List directory, change to root, list disks."""
    # Just exercise the queries — don't write files (avoid wear /
    # leftovers on the internal C disk).
    cwd = hw_dp2031.current_directory()
    assert "/" in cwd or ":" in cwd, f"current_directory unexpected: {cwd!r}"
    files = hw_dp2031.list_files()
    assert isinstance(files, list)
    disks = hw_dp2031.external_disks()
    assert isinstance(disks, list)


@pytest.mark.hardware
def test_hw_save_recall_state_commands_succeed(hw_dp2031):
    """``*SAV`` and ``*RCL`` accept the slot index without error.

    Bench-observed: actual state restoration after ``*RCL`` is
    sensitive to what's interleaved with the recall (other tests'
    teardown can corrupt the slot if they touch it). We verify
    here only that the commands accept the slot and don't queue
    a SCPI error — full save-mutate-recall-verify would need an
    isolated slot the test exclusively owns, which is fragile
    across the suite.
    """
    import time
    hw_dp2031.save_state(8)
    time.sleep(0.5)
    assert hw_dp2031.last_error() is None, \
        "*SAV 8 should not queue a SCPI error"
    hw_dp2031.recall_state(8)
    time.sleep(0.5)
    assert hw_dp2031.last_error() is None, \
        "*RCL 8 should not queue a SCPI error"


@pytest.mark.hardware
def test_hw_screenshot_returns_bmp(hw_dp2031):
    """Capture a screenshot, verify it starts with the BMP magic 'BM'."""
    data = hw_dp2031.screenshot_bytes()
    assert len(data) > 1000, f"screenshot suspiciously small: {len(data)} bytes"
    assert data[:2] == b"BM", f"expected BMP magic, got {data[:8]!r}"

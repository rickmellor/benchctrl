"""Tests for the Rigol DL3031A electronic-load driver.

Hardware-free tests use a fake instrument that mimics the pyvisa
Resource interface (just ``write`` / ``query``). Hardware-marked
tests need the real DL3031A reachable via USB-TMC.
"""
from __future__ import annotations

import os
from collections import deque

import pytest

from opensmu.bench.rigol_dl3031a import (
    RigolDL3031A,
    RigolDLCommandError,
    RigolDLError,
    RigolDLValueError,
)


# ---------------------------------------------------------------------------
# Fake pyvisa-style instrument for hardware-free tests
# ---------------------------------------------------------------------------


class FakeInstrument:
    """Records every ``write`` and replies to ``query`` from a script."""

    def __init__(self, responses: dict[str, str] | None = None):
        self.writes: list[str] = []
        self.responses: dict[str, str] = responses or {}
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


def _make(responses: dict | None = None) -> tuple[RigolDL3031A, FakeInstrument]:
    inst = FakeInstrument(responses)
    drv = RigolDL3031A(inst, resource_string="FAKE::INSTR")
    return drv, inst


# ---------------------------------------------------------------------------
# Info / housekeeping
# ---------------------------------------------------------------------------


def test_info_parses_idn():
    drv, inst = _make({
        "*IDN?": "RIGOL TECHNOLOGIES,DL3031A,DL3D232300106,00.01.05.00.01",
    })
    info = drv.info()
    assert info.manufacturer == "RIGOL TECHNOLOGIES"
    assert info.model == "DL3031A"
    assert info.serial == "DL3D232300106"
    assert info.firmware == "00.01.05.00.01"
    assert info.resource == "FAKE::INSTR"


def test_info_cached():
    drv, inst = _make({"*IDN?": "A,B,C,D,extra"})
    drv.info()
    drv.info()
    assert inst.writes.count("*IDN?") == 1


def test_info_malformed_raises():
    drv, _ = _make({"*IDN?": "garbage"})
    with pytest.raises(RigolDLError):
        drv.info()


def test_reset_and_cls():
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


def test_raise_if_error_raises_on_non_zero():
    drv, _ = _make({":SYSTem:ERRor?": '-220,"Parameter error"'})
    with pytest.raises(RigolDLCommandError) as exc_info:
        drv.raise_if_error()
    assert exc_info.value.code == -220
    assert "Parameter error" in str(exc_info.value)


def test_raise_if_error_quiet_when_clean():
    drv, _ = _make({":SYSTem:ERRor?": '0,"No error"'})
    drv.raise_if_error()  # should not raise


# ---------------------------------------------------------------------------
# Mode / input
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "input_str,expected_scpi",
    [
        ("CC", "CURRent"),
        ("cc", "CURRent"),
        ("CV", "VOLTage"),
        ("CR", "RESistance"),
        ("CP", "POWer"),
        ("CURRent", "CURRent"),
        ("VOLTAGE", "VOLTage"),
    ],
)
def test_set_mode_maps_to_scpi(input_str, expected_scpi):
    drv, inst = _make()
    drv.set_mode(input_str)
    assert inst.writes[-1] == f":SOURce:FUNCtion {expected_scpi}"


def test_set_mode_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_mode("CX")


@pytest.mark.parametrize(
    "device_reply,expected",
    [("CC", "CC"), ("cr", "CR"), ("CV ", "CV")],
)
def test_get_mode(device_reply, expected):
    drv, _ = _make({":SOURce:FUNCtion?": device_reply})
    assert drv.get_mode() == expected


def test_get_mode_rejects_garbage():
    drv, _ = _make({":SOURce:FUNCtion?": "FOO"})
    with pytest.raises(RigolDLError):
        drv.get_mode()


def test_set_input_on_off():
    drv, inst = _make()
    drv.set_input(True)
    drv.set_input(False)
    assert inst.writes == [
        ":SOURce:INPut:STATe 1",
        ":SOURce:INPut:STATe 0",
    ]


def test_get_input():
    drv, _ = _make({":SOURce:INPut:STATe?": "1"})
    assert drv.get_input() is True
    drv2, _ = _make({":SOURce:INPut:STATe?": "0"})
    assert drv2.get_input() is False


# ---------------------------------------------------------------------------
# Setpoints
# ---------------------------------------------------------------------------


def test_set_current_writes_formatted_command():
    drv, inst = _make()
    drv.set_current(0.030)
    assert inst.writes[-1].startswith(":SOURce:CURRent:LEVel:IMMediate ")
    val = float(inst.writes[-1].split()[-1])
    assert val == pytest.approx(0.030)


def test_set_current_rejects_negative():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_current(-0.001)


def test_get_current():
    drv, _ = _make({
        ":SOURce:CURRent:LEVel:IMMediate?": "0.030000"
    })
    assert drv.get_current() == pytest.approx(0.030)


def test_set_resistance_rejects_zero():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_resistance(0.0)


def test_set_resistance_writes_command():
    drv, inst = _make()
    drv.set_resistance(100.0)
    assert inst.writes[-1].startswith(":SOURce:RESistance:LEVel:IMMediate ")
    assert float(inst.writes[-1].split()[-1]) == pytest.approx(100.0)


def test_set_voltage_rejects_negative():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_voltage(-1.0)


def test_set_power_rejects_negative():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_power(-1.0)


# ---------------------------------------------------------------------------
# Ranges and slew
# ---------------------------------------------------------------------------


def test_set_current_range_writes_command():
    drv, inst = _make()
    drv.set_current_range(60.0)
    assert inst.writes[-1].startswith(":SOURce:CURRent:RANGe ")
    assert float(inst.writes[-1].split()[-1]) == pytest.approx(60.0)


def test_set_slew_rejects_zero():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_slew(0.0)


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def test_measure_voltage():
    drv, _ = _make({":MEASure:VOLTage:DC?": "3.234500"})
    assert drv.measure_voltage() == pytest.approx(3.2345)


def test_measure_current():
    drv, _ = _make({":MEASure:CURRent:DC?": "0.029384"})
    assert drv.measure_current() == pytest.approx(0.029384)


def test_measure_all_returns_dict():
    drv, _ = _make({
        ":MEASure:VOLTage:DC?": "3.2",
        ":MEASure:CURRent:DC?": "0.03",
        ":MEASure:POWer:DC?": "0.096",
        ":MEASure:RESistance:DC?": "106.6667",
    })
    snap = drv.measure_all()
    assert snap == {
        "voltage_V": pytest.approx(3.2),
        "current_A": pytest.approx(0.03),
        "power_W": pytest.approx(0.096),
        "resistance_ohm": pytest.approx(106.6667),
    }


# ---------------------------------------------------------------------------
# Context manager safety
# ---------------------------------------------------------------------------


def test_context_manager_disables_input_on_exit():
    drv, inst = _make()
    with drv:
        pass
    assert ":SOURce:INPut:STATe 0" in inst.writes
    assert inst.closed


def test_close_is_idempotent():
    drv, inst = _make()
    drv.close()
    drv.close()
    assert inst.closed


# ---------------------------------------------------------------------------
# :FETCh: (non-blocking reads)
# ---------------------------------------------------------------------------


def test_fetch_voltage_uses_fetch_not_measure():
    drv, inst = _make({":FETCh:VOLTage:DC?": "3.2"})
    assert drv.fetch_voltage() == pytest.approx(3.2)
    assert ":FETCh:VOLTage:DC?" in inst.writes
    assert ":MEASure:VOLTage:DC?" not in inst.writes


def test_fetch_all_returns_dict():
    drv, _ = _make({
        ":FETCh:VOLTage:DC?": "3.2",
        ":FETCh:CURRent:DC?": "0.03",
        ":FETCh:POWer:DC?": "0.096",
        ":FETCh:RESistance:DC?": "106.6667",
    })
    snap = drv.fetch_all()
    assert snap["voltage_V"] == pytest.approx(3.2)
    assert snap["current_A"] == pytest.approx(0.03)


# ---------------------------------------------------------------------------
# Function mode (top-level regulation source)
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("m", ["FIXed", "LIST", "WAVe", "BATTery", "OCP", "OPP"])
def test_set_function_mode_accepts_canonical(m):
    drv, inst = _make()
    drv.set_function_mode(m)
    assert inst.writes[-1] == f":SOURce:FUNCtion:MODE {m}"


def test_set_function_mode_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.set_function_mode("BOGUS")


# ---------------------------------------------------------------------------
# LIST mode
# ---------------------------------------------------------------------------


def test_list_set_mode_uses_two_letter_form():
    drv, inst = _make()
    drv.list_set_mode("CC")
    assert inst.writes[-1] == ":SOURce:LIST:MODE CC"
    drv.list_set_mode("CR")
    assert inst.writes[-1] == ":SOURce:LIST:MODE CR"


def test_list_set_count_validates_range():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.list_set_count(-1)
    with pytest.raises(RigolDLValueError):
        drv.list_set_count(100_000)


def test_list_set_step_count_validates_range():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.list_set_step_count(1)   # < min of 2
    with pytest.raises(RigolDLValueError):
        drv.list_set_step_count(513)  # > max of 512


def test_list_set_step_count_writes_total_directly():
    """Bench-verified semantics (v0.9.7): :SOUR:LIST:STEP N plays N
    total steps. The manual's application-instance suggesting STEP=2
    plays 3 steps was incorrect; with 5 distinct programmed levels,
    STEP=5 plays all five and STEP=2 plays only the first two."""
    drv, inst = _make()
    drv.list_set_step_count(5)
    assert inst.writes[-1] == ":SOURce:LIST:STEP 5"
    drv.list_set_step_count(3)
    assert inst.writes[-1] == ":SOURce:LIST:STEP 3"


def test_list_set_step_count_rejects_four_firmware_bug():
    """STEP=4 specifically fails on firmware 00.01.05.00.01 —
    no steps fire. Driver rejects with a clear error pointing to the
    workaround (3 or 5 steps + appropriate count)."""
    drv, _ = _make()
    with pytest.raises(RigolDLValueError, match="firmware bug"):
        drv.list_set_step_count(4)


def test_list_set_step_writes_level_then_width():
    drv, inst = _make()
    drv.list_set_step(3, 0.030, 0.050)
    # Last two writes are level then width for step 3
    assert inst.writes[-2].startswith(":SOURce:LIST:LEVel 3,")
    assert inst.writes[-1].startswith(":SOURce:LIST:WIDth 3,")
    level_arg = inst.writes[-2].split(",", 1)[1]
    width_arg = inst.writes[-1].split(",", 1)[1]
    assert float(level_arg) == pytest.approx(0.030)
    assert float(width_arg) == pytest.approx(0.050)


def test_list_set_step_with_slew_writes_third_command():
    drv, inst = _make()
    drv.list_set_step(2, 0.030, 0.050, slew_A_per_us=0.5)
    assert any(w.startswith(":SOURce:LIST:SLEW 2,") for w in inst.writes)


def test_list_set_step_validates_width_lower_bound():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.list_set_step(0, 0.001, 0.00001)  # 10 µs < 50 µs minimum


def test_list_set_step_validates_width_upper_bound():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.list_set_step(0, 0.001, 4000.0)


def test_list_set_end_validates():
    drv, _ = _make()
    drv.list_set_end("OFF")
    drv.list_set_end("LAST")
    with pytest.raises(RigolDLValueError):
        drv.list_set_end("BOGUS")


def test_program_list_pushes_full_sequence():
    drv, inst = _make()
    drv.program_list(
        steps=[(0.00001, 1.0), (0.030, 0.05), (0.00001, 1.0)],
        mode="CC", count=3, range_value=6.0,
        slew_A_per_us=0.5, end_behavior="OFF",
        trigger_source="BUS",
    )
    # Exact wire-format assertions — startswith() would let a typo
    # like :RANGE (instead of :RANGe) silently pass
    assert ":SOURce:LIST:MODE CC" in inst.writes
    assert ":SOURce:LIST:RANGe 6.000000" in inst.writes
    assert ":SOURce:LIST:COUNt 3" in inst.writes
    assert ":SOURce:LIST:STEP 3" in inst.writes        # total step count
    assert ":SOURce:LIST:LEVel 0,0.000010" in inst.writes
    assert ":SOURce:LIST:WIDth 0,1.000000" in inst.writes
    assert ":SOURce:LIST:SLEW 0,0.500000" in inst.writes
    assert ":SOURce:LIST:LEVel 1,0.030000" in inst.writes
    assert ":SOURce:LIST:WIDth 1,0.050000" in inst.writes
    assert ":SOURce:LIST:SLEW 1,0.500000" in inst.writes
    assert ":SOURce:LIST:LEVel 2,0.000010" in inst.writes
    assert ":SOURce:LIST:WIDth 2,1.000000" in inst.writes
    assert ":SOURce:LIST:SLEW 2,0.500000" in inst.writes
    assert ":SOURce:LIST:END OFF" in inst.writes
    assert ":TRIGger:SOURce BUS" in inst.writes
    # FUNC:MODE LIST must be the LAST write so the device latches in
    # after everything is programmed
    assert inst.writes[-1] == ":SOURce:FUNCtion:MODE LIST"


def test_program_list_rejects_too_few_steps():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.program_list(steps=[(0.001, 0.001)], mode="CC")


def test_program_list_rejects_four_step_firmware_bug():
    """program_list refuses 4-step programs (STEP=4 firmware bug)."""
    drv, _ = _make()
    with pytest.raises(RigolDLValueError, match="firmware bug"):
        drv.program_list(
            steps=[(0.0001, 0.5), (0.030, 0.05), (0.0001, 0.3), (0.030, 0.05)],
            mode="CC",
        )


def test_trigger_now_writes_trigger():
    drv, inst = _make()
    drv.trigger_now()
    assert inst.writes[-1] == ":TRIGger"


@pytest.mark.parametrize("src,expected", [
    ("BUS", "BUS"),
    ("bus", "BUS"),
    ("EXTernal", "EXTernal"),
    ("MANUal", "MANUal"),
])
def test_set_trigger_source(src, expected):
    drv, inst = _make()
    drv.set_trigger_source(src)
    assert inst.writes[-1] == f":TRIGger:SOURce {expected}"


# ---------------------------------------------------------------------------
# CC transient mode
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("m", ["CONTinuous", "PULSe", "TOGGle"])
def test_transient_mode_canonical_forms(m):
    drv, inst = _make()
    drv.transient_set_mode(m)
    assert inst.writes[-1] == f":SOURce:CURRent:TRANsient:MODE {m}"


def test_transient_mode_rejects_unknown():
    drv, _ = _make()
    with pytest.raises(RigolDLValueError):
        drv.transient_set_mode("FOO")


def test_configure_transient_pulse_full_sequence():
    drv, inst = _make()
    drv.configure_transient_pulse(
        a_level_A=0.030, b_level_A=0.0001,
        a_width_s=0.050, b_width_s=1.0,
        mode="TOGGle",
    )
    # Should have set CC mode + TOGG transient
    assert ":SOURce:FUNCtion CURRent" in inst.writes
    assert ":SOURce:CURRent:TRANsient:MODE TOGGle" in inst.writes
    assert any(w.startswith(":SOURce:CURRent:TRANsient:ALEVel") for w in inst.writes)
    assert any(w.startswith(":SOURce:CURRent:TRANsient:BLEVel") for w in inst.writes)


def test_transient_enable_writes_state():
    drv, inst = _make()
    drv.transient_enable(True)
    drv.transient_enable(False)
    assert ":SOURce:TRANsient:STATe 1" in inst.writes
    assert ":SOURce:TRANsient:STATe 0" in inst.writes


# ---------------------------------------------------------------------------
# Battery discharge mode
# ---------------------------------------------------------------------------


def test_battery_voltage_stop_writes_stop_then_enable():
    drv, inst = _make()
    drv.battery_set_voltage_stop(2.7, enabled=True)
    vstop = [w for w in inst.writes if w.startswith(":SOURce:BATTary:VSTop")]
    venab = [w for w in inst.writes if w.startswith(":SOURce:BATTary:VENabstop")]
    assert vstop and venab
    assert venab[-1].endswith(" 1")


def test_configure_battery_test_full_setup():
    drv, inst = _make()
    drv.configure_battery_test(
        current_A=0.050, v_stop_V=2.7,
        capacity_stop_mAh=200.0, time_stop_s=1800.0,
        range_A=6.0,
    )
    # Final write switches to BATTery function mode
    assert inst.writes[-1] == ":SOURce:FUNCtion:MODE BATTery"
    # All three stop conditions enabled
    assert ":SOURce:BATTary:VENabstop 1" in inst.writes
    assert ":SOURce:BATTary:CENabstop 1" in inst.writes
    assert ":SOURce:BATTary:TENabstop 1" in inst.writes


def test_battery_stats_parses_HMS_discharge_time():
    """Bench-verified v0.9.7: :FETCh:DISChargingTime? returns
    H:M:S, not a float. battery_stats parses it to seconds."""
    drv, _ = _make({
        ":FETCh:CAPability?": "12.345",
        ":FETCh:WATThours?": "0.0394",
        ":FETCh:DISChargingTime?": "0:0:15",   # 15 seconds
        ":FETCh:VOLTage:DC?": "3.05",
        ":FETCh:CURRent:DC?": "0.050",
    })
    stats = drv.battery_stats()
    assert stats["capacity_mAh"] == pytest.approx(12.345)
    assert stats["energy_Wh"] == pytest.approx(0.0394)
    assert stats["discharge_time_s"] == pytest.approx(15.0)
    assert stats["voltage_V"] == pytest.approx(3.05)
    assert stats["current_A"] == pytest.approx(0.050)


def test_battery_stats_handles_hour_minute_seconds():
    drv, _ = _make({
        ":FETCh:CAPability?": "100.0",
        ":FETCh:WATThours?": "0.3",
        ":FETCh:DISChargingTime?": "1:23:45",  # 1h 23m 45s = 5025s
        ":FETCh:VOLTage:DC?": "3.0",
        ":FETCh:CURRent:DC?": "0.050",
    })
    stats = drv.battery_stats()
    assert stats["discharge_time_s"] == pytest.approx(5025.0)


def test_battery_stats_subsecond_in_h_m_s_format():
    """Future firmware may include sub-second precision in the
    seconds field (e.g. '0:0:15.250'). Driver accepts float seconds
    while still parsing H and M as integers."""
    drv, _ = _make({
        ":FETCh:CAPability?": "10.0",
        ":FETCh:WATThours?": "0.03",
        ":FETCh:DISChargingTime?": "0:0:15.250",
        ":FETCh:VOLTage:DC?": "3.05",
        ":FETCh:CURRent:DC?": "0.050",
    })
    stats = drv.battery_stats()
    assert stats["discharge_time_s"] == pytest.approx(15.25)


def test_battery_stats_fallback_to_float_if_no_colon():
    """Belt-and-braces: if a future firmware revision returns a
    plain float instead of H:M:S, accept it."""
    drv, _ = _make({
        ":FETCh:CAPability?": "12.0",
        ":FETCh:WATThours?": "0.04",
        ":FETCh:DISChargingTime?": "247.5",
        ":FETCh:VOLTage:DC?": "3.0",
        ":FETCh:CURRent:DC?": "0.050",
    })
    stats = drv.battery_stats()
    assert stats["discharge_time_s"] == pytest.approx(247.5)


def test_battery_stats_malformed_time_raises():
    drv, _ = _make({
        ":FETCh:CAPability?": "0",
        ":FETCh:WATThours?": "0",
        ":FETCh:DISChargingTime?": "garbage",
        ":FETCh:VOLTage:DC?": "0",
        ":FETCh:CURRent:DC?": "0",
    })
    with pytest.raises(RigolDLError, match="DISChargingTime"):
        drv.battery_stats()


# ---------------------------------------------------------------------------
# Hardware-required tests
# ---------------------------------------------------------------------------


pytestmark_hw = pytest.mark.hardware


@pytest.mark.hardware
def test_hardware_idn_and_basic_setpoints():
    """Talk to the real DL3031A. Skips if no Rigol VISA resource found."""
    pytest.importorskip("pyvisa")
    resource = os.environ.get("OPENSMU_DL3031A_RESOURCE")  # None → auto
    try:
        load = RigolDL3031A.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DL3031A unavailable: {e}")

    try:
        info = load.info()
        assert "RIGOL" in info.manufacturer.upper()
        assert info.model.startswith("DL30")
        load.reset()
        load.clear_status()
        load.set_mode("CR")
        load.set_resistance(100.0)
        assert load.get_mode() == "CR"
        assert load.get_resistance() == pytest.approx(100.0, abs=0.01)
        load.set_mode("CC")
        load.set_current(0.005)
        assert load.get_mode() == "CC"
        # V/I read should succeed (value depends on whether an SMU is
        # also connected — don't assume open-circuit).
        v = load.measure_voltage()
        i = load.measure_current()
        assert isinstance(v, float)
        assert isinstance(i, float)
        assert load.last_error() is None
    finally:
        load.close()


@pytest.mark.hardware
def test_hardware_battery_discharge_test_smoke():
    """Smoke test: configure a tiny battery-discharge run with a 2 s
    time stop and verify battery_stats() returns sensible numbers.

    Requires a power supply on the load's input (SMU at 3.2 V or
    similar) to actually flow current; without that the load won't
    engage and the test just verifies the SCPI surface is intact.
    """
    pytest.importorskip("pyvisa")
    resource = os.environ.get("OPENSMU_DL3031A_RESOURCE")
    try:
        load = RigolDL3031A.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DL3031A unavailable: {e}")
    try:
        load.reset()
        load.clear_status()
        load.configure_battery_test(
            current_A=0.001, time_stop_s=1.0, range_A=6.0,
        )
        # Without input on, stats should return valid types (parses cleanly)
        stats = load.battery_stats()
        # All fields must be parseable as floats
        for key in ("capacity_mAh", "energy_Wh", "discharge_time_s",
                    "voltage_V", "current_A"):
            assert isinstance(stats[key], float), f"{key} not a float"
        # No SCPI errors
        assert load.last_error() is None
    finally:
        try:
            load.set_input(False)
        except Exception:
            pass
        load.close()


@pytest.mark.hardware
def test_hardware_transient_frequency_is_hz_not_khz():
    """Bench-verified v0.9.7: contra the manual, transient frequency
    is in Hz. period = 1/freq must hold."""
    pytest.importorskip("pyvisa")
    resource = os.environ.get("OPENSMU_DL3031A_RESOURCE")
    try:
        load = RigolDL3031A.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DL3031A unavailable: {e}")
    try:
        load.reset()
        load.set_mode("CC")
        load.set_current_range(6.0)
        load.transient_set_mode("CONTinuous")
        load.transient_set_a_level(0.001)
        load.transient_set_b_level(0.0001)
        load.transient_set_frequency(10.0)
        freq_read = float(load.query(":SOUR:CURR:TRAN:FREQ?"))
        period_read = float(load.query(":SOUR:CURR:TRAN:PER?"))
        assert freq_read == pytest.approx(10.0, abs=0.01)
        # Period must be 1/freq → 0.1 s
        assert period_read == pytest.approx(0.1, abs=0.001)
    finally:
        load.close()


@pytest.mark.hardware
def test_hardware_list_program_accepted():
    """Push a small LIST and verify the firmware accepts the program."""
    pytest.importorskip("pyvisa")
    resource = os.environ.get("OPENSMU_DL3031A_RESOURCE")
    try:
        load = RigolDL3031A.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DL3031A unavailable: {e}")
    try:
        load.reset()
        load.clear_status()
        # Three-step CC list: idle / TX burst / idle
        load.program_list(
            steps=[(0.0001, 0.5), (0.030, 0.050), (0.0001, 0.5)],
            mode="CC", count=1, range_value=6.0, end_behavior="OFF",
        )
        # Device should report LIST as the active function mode
        assert load.get_function_mode().startswith("LIST")
        # And no errors should have queued
        assert load.last_error() is None
    finally:
        # Return to FIXed and disable
        try:
            load.set_function_mode("FIXed")
            load.set_input(False)
        except Exception:
            pass
        load.close()

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
        # No DUT connected → V and I both essentially zero
        assert load.measure_voltage() < 0.5
        assert abs(load.measure_current()) < 0.001
        assert load.last_error() is None
    finally:
        load.close()

"""Tests for benchctrl.bench.qr10x.

Hardware-free tests exercise the parser helpers and validation.
Hardware-required tests (marked) drive a real QR10x on a known port —
default ``COM7``; override via ``BENCHCTRL_QR10X_PORT``.
"""

from __future__ import annotations

import os

import pytest

from benchctrl.drivers.eastwood_qr10x import (
    QR10x,
    QR10xInfo,
    QR10xValueError,
)


# ---------------------------------------------------------------------------
# Hardware-free — parser + validation logic
# ---------------------------------------------------------------------------


def test_parse_kv_extracts_value():
    lines = [
        "+USER.SP=10.0000",
        "+OK.",
    ]
    assert QR10x._parse_kv(lines, "USER.SP") == "10.0000"


def test_parse_kv_returns_none_when_absent():
    lines = ["+OK."]
    assert QR10x._parse_kv(lines, "DEV.FW") is None


def test_parse_kv_strips_whitespace():
    lines = ["+USER.SP=  42.5  "]
    assert QR10x._parse_kv(lines, "USER.SP") == "42.5"


def test_parse_state_dump_from_set_response():
    lines = [
        "+OK.",
        "SP(R)=100.0",
        "PV(R)=100.038",
        "UMax(V)=12.9",
        "RLimit(R)=12.0",
        "InnerT(C)=24.72",
    ]
    out = QR10x._parse_state_dump(lines)
    assert out["ok"] is True
    assert out["SP(R)"] == 100.0
    assert out["PV(R)"] == 100.038
    assert out["UMax(V)"] == 12.9
    assert out["RLimit(R)"] == 12.0
    assert out["InnerT(C)"] == 24.72


def test_parse_state_dump_marks_ok_false_when_missing():
    lines = ["SP(R)=10.0", "PV(R)=10.0"]
    out = QR10x._parse_state_dump(lines)
    assert out["ok"] is False
    assert out["SP(R)"] == 10.0


def test_parse_state_dump_preserves_non_numeric_values():
    lines = ["+OK.", "Mode=current", "SP(R)=42.0"]
    out = QR10x._parse_state_dump(lines)
    assert out["Mode"] == "current"
    assert out["SP(R)"] == 42.0


def test_qr10x_info_to_dict():
    info = QR10xInfo(
        device_type="QR101B-AM-1R",
        serial="00000248",
        hardware_version="5.1N",
        firmware_version="5.967KS",
        production_date="20221119",
        temperature_coefficient_ppm=25,
    )
    d = info.to_dict()
    assert d["device_type"] == "QR101B-AM-1R"
    assert d["temperature_coefficient_ppm"] == 25


def test_set_resistance_rejects_negative():
    qr = QR10x("__dummy__")  # not opened
    with pytest.raises(QR10xValueError):
        qr.set_resistance(-1.0)


def test_set_safety_limit_rejects_negative():
    qr = QR10x("__dummy__")
    with pytest.raises(QR10xValueError):
        qr.set_safety_limit(-1.0)


def test_set_resistance_rejects_non_number():
    qr = QR10x("__dummy__")
    with pytest.raises(QR10xValueError):
        qr.set_resistance("100")  # type: ignore[arg-type]


def test_incr_rejects_negative_delta():
    qr = QR10x("__dummy__")
    with pytest.raises(QR10xValueError):
        qr.incr(-10.0)


def test_decr_rejects_negative_delta():
    qr = QR10x("__dummy__")
    with pytest.raises(QR10xValueError):
        qr.decr(-10.0)


def test_unopened_cmd_raises_connection_error():
    from benchctrl.drivers.eastwood_qr10x.driver import QR10xConnectionError

    qr = QR10x("__dummy__")  # not opened
    with pytest.raises(QR10xConnectionError):
        qr._cmd("AT+DEV.SN?")


# ---------------------------------------------------------------------------
# Hardware-required — drives the real QR10x
# ---------------------------------------------------------------------------


HW_PORT = os.environ.get("BENCHCTRL_QR10X_PORT", "COM7")


@pytest.fixture
def qr():
    """Open the connected QR10x. Skips if unreachable."""
    try:
        qr = QR10x.open(HW_PORT)
    except Exception as exc:
        pytest.skip(f"QR10x not reachable on {HW_PORT}: {exc}")
    try:
        yield qr
    finally:
        qr.close()


@pytest.mark.hardware
def test_qr_info_returns_populated_fields(qr):
    info = qr.info()
    assert info.device_type
    assert info.serial
    assert info.firmware_version


@pytest.mark.hardware
def test_qr_setpoint_round_trip(qr):
    qr.set_resistance(1000.0)
    assert qr.get_setpoint() == pytest.approx(1000.0, abs=0.5)
    pv = qr.actual_resistance()
    # Class B: ±(0.02% + 20 mΩ) on the value
    tolerance = max(0.05, 0.001 * 1000.0)
    assert abs(pv - 1000.0) < tolerance, f"PV {pv} too far from SP 1000"


@pytest.mark.hardware
def test_qr_safety_limit_round_trip(qr):
    # Save, set, verify, restore
    original = qr.get_safety_limit()
    try:
        qr.set_safety_limit(50.0)
        assert qr.get_safety_limit() == pytest.approx(50.0, abs=0.5)
    finally:
        qr.set_safety_limit(original)


@pytest.mark.hardware
def test_qr_temperature_in_room_range(qr):
    t = qr.get_temperature()
    # Should be a sensible room temperature (broad window)
    assert 0.0 < t < 60.0


@pytest.mark.hardware
def test_qr_incr_decr_round_trip(qr):
    qr.set_resistance(100.0)
    qr.incr(50.0)
    assert qr.get_setpoint() == pytest.approx(150.0, abs=0.5)
    qr.decr(50.0)
    assert qr.get_setpoint() == pytest.approx(100.0, abs=0.5)


@pytest.mark.hardware
def test_qr_context_manager_closes(qr):
    """The fixture's QR was opened; verify is_open semantics."""
    assert qr.is_open

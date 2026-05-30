"""Hardware-free tests for v0.2 newly-decoded wire commands."""

from __future__ import annotations

import struct

import pytest

from benchctrl.drivers.otii_arc.protocol import (
    CMD_GET_DEVICE_NAME,
    CMD_SET_POWER_REGULATION,
    CMD_WRITE_TX,
    POWER_REGULATION_MAP,
    TYPE_GET_PARAMETER,
    TYPE_PREPARE_STOP,
    TYPE_SET_PARAMETER,
    TYPE_WRITE_TEXT,
    Response,
    encode_get_command,
    encode_gpo,
    encode_poll,
    encode_prepare_stop,
    encode_set_command,
    encode_write_tx,
    parse_response,
    power_regulation_value,
)


# ----- power regulation -----------------------------------------------------


def test_power_regulation_map_values():
    """Mode -> wire value mapping, decoded from cap #43."""
    assert POWER_REGULATION_MAP["voltage"] == 0
    assert POWER_REGULATION_MAP["current"] == 1
    assert POWER_REGULATION_MAP["inline"] == 10
    assert POWER_REGULATION_MAP["off"] == 100


def test_power_regulation_value_helper():
    assert power_regulation_value("voltage") == 0
    assert power_regulation_value("off") == 100


def test_power_regulation_value_rejects_unknown_mode():
    from benchctrl.exceptions import BenchProtocolError

    with pytest.raises(BenchProtocolError):
        power_regulation_value("eternal-glory")


def test_set_power_regulation_encoding():
    """Wire form: type=0x66 cmd=0x0A val=<mode>."""
    payload = encode_set_command(0x100, CMD_SET_POWER_REGULATION, 1)
    seq, typ, cmd, val = struct.unpack("<IIII", payload)
    assert seq == 0x100
    assert typ == TYPE_SET_PARAMETER
    assert cmd == 0x0A
    assert val == 1


# ----- GPO pin 3 / set_tx ---------------------------------------------------


def test_encode_gpo_pin_3():
    """Cap #43 confirms pin 3 (= TX pin) uses bits 6/7."""
    assert encode_gpo(3, False) == 64
    assert encode_gpo(3, True) == 128


# ----- write_tx -------------------------------------------------------------


def test_encode_write_tx_layout():
    """[seq][0x82][0x19][utf-8 bytes] — no length prefix, length implied by frame."""
    payload = encode_write_tx(0x55, "hello world")
    assert len(payload) == 4 + 4 + 4 + 11
    seq, typ, cmd = struct.unpack_from("<III", payload, 0)
    assert seq == 0x55
    assert typ == TYPE_WRITE_TEXT
    assert cmd == CMD_WRITE_TX
    assert payload[12:] == b"hello world"


def test_encode_write_tx_accepts_bytes():
    payload = encode_write_tx(0x01, b"\x01\x02\x03")
    assert payload[12:] == b"\x01\x02\x03"


def test_encode_write_tx_utf8():
    payload = encode_write_tx(0x01, "µa")
    assert payload[12:] == "µa".encode("utf-8")


# ----- prepare-stop (0x7E) --------------------------------------------------


def test_encode_prepare_stop_is_8_bytes():
    payload = encode_prepare_stop(0x42)
    assert len(payload) == 8
    seq, typ = struct.unpack("<II", payload)
    assert seq == 0x42
    assert typ == TYPE_PREPARE_STOP


# ----- POLL (0x0A) ----------------------------------------------------------


def test_encode_poll_carries_timestamp():
    payload = encode_poll(0x10, 1_234_567)
    seq, typ, ts, val = struct.unpack("<IIII", payload)
    assert seq == 0x10
    assert typ == 0x0A
    assert ts == 1_234_567
    assert val == 4  # constant in every observed POLL


# ----- GET command + Response unified format --------------------------------


def test_encode_get_command():
    payload = encode_get_command(0x07, 0x0B)
    seq, typ, cmd, val = struct.unpack("<IIII", payload)
    assert seq == 0x07
    assert typ == TYPE_GET_PARAMETER
    assert cmd == 0x0B
    assert val == 0


def test_parse_response_short_ack():
    """12-byte response: only status, no data."""
    payload = b"\x0e\x03\x99\xff" + struct.pack("<I", 0x33) + struct.pack("<i", 0)
    r = parse_response(payload)
    assert r is not None
    assert r.response_seq == 0x33
    assert r.status == 0
    assert r.ok
    assert r.data == b""


def test_parse_response_not_available():
    payload = b"\x0e\x03\x99\xff" + struct.pack("<I", 0x33) + struct.pack("<i", -3)
    r = parse_response(payload)
    assert r is not None
    assert r.not_available
    assert not r.ok


def test_parse_response_with_value_data():
    """16-byte response: status + 1 u32 value."""
    payload = (
        b"\x0e\x03\x99\xff"
        + struct.pack("<I", 0x10)
        + struct.pack("<i", 0)
        + struct.pack("<I", 3_250_000)
    )
    r = parse_response(payload)
    assert r is not None
    assert r.ok
    assert r.as_u32() == 3_250_000


def test_parse_response_with_text_data():
    """44-byte response carrying ASCII device id."""
    text = b"442032203546324D3230353235313033"
    payload = (
        b"\x0e\x03\x99\xff"
        + struct.pack("<I", 0x82)
        + struct.pack("<i", 0)
        + text
    )
    r = parse_response(payload)
    assert r is not None
    assert r.ok
    assert r.as_text() == text.decode("ascii")


def test_parse_response_rejects_garbage():
    assert parse_response(b"") is None
    assert parse_response(b"\xde\xad\xbe\xef" + b"\x00" * 8) is None


# ----- legacy parse_error_frame + parse_set_ack_frame still work ------------


def test_legacy_parse_error_frame_uses_status_word():
    """Updated v0.2 semantics: status < 0 is an error.

    The legacy v0.1 implementation hard-coded the ``04 10 00 00`` prefix
    which was actually ``seq=0x1004``. v0.2 keys off the status field,
    which is semantically correct."""
    from benchctrl.drivers.otii_arc.protocol import parse_error_frame

    # status = -101 with last_good = 3_000_000
    payload = (
        b"\x0e\x03\x99\xff"
        + struct.pack("<I", 0x1004)         # seq (any)
        + struct.pack("<i", -101)            # status
        + struct.pack("<I", 3_000_000)       # last_good
    )
    err = parse_error_frame(payload)
    assert err is not None
    assert err.error_code == -101
    assert err.last_good_value == 3_000_000


def test_legacy_parse_set_ack_returns_set_value():
    from benchctrl.drivers.otii_arc.protocol import parse_set_ack_frame

    payload = (
        b"\x0e\x03\x99\xff"
        + struct.pack("<I", 0x100B)         # seq (legacy: "cmd | 0x1000")
        + struct.pack("<i", 0)               # status = OK
        + struct.pack("<I", 3_250_000)
    )
    ack = parse_set_ack_frame(payload)
    assert ack is not None
    assert ack.value == 3_250_000

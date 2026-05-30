"""Inbound frame parsing — samples, errors, SET-ack."""

from __future__ import annotations

import struct

from benchctrl.drivers.otii_arc.protocol import (
    ERROR_PAYLOAD_HEADER,
    SAMPLE_RECORD_HEADER,
    encode_frame,
    iter_frames,
    iter_samples,
    parse_error_frame,
    parse_set_ack_frame,
)


def _sample_record(wire_id: int, value: float) -> bytes:
    return SAMPLE_RECORD_HEADER + struct.pack("<I", wire_id) + struct.pack("<f", value)


def test_iter_samples_extracts_one_record():
    payload = _sample_record(0x01, 3.25)
    samples = list(iter_samples(payload))
    assert len(samples) == 1
    assert samples[0].channel_id == 0x01
    assert abs(samples[0].value - 3.25) < 1e-5


def test_iter_samples_extracts_multiple_records_in_one_payload():
    payload = _sample_record(0x00, 0.012) + _sample_record(0x01, 3.30) + _sample_record(0x06, 0.04)
    samples = list(iter_samples(payload))
    assert [s.channel_id for s in samples] == [0x00, 0x01, 0x06]


def test_iter_samples_skips_garbage_bytes_between_records():
    payload = b"\xff\xff\xff" + _sample_record(0x01, 1.0) + b"\xee" + _sample_record(0x00, 0.5)
    samples = list(iter_samples(payload))
    assert [s.channel_id for s in samples] == [0x01, 0x00]


def test_parse_error_frame_decodes_signed_error_code():
    payload = (
        ERROR_PAYLOAD_HEADER
        + struct.pack("<i", -101)
        + struct.pack("<I", 3_000_000)
    )
    err = parse_error_frame(payload)
    assert err is not None
    assert err.error_code == -101
    assert err.last_good_value == 3_000_000


def test_parse_error_frame_rejects_wrong_length():
    short = ERROR_PAYLOAD_HEADER + b"\x00\x00\x00\x00"  # only 12 bytes
    assert parse_error_frame(short) is None


def test_parse_error_frame_rejects_wrong_header():
    bogus = b"\xde\xad\xbe\xef\x04\x10\x00\x00" + b"\x00" * 8
    assert parse_error_frame(bogus) is None


def test_parse_set_ack_frame_recognises_command_echo():
    # 0x0B = SET_MAIN_VOLTAGE; ack echoes 0x1000 | cmd_code in word[1].
    payload = (
        b"\x0e\x03\x99\xff"
        + struct.pack("<I", 0x100B)
        + struct.pack("<I", 0)
        + struct.pack("<I", 3_250_000)
    )
    ack = parse_set_ack_frame(payload)
    assert ack is not None
    assert ack.command_code == 0x0B
    assert ack.value == 3_250_000


def test_parse_set_ack_frame_does_not_match_error_frames():
    # Error frame has word[1] = 0x00001004, which would also satisfy 0x1000 mask
    payload = (
        b"\x0e\x03\x99\xff\x04\x10\x00\x00"
        + struct.pack("<i", -1)
        + struct.pack("<I", 0)
    )
    assert parse_set_ack_frame(payload) is None
    # ... but parse_error_frame should still match it
    assert parse_error_frame(payload) is not None


def test_error_frame_round_trips_through_iter_frames():
    payload = ERROR_PAYLOAD_HEADER + struct.pack("<iI", -42, 1_500_000)
    blob = encode_frame(payload)
    frames = list(iter_frames(blob))
    assert len(frames) == 1
    err = parse_error_frame(frames[0].payload)
    assert err is not None
    assert err.error_code == -42
    assert err.last_good_value == 1_500_000

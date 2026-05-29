"""Encoding of SET commands and recording payloads."""

from __future__ import annotations

import struct

from opensmu.protocol import (
    CMD_ENABLE_5V,
    CMD_SET_MAIN_VOLTAGE,
    CMD_SET_OC_PROTECTION,
    RANGE_HIGH,
    RANGE_LOW,
    START_REC_HEADER,
    START_REC_SENTINEL,
    TYPE_REC_CLEANUP,
    TYPE_SET_PARAMETER,
    TYPE_STOP_RECORDING,
    RecordingChannel,
    amps_to_microamps,
    amps_to_milliamps,
    encode_gpo,
    encode_recording_cleanup,
    encode_session_init_step2,
    encode_session_init_step3,
    encode_set_command,
    encode_start_recording,
    encode_stop_recording,
    ohms_to_microohms,
    volts_to_microvolts,
)


def _unpack4(buf):
    return struct.unpack("<IIII", buf)


def test_set_main_voltage_encoding():
    payload = encode_set_command(0x1000, CMD_SET_MAIN_VOLTAGE, volts_to_microvolts(3.3))
    seq, typ, cmd, val = _unpack4(payload)
    assert seq == 0x1000
    assert typ == TYPE_SET_PARAMETER
    assert cmd == CMD_SET_MAIN_VOLTAGE
    assert val == 3_300_000  # 3.3 V in microvolts


def test_set_oc_protection_uses_milliamps():
    payload = encode_set_command(1, CMD_SET_OC_PROTECTION, amps_to_milliamps(2.5))
    _, _, _, val = _unpack4(payload)
    assert val == 2500


def test_enable_5v_uses_specific_magic():
    payload = encode_set_command(1, CMD_ENABLE_5V, 5_000_000)
    _, _, _, val = _unpack4(payload)
    assert val == 5_000_000


def test_gpo_bit_pattern_matches_capture_observations():
    # Verified against captures in the parent project (cap #29):
    assert encode_gpo(1, True) == 2  # bit 1
    assert encode_gpo(1, False) == 1  # bit 0
    assert encode_gpo(2, True) == 16  # bit 4
    assert encode_gpo(2, False) == 8  # bit 3


def test_session_init_step2_and_step3_shape():
    p2 = encode_session_init_step2(0x5F)
    assert len(p2) == 16
    seq, typ, cmd, val = _unpack4(p2)
    assert seq == 0x5F
    assert typ == 0x64
    assert cmd == 0x29
    assert val == 0

    p3 = encode_session_init_step3(0x61)
    assert len(p3) == 16
    seq, typ, cmd, val = _unpack4(p3)
    assert seq == 0x61
    assert typ == TYPE_STOP_RECORDING  # same wire code (0x78)
    assert cmd == 0x17
    assert val == 1


def test_stop_recording_payload_targets_wire_id():
    payload = encode_stop_recording(0x100, 0x06)
    seq, typ, cmd, val = _unpack4(payload)
    assert typ == TYPE_STOP_RECORDING
    assert cmd == 0x06  # the targeted wire id
    assert val == 0


def test_recording_cleanup_is_eight_bytes():
    cleanup = encode_recording_cleanup(0x200)
    assert len(cleanup) == 8
    seq, typ = struct.unpack("<II", cleanup)
    assert seq == 0x200
    assert typ == TYPE_REC_CLEANUP


def test_recording_subtype_one_record_is_twelve_bytes():
    rec = RecordingChannel(wire_id=0x01, subtype=1, sample_rate=1000)
    encoded = rec.encode()
    assert len(encoded) == 12
    wid, sub, rate, val = struct.unpack("<HHII", encoded)
    assert wid == 0x01
    assert sub == 1
    assert rate == 1000
    assert val == 0


def test_recording_subtype_four_record_is_twentyfour_bytes():
    rec = RecordingChannel(wire_id=0x00, subtype=4, sample_rate=4000)
    encoded = rec.encode()
    assert len(encoded) == 24
    wid, sub, rate = struct.unpack_from("<HHI", encoded, 0)
    assert wid == 0x00
    assert sub == 4
    assert rate == 4000
    assert encoded[8:] == b"\x00" * 16


def test_start_recording_payload_structure():
    channels = [
        RecordingChannel(wire_id=0x00, subtype=4, sample_rate=4000),
        RecordingChannel(wire_id=0x01, subtype=1, sample_rate=1000),
    ]
    payload = encode_start_recording(channels)
    # Header + 24 (subtype-4) + 12 (subtype-1) + sentinel
    expected_len = len(START_REC_HEADER) + 24 + 12 + len(START_REC_SENTINEL)
    assert len(payload) == expected_len
    assert payload[: len(START_REC_HEADER)] == START_REC_HEADER
    assert payload[-len(START_REC_SENTINEL):] == START_REC_SENTINEL


def test_unit_conversion_helpers():
    assert volts_to_microvolts(3.3) == 3_300_000
    assert volts_to_microvolts(0) == 0
    assert amps_to_milliamps(1.5) == 1500
    assert amps_to_microamps(0.05) == 50_000
    assert ohms_to_microohms(0.1) == 100_000

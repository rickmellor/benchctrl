"""Framing encode/decode + iter_frames behaviour."""

from __future__ import annotations

import struct

import pytest

from benchctrl.exceptions import BenchProtocolError
from benchctrl.protocol import (
    WIRE_MAGIC,
    checksum,
    encode_frame,
    iter_frames,
)


def test_checksum_is_simple_sum_mod_65536():
    assert checksum(b"") == 0
    assert checksum(b"\x01\x02\x03") == 6
    assert checksum(b"\xff" * 257) == (0xFF * 257) & 0xFFFF


def test_encode_frame_structure():
    payload = b"hello world!"
    framed = encode_frame(payload)
    assert framed[:4] == WIRE_MAGIC
    L = struct.unpack_from("<H", framed, 4)[0]
    S = struct.unpack_from("<H", framed, 6)[0]
    assert len(payload) == L
    assert checksum(payload) == S
    assert framed[8:] == payload


def test_encode_frame_rejects_oversized_payload():
    with pytest.raises(BenchProtocolError):
        encode_frame(b"\x00" * 70_000)


def test_iter_frames_round_trips_single_frame():
    payload = b"\x66" * 16
    framed = encode_frame(payload)
    frames = list(iter_frames(framed))
    assert len(frames) == 1
    assert frames[0].payload == payload
    assert frames[0].offset == 0


def test_iter_frames_round_trips_multiple_frames():
    payloads = [b"\x00" * 8, b"\xaa" * 16, b"\x55" * 24]
    blob = b"".join(encode_frame(p) for p in payloads)
    frames = list(iter_frames(blob))
    assert [f.payload for f in frames] == payloads


def test_iter_frames_skips_leading_garbage():
    payload = b"\x66" * 16
    framed = encode_frame(payload)
    blob = b"garbage bytes here" + framed
    frames = list(iter_frames(blob))
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_iter_frames_resyncs_after_bad_checksum():
    payload = b"\x66" * 16
    framed = encode_frame(payload)
    # Corrupt the first frame's checksum
    bad = bytearray(framed)
    bad[6] = (bad[6] + 1) & 0xFF  # bump the checksum byte
    # Append a valid frame
    blob = bytes(bad) + framed
    frames = list(iter_frames(blob))
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_iter_frames_handles_truncated_tail():
    payload = b"\x66" * 16
    framed = encode_frame(payload)
    blob = framed + b"\xa3\x2c\xb5\x7f\x10\x00"  # tail magic + partial length only
    frames = list(iter_frames(blob))
    assert len(frames) == 1
    assert frames[0].payload == payload


def test_iter_frames_empty_buffer():
    assert list(iter_frames(b"")) == []
    assert list(iter_frames(b"\xff" * 7)) == []  # too short to be a frame

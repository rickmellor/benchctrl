"""Sample parsing, ChannelBuffer slicing, statistics."""

from __future__ import annotations

import math
import struct

import pytest

from benchctrl.channels import Channel
from benchctrl.protocol import (
    SAMPLE_RECORD_HEADER,
    encode_frame,
)
from benchctrl.samples import (
    ChannelBuffer,
    compute_statistics,
    parse_samples_by_channel,
    parse_samples_by_id,
    write_csv_long,
    write_csv_wide,
    write_json,
)


def _frame_for(wire_id: int, value: float) -> bytes:
    payload = SAMPLE_RECORD_HEADER + struct.pack("<I", wire_id) + struct.pack("<f", value)
    return encode_frame(payload)


def test_parse_samples_by_id_extracts_grouped():
    buf = b"".join(
        [
            _frame_for(0x01, 3.30),
            _frame_for(0x01, 3.31),
            _frame_for(0x00, 0.012),
        ]
    )
    out = parse_samples_by_id(buf)
    assert sorted(out.keys()) == [0x00, 0x01]
    assert len(out[0x01]) == 2
    assert math.isclose(out[0x01][0], 3.30, abs_tol=1e-5)


def test_parse_samples_by_channel_uses_enum_keys():
    buf = _frame_for(0x01, 3.30) + _frame_for(0x06, 0.05)
    out = parse_samples_by_channel(buf)
    assert Channel.MAIN_VOLTAGE in out
    assert Channel.MAIN_POWER in out


def test_channel_buffer_timestamps_align_with_rate():
    buf = ChannelBuffer(channel=Channel.MAIN_VOLTAGE, sample_rate=1000, t0=0.5)
    buf.extend([1.0, 2.0, 3.0, 4.0])
    ts = buf.timestamps()
    assert ts == pytest.approx([0.5, 0.501, 0.502, 0.503])
    assert buf.duration == pytest.approx(0.004)
    assert buf.t_end == pytest.approx(0.504)


def test_channel_buffer_slice_indices_clamp_to_range():
    buf = ChannelBuffer(channel=Channel.MAIN_VOLTAGE, sample_rate=1000, t0=0.0)
    buf.extend([0.0] * 100)
    assert buf.slice_indices(None, None) == (0, 100)
    assert buf.slice_indices(0.05, 0.07) == (50, 70)
    # Out of bounds clamps
    assert buf.slice_indices(-1.0, 10.0) == (0, 100)


def test_compute_statistics_min_max_avg_rms():
    buf = ChannelBuffer(channel=Channel.MAIN_VOLTAGE, sample_rate=1000)
    buf.extend([1.0, 2.0, 3.0, 4.0])
    stats = compute_statistics(buf)
    assert stats.sample_count == 4
    assert stats.min == 1.0
    assert stats.max == 4.0
    assert math.isclose(stats.average, 2.5)
    expected_rms = math.sqrt((1 + 4 + 9 + 16) / 4)
    assert math.isclose(stats.rms, expected_rms)
    # Voltage channel: no charge/energy
    assert stats.charge is None
    assert stats.energy is None


def test_compute_statistics_charge_on_current_channel():
    buf = ChannelBuffer(channel=Channel.MAIN_CURRENT, sample_rate=4000)
    # 1 A constant for 1 second = 1 C
    buf.extend([1.0] * 4000)
    stats = compute_statistics(buf)
    assert stats.duration == pytest.approx(1.0)
    assert stats.charge == pytest.approx(1.0, rel=1e-3)
    assert stats.energy is None


def test_compute_statistics_energy_on_power_channel():
    buf = ChannelBuffer(channel=Channel.MAIN_POWER, sample_rate=4000)
    # 0.5 W constant for 1 second = 0.5 J
    buf.extend([0.5] * 4000)
    stats = compute_statistics(buf)
    assert stats.energy == pytest.approx(0.5, rel=1e-3)
    assert stats.charge is None


def test_compute_statistics_empty_returns_zero_count():
    buf = ChannelBuffer(channel=Channel.MAIN_VOLTAGE, sample_rate=1000)
    stats = compute_statistics(buf)
    assert stats.sample_count == 0
    assert math.isnan(stats.min)


def test_write_csv_long_round_trip(tmp_path):
    buffers = {
        Channel.MAIN_VOLTAGE: ChannelBuffer(
            channel=Channel.MAIN_VOLTAGE, sample_rate=1000, values=[3.3, 3.31]
        ),
        Channel.MAIN_CURRENT: ChannelBuffer(
            channel=Channel.MAIN_CURRENT, sample_rate=4000, values=[0.01, 0.012, 0.011, 0.010]
        ),
    }
    p = tmp_path / "out.csv"
    write_csv_long(p, buffers)
    lines = p.read_text().splitlines()
    assert lines[0] == "timestamp_s,channel,value,unit"
    # 2 mv rows + 4 mc rows + 1 header = 7
    assert len(lines) == 7


def test_write_csv_wide_has_one_column_per_channel(tmp_path):
    buffers = {
        Channel.MAIN_VOLTAGE: ChannelBuffer(
            channel=Channel.MAIN_VOLTAGE, sample_rate=1000, values=[3.3]
        ),
        Channel.MAIN_CURRENT: ChannelBuffer(
            channel=Channel.MAIN_CURRENT, sample_rate=4000, values=[0.01, 0.012, 0.011, 0.010]
        ),
    }
    p = tmp_path / "wide.csv"
    write_csv_wide(p, buffers)
    header = p.read_text().splitlines()[0]
    assert "timestamp_s" in header
    assert "mc (A)" in header
    assert "mv (V)" in header


def test_write_json_round_trip(tmp_path):
    import json

    buffers = {
        Channel.MAIN_VOLTAGE: ChannelBuffer(
            channel=Channel.MAIN_VOLTAGE, sample_rate=1000, values=[3.3, 3.31]
        )
    }
    p = tmp_path / "out.json"
    write_json(p, buffers, metadata={"name": "test"})
    blob = json.loads(p.read_text())
    assert blob["metadata"]["name"] == "test"
    assert "mv" in blob["channels"]
    assert blob["channels"]["mv"]["sample_rate"] == 1000
    assert blob["channels"]["mv"]["values"] == [3.3, 3.31]

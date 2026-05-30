"""Recording in-memory operations + accessors."""

from __future__ import annotations

import pytest

from benchctrl.channels import Channel
from benchctrl.exceptions import BenchValueError
from benchctrl.recording import Recording


def _make_recording_with_data():
    rec = Recording(name="test")
    buf = rec._ensure_buffer(Channel.MAIN_VOLTAGE, sample_rate=1000)
    buf.extend([3.30, 3.31, 3.32, 3.33, 3.34])
    return rec


def test_info_uses_offset():
    rec = _make_recording_with_data()
    info = rec.info(Channel.MAIN_VOLTAGE)
    assert info.from_time == 0.0
    assert info.sample_rate == 1000
    assert info.count == 5

    rec.offset = 1.0
    info = rec.info(Channel.MAIN_VOLTAGE)
    assert info.from_time == 1.0


def test_buffer_missing_channel_raises():
    rec = _make_recording_with_data()
    with pytest.raises(BenchValueError):
        rec.buffer(Channel.MAIN_CURRENT)


def test_statistics_via_recording():
    rec = _make_recording_with_data()
    stats = rec.statistics(Channel.MAIN_VOLTAGE)
    assert stats.sample_count == 5
    assert stats.min == 3.30
    assert stats.max == 3.34


def test_data_respects_offset_window():
    rec = _make_recording_with_data()
    rec.offset = 5.0
    # Pick a window with bounds well inside sample-boundary intervals so
    # floating-point granularity doesn't push i0/i1 one index either way.
    # 1 kHz rate -> samples at 5.000, 5.001, 5.002, 5.003, 5.004.
    # Window [5.0015, 5.0035] covers indices 2 and 3 = 2 samples.
    data = rec.data(Channel.MAIN_VOLTAGE, start=5.0015, end=5.0035)
    assert len(data) == 2


def test_crop_trims_in_place():
    rec = _make_recording_with_data()
    # crop to [0.001, 0.003]
    rec.crop(0.001, 0.003)
    buf = rec.buffer(Channel.MAIN_VOLTAGE)
    assert len(buf.values) == 2


def test_downsample_halves_count_and_rate():
    rec = _make_recording_with_data()
    rec.downsample(Channel.MAIN_VOLTAGE, factor=2)
    buf = rec.buffer(Channel.MAIN_VOLTAGE)
    assert len(buf.values) == 3  # 5 -> ceil(5/2) = 3
    assert buf.sample_rate == 500


def test_downsample_rejects_bad_factor():
    rec = _make_recording_with_data()
    with pytest.raises(BenchValueError):
        rec.downsample(Channel.MAIN_VOLTAGE, factor=0)


def test_rename():
    rec = _make_recording_with_data()
    rec.rename("renamed")
    assert rec.name == "renamed"


def test_log_appends_to_notes():
    rec = _make_recording_with_data()
    rec.log("hello", timestamp=1.0)
    rec.log("world", timestamp=2.0)
    assert rec.device_info["notes"] == [
        {"timestamp": 1.0, "text": "hello"},
        {"timestamp": 2.0, "text": "world"},
    ]


def test_index_at_for_internal_timestamps():
    rec = _make_recording_with_data()
    # 1 kHz sample rate, t0=0; t=0.0035 -> sample index 3
    assert rec.index_at(Channel.MAIN_VOLTAGE, 0.0035) == 3


def test_contains_dunder():
    rec = _make_recording_with_data()
    assert Channel.MAIN_VOLTAGE in rec
    assert Channel.MAIN_CURRENT not in rec
    assert "mv" in rec
    assert "mc" not in rec


def test_deferred_methods_raise():
    from benchctrl.exceptions import BenchNotImplementedError

    rec = _make_recording_with_data()
    with pytest.raises(BenchNotImplementedError):
        rec.get_log_offset(Channel.MAIN_VOLTAGE)
    with pytest.raises(BenchNotImplementedError):
        rec.import_log("/tmp/x", "auto")
    with pytest.raises(BenchNotImplementedError):
        rec.append_user_log("id", 0.0, "msg")

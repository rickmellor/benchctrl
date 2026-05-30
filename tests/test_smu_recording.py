"""Hardware-required: recording lifecycle + statistics on real data."""

from __future__ import annotations

import time

import pytest

from benchctrl import Recording
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel

pytestmark = pytest.mark.hardware


def test_record_context_manager_yields_samples(smu):
    smu.disable_all_channels()
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    with smu.record(name="ctx-test") as rec:
        time.sleep(2.0)
    assert isinstance(rec, Recording)
    assert not rec.is_running
    mv = rec.buffer(Channel.MAIN_VOLTAGE)
    mc = rec.buffer(Channel.MAIN_CURRENT)
    # Native rates after the type=0x78 channel-enable unlock: mv = 1 kHz
    # (1 sample per packed frame), mc = 4 kHz (4 samples per packed frame).
    # Allow 75% headroom for thread scheduling and start/stop overhead.
    assert len(mv.values) >= 1500, f"only {len(mv.values)} mv samples (expected >=1500 @ 1 kHz)"
    assert len(mc.values) >= 6000, f"only {len(mc.values)} mc samples (expected >=6000 @ 4 kHz)"


def test_record_statistics_on_main_voltage(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    with smu.record(name="stats-test") as rec:
        time.sleep(1.0)
    stats = rec.statistics(Channel.MAIN_VOLTAGE)
    assert stats.sample_count > 0
    # With output off and no load, main voltage reads near 0 V
    assert -1.0 < stats.average < 4.0  # generous range


def test_recording_save_csv_long(smu, tmp_path):
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    with smu.record() as rec:
        time.sleep(0.8)
    out = rec.save_csv(tmp_path / "long.csv", format="long")
    lines = out.read_text().splitlines()
    assert lines[0].startswith("timestamp_s,")
    assert len(lines) > 1


def test_recording_save_csv_wide(smu, tmp_path):
    smu.disable_all_channels()
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    with smu.record() as rec:
        time.sleep(0.8)
    out = rec.save_csv(tmp_path / "wide.csv", format="wide")
    header = out.read_text().splitlines()[0]
    assert "mv" in header
    assert "mc" in header


def test_recording_native_round_trip(smu, tmp_path):
    smu.disable_all_channels()
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.VBUS)
    with smu.record(name="rt") as rec:
        time.sleep(0.8)
    out = rec.save(tmp_path / "rt.opensmu")
    loaded = Recording.load(out)
    assert loaded.name == "rt"
    assert Channel.MAIN_VOLTAGE in loaded
    assert Channel.VBUS in loaded
    # Sample counts match
    assert loaded.count(Channel.MAIN_VOLTAGE) == rec.count(Channel.MAIN_VOLTAGE)


def test_recording_charge_on_current_channel(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_CURRENT)
    with smu.record() as rec:
        time.sleep(1.0)
    stats = rec.statistics(Channel.MAIN_CURRENT)
    # Current channel should have charge populated (whether 0 or not)
    assert stats.charge is not None


def test_recording_energy_on_power_channel(smu):
    smu.disable_all_channels()
    smu.enable_channels(Channel.MAIN_CURRENT)  # auto-co-enables mp
    with smu.record() as rec:
        time.sleep(1.0)
    stats = rec.statistics(Channel.MAIN_POWER)
    assert stats.energy is not None


def test_start_stop_recording_outside_context(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    rec = smu.start_recording(name="manual")
    assert rec.is_running
    time.sleep(0.5)
    returned = smu.stop_recording()
    assert returned is rec
    assert not rec.is_running

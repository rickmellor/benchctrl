"""Hardware-required: live streaming iterator."""

from __future__ import annotations

import pytest

from benchctrl import Sample
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel

pytestmark = pytest.mark.hardware


def test_stream_yields_samples(smu):
    count = 0
    for sample in smu.stream(seconds=1.0):
        assert isinstance(sample, Sample)
        assert isinstance(sample.channel, Channel)
        count += 1
        if count > 100:
            break
    assert count > 0


def test_stream_during_recording_raises(smu):
    from benchctrl.exceptions import BenchValueError

    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    smu.start_recording()
    try:
        with pytest.raises(BenchValueError):
            next(iter(smu.stream(seconds=0.5)))
    finally:
        smu.stop_recording()


# ----- read_window ---------------------------------------------------------


def test_read_window_returns_parsed_samples(smu):
    samples = smu.read_window(
        [Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT], 0.5
    )
    assert Channel.MAIN_VOLTAGE in samples
    assert Channel.MAIN_CURRENT in samples
    # Baseline streaming on all channels at ~6 Hz, so a 0.5 s window
    # should give at least one sample on each requested channel.
    assert len(samples[Channel.MAIN_VOLTAGE]) >= 1
    assert len(samples[Channel.MAIN_CURRENT]) >= 1


def test_read_window_ignores_unrequested_channels(smu):
    samples = smu.read_window([Channel.MAIN_VOLTAGE], 0.5)
    assert set(samples.keys()) == {Channel.MAIN_VOLTAGE}


def test_read_window_during_recording_raises(smu):
    from benchctrl.exceptions import BenchValueError

    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    smu.start_recording()
    try:
        with pytest.raises(BenchValueError):
            smu.read_window([Channel.MAIN_VOLTAGE], 0.5)
    finally:
        smu.stop_recording()

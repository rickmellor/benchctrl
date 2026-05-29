"""Hardware-required: live streaming iterator."""

from __future__ import annotations

import pytest

from opensmu import Channel, Sample


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
    from opensmu.exceptions import SMUValueError

    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    rec = smu.start_recording()
    try:
        with pytest.raises(SMUValueError):
            next(iter(smu.stream(seconds=0.5)))
    finally:
        smu.stop_recording()

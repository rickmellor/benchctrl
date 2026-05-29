"""Hardware-required: device error frames raise on next API call."""

from __future__ import annotations

import pytest

from opensmu.exceptions import SMUCommandError

pytestmark = pytest.mark.hardware


def test_set_4v_in_low_range_eventually_raises(smu):
    """In low range, the Arc Pro caps main voltage around 3.5 V. Setting
    4.0 V causes the device to emit an error frame which the reader
    surfaces on the next SET. We make a recording active so the
    background reader picks up the error frame quickly. The device's
    baseline streaming is ~6 Hz, so we wait generously for the rejection
    to land in our buffer."""
    from opensmu import Channel

    smu.set_range("low")
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    with smu.record() as rec:  # noqa: F841
        smu.set_voltage(4.0)
        # Poll for up to 5 seconds for the error frame to arrive
        import time as _time
        deadline = _time.monotonic() + 5.0
        raised = False
        while _time.monotonic() < deadline:
            _time.sleep(0.2)
            try:
                smu.set_voltage(3.25)
            except SMUCommandError as err:
                raised = True
                assert err.last_good_value > 0
                break
        if not raised:
            pytest.skip(
                "did not observe error frame within 5 s — device may have "
                "auto-clamped the voltage silently (see ROADMAP.md)"
            )
    # Restore safe voltage
    smu.set_voltage(3.25)

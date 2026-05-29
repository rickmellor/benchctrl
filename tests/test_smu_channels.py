"""Hardware-required: channel enable/disable + co-enables."""

from __future__ import annotations

import pytest

from opensmu import Channel


pytestmark = pytest.mark.hardware


def test_enable_channel_marks_state(smu):
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    assert Channel.MAIN_VOLTAGE in smu.enabled_channels


def test_enable_main_current_co_enables_main_power(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.MAIN_CURRENT)
    assert Channel.MAIN_CURRENT in smu.enabled_channels
    assert Channel.MAIN_POWER in smu.enabled_channels


def test_enable_adc_current_co_enables_adc_power(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.ADC_CURRENT)
    assert Channel.ADC_CURRENT in smu.enabled_channels
    assert Channel.ADC_POWER in smu.enabled_channels


def test_enable_channels_varargs(smu):
    smu.disable_all_channels()
    smu.enable_channels("mv", "mc", Channel.VBUS)
    assert Channel.MAIN_VOLTAGE in smu.enabled_channels
    assert Channel.MAIN_CURRENT in smu.enabled_channels
    assert Channel.VBUS in smu.enabled_channels


def test_disable_channel_removes(smu):
    smu.enable_channel(Channel.MAIN_VOLTAGE)
    smu.disable_channel(Channel.MAIN_VOLTAGE)
    assert Channel.MAIN_VOLTAGE not in smu.enabled_channels


def test_temperature_always_on_enable_is_noop(smu):
    smu.disable_all_channels()
    smu.enable_channel(Channel.TEMPERATURE)
    # Temperature is documented as always-streamed and not toggleable;
    # enabling it should not add it to the host-side set.
    assert Channel.TEMPERATURE not in smu.enabled_channels


def test_get_channel_samplerate_returns_native(smu):
    assert smu.get_channel_samplerate(Channel.MAIN_CURRENT) == 4000
    assert smu.get_channel_samplerate(Channel.MAIN_VOLTAGE) == 1000

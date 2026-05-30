"""Channel enum + metadata."""

from __future__ import annotations

import pytest

from benchctrl.channels import WIRE_ID_TO_CHANNEL, Channel, ChannelInfo


def test_main_channels_have_correct_metadata():
    assert Channel.MAIN_CURRENT.code == "mc"
    assert Channel.MAIN_CURRENT.wire_id == 0x00
    assert Channel.MAIN_CURRENT.subtype == 4
    assert Channel.MAIN_CURRENT.sample_rate == 4000
    assert Channel.MAIN_CURRENT.unit == "A"

    assert Channel.MAIN_VOLTAGE.code == "mv"
    assert Channel.MAIN_VOLTAGE.wire_id == 0x01
    assert Channel.MAIN_VOLTAGE.subtype == 1
    assert Channel.MAIN_VOLTAGE.sample_rate == 1000
    assert Channel.MAIN_VOLTAGE.unit == "V"

    assert Channel.MAIN_POWER.code == "mp"
    assert Channel.MAIN_POWER.wire_id == 0x06
    assert Channel.MAIN_POWER.subtype == 4
    assert Channel.MAIN_POWER.unit == "W"


def test_all_channels_have_two_letter_code_or_text():
    for ch in Channel:
        assert isinstance(ch.value, ChannelInfo)
        # all codes are exactly two ASCII chars
        assert len(ch.code) == 2


def test_from_code_normalisation():
    assert Channel.from_code("mc") is Channel.MAIN_CURRENT
    assert Channel.from_code("MC") is Channel.MAIN_CURRENT
    assert Channel.from_code(" mv ") is Channel.MAIN_VOLTAGE
    with pytest.raises(KeyError):
        Channel.from_code("xx")


def test_coerce_accepts_enum_and_string():
    assert Channel.coerce(Channel.MAIN_VOLTAGE) is Channel.MAIN_VOLTAGE
    assert Channel.coerce("mc") is Channel.MAIN_CURRENT
    with pytest.raises(TypeError):
        Channel.coerce(42)  # type: ignore[arg-type]


def test_co_enables_mc_to_mp_and_ac_to_ap():
    assert "mp" in Channel.MAIN_CURRENT.co_enables
    assert "ap" in Channel.ADC_CURRENT.co_enables
    # Their co-enable targets do not themselves co-enable anything
    assert Channel.MAIN_POWER.co_enables == ()
    assert Channel.ADC_POWER.co_enables == ()


def test_temperature_is_not_toggleable():
    assert Channel.TEMPERATURE.toggleable is False


def test_gpi_shares_wire_id_but_distinct_enum():
    assert Channel.GPI1.wire_id == Channel.GPI2.wire_id == 0x16
    assert Channel.GPI1 is not Channel.GPI2


def test_wire_id_reverse_lookup_resolves_canonical_channels():
    assert WIRE_ID_TO_CHANNEL[0x00] is Channel.MAIN_CURRENT
    assert WIRE_ID_TO_CHANNEL[0x01] is Channel.MAIN_VOLTAGE
    assert WIRE_ID_TO_CHANNEL[0x06] is Channel.MAIN_POWER
    assert WIRE_ID_TO_CHANNEL[0x14] is Channel.TEMPERATURE
    # 0x16 is shared; first declared wins (GPI1)
    assert WIRE_ID_TO_CHANNEL[0x16] is Channel.GPI1

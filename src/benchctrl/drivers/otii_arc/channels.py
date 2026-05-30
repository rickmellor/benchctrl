"""Channel enum and per-channel metadata.

Each channel carries the two-letter code (matching the official Otii API),
the wire id used in frames, the wire subtype (1 or 4), the native sample
rate in samples-per-second, the unit string, and a friendly label.

Use the enum constants in new code; strings are accepted at API boundaries
for backwards-friendly use.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import Enum
from typing import Union


@dataclass(frozen=True)
class ChannelInfo:
    """Static descriptor of a measurement channel."""

    code: str  # two-letter code, e.g. "mc"
    wire_id: int  # device-side channel id used in frames and START_RECORDING records
    subtype: int  # 1 for low-rate (1 kHz), 4 for high-rate (4 kHz)
    sample_rate: int  # native samples/sec
    unit: str  # "V" / "A" / "W" / "C" (Celsius) / "" (digital) / "text"
    label: str  # human-friendly label
    toggleable: bool = True  # if False, always-on (temperature, GPI bitmap)
    co_enables: tuple[str, ...] = ()  # other channel codes the device auto-enables
    digital: bool = False  # digital pin (i1, i2) vs analog
    text: bool = False  # text stream (rx)


class OtiiArcChannel(Enum):
    """Measurement channels on the Arc/Arc Pro.

    Use ``OtiiArcChannel.MAIN_CURRENT`` etc., or the short code via
    :py:meth:`OtiiArcChannel.from_code`.
    """

    MAIN_CURRENT = ChannelInfo("mc", 0x00, 4, 4000, "A", "Main Current",
                               co_enables=("mp",))
    MAIN_VOLTAGE = ChannelInfo("mv", 0x01, 1, 1000, "V", "Main Voltage")
    MAIN_POWER = ChannelInfo("mp", 0x06, 4, 4000, "W", "Main Power")
    ADC_CURRENT = ChannelInfo("ac", 0x02, 1, 1000, "A", "ADC Current",
                              co_enables=("ap",))
    ADC_VOLTAGE = ChannelInfo("av", 0x03, 1, 1000, "V", "ADC Voltage")
    ADC_POWER = ChannelInfo("ap", 0x07, 1, 1000, "W", "ADC Power")
    SENSE_PLUS = ChannelInfo("sp", 0x05, 1, 1000, "V", "Sense+ Voltage")
    SENSE_MINUS = ChannelInfo("sn", 0x04, 1, 1000, "V", "Sense- Voltage")
    VBUS = ChannelInfo("vb", 0x10, 1, 1000, "V", "VBUS")
    DC_JACK = ChannelInfo("vj", 0x11, 1, 1000, "V", "DC Jack")
    TEMPERATURE = ChannelInfo("tp", 0x14, 1, 1000, "C", "Temperature",
                              toggleable=False)
    GPI1 = ChannelInfo("i1", 0x16, 1, 1000, "", "GPI 1", digital=True)
    GPI2 = ChannelInfo("i2", 0x16, 1, 1000, "", "GPI 2", digital=True)
    UART_RX = ChannelInfo("rx", 0x17, 1, 0, "text", "UART log", text=True)

    @property
    def code(self) -> str:
        return self.value.code

    @property
    def wire_id(self) -> int:
        return self.value.wire_id

    @property
    def subtype(self) -> int:
        return self.value.subtype

    @property
    def sample_rate(self) -> int:
        return self.value.sample_rate

    @property
    def unit(self) -> str:
        return self.value.unit

    @property
    def label(self) -> str:
        return self.value.label

    @property
    def toggleable(self) -> bool:
        return self.value.toggleable

    @property
    def co_enables(self) -> tuple[str, ...]:
        return self.value.co_enables

    @classmethod
    def from_code(cls, code: str) -> Channel:
        """Look up a channel by its two-letter code."""
        norm = code.strip().lower()
        for ch in cls:
            if ch.value.code == norm:
                return ch
        raise KeyError(f"no channel with code {code!r}")

    @classmethod
    def coerce(cls, value) -> OtiiArcChannel:
        """Accept an `OtiiArcChannel`, a `StandardChannel`, or a short code."""
        from benchctrl.channels import StandardChannel
        if isinstance(value, cls):
            return value
        if isinstance(value, StandardChannel):
            return cls.from_code(value.code)
        if isinstance(value, str):
            return cls.from_code(value)
        raise TypeError(
            f"expected OtiiArcChannel, StandardChannel, or str, "
            f"got {type(value).__name__}"
        )


# Wire-id reverse lookup, including the alias for GPI bitmap.
WIRE_ID_TO_CHANNEL: dict[int, OtiiArcChannel] = {}
for _ch in OtiiArcChannel:
    # GPI1 and GPI2 share a wire id; first one wins (bitmap is decoded later).
    WIRE_ID_TO_CHANNEL.setdefault(_ch.wire_id, _ch)

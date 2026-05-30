"""Standard channel identifiers used by the framework.

`StandardChannel` lists the measurement channel names that the
framework-level subsystems (battery emulator, validation harness,
analysis tools) depend on. Concrete drivers expose richer per-vendor
channel enums in their own modules (see
``benchctrl.drivers.otii_arc.channels.OtiiArcChannel``) — those
driver-specific enums are expected to include members with matching
codes for the standard subset.

The two-letter codes (``"mc"`` / ``"mv"`` / ``"mp"``) match Otii's
historic naming and serve as a stable interchange format across
drivers.
"""

from __future__ import annotations

from enum import Enum


class StandardChannel(Enum):
    """The minimal channel inventory framework subsystems expect.

    Members:
        MAIN_CURRENT: Through-the-shunt current at the DUT (``"mc"``).
        MAIN_VOLTAGE: Voltage at the DUT terminals (``"mv"``).
        MAIN_POWER:   Instantaneous power V·I at the DUT (``"mp"``).

    Driver-specific enums (e.g. ``OtiiArcChannel``) extend this with
    the device's full inventory. Where a driver implements the
    standard subset, its members carry the same ``code`` so a string
    code can be used interchangeably.
    """

    MAIN_CURRENT = "mc"
    MAIN_VOLTAGE = "mv"
    MAIN_POWER = "mp"

    @property
    def code(self) -> str:
        """The two-letter interchange code."""
        return self.value

    @classmethod
    def from_code(cls, code: str) -> "StandardChannel":
        for member in cls:
            if member.value == code:
                return member
        raise ValueError(f"unknown standard channel code: {code!r}")

    def __str__(self) -> str:  # nicer in error messages
        return f"{type(self).__name__}.{self.name}"

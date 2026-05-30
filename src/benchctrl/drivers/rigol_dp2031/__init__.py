"""Rigol DP2031 triple-output programmable DC power supply driver.

Exposes:

- :py:class:`RigolDP2031` — the device class. Per-channel V/I setpoints,
  OVP / OCP protection, measurement, tracking / pair / sense modes,
  the Timer (Arb-like sequencer) subsystem, the Analyzer (power /
  IoT energy capture), trigger I/O on the rear digital lines, and
  file storage on the internal C disk + USB drives.
- :py:class:`DP2031Channel` — IntEnum identifying CH1 / CH2 / CH3.
- :py:class:`RigolDP2031Info` — identity / firmware metadata.
- :py:class:`RigolDP2031Error` and subclasses — driver-specific
  exceptions.

Wire stack: USB-TMC + SCPI via pyvisa. LAN / RS232 / GPIB are
deliberately out of scope for this driver — USB only.

Implements the manual's three-channel concrete surface; it does NOT
implement the :py:class:`benchctrl.interfaces.SourceMeasurementUnit`
Protocol today (different shape — multi-channel + no per-instant
recording stream).
"""

from benchctrl.drivers.rigol_dp2031.driver import (
    DP2031Channel,
    RigolDP2031,
    RigolDP2031CommandError,
    RigolDP2031ConnectionError,
    RigolDP2031Error,
    RigolDP2031Info,
    RigolDP2031TimeoutError,
    RigolDP2031ValueError,
)

__all__ = [
    "RigolDP2031",
    "RigolDP2031Info",
    "DP2031Channel",
    "RigolDP2031Error",
    "RigolDP2031ConnectionError",
    "RigolDP2031CommandError",
    "RigolDP2031TimeoutError",
    "RigolDP2031ValueError",
]

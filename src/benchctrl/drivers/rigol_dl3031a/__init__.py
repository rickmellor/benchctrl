"""Rigol DL3031A programmable DC electronic load driver.

Exposes:

- :py:class:`RigolDL3031A` — the device class. CC / CV / CR / CP modes
  plus firmware-side LIST sequence, transient pulse, battery
  discharge, OCP / OPP modes.
- :py:class:`RigolDLInfo` — identity / firmware metadata.
- :py:class:`RigolDLError` and subclasses — driver-specific exceptions.

Wire stack: USB-TMC (or LAN, or RS232) via pyvisa. Needs
``benchctrl[bench-visa]`` extras.

Like QR10x, the DL3031A doesn't fit
:py:class:`SourceMeasurementUnit` cleanly — it's a programmable load
with its own quite rich SCPI surface. Concrete class; full surface is
the contract.
"""

from benchctrl.drivers.rigol_dl3031a.driver import (
    RigolDL3031A,
    RigolDLCommandError,
    RigolDLConnectionError,
    RigolDLError,
    RigolDLInfo,
    RigolDLTimeoutError,
    RigolDLValueError,
)

__all__ = [
    "RigolDL3031A",
    "RigolDLInfo",
    "RigolDLError",
    "RigolDLConnectionError",
    "RigolDLCommandError",
    "RigolDLTimeoutError",
    "RigolDLValueError",
]

"""Bench instruments — companions to the SMU for measurement / DUT loads.

benchctrl's "bench" subpackage hosts drivers for other lab instruments
typically wired alongside an Otii Arc Pro:

- **Programmable resistors** for emulator validation, current-draw
  testing, sensor simulation
- **Electronic loads** for higher-current testing (DL3031A class)
- **DMMs / scopes** for cross-validation

Each driver is independent and optional. Import only what you need.

Currently available:

- :py:class:`benchctrl.bench.QR10x` — Eastwood Tech QR10x programmable
  resistance substitution box (CH340 USB-Serial, AT command set)
- :py:class:`benchctrl.bench.RigolDL3031A` — Rigol DL3031A programmable
  DC electronic load (USB-TMC via pyvisa; install `benchctrl[bench-visa]`)

The DL3031A driver is lazily exported: importing ``benchctrl.bench``
will not pull in pyvisa unless you reach for the symbol.
"""

from benchctrl.bench.qr10x import QR10x, QR10xError, QR10xInfo

__all__ = [
    "QR10x", "QR10xInfo", "QR10xError",
    "RigolDL3031A", "RigolDLInfo", "RigolDLError",
]


def __getattr__(name):
    # PEP 562: lazy attribute lookup. Keeps pyvisa out of the import
    # graph for callers that only use QR10x.
    if name in ("RigolDL3031A", "RigolDLInfo", "RigolDLError"):
        from benchctrl.bench.rigol_dl3031a import (
            RigolDL3031A, RigolDLInfo, RigolDLError,
        )
        return {
            "RigolDL3031A": RigolDL3031A,
            "RigolDLInfo": RigolDLInfo,
            "RigolDLError": RigolDLError,
        }[name]
    raise AttributeError(f"module 'benchctrl.bench' has no attribute {name!r}")

"""Bench instruments — companions to the SMU for measurement / DUT loads.

OpenSMU's "bench" subpackage hosts drivers for other lab instruments
typically wired alongside an Otii Arc Pro:

- **Programmable resistors** for emulator validation, current-draw
  testing, sensor simulation
- **Electronic loads** for higher-current testing (DL3031A class)
- **DMMs / scopes** for cross-validation

Each driver is independent and optional. Import only what you need.

Currently available:

- :py:class:`opensmu.bench.QR10x` — Eastwood Tech QR10x programmable
  resistance substitution box (CH340 USB-Serial, AT command set)
"""

from opensmu.bench.qr10x import QR10x, QR10xError, QR10xInfo

__all__ = ["QR10x", "QR10xInfo", "QR10xError"]

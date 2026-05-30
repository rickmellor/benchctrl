"""Eastwood Tech QR10x programmable resistance substitution box driver.

Exposes:

- :py:class:`QR10x` — the device class. Set / read resistance, safety
  limits, temperature.
- :py:class:`QR10xInfo` — identity / firmware metadata.
- :py:class:`QR10xError` and subclasses — driver-specific exceptions.

Wire stack: USB-Serial (CH340), 115200 8N1, AT command set.

The QR10x doesn't fit the :py:class:`SourceMeasurementUnit` Protocol
(it's a pure programmable load, not a source-measure device). It's a
concrete class — callers depend on its full surface directly.
"""

from benchctrl.drivers.eastwood_qr10x.driver import (
    QR10x,
    QR10xConnectionError,
    QR10xError,
    QR10xInfo,
    QR10xProtocolError,
    QR10xTimeoutError,
    QR10xValueError,
)

__all__ = [
    "QR10x",
    "QR10xInfo",
    "QR10xError",
    "QR10xConnectionError",
    "QR10xProtocolError",
    "QR10xTimeoutError",
    "QR10xValueError",
]

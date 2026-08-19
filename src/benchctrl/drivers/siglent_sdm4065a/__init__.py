"""Siglent SDM4065A 6½-digit bench digital multimeter driver.

Exposes:

- :py:class:`SiglentSDM4065A` — the device class. DC/AC volts and amps,
  2- and 4-wire resistance, capacitance, frequency/period, continuity,
  diode and temperature, plus range/NPLC/autozero/null control and the
  ``CONFigure``/``INITiate``/``FETCh`` acquisition model.
- :py:class:`SDM4065AInfo` — identity / firmware metadata.
- :py:class:`SDM4065AError` and subclasses — driver-specific exceptions.

Wire stack: USB-TMC (or LAN) via pyvisa. Needs ``benchctrl[bench-visa]``
extras. With the pyvisa-py backend, USB-TMC runs in userspace over
libusb, so no kernel ``usbtmc`` module is required.

Concrete class, no Protocol: this is the first DMM in the tree, and a
measurement abstraction invented for a single instance would be shape
guessed from one example. The full surface is the contract; a Protocol
can be extracted when a second meter lands.

The one thing worth reading before measuring low resistance: 2-wire
readings carry ~0.2 Ω of lead and contact error unless nulled
(datasheet note [6]). Use :py:meth:`SiglentSDM4065A.null_now`, or 4-wire.
"""

from benchctrl.drivers.siglent_sdm4065a.driver import (
    SDM4065ACommandError,
    SDM4065AConnectionError,
    SDM4065AError,
    SDM4065AInfo,
    SDM4065AOverloadError,
    SDM4065ATimeoutError,
    SDM4065AValueError,
    SiglentSDM4065A,
    discover,
)

__all__ = [
    "SiglentSDM4065A",
    "SDM4065AInfo",
    "SDM4065AError",
    "SDM4065AConnectionError",
    "SDM4065ACommandError",
    "SDM4065AOverloadError",
    "SDM4065ATimeoutError",
    "SDM4065AValueError",
    "discover",
]

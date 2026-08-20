"""Transports that make a device reachable when the kernel cannot.

A userspace CH341 USB-serial driver plus a pty bridge, for kernels built
without ``CONFIG_USB_SERIAL_CH341`` (Arduino's Uno Q). The bridge exposes a
genuine ``/dev/pts/N``, so instrument drivers open it with unmodified
``serial.Serial`` and need no knowledge of any of this.

:py:mod:`benchctrl.transports.autoserial` decides *which* of the two to use —
kernel driver first, userspace only when the kernel bound nothing — so the
same config works on desktop Linux and on the Uno Q.
"""

from benchctrl.transports.autoserial import (
    SerialTarget,
    open_serial_driver,
    resolve_ch341_port,
)
from benchctrl.transports.ch341 import CH340_PID, CH340_VID, CH341Device
from benchctrl.transports.ptybridge import PtySerialBridge, ch341_present, open_ch341_pty

__all__ = [
    "CH340_PID",
    "CH340_VID",
    "CH341Device",
    "PtySerialBridge",
    "SerialTarget",
    "ch341_present",
    "open_ch341_pty",
    "open_serial_driver",
    "resolve_ch341_port",
]

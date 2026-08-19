"""Transports that make a device reachable when the kernel cannot.

Currently one member: a userspace CH341 USB-serial driver plus a pty bridge,
for kernels built without ``CONFIG_USB_SERIAL_CH341`` (Arduino's Uno Q). The
bridge exposes a genuine ``/dev/pts/N``, so instrument drivers open it with
unmodified ``serial.Serial`` and need no knowledge of any of this.
"""

from benchctrl.transports.ch341 import CH340_PID, CH340_VID, CH341Device
from benchctrl.transports.ptybridge import PtySerialBridge, open_ch341_pty

__all__ = [
    "CH340_PID",
    "CH340_VID",
    "CH341Device",
    "PtySerialBridge",
    "open_ch341_pty",
]

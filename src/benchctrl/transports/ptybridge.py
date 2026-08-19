"""Bridge a userspace USB-serial device to a real pty.

The payoff: instrument drivers keep using ``serial.Serial`` against a device
path. :py:class:`~benchctrl.drivers.eastwood_qr10x.QR10x` needs no change, no
injection seam, and no knowledge that its port is synthetic — the same
argument :py:mod:`benchctrl.sim.loopback` makes for simulators, reused here
for a real instrument behind a driverless bridge.

Layering note: this sits *below* the driver, at the same height as pyserial.
It does not import any driver, and no driver imports it.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from benchctrl.exceptions import BenchConnectionError
from benchctrl.sim.loopback import SerialLoopback
from benchctrl.transports.ch341 import CH341Device

log = logging.getLogger("benchctrl.transports.ptybridge")


class PtySerialBridge:
    """Pumps bytes between a pty and a userspace serial device.

    ``device`` needs only ``read(size)``, ``write(bytes)``, ``close()`` and
    ``is_open`` — :py:class:`~benchctrl.transports.ch341.CH341Device`
    satisfies that, and so would any future libusb bridge.
    """

    #: Poll interval for the pty side.
    #:
    #: This is a correctness constraint, not a tuning knob. Drivers that infer
    #: end-of-response from silence (the QR10x waits 60 ms) will truncate a
    #: multi-packet reply if the bridge stalls mid-response for long enough to
    #: look like the device went quiet.
    #:
    #: The margin is larger than the 60 ms window suggests, because pyserial's
    #: own blocking read timeout absorbs shorter gaps: a QR10x read uses
    #: ``timeout=0.2``, so truncation is measured to begin above ~200 ms, not
    #: above 60 ms (see tests/test_ch341.py). 5 ms leaves a wide margin against
    #: the stricter of the two and against scheduling jitter on a loaded board.
    _POLL_S = 0.005

    def __init__(self, device, *, name: str = "usb-serial") -> None:
        self._device = device
        self._name = name
        self._pty = SerialLoopback()
        self._stop = threading.Event()
        self._pump: Optional[threading.Thread] = None
        self._closed = False
        self.host_to_device_bytes = 0
        self.device_to_host_bytes = 0

    @property
    def port(self) -> str:
        """The device path to hand to ``serial.Serial`` (e.g. /dev/pts/3)."""
        return self._pty.port

    @property
    def is_open(self) -> bool:
        return not self._closed

    def start(self) -> "PtySerialBridge":
        if self._pump is not None:
            return self
        self._stop.clear()
        self._pump = threading.Thread(
            target=self._pump_loop, name=f"ptybridge-{self._name}", daemon=True
        )
        self._pump.start()
        log.info("ptybridge: %s available at %s", self._name, self.port)
        return self

    def _pump_loop(self) -> None:
        while not self._stop.is_set():
            moved = False

            # Host -> device. read() already blocks up to its timeout, which
            # is what paces this loop.
            try:
                out = self._pty.read(4096, timeout=self._POLL_S)
            except Exception as exc:  # noqa: BLE001
                log.warning("ptybridge: pty read failed: %s", exc)
                out = b""
            if out:
                try:
                    self._device.write(out)
                    self.host_to_device_bytes += len(out)
                    moved = True
                except Exception as exc:  # noqa: BLE001
                    # Propagating here would kill the pump and silently strand
                    # the pty; the driver's own timeout is the better signal.
                    log.warning("ptybridge: device write failed: %s", exc)

            # Device -> host.
            try:
                inbound = self._device.read(4096)
            except Exception as exc:  # noqa: BLE001
                log.warning("ptybridge: device read failed: %s", exc)
                inbound = b""
            if inbound:
                self._pty.write(inbound)
                self.device_to_host_bytes += len(inbound)
                moved = True

            if not moved:
                # Both directions idle: the pty read already waited _POLL_S,
                # so fall through without an extra sleep.
                continue

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._pump is not None:
            self._pump.join(timeout=1.0)
            self._pump = None
        try:
            self._device.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("ptybridge: closing the device failed: %s", exc)
        self._pty.close()

    def __enter__(self) -> "PtySerialBridge":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "open" if not self._closed else "closed"
        return f"<PtySerialBridge {self._name} {self.port} {state}>"


def open_ch341_pty(*, serial_number: Optional[str] = None, index: int = 0,
                   baudrate: int = 115200, bytesize: int = 8,
                   parity: str = "N", stopbits: int = 1) -> PtySerialBridge:
    """Open a CH340 and expose it as a pty, started and ready.

    ``bridge.port`` is then a device path any pyserial-based driver can open::

        bridge = open_ch341_pty(baudrate=115200)
        qr = QR10x.open(bridge.port)
    """
    device = CH341Device.open(
        serial_number=serial_number,
        index=index,
        baudrate=baudrate,
        bytesize=bytesize,
        parity=parity,
        stopbits=stopbits,
    )
    try:
        return PtySerialBridge(device, name="ch341").start()
    except Exception:
        # Don't leak a claimed USB device if the pty side fails to come up.
        device.close()
        raise


def ch341_present() -> bool:
    """Whether any CH340 is on this host's USB bus."""
    try:
        return bool(CH341Device.find_all())
    except BenchConnectionError:
        return False

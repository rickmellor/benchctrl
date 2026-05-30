"""Serial transport — the I/O boundary.

Owns the pyserial port. Provides discovery, open/close, raw read/write
with deadlines. Knows nothing about commands or framing.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Optional

import serial
import serial.tools.list_ports as list_ports

from benchctrl.exceptions import BenchConnectionError
from benchctrl.protocol import PID, VID


@dataclass(frozen=True)
class PortInfo:
    """Information about a discovered Arc serial port."""

    device: str  # OS-level path (COM6 / /dev/ttyACM0 / /dev/cu.usbmodemXXX)
    name: str  # vendor-friendly name (e.g. "Arc")
    serial_number: Optional[str]
    description: str

    def __str__(self) -> str:
        sn = f" sn={self.serial_number}" if self.serial_number else ""
        return f"{self.device} ({self.description}{sn})"


def discover_arc_ports() -> list[PortInfo]:
    """Find every Arc/Arc Pro currently enumerated as a CDC-ACM port."""
    found: list[PortInfo] = []
    for p in list_ports.comports():
        if p.vid == VID and p.pid == PID:
            found.append(
                PortInfo(
                    device=p.device,
                    name=getattr(p, "product", None) or "Arc",
                    serial_number=getattr(p, "serial_number", None),
                    description=p.description or "",
                )
            )
    return found


class Transport:
    """Thin pyserial wrapper.

    Not thread-safe. One thread owns one Transport instance.
    """

    def __init__(self, port: str, baudrate: int = 9600, open_timeout: float = 2.0):
        self.port = port
        self.baudrate = baudrate
        self.open_timeout = open_timeout
        self._ser: Optional[serial.Serial] = None

    # --- lifecycle -------------------------------------------------------

    def open(self) -> None:
        if self._ser is not None and self._ser.is_open:
            return
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.5,
                dsrdtr=False,
                rtscts=False,
                xonxoff=False,
            )
        except (OSError, serial.SerialException) as e:
            raise BenchConnectionError(f"could not open {self.port!r}: {e}") from e
        # Match the Otii vendor stack's posture
        self._ser.dtr = False
        self._ser.rts = False
        time.sleep(0.2)
        try:
            self._ser.reset_input_buffer()
        except Exception:
            # Some platforms throw here; non-fatal.
            pass

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    # --- I/O -------------------------------------------------------------

    def write(self, data: bytes) -> int:
        """Write `data` and flush. Returns bytes written."""
        if self._ser is None or not self._ser.is_open:
            raise BenchConnectionError("transport not open")
        try:
            n = self._ser.write(data)
            self._ser.flush()
            return n
        except (OSError, serial.SerialException) as e:
            raise BenchConnectionError(f"write failed: {e}") from e

    def read_chunk(self, max_size: int = 8192) -> bytes:
        """Read up to `max_size` bytes; returns immediately if none available
        within the underlying pyserial timeout (0.5 s)."""
        if self._ser is None or not self._ser.is_open:
            raise BenchConnectionError("transport not open")
        try:
            return bytes(self._ser.read(max_size))
        except (OSError, serial.SerialException) as e:
            raise BenchConnectionError(f"read failed: {e}") from e

    def read_for(self, seconds: float, max_chunk: int = 8192) -> bytes:
        """Drain the inbound stream for the next `seconds`."""
        if seconds <= 0:
            return b""
        buf = bytearray()
        t_end = time.monotonic() + seconds
        while time.monotonic() < t_end:
            chunk = self.read_chunk(max_chunk)
            if chunk:
                buf.extend(chunk)
        return bytes(buf)

    def __enter__(self) -> Transport:
        self.open()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

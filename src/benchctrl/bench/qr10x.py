"""Eastwood Tech QR10x programmable resistance substitution box driver.

The QR10x family (QR100 / QR101 — see ``QR10x_Spec_Manual_V3.1.pdf``) is a
relay-network programmable resistance, 1 Ω to 8.4 MΩ depending on model,
exposed over a CH340 USB-Serial adapter.

Wire protocol (per the manufacturer's spec):

- 115200 baud, 8 data bits, no parity, 1 stop bit
- Private AT command set, line-oriented
- Commands must be terminated with ``\\r`` or ``\\n``
- Responses come as one or more lines of ``+KEY=VALUE`` or ``+OK.``
- No documented end-of-response marker; we infer end-of-burst from a
  short quiet period after the last received byte.

Typical use::

    from benchctrl.bench import QR10x

    with QR10x.open("COM7") as qr:
        print(qr.info())
        qr.set_safety_limit(12.0)        # device-enforced minimum
        qr.set_resistance(10_000)        # 10 kΩ
        print(qr.actual_resistance())    # PV — what the device actually achieved

Safety
------
The QR10x has a built-in **safety output limit** (``RLIMIT``) that
clamps any setpoint at or above the configured value. The QR's rated
power is 1-2 W depending on output value, max 200 V across the
terminals. For a 3.2 V source (e.g. two AA cells in series), staying
above ~12 Ω keeps power below 1 W. Always set ``RLIMIT`` for the
voltage range you're working in.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional

import serial

log = logging.getLogger("benchctrl.bench.qr10x")


# ---------------------------------------------------------------------------
# Exception hierarchy (kept independent of benchctrl.exceptions to keep
# `benchctrl.bench` import-independent of the rest of benchctrl's surface)
# ---------------------------------------------------------------------------


class QR10xError(RuntimeError):
    """Base class for QR10x driver errors."""


class QR10xConnectionError(QR10xError):
    """Couldn't open the serial port, or lost connection mid-stream."""


class QR10xProtocolError(QR10xError):
    """Got an unexpected response shape from the device."""


class QR10xTimeoutError(QR10xError, TimeoutError):
    """No response within the timeout."""


class QR10xValueError(QR10xError, ValueError):
    """Client-side range check failed before sending."""


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class QR10xInfo:
    """Identity + static specs read once at connect time.

    Attributes:
        device_type: ordering code (e.g. ``"QR101B-AM-1R"``).
        serial: serial number (8-digit zero-padded).
        hardware_version: e.g. ``"5.1N"``.
        firmware_version: e.g. ``"5.963KS"``.
        production_date: YYYYMMDD.
        temperature_coefficient_ppm: TCR in ppm/°C (e.g. 25).
    """

    device_type: str
    serial: str
    hardware_version: str
    firmware_version: str
    production_date: str
    temperature_coefficient_ppm: Optional[int]

    def to_dict(self) -> dict:
        return {
            "device_type": self.device_type,
            "serial": self.serial,
            "hardware_version": self.hardware_version,
            "firmware_version": self.firmware_version,
            "production_date": self.production_date,
            "temperature_coefficient_ppm": self.temperature_coefficient_ppm,
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class QR10x:
    """A connected Eastwood QR10x programmable resistance.

    Drives the device over its CH340 USB-Serial port. Talks the
    documented private AT command set. Use as a context manager or call
    ``open()`` / ``close()`` explicitly.
    """

    # Heuristics for end-of-response detection.
    _EOR_QUIET_S = 0.06   # consider response done after 60 ms of silence
    _INITIAL_WAIT_S = 0.05  # wait at least this long after sending

    def __init__(self, port: str, baudrate: int = 115200):
        self.port = port
        self.baudrate = baudrate
        self._ser: Optional[serial.Serial] = None

    # ---- lifecycle ---------------------------------------------------

    @classmethod
    def open(cls, port: str, baudrate: int = 115200) -> "QR10x":
        """Open a connection to the QR10x at ``port``."""
        qr = cls(port, baudrate)
        qr._connect()
        return qr

    def _connect(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                dsrdtr=False,
                rtscts=False,
                xonxoff=False,
            )
        except (OSError, serial.SerialException) as e:
            raise QR10xConnectionError(
                f"could not open {self.port!r}: {e}"
            ) from e
        # Settle + drain anything the device may have buffered
        time.sleep(0.15)
        try:
            self._ser.reset_input_buffer()
        except Exception:
            pass

    def close(self) -> None:
        """Close the serial port. Safe to call repeatedly."""
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:
                pass
            self._ser = None

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def __enter__(self) -> "QR10x":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    # ---- low-level AT round-trip ------------------------------------

    def _cmd(self, command: str, *, timeout: float = 3.0) -> list[str]:
        """Send an AT command and return all response lines (stripped).

        The QR10x's protocol has no explicit end-of-response marker, so
        we wait for an initial response then collect bytes until the
        device goes quiet for :py:attr:`_EOR_QUIET_S` seconds.
        """
        if self._ser is None or not self._ser.is_open:
            raise QR10xConnectionError("not open")

        payload = (command.strip() + "\r\n").encode("ascii")
        try:
            self._ser.reset_input_buffer()
            self._ser.write(payload)
            self._ser.flush()
        except (OSError, serial.SerialException) as e:
            raise QR10xConnectionError(f"write failed: {e}") from e

        deadline = time.monotonic() + timeout
        buf = b""
        last_byte_t: Optional[float] = None
        time.sleep(self._INITIAL_WAIT_S)
        while time.monotonic() < deadline:
            try:
                chunk = self._ser.read(max(1, self._ser.in_waiting))
            except (OSError, serial.SerialException) as e:
                raise QR10xConnectionError(f"read failed: {e}") from e
            now = time.monotonic()
            if chunk:
                buf += chunk
                last_byte_t = now
            else:
                # No bytes this tick — if we have a response in flight,
                # check whether the quiet window has elapsed.
                if last_byte_t is not None and now - last_byte_t >= self._EOR_QUIET_S:
                    break
                time.sleep(0.01)

        if not buf:
            raise QR10xTimeoutError(
                f"no response to {command!r} within {timeout:.2f}s"
            )

        text = buf.decode("ascii", errors="replace")
        lines = [ln.strip() for ln in text.replace("\r", "\n").split("\n") if ln.strip()]
        log.debug("CMD %r -> %r", command, lines)
        return lines

    @staticmethod
    def _parse_kv(lines: list[str], key: str) -> Optional[str]:
        """Return the value of a ``+key=value`` line, else None."""
        prefix = f"+{key}="
        for line in lines:
            if line.startswith(prefix):
                return line[len(prefix):].strip()
        return None

    @staticmethod
    def _parse_state_dump(lines: list[str]) -> dict:
        """Parse a multi-line SET response (``+OK.`` + state lines)."""
        out: dict = {"ok": any(line.startswith("+OK") for line in lines)}
        for line in lines:
            line = line.strip()
            if "=" not in line or line.startswith("+OK"):
                continue
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip()
            try:
                out[key] = float(value)
            except ValueError:
                out[key] = value
        return out

    # ---- public API --------------------------------------------------

    def info(self) -> QR10xInfo:
        """Query the device identity + static specs (one round-trip per item)."""
        device_type = self._parse_kv(self._cmd("AT+DEV.TYPE?"), "DEV.TYPE") or ""
        serial_no = self._parse_kv(self._cmd("AT+DEV.SN?"), "DEV.SN") or ""
        hw = self._parse_kv(self._cmd("AT+DEV.HW?"), "DEV.HW") or ""
        fw = self._parse_kv(self._cmd("AT+DEV.FW?"), "DEV.FW") or ""
        prod = self._parse_kv(self._cmd("AT+DEV.PROD?"), "DEV.PROD") or ""
        tcr_raw = self._parse_kv(self._cmd("AT+DEV.TCR?"), "DEV.TCR")
        tcr = None
        if tcr_raw:
            try:
                tcr = int(tcr_raw)
            except ValueError:
                tcr = None
        return QR10xInfo(
            device_type=device_type,
            serial=serial_no,
            hardware_version=hw,
            firmware_version=fw,
            production_date=prod,
            temperature_coefficient_ppm=tcr,
        )

    def set_resistance(self, ohms: float) -> dict:
        """Set the resistance setpoint (Ω). Returns the device's state dump."""
        if not isinstance(ohms, (int, float)):
            raise QR10xValueError(f"ohms must be a number, got {type(ohms).__name__}")
        if ohms < 0:
            raise QR10xValueError(f"ohms must be >= 0, got {ohms}")
        return self._parse_state_dump(self._cmd(f"AT+USER.SP={ohms}"))

    def get_setpoint(self) -> float:
        """Read the current setpoint (Ω)."""
        v = self._parse_kv(self._cmd("AT+USER.SP?"), "USER.SP")
        return float(v) if v is not None else float("nan")

    def actual_resistance(self) -> float:
        """Read the actual achieved value (PV) in Ω.

        The QR10x snaps to discrete steps determined by its relay network;
        ``actual_resistance()`` returns the *real* value the device can
        produce, which may differ from ``get_setpoint()`` by up to one
        nominal step.
        """
        v = self._parse_kv(self._cmd("AT+USER.PV?"), "USER.PV")
        return float(v) if v is not None else float("nan")

    def get_safety_limit(self) -> float:
        """Read the device's safety minimum-resistance limit (Ω)."""
        v = self._parse_kv(self._cmd("AT+USER.RLIMIT?"), "USER.RLIMIT")
        return float(v) if v is not None else 0.0

    def set_safety_limit(self, ohms: float) -> dict:
        """Set the device's safety minimum-resistance limit (Ω).

        Any subsequent ``set_resistance()`` below this value is clamped
        device-side. Use this to enforce a power-rating safety boundary
        for the source you're connected to.
        """
        if not isinstance(ohms, (int, float)):
            raise QR10xValueError(f"ohms must be a number, got {type(ohms).__name__}")
        if ohms < 0:
            raise QR10xValueError(f"ohms must be >= 0, got {ohms}")
        return self._parse_state_dump(self._cmd(f"AT+USER.RLIMIT={ohms}"))

    def get_temperature(self) -> float:
        """Read the internal temperature sensor (°C)."""
        v = self._parse_kv(self._cmd("AT+USER.T_SENSOR?"), "USER.T_SENSOR")
        return float(v) if v is not None else float("nan")

    def incr(self, delta: float) -> dict:
        """Increase the setpoint by ``delta`` Ω."""
        if delta < 0:
            raise QR10xValueError(f"delta must be >= 0; use decr() to step down")
        return self._parse_state_dump(self._cmd(f"AT+USER.SP+={delta}"))

    def decr(self, delta: float) -> dict:
        """Decrease the setpoint by ``delta`` Ω."""
        if delta < 0:
            raise QR10xValueError(f"delta must be >= 0; use incr() to step up")
        return self._parse_state_dump(self._cmd(f"AT+USER.SP-={delta}"))

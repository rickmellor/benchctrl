"""A simulated Eastwood QR10x programmable resistor.

Line-oriented AT protocol over the same pty loopback the Arc simulator uses,
so the real :py:class:`~benchctrl.drivers.eastwood_qr10x.driver.QR10x` driver
drives it unmodified — including its end-of-response heuristic, which infers
"done" from 60 ms of silence rather than any terminator. That heuristic is
exactly the sort of thing an in-process mock cannot test and a pty can.

The device models a relay ladder: setpoints snap to the nearest achievable
step, so ``set_resistance(100)`` followed by ``actual_resistance()`` returns
the quantised value the hardware would actually land on, not the request.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from benchctrl.sim.base import SimDevice
from benchctrl.sim.loopback import SerialLoopback

log = logging.getLogger("benchctrl.sim.qr10x")

DEFAULT_IDENTITY = {
    "DEV.TYPE": "QR10X",
    "DEV.SN": "SIM-QR10X-0001",
    "DEV.HW": "1.0",
    "DEV.FW": "2.4.1",
    "DEV.PROD": "QR104",
    "DEV.TCR": "25",
}


class SimulatedQR10x(SimDevice):
    """A QR10x that answers AT commands from a pty.

    Args:
        step_ohm: relay ladder resolution. Setpoints quantise to this.
        min_ohm / max_ohm: the achievable range; requests outside it are
            clamped and reported with ``+ERR``.
        settle_s: modelled relay settling delay before ``USER.PV`` tracks
            ``USER.SP``. Zero means instantaneous.
    """

    def __init__(
        self,
        *,
        loopback: Optional[SerialLoopback] = None,
        step_ohm: float = 0.1,
        min_ohm: float = 0.1,
        max_ohm: float = 10_000.0,
        settle_s: float = 0.0,
        temperature_c: float = 26.5,
        identity: Optional[dict[str, str]] = None,
        free_run: bool = True,
    ) -> None:
        super().__init__(loopback=loopback, tick_hz=100.0, free_run=free_run)
        self._lock = threading.RLock()
        self._rx = bytearray()

        self.step_ohm = step_ohm
        self.min_ohm = min_ohm
        self.max_ohm = max_ohm
        self.settle_s = settle_s
        self.temperature_c = temperature_c
        self.identity = dict(DEFAULT_IDENTITY)
        if identity:
            self.identity.update(identity)

        self.setpoint_ohm = min_ohm
        self.actual_ohm = min_ohm
        self.safety_limit_ohm = max_ohm
        self.command_log: list[str] = []
        self._settle_at: Optional[float] = None
        #: When set, the next command answers ``+ERR=<text>``.
        self.force_next_error: Optional[str] = None

    # --- helpers --------------------------------------------------------

    def _quantise(self, ohms: float) -> float:
        if self.step_ohm <= 0:
            return ohms
        steps = round(ohms / self.step_ohm)
        return round(steps * self.step_ohm, 6)

    def _state_lines(self) -> list[str]:
        return [
            "+OK.",
            f"+USER.SP={self.setpoint_ohm:.3f}",
            f"+USER.PV={self.actual_ohm:.3f}",
            f"+USER.RLIMIT={self.safety_limit_ohm:.3f}",
        ]

    def _reply(self, lines: list[str]) -> None:
        self.send(("\r\n".join(lines) + "\r\n").encode("ascii"))

    def _apply_setpoint(self, ohms: float) -> list[str]:
        if ohms > self.safety_limit_ohm:
            return [f"+ERR=setpoint {ohms:.3f} exceeds RLIMIT {self.safety_limit_ohm:.3f}"]
        clamped = min(max(ohms, self.min_ohm), self.max_ohm)
        self.setpoint_ohm = self._quantise(clamped)
        if self.settle_s <= 0:
            self.actual_ohm = self.setpoint_ohm
            self._settle_at = None
        else:
            self._settle_at = self.elapsed_s + self.settle_s
        return self._state_lines()

    # --- inbound --------------------------------------------------------

    def on_frame_bytes(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)
            while b"\n" in self._rx:
                line, _, rest = bytes(self._rx).partition(b"\n")
                self._rx = bytearray(rest)
                text = line.decode("ascii", errors="replace").strip()
                if text:
                    self._handle_line(text)

    def _handle_line(self, line: str) -> None:
        self.command_log.append(line)

        if self.force_next_error is not None:
            msg = self.force_next_error
            self.force_next_error = None
            self._reply([f"+ERR={msg}"])
            return

        if not line.upper().startswith("AT+"):
            self._reply(["+ERR=bad command"])
            return

        body = line[3:]

        # Query: AT+KEY?
        if body.endswith("?"):
            key = body[:-1]
            self._reply(self._query(key))
            return

        # Increment / decrement: AT+USER.SP+=1.0 / AT+USER.SP-=1.0
        for op in ("+=", "-="):
            if op in body:
                key, _, raw = body.partition(op)
                try:
                    delta = float(raw)
                except ValueError:
                    self._reply(["+ERR=bad value"])
                    return
                if key != "USER.SP":
                    self._reply(["+ERR=not adjustable"])
                    return
                target = self.setpoint_ohm + (delta if op == "+=" else -delta)
                self._reply(self._apply_setpoint(target))
                return

        # Assignment: AT+KEY=value
        if "=" in body:
            key, _, raw = body.partition("=")
            try:
                value = float(raw)
            except ValueError:
                self._reply(["+ERR=bad value"])
                return
            if key == "USER.SP":
                self._reply(self._apply_setpoint(value))
                return
            if key == "USER.RLIMIT":
                self.safety_limit_ohm = value
                self._reply(self._state_lines())
                return
            self._reply(["+ERR=unknown parameter"])
            return

        self._reply(["+ERR=unknown command"])

    def _query(self, key: str) -> list[str]:
        if key in self.identity:
            return [f"+{key}={self.identity[key]}"]
        if key == "USER.SP":
            return [f"+USER.SP={self.setpoint_ohm:.3f}"]
        if key == "USER.PV":
            return [f"+USER.PV={self.actual_ohm:.3f}"]
        if key == "USER.RLIMIT":
            return [f"+USER.RLIMIT={self.safety_limit_ohm:.3f}"]
        if key == "USER.T_SENSOR":
            return [f"+USER.T_SENSOR={self.temperature_c:.2f}"]
        return ["+ERR=unknown parameter"]

    # --- tick -----------------------------------------------------------

    def on_tick(self, elapsed_s: float) -> None:
        if self._settle_at is not None and elapsed_s >= self._settle_at:
            self.actual_ohm = self.setpoint_ohm
            self._settle_at = None

    # --- fault injection ------------------------------------------------

    def inject_error(self, message: str = "simulated fault") -> None:
        """Make the next command answer ``+ERR=<message>``."""
        self.force_next_error = message

"""Common scaffolding for loopback-backed instrument simulators.

A simulator owns one :py:class:`~benchctrl.sim.loopback.SerialLoopback` and a
thread that alternates between draining client bytes and emitting whatever
the device would emit on its own (sample streams, unsolicited status).

Two drive modes, because tests want different things:

- **free-running** (default) — a thread ticks at ``tick_hz``, so timing-
  dependent driver code (the session-init sleeps, ``read_for`` windows, the
  reader thread) sees a device behaving in real time.
- **pumped** (``free_run=False``) — nothing happens until a test calls
  :py:meth:`pump`. Emission becomes exactly reproducible, so sample counts
  can be asserted precisely instead of approximately.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from benchctrl.sim.loopback import SerialLoopback

log = logging.getLogger("benchctrl.sim")


class SimDevice:
    """Base class for a simulated instrument behind a pty.

    Subclasses implement :py:meth:`on_frame_bytes` (client -> device) and
    optionally :py:meth:`on_tick` (device -> client, periodic).
    """

    def __init__(
        self,
        *,
        loopback: Optional[SerialLoopback] = None,
        tick_hz: float = 200.0,
        free_run: bool = True,
    ) -> None:
        self.link = loopback or SerialLoopback()
        self._owns_link = loopback is None
        self.tick_hz = tick_hz
        self.free_run = free_run
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._tx_lock = threading.Lock()
        self._t0 = time.monotonic()
        self._started = False

    # --- lifecycle ------------------------------------------------------

    @property
    def port(self) -> str:
        """Device path for the client to open."""
        return self.link.port

    @property
    def elapsed_s(self) -> float:
        """Seconds since :py:meth:`start`."""
        return time.monotonic() - self._t0

    def start(self) -> SimDevice:
        if self._started:
            return self
        self._started = True
        self._t0 = time.monotonic()
        self._stop.clear()
        if self.free_run:
            self._thread = threading.Thread(
                target=self._run,
                name=f"{type(self).__name__}-sim",
                daemon=True,
            )
            self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            if self._thread.is_alive():
                log.warning("%s sim thread did not stop within 2s", type(self).__name__)
            self._thread = None
        self._started = False

    def close(self) -> None:
        self.stop()
        if self._owns_link:
            self.link.close()

    def __enter__(self) -> SimDevice:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- drive ----------------------------------------------------------

    def _run(self) -> None:
        period = 1.0 / self.tick_hz if self.tick_hz > 0 else 0.005
        next_tick = time.monotonic()
        while not self._stop.is_set():
            # Read with a short timeout so we stay responsive to both
            # directions without spinning.
            data = self.link.read(8192, timeout=min(period, 0.02))
            if data:
                self._feed(data)
            now = time.monotonic()
            if now >= next_tick:
                try:
                    self.on_tick(now - self._t0)
                except Exception:  # pragma: no cover - simulator bug guard
                    log.exception("%s.on_tick raised", type(self).__name__)
                # Skip missed ticks rather than trying to catch up in a burst.
                next_tick = max(now, next_tick + period)
            self.link.flush()

    def pump(self, *, ticks: int = 1, read_first: bool = True) -> None:
        """Advance a pumped (``free_run=False``) simulator deterministically."""
        for _ in range(max(1, ticks)):
            if read_first:
                data = self.link.read(8192, timeout=0.01)
                if data:
                    self._feed(data)
            self.on_tick(self.elapsed_s)
            self.link.flush(timeout=0.05)

    def drain_input(self, timeout: float = 0.05) -> None:
        """Process any pending client bytes without emitting anything."""
        data = self.link.read(8192, timeout=timeout)
        if data:
            self._feed(data)

    # --- transmit -------------------------------------------------------

    def send(self, data: bytes) -> None:
        """Write raw bytes toward the client, serialized across threads."""
        with self._tx_lock:
            self.link.write(data)

    # --- subclass hooks -------------------------------------------------

    def _feed(self, data: bytes) -> None:
        """Route inbound bytes. Subclasses override :py:meth:`on_frame_bytes`."""
        self.on_frame_bytes(data)

    def on_frame_bytes(self, data: bytes) -> None:
        """Handle bytes written by the client."""
        raise NotImplementedError

    def on_tick(self, elapsed_s: float) -> None:
        """Emit anything the device produces on its own. Default: nothing."""
        return None

"""Pseudo-terminal loopback — a real serial port backed by a simulator.

The point of this module is fidelity. A pty pair gives us an honest
``/dev/pts/N`` device path that ``serial.Serial`` opens exactly the way it
opens a real Arc, which means an end-to-end test exercises:

- :py:class:`benchctrl.drivers.otii_arc.transport.Transport` (open flags,
  DTR/RTS posture, ``reset_input_buffer``, the 0.5 s read timeout)
- the framing in :py:mod:`benchctrl.drivers.otii_arc.protocol`
  (magic, length, checksum, resync-on-garbage)
- the timed session-init handshake in ``OtiiArc._connect``
- the background reader thread and its buffer demux

None of that is stubbed. The only thing that isn't real is the silicon.

Binary-safety note: a pty's line discipline will happily mangle binary data
(``\\r`` <-> ``\\n`` translation, echo, ``^C`` interpretation). We put the
slave side into raw mode before anyone opens it, which disables all of it.
"""

from __future__ import annotations

import errno
import logging
import os
import select
import termios
import tty
from typing import Optional

log = logging.getLogger("benchctrl.sim.loopback")

#: A pty's kernel buffer is small (typically 4 KB). Writes beyond it block
#: until the reader drains. We cap our own outbound queue well above that so
#: a briefly-stalled reader doesn't cost us samples, but a wedged one is
#: reported rather than silently swallowing data.
DEFAULT_MAX_TX_QUEUE = 1 << 20  # 1 MiB


class SerialLoopback:
    """A pty pair presenting a real serial device path.

    The simulator keeps the master end; the device path returned by
    :py:attr:`port` is the slave end, which is what pyserial opens.

    The slave fd is held open for the lifetime of this object so the pty
    survives a client closing and reopening the port — real hardware doesn't
    disappear when you close it either.

    Not thread-safe for concurrent :py:meth:`write` calls; the simulator
    threads in this package serialize their own writes.
    """

    def __init__(self, *, max_tx_queue: int = DEFAULT_MAX_TX_QUEUE) -> None:
        self._master_fd, self._slave_fd = os.openpty()
        self._port = os.ttyname(self._slave_fd)
        self._max_tx_queue = max_tx_queue
        self._txq = bytearray()
        self._closed = False
        self.overruns = 0  # bytes dropped because the client stopped reading

        # Raw mode on the slave — no echo, no CR/LF translation, no signal
        # characters. Without this, any 0x0D in a binary frame becomes 0x0A
        # and every checksum fails in a way that looks like a protocol bug.
        tty.setraw(self._slave_fd)
        # Belt and braces: some platforms leave ECHO set after setraw.
        attrs = termios.tcgetattr(self._slave_fd)
        attrs[3] &= ~(termios.ECHO | termios.ECHONL | termios.ICANON | termios.ISIG)
        termios.tcsetattr(self._slave_fd, termios.TCSANOW, attrs)

        os.set_blocking(self._master_fd, False)

    @property
    def port(self) -> str:
        """The device path a client should open (e.g. ``/dev/pts/7``)."""
        return self._port

    @property
    def is_open(self) -> bool:
        return not self._closed

    # --- I/O ------------------------------------------------------------

    def read(self, max_bytes: int = 8192, timeout: float = 0.05) -> bytes:
        """Read what the client has written, waiting up to ``timeout``.

        Returns an empty bytes object on timeout — never raises on an idle
        line, which keeps simulator loops simple.
        """
        if self._closed:
            return b""
        try:
            ready, _, _ = select.select([self._master_fd], [], [], timeout)
        except (OSError, ValueError):
            return b""
        if not ready:
            return b""
        try:
            return os.read(self._master_fd, max_bytes)
        except BlockingIOError:
            return b""
        except OSError as exc:
            # EIO on Linux means the slave side went away mid-read. Treat it
            # as "no data" rather than an error: the client closing its port
            # is a normal event, not a fault.
            if exc.errno in (errno.EIO, errno.EBADF):
                return b""
            raise

    def write(self, data: bytes) -> int:
        """Queue ``data`` toward the client and flush as much as will fit.

        The pty buffer is small, so this queues internally and drains on
        every call plus every :py:meth:`flush`. Returns the number of bytes
        accepted into the queue (which is all of them, unless the queue is
        full and we had to drop).
        """
        if self._closed:
            return 0
        self._txq.extend(data)
        if len(self._txq) > self._max_tx_queue:
            dropped = len(self._txq) - self._max_tx_queue
            del self._txq[:dropped]
            self.overruns += dropped
            log.warning(
                "loopback tx queue full — dropped %d bytes (total %d). "
                "The client is not reading fast enough.",
                dropped,
                self.overruns,
            )
        self.flush()
        return len(data)

    def flush(self, timeout: float = 0.0) -> int:
        """Push queued bytes into the pty. Returns bytes actually written."""
        if self._closed or not self._txq:
            return 0
        written = 0
        while self._txq:
            try:
                _, ready, _ = select.select([], [self._master_fd], [], timeout)
            except (OSError, ValueError):
                break
            if not ready:
                break
            try:
                n = os.write(self._master_fd, bytes(self._txq))
            except BlockingIOError:
                break
            except OSError as exc:
                if exc.errno in (errno.EIO, errno.EBADF):
                    break
                raise
            if n <= 0:
                break
            del self._txq[:n]
            written += n
        return written

    @property
    def pending_tx(self) -> int:
        """Bytes queued but not yet accepted by the pty."""
        return len(self._txq)

    # --- lifecycle ------------------------------------------------------

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        for fd in (self._master_fd, self._slave_fd):
            try:
                os.close(fd)
            except OSError:
                pass

    def __enter__(self) -> SerialLoopback:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "open" if not self._closed else "closed"
        return f"<SerialLoopback {self._port} {state}>"


def open_loopback(max_tx_queue: Optional[int] = None) -> SerialLoopback:
    """Convenience constructor mirroring the repo's ``open()`` classmethods."""
    if max_tx_queue is None:
        return SerialLoopback()
    return SerialLoopback(max_tx_queue=max_tx_queue)

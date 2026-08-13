"""Wire framing: ``[u8 type][u32 length BE][payload]``.

Length-prefixed rather than newline-delimited for two reasons. Binary rides
natively — a 130 MB ``.opensmu`` blob would cost 33 % inflation as base64,
and recordings are the largest thing that crosses this link. And blob chunks
interleave with control frames on one socket, so a large transfer never
blocks a heartbeat or a safety event behind it.

One connection carries everything. A second port would be a second auth
surface and a second firewall rule for no gain.
"""

from __future__ import annotations

import socket
import struct
import threading
from typing import Optional

from benchctrl.exceptions import BenchConnectionError, BenchProtocolError

HEADER = struct.Struct(">BI")
HEADER_SIZE = HEADER.size  # 5

#: Refuse absurd frames rather than trying to allocate them. Blob payloads
#: are chunked well below this; anything larger is a desync or an attack.
MAX_FRAME_BYTES = 32 * 1024 * 1024


class FrameType:
    """Frame type tags. Values are wire-stable — never renumber."""

    REQ = 0x01
    RSP = 0x02
    ERR = 0x03
    EVT = 0x04
    BLOB_HDR = 0x05
    BLOB_CHUNK = 0x06
    BLOB_END = 0x07
    SAMPLES = 0x08
    PING = 0x09
    PONG = 0x0A
    HELLO = 0x0B
    CHALLENGE = 0x0C
    AUTH = 0x0D
    WELCOME = 0x0E
    CANCEL = 0x0F


NAMES = {v: k for k, v in vars(FrameType).items() if isinstance(v, int)}


def name_of(frame_type: int) -> str:
    return NAMES.get(frame_type, f"0x{frame_type:02X}")


def encode(frame_type: int, payload: bytes = b"") -> bytes:
    """Frame ``payload``. Raises if it exceeds :py:data:`MAX_FRAME_BYTES`."""
    if len(payload) > MAX_FRAME_BYTES:
        raise BenchProtocolError(
            f"frame payload too large: {len(payload)} > {MAX_FRAME_BYTES}"
        )
    return HEADER.pack(frame_type, len(payload)) + payload


class FrameWriter:
    """Serializes whole frames onto a socket.

    Every frame is written under one lock so a blob chunk can never be
    interleaved mid-frame with a heartbeat from another thread. Partial
    frames on the wire are unrecoverable — this lock is the thing that
    prevents them.
    """

    def __init__(self, sock: socket.socket) -> None:
        self._sock = sock
        self._lock = threading.Lock()
        self._closed = False

    def send(self, frame_type: int, payload: bytes = b"") -> None:
        data = encode(frame_type, payload)
        with self._lock:
            if self._closed:
                raise BenchConnectionError("connection closed")
            try:
                self._sock.sendall(data)
            except OSError as exc:
                self._closed = True
                raise BenchConnectionError(f"send failed: {exc}") from exc

    def close(self) -> None:
        with self._lock:
            self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed


class FrameReader:
    """Reassembles frames from a stream socket.

    Owns its buffer, so one reader per socket and one thread per reader.
    """

    def __init__(self, sock: socket.socket, *, max_frame: int = MAX_FRAME_BYTES) -> None:
        self._sock = sock
        self._buf = bytearray()
        self._max_frame = max_frame
        self._eof = False

    def read_frame(self, timeout: Optional[float] = None) -> Optional[tuple[int, bytes]]:
        """Return the next ``(type, payload)``, or None on clean EOF.

        Raises:
            BenchConnectionError: on socket error or a timeout with a
                partially-received frame (which cannot be resumed).
            BenchProtocolError: on an oversized length field.
        """
        self._sock.settimeout(timeout)
        while True:
            frame = self._take()
            if frame is not None:
                return frame
            if self._eof:
                if self._buf:
                    raise BenchConnectionError(
                        f"connection closed mid-frame ({len(self._buf)} bytes pending)"
                    )
                return None
            try:
                chunk = self._sock.recv(65536)
            except socket.timeout:
                raise BenchConnectionError("timed out waiting for a frame") from None
            except OSError as exc:
                raise BenchConnectionError(f"recv failed: {exc}") from exc
            if not chunk:
                self._eof = True
                continue
            self._buf.extend(chunk)

    def _take(self) -> Optional[tuple[int, bytes]]:
        if len(self._buf) < HEADER_SIZE:
            return None
        frame_type, length = HEADER.unpack_from(self._buf, 0)
        if length > self._max_frame:
            raise BenchProtocolError(
                f"declared frame length {length} exceeds limit {self._max_frame} "
                f"(type {name_of(frame_type)}) — stream desync or hostile peer"
            )
        end = HEADER_SIZE + length
        if len(self._buf) < end:
            return None
        payload = bytes(self._buf[HEADER_SIZE:end])
        del self._buf[:end]
        return frame_type, payload

    @property
    def pending_bytes(self) -> int:
        return len(self._buf)


def iter_frames(data: bytes) -> list[tuple[int, bytes]]:
    """Decode every complete frame in ``data``. For tests and diagnostics."""
    out: list[tuple[int, bytes]] = []
    i = 0
    while i + HEADER_SIZE <= len(data):
        frame_type, length = HEADER.unpack_from(data, i)
        end = i + HEADER_SIZE + length
        if end > len(data):
            break
        out.append((frame_type, bytes(data[i + HEADER_SIZE : end])))
        i = end
    return out

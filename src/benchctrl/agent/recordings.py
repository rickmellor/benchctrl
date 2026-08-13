"""Agent-side handle tables for recordings and streaming iterators.

A ``Recording`` is not a value — it is a live object that fills over time,
and its samples stay on the bench until someone asks for them. So the client
gets a handle, and the bytes move only on stop (or on demand).

Why ``.opensmu`` is the transfer format: it already exists, it is
float32-packed at 4 B/sample against ~20 B for JSON floats, it round-trips
by channel *code* so there is no enum-identity problem across the wire, and
``Recording.load_from_stream`` is already its exact inverse. One format
serves the file on disk, the wire, the run engine's chunks, and the
analyzer.

The memory ceiling is real and shapes the API. ``ChannelBuffer.values`` is a
Python ``list[float]`` at roughly 40 bytes per sample. At 9 ksps
(mc+mp at 4 kHz, mv at 1 kHz) that is ~110 MB for five minutes and ~1.3 GB
for an hour, on a board with 3.6 GB total and an LLM holding 1.1 GB of it.
Hence :py:data:`DEFAULT_MAX_RECORDING_S`: longer captures must go through
the run engine, which chunks.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Iterator, Optional

from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.agent.recordings")

#: Hard cap on a single ``record()`` call, from the board's RAM budget.
#: ~500 MB of usable headroom / ~40 B per sample / 9 ksps ~= 22 minutes;
#: 5 minutes leaves a wide margin and matches the run engine's chunk size.
DEFAULT_MAX_RECORDING_S = 300.0

#: Decimated preview rate for progress events during a long recording.
#: 20 Hz on one channel is ~200 B/s instead of 36 KB/s for the real stream.
PREVIEW_HZ = 20.0


@dataclass
class RecordingHandle:
    """One in-flight or completed recording."""

    rec_id: str
    device_key: str
    recording: Any
    name: str
    started_at: float
    session_id: Optional[str] = None
    stopped_at: Optional[float] = None
    blob_id: Optional[str] = None
    sha256: Optional[str] = None
    size: Optional[int] = None

    @property
    def is_running(self) -> bool:
        return self.stopped_at is None

    @property
    def elapsed_s(self) -> float:
        end = self.stopped_at if self.stopped_at is not None else time.monotonic()
        return end - self.started_at

    def to_dict(self) -> dict:
        d = {
            "rec_id": self.rec_id,
            "device": self.device_key,
            "name": self.name,
            "running": self.is_running,
            "elapsed_s": round(self.elapsed_s, 3),
            "channels": [ch.code for ch in getattr(self.recording, "channels", [])],
        }
        if self.blob_id:
            d.update({"blob": self.blob_id, "sha256": self.sha256, "bytes": self.size})
        return d

    def counts(self) -> dict[str, int]:
        return {
            ch.code: len(self.recording.buffer(ch).values)
            for ch in getattr(self.recording, "channels", [])
        }

    def preview(self, channel_code: Optional[str] = None, points: int = 40) -> dict:
        """A decimated tail of one channel, for progress events."""
        channels = list(getattr(self.recording, "channels", []))
        if not channels:
            return {}
        target = channels[0]
        if channel_code:
            for ch in channels:
                if ch.code == channel_code:
                    target = ch
                    break
        values = self.recording.buffer(target).values
        if not values:
            return {"channel": target.code, "values": []}
        step = max(1, len(values) // points)
        return {
            "channel": target.code,
            "values": [round(v, 9) for v in values[::step][-points:]],
            "count": len(values),
        }


class RecordingTable:
    """Tracks recordings the agent holds on behalf of clients."""

    def __init__(self, *, max_recording_s: float = DEFAULT_MAX_RECORDING_S) -> None:
        self._handles: dict[str, RecordingHandle] = {}
        self._by_device: dict[str, str] = {}
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        self.max_recording_s = max_recording_s

    def check_duration(self, seconds: Optional[float]) -> None:
        """Refuse a capture that would exhaust the board's RAM."""
        if seconds is None:
            return
        if seconds > self.max_recording_s:
            raise BenchValueError(
                f"requested {seconds:.0f}s exceeds max_recording_s "
                f"({self.max_recording_s:.0f}s). A single in-memory recording "
                f"is bounded by board RAM (~40 bytes/sample); use the run "
                f"engine, which chunks to disk, for longer captures."
            )

    def start(
        self,
        device_key: str,
        recording: Any,
        *,
        name: str = "recording",
        session_id: Optional[str] = None,
    ) -> RecordingHandle:
        with self._lock:
            existing = self._by_device.get(device_key)
            if existing is not None and self._handles[existing].is_running:
                raise BenchValueError(
                    f"{device_key} already has recording {existing} in progress"
                )
            rec_id = f"r-{next(self._counter)}"
            handle = RecordingHandle(
                rec_id=rec_id,
                device_key=device_key,
                recording=recording,
                name=name,
                started_at=time.monotonic(),
                session_id=session_id,
            )
            self._handles[rec_id] = handle
            self._by_device[device_key] = rec_id
            return handle

    def get(self, rec_id: str) -> RecordingHandle:
        with self._lock:
            handle = self._handles.get(rec_id)
        if handle is None:
            raise BenchValueError(f"unknown recording {rec_id!r}")
        return handle

    def for_device(self, device_key: str) -> Optional[RecordingHandle]:
        with self._lock:
            rec_id = self._by_device.get(device_key)
            return self._handles.get(rec_id) if rec_id else None

    def finish(
        self, rec_id: str, *, blob_id: str, sha256: str, size: int
    ) -> RecordingHandle:
        handle = self.get(rec_id)
        with self._lock:
            handle.stopped_at = time.monotonic()
            handle.blob_id = blob_id
            handle.sha256 = sha256
            handle.size = size
            self._by_device.pop(handle.device_key, None)
        return handle

    def release(self, rec_id: str) -> bool:
        with self._lock:
            handle = self._handles.pop(rec_id, None)
            if handle is not None:
                self._by_device.pop(handle.device_key, None)
        return handle is not None

    def active(self) -> list[RecordingHandle]:
        with self._lock:
            return [h for h in self._handles.values() if h.is_running]

    def describe(self) -> list[dict]:
        with self._lock:
            return [h.to_dict() for h in self._handles.values()]


@dataclass
class IteratorHandle:
    """A streaming generator running on the agent."""

    iter_id: int
    device_key: str
    generator: Iterator
    session_id: Optional[str] = None
    delivered: int = 0
    exhausted: bool = False
    error: Optional[str] = None
    _lock: threading.Lock = field(default_factory=threading.Lock)

    def take(self, max_items: int) -> list:
        """Pull up to ``max_items``, marking exhaustion."""
        out = []
        with self._lock:
            if self.exhausted:
                return out
            for _ in range(max_items):
                try:
                    out.append(next(self.generator))
                except StopIteration:
                    self.exhausted = True
                    break
                except Exception as exc:  # noqa: BLE001
                    self.exhausted = True
                    self.error = repr(exc)
                    log.warning("iterator %d failed: %r", self.iter_id, exc)
                    break
            self.delivered += len(out)
        return out

    def close(self) -> None:
        with self._lock:
            self.exhausted = True
        closer = getattr(self.generator, "close", None)
        if callable(closer):
            try:
                closer()
            except Exception:  # noqa: BLE001
                pass


class IteratorTable:
    """Tracks live streaming generators."""

    def __init__(self) -> None:
        self._handles: dict[int, IteratorHandle] = {}
        self._lock = threading.RLock()
        self._counter = itertools.count(1)

    def register(
        self, device_key: str, generator: Iterator, *, session_id: Optional[str] = None
    ) -> IteratorHandle:
        with self._lock:
            iter_id = next(self._counter)
            handle = IteratorHandle(
                iter_id=iter_id,
                device_key=device_key,
                generator=generator,
                session_id=session_id,
            )
            self._handles[iter_id] = handle
            return handle

    def get(self, iter_id: int) -> IteratorHandle:
        with self._lock:
            handle = self._handles.get(iter_id)
        if handle is None:
            raise BenchValueError(f"unknown iterator {iter_id!r}")
        return handle

    def close(self, iter_id: int) -> bool:
        with self._lock:
            handle = self._handles.pop(iter_id, None)
        if handle is None:
            return False
        handle.close()
        return True

    def close_all(self) -> None:
        with self._lock:
            ids = list(self._handles)
        for iter_id in ids:
            self.close(iter_id)

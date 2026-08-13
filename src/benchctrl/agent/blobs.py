"""Blob store for payloads too large to inline.

Recordings are the reason this exists. A 5-minute capture at full Arc rates
is ~11 MB of ``.opensmu``; an hour is ~130 MB. Those cannot ride inside a
JSON response, and they cannot sit in RAM indefinitely on a board with
3.6 GB total and an LLM already holding 1.1 GB of it.

So blobs spill to disk past a threshold, and disk means ``/home/arduino``
— the root partition has under 2 GB free and is shared with Docker images
and App Lab's models. Filling it would take the whole board down, not just
this transfer.

Content is addressed by SHA-256 as well as by id, so a re-fetch after a
dropped connection is idempotent and a truncated transfer is detectable.
"""

from __future__ import annotations

import hashlib
import itertools
import logging
import os
import tempfile
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.agent.blobs")

#: Keep blobs this size or smaller in RAM; spill anything larger.
DEFAULT_SPILL_THRESHOLD = 4 * 1024 * 1024

#: Chunk size on the wire. Small enough that a 130 MB transfer never blocks
#: a heartbeat or a safety event behind it for long.
CHUNK_BYTES = 64 * 1024

#: Blobs nobody fetched are dropped after this long.
DEFAULT_TTL_S = 3600.0


@dataclass
class BlobInfo:
    blob_id: str
    size: int
    sha256: str
    created_at: float
    content_type: str = "application/octet-stream"
    path: Optional[Path] = None

    @property
    def on_disk(self) -> bool:
        return self.path is not None

    def to_dict(self) -> dict:
        return {
            "blob": self.blob_id,
            "len": self.size,
            "sha256": self.sha256,
            "ct": self.content_type,
        }


class BlobStore:
    """Stores blobs in RAM, spilling large ones to disk."""

    def __init__(
        self,
        *,
        spill_dir: Optional[Path] = None,
        spill_threshold: int = DEFAULT_SPILL_THRESHOLD,
        ttl_s: float = DEFAULT_TTL_S,
        max_total_bytes: int = 512 * 1024 * 1024,
    ) -> None:
        self._mem: dict[str, bytes] = {}
        self._info: dict[str, BlobInfo] = {}
        self._lock = threading.RLock()
        self._counter = itertools.count(1)
        self._spill_dir = Path(spill_dir) if spill_dir else None
        self._spill_threshold = spill_threshold
        self._ttl_s = ttl_s
        self._max_total_bytes = max_total_bytes
        self._owned_dir: Optional[tempfile.TemporaryDirectory] = None

    def _dir(self) -> Path:
        if self._spill_dir is not None:
            self._spill_dir.mkdir(parents=True, exist_ok=True)
            return self._spill_dir
        if self._owned_dir is None:
            self._owned_dir = tempfile.TemporaryDirectory(prefix="benchctrl-blobs-")
        return Path(self._owned_dir.name)

    # --- writing --------------------------------------------------------

    def put(
        self, data: bytes, *, content_type: str = "application/octet-stream"
    ) -> BlobInfo:
        """Store ``data`` and return its descriptor."""
        digest = hashlib.sha256(data).hexdigest()
        blob_id = f"b-{next(self._counter)}"
        info = BlobInfo(
            blob_id=blob_id,
            size=len(data),
            sha256=digest,
            created_at=time.monotonic(),
            content_type=content_type,
        )
        with self._lock:
            self.evict_expired()
            self._enforce_budget(len(data))
            if len(data) > self._spill_threshold:
                path = self._dir() / f"{blob_id}.bin"
                path.write_bytes(data)
                info.path = path
                log.debug("blobs: spilled %s (%d bytes) to %s", blob_id, len(data), path)
            else:
                self._mem[blob_id] = data
            self._info[blob_id] = info
        return info

    def put_stream(self, source, *, content_type: str = "application/octet-stream") -> BlobInfo:
        """Store from a file-like object without holding two copies."""
        return self.put(source.read(), content_type=content_type)

    # --- reading --------------------------------------------------------

    def info(self, blob_id: str) -> BlobInfo:
        with self._lock:
            info = self._info.get(blob_id)
        if info is None:
            raise BenchValueError(
                f"unknown blob {blob_id!r} — it may have expired; re-run the "
                f"operation that produced it"
            )
        return info

    def get(self, blob_id: str) -> bytes:
        info = self.info(blob_id)
        if info.path is not None:
            return info.path.read_bytes()
        with self._lock:
            data = self._mem.get(blob_id)
        if data is None:  # pragma: no cover - info/mem kept in step
            raise BenchValueError(f"blob {blob_id!r} vanished")
        return data

    def iter_chunks(self, blob_id: str, chunk_bytes: int = CHUNK_BYTES) -> Iterator[bytes]:
        """Yield the blob in wire-sized pieces."""
        info = self.info(blob_id)
        if info.path is not None:
            with info.path.open("rb") as fh:
                while True:
                    chunk = fh.read(chunk_bytes)
                    if not chunk:
                        return
                    yield chunk
        else:
            data = self.get(blob_id)
            for i in range(0, len(data), chunk_bytes):
                yield data[i : i + chunk_bytes]

    # --- lifecycle ------------------------------------------------------

    def release(self, blob_id: str) -> bool:
        with self._lock:
            info = self._info.pop(blob_id, None)
            self._mem.pop(blob_id, None)
        if info is None:
            return False
        if info.path is not None:
            try:
                info.path.unlink()
            except OSError:
                pass
        return True

    def evict_expired(self, now: Optional[float] = None) -> int:
        now = time.monotonic() if now is None else now
        with self._lock:
            stale = [
                bid
                for bid, info in self._info.items()
                if now - info.created_at > self._ttl_s
            ]
        for bid in stale:
            self.release(bid)
        if stale:
            log.debug("blobs: evicted %d expired", len(stale))
        return len(stale)

    def _enforce_budget(self, incoming: int) -> None:
        """Drop oldest blobs until ``incoming`` fits inside the budget."""
        while True:
            with self._lock:
                total = sum(i.size for i in self._info.values())
                if total + incoming <= self._max_total_bytes or not self._info:
                    return
                oldest = min(self._info.values(), key=lambda i: i.created_at)
            log.warning(
                "blobs: budget exceeded (%d + %d > %d) — dropping oldest blob %s",
                total,
                incoming,
                self._max_total_bytes,
                oldest.blob_id,
            )
            self.release(oldest.blob_id)

    @property
    def total_bytes(self) -> int:
        with self._lock:
            return sum(i.size for i in self._info.values())

    @property
    def count(self) -> int:
        with self._lock:
            return len(self._info)

    def stats(self) -> dict:
        with self._lock:
            return {
                "count": len(self._info),
                "total_bytes": sum(i.size for i in self._info.values()),
                "on_disk": sum(1 for i in self._info.values() if i.on_disk),
                "budget_bytes": self._max_total_bytes,
            }

    def close(self) -> None:
        with self._lock:
            ids = list(self._info)
        for bid in ids:
            self.release(bid)
        if self._owned_dir is not None:
            self._owned_dir.cleanup()
            self._owned_dir = None


def default_spill_dir() -> Optional[Path]:
    """``/home/arduino`` on the board; the system temp dir elsewhere.

    The root partition on an Uno Q has under 2 GB free and is shared with
    Docker and App Lab's models. Spilling recordings there would fill it.
    """
    candidate = Path("/home/arduino")
    if candidate.is_dir() and os.access(candidate, os.W_OK):
        return candidate / "benchctrl" / "blobs"
    return None

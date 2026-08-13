"""One owning thread per device, with a priority lane for safety.

``Transport`` documents itself as not thread-safe: "One thread owns one
Transport instance." Locally that holds because the MCP server assumes a
single client. An agent serving the network breaks that assumption the
moment two sessions connect, so the invariant has to be restored
deliberately rather than hoped for.

Every call for a device is queued to the thread that owns it. Three
consequences:

- the local invariant holds again, unchanged, with the driver none the wiser
- multiple sessions become safe without touching driver code
- the safety governor gets a lane that jumps the queue, which is the whole
  point — a deadman trip that waits behind a 5-second measurement is not a
  deadman

Blocking calls are clamped server-side (see ``max_blocking_s``) so the
priority lane is never starved for longer than that bound.
"""

from __future__ import annotations

import heapq
import itertools
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from benchctrl.exceptions import BenchError, BenchTimeoutError, BenchValueError

log = logging.getLogger("benchctrl.agent.worker")

PRIORITY_SAFETY = 0
PRIORITY_NORMAL = 10

#: Bound on how long a single queued call may occupy the worker. Long
#: driver calls (read_raw, read_window) are clamped to this and looped by
#: the dispatch layer, so a safety trip never waits longer.
DEFAULT_MAX_BLOCKING_S = 5.0

#: Bounded queue: an unbounded one turns a stuck device into unbounded
#: memory growth, and the caller would rather be told than wait forever.
DEFAULT_QUEUE_LIMIT = 256


class WorkerBusy(BenchError):
    """The device's queue is full."""


class WorkerStopped(BenchError):
    """The worker was shut down while this call was queued."""


@dataclass(order=True)
class _Job:
    priority: int
    seq: int
    fn: Callable[[], Any] = field(compare=False)
    label: str = field(compare=False, default="")
    done: threading.Event = field(compare=False, default_factory=threading.Event)
    result: Any = field(compare=False, default=None)
    error: Optional[BaseException] = field(compare=False, default=None)
    started: bool = field(compare=False, default=False)
    cancelled: bool = field(compare=False, default=False)


class DeviceWorker:
    """Serializes all access to one device onto a single thread."""

    def __init__(
        self,
        device_key: str,
        *,
        queue_limit: int = DEFAULT_QUEUE_LIMIT,
        max_blocking_s: float = DEFAULT_MAX_BLOCKING_S,
    ) -> None:
        self.device_key = device_key
        self.max_blocking_s = max_blocking_s
        self._queue_limit = queue_limit
        self._heap: list[_Job] = []
        self._counter = itertools.count()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._current: Optional[_Job] = None
        self.calls_served = 0
        self.calls_rejected = 0

    # --- lifecycle ------------------------------------------------------

    def start(self) -> DeviceWorker:
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"worker-{self.device_key}", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        with self._wake:
            self._wake.notify_all()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            if self._thread.is_alive():
                log.warning(
                    "worker %s did not stop within %.1fs — a driver call is "
                    "wedged; the device may be left armed",
                    self.device_key,
                    timeout,
                )
            self._thread = None
        # Fail anything still queued rather than leaving callers hanging.
        with self._lock:
            pending, self._heap = self._heap, []
        for job in pending:
            job.error = WorkerStopped(f"worker {self.device_key} stopped")
            job.done.set()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    @property
    def depth(self) -> int:
        with self._lock:
            return len(self._heap)

    @property
    def busy_with(self) -> Optional[str]:
        job = self._current
        return job.label if job is not None else None

    # --- submission -----------------------------------------------------

    def submit(
        self,
        fn: Callable[[], Any],
        *,
        priority: int = PRIORITY_NORMAL,
        timeout: Optional[float] = None,
        label: str = "",
    ) -> Any:
        """Run ``fn`` on the owning thread and return its result.

        Raises whatever ``fn`` raises, with the traceback preserved.
        """
        job = self._enqueue(fn, priority=priority, label=label)
        wait = timeout if timeout is not None else self.max_blocking_s * 4
        if not job.done.wait(timeout=wait):
            job.cancelled = True
            raise BenchTimeoutError(
                f"{self.device_key}: {label or 'call'} did not complete within "
                f"{wait:.1f}s (queue depth {self.depth}, busy with "
                f"{self.busy_with!r})"
            )
        if job.error is not None:
            raise job.error
        return job.result

    def submit_nowait(
        self, fn: Callable[[], Any], *, priority: int = PRIORITY_NORMAL, label: str = ""
    ) -> _Job:
        """Queue ``fn`` without waiting. Used by the safety governor."""
        return self._enqueue(fn, priority=priority, label=label)

    def _enqueue(self, fn, *, priority: int, label: str) -> _Job:
        job = _Job(priority=priority, seq=next(self._counter), fn=fn, label=label)
        with self._wake:
            if self._stop.is_set():
                raise WorkerStopped(f"worker {self.device_key} is stopped")
            # Safety work is never rejected for queue pressure — a full
            # queue is exactly when a trip matters most.
            if priority > PRIORITY_SAFETY and len(self._heap) >= self._queue_limit:
                self.calls_rejected += 1
                raise WorkerBusy(
                    f"{self.device_key}: queue full ({self._queue_limit}); "
                    f"the device is not keeping up"
                )
            heapq.heappush(self._heap, job)
            self._wake.notify()
        return job

    # --- the owning thread ----------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            with self._wake:
                while not self._heap and not self._stop.is_set():
                    self._wake.wait(timeout=0.25)
                if self._stop.is_set():
                    return
                job = heapq.heappop(self._heap)
                self._current = job
            if job.cancelled:
                self._current = None
                continue
            job.started = True
            started_at = time.monotonic()
            try:
                job.result = job.fn()
            except BaseException as exc:  # noqa: BLE001 - relayed to the caller
                job.error = exc
            finally:
                elapsed = time.monotonic() - started_at
                if elapsed > self.max_blocking_s * 1.5:
                    log.warning(
                        "worker %s: %s occupied the device for %.1fs "
                        "(limit %.1fs) — safety response was delayed",
                        self.device_key,
                        job.label or "call",
                        elapsed,
                        self.max_blocking_s,
                    )
                self.calls_served += 1
                self._current = None
                job.done.set()


class WorkerPool:
    """One :py:class:`DeviceWorker` per device key."""

    def __init__(self, **worker_kwargs) -> None:
        self._workers: dict[str, DeviceWorker] = {}
        self._lock = threading.RLock()
        self._kwargs = worker_kwargs

    def get(self, device_key: str) -> DeviceWorker:
        with self._lock:
            worker = self._workers.get(device_key)
            if worker is None:
                worker = DeviceWorker(device_key, **self._kwargs).start()
                self._workers[device_key] = worker
            return worker

    def drop(self, device_key: str, timeout: float = 5.0) -> None:
        with self._lock:
            worker = self._workers.pop(device_key, None)
        if worker is not None:
            worker.stop(timeout=timeout)

    def stop_all(self, timeout: float = 5.0) -> None:
        with self._lock:
            workers = list(self._workers.values())
            self._workers.clear()
        for worker in workers:
            worker.stop(timeout=timeout)

    def stats(self) -> dict:
        with self._lock:
            return {
                key: {
                    "depth": w.depth,
                    "busy_with": w.busy_with,
                    "served": w.calls_served,
                    "rejected": w.calls_rejected,
                    "running": w.is_running,
                }
                for key, w in self._workers.items()
            }


def clamp_blocking(seconds: float, limit: float) -> float:
    """Clamp a caller-supplied duration to the server-side bound."""
    if seconds < 0:
        raise BenchValueError(f"duration must be >= 0, got {seconds}")
    return min(seconds, limit)

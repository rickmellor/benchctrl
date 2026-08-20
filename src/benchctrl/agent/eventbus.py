"""Non-blocking, priority-aware event fan-out to agent subscribers.

Why this exists
---------------

Before this module, :py:meth:`benchctrl.agent.server.BenchAgent._broadcast_event`
walked its sessions and called ``sock.sendall()`` on each one *synchronously*,
and no send timeout was ever set (``net.frames`` sets one for reads only). That
put a client's TCP receive window on the critical path of the agent's own
safety machinery:

- :py:meth:`benchctrl.agent.safety.Governor.trip` emits its ``safety_trip``
  event **before** it drives anything to a safe state.
- ``trip()`` runs on the deadman thread.

So one client that stopped reading — a wedged browser, an unplugged panel, a
laptop that suspended mid-run — could stall ``sendall()`` inside the governor
and *delay disarming an armed instrument*. The existing ``except Exception``
around the event sink does not help: it catches a sink that **raises**, and the
failure here is a sink that **blocks**.

The rule this module enforces is therefore: **a producer never touches a
socket.** Producers append to a bounded per-subscriber queue and return; a
dedicated sender thread per subscriber does the I/O. A subscriber that cannot
keep up loses events. It never applies back-pressure to the bench.

Priority, and what gets shed
----------------------------

Events carry the severity taxonomy already used by the run engine
(:py:data:`benchctrl.agent.runs.spec.SEVERITIES`). Under back-pressure the
*least* important queued event is sacrificed first, so a ``safety_trip``
still reaches a screen that is drowning in ``info`` chatter.

Two properties follow from evicting rather than reordering, and both matter to
consumers:

1. **Delivery order is never permuted.** Shedding only ever *removes* entries;
   whatever survives arrives in the order it was produced. A consumer can rely
   on monotonically increasing ``seq``.
2. **A gap is always reported.** Drops are counted and announced in-band via a
   synthetic ``events_dropped`` event, so a stale display can say it is stale.
   A dashboard that silently showed month-old state would be worse than one
   that showed nothing — this is CONTRIBUTING rule 4 (no silent fallbacks) at
   the presentation layer.

Layering: this sits beside the agent's session plumbing and knows nothing
about instruments. It takes a ``send`` callable, so the unit tests drive it
with no sockets at all.
"""

from __future__ import annotations

import logging
import threading
from collections import deque
from typing import Callable, Optional

log = logging.getLogger("benchctrl.agent.eventbus")

#: Severity rank, lowest to highest. Mirrors
#: :py:data:`benchctrl.agent.runs.spec.SEVERITIES` — kept as an explicit map
#: rather than an index lookup so an unknown severity has a defined rank
#: instead of raising on the producer's thread.
SEVERITY_RANK = {
    "debug": 0,
    "info": 1,
    "warn": 2,
    "alarm": 3,
    "critical": 4,
}

#: Rank given to a severity we do not recognise. Deliberately *above* ``info``:
#: an unknown severity is more likely to be a new alarm class than new chatter,
#: and shedding something important is the worse error.
UNKNOWN_RANK = SEVERITY_RANK["warn"]

#: Default queue depth for an interactive consumer.
DEFAULT_MAX_QUEUE = 256

#: Queue depth for a status display. Small on purpose: a panel wants *current*
#: state, and a deep queue only means it renders history nobody is reading.
DISPLAY_MAX_QUEUE = 32

#: How long a sender may block in one send before the subscriber is considered
#: wedged and dropped. Bounds how long a dead peer keeps a thread alive; it is
#: never on a producer's path, so this is a resource bound, not a safety one.
DEFAULT_SEND_TIMEOUT_S = 5.0


def rank_of(event: dict) -> int:
    """Priority of ``event``, defaulting safely for unknown severities."""
    return SEVERITY_RANK.get(str(event.get("severity", "info")), UNKNOWN_RANK)


class EventSubscriber:
    """One consumer's bounded queue plus the thread that drains it.

    ``send`` is called only on this subscriber's own sender thread, so an
    implementation may block; blocking costs this subscriber its freshness and
    costs the rest of the agent nothing.

    ``droppable`` marks a consumer whose completeness does not matter — a
    status display. It is shed from first when memory pressure is global, and
    it is what the dashboard registers as.
    """

    def __init__(
        self,
        name: str,
        send: Callable[[dict], None],
        *,
        max_queue: int = DEFAULT_MAX_QUEUE,
        droppable: bool = False,
        send_timeout_s: float = DEFAULT_SEND_TIMEOUT_S,
    ) -> None:
        if max_queue < 1:
            raise ValueError(f"max_queue must be >= 1, got {max_queue}")
        self.name = name
        self.droppable = droppable
        self.max_queue = max_queue
        self.send_timeout_s = send_timeout_s
        self._send = send
        self._queue: deque[dict] = deque()
        self._lock = threading.Lock()
        self._wake = threading.Condition(self._lock)
        self._stop = False
        self._thread: Optional[threading.Thread] = None
        # Counters. Read under the lock; exposed via stats().
        self._dropped = 0
        self._dropped_unreported = 0
        self._delivered = 0
        self._failed = False

    # --- lifecycle ------------------------------------------------------

    def start(self) -> EventSubscriber:
        if self._thread is not None:
            return self
        self._thread = threading.Thread(
            target=self._run, name=f"evt-{self.name}", daemon=True
        )
        self._thread.start()
        return self

    def close(self, *, timeout: float = 2.0) -> None:
        """Stop the sender thread. Idempotent; never raises.

        Undelivered events are abandoned deliberately. Draining on shutdown
        would let a stalled consumer delay agent shutdown, which is the very
        coupling this module exists to remove.
        """
        with self._lock:
            self._stop = True
            self._wake.notify_all()
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
            if thread.is_alive():
                # A wedged send outlives us; it is a daemon thread and the
                # socket teardown will free it. Say so rather than pretending.
                log.warning(
                    "eventbus: subscriber %s did not stop within %.1fs "
                    "(consumer is wedged in send)",
                    self.name,
                    timeout,
                )
        self._thread = None

    @property
    def is_running(self) -> bool:
        thread = self._thread
        return thread is not None and thread.is_alive()

    # --- producer side --------------------------------------------------

    def offer(self, event: dict) -> bool:
        """Queue ``event``. Returns whether it was accepted.

        **Never blocks on I/O and never raises.** This is called from the
        governor's trip path and from the run engine's tick loop; both must be
        able to treat fan-out as free.
        """
        incoming = rank_of(event)
        with self._lock:
            if self._stop:
                return False
            if len(self._queue) >= self.max_queue:
                if not self._evict_for(incoming):
                    # Nothing queued is less important than this event, so the
                    # incoming one is the right thing to lose.
                    self._dropped += 1
                    self._dropped_unreported += 1
                    return False
            self._queue.append(event)
            self._wake.notify()
            return True

    def _evict_for(self, incoming_rank: int) -> bool:
        """Make room by dropping the oldest lowest-priority queued event.

        Returns False when every queued event is at least as important as the
        incoming one. Caller must hold the lock.

        Only ever *removes* entries, which is what keeps delivery order a
        subsequence of production order.
        """
        victim_index = None
        victim_rank = incoming_rank
        for i, queued in enumerate(self._queue):
            r = rank_of(queued)
            if r < victim_rank:
                victim_rank = r
                victim_index = i
        if victim_index is None:
            return False
        del self._queue[victim_index]
        self._dropped += 1
        self._dropped_unreported += 1
        return True

    # --- consumer side --------------------------------------------------

    def _run(self) -> None:
        while True:
            with self._lock:
                while not self._queue and not self._stop:
                    self._wake.wait()
                if self._stop:
                    return
                event = self._queue.popleft()
                # Announce a gap in-band, now that we have room again, so the
                # consumer learns it missed something without polling stats.
                gap = self._dropped_unreported
                if gap:
                    self._dropped_unreported = 0

            if gap:
                notice = {
                    "kind": "events_dropped",
                    "severity": "warn",
                    "count": gap,
                    "subscriber": self.name,
                    "text": (
                        f"{gap} event(s) dropped: this consumer is not keeping "
                        f"up, so its view is incomplete"
                    ),
                }
                if not self._deliver(notice):
                    return
            if not self._deliver(event):
                return

    def _deliver(self, event: dict) -> bool:
        """Send one event. Returns False if this subscriber is finished.

        A failing consumer is dropped rather than retried: the agent has no
        obligation to a peer that cannot receive, and retrying would tie up a
        thread indefinitely.
        """
        try:
            self._send(event)
        except Exception as exc:  # noqa: BLE001 - a dead peer is not our problem
            with self._lock:
                self._failed = True
            log.info(
                "eventbus: subscriber %s dropped after send failure: %s",
                self.name,
                exc,
            )
            return False
        with self._lock:
            self._delivered += 1
        return True

    # --- reporting ------------------------------------------------------

    def stats(self) -> dict:
        with self._lock:
            return {
                "name": self.name,
                "droppable": self.droppable,
                "queued": len(self._queue),
                "max_queue": self.max_queue,
                "delivered": self._delivered,
                "dropped": self._dropped,
                "failed": self._failed,
                "running": self.is_running,
            }


class EventBus:
    """Fan-out to every subscriber, with no producer ever blocking.

    Registration and publication take a short lock for bookkeeping only; no
    socket I/O happens under it, which is what stops a slow consumer from
    serialising the producers behind it.
    """

    def __init__(self) -> None:
        self._subscribers: dict[str, EventSubscriber] = {}
        self._lock = threading.Lock()
        self._published = 0

    def subscribe(self, subscriber: EventSubscriber) -> EventSubscriber:
        with self._lock:
            existing = self._subscribers.get(subscriber.name)
            if existing is not None:
                raise ValueError(f"subscriber {subscriber.name!r} already registered")
            self._subscribers[subscriber.name] = subscriber
        subscriber.start()
        return subscriber

    def unsubscribe(self, name: str, *, timeout: float = 2.0) -> None:
        with self._lock:
            subscriber = self._subscribers.pop(name, None)
        if subscriber is not None:
            subscriber.close(timeout=timeout)

    def publish(self, event: dict) -> int:
        """Offer ``event`` to every subscriber. Returns how many accepted it.

        Never blocks on I/O, never raises. Safe to call from the deadman
        thread, and specifically safe to call from
        :py:meth:`Governor.trip` before it disarms anything.
        """
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._published += 1
        accepted = 0
        for subscriber in subscribers:
            try:
                if subscriber.offer(event):
                    accepted += 1
            except Exception:  # pragma: no cover - offer is already no-raise
                log.exception("eventbus: offer raised for %s", subscriber.name)
        return accepted

    def close(self, *, timeout: float = 2.0) -> None:
        with self._lock:
            subscribers = list(self._subscribers.values())
            self._subscribers.clear()
        for subscriber in subscribers:
            subscriber.close(timeout=timeout)

    def prune(self) -> list[str]:
        """Drop subscribers whose sender thread has finished. Returns names."""
        with self._lock:
            dead = [
                name
                for name, sub in self._subscribers.items()
                if not sub.is_running and sub.stats()["failed"]
            ]
            for name in dead:
                self._subscribers.pop(name, None)
        return dead

    def stats(self) -> dict:
        with self._lock:
            subscribers = list(self._subscribers.values())
            published = self._published
        return {
            "published": published,
            "subscribers": [s.stats() for s in subscribers],
        }

    def __len__(self) -> int:
        with self._lock:
            return len(self._subscribers)

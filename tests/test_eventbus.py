"""Event fan-out: the producer must never be able to block.

The hazard these tests exist for is specific and was real. ``Governor.trip()``
emits ``safety_trip`` *before* it drives instruments to a safe state, on the
deadman thread, and the old fan-out called ``sock.sendall()`` synchronously
with no send timeout. A client that stopped reading could therefore stall the
governor and delay a disarm.

So the assertions here are mostly about *timing and non-interference* rather
than about content: a wedged consumer must cost the producer nothing.
"""

from __future__ import annotations

import threading
import time

import pytest

from benchctrl.agent.eventbus import (
    DISPLAY_MAX_QUEUE,
    SEVERITY_RANK,
    EventBus,
    EventSubscriber,
    rank_of,
)
from benchctrl.agent.runs.spec import SEVERITIES


def _evt(kind="thing", severity="info", **extra):
    return {"kind": kind, "severity": severity, **extra}


class RecordingSink:
    """Collects events, optionally blocking to model a consumer that stalls."""

    def __init__(self, *, block: bool = False):
        self.events: list[dict] = []
        self._lock = threading.Lock()
        self.gate = threading.Event()
        self.entered = threading.Event()
        self._block = block

    def __call__(self, event: dict) -> None:
        if self._block:
            self.entered.set()
            self.gate.wait(timeout=10.0)
        with self._lock:
            self.events.append(event)

    @property
    def kinds(self) -> list[str]:
        with self._lock:
            return [e.get("kind") for e in self.events]

    def wait_for(self, n: int, timeout: float = 2.0) -> bool:
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                if len(self.events) >= n:
                    return True
            time.sleep(0.005)
        return False


@pytest.fixture
def bus():
    b = EventBus()
    try:
        yield b
    finally:
        b.close(timeout=1.0)


# --------------------------------------------------------------------------
# The property the module exists for
# --------------------------------------------------------------------------


def test_publish_does_not_block_on_a_wedged_consumer(bus):
    """The whole point: a consumer stuck in send must not stall the producer.

    If this fails, a wedged HDMI panel can delay the governor disarming an
    armed instrument. The bound is deliberately loose (0.5 s against a
    consumer blocked for up to 10 s) so it fails on *blocking*, not on a slow
    CI runner.
    """
    sink = RecordingSink(block=True)
    bus.subscribe(EventSubscriber("wedged", sink, max_queue=4))
    bus.publish(_evt(kind="first"))
    assert sink.entered.wait(timeout=2.0), "sender thread never entered send"

    # Fill the queue and keep publishing well past its depth.
    start = time.monotonic()
    for i in range(200):
        bus.publish(_evt(kind=f"e{i}"))
    elapsed = time.monotonic() - start

    assert elapsed < 0.5, (
        f"publish() took {elapsed:.2f}s against a blocked consumer — the "
        f"producer is being back-pressured, which is the bug this module fixes"
    )
    sink.gate.set()


def test_a_wedged_consumer_does_not_delay_another(bus):
    """One bad subscriber must not starve a healthy one.

    Models the real deployment: the dashboard panel wedges while an operator
    client is mid-run and still needs its events.
    """
    stalled = RecordingSink(block=True)
    healthy = RecordingSink()
    bus.subscribe(EventSubscriber("stalled", stalled, max_queue=2))
    bus.subscribe(EventSubscriber("healthy", healthy, max_queue=64))

    for i in range(20):
        bus.publish(_evt(kind=f"e{i}"))

    assert healthy.wait_for(20), f"healthy subscriber only got {len(healthy.events)}"
    stalled.gate.set()


def test_offer_never_raises_even_when_closed():
    """Producers treat fan-out as free; a closed subscriber returns False."""
    sub = EventSubscriber("s", RecordingSink(), max_queue=2)
    sub.start()
    sub.close(timeout=1.0)
    assert sub.offer(_evt()) is False


def test_publish_survives_a_subscriber_whose_send_raises(bus):
    """A dead peer is dropped, and the bus keeps serving everyone else."""

    def explode(event):
        raise OSError("peer reset")

    good = RecordingSink()
    bus.subscribe(EventSubscriber("bad", explode, max_queue=8))
    bus.subscribe(EventSubscriber("good", good, max_queue=8))

    for i in range(5):
        assert bus.publish(_evt(kind=f"e{i}")) >= 1

    assert good.wait_for(5)


# --------------------------------------------------------------------------
# Priority: what gets shed under back-pressure
# --------------------------------------------------------------------------


def test_severity_ranks_cover_the_run_engines_taxonomy():
    """A severity the run engine can emit must have a defined rank.

    Otherwise a real `alarm` would be ranked as unknown and shed as if it were
    chatter.
    """
    for severity in SEVERITIES:
        assert severity in SEVERITY_RANK, f"{severity!r} has no rank"


def test_an_unknown_severity_outranks_info():
    """Unknown severities must not be shed as chatter.

    A new alarm class added later must not be silently deprioritised because
    this table predates it.
    """
    assert rank_of({"severity": "no-such-severity"}) > rank_of({"severity": "info"})


def test_a_critical_event_evicts_queued_chatter():
    """The invariant that matters: a safety trip reaches a saturated consumer.

    Inverting the eviction (dropping the incoming event instead of a queued
    lower-priority one) makes this fail — the trip would be the thing lost.
    """
    sink = RecordingSink(block=True)
    sub = EventSubscriber("s", sink, max_queue=4)
    # No thread started: the queue stays saturated so eviction is observable.
    for i in range(4):
        assert sub.offer(_evt(kind=f"chatter{i}", severity="info")) is True
    assert sub.stats()["queued"] == 4

    assert sub.offer(_evt(kind="safety_trip", severity="critical")) is True

    kinds = [e["kind"] for e in sub._queue]
    assert "safety_trip" in kinds, "a critical event was dropped for chatter"
    assert sub.stats()["dropped"] == 1


def test_chatter_is_refused_when_everything_queued_is_more_important():
    """With a queue full of criticals, new info is the right thing to lose."""
    sub = EventSubscriber("s", RecordingSink(), max_queue=3)
    for i in range(3):
        assert sub.offer(_evt(kind=f"trip{i}", severity="critical")) is True

    assert sub.offer(_evt(kind="chatter", severity="info")) is False

    kinds = [e["kind"] for e in sub._queue]
    assert kinds == ["trip0", "trip1", "trip2"], "a critical event was evicted"


def test_shedding_never_reorders_delivery():
    """Delivery order must stay a subsequence of production order.

    Priority is implemented by *eviction*, not by queue-jumping, so a consumer
    can trust monotonically increasing seq. A jump-the-queue implementation
    fails this.
    """
    sub = EventSubscriber("s", RecordingSink(), max_queue=4)
    sub.offer(_evt(kind="a", severity="info"))
    sub.offer(_evt(kind="b", severity="critical"))
    sub.offer(_evt(kind="c", severity="info"))
    sub.offer(_evt(kind="d", severity="critical"))
    # Saturated; this evicts the oldest info ("a"), leaving relative order.
    sub.offer(_evt(kind="e", severity="critical"))

    kinds = [e["kind"] for e in sub._queue]
    assert kinds == ["b", "c", "d", "e"], f"order was permuted: {kinds}"


# --------------------------------------------------------------------------
# Drops are reported, never silent
# --------------------------------------------------------------------------


def test_a_dropped_event_is_announced_in_band(bus):
    """A stale consumer must learn it is stale (CONTRIBUTING rule 4).

    A display that silently rendered an incomplete view would be worse than
    one that said so.
    """
    sink = RecordingSink(block=True)
    sub = EventSubscriber("panel", sink, max_queue=2, droppable=True)
    bus.subscribe(sub)
    # The sender only enters send once something is queued, so publish first
    # and *then* wait for it to be parked inside the blocked sink.
    bus.publish(_evt(kind="first"))
    assert sink.entered.wait(timeout=2.0), "sender thread never entered send"

    for i in range(30):
        bus.publish(_evt(kind=f"e{i}"))
    assert sub.stats()["dropped"] > 0, "queue should have overflowed"

    sink.gate.set()
    assert sink.wait_for(3, timeout=3.0)
    assert "events_dropped" in sink.kinds, (
        "the consumer was never told it missed events"
    )
    notice = next(e for e in sink.events if e.get("kind") == "events_dropped")
    assert notice["count"] > 0
    assert notice["severity"] == "warn"


def test_the_drop_notice_is_not_repeated_once_reported(bus):
    """The gap notice reports a count, not one event per drop.

    Otherwise recovering from a burst floods the consumer with notices, which
    is its own denial of service.
    """
    sink = RecordingSink()
    sub = EventSubscriber("panel", sink, max_queue=2, droppable=True)
    for i in range(10):
        sub.offer(_evt(kind=f"e{i}"))
    bus.subscribe(sub)

    assert sink.wait_for(3, timeout=3.0)
    time.sleep(0.2)
    notices = [e for e in sink.events if e.get("kind") == "events_dropped"]
    assert len(notices) == 1, f"expected one coalesced notice, got {len(notices)}"


def test_stats_report_queue_depth_and_drops():
    sub = EventSubscriber("s", RecordingSink(), max_queue=2)
    sub.offer(_evt())
    sub.offer(_evt())
    sub.offer(_evt())
    st = sub.stats()
    assert st["queued"] == 2
    assert st["max_queue"] == 2
    assert st["dropped"] == 1
    assert st["name"] == "s"


# --------------------------------------------------------------------------
# Bounded memory and lifecycle
# --------------------------------------------------------------------------


def test_the_queue_is_bounded_however_much_is_published(bus):
    """An undrained subscriber must not grow without limit.

    A long unattended run emits continuously; an unbounded queue behind a dark
    panel is an OOM on the bench machine.
    """
    sink = RecordingSink(block=True)
    sub = EventSubscriber("panel", sink, max_queue=DISPLAY_MAX_QUEUE)
    bus.subscribe(sub)
    bus.publish(_evt(kind="first"))
    assert sink.entered.wait(timeout=2.0)

    for i in range(5000):
        bus.publish(_evt(kind=f"e{i}"))

    assert sub.stats()["queued"] <= DISPLAY_MAX_QUEUE
    sink.gate.set()


def test_a_display_queue_is_shallower_than_an_interactive_one():
    """A panel wants current state, not history nobody reads."""
    from benchctrl.agent.eventbus import DEFAULT_MAX_QUEUE

    assert DISPLAY_MAX_QUEUE < DEFAULT_MAX_QUEUE


def test_max_queue_must_be_positive():
    with pytest.raises(ValueError, match="max_queue"):
        EventSubscriber("s", RecordingSink(), max_queue=0)


def test_close_does_not_wait_for_a_wedged_consumer(bus):
    """Shutdown must not be held hostage by a stalled peer.

    Draining on close would reintroduce the coupling this module removes — a
    dark panel would delay agent shutdown, and shutdown is when anything still
    armed gets driven safe.
    """
    sink = RecordingSink(block=True)
    sub = EventSubscriber("wedged", sink, max_queue=4)
    bus.subscribe(sub)
    bus.publish(_evt(kind="first"))
    assert sink.entered.wait(timeout=2.0)
    for i in range(10):
        bus.publish(_evt(kind=f"e{i}"))

    start = time.monotonic()
    bus.close(timeout=0.3)
    elapsed = time.monotonic() - start

    assert elapsed < 1.5, f"close() blocked for {elapsed:.2f}s on a wedged consumer"
    sink.gate.set()


def test_close_is_idempotent():
    sub = EventSubscriber("s", RecordingSink(), max_queue=2)
    sub.start()
    sub.close(timeout=1.0)
    sub.close(timeout=1.0)
    assert not sub.is_running


def test_duplicate_subscriber_names_are_rejected(bus):
    """Names identify a subscriber for unsubscribe; silent replacement would
    orphan the first one's thread."""
    bus.subscribe(EventSubscriber("dup", RecordingSink()))
    with pytest.raises(ValueError, match="already registered"):
        bus.subscribe(EventSubscriber("dup", RecordingSink()))


def test_unsubscribe_stops_delivery(bus):
    sink = RecordingSink()
    bus.subscribe(EventSubscriber("s", sink, max_queue=8))
    bus.publish(_evt(kind="before"))
    assert sink.wait_for(1)

    bus.unsubscribe("s", timeout=1.0)
    assert bus.publish(_evt(kind="after")) == 0
    assert "after" not in sink.kinds


def test_events_reach_every_subscriber(bus):
    a, b = RecordingSink(), RecordingSink()
    bus.subscribe(EventSubscriber("a", a, max_queue=8))
    bus.subscribe(EventSubscriber("b", b, max_queue=8))

    assert bus.publish(_evt(kind="fanout")) == 2

    assert a.wait_for(1) and b.wait_for(1)
    assert "fanout" in a.kinds and "fanout" in b.kinds


def test_prune_removes_only_failed_subscribers(bus):
    def explode(event):
        raise OSError("gone")

    healthy = RecordingSink()
    bus.subscribe(EventSubscriber("bad", explode, max_queue=4))
    bus.subscribe(EventSubscriber("good", healthy, max_queue=4))
    bus.publish(_evt())
    assert healthy.wait_for(1)

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not bus.prune():
        time.sleep(0.01)

    names = [s["name"] for s in bus.stats()["subscribers"]]
    assert names == ["good"], f"prune left {names}"

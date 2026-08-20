"""The dashboard's state model, and the feed that drives it.

Everything here is about one property: **a display that has fallen behind
reality must say so.** A bench panel reading ``IDLE`` beside a live output
invites someone to touch the DUT, so the tests below are mostly adversarial
about staleness rather than about happy-path rendering.

No sockets in the state tests; ``now`` is injected so the silence logic is
deterministic rather than slept through. The feed tests use a fake client so
the whole reconnect loop runs in milliseconds.
"""

from __future__ import annotations

import threading
import time

import pytest

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.dashboards.state import (
    DEFAULT_SILENCE_S,
    LOG_LIMIT,
    SILENCE_HEARTBEATS,
    STARTUP_GRACE_S,
    BenchStatus,
)


def status_payload(devices=None, *, since_contact=0.1, deadman_s=15.0, trips=()):
    return {
        "safety": {
            "armed": [k for k, v in (devices or {}).items() if v.get("armed")],
            "seconds_since_contact": since_contact,
            "deadman_s": deadman_s,
            "devices": devices or {},
            "trips": list(trips),
        },
        "workers": {},
        "blobs": {},
        "recordings": [],
    }


def armed(**overrides):
    base = {"armed": True, "output_armed": True, "recording": False, "emulating": False}
    base.update(overrides)
    return base


def idle(**overrides):
    base = {
        "armed": False,
        "output_armed": False,
        "recording": False,
        "emulating": False,
    }
    base.update(overrides)
    return base


WELCOME = {
    "agent": "benchctrl-agent",
    "observer": True,
    "heartbeat_s": 5.0,
    "deadman_s": 15.0,
}


@pytest.fixture()
def live():
    """A connected panel with a fresh authoritative snapshot."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_status(status_payload({"otii_arc": idle()}), now=100.0)
    return s


# --------------------------------------------------------------------------
# Headline priority: the most dangerous true thing wins
# --------------------------------------------------------------------------


def test_a_fresh_model_does_not_claim_to_be_idle():
    """Before anything is known, the panel must not look reassuring."""
    s = BenchStatus()
    assert s.headline != "IDLE"
    assert not s.trustworthy


# --------------------------------------------------------------------------
# The startup window: "I have not tried" is not "there is no agent"
# --------------------------------------------------------------------------


def test_the_first_frame_says_starting_not_no_agent():
    """Seen on the board: the boot screen's opening frame read ``NO AGENT``.

    The feed connects on a background thread, so the first render happens
    before any attempt has resolved. ``NO AGENT`` there is a guess about
    ourselves dressed up as a fact about the bench, and a kiosk display that
    cries wolf every boot is one an operator stops reading.
    """
    s = BenchStatus()
    assert s.headline == "STARTING"
    assert s.starting
    assert s.severity == "info", "startup was drawn as a warning"
    assert not s.trustworthy, "STARTING must not imply the view is current"


def test_a_failed_attempt_still_says_no_agent():
    """The half that must not weaken: a real failure is still reported as one.

    This is the whole risk of the fix — buying a quiet first frame by making a
    genuinely-absent agent look like a slow start would be a far worse bug than
    the flicker it replaces.
    """
    s = BenchStatus()
    s.apply_disconnected("cannot reach the agent: [Errno 111] refused")
    assert s.headline == "NO AGENT"
    assert not s.starting
    assert s.severity == "warn"
    assert "Errno 111" in s.stale_reason


def test_a_connected_session_ends_the_startup_window():
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    assert not s.starting
    assert s.attempted


def test_a_drop_after_connecting_says_no_agent_not_starting(live):
    """Once we have talked to the agent, losing it is never "starting"."""
    live.apply_disconnected("the agent closed the connection")
    assert live.headline == "NO AGENT"
    assert not live.starting


def test_the_startup_window_expires_rather_than_hanging_there():
    """The backstop, for a feed thread that never reports either way.

    Without it, a feed that was never started — or one whose thread died before
    its first attempt — would leave "starting" on screen forever, which is the
    stale-but-plausible display this module exists to prevent.
    """
    s = BenchStatus()
    s.created_mono = 100.0
    s.expire_startup_grace(now=100.0 + STARTUP_GRACE_S - 0.1)
    assert s.headline == "STARTING", "the grace ended early"

    s.expire_startup_grace(now=100.0 + STARTUP_GRACE_S)
    assert s.headline == "NO AGENT"
    assert "no connection attempt has completed" in s.stale_reason


def test_expiring_the_grace_cannot_overwrite_a_real_reason():
    """A recorded failure outranks the generic timeout message.

    ``expire_startup_grace`` runs on every render, so a bug here would replace
    "[Errno 111] refused" with "is the feed running?" — swapping the actionable
    diagnosis for a vague one.
    """
    s = BenchStatus()
    s.created_mono = 100.0
    s.apply_disconnected("cannot reach the agent: [Errno 111] refused")
    s.expire_startup_grace(now=100.0 + STARTUP_GRACE_S + 60.0)
    assert "Errno 111" in s.stale_reason


def test_an_unsafe_latch_outranks_the_startup_window():
    """Nothing gets to look calm while an output is unproven.

    Reachable in practice: a ``safety_failed`` event can arrive on the client's
    rx thread before the feed's first status poll has landed.
    """
    s = BenchStatus()
    s.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=100.0)
    assert s.headline == "UNSAFE"
    assert s.severity == "critical"


def test_an_idle_bench_reads_idle(live):
    assert live.headline == "IDLE"
    assert live.trustworthy


def test_an_armed_bench_reads_armed(live):
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    assert live.headline == "ARMED"
    assert live.armed_devices == ["otii_arc"]
    assert live.severity == "alarm"


def test_staleness_outranks_an_armed_reading(live):
    """A stale ARMED is still a lie about *when*, so STALE wins.

    The operator needs to know the screen is not current more urgently than
    they need the last-known arm state, which is still shown in the device
    pane.
    """
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    live.apply_event({"kind": "events_dropped", "count": 4}, now=102.0)
    assert live.headline == "STALE"
    assert live.any_armed, "the arm state must still be visible underneath"


def test_an_unconfirmed_safe_output_outranks_everything(live):
    """`safety_failed` means the agent could not prove the output went off."""
    live.apply_event({"kind": "events_dropped", "count": 1}, now=101.0)
    live.apply_event(
        {
            "kind": "safety_failed",
            "severity": "critical",
            "device": "otii_arc",
            "guidance": "Output may still be live — physically disconnect the DUT.",
        },
        now=102.0,
    )
    assert live.headline == "UNSAFE"
    assert live.severity == "critical"
    assert "disconnect" in live.unsafe_latch["guidance"]


def test_the_unsafe_latch_survives_a_clean_status_snapshot(live):
    """The dangerous one. A later snapshot must not clear it.

    The agent reporting "nothing armed" after a `safety_failed` means its
    *bookkeeping* was cleared, not that the hardware output went off — that is
    exactly what `safety_failed` said it could not establish. Only a human can
    decide the DUT is disconnected.
    """
    live.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=101.0)
    live.apply_status(status_payload({"otii_arc": idle()}), now=102.0)
    assert live.headline == "UNSAFE", "a clean snapshot silently cleared the latch"
    assert live.stale_reason is not None


def test_the_unsafe_latch_survives_a_reconnect(live):
    """A fresh socket says nothing about whether the output went off."""
    live.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=101.0)
    live.apply_disconnected()
    live.apply_connected(WELCOME, now=200.0)
    live.apply_status(status_payload({"otii_arc": idle()}), now=200.0)
    assert live.headline == "UNSAFE"


# --------------------------------------------------------------------------
# Staleness: every way the view can fall behind
# --------------------------------------------------------------------------


def test_a_dropped_event_makes_the_view_untrustworthy(live):
    """The in-band gap notice from the event bus must land on screen."""
    live.apply_event({"kind": "events_dropped", "severity": "warn", "count": 7}, now=101)
    assert not live.trustworthy
    assert live.dropped_events == 7
    assert "7" in live.stale_reason


def test_dropped_counts_accumulate(live):
    """A chronically-behind panel should look chronic, not briefly odd."""
    live.apply_event({"kind": "events_dropped", "count": 3}, now=101.0)
    live.apply_event({"kind": "events_dropped", "count": 4}, now=102.0)
    assert live.dropped_events == 7


def test_a_dropped_notice_without_a_count_still_counts(live):
    """Malformed input must not silently mean zero."""
    live.apply_event({"kind": "events_dropped"}, now=101.0)
    assert live.dropped_events == 1
    assert not live.trustworthy


def test_a_disconnect_makes_the_view_untrustworthy(live):
    live.apply_disconnected("socket closed")
    assert live.headline == "NO AGENT"
    assert not live.trustworthy
    assert "socket closed" in live.stale_reason


def test_a_disconnect_keeps_the_last_known_arm_state_but_marks_it_inferred(live):
    """Blanking the devices would make an armed bench look idle."""
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    live.apply_disconnected()
    assert live.armed_devices == ["otii_arc"], "arm state was thrown away"
    assert live.devices["otii_arc"].inferred, "unconfirmed state not marked as such"


def test_silence_makes_the_view_untrustworthy(live):
    """Nothing crashed, nothing dropped — the agent simply went quiet."""
    budget = WELCOME["heartbeat_s"] * SILENCE_HEARTBEATS
    assert live.check_silence(now=100.0 + budget - 1) is None, "cried wolf early"
    reason = live.check_silence(now=100.0 + budget + 1)
    assert reason is not None
    assert not live.trustworthy


def test_the_silence_budget_follows_the_agents_own_heartbeat(live):
    """A bench configured for slow heartbeats is not stale for honouring it."""
    slow = BenchStatus()
    slow.apply_connected({**WELCOME, "heartbeat_s": 60.0}, now=100.0)
    slow.apply_status(status_payload(), now=100.0)
    # Well past the fast bench's budget, nowhere near this one's.
    assert slow.check_silence(now=100.0 + 100.0) is None
    assert slow.check_silence(now=100.0 + 60.0 * SILENCE_HEARTBEATS + 1) is not None


def test_silence_falls_back_to_a_default_when_no_heartbeat_was_advertised():
    s = BenchStatus()
    s.apply_connected({"agent": "x"}, now=100.0)
    s.apply_status(status_payload(), now=100.0)
    assert s.check_silence(now=100.0 + DEFAULT_SILENCE_S - 1) is None
    assert s.check_silence(now=100.0 + DEFAULT_SILENCE_S + 1) is not None


def test_a_disconnected_panel_is_not_relabelled_by_the_silence_check(live):
    """The disconnect reason is more specific; silence must not overwrite it."""
    live.apply_disconnected("the agent closed the connection")
    live.check_silence(now=100_000.0)
    assert "closed the connection" in live.stale_reason


def test_connecting_alone_does_not_make_the_view_trustworthy():
    """A socket is not state. Only a snapshot clears staleness."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    assert s.connected
    assert not s.trustworthy, "a bare connection was treated as current state"
    s.apply_status(status_payload({"otii_arc": idle()}), now=100.0)
    assert s.trustworthy


def test_an_event_alone_does_not_clear_staleness(live):
    """Events carry transitions, not whole state.

    A panel that started mid-run has missed the events that produced current
    state, so a single arriving event must not be taken as proof the view is
    complete.
    """
    live.apply_event({"kind": "events_dropped", "count": 1}, now=101.0)
    live.apply_event({"kind": "note", "severity": "info"}, now=102.0)
    assert not live.trustworthy
    live.apply_status(status_payload({"otii_arc": idle()}), now=103.0)
    assert live.trustworthy


# --------------------------------------------------------------------------
# Arming is tracked pessimistically
# --------------------------------------------------------------------------


def test_an_arm_event_is_believed_immediately(live):
    """Erring toward "armed" is the safe direction for an unconfirmed event."""
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    assert live.armed_devices == ["otii_arc"]
    assert live.devices["otii_arc"].inferred


def test_an_arm_event_for_an_unknown_device_still_shows_up(live):
    """A device the panel has never seen arming is the worst thing to drop."""
    live.apply_event({"kind": "device_armed", "device": "surprise_smu"}, now=101.0)
    assert "surprise_smu" in live.armed_devices


def test_a_trip_event_disarms(live):
    """A trip is authoritative: the governor cleared the state itself."""
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    live.apply_event(
        {"kind": "safety_trip", "severity": "critical", "reason": "heartbeat_lost",
         "devices": ["otii_arc"]},
        now=102.0,
    )
    assert live.armed_devices == []
    assert live.last_trip["reason"] == "heartbeat_lost"


def test_a_status_snapshot_is_trusted_to_disarm(live):
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    live.apply_status(status_payload({"otii_arc": idle()}), now=102.0)
    assert live.armed_devices == []
    assert not live.devices["otii_arc"].inferred


def test_a_device_the_agent_stops_reporting_is_removed(live):
    """A vanished device must not linger on screen as silently armed."""
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    live.apply_status(status_payload({"sdm4065a": idle()}), now=102.0)
    assert "otii_arc" not in live.devices
    assert not live.any_armed


def test_emulating_counts_as_a_state_worth_showing(live):
    live.apply_status(
        status_payload({"otii_arc": idle(emulating=True)}), now=101.0
    )
    assert live.devices["otii_arc"].label == "EMULATING"


def test_recording_is_visible_but_not_alarming(live):
    live.apply_status(status_payload({"otii_arc": idle(recording=True)}), now=101.0)
    assert live.headline == "RECORDING"
    assert live.severity == "info"


# --------------------------------------------------------------------------
# Bookkeeping
# --------------------------------------------------------------------------


def test_the_log_is_bounded(live):
    for i in range(LOG_LIMIT * 4):
        live.apply_event({"kind": "note", "n": i}, now=101.0 + i)
    assert len(live.log) == LOG_LIMIT
    # The newest must survive, not the oldest.
    assert live.log[-1]["n"] == LOG_LIMIT * 4 - 1


def test_alarms_are_kept_separately_from_chatter(live):
    live.apply_event({"kind": "note", "severity": "info"}, now=101.0)
    live.apply_event({"kind": "limit", "severity": "alarm"}, now=102.0)
    for i in range(LOG_LIMIT * 2):
        live.apply_event({"kind": "note", "severity": "info", "n": i}, now=103.0 + i)
    assert [e["kind"] for e in live.sticky] == ["limit"], (
        "an alarm scrolled out of view under info chatter"
    )


def test_seq_is_forgotten_across_a_reconnect(live):
    """The event bus guarantees monotonic seq *per session*."""
    live.apply_event({"kind": "note", "seq": 900}, now=101.0)
    assert live.last_seq == 900
    live.apply_disconnected()
    live.apply_connected(WELCOME, now=200.0)
    assert live.last_seq is None, "a stale seq would read the next session as a gap"


def test_run_progress_is_tracked(live):
    live.apply_event({"kind": "run_started", "run_id": "r1", "name": "soak"}, now=101.0)
    live.apply_event(
        {"kind": "run_step", "run_id": "r1", "step": "ramp", "progress": 0.4}, now=102.0
    )
    assert live.runs["r1"].state == "running"
    assert live.runs["r1"].step == "ramp"
    assert live.runs["r1"].progress == pytest.approx(0.4)
    live.apply_event({"kind": "run_finished", "run_id": "r1", "state": "ok"}, now=103.0)
    assert live.runs["r1"].state == "ok"


def test_a_run_event_with_no_id_is_ignored_rather_than_crashing(live):
    live.apply_event({"kind": "run_step", "step": "ramp"}, now=101.0)
    assert live.runs == {}


def test_to_dict_reports_the_honest_headline(live):
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    live.apply_event({"kind": "events_dropped", "count": 2}, now=102.0)
    data = live.to_dict()
    assert data["headline"] == "STALE"
    assert data["armed"] == ["otii_arc"]
    assert data["trustworthy"] is False
    assert data["dropped_events"] == 2


# --------------------------------------------------------------------------
# The feed
# --------------------------------------------------------------------------


class FakeClient:
    """A RemoteClient stand-in that can be made to fail on demand."""

    def __init__(self, *, fail_after=None, status=None, welcome=None,
                 inventory=None, discover_raises=None):
        self.welcome = welcome if welcome is not None else dict(WELCOME)
        self.is_connected = True
        self.on_event = None
        self.closed = False
        self.status_calls = 0
        self.discover_calls = 0
        self._fail_after = fail_after
        self._status = status or status_payload({"otii_arc": idle()})
        self._inventory = inventory if inventory is not None else {"devices": []}
        self._discover_raises = discover_raises

    def status(self):
        self.status_calls += 1
        if self._fail_after is not None and self.status_calls > self._fail_after:
            self.is_connected = False
            raise OSError("connection reset")
        return self._status

    def discover(self):
        self.discover_calls += 1
        if self._discover_raises is not None:
            raise self._discover_raises
        return self._inventory

    def close(self):
        self.closed = True
        self.is_connected = False


ENDPOINT = EndpointConfig(host="127.0.0.1", port=9737, token="t")


def _wait(predicate, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


def test_the_feed_folds_a_status_snapshot():
    client = FakeClient(status=status_payload({"otii_arc": armed()}))
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: client)
    with feed:
        assert _wait(lambda: feed.snapshot()["headline"] == "ARMED")
    assert client.closed


def test_the_feed_folds_events_pushed_by_the_agent():
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: client)
    with feed:
        assert _wait(lambda: client.on_event is not None)
        client.on_event({"kind": "device_armed", "device": "otii_arc"})
        assert _wait(lambda: feed.snapshot()["armed"] == ["otii_arc"])


def test_the_feed_reports_an_unreachable_agent_rather_than_a_blank_screen():
    """A board that boots before the agent must not show a plausible IDLE."""

    def refuse():
        raise OSError("connection refused")

    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=refuse)
    with feed:
        assert _wait(lambda: feed.snapshot()["stale_reason"] is not None)
        snap = feed.snapshot()
    assert snap["headline"] == "NO AGENT"
    assert "cannot reach the agent" in snap["stale_reason"]


def test_the_feed_says_starting_while_its_first_connect_is_still_in_flight():
    """The board's real first frame: rendered before the thread has connected.

    Blocks inside ``connect`` to hold the window open deterministically, rather
    than racing a real socket for the few milliseconds it lasts on localhost.
    """
    release = threading.Event()

    def slow_connect():
        release.wait(timeout=5.0)
        raise OSError("connection refused")

    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=slow_connect)
    try:
        feed.start()
        snap = feed.snapshot()
        assert snap["headline"] == "STARTING", snap
        assert snap["starting"]
        assert not snap["trustworthy"]
        # And the moment the attempt resolves, it tells the truth instead.
        release.set()
        assert _wait(lambda: feed.snapshot()["headline"] == "NO AGENT")
    finally:
        release.set()
        feed.stop()


def test_a_feed_that_never_starts_stops_claiming_to_be_starting():
    """``snapshot()`` is what bounds the window, so it has to do the expiring.

    A panel whose feed thread never ran would otherwise sit on "starting"
    indefinitely — a calm screen with nothing behind it.
    """
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: None)
    # Never started. Backdate the model instead of sleeping out the grace.
    feed.status.created_mono = time.monotonic() - (STARTUP_GRACE_S + 1.0)
    snap = feed.snapshot()
    assert snap["headline"] == "NO AGENT", snap
    assert "no connection attempt has completed" in snap["stale_reason"]


def test_the_feed_reconnects_after_the_session_drops():
    """The agent restarting must not leave the panel dead until someone logs in.

    Nobody can log in — the board has no keyboard.
    """
    clients = []

    def connect():
        client = FakeClient(fail_after=1)
        clients.append(client)
        return client

    feed = AgentFeed(ENDPOINT, poll_s=0.01, connect=connect)
    with feed:
        assert _wait(lambda: len(clients) >= 3), f"only reconnected {len(clients)}x"


def test_the_feed_marks_the_view_stale_while_it_is_reconnecting():
    def connect():
        raise OSError("down")

    feed = AgentFeed(ENDPOINT, poll_s=0.01, connect=connect)
    with feed:
        assert _wait(lambda: not feed.snapshot()["trustworthy"])


def test_a_malformed_event_does_not_kill_the_feed():
    """An event handler that raises would silently stop all future events.

    ``None`` is used rather than a dict with junk values because the state
    model coerces junk (``str(...)``, ``isinstance`` guards) without raising —
    an earlier version of this test passed a weird-but-dict event and proved
    nothing. A non-dict is the real way in: it fails at ``.get``.
    """
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: client)
    with feed:
        assert _wait(lambda: client.on_event is not None)
        with pytest.raises(AttributeError):
            BenchStatus().apply_event(None)  # the failure the feed must absorb
        client.on_event(None)
        client.on_event({"kind": "device_armed", "device": "otii_arc"})
        assert _wait(lambda: feed.snapshot()["armed"] == ["otii_arc"]), (
            "a bad event stopped the feed from folding later ones"
        )


def test_the_feed_thread_does_not_outlive_stop():
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: client)
    feed.start()
    assert _wait(lambda: feed.snapshot()["connected"])
    feed.stop()
    names = [t.name for t in threading.enumerate()]
    assert "benchctrl-dashboard-feed" not in names, f"feed thread leaked: {names}"


def test_stopping_a_feed_that_never_started_is_harmless():
    AgentFeed(ENDPOINT, connect=lambda: FakeClient()).stop()


# --------------------------------------------------------------------------
# The bus inventory
#
# agent.status says what devices are DOING; agent.discover says what is
# ATTACHED. They are separate polls because they cost two orders of magnitude
# apart — measured on the bench board, ~1.65 s against ~5 ms, because
# identifying a USB-TMC instrument means reading its string descriptors over
# libusb.
# --------------------------------------------------------------------------


def test_the_feed_takes_an_inventory_and_folds_it_in():
    client = FakeClient(
        inventory={"devices": [{"device_key": "otii_arc", "confidence": "exact",
                                "path": "/dev/ttyACM0"}]}
    )
    feed = AgentFeed(ENDPOINT, poll_s=0.05, inventory_s=0.05, connect=lambda: client)
    with feed:
        assert _wait(lambda: feed.snapshot()["inventory_taken"])
    snap = feed.snapshot()
    assert snap["slots"]["otii_arc"]["present"] is True
    assert snap["slots"]["otii_arc"]["path"] == "/dev/ttyACM0"


def test_the_first_inventory_is_taken_immediately_not_after_one_interval():
    """Until a scan lands every slot's presence is unknown, and unknown is the
    one thing the rail cannot render as a fact. Waiting a full interval would
    leave the panel unable to describe the bench for the whole of it."""
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.05, inventory_s=3600.0, connect=lambda: client)
    with feed:
        # inventory_s is an hour, so anything arriving here came from the
        # due-immediately path rather than the interval.
        assert _wait(lambda: client.discover_calls >= 1)


def test_the_inventory_is_polled_far_less_often_than_the_status():
    """The cadence separation is the point: discover costs ~300x what status
    costs, so folding it into the status poll would pace the whole panel at the
    slowest thing it does."""
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.01, inventory_s=3600.0, connect=lambda: client)
    with feed:
        assert _wait(lambda: client.status_calls >= 8)
    # One from the due-immediately path, and no more for an hour.
    assert client.discover_calls == 1, (
        f"discover ran {client.discover_calls}x against {client.status_calls} "
        f"status polls — the expensive call is riding the fast clock"
    )


def test_a_failed_inventory_keeps_the_last_one_rather_than_blanking_it():
    """Discovery walks USB and can fail transiently. Flapping a slot between
    ATTACHED and NOT FOUND would make the rail unreadable, and the staleness
    machinery already covers a scan that stops succeeding for good."""
    client = FakeClient(
        inventory={"devices": [{"device_key": "otii_arc", "confidence": "exact"}]}
    )
    feed = AgentFeed(ENDPOINT, poll_s=0.02, inventory_s=0.02, connect=lambda: client)
    with feed:
        assert _wait(lambda: feed.snapshot()["inventory_taken"])
        client._discover_raises = OSError("usb busy")
        before = client.discover_calls
        assert _wait(lambda: client.discover_calls > before + 1)
        snap = feed.snapshot()
    assert snap["inventory_taken"], "a transient scan failure blanked the inventory"
    assert snap["slots"]["otii_arc"]["present"] is True


def test_a_failed_inventory_does_not_kill_the_session():
    """An inventory is an enrichment. Losing it must not cost the panel the
    status poll, which is what staleness and arm state depend on."""
    client = FakeClient(discover_raises=OSError("no usb access"))
    feed = AgentFeed(ENDPOINT, poll_s=0.02, inventory_s=0.02, connect=lambda: client)
    with feed:
        assert _wait(lambda: client.status_calls >= 3)
        snap = feed.snapshot()
    assert snap["connected"]
    assert not snap["inventory_taken"]


def test_a_feed_against_a_client_with_no_discover_still_runs():
    """An older agent, or any client stub without the method. The status half
    must be untouched by the absence of the inventory half.

    Written as a class that genuinely lacks the attribute rather than a subclass
    with ``discover = None``: the failure mode being pinned is an AttributeError
    from the lookup, and a None attribute would instead test a TypeError from
    calling it, which is a different (and unreachable) path.
    """

    class NoDiscover:
        def __init__(self):
            self.welcome = dict(WELCOME)
            self.is_connected = True
            self.on_event = None
            self.status_calls = 0

        def status(self):
            self.status_calls += 1
            return status_payload({"otii_arc": idle()})

        def close(self):
            self.is_connected = False

    client = NoDiscover()
    feed = AgentFeed(ENDPOINT, poll_s=0.02, inventory_s=0.02, connect=lambda: client)
    with feed:
        assert _wait(lambda: feed.snapshot()["connected"])
        assert _wait(lambda: client.status_calls >= 3)
    assert not feed.snapshot()["inventory_taken"]


def test_the_feed_requests_an_observer_session():
    """The load-bearing default. If this regresses, polling starves the deadman.

    Checks the real constructor path rather than the injected fake, because the
    fake is exactly where this guarantee could get lost.
    """
    import benchctrl.net.client as clientmod

    seen = {}

    class Spy:
        def __init__(self, endpoint, *, observer=False):
            seen["observer"] = observer

        def connect(self):
            return FakeClient()

    original = clientmod.RemoteClient
    clientmod.RemoteClient = Spy
    try:
        feed = AgentFeed(ENDPOINT, poll_s=0.05)
        with feed:
            assert _wait(lambda: "observer" in seen)
    finally:
        clientmod.RemoteClient = original

    assert seen["observer"] is True, (
        "the dashboard opened a NORMAL session — its polling will now feed the "
        "deadman and an armed bench will never auto-disarm"
    )


# --------------------------------------------------------------------------
# Asking for the observer role is not the same as having it
# --------------------------------------------------------------------------
#
# The test above proves we ASK. Only the agent can enforce what the role means,
# and the agent echoes it back in WELCOME specifically so a client can check the
# answer. These tests are about checking the answer, because the failure is
# silent and its consequence is the hazard the whole display was designed
# around: a session whose frames call Governor.touch() pins
# seconds_since_contact near zero, and an armed output never auto-disarms.


def test_an_agent_that_denies_the_observer_role_is_not_trusted():
    """The panel is wrong about the bench in the most dangerous direction here:
    its readings may be perfectly current while it silently holds the deadman
    open. Current-looking data is exactly why this cannot be quiet."""
    s = BenchStatus()
    s.apply_connected({**WELCOME, "observer": False}, now=100.0)
    s.apply_status(status_payload({"otii_arc": idle()}), now=100.0)

    assert s.observer_denied is not None
    assert s.headline == "NOT OBSERVER"
    assert s.severity == "critical"
    assert not s.trustworthy, "fresh data does not make a deadman-holding session safe"


def test_an_agent_too_old_to_echo_the_role_is_also_not_trusted():
    """A missing key is the realistic case, not an explicit False: an older agent
    predating the observer role would simply not mention it. Defaulting a missing
    safety flag to "fine" is how this check would come to pass while meaning
    nothing."""
    welcome = {k: v for k, v in WELCOME.items() if k != "observer"}
    s = BenchStatus()
    s.apply_connected(welcome, now=100.0)

    assert s.observer_denied is not None
    assert s.headline == "NOT OBSERVER"


def test_a_confirmed_observer_role_says_nothing():
    """The happy path must stay quiet, or the warning is noise on every boot."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_status(status_payload({"otii_arc": idle()}), now=100.0)

    assert s.observer_denied is None
    assert s.headline == "IDLE"
    assert s.trustworthy


def test_a_denied_observer_role_outranks_staleness_but_not_unsafe():
    """Ordering, worst-case first. A stale view is a display problem; a held-open
    deadman is a bench problem and outranks it. An unconfirmed output still beats
    both, because that one is already-happened rather than might-happen."""
    s = BenchStatus()
    s.apply_connected({**WELCOME, "observer": False}, now=100.0)
    s.stale_reason = "no snapshot in a while"
    assert s.headline == "NOT OBSERVER"

    s.unsafe_latch = {"device": "otii_arc"}
    assert s.headline == "UNSAFE"


def test_the_denial_latches_across_a_reconnect():
    """A reconnect that happens to land on a good agent says nothing about how
    long the deadman was held open on the previous session. Same reasoning as
    unsafe_latch: only a human can decide that is resolved."""
    s = BenchStatus()
    s.apply_connected({**WELCOME, "observer": False}, now=100.0)
    s.apply_disconnected("link dropped")
    s.apply_connected(WELCOME, now=200.0)

    assert s.observer_denied is not None, "a good reconnect must not erase it"
    assert s.headline == "NOT OBSERVER"

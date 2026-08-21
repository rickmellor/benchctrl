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
    DeviceSlot,
)


def status_payload(
    devices=None, *, since_contact=0.1, deadman_s=15.0, trips=(), workers=None
):
    return {
        "safety": {
            "armed": [k for k, v in (devices or {}).items() if v.get("armed")],
            "seconds_since_contact": since_contact,
            "deadman_s": deadman_s,
            "devices": devices or {},
            "trips": list(trips),
        },
        "workers": workers or {},
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


def test_an_action_event_reaches_the_log(live):
    """The pane's whole content. Before action events existed the only kinds
    anything emitted were safety_trip/safety_failed/events_dropped, so on a
    healthy bench the log was permanently empty."""
    live.apply_event(
        {
            "kind": "action",
            "severity": "info",
            "method": "device.call",
            "action": "set_voltage",
            "device": "rigol_dp2031",
            "detail": "3.3 → none",
            "count": 1,
            "ok": True,
        },
        now=101.0,
    )
    assert [e["kind"] for e in live.log] == ["action"]
    assert live.log[-1]["action"] == "set_voltage"
    assert live.actions_seen == 1


def test_an_action_event_does_not_change_arm_state(live):
    """Two sources for one fact drift, and the quieter one wins on screen.

    An action event carries the method name, so inferring "set_output means
    armed" from it is tempting. It would also be wrong in the dangerous
    direction: the governor owns the driver and knows whether the call took
    effect, while the log only knows it was dispatched. A refused or raising
    ``set_output`` would light ARMED on a bench that never armed.
    """
    live.apply_event(
        {
            "kind": "action",
            "severity": "warn",
            "method": "device.call",
            "action": "set_output",
            "device": "otii_arc",
            "ok": True,
        },
        now=101.0,
    )
    assert not live.any_armed, "the action log inferred arm state"
    assert live.headline == "IDLE"


def test_a_folded_count_is_carried_rather_than_accumulated(live):
    """``folded`` is the agent's own cumulative figure.

    Adding it per event would multiply: three events reporting 10, 20, 30 folded
    actions describe 30 folded actions, not 60.
    """
    for folded in (10, 20, 30):
        live.apply_event(
            {"kind": "action", "severity": "debug", "count": 5, "folded": folded},
            now=101.0 + folded,
        )
    assert live.actions_folded == 30
    # actions_seen counts what the rows stand for, not the rows.
    assert live.actions_seen == 15


def test_a_malformed_action_count_does_not_kill_the_feed(live):
    """``apply_event`` runs on the feed's receive path.

    ``int(event["count"])`` would raise on a payload from a future or buggy
    agent, and one bad event would end the session and blank the panel — the same
    rule as ``test_a_malformed_event_does_not_kill_the_feed``, applied to the
    numeric fields that now arrive from another process.
    """
    for bad in ("many", None, -3, 0, [1], True, 2.5):
        live.apply_event({"kind": "action", "count": bad, "folded": bad}, now=101.0)
    # Every event still landed, and each counted as at least the one action it is.
    assert len(live.log) == 7
    assert live.actions_seen == 7
    assert live.actions_folded == 0


def test_folded_actions_are_reported_separately_from_dropped_events(live):
    """Two different failures that must not be conflated.

    A *folded* action was counted and deliberately summarised at the producer; a
    *dropped* event was lost because this consumer could not keep up. Only the
    second means the view has holes, and only the second may make it stale.
    """
    live.apply_event(
        {"kind": "action", "severity": "debug", "count": 48, "folded": 4000}, now=101.0
    )
    assert live.actions_folded == 4000
    assert live.dropped_events == 0
    assert live.stale_reason is None, "summarising is not staleness"

    live.apply_event({"kind": "events_dropped", "count": 3}, now=102.0)
    assert live.dropped_events == 3
    assert live.actions_folded == 4000
    assert live.stale_reason is not None


def test_an_action_flood_cannot_evict_a_sticky_alarm(live):
    """The log is bounded, so the pane's memory is the thing under pressure.

    A run's reads arrive by the thousand. An alarm must not scroll out from under
    them — which is what ``sticky`` is for, and this is that guarantee restated
    against the traffic that now actually exists.
    """
    live.apply_event(
        {"kind": "safety_trip", "severity": "critical", "devices": []}, now=101.0
    )
    for i in range(LOG_LIMIT * 10):
        live.apply_event(
            {"kind": "action", "severity": "debug", "action": "read_raw", "n": i},
            now=102.0 + i,
        )
    assert len(live.log) == LOG_LIMIT, "the log grew without bound"
    assert [e["kind"] for e in live.sticky] == ["safety_trip"], (
        "a trip was lost under action chatter"
    )
    assert live.actions_seen == LOG_LIMIT * 10


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
# Instrument identity: which box, not just which state
#
# The rail's row heading is the device *key*, a benchctrl name for a driver. It
# does not say which of two Rigols is on the bench, and it cannot say whether
# the supply attached now is the one the last run was calibrated against. Only
# the serial number answers that, and on this bench it mostly is not where you
# would expect it: measured against the board, every VISA instrument reports
# ``serial_number: null`` while its serial sits in field 3 of the resource
# string it was found at.
# --------------------------------------------------------------------------


#: One ``agent.discover`` entry, shaped exactly as the bench board reports the
#: DP2031 — including ``serial_number: None``, which is the whole problem.
DISCOVERED_PSU = {
    "path": "USB0::6833::42152::DP2A243500269::0::INSTR",
    "transport": "visa",
    "device_key": "rigol_dp2031",
    "label": "Rigol DP2000-series power supply",
    "usb_id": "1ab1:a4a8",
    "serial_number": None,
    "confidence": "exact",
}


def test_a_visa_instruments_serial_is_read_out_of_its_resource_string():
    """The measured case: the payload's ``serial_number`` is null and the serial
    is field 3 of the resource string. ``list_resources()`` never opens a
    resource, so no descriptor has been read at that point — trusting only the
    reported field would leave the rail with no serial for the DP2031, the
    DL3031A and the SDM4065A, which is most of this bench.
    """
    s = BenchStatus()
    s.apply_inventory({"devices": [DISCOVERED_PSU]})
    psu = s.to_dict()["slots"]["rigol_dp2031"]
    assert psu["serial_number"] == "DP2A243500269"
    assert psu["label"] == "Rigol DP2000-series power supply"
    assert psu["usb_id"] == "1ab1:a4a8"


def test_the_ni_visa_form_without_an_interface_number_also_yields_a_serial():
    """Which form arrives depends on the backend, not on the instrument.

    The same DP2031 is ``USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR`` under
    NI-VISA and ``USB0::6833::42152::DP2A243500269::0::INSTR`` under pyvisa-py:
    the interface field is optional. A parser that required it would show a
    serial on the bench board (which needs pyvisa-py, having no kernel
    ``usbtmc`` module) and nothing at all on a laptop with NI-VISA — a
    difference in the display that looks like a difference in the hardware.
    """
    s = BenchStatus()
    s.apply_inventory(
        {
            "devices": [
                dict(
                    DISCOVERED_PSU,
                    path="USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR",
                )
            ]
        }
    )
    assert s.to_dict()["slots"]["rigol_dp2031"]["serial_number"] == "DP2A243500269"


@pytest.mark.parametrize(
    "resource",
    [
        "ASRL/dev/ttyS0::INSTR",
        "TCPIP0::192.168.1.5::inst0::INSTR",
    ],
)
def test_a_resource_with_no_serial_field_reports_none_not_a_wrong_string(resource):
    """Only USB resources carry a serial. The board has four ``/dev/ttyS*``
    aliases, so this path is walked on every scan.

    Reading whichever token sits in that position would put ``INSTR`` or an IP
    address on the rail as a serial number. That is worse than a blank: an
    operator can act on a blank, and would act wrongly on something that looks
    like an answer.
    """
    s = BenchStatus()
    s.apply_inventory(
        {"devices": [dict(DISCOVERED_PSU, path=resource, usb_id=None)]}
    )
    assert s.to_dict()["slots"]["rigol_dp2031"]["serial_number"] is None


def test_a_reported_serial_number_beats_the_one_parsed_from_the_path():
    """When the agent read the string descriptors, believe it over a backend's
    rendering of them. The parse is a fallback, not an override — an agent that
    grows the ability to report a serial must not be second-guessed by it.
    """
    s = BenchStatus()
    s.apply_inventory(
        {"devices": [dict(DISCOVERED_PSU, serial_number="FROM-DESCRIPTOR")]}
    )
    psu = s.to_dict()["slots"]["rigol_dp2031"]
    assert psu["serial_number"] == "FROM-DESCRIPTOR"


def test_identity_does_not_outlive_the_session_that_vouched_for_it(live):
    """The dangerous direction, and the reason identity is dropped on disconnect.

    A serial number reads as proof. ``DP2A243500269 ATTACHED`` names one specific
    box, so nobody reads it as a guess — and it stays just as convincing after
    the cable has been pulled, which is exactly when the panel is blind. Presence
    reverting to None already makes the slot say "not scanned"; a confident
    identity sitting beside that undoes it.
    """
    live.apply_inventory({"devices": [DISCOVERED_PSU]})
    attached = live.to_dict()["slots"]["rigol_dp2031"]
    assert attached["serial_number"] == "DP2A243500269", "fixture never identified it"

    live.apply_disconnected("cable pulled")

    blind = live.to_dict()["slots"]["rigol_dp2031"]
    assert blind["present"] is None
    assert blind["serial_number"] is None, (
        "the rail still names a specific instrument nobody has seen since the "
        "link died"
    )
    assert blind["label"] is None
    assert blind["usb_id"] is None


def test_a_scan_that_no_longer_finds_an_instrument_drops_its_identity(live):
    """The same rule for a live session: a scan is authoritative about absence,
    so a slot the scan missed must not keep the identity an earlier scan gave it.
    Without this, unplugging one instrument on a bench that stays connected
    leaves its serial on screen indefinitely.
    """
    live.apply_inventory({"devices": [DISCOVERED_PSU]})
    live.apply_inventory({"devices": []})
    psu = live.to_dict()["slots"]["rigol_dp2031"]
    assert psu["present"] is False
    assert psu["serial_number"] is None
    assert psu["label"] is None
    assert psu["usb_id"] is None


def test_an_inventory_with_no_identity_fields_at_all_still_folds_in(live):
    """An older agent whose ``discover`` predates these fields. The panel must
    lose the identity, not the inventory: presence is the fact the rail cannot
    do without, and raising here would cost the whole scan.
    """
    live.apply_inventory({"devices": [{"device_key": "rigol_dp2031"}, "not a dict"]})
    psu = live.to_dict()["slots"]["rigol_dp2031"]
    assert psu["present"] is True, "the scan was lost, not just its identity"
    assert psu["serial_number"] is None
    assert psu["label"] is None
    assert psu["usb_id"] is None


def test_a_serial_transport_device_falls_back_to_its_product_name(live):
    """The Otii Arc, as the board reports it: ``serial_number`` null and no
    resource string to mine, but ``manufacturer``/``product`` populated. Worth
    showing — a labelled slot with no serial still tells an operator more than a
    bare device key does.
    """
    live.apply_inventory(
        {
            "devices": [
                {
                    "path": "/dev/ttyACM0",
                    "transport": "serial",
                    "device_key": "otii_arc",
                    "label": "",
                    "product": "Arc",
                    "manufacturer": "Qoitech",
                    "usb_id": "0fce:d1e6",
                    "serial_number": None,
                    "confidence": "exact",
                }
            ]
        }
    )
    arc = live.to_dict()["slots"]["otii_arc"]
    assert arc["label"] == "Arc"
    assert arc["usb_id"] == "0fce:d1e6"
    assert arc["serial_number"] is None, "a tty path is not a serial number"


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


def test_a_busy_method_is_not_prefixed_with_the_device_it_already_names(live):
    """Observed on the board: the detail line read
    ``siglent_sdm4065a: siglent_sdm4065a.measure_resistance_4wire``.

    The worker's ``busy_with`` is the label ``WorkerPool.submit`` was given, which
    is already ``f"{key}.{name}"``, so printing it beside the device key doubles
    the key. On a 1080p panel that doubling costs real width next to a name the
    line has just printed.
    """
    live.apply_status(
        status_payload(
            {"otii_arc": idle()},
            workers={
                "siglent_sdm4065a": {
                    "busy_with": "siglent_sdm4065a.measure_resistance_4wire",
                    "depth": 0,
                }
            },
        ),
        now=101.0,
    )
    assert live.busy_devices == {"siglent_sdm4065a": "measure_resistance_4wire"}
    assert live.busy_summary == "siglent_sdm4065a: measure_resistance_4wire"


def test_a_method_name_containing_a_dot_survives_the_prefix_strip(live):
    """``split(".")[-1]`` would have been the two-character version of this fix and
    would silently rename any method with a dot in it. Only the device's own
    prefix is removed, and only when it is actually there — a label that arrives
    unprefixed is passed through rather than guessed at.
    """
    live.apply_status(
        status_payload(
            {"otii_arc": idle()},
            workers={"psu": {"busy_with": "psu.set.output.state", "depth": 0}},
        ),
        now=101.0,
    )
    assert live.busy_devices == {"psu": "set.output.state"}

    live.apply_status(
        status_payload(
            {"otii_arc": idle()},
            workers={"psu": {"busy_with": "bare_method", "depth": 0}},
        ),
        now=102.0,
    )
    assert live.busy_devices == {"psu": "bare_method"}


# --------------------------------------------------------------------------
# What a device was last seen doing
#
# Reported from the bench: during a live 6-setpoint 4-wire resistance sweep the
# sidebar read STANDBY for both instruments for the whole test. ``busy_devices``
# is *sampled* on the 5s status poll while a device call takes ~200ms, so the
# poll essentially never lands inside one. Action events arrive as each call
# completes, so they see every call — and "last seen doing X, N seconds ago" is a
# claim about the past, which stays true as it ages, unlike "busy now".
# --------------------------------------------------------------------------


def action(**overrides):
    """One action event, shaped as the agent's ``build_action_event`` emits it."""
    e = {
        "kind": "action",
        "severity": "debug",
        "method": "device.call",
        "action": "measure_resistance_4wire",
        "device": "siglent_sdm4065a",
        "ok": True,
        "count": 1,
        "detail": "→ 100.038",
    }
    e.update(overrides)
    return e


def test_an_action_event_records_what_that_device_was_last_seen_doing(live):
    """Without this the rail had no source that could see a driven bench at all.

    A 4-wire sweep is a few hundred ~200ms calls with nothing armed and no worker
    busy at poll time, so every readout the panel had said "untouched" while an
    operator watched the DMM's own display change.
    """
    live.apply_event(action(), now=101.0)
    assert live.last_action_at == {"siglent_sdm4065a": 101.0}
    assert live.last_action_name == {"siglent_sdm4065a": "measure_resistance_4wire"}

    # Replaced rather than kept: the field is "last seen". A first-wins version
    # would freeze on the opening call of a sweep and then age out mid-run, so the
    # rail would go quiet while the bench was at its busiest.
    live.apply_event(action(action="measure_resistance_2wire"), now=131.0)
    assert live.last_action_at == {"siglent_sdm4065a": 131.0}
    assert live.last_action_name["siglent_sdm4065a"] == "measure_resistance_2wire"


def test_a_recorded_action_keeps_a_method_reached_through_a_sub_object(live):
    """Naming the wrong call is worse than naming a long one.

    The agent labels a job ``f"{key}.{method}"``, so the key has to come off — but
    a driver method reached through a sub-object arrives as
    ``rigol_dp2031.channel.set_current``, and ``rsplit(".")[-1]`` would put
    ``set_current`` on the rail. The supply has one of those per channel, so the
    shortened name names a call that is not the one that ran.

    ``_strip_device_prefix`` itself is already pinned through ``busy_devices``
    above; what this pins is that the *recording* path goes through it at all,
    because ``last_action_name`` is a second, separately-written field feeding the
    same row.
    """
    live.apply_event(
        action(device="rigol_dp2031", action="rigol_dp2031.channel.set_current"),
        now=101.0,
    )
    assert live.last_action_name == {"rigol_dp2031": "channel.set_current"}
    assert live.to_dict()["last_action"] == {"rigol_dp2031": "channel.set_current"}


def test_a_recorded_action_falls_back_to_the_method_when_there_is_no_action_name(live):
    """Not every verb carries a device-level method: ``agent.open`` grades to an
    empty ``action`` because there is no driver call inside it.

    An open is exactly the event a slot's row wants to show — it is the moment
    STANDBY becomes OPEN — and recording it as an empty string would draw a marker
    that says something happened without saying what, which is the shape of
    readout this panel exists to rule out.
    """
    live.apply_event(action(method="agent.open", action="", device="qr10x"), now=101.0)
    assert live.last_action_name == {"qr10x": "agent.open"}


def test_an_action_with_no_device_records_no_phantom_key(live):
    """``agent.status``, run control and the folded-summary lines name no device.

    The agent sends ``device: ""`` for those rather than omitting the field, so a
    truthiness check is the only thing standing between them and a slot keyed on
    the empty string — a nameless row aging up beside the real instruments.
    """
    live.apply_event(action(device="", action="agent.status"), now=101.0)
    live.apply_event({"kind": "action", "severity": "debug", "count": 5}, now=102.0)

    assert live.last_action_at == {}
    assert live.last_action_name == {}
    assert live.to_dict()["action_age"] == {}
    # The events themselves still landed; only the per-device claim was withheld.
    assert live.actions_seen == 6


def test_to_dict_reports_action_ages_rather_than_another_processes_monotonics(live):
    """A monotonic read on the bench board means nothing on the panel's clock.

    The two processes have unrelated epochs, so shipping the raw stamp would have
    the renderer subtract it from its own clock and print an age of days.
    """
    stamp = time.monotonic() - 3.0
    live.apply_event(action(device="siglent_sdm4065a"), now=stamp)
    live.apply_event(action(device="bench_qr10x"), now=stamp)

    data = live.to_dict()
    assert data["action_age"]["siglent_sdm4065a"] == pytest.approx(3.0, abs=0.5)
    # Exactly equal, which is the assertion: one clock reading for the whole
    # snapshot. A reading per key differs by nanoseconds, and two rails that
    # disagree about what "now" was are two rails that can age their markers out
    # on different frames while describing the same instant.
    assert data["action_age"]["siglent_sdm4065a"] == data["action_age"]["bench_qr10x"]


def test_an_action_age_is_never_reported_as_negative(live):
    """An unclamped age costs the readout entirely, not just its number.

    ``fui/view._recent_action`` withholds the marker for ``age < 0``, so a stamp
    that lands ahead of the snapshot's clock reading would blank the very row this
    field exists to fill. ``time.monotonic()`` cannot go backwards within a
    process, so this is a guard rather than a live path — and a guard that cannot
    be tested is the failure this module exists to prevent.
    """
    live.apply_event(action(), now=time.monotonic() + 5.0)
    assert live.to_dict()["action_age"]["siglent_sdm4065a"] == 0.0


def test_a_last_action_does_not_outlive_the_session_that_observed_it(live):
    """"The DMM was reading 2s ago" is a claim about a session that is gone.

    Kept across a disconnect the age would keep counting up against a feed that
    stopped arriving, so the marker would sit there getting older on a link where
    nothing can arrive to move it — and on reconnect it would be measured from
    before the gap, dating an action to the wrong session entirely.
    """
    live.apply_event(action(), now=101.0)
    assert live.last_action_name == {"siglent_sdm4065a": "measure_resistance_4wire"}

    live.apply_disconnected("the agent closed the connection")

    assert live.last_action_at == {}
    assert live.last_action_name == {}
    data = live.to_dict()
    assert data["action_age"] == {}
    assert data["last_action"] == {}


# --------------------------------------------------------------------------
# The link heartbeat: an idle bench must still prove it is alive
# --------------------------------------------------------------------------


def beat(**overrides):
    """One link heartbeat as the agent emits it."""
    e = {"kind": "link", "severity": "debug", "heartbeat_s": 5.0, "armed": [],
         "observers": 1}
    e.update(overrides)
    return e


def test_a_quiet_bench_goes_stale_without_heartbeats(live):
    """The problem the heartbeat exists to solve.

    An idle bench emits no events: nothing armed, no run, no actions. Without a
    heartbeat the panel cannot tell that from a link that died, so after the
    silence budget it must say so rather than keep showing a reassuring IDLE.
    """
    budget = 5.0 * SILENCE_HEARTBEATS
    assert live.check_silence(now=100.0 + budget - 1.0) is None
    assert live.check_silence(now=100.0 + budget + 1.0) is not None
    assert live.headline == "STALE"


def test_heartbeats_keep_a_quiet_bench_from_going_stale(live):
    """The same span of wall clock, with beats arriving, stays IDLE."""
    budget = 5.0 * SILENCE_HEARTBEATS
    for t in (105.0, 110.0, 115.0, 120.0):
        live.apply_event(beat(), now=t)
    assert live.check_silence(now=100.0 + budget + 5.0) is None
    assert live.headline == "IDLE"
    # Not merely "not stale": the beats actually arrived. The panel's own status
    # polling would satisfy the weaker claim on its own, so asserting freshness
    # without this would pass against a heartbeat that was never implemented.
    assert live.link_beats == 4


def test_a_heartbeat_is_never_written_to_the_log(live):
    """LOG.MGR is 24 visible rows and belongs to the bench, not the link.

    At one beat per 5s a logged heartbeat would evict every real action within
    about two minutes, leaving an operator watching the connection describe
    itself. This is the assertion that keeps that from regressing.
    """
    live.apply_event(
        {"kind": "action", "severity": "debug", "action": "measure", "detail": "100.04"},
        now=101.0,
    )
    for i in range(LOG_LIMIT * 3):
        live.apply_event(beat(), now=102.0 + i)

    assert [e.get("kind") for e in live.log] == ["action"]
    assert live.log[0].get("action") == "measure"
    assert live.link_beats == LOG_LIMIT * 3
    assert not any(e.get("kind") == "link" for e in live.sticky)


def test_a_heartbeat_does_not_touch_arm_state(live):
    """Arm state has one source of truth, and this is not it.

    The heartbeat carries ``armed`` for a consumer that wants it, but folding it
    in here would make two writers for one safety fact — and the quieter one
    eventually contradicts the louder one on screen.
    """
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    assert live.devices["otii_arc"].armed is True

    # A heartbeat that disagrees must not disarm the panel's view.
    live.apply_event(beat(armed=[]), now=102.0)
    assert live.devices["otii_arc"].armed is True
    assert live.headline == "ARMED"


def test_a_heartbeat_updates_the_silence_budget_to_the_agents_live_figure(live):
    """A reconfigured agent reports its new interval on the beat itself.

    The budget should follow that rather than the value captured at connect
    time, or a bench moved to slow heartbeats reads as perpetually stale.
    """
    assert live.heartbeat_s == 5.0
    live.apply_event(beat(heartbeat_s=30.0), now=101.0)
    assert live.heartbeat_s == 30.0
    # 40s of quiet is stale under the old 15s budget, fine under the new 90s one.
    assert live.check_silence(now=141.0) is None


def test_a_malformed_heartbeat_does_not_disturb_the_budget(live):
    """Junk in the field leaves the previous figure standing."""
    for bad in (None, "soon", 0, -5, float("nan")):
        live.apply_event(beat(heartbeat_s=bad), now=101.0)
        assert live.heartbeat_s == 5.0, f"{bad!r} changed the budget"


def test_beats_do_not_survive_a_disconnect(live):
    """A beat count is a claim about *this* link being alive.

    Carrying it across a disconnect would assert beats are arriving on a session
    where by definition none can.
    """
    live.apply_event(beat(), now=101.0)
    assert live.link_beats == 1
    live.apply_disconnected("the agent closed the connection")
    assert live.link_beats == 0
    assert live.to_dict()["link_beats"] == 0


def test_a_timestamp_of_zero_is_a_real_mark(live):
    """Regression: ``if m`` discarded a mark of exactly 0.0.

    ``time.monotonic()`` is never 0.0, so this could not misfire in production —
    but a test using 0.0 as its clock base had its only mark dropped and the
    staleness assertion passed against a view that was never checked. A guard
    that cannot be tested is the failure this module exists to prevent.
    """
    s = BenchStatus()
    s.apply_connected(WELCOME, now=0.0)
    # A snapshot too, so the only thing under test is the 0.0 mark rather than
    # the "waiting for the first status snapshot" message apply_connected leaves
    # behind. Asserting `is not None` alone passed against that leftover text —
    # it could not tell a real staleness verdict from startup boilerplate.
    s.apply_status(status_payload({"otii_arc": idle()}), now=0.0)
    s.apply_event({"kind": "action", "severity": "debug"}, now=0.0)
    assert s.last_event_mono == 0.0
    assert s.last_snapshot_mono == 0.0
    assert s.check_silence(now=0.0) is None

    reason = s.check_silence(now=5.0 * SILENCE_HEARTBEATS + 1.0)
    assert reason is not None and "nothing from the agent" in reason
    assert s.headline == "STALE"


# --------------------------------------------------------------------------
# The bench-pushed presence sweep
#
# Presence used to be knowable only by the *display* calling agent.discover on
# its own timer, which made the panel the cause of a ~1.65s USB scan. The bench
# now pushes the answer, so an instrument appearing or vanishing arrives without
# anyone asking — and a sweep is narrower than a full inventory on purpose: it
# runs probe=False, so it can say the DMM is on the bus and has nothing new to
# say about which DMM it is.
# --------------------------------------------------------------------------


def sweep(present=("siglent_sdm4065a",), served=("siglent_sdm4065a", "bench_qr10x"),
          **overrides):
    """One presence sweep, as ``_presence_sweep`` in the agent publishes it."""
    e = {
        "kind": "presence",
        "severity": "debug",
        "present": list(present),
        "served": list(served),
        "changed": False,
        "first": False,
    }
    e.update(overrides)
    return e


def test_a_presence_sweep_marks_the_bus_without_the_panel_having_asked(live):
    """The push half of "the display never probes".

    A sweep also counts as somebody having looked, so ``inventory_taken`` is set:
    before that, a panel connecting to a bench that had been running for hours
    showed UNSCANNED on every row until its own first ~1.65s scan came back, which
    is the one state the rail cannot render as a fact about the hardware.
    """
    live.apply_event(sweep(), now=101.0)

    assert live.slots["siglent_sdm4065a"].present is True
    assert live.slots["bench_qr10x"].present is False
    assert live.inventory_taken, "a sweep is a scan; the rail still said UNSCANNED"


def test_a_sweep_records_absence_only_for_the_keys_it_actually_looked_for(live):
    """Marking an unconsidered key absent asserts a negative nobody measured.

    A sweep reports the keys the agent serves. A key outside that list — one an
    earlier session's device table left behind, or one only discovery has ever
    seen — was not looked for, and ABSENT on its row would read as "a scan checked
    and it is gone" when no scan checked. None keeps it at UNSCANNED, which is
    true.
    """
    # Planted directly: a bare slot with no presence answer yet is exactly the
    # state under test, and every public path that creates one also fills in a
    # presence, which would mask the thing being asserted.
    live.slots["rigol_dp2031"] = DeviceSlot(key="rigol_dp2031")
    live.apply_event(sweep(), now=101.0)

    assert live.slots["rigol_dp2031"].present is None, (
        "a key the sweep never considered was reported as measured-absent"
    )
    # And the keys it did consider still got their answers, either way.
    assert live.slots["siglent_sdm4065a"].present is True
    assert live.slots["bench_qr10x"].present is False


def test_a_sweep_does_not_blank_the_identity_a_full_inventory_learned(live):
    """A sweep every 30s that cleared identity would make the rail flicker.

    Sweeps run ``probe=False``, so they carry device keys and nothing else — they
    have nothing new to say about a serial number. Clearing it anyway would leave
    the row alternating between naming one specific box and naming none, on a
    bench where nothing changed, and the serial is the only field that answers
    whether the supply attached now is the one the last run was calibrated
    against.
    """
    live.apply_inventory({"devices": [DISCOVERED_PSU]})
    live.apply_event(
        sweep(present=("rigol_dp2031",), served=("rigol_dp2031",)), now=101.0
    )

    psu = live.to_dict()["slots"]["rigol_dp2031"]
    assert psu["present"] is True
    assert psu["serial_number"] == "DP2A243500269", "a sweep blanked the serial number"
    assert psu["label"] == "Rigol DP2000-series power supply"
    assert psu["usb_id"] == "1ab1:a4a8"
    assert psu["confidence"] == "exact"


@pytest.mark.parametrize(
    "bad",
    [
        None,
        "presence",
        {"kind": "presence"},
        {"kind": "presence", "present": "siglent_sdm4065a", "served": []},
        {"kind": "presence", "present": [], "served": "siglent_sdm4065a"},
    ],
)
def test_a_malformed_sweep_costs_this_update_and_nothing_else(live, bad):
    """``apply_presence`` runs on the feed's receive path.

    Raising there drops the session and blanks the screen over a bad optional
    field — and presence is exactly the field to arrive malformed, since the agent
    sending it may be a release ahead or behind the panel. The previous scan's
    answer is left standing rather than cleared, for the same reason a failed
    ``agent.discover`` keeps the last one: flapping a row between ATTACHED and NOT
    FOUND makes the rail unreadable.
    """
    live.apply_inventory({"devices": [DISCOVERED_PSU]})
    live.apply_presence(bad, now=101.0)

    psu = live.to_dict()["slots"]["rigol_dp2031"]
    assert psu["present"] is True, "a malformed sweep threw away a real measurement"
    assert psu["serial_number"] == "DP2A243500269"
    assert live.presence_sweeps == 0, "a sweep that said nothing was counted anyway"
    assert live.last_presence_mono is None


def test_sweeps_are_counted_and_stamped_so_the_panel_can_prove_it_is_being_told(live):
    """Not merely "presence is known": known *because the bench keeps saying so*.

    The panel's own inventory poll would satisfy the weaker claim on its own, so
    without a counter this whole mechanism could be absent and every presence
    assertion would still pass. The stamp is kept apart from the general freshness
    mark because a sweep answers a much slower question, and folding it in would
    make a half-minute-old bus inventory look as current as a 5s status poll.
    """
    live.apply_event(sweep(), now=101.0)
    live.apply_event(sweep(), now=131.0)

    assert live.presence_sweeps == 2
    assert live.last_presence_mono == 131.0
    assert live.to_dict()["presence_sweeps"] == 2


def test_an_unchanged_sweep_is_not_written_to_the_log(live):
    """LOG.MGR shows 24 rows and belongs to the bench, not to its bookkeeping.

    An unchanged sweep is the bench saying "still four instruments" every 30s.
    Logged, it would evict every real bench action within about twelve minutes —
    the same trap a logged heartbeat sets, arriving from a second source.
    """
    live.apply_event(action(), now=101.0)
    for i in range(LOG_LIMIT * 3):
        live.apply_event(sweep(), now=102.0 + i * 30.0)

    assert [e.get("kind") for e in live.log] == ["action"]
    assert live.presence_sweeps == LOG_LIMIT * 3, "the sweeps were dropped, not just unlogged"
    assert not any(e.get("kind") == "presence" for e in live.sticky)


def test_a_sweep_that_found_a_change_does_reach_the_log(live):
    """The other half, and the reason the early return is conditional.

    An instrument leaving the bus mid-session is a real bench event — the agent
    grades it ``warn`` for exactly that reason — and it is the one an operator
    needs to see in the log next to whatever failed right after it. Suppressing
    every sweep to keep the log clean would silently discard it.
    """
    live.apply_event(sweep(present=(), changed=True, severity="warn"), now=101.0)

    assert [e.get("kind") for e in live.log] == ["presence"]
    assert live.slots["siglent_sdm4065a"].present is False
    assert live.presence_sweeps == 1


def test_sweep_bookkeeping_does_not_outlive_the_session_that_produced_it(live):
    """A sweep count is a claim that the bench is confirming its hardware *to us*.

    Carried across a disconnect it would say the bench is still checking in on a
    link where nothing can arrive — and the presence it vouched for goes with it,
    because a slot claiming ATTACHED after the cable was pulled while the panel
    was blind is a stale claim about the physical bench rather than about the
    display, which is the direction that gets someone hurt.
    """
    live.apply_event(sweep(), now=101.0)
    assert live.presence_sweeps == 1

    live.apply_disconnected("the agent closed the connection")

    assert live.presence_sweeps == 0
    assert live.last_presence_mono is None
    assert live.to_dict()["presence_sweeps"] == 0
    assert live.slots["siglent_sdm4065a"].present is None, (
        "a slot still claims ATTACHED on a link nobody can see down"
    )

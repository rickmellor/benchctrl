"""Read-only observer sessions: a status display must not change the bench.

The HDMI dashboard is the motivating consumer. It runs in its own process on
the bench board and talks to the agent over the network like any other client,
so "read-only" has to be a protocol property rather than an in-process
convention.

The load-bearing test in here is
``test_an_observer_polling_does_not_keep_an_armed_bench_alive``. Before the
observer role existed, ``_serve`` called ``governor.touch()`` on *every*
inbound frame — the docstring on ``Governor.touch`` says "Any inbound frame
counts" — so a display polling once a second would pin
``seconds_since_contact`` near zero. Since
``should_trip() == any_armed and seconds_since_contact > deadman_s``, the
deadman could then never fire, and the *status display* would be the reason an
armed bench stayed armed.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import OBSERVER_METHODS, AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient
from benchctrl.net.errors import PolicyError
from benchctrl.sim import SimulatedOtiiArc

TOKEN = "observer-test-token"


@pytest.fixture()
def bench():
    """An agent with a simulated Arc, a normal client, and an observer."""
    from benchctrl.drivers.otii_arc import OtiiArc

    sim = SimulatedOtiiArc()
    sim.start()
    smu = OtiiArc.open(sim.port)

    registry = DeviceRegistry()
    registry.register_open("otii_arc", smu)
    # deadman_s is short so the trip is observable inside a test.
    agent = BenchAgent(registry, token=TOKEN, deadman_s=1.0, heartbeat_s=0.3)
    server = AgentServer(agent, host="127.0.0.1", port=0).start()

    def endpoint():
        return EndpointConfig(
            host="127.0.0.1",
            port=server.port,
            token=TOKEN,
            heartbeat_s=0.3,
            deadman_s=1.0,
        )

    clients = []

    def connect(*, observer=False):
        c = RemoteClient(endpoint(), observer=observer).connect()
        clients.append(c)
        return c

    try:
        yield type(
            "Bench",
            (),
            {
                "sim": sim,
                "smu": smu,
                "agent": agent,
                "server": server,
                "connect": staticmethod(connect),
            },
        )
    finally:
        for c in clients:
            with contextlib.suppress(Exception):  # teardown
                c.close()
        server.stop()
        sim.close()


# --------------------------------------------------------------------------
# The safety invariant
# --------------------------------------------------------------------------


def _poll_until(client, deadline, predicate, *, interval=0.05):
    """Poll ``client.status()`` until ``predicate()`` or ``deadline``.

    Returns ``(predicate_result, polls)``. Polling is the whole point of the
    test — a display that sat silent would prove nothing about the deadman.
    """
    polls = 0
    hit = False
    while time.monotonic() < deadline:
        client.status()
        polls += 1
        if predicate():
            hit = True
            break
        time.sleep(interval)
    return hit, polls


def test_an_observer_polling_does_not_keep_an_armed_bench_alive(bench):
    """The reason the observer role exists.

    An armed bench with only an observer connected must still be driven safe by
    the deadman. If this fails, an always-on HDMI panel silently disables the
    interlock that protects an unattended run.

    Asserted against the *trip log* rather than ``should_trip()``: the deadman
    thread is live, so by the time the test looks, a successful trip has
    already cleared ``output_armed`` and ``should_trip()`` has gone back to
    False. Waiting for the trip itself is both race-free and the stronger
    claim — it proves the bench was actually made safe, not merely that it was
    eligible.
    """
    agent = bench.agent
    observer = bench.connect(observer=True)

    # Arm a device directly on the governor: this test is about the deadman,
    # not about how arming happens.
    agent.governor.state_for("otii_arc").output_armed = True
    assert agent.governor.any_armed

    def tripped():
        return any(
            t["reason"] == "heartbeat_lost" for t in agent.governor.status()["trips"]
        )

    deadline = time.monotonic() + (agent.deadman_s * 6)
    hit, polls = _poll_until(observer, deadline, tripped)

    assert polls > 10, f"only managed {polls} polls; the test proves nothing"
    assert hit, (
        f"an armed bench was never tripped despite {polls} observer polls over "
        f"more than {agent.deadman_s:.1f}s — the deadman has been defeated by "
        f"the status display (last contact "
        f"{agent.governor.seconds_since_contact:.2f}s ago)"
    )
    assert not agent.governor.any_armed, "the trip did not disarm the device"


def test_a_normal_client_does_still_feed_the_deadman(bench):
    """The complement, so the test above is not vacuously true.

    A real operator client must keep the bench alive — that is the whole point
    of the heartbeat. Without this test, deleting ``touch()`` outright would
    leave the suite green.
    """
    agent = bench.agent
    client = bench.connect()

    agent.governor.state_for("otii_arc").output_armed = True

    def tripped():
        return bool(agent.governor.status()["trips"])

    deadline = time.monotonic() + (agent.deadman_s * 3)
    hit, polls = _poll_until(client, deadline, tripped)

    assert polls > 10, f"only managed {polls} polls; the test proves nothing"
    assert not hit, (
        f"a normal client polling every 50ms was tripped anyway after {polls} "
        f"polls — its traffic is not registering as operator contact"
    )
    assert agent.governor.seconds_since_contact < agent.deadman_s
    assert agent.governor.any_armed


# --------------------------------------------------------------------------
# Read-only enforcement
# --------------------------------------------------------------------------


def test_an_observer_may_read_status(bench):
    observer = bench.connect(observer=True)
    status = observer.status()
    assert "safety" in status
    assert "workers" in status


def test_an_observer_is_told_its_role_in_the_welcome(bench):
    """A client should not have to trigger a PolicyError to learn its role."""
    observer = bench.connect(observer=True)
    assert observer.welcome.get("observer") is True

    normal = bench.connect()
    assert normal.welcome.get("observer") is False


def test_an_observer_cannot_open_a_device(bench):
    observer = bench.connect(observer=True)
    with pytest.raises(PolicyError, match="observer"):
        observer.call("agent.open", {"device": "otii_arc", "open": {}})


def test_an_observer_cannot_claim_a_device(bench):
    observer = bench.connect(observer=True)
    with pytest.raises(PolicyError, match="observer"):
        observer.call("agent.claim", {"device": "otii_arc"})


def test_an_observer_cannot_call_a_device_method(bench):
    """The one that would actually move hardware."""
    observer = bench.connect(observer=True)
    with pytest.raises(PolicyError, match="observer"):
        observer.call(
            "device.call",
            {"device": "otii_arc", "method": "set_output", "args": [True]},
        )


def test_an_observer_cannot_abort_a_run(bench):
    """Read-only means read-only even for actions that sound protective.

    `run.abort` makes the bench *safer*, so it is tempting to allow. It is
    still a write, and the e-stop is a separate deliberate mechanism rather
    than a side effect of the status display's permissions.
    """
    observer = bench.connect(observer=True)
    with pytest.raises(PolicyError, match="observer"):
        observer.call("run.abort", {"run_id": "whatever", "reason": "test"})


def test_the_observer_allowlist_contains_no_mutating_verbs():
    """A guard against the allowlist growing something dangerous.

    Pins the *shape* of the allowlist rather than its exact contents, so
    adding another genuinely read-only method does not fail the suite but
    adding `agent.open` does.
    """
    forbidden_prefixes = ("agent.open", "agent.close", "agent.claim", "agent.release")
    forbidden_substrings = ("abort", "submit", "start", "stop", "write", "set")
    for method in OBSERVER_METHODS:
        assert not method.startswith(forbidden_prefixes), f"{method} mutates"
        for bad in forbidden_substrings:
            assert bad not in method, f"{method} looks like a write ({bad!r})"


def test_an_unknown_method_is_refused_for_an_observer(bench):
    """Allowlist, not denylist: anything unrecognised defaults to forbidden."""
    observer = bench.connect(observer=True)
    with pytest.raises(PolicyError, match="observer"):
        observer.call("some.future.method", {})


def test_a_normal_client_is_unaffected_by_the_allowlist(bench):
    """The gate must apply only to observers."""
    client = bench.connect()
    described = client.call("agent.open", {"device": "otii_arc", "open": {}})
    assert described


# --------------------------------------------------------------------------
# Events still reach an observer
# --------------------------------------------------------------------------


def test_an_observer_receives_events(bench):
    """Read-only must not mean deaf: the panel is driven by events."""
    observer = bench.connect(observer=True)
    received = []
    observer.on_event = received.append

    bench.agent._broadcast_event({"kind": "test_event", "severity": "info"})

    deadline = time.monotonic() + 2.0
    while time.monotonic() < deadline and not received:
        time.sleep(0.01)
    assert any(e.get("kind") == "test_event" for e in received), (
        f"observer got {received}"
    )


def test_an_observer_receives_a_safety_trip_event(bench):
    """The event the panel most needs, end to end over the wire."""
    observer = bench.connect(observer=True)
    received = []
    observer.on_event = received.append

    bench.agent.governor.state_for("otii_arc").output_armed = True
    from benchctrl.agent.safety import TripReason

    bench.agent.trip(TripReason.OPERATOR)

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        if any(e.get("kind") == "safety_trip" for e in received):
            break
        time.sleep(0.01)

    trip = [e for e in received if e.get("kind") == "safety_trip"]
    assert trip, f"no safety_trip reached the observer; got {received}"
    assert trip[0]["severity"] == "critical"


def test_each_session_gets_its_own_event_subscriber(bench):
    """Per-session queues are what stop one slow consumer starving another."""
    bench.connect(observer=True)
    bench.connect()

    names = {s["name"] for s in bench.agent.events.stats()["subscribers"]}
    assert len(names) >= 2, f"expected a subscriber per session, got {names}"


def test_an_observers_queue_is_shallow_and_droppable(bench):
    """A display falls behind by design; it must be the one that sheds."""
    from benchctrl.agent.eventbus import DEFAULT_MAX_QUEUE, DISPLAY_MAX_QUEUE

    observer = bench.connect(observer=True)
    normal = bench.connect()

    subs = {s["name"]: s for s in bench.agent.events.stats()["subscribers"]}
    obs_sub = subs[observer.welcome["session"]]
    normal_sub = subs[normal.welcome["session"]]

    assert obs_sub["droppable"] is True
    assert obs_sub["max_queue"] == DISPLAY_MAX_QUEUE
    assert normal_sub["droppable"] is False
    assert normal_sub["max_queue"] == DEFAULT_MAX_QUEUE


def test_dropping_a_session_removes_its_subscriber(bench):
    """A closed session must not leave a sender thread behind."""
    observer = bench.connect(observer=True)
    session_id = observer.welcome["session"]
    assert any(
        s["name"] == session_id for s in bench.agent.events.stats()["subscribers"]
    )

    observer.close()

    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        names = {s["name"] for s in bench.agent.events.stats()["subscribers"]}
        if session_id not in names:
            break
        time.sleep(0.02)
    names = {s["name"] for s in bench.agent.events.stats()["subscribers"]}
    assert session_id not in names, f"subscriber leaked: {names}"

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
import json
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


# --------------------------------------------------------------------------
# The action log: every discrete thing the bench did
# --------------------------------------------------------------------------
#
# The LOG.MGR pane used to be almost always empty, because the only event kinds
# anything produced were safety_trip, safety_failed and events_dropped — three
# things that on a healthy bench never happen. `_handle_request` now emits one
# event per dispatched method, which is the chokepoint every remote action passes
# through.


def _collect(observer):
    """Attach a sink to ``observer`` and return the list it appends to."""
    received: list = []
    observer.on_event = received.append
    return received


def _wait_for(received, predicate, *, timeout=3.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if any(predicate(e) for e in list(received)):
            return True
        time.sleep(0.01)
    return False


def _actions(received):
    return [e for e in list(received) if str(e.get("kind", "")).startswith("action")]


def test_an_observer_receives_action_events_for_what_another_client_did(bench):
    """The whole point of the pane: a display shows what the bench is doing.

    Read-only and pushed-to, so the panel learns about a device call it has no
    permission to make and without polling for it.
    """
    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    client.call(
        "device.call",
        {"device": "otii_arc", "method": "set_voltage", "args": [3.3]},
    )

    assert _wait_for(received, lambda e: e.get("action") == "set_voltage"), (
        f"no action event for the call; got {[e.get('kind') for e in received]}"
    )
    event = next(e for e in _actions(received) if e.get("action") == "set_voltage")
    assert event["kind"] == "action"
    assert event["device"] == "otii_arc"
    assert event["method"] == "device.call"
    assert event["ok"] is True
    assert "3.3" in event["detail"]


def test_a_failed_action_reaches_the_panel_and_is_more_severe(bench):
    """A refused or raising call is the half of the log worth reading.

    Graded above the same verb succeeding, so it survives back-pressure that the
    success would not.
    """
    from benchctrl.agent.eventbus import rank_of

    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    client.call(
        "device.call", {"device": "otii_arc", "method": "set_voltage", "args": [3.3]}
    )
    # A method the driver does not have: refused by the dispatch allowlist.
    with pytest.raises(Exception):  # noqa: B017 - any refusal will do
        client.call("device.call", {"device": "otii_arc", "method": "no_such_method"})

    assert _wait_for(received, lambda e: e.get("kind") == "action_failed"), (
        f"a failed call produced no event; got {[e.get('kind') for e in received]}"
    )
    failed = next(e for e in _actions(received) if e["kind"] == "action_failed")
    ok = next(e for e in _actions(received) if e.get("action") == "set_voltage")
    assert failed["ok"] is False
    assert failed["error"], "a failure with no error text says nothing"
    assert rank_of(failed) > rank_of(ok), (
        f"a failed action ({failed['severity']}) is not more severe than a "
        f"successful one ({ok['severity']})"
    )


def test_an_arming_call_is_logged_more_severely_than_a_read(bench):
    """Arm/disarm are the lines an incident review needs; reads are noise."""
    from benchctrl.agent.eventbus import rank_of

    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    client.call(
        "device.call", {"device": "otii_arc", "method": "set_output", "args": [True]}
    )
    client.call("device.getprops", {"device": "otii_arc"})

    assert _wait_for(received, lambda e: e.get("action") == "set_output")
    assert _wait_for(received, lambda e: e.get("method") == "device.getprops")
    arm = next(e for e in _actions(received) if e.get("action") == "set_output")
    read = next(e for e in _actions(received) if e["method"] == "device.getprops")
    assert rank_of(arm) > rank_of(read)


def test_the_auth_token_never_appears_in_an_event(bench):
    """The agent holds a shared secret; the bus fans out to every session.

    An observer is authenticated but is not the operator's client, and the pane it
    feeds is on a wall. The token must not be reconstructible from the log — so it
    is asserted against the *whole* event stream, including the handshake-adjacent
    verbs and a failure's exception text, rather than against one field.
    """
    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    client.call("agent.hello", {})
    client.call("agent.status", {})
    client.call(
        "device.call", {"device": "otii_arc", "method": "set_voltage", "args": [3.3]}
    )
    with pytest.raises(Exception):  # noqa: B017 - we want the error text logged
        client.call("device.call", {"device": "otii_arc", "method": "no_such_method"})

    _wait_for(received, lambda e: e.get("kind") == "action_failed")
    blob = json.dumps(list(received))
    assert TOKEN not in blob, "the agent's token reached the event bus"
    assert bench.agent.token == TOKEN, "fixture no longer proves anything"


def test_a_successful_observer_call_is_not_logged_as_a_bench_action(bench):
    """The pane must not fill with the display describing itself.

    The dashboard polls agent.status/devices/discover on its own timer. Logging
    those would be a feedback loop: at 24 visible rows the panel would show
    nothing but its own polling within seconds, pushing every real bench action
    off the glass. An observer also cannot *do* anything — the allowlist is
    read-only by construction — so there is no bench action to lose.
    """
    observer = bench.connect(observer=True)
    received = _collect(observer)
    for _ in range(5):
        observer.status()
        observer.call("agent.devices", {})

    # Something else must arrive, or this proves only that events are broken.
    bench.agent._broadcast_event({"kind": "canary", "severity": "info"})
    assert _wait_for(received, lambda e: e.get("kind") == "canary")

    polls = [
        e
        for e in _actions(received)
        if e.get("method") in ("agent.status", "agent.devices") and e.get("ok")
    ]
    assert polls == [], f"the observer's own polling was logged: {polls}"


def test_a_refused_observer_write_is_still_logged(bench):
    """A read-only session attempting a write is exactly what the pane should show.

    The success case is excluded as self-description; the failure case is not, and
    collapsing the two would hide an attempt to drive the bench from a session
    that is not allowed to.
    """
    observer = bench.connect(observer=True)
    received = _collect(observer)
    with pytest.raises(PolicyError):
        observer.call(
            "device.call",
            {"device": "otii_arc", "method": "set_output", "args": [True]},
        )

    assert _wait_for(received, lambda e: e.get("kind") == "action_failed"), (
        f"a refused write was not logged; got {[e.get('kind') for e in received]}"
    )
    event = next(e for e in _actions(received) if e["kind"] == "action_failed")
    assert event["action"] == "set_output"
    assert "observer" in event["error"]


def test_a_burst_of_identical_reads_is_folded_rather_than_flooding(bench):
    """Volume control, end to end.

    A run polling an instrument can do thousands of transactions a second against
    a pane of 24 rows. Repeats are folded at the producer into a count, and the
    agent reports how many it folded, so the log is a *declared* summary rather
    than a silently truncated one.
    """
    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    for _ in range(40):
        client.call("device.getprops", {"device": "otii_arc"})

    assert _wait_for(received, lambda e: e.get("method") == "device.getprops")
    stats = bench.agent.actions.stats()
    assert stats["folded"] > 0, (
        f"40 identical reads folded nothing: {stats} — the coalescer is inert"
    )
    assert stats["emitted"] < 40, f"every read got its own line: {stats}"


def test_a_recorded_waveform_never_lands_in_a_log_line(bench):
    """A blob must not ride the event bus to a wall panel.

    `read_raw` returns an encoded measurement — hundreds of thousands of samples,
    base64 on the wire. A log line rendering it would both be unreadable and cost
    the bench real allocation per read, in a loop, on the board.
    """
    from benchctrl.agent.server import ACTION_DETAIL_CHARS

    observer = bench.connect(observer=True)
    received = _collect(observer)
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})
    client.call("device.call", {"device": "otii_arc", "method": "read_raw", "args": [0.2]})

    assert _wait_for(received, lambda e: e.get("action") == "read_raw")
    event = next(e for e in _actions(received) if e.get("action") == "read_raw")
    assert len(event["detail"]) <= ACTION_DETAIL_CHARS, (
        f"detail is {len(event['detail'])} chars: {event['detail'][:80]!r}"
    )
    # And it must not be a *prefix of the payload* dressed up as a summary.
    assert "b64[" in event["detail"] or "→" in event["detail"]


# --------------------------------------------------------------------------
# The action event itself: grading, truncation, redaction
# --------------------------------------------------------------------------
#
# No sockets and no bench: build_action_event is pure, so what the log is allowed
# to claim is asserted directly rather than fished out of a stream.


def test_a_container_result_is_reduced_to_its_shape_not_rendered():
    """Truncation happens *before* formatting, and that is the point.

    ``_clip(str(value))`` would be a bug with a measurable cost: a recorded window
    is hundreds of thousands of samples, and rendering it to a string only to keep
    40 characters allocates megabytes on the request path — per read, in a loop, on
    a board with 2 GB of RAM.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "d", "method": "read_raw", "args": [0.1]},
        ok=True,
        result=list(range(262_144)),
    )
    assert "list[262144]" in event["detail"]
    assert "262143" not in event["detail"], "the container was rendered, not described"


def test_an_encoded_measurement_is_described_not_pasted():
    """A `device.call` result reaches the log already *encoded*.

    So a recording arrives as ``{"__t": "b64", "v": "<megabytes of base64>"}``.
    Rendered generically that put 40 characters of base64 on the glass — observed,
    not hypothesised — which is worse than useless because it looks like data.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "d", "method": "read_raw", "args": [0.1]},
        ok=True,
        result={"__t": "b64", "v": "QUJD" * 262_144},
    )
    assert "b64[" in event["detail"]
    assert "QUJDQUJD" not in event["detail"], "base64 payload leaked into a log line"


def test_summarising_a_huge_value_is_bounded_work_not_a_full_scan():
    """The cost, not just the output length.

    A summariser that scanned its input would be correct and still wrong: it runs
    on the request path of every call, and the input can be megabytes.
    """
    from benchctrl.agent.server import ACTION_DETAIL_CHARS, build_action_event

    small = build_action_event(
        "s", "device.call", {"device": "d", "method": "q"}, ok=True, result="x" * 64
    )
    huge = build_action_event(
        "s",
        "device.call",
        {"device": "d", "method": "q"},
        ok=True,
        result="x" * 8_000_000,
    )
    assert len(huge["detail"]) <= ACTION_DETAIL_CHARS
    # Both bounded by the same limit, so the 8 MB input produced no more output
    # than the 64-byte one — the work did not scale with the payload.
    assert len(huge["detail"]) <= len(small["detail"]) + 2


def test_a_credential_shaped_kwarg_is_redacted_from_a_log_line():
    """The agent's own token cannot reach here — authentication happens before
    routing — so this is about a *device* method that takes a credential. Nothing
    in tree does today, and a log line on a wall panel is a bad place to discover
    the first one.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "device.call",
        {
            "device": "d",
            "method": "connect_wifi",
            "kwargs": {"ssid": "bench", "password": "hunter2", "token": "abc123"},
        },
        ok=True,
        result=True,
    )
    assert "hunter2" not in event["detail"]
    assert "abc123" not in event["detail"]
    assert "ssid" in event["detail"], "redaction ate the whole line"


def test_a_positionally_passed_secret_is_redacted_not_merely_clipped():
    """``login(tok)`` is how that call is written, and the kwarg guard cannot see it.

    The observed failure: a 43-character agent token passed as ``args[0]`` rendered
    39 of its characters into ``detail``. It looked bounded only because the value
    clip had trimmed it, and truncation is not redaction — a test asserting the
    field's *length* would have passed against that bug. So this asserts the
    string's ABSENCE, and every long prefix of it, from the whole event.
    """
    from benchctrl.agent.server import build_action_event

    token = "Z0y5Kflnuj2s6c3fHn7IYk19X-s82ZB0f0EsobFdcF4"
    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "psu", "method": "login", "args": [token]},
        ok=True,
        result=None,
    )
    blob = repr(event)
    assert token not in blob
    # Prefixes, because a clip leaks a prefix and nothing else. 12 characters of a
    # 43-character urlsafe token is already most of the way to guessable.
    for cut in range(12, len(token)):
        assert token[:cut] not in blob, f"leaked the first {cut} characters"
    assert "login" in blob, "redaction hid which action was attempted"


def test_a_short_positional_credential_is_redacted_by_the_methods_name():
    """The case the shape check cannot see, so the case that tests the method grade.

    ``login("pin1234")`` is indistinguishable *as a value* from a reading or an SCPI
    string — it is short, and no shape rule can call it a secret without also
    silencing half the log. Only the fact that it was passed to ``login`` makes it
    one. Written because removing the method grade left every other redaction test
    still passing: the long-token tests are caught by the shape check too, so they
    could not tell the two guards apart.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "d", "method": "login", "args": ["pin1234"], "kwargs": {}},
        ok=True,
        result=True,
    )
    assert "pin1234" not in repr(event)
    assert "login" in repr(event), "the attempt itself must still be on the record"


def test_a_secret_shaped_positional_arg_is_redacted_under_an_innocent_method_name():
    """The backstop for the case grading by method name cannot catch.

    ``_is_credential_method`` reads the method's name; a token handed to a method
    whose name says nothing would sail past it. The shape check is what closes
    that, so it is tested through a method name with no credential hint at all.
    """
    from benchctrl.agent.server import build_action_event

    token = "Z0y5Kflnuj2s6c3fHn7IYk19X-s82ZB0f0EsobFdcF4"
    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "psu", "method": "set_label", "args": [token]},
        ok=True,
        result=None,
    )
    assert token not in repr(event)
    assert token[:12] not in repr(event)
    assert "43c" in event["detail"], "the length is the informative part that is left"


def test_an_ordinary_string_argument_still_reaches_the_log():
    """The redaction must not be so eager it empties the pane.

    Every string a driver signature actually passes in this tree is short and
    readable (``RESistance``, ``CONTinuous``); a shape check that swallowed those
    would trade a leak for a blank log, which is the failure this whole panel
    exists to prevent.
    """
    from benchctrl.agent.server import build_action_event

    for value in ("RESistance", "CONTinuous", "auto", "OFF", "siglent_sdm4065a"):
        event = build_action_event(
            "s-1",
            "device.call",
            {"device": "d", "method": "set_mode", "args": [value]},
            ok=True,
            result=True,
        )
        assert value in event["detail"], f"redaction ate a legitimate argument: {value}"


def test_a_credential_methods_result_is_withheld_by_grade_not_by_length():
    """A short secret fits inside every clip this module has.

    ``get_token()`` returning an 8-character PIN would render whole, because a
    length bound is no protection against a bounded-length secret. Grading the
    method is what makes withholding a decision rather than an accident of size.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "d", "method": "get_token", "args": []},
        ok=True,
        result="1234PIN9",
    )
    assert "1234PIN9" not in repr(event)


def test_no_driver_method_in_tree_is_graded_as_a_credential_method():
    """The redaction must cost no real log line on this bench.

    Grading by name is a guess, and the way that guess goes wrong is by matching a
    method the bench actually calls — ``set_keyboard_lock`` contains "key" as a
    substring, and a substring rule would have silenced it. Segment matching is
    what makes it safe, and this asserts that against every driver in tree rather
    than against the two examples I thought of.
    """
    import pathlib
    import re

    from benchctrl.agent.server import _is_credential_method

    root = pathlib.Path(__import__("benchctrl").__file__).parent / "drivers"
    names = set()
    for path in root.rglob("*.py"):
        names.update(re.findall(r"^    def ([a-z][a-z0-9_]*)", path.read_text(), re.M))
    assert len(names) > 100, "found no driver methods; the sweep proved nothing"
    graded = sorted(n for n in names if _is_credential_method(n))
    assert graded == [], f"would silence real bench methods: {graded}"


def test_an_encoded_dataclass_names_its_class_rather_than_its_tag():
    """Observed on the board: ``info`` logged the two characters ``dc``.

    ``device.call``'s result reaches the log already wire-encoded, and
    ``{"__t": "dc", "c": ..., "f": ...}`` has exactly three keys — small enough that
    the generic dict branch rendered it inline, with the tag first. Every
    dataclass-returning method on the bench produced the same meaningless line.
    """
    from benchctrl.net.codec import TAG

    from benchctrl.agent.server import build_action_event

    encoded = {
        TAG: "dc",
        "c": "SDM4065AInfo",
        "f": {"model": "SDM4065A", "serial": "SDM36HBD800123", "fw": "1.01"},
    }
    event = build_action_event(
        "s-1",
        "device.call",
        {"device": "siglent_sdm4065a", "method": "info"},
        ok=True,
        result=encoded,
    )
    assert "SDM4065AInfo" in event["detail"]
    assert event["detail"].strip() != "→ dc"


def test_no_codec_tag_renders_as_the_bare_tag_name():
    """A two-letter tag is not a log line, and the codec has several tags.

    Written as a sweep of the codec's own tag vocabulary rather than of the tags I
    happened to fix, because the failure mode here is a tag added later quietly
    reintroducing ``→ dc``. Reads the tags out of ``codec.py`` so a new one shows up
    here as a failure instead of as an uninformative row on the panel.
    """
    import pathlib
    import re

    from benchctrl.net.codec import TAG

    from benchctrl.agent.server import _summarise

    source = pathlib.Path(__import__("benchctrl.net.codec", fromlist=["x"]).__file__)
    tags = set(re.findall(r"TAG:\s*\"([a-z0-9]+)\"", source.read_text()))
    assert {"dc", "rec", "map", "iter", "b64", "blob", "enum"} <= tags, tags
    for tag in sorted(tags):
        rendered = _summarise({TAG: tag, "v": "x", "c": "C", "f": {}, "id": 1, "items": []})
        assert rendered != tag, f"tag {tag!r} renders as nothing but its own name"


def test_an_open_reports_which_driver_class_took_the_device():
    """``agent.open → dict[8]`` was honest and told an operator nothing.

    The interesting fact about an open is which driver class claimed the device,
    because that is what a wrong-instrument mistake looks like from across a bench.
    """
    from benchctrl.agent.server import build_action_event

    event = build_action_event(
        "s-1",
        "agent.open",
        {"device": "siglent_sdm4065a"},
        ok=True,
        result={
            "key": "siglent_sdm4065a",
            "open": True,
            "cls": "SiglentSDM4065A",
            "methods": [],
            "properties": [],
            "special": [],
            "snapshot_props": [],
            "mutators": [],
        },
    )
    assert "SiglentSDM4065A" in event["detail"]


def test_no_action_severity_ever_reaches_critical():
    """The ceiling, asserted over every verb the agent routes.

    ``critical`` is what ``safety_trip`` uses, and the bus refuses an incoming
    event when nothing queued ranks below it. An action graded ``critical`` could
    therefore fill a display queue with chatter that a trip cannot evict its way
    into. Swept over the whole verb table rather than spot-checked, so a verb added
    later is covered.
    """
    from benchctrl.agent.eventbus import SEVERITY_RANK
    from benchctrl.agent.server import (
        ACTION_SEVERITY_CEILING,
        OBSERVER_METHODS,
        action_severity,
    )

    methods = set(OBSERVER_METHODS) | {
        "agent.open",
        "agent.close",
        "agent.claim",
        "agent.release",
        "device.call",
        "device.getprops",
        "device.read_window",
        "blob.fetch",
        "blob.release",
        "rec.start",
        "rec.stop",
        "rec.stats",
        "rec.release",
        "run.submit",
        "run.abort",
        "run.artifacts",
        "run.fetch_chunk",
        "iter.open",
        "iter.next",
        "iter.close",
        "some.future.verb",
    }
    device_methods = ("", "read_raw", "set_voltage", "set_output", "disable_output")
    ceiling = SEVERITY_RANK[ACTION_SEVERITY_CEILING]
    for method in sorted(methods):
        for action in device_methods:
            for ok in (True, False):
                severity = action_severity(method, action, ok=ok)
                rank = SEVERITY_RANK[severity]
                assert rank <= ceiling, (
                    f"{method}/{action} ok={ok} graded {severity!r}, above the "
                    f"{ACTION_SEVERITY_CEILING!r} ceiling"
                )
                assert rank < SEVERITY_RANK["critical"], (
                    f"{method}/{action} ok={ok} graded {severity!r}: it could "
                    f"crowd out a safety_trip"
                )


def test_the_logs_idea_of_arming_comes_from_the_governors_own_table():
    """Restating the arming methods here would let them drift silently.

    The drift is invisible: the panel would keep grading ``set_output`` as
    ordinary chatter while the bench armed on it, and the line an incident review
    needs would be the first thing shed.
    """
    from benchctrl.agent.safety import _ARMING_CALLS
    from benchctrl.agent.server import ACTION_SEVERITY_READ, action_severity

    assert _ARMING_CALLS, "the governor's arming table is empty; this proves nothing"
    for method in _ARMING_CALLS:
        assert action_severity("device.call", method) != ACTION_SEVERITY_READ, (
            f"{method} arms the bench but is graded as a value read"
        )


def test_a_coalescer_always_emits_the_first_action_of_a_burst():
    """No action is ever invisible.

    Folding collapses *repeats*; the first of a signature always gets its own
    line, so a one-off call can never be swallowed by a window opened by an
    earlier one.
    """
    from benchctrl.agent.server import ActionCoalescer, build_action_event

    coalescer = ActionCoalescer(window_s=10.0)

    def event(action):
        return build_action_event(
            "s", "device.call", {"device": "d", "method": action}, ok=True, result=1.0
        )

    assert coalescer.offer(event("read_raw"), now=0.0) is not None
    assert coalescer.offer(event("read_raw"), now=1.0) is None
    # A different action inside the same window is a different signature.
    assert coalescer.offer(event("read_voltage"), now=1.0) is not None


def test_folding_reports_the_repeats_it_absorbed():
    """A count, not a silence. "read voltage ×47" instead of 46 vanished lines."""
    from benchctrl.agent.server import ActionCoalescer, build_action_event

    coalescer = ActionCoalescer(window_s=1.0)

    def event():
        return build_action_event(
            "s", "device.call", {"device": "d", "method": "read_raw"}, ok=True, result=1.0
        )

    first = coalescer.offer(event(), now=0.0)
    assert first is not None and first["count"] == 1
    for i in range(46):
        assert coalescer.offer(event(), now=0.1 + i * 0.01) is None
    later = coalescer.offer(event(), now=2.0)
    assert later is not None
    assert later["count"] == 47, f"the repeats were lost, not counted: {later}"
    assert later["folded"] == 46


def test_a_failure_is_never_folded_away():
    """Failures are rare and each one matters; folding them would hide a pattern
    of intermittent errors behind a single count."""
    from benchctrl.agent.server import ActionCoalescer, build_action_event

    coalescer = ActionCoalescer(window_s=10.0)

    def failure():
        return build_action_event(
            "s",
            "device.call",
            {"device": "d", "method": "set_output"},
            ok=False,
            error=TimeoutError("no response"),
        )

    emitted = [coalescer.offer(failure(), now=t / 100) for t in range(5)]
    assert all(e is not None for e in emitted), "a failed action was folded away"


def test_an_arming_action_is_never_folded_away():
    """Arms and disarms are rare enough to each deserve a line, and they are the
    lines an incident review reads."""
    from benchctrl.agent.server import ActionCoalescer, build_action_event

    coalescer = ActionCoalescer(window_s=10.0)

    def arm():
        return build_action_event(
            "s",
            "device.call",
            {"device": "d", "method": "set_output", "args": [True]},
            ok=True,
            result=True,
        )

    emitted = [coalescer.offer(arm(), now=t / 100) for t in range(5)]
    assert all(e is not None for e in emitted), "an arming action was folded away"


def test_the_coalescers_signature_table_is_bounded():
    """A client calling a thousand distinct methods must not grow the agent.

    The cumulative counters survive the reset, so the honesty of ``folded`` does
    not depend on the size of the table.
    """
    from benchctrl.agent.server import ActionCoalescer, build_action_event

    coalescer = ActionCoalescer(window_s=10.0, max_signatures=8)
    for i in range(500):
        coalescer.offer(
            build_action_event(
                "s", "device.call", {"device": "d", "method": f"m{i}"}, ok=True, result=1
            ),
            now=float(i),
        )
    assert coalescer.stats()["signatures"] <= 8


def test_a_broken_summariser_cannot_fail_a_bench_action(bench):
    """The log is strictly less important than the call it describes.

    A bug in the summarising must not turn a successful ``set_voltage`` into an
    error the client sees — and must certainly not turn a *disarm* into one.
    """
    client = bench.connect()
    client.call("agent.claim", {"device": "otii_arc"})

    class Exploding:
        def offer(self, event, **kwargs):
            raise RuntimeError("action log is broken")

        def stats(self):
            return {}

    bench.agent.actions = Exploding()
    # Must still succeed, and must still return the driver's real answer.
    result = client.call(
        "device.call", {"device": "otii_arc", "method": "set_voltage", "args": [3.3]}
    )
    assert result is None or result is not Ellipsis
    assert client.call("agent.status", {})["safety"] is not None


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

"""Per-owner deadman and the run watchdog.

Before per-owner contact the governor kept one bench-wide ``_last_contact``.
Any writer's traffic kept *every* armed device alive, so a client that armed
an output and then died was covered for as long as some other client kept
talking, and a run engine (which never touched at all) was covered by nobody.

Now each armed device belongs to an owner: the session that armed it, or
``run:<id>`` for a run engine. A device trips when *its* owner has been
silent for ``deadman_s`` and not before; a session owner trips with
HEARTBEAT_LOST and a run owner with RUN_WATCHDOG.

Most of the timing here is proved against a synthetic clock (the governor's
``clock`` is injectable), so the boundaries are exact rather than slept-for.
One real-time test per behaviour proves the wiring through the agent.
"""

from __future__ import annotations

import contextlib
import threading
import time
from types import SimpleNamespace

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs import engine as engine_mod
from benchctrl.agent.runs.engine import WAIT_SLICE_S, RunEngine
from benchctrl.agent.runs.spec import Condition, Phase, RunSpec, Safety, Sampling
from benchctrl.agent.safety import SafetyGovernor, TripReason, run_owner
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.agent.worker import DeviceWorker
from benchctrl.config import EndpointConfig
from benchctrl.exceptions import BenchTimeoutError
from benchctrl.net.client import RemoteClient

TOKEN = "per-owner-deadman-token"
DEADMAN_S = 1.0


class FakeClock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def advance(self, s: float) -> None:
        self.now += s


def _arm(gov: SafetyGovernor, key: str, owner):
    gov.observe_call(key, "set_output", (True,), {}, session_id=owner)


# ---------------------------------------------------------------- governor, synthetic clock


@pytest.mark.plan("CORE-P0.5")
def test_only_the_silent_owners_device_is_stale():
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    _arm(gov, "a", "s-A")
    _arm(gov, "b", "s-B")

    # B keeps chatting; A says nothing.
    for _ in range(4):
        clock.advance(0.3)
        gov.touch("s-B")
    # 1.2 s since A armed: past the window. B was heard 0 s ago.
    assert gov.stale_devices() == {"a": "s-A"}
    assert gov.should_trip()


@pytest.mark.plan("CORE-P0.5")
def test_a_device_is_not_stale_at_exactly_the_window():
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    _arm(gov, "a", "s-A")
    clock.advance(DEADMAN_S)
    assert gov.stale_devices() == {}
    clock.advance(0.001)
    assert gov.stale_devices() == {"a": "s-A"}


@pytest.mark.plan("CORE-P0.5")
def test_an_owners_own_touch_keeps_its_device_alive():
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    _arm(gov, "a", "s-A")
    for _ in range(10):
        clock.advance(0.9)
        gov.touch("s-A")
    assert gov.stale_devices() == {}


def test_an_ownerless_device_is_kept_alive_by_anyone():
    # The legacy rule, kept for devices armed with no owner (a direct
    # in-process caller): any contact counts.
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    _arm(gov, "a", None)
    clock.advance(0.9)
    gov.touch("s-B")
    clock.advance(0.9)
    assert gov.stale_devices() == {}
    clock.advance(0.2)
    assert gov.stale_devices() == {"a": None}


@pytest.mark.plan("CORE-P0.6")
def test_a_run_owner_is_not_kept_alive_by_a_session():
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    owner = run_owner("r1")
    _arm(gov, "arc", owner)
    for _ in range(4):
        clock.advance(0.3)
        gov.touch("s-chatty")
    assert gov.stale_devices() == {"arc": owner}


def test_transfer_hands_the_device_to_its_new_owner():
    clock = FakeClock()
    gov = SafetyGovernor(deadman_s=DEADMAN_S, clock=clock)
    _arm(gov, "a", "s-old")
    clock.advance(0.9)
    assert gov.transfer_owner("a", "s-new")
    clock.advance(0.9)
    gov.touch("s-new")
    clock.advance(0.9)
    assert gov.stale_devices() == {}
    assert gov.owner_of("a") == "s-new"
    # The old owner's touch no longer counts for it.
    clock.advance(0.2)
    gov.touch("s-old")
    assert gov.stale_devices() == {"a": "s-new"}


def test_a_targeted_trip_leaves_other_owners_armed():
    from benchctrl.sim.factories import make_otii_arc

    gov = SafetyGovernor(deadman_s=DEADMAN_S)
    devs = {"a": make_otii_arc(), "b": make_otii_arc()}
    workers = {k: DeviceWorker(k).start() for k in devs}
    try:
        _arm(gov, "a", "s-A")
        _arm(gov, "b", "s-B")
        outcomes = gov.trip(TripReason.HEARTBEAT_LOST, devs, workers, only=["a"])
        assert {k: v.value for k, v in outcomes.items()} == {"a": "safe"}
        assert gov.armed_devices == ["b"]
    finally:
        for w in workers.values():
            w.stop()
        for d in devs.values():
            with contextlib.suppress(Exception):
                d.close()


# ---------------------------------------------------------------- engine waits


def _fake_engine():
    touches: list[float] = []
    return SimpleNamespace(_touch=lambda: touches.append(time.monotonic())), touches


@pytest.mark.plan("CORE-P0.6")
def test_a_slow_bounded_wait_keeps_touching():
    """A 30 s PDU switch must not read as a wedged engine to a 15 s deadman."""
    fake, touches = _fake_engine()
    worker = DeviceWorker("slow", max_blocking_s=1.0).start()
    try:
        started = time.monotonic()
        result = RunEngine._await(fake, worker, lambda: time.sleep(2.5) or "ok", "slow", 10.0)
        assert result == "ok"
    finally:
        worker.stop()
    stamps = [started, *touches]
    gaps = [b - a for a, b in zip(stamps, stamps[1:])]
    assert len(touches) >= 3, touches
    assert max(gaps) <= WAIT_SLICE_S + 0.25, gaps


def test_the_bounded_wait_keeps_submits_contract():
    fake, _ = _fake_engine()
    worker = DeviceWorker("w", max_blocking_s=0.2).start()
    release = threading.Event()
    try:
        with pytest.raises(BenchTimeoutError, match="did not complete within 0.5s"):
            RunEngine._await(fake, worker, release.wait, "stuck", 0.5)
        release.set()

        def boom():
            raise ValueError("driver said no")

        with pytest.raises(ValueError, match="driver said no"):
            RunEngine._await(fake, worker, boom, "boom", None)
    finally:
        release.set()
        worker.stop()


# ---------------------------------------------------------------- through the agent, real time


@pytest.fixture()
def bench(tmp_path):
    from benchctrl.sim.factories import make_otii_arc

    registry = DeviceRegistry()
    registry.register_open("arc_a", make_otii_arc())
    registry.register_open("arc_b", make_otii_arc())
    agent = BenchAgent(
        registry,
        token=TOKEN,
        deadman_s=DEADMAN_S,
        heartbeat_s=0.25,
        runs_dir=tmp_path / "runs",
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = EndpointConfig(
        host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=0.25, deadman_s=DEADMAN_S
    )
    try:
        yield agent, endpoint
    finally:
        with contextlib.suppress(Exception):
            agent.runs.abort_all("test teardown")
        server.stop()
        registry.close_all()


def _arm_remote(client, key):
    client.call("agent.claim", {"device": key})
    client.call("device.call", {"device": key, "method": "set_output", "args": [True]})


@pytest.mark.plan("CORE-P0.5")
def test_two_writers_only_the_silent_ones_device_trips(bench):
    agent, endpoint = bench
    b = RemoteClient(endpoint).connect()
    try:
        with RemoteClient(endpoint) as a:
            _arm_remote(a, "arc_a")
            _arm_remote(b, "arc_b")
        # A is gone. B keeps talking for three deadman windows.
        end = time.monotonic() + DEADMAN_S * 3
        while time.monotonic() < end:
            b.call("agent.time", {})
            time.sleep(0.2)

        trips = agent.governor.status()["trips"]
        assert [(t["reason"], t["outcomes"]) for t in trips] == [
            ("heartbeat_lost", {"arc_a": "safe"})
        ]
        assert agent.governor.armed_devices == ["arc_b"]
    finally:
        b.close()


@pytest.mark.plan("CORE-P0.6")
def test_a_wedged_engine_trips_within_deadman_plus_one_second(bench, monkeypatch):
    agent, endpoint = bench
    wedged_at: list[float] = []
    unwedge = threading.Event()

    def wedge(self, idx, values):
        if not wedged_at:
            wedged_at.append(time.time())
        unwedge.wait(30)

    # The rule check is on the engine's own thread and outside every bounded
    # wait, so blocking it stops the tick loop dead: exactly a wedged engine.
    monkeypatch.setattr(RunEngine, "_check_rules", wedge)
    spec = RunSpec(
        name="wedged",
        device="arc_a",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False),
        phases=(Phase(name="hold", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=60),),
    )
    with RemoteClient(endpoint) as client:
        client.call("agent.claim", {"device": "arc_a"})
        run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
        # The client stays connected and chatting: its traffic must not
        # keep the run's device alive.
        deadline = time.monotonic() + DEADMAN_S + 3
        while time.monotonic() < deadline and not agent.governor.status()["trips"]:
            client.call("agent.time", {})
            time.sleep(0.1)

    trips = agent.governor.status()["trips"]
    assert [(t["reason"], t["outcomes"]) for t in trips] == [("run_watchdog", {"arc_a": "safe"})]
    latency = trips[0]["at"] - wedged_at[0]
    # Not before the window (the last touch was just before the wedge), and
    # within the plan's bound after it.
    assert DEADMAN_S - 0.3 <= latency <= DEADMAN_S + 1.0, latency

    unwedge.set()
    engine = agent.runs.get(run_id)
    assert engine.join(timeout=10)
    assert engine.status == "safe_stopped", engine.status_dict()


@pytest.mark.plan("CORE-P0.6")
def test_max_duration_is_enforced_inside_a_phase(bench):
    agent, endpoint = bench
    spec = RunSpec(
        name="too-long",
        device="arc_a",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=1.5),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False),
        # No duration: the phase ends on an exit rule that never fires, so
        # the spec's own total (0 s) is inside the envelope and only the
        # in-phase check can stop it.
        phases=(
            Phase(
                name="hold",
                mode="cv",
                setpoints={"voltage_V": 3.0},
                exit=(Condition(channel="mv", op="<", value=-1000.0, reason="never"),),
            ),
        ),
    )
    with RemoteClient(endpoint) as client:
        client.call("agent.claim", {"device": "arc_a"})
        started = time.monotonic()
        run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
        engine = agent.runs.get(run_id)
        assert engine.join(timeout=10), "run outlived its max_duration_s by > 8 s"
    assert time.monotonic() - started < 1.5 + 1.0
    assert engine.status == "safe_stopped", engine.status_dict()
    assert agent.governor.armed_devices == []


def test_the_engine_arms_as_its_run_owner(bench):
    agent, endpoint = bench
    spec = RunSpec(
        name="owner",
        device="arc_a",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False),
        phases=(Phase(name="hold", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=2),),
    )
    with RemoteClient(endpoint) as client:
        client.call("agent.claim", {"device": "arc_a"})
        run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline and agent.governor.owner_of("arc_a") is None:
            time.sleep(0.05)
        assert agent.governor.owner_of("arc_a") == engine_mod.run_owner(run_id)


@pytest.mark.plan("CORE-P0.4")
def test_a_run_that_rarely_samples_is_still_kept_alive(bench):
    """The tick loop's own touch, isolated.

    Every worker call also touches when it returns, so a run sampling every
    0.2 s is kept alive by its samples alone and could not show whether the
    tick loop touches. Sampling every 10 s against a 1 s deadman leaves the
    tick loop as the only thing that can.
    """
    agent, endpoint = bench
    duration_s = DEADMAN_S * 4
    spec = RunSpec(
        name="sparse",
        device="arc_a",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=10.0, record=False),
        phases=(
            Phase(name="hold", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=duration_s),
        ),
    )
    with RemoteClient(endpoint) as client:
        client.call("agent.claim", {"device": "arc_a"})
        run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
    engine = agent.runs.get(run_id)
    assert engine.join(timeout=duration_s + 10)
    assert agent.governor.status()["trips"] == []
    assert engine.status == "complete", engine.status_dict()

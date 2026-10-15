"""A submitted run outlives the client that submitted it, and is still watched.

``docs/runs.md`` promises "submit a run, walk away". Before per-owner contact,
the governor kept one ``_last_contact`` that only inbound client frames
refreshed, and the run engine armed its device without ever touching it. So
the moment the last client left, the deadman counted down from that client's
last frame and tripped HEARTBEAT_LOST on a run that was working fine.

The fix makes the run engine its own owner, ``run:<id>``, which it keeps alive
from its tick loop. The same mechanism is what makes a *wedged* engine trip:
an engine that stops ticking stops touching, and its device is driven safe.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.spec import Phase, RunSpec, Safety, Sampling
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "unattended-run-token"
ARC = "otii_arc"
DEADMAN_S = 1.0


@pytest.fixture()
def bench(tmp_path):
    from benchctrl.sim.factories import make_otii_arc

    registry = DeviceRegistry()
    registry.register_open(ARC, make_otii_arc())
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


def _spec(duration_s: float) -> RunSpec:
    return RunSpec(
        name="unattended",
        device=ARC,
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False),
        phases=(
            Phase(name="hold", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=duration_s),
        ),
    )


def _wait_finished(agent, run_id, budget_s):
    engine = agent.runs.get(run_id)
    assert engine.join(timeout=budget_s), f"run still going after {budget_s}s"
    return engine


@pytest.mark.plan("CORE-P0.4")
def test_a_run_completes_after_its_client_disconnects(bench):
    agent, endpoint = bench
    # Several deadman windows long, so a trip has every chance to happen.
    duration_s = DEADMAN_S * 5

    with RemoteClient(endpoint) as client:
        client.call("agent.claim", {"device": ARC})
        run_id = client.call("run.submit", {"spec": _spec(duration_s).to_dict()})["run_id"]
    # The client is gone. Nothing but the run is keeping the bench alive.
    left = time.monotonic()

    engine = _wait_finished(agent, run_id, duration_s + 10)
    trips = agent.governor.status()["trips"]

    assert [t["reason"] for t in trips] == [], trips
    assert engine.status == "complete", engine.status_dict()
    # It ran to its duration, not to a trip: guard against a run that "completed"
    # because the deadman cut it short and the engine didn't notice.
    assert time.monotonic() - left >= duration_s * 0.9

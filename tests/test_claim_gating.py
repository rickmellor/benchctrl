"""Claim gating: the verbs that used to ignore another session's claim.

Before this, the writer claim gated ``device.call`` and ``run.submit`` and
nothing else. Any connected session could ``agent.close`` a device another
session held (which trips it: a remote disarm of someone else's output),
``run.abort`` anyone's run, and release anyone's blobs and recordings.

Each test sets up the owner and the intruder as two real sessions on a real
agent, asserts the refusal by its structured ``code`` (and ``holder``, which
names the session to go and ask), and then asserts the thing the refusal
protects is still there. A refusal that also broke the resource would pass
the first half.
"""

from __future__ import annotations

import contextlib
import time

import pytest

from benchctrl import cli
from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.spec import Phase, RunSpec, Safety, Sampling
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient
from benchctrl.net.errors import CLAIM_CONFLICT, NOT_OWNER, PolicyError

pytestmark = pytest.mark.plan("CORE-P0.8")

TOKEN = "claim-gating-token"


@pytest.fixture()
def bench(tmp_path):
    from benchctrl.sim.factories import make_otii_arc

    registry = DeviceRegistry()
    registry.register_open("arc", make_otii_arc())
    registry.register_open("spare", make_otii_arc())
    # A long deadman: nothing here is about the deadman, and a trip from a
    # slow CI box would read as a gating failure.
    agent = BenchAgent(
        registry, token=TOKEN, deadman_s=60.0, heartbeat_s=1.0, runs_dir=tmp_path / "runs"
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = EndpointConfig(host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=1.0)
    clients: list[RemoteClient] = []

    def connect() -> RemoteClient:
        c = RemoteClient(endpoint).connect()
        clients.append(c)
        return c

    try:
        yield agent, connect
    finally:
        with contextlib.suppress(Exception):
            agent.runs.abort_all("test teardown")
        for c in clients:
            with contextlib.suppress(Exception):
                c.close()
        server.stop()
        registry.close_all()


def _claim(client, key="arc") -> str:
    return client.call("agent.claim", {"device": key})["session"]


def _refusal(fn, *args) -> PolicyError:
    with pytest.raises(PolicyError) as caught:
        fn(*args)
    return caught.value


def _wait_for(pred, timeout=5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if pred():
            return True
        time.sleep(0.02)
    return pred()


# ---------------------------------------------------------------- open / close


def test_close_across_a_claim_is_refused_and_leaves_the_output_armed(bench):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    owner.call("device.call", {"device": "arc", "method": "set_output", "args": [True]})

    err = _refusal(intruder.call, "agent.close", {"device": "arc"})
    assert (err.code, err.holder) == (CLAIM_CONFLICT, sid)
    assert agent.governor.armed_devices == ["arc"]
    assert agent.governor.status()["trips"] == []
    assert "arc" in agent.registry.open_devices()


def test_open_across_a_claim_is_refused(bench):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    err = _refusal(intruder.call, "agent.open", {"device": "arc"})
    assert (err.code, err.holder) == (CLAIM_CONFLICT, sid)


def test_the_holder_and_anyone_on_an_unclaimed_device_may_still_close(bench):
    agent, connect = bench
    owner, other = connect(), connect()
    _claim(owner)
    assert owner.call("agent.close", {"device": "arc"})["closed"] is True
    # Nobody claims "spare": closing it is anyone's to do, as before.
    assert other.call("agent.close", {"device": "spare"})["closed"] is True


# ---------------------------------------------------------------- run.abort


def _submit(client) -> str:
    spec = RunSpec(
        name="gated",
        device="arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.5, record=False),
        phases=(Phase(name="hold", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=60),),
    )
    return client.call("run.submit", {"spec": spec.to_dict()})["run_id"]


def _record_events(agent, monkeypatch) -> list[dict]:
    seen: list[dict] = []
    publish = agent.events.publish

    def spy(event):
        seen.append(dict(event))
        return publish(event)

    monkeypatch.setattr(agent.events, "publish", spy)
    return seen


def test_abort_by_a_third_session_is_refused_and_the_run_keeps_going(bench, monkeypatch):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    run_id = _submit(owner)
    events = _record_events(agent, monkeypatch)

    err = _refusal(intruder.call, "run.abort", {"run_id": run_id, "reason": "mine now"})
    assert (err.code, err.holder) == (NOT_OWNER, sid)
    # force with no reason is still a refusal: the reason is the audit.
    err = _refusal(intruder.call, "run.abort", {"run_id": run_id, "force": True})
    assert err.code == NOT_OWNER
    err = _refusal(intruder.call, "run.abort", {"run_id": run_id, "force": True, "reason": " "})
    assert err.code == NOT_OWNER

    time.sleep(0.3)
    assert agent.runs.get(run_id).is_running
    assert [e for e in events if e.get("kind") == "run_abort_forced"] == []


def test_a_forced_abort_stops_the_run_and_says_who_and_why(bench, monkeypatch):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    run_id = _submit(owner)
    events = _record_events(agent, monkeypatch)

    intruder.call("run.abort", {"run_id": run_id, "force": True, "reason": "fire alarm"})
    engine = agent.runs.get(run_id)
    assert engine.join(timeout=10)
    assert not engine.is_running

    forced = [e for e in events if e.get("kind") == "run_abort_forced"]
    assert len(forced) == 1, events
    event = forced[0]
    assert event["severity"] == "alarm"
    assert event["owner"] == sid
    assert event["reason"] == "fire alarm"
    assert event["session"] != sid and event["session"].startswith("s-")
    assert (event["run_id"], event["device"]) == (run_id, "arc")


def test_the_submitter_and_the_claim_holder_may_each_abort_without_force(bench):
    agent, connect = bench
    submitter, holder = connect(), connect()
    _claim(submitter)
    first = _submit(submitter)
    # The submitter lets go of the claim; the run carries on under its own
    # run owner, and a second session takes the claim.
    submitter.call("agent.release", {"device": "arc"})
    _claim(holder)
    # The holder did not submit this run, and may abort it: it is the run on
    # the device it holds.
    holder.call("run.abort", {"run_id": first, "reason": "holder"})
    assert agent.runs.get(first).join(timeout=10)

    # Swap roles: `holder` submits and lets go, `submitter` takes the claim.
    # Now `holder` is the submitter only, and may abort its own run.
    second = _submit(holder)
    holder.call("agent.release", {"device": "arc"})
    _claim(submitter)
    holder.call("run.abort", {"run_id": second, "reason": "submitter"})
    assert agent.runs.get(second).join(timeout=10)


# ---------------------------------------------------------------- blob / rec release


def test_blob_release_is_owner_only_while_the_owner_is_connected(bench):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    mine = agent.blobs.put(b"owner's data", owner=sid).blob_id

    err = _refusal(intruder.call, "blob.release", {"blob": mine})
    assert (err.code, err.holder) == (NOT_OWNER, sid)
    assert agent.blobs.get(mine) == b"owner's data"
    assert owner.call("blob.release", {"blob": mine})["released"] is True

    # An agent-made blob, an unknown one, and one whose owner has gone are
    # anyone's: nobody is left to be surprised.
    agents = agent.blobs.put(b"agent").blob_id
    assert intruder.call("blob.release", {"blob": agents})["released"] is True
    assert intruder.call("blob.release", {"blob": "b-nope"})["released"] is False
    orphan = agent.blobs.put(b"orphan", owner=sid).blob_id
    owner.close()
    assert _wait_for(lambda: sid not in agent._sessions)
    assert intruder.call("blob.release", {"blob": orphan})["released"] is True


def test_rec_release_is_owner_only_while_the_owner_is_connected(bench):
    agent, connect = bench
    owner, intruder = connect(), connect()
    sid = _claim(owner)
    rec_id = agent.recordings.start("spare", object(), session_id=sid).rec_id

    err = _refusal(intruder.call, "rec.release", {"rec_id": rec_id})
    assert (err.code, err.holder) == (NOT_OWNER, sid)
    assert agent.recordings.owner_of(rec_id) == sid
    owner.close()
    assert _wait_for(lambda: sid not in agent._sessions)
    assert intruder.call("rec.release", {"rec_id": rec_id})["released"] is True


# ---------------------------------------------------------------- the CLI's match


def test_the_claim_conflict_code_crosses_the_wire(bench):
    agent, connect = bench
    first, second = connect(), connect()
    sid = _claim(first)
    err = _refusal(second.call, "agent.claim", {"device": "arc"})
    assert (err.code, err.holder) == (CLAIM_CONFLICT, sid)
    assert cli.claim_conflict_help(err) is not None


def test_the_cli_matches_the_code_before_the_text():
    # Code alone, reworded text: recognised. This is the point of the code.
    assert cli.claim_conflict_help(PolicyError("held elsewhere", code=CLAIM_CONFLICT))
    # A different code wins over text that happens to match.
    assert (
        cli.claim_conflict_help(PolicyError("blob b-1 is claimed by session s-2", code=NOT_OWNER))
        is None
    )
    # No code (an older agent): the text fallback still works.
    assert cli.claim_conflict_help(PolicyError("arc is claimed by session s-1; …"))
    assert cli.claim_conflict_help(PolicyError("something else")) is None


def test_a_refusal_without_a_code_serialises_as_before():
    from benchctrl.net.errors import encode_exception

    assert encode_exception(PolicyError("plain"))["attrs"] == {}
    assert encode_exception(PolicyError("x", code=NOT_OWNER, holder="s-1"))["attrs"] == {
        "code": NOT_OWNER,
        "holder": "s-1",
    }

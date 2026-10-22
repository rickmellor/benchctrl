"""Session ids are ``s-<agent6>-<boot6>-N`` and do not repeat across restarts.

A bare ``s-N`` counter restarted at ``s-1`` with every agent process. An id
held from before a restart (by a client, a run record, a log reader) then
named a different session after it, and two benches' ids could not be told
apart in a merged log.
"""

from __future__ import annotations

import re

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

pytestmark = pytest.mark.plan("CORE-P0.9")

TOKEN = "session-id-token"
SESSION_ID = re.compile(r"^s-([0-9a-f]{6})-([0-9a-f]{6})-([1-9][0-9]*)$")


def _ids(agent: BenchAgent, n: int) -> list[str]:
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    try:
        endpoint = EndpointConfig(host="127.0.0.1", port=server.port, token=TOKEN)
        out = []
        for _ in range(n):
            with RemoteClient(endpoint) as client:
                out.append(client.call("agent.claim", {"device": "arc"})["session"])
        return out
    finally:
        server.stop()


@pytest.fixture()
def registry():
    from benchctrl.sim.factories import make_otii_arc

    reg = DeviceRegistry()
    reg.register_open("arc", make_otii_arc())
    yield reg
    reg.close_all()


def test_the_format_over_the_wire(registry):
    agent = BenchAgent(registry, token=TOKEN, agent_id="bench-one")
    ids = _ids(agent, 3)
    parsed = [SESSION_ID.match(i) for i in ids]
    assert all(parsed), ids
    assert {m.group(1) for m in parsed} == {agent.agent_tag}
    assert {m.group(2) for m in parsed} == {agent.boot_tag}
    assert [int(m.group(3)) for m in parsed] == [1, 2, 3]


def test_a_restarted_agent_does_not_reuse_an_id(registry):
    # Two agent processes on one bench: same agent6, different boot6, so the
    # counter restarting at 1 no longer collides.
    first = BenchAgent(registry, token=TOKEN, agent_id="bench-one")
    before = _ids(first, 2)
    second = BenchAgent(registry, token=TOKEN, agent_id="bench-one")
    after = _ids(second, 2)
    assert first.agent_tag == second.agent_tag
    assert set(before).isdisjoint(after), (before, after)
    assert [SESSION_ID.match(i).group(3) for i in after] == ["1", "2"]


def test_two_benches_are_told_apart_and_the_name_is_not_exposed():
    a = BenchAgent(DeviceRegistry(), token=TOKEN, agent_id="bench-one")
    b = BenchAgent(DeviceRegistry(), token=TOKEN, agent_id="bench-two")
    assert a.agent_tag != b.agent_tag
    assert "bench" not in a.next_session_id()
    # Stable for a given identity: the same bench always gets the same agent6.
    assert BenchAgent(DeviceRegistry(), token=TOKEN, agent_id="bench-one").agent_tag == a.agent_tag

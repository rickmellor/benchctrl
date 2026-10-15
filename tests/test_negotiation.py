"""Handshake negotiation: ``v_min``/``v_max``/``caps`` next to the old ``v``.

The compatibility claim is two-sided, and each side is tested against the
*frozen* v1 wire shape rather than against today's builders. A test that
built the "old" HELLO with ``auth.build_hello`` would change shape with the
code it is meant to hold still.

- An old client (HELLO with ``v`` only) against this agent.
- This client against an old agent: a raw-socket stand-in that sends exactly
  the CHALLENGE and WELCOME an agent sent before negotiation existed.
"""

from __future__ import annotations

import json
import socket
import threading

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.exceptions import BenchProtocolError
from benchctrl.net import auth
from benchctrl.net.client import RemoteClient
from benchctrl.net.errors import PolicyError
from benchctrl.net.frames import FrameReader, FrameType, FrameWriter

pytestmark = pytest.mark.plan("CORE-P0.10")

TOKEN = "negotiation-token"

#: Exactly what a v1 client sent before ranges existed.
V1_HELLO_KEYS = {"v", "nonce_c", "client"}


def _v1_hello(client_name: str = "benchctrl"):
    nonce = auth.new_nonce()
    return {"v": 1, "nonce_c": nonce, "client": client_name}, nonce


# ---------------------------------------------------------------- the rule


@pytest.mark.parametrize(
    ("peer", "expected"),
    [
        ({"v": 1}, 1),  # pre-range peer, our version
        ({"v": 1, "v_min": 1, "v_max": 1}, 1),
        ({"v": 2, "v_min": 1, "v_max": 2}, 1),  # a newer peer that still speaks v1
        ({"v": 1, "v_min": 1, "v_max": 9}, 1),
    ],
)
def test_negotiate_accepts(peer, expected):
    assert auth.negotiate(peer) == (expected, None)


@pytest.mark.parametrize(
    ("peer", "needle"),
    [
        ({"v": 2}, "peer speaks v2, this build speaks v1"),  # strict, as before
        ({}, "peer speaks vNone"),
        ({"v": 2, "v_min": 2, "v_max": 3}, "peer speaks v2..v3"),
        ({"v_min": 1}, "malformed"),  # half a range is not guessed at
        ({"v_min": 2, "v_max": 1}, "malformed"),
        ({"v_min": "1", "v_max": 1}, "malformed"),
        ({"v_min": True, "v_max": 1}, "malformed"),
        ({"v_min": 0, "v_max": 1}, "malformed"),
    ],
)
def test_negotiate_refuses(peer, needle):
    version, error = auth.negotiate(peer)
    assert version is None
    assert needle in error


def test_this_build_advertises_its_range_and_caps_beside_the_old_v():
    hello, _ = auth.build_hello()
    challenge, _ = auth.build_challenge()
    for payload in (hello, challenge):
        assert payload["v"] == auth.PROTOCOL_VERSION == 1
        assert (payload["v_min"], payload["v_max"]) == (auth.PROTOCOL_MIN, auth.PROTOCOL_MAX)
        assert payload["caps"] == list(auth.CAPS)
    assert set(hello) >= V1_HELLO_KEYS


def test_peer_caps_ignores_what_it_cannot_read():
    assert auth.peer_caps({}) == frozenset()
    assert auth.peer_caps({"caps": "err-code"}) == frozenset()
    assert auth.peer_caps({"caps": ["err-code", 3, None]}) == {"err-code"}


# ---------------------------------------------------------------- new agent


@pytest.fixture()
def agent():
    from benchctrl.sim.factories import make_otii_arc

    registry = DeviceRegistry()
    registry.register_open("arc", make_otii_arc())
    bench = BenchAgent(registry, token=TOKEN)
    server = AgentServer(bench, host="127.0.0.1", port=0).start()
    try:
        yield bench, EndpointConfig(host="127.0.0.1", port=server.port, token=TOKEN)
    finally:
        server.stop()
        registry.close_all()


def test_new_client_and_new_agent_settle_on_a_version_and_swap_caps(agent):
    bench, endpoint = agent
    with RemoteClient(endpoint) as client:
        assert client.protocol_v == 1
        assert client.peer_caps == frozenset(auth.CAPS)
        sid = client.welcome["session"]
        session = bench._sessions[sid]
        assert session.protocol_v == 1
        assert session.peer_caps == frozenset(auth.CAPS)


def test_an_old_client_still_connects_to_a_new_agent(agent, monkeypatch):
    bench, endpoint = agent
    monkeypatch.setattr(auth, "build_hello", _v1_hello)
    with RemoteClient(endpoint) as client:
        assert "utc" in client.call("agent.time", {})
        session = bench._sessions[client.welcome["session"]]
        assert session.protocol_v == 1
        assert session.peer_caps == frozenset()


def test_an_old_client_of_another_version_is_still_refused(agent, monkeypatch):
    _, endpoint = agent

    def v2_hello(client_name="benchctrl"):
        hello, nonce = _v1_hello(client_name)
        return {**hello, "v": 2}, nonce

    monkeypatch.setattr(auth, "build_hello", v2_hello)
    with pytest.raises(PolicyError, match="peer speaks v2, this build speaks v1"):
        RemoteClient(endpoint).connect()


def test_a_newer_client_with_a_range_that_includes_v1_is_accepted(agent, monkeypatch):
    bench, endpoint = agent

    def future_hello(client_name="benchctrl"):
        hello, nonce = _v1_hello(client_name)
        return {**hello, "v": 2, "v_min": 1, "v_max": 2, "caps": ["teleport"]}, nonce

    monkeypatch.setattr(auth, "build_hello", future_hello)
    with RemoteClient(endpoint) as client:
        assert client.welcome["v"] == 1
        session = bench._sessions[client.welcome["session"]]
        assert session.peer_caps == {"teleport"}


# ---------------------------------------------------------------- old agent


class _V1Agent:
    """Speaks the handshake exactly as an agent did before negotiation."""

    def __init__(self, challenge_v: int = 1) -> None:
        self.challenge_v = challenge_v
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(1)
        self.port = self.sock.getsockname()[1]
        self.got_auth = threading.Event()
        self.hello: dict = {}
        self._conn = None
        self._thread = threading.Thread(target=self._serve, daemon=True)
        self._thread.start()

    def _send(self, writer, ftype, payload: dict) -> None:
        writer.send(ftype, json.dumps(payload).encode())

    def _serve(self) -> None:
        conn, _ = self.sock.accept()
        self._conn = conn
        reader, writer = FrameReader(conn), FrameWriter(conn)
        _, payload = reader.read_frame(timeout=5)
        self.hello = json.loads(payload)
        nonce_s = auth.new_nonce()
        challenge = {
            "v": self.challenge_v,
            "nonce_s": nonce_s,
            "agent": "benchctrl-agent",
            "auth": "hmac-sha256",
        }
        self._send(writer, FrameType.CHALLENGE, challenge)
        frame = reader.read_frame(timeout=5)
        if frame is None or frame[0] != FrameType.AUTH:
            return
        self.got_auth.set()
        mac = json.loads(frame[1])["mac"]
        assert auth.verify_mac(TOKEN, self.hello["nonce_c"], nonce_s, mac)
        welcome = {
            "session": "s-1",
            "agent": "benchctrl-agent",
            "observer": False,
            "heartbeat_s": 5.0,
            "deadman_s": 15.0,
            "clock_utc": "2026-10-14T00:00:00Z",
            "devices": [],
            "limits": {"max_blocking_s": 5.0, "max_recording_s": 300.0, "chunk_bytes": 65536},
        }
        self._send(writer, FrameType.WELCOME, welcome)

    def close(self) -> None:
        for s in (self._conn, self.sock):
            if s is not None:
                s.close()


def test_a_new_client_still_connects_to_an_old_agent():
    old = _V1Agent()
    try:
        client = RemoteClient(EndpointConfig(host="127.0.0.1", port=old.port, token=TOKEN))
        client.connect()
        try:
            assert client.protocol_v == 1
            assert client.peer_caps == frozenset()
            assert client.welcome["session"] == "s-1"
        finally:
            client.close()
        # The old agent saw the keys it always read, alongside the new ones.
        assert set(old.hello) >= V1_HELLO_KEYS
        assert old.hello["v"] == 1
    finally:
        old.close()


def test_a_new_client_refuses_an_old_agent_of_another_version_before_auth():
    old = _V1Agent(challenge_v=2)
    try:
        client = RemoteClient(EndpointConfig(host="127.0.0.1", port=old.port, token=TOKEN))
        with pytest.raises(BenchProtocolError, match="peer speaks v2"):
            client.connect()
        assert not old.got_auth.wait(0.3), "client sent AUTH to an agent it cannot speak to"
    finally:
        old.close()

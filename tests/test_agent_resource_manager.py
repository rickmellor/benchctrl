"""The agent's one shared VISA ResourceManager.

``pyvisa.ResourceManager()`` is a **singleton**: a second call returns the same
underlying object, so anything that builds "its own" manager and closes it
afterwards closes everyone's, and every already-open instrument's next
``query()`` fails with ``InvalidSession: Invalid session handle``.

That is the bug these tests exist to prevent, and it was observed on the bench
board rather than imagined: a read-only HDMI dashboard, whose entire job is to
*watch*, called ``agent.discover`` on its 30 s inventory timer and killed a live
4-wire resistance sweep (Eastwood QR10x + Siglent SDM4065A) two setpoints in.
The agent's ``agent.discover`` handler passed no manager, so the scan made one
and closed it. **The dashboard must never be able to disrupt bench function** —
these tests are the guard on that invariant.

No instruments here: pyvisa is faked, so the tests assert the *ownership* rules
(one manager, never closed by a scan) rather than any VISA behaviour.
"""

from __future__ import annotations

import contextlib
import sys
import threading
import time
import types
from typing import Optional

import pytest

from benchctrl import discovery
from benchctrl.agent import server as server_mod
from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "resource-manager-test-token"

#: A resource string in the radix pyvisa-py uses on the bench board, so the
#: fixture exercises the same parsing path the real board does.
SIGLENT_RESOURCE = "USB0::62700::4640::SDM4XCAX7R1234::0::INSTR"


# --------------------------------------------------------------------------
# Fakes
# --------------------------------------------------------------------------


class FakeResourceManager:
    """Records what was done to it. ``closed`` is the assertion that matters."""

    def __init__(self, resources=(SIGLENT_RESOURCE,)) -> None:
        self.resources = tuple(resources)
        self.closed = False
        self.close_calls = 0
        self.list_calls = 0

    def list_resources(self):
        self.list_calls += 1
        if self.closed:
            # What pyvisa does in spirit: a closed manager cannot enumerate.
            raise RuntimeError("Invalid session handle. The resource might be closed.")
        return self.resources

    def close(self) -> None:
        self.closed = True
        self.close_calls += 1


class FakePyvisa:
    """A stand-in ``pyvisa`` module that counts ResourceManager constructions.

    ``constructions`` is the evidence for "exactly once": identity alone would
    also hold if the code built a manager per scan and the *last* one happened
    to be the one handed back.
    """

    def __init__(self, *, raises: Optional[BaseException] = None, on_build=None) -> None:
        self.constructions = 0
        self.instances: list[FakeResourceManager] = []
        self._raises = raises
        self._on_build = on_build
        self._lock = threading.Lock()

    def ResourceManager(self, *_args, **_kwargs):  # noqa: N802 - pyvisa's name
        with self._lock:
            self.constructions += 1
        if self._on_build is not None:
            self._on_build()
        if self._raises is not None:
            raise self._raises
        rm = FakeResourceManager()
        self.instances.append(rm)
        return rm

    @property
    def only_instance(self) -> FakeResourceManager:
        assert len(self.instances) == 1, (
            f"expected exactly one ResourceManager, got {len(self.instances)}"
        )
        return self.instances[0]


@pytest.fixture()
def fake_visa(monkeypatch):
    """Install a fake ``pyvisa`` and quiet the non-VISA transports.

    The other scans are stubbed out so an inventory reflects only what this
    test put on the fake bus — whatever is really plugged into the machine
    running the suite must not change the result.
    """

    def install(**kwargs) -> FakePyvisa:
        fake = FakePyvisa(**kwargs)
        module = types.ModuleType("pyvisa")
        module.ResourceManager = fake.ResourceManager
        monkeypatch.setitem(sys.modules, "pyvisa", module)
        return fake

    monkeypatch.setattr(discovery, "scan_serial", lambda: [])
    monkeypatch.setattr(discovery, "scan_driverless_bridges", lambda: [])
    monkeypatch.setattr(discovery, "scan_usbtmc", lambda: [])
    return install


@pytest.fixture()
def agent(tmp_path):
    """A bare agent. No devices: the manager is what is under test."""
    a = BenchAgent(DeviceRegistry(), token=TOKEN, runs_dir=tmp_path / "runs")
    try:
        yield a
    finally:
        a.shutdown()


def _discover(agent, *, observer: bool = False) -> dict:
    """Route ``agent.discover`` exactly as a connected session would.

    Goes through ``_Handler._route`` rather than calling ``discovery.inventory``
    directly, because the missing ``resource_manager=`` argument *was in the
    handler*: a test that called discovery itself would have passed throughout
    the outage.
    """
    handler = types.SimpleNamespace(agent=agent)
    session = types.SimpleNamespace(observer=observer)
    result, _ = server_mod._Handler._route(handler, session, "agent.discover", {})
    return result


# --------------------------------------------------------------------------
# One manager, owned by the agent
# --------------------------------------------------------------------------


def test_the_agent_reuses_one_resource_manager_across_repeated_discovers(
    agent, fake_visa
):
    """Prevents: every 30 s inventory poll building a fresh VISA manager.

    Asserts construction happened *once* as well as identity, since a
    per-scan manager would still satisfy an identity check against whatever
    was built last.
    """
    fake = fake_visa()

    first = agent.resource_manager()
    for _ in range(3):
        _discover(agent)

    assert fake.constructions == 1, (
        f"built {fake.constructions} ResourceManagers; pyvisa's is a singleton "
        f"and each extra one is a handle that can close the bench's sessions"
    )
    assert agent.resource_manager() is first
    assert first is fake.only_instance


def test_the_discover_handler_uses_the_agents_manager_not_its_own(agent, fake_visa):
    """The actual regression: the handler called ``discovery.inventory()`` bare.

    Evidence is that the *agent's* manager is the object the scan enumerated
    through. A handler that made its own would leave the agent's untouched.
    """
    fake = fake_visa()

    inventory = _discover(agent)

    rm = agent.resource_manager()
    assert rm.list_calls == 1, (
        "the scan did not enumerate through the agent's manager, so it must "
        "have created one of its own — the bug that killed the sweep"
    )
    assert fake.constructions == 1
    assert inventory["count"] == 1
    assert inventory["devices"][0]["device_key"] == "siglent_sdm4065a"


def test_a_scan_never_closes_a_manager_it_was_given(fake_visa):
    """The mechanism of the failure: ``close()`` on a caller's manager.

    Closing invalidates every session made from the singleton, so an open
    instrument's next query fails with "Invalid session handle" — nothing
    closed that DMM; a discovery scan did.
    """
    fake_visa()
    rm = FakeResourceManager()

    found = discovery.scan_visa(rm)

    assert rm.close_calls == 0, "scan_visa closed a manager it did not create"
    assert not rm.closed
    assert [d.path for d in found] == [SIGLENT_RESOURCE]


def test_repeated_discovers_leave_the_shared_manager_open(agent, fake_visa):
    """End to end over the handler: polling must not close the bench's manager.

    ``list_resources`` on the fake raises once it has been closed, in the same
    way the real one does, so a scan that closed it would also break the *next*
    inventory rather than failing silently.
    """
    fake_visa()

    for _ in range(4):
        assert _discover(agent)["count"] == 1

    rm = agent.resource_manager()
    assert rm.close_calls == 0
    assert not rm.closed
    assert rm.list_calls == 4


def test_an_observer_session_gets_the_same_shared_manager(agent, fake_visa):
    """The dashboard is an observer, and observers may call ``agent.discover``.

    The role that caused the outage is the one checked here: a read-only
    session must go through the agent's manager like everybody else.
    """
    fake = fake_visa()

    _discover(agent, observer=True)
    _discover(agent, observer=True)

    assert fake.constructions == 1
    assert agent.resource_manager().close_calls == 0


# --------------------------------------------------------------------------
# Concurrency
# --------------------------------------------------------------------------


def test_concurrent_discovers_build_only_one_manager(agent, fake_visa):
    """Several request threads can call ``agent.discover`` at once.

    Unsynchronised lazy init would race two managers into existence, and the
    loser would be a second handle on the singleton — exactly the thing whose
    ``close()`` takes the bench down. The construction is slowed to widen the
    window a bare ``if self._rm is None`` would lose in.
    """
    fake = fake_visa(on_build=lambda: time.sleep(0.05))

    results: list[object] = []
    errors: list[BaseException] = []
    start = threading.Barrier(8)

    def call():
        try:
            start.wait(timeout=5.0)
            results.append(agent.resource_manager())
        except BaseException as exc:  # noqa: BLE001 - reported, not swallowed
            errors.append(exc)

    threads = [threading.Thread(target=call, name=f"rm-{i}") for i in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=10.0)

    assert not errors, f"threads raised: {errors!r}"
    assert fake.constructions == 1, (
        f"{fake.constructions} managers built by 8 concurrent callers; the "
        f"lazy init is racing"
    )
    assert len(results) == 8
    assert all(r is results[0] for r in results)


def test_building_the_manager_does_not_hold_the_session_lock(agent, fake_visa):
    """Why this field has its own lock rather than reusing ``self._lock``.

    ``self._lock`` guards the session and claim tables, taken on every connect
    and disconnect, and ``ResourceManager()`` enumerates USB in ~1.6 s on the
    board. Holding it across construction would park every unrelated connect
    behind a bus scan. Probed from another thread, because ``self._lock`` is an
    RLock and re-acquiring it on the constructing thread would succeed either
    way and prove nothing.
    """
    acquired: list[bool] = []

    def probe_from_another_thread():
        def probe():
            got = agent._lock.acquire(timeout=1.0)
            acquired.append(got)
            if got:
                agent._lock.release()

        t = threading.Thread(target=probe, name="session-lock-probe")
        t.start()
        t.join(timeout=5.0)

    fake_visa(on_build=probe_from_another_thread)

    assert agent.resource_manager() is not None
    assert acquired == [True], (
        "the session lock was held while the VISA manager was being built; a "
        "slow USB enumeration would stall unrelated connects and disconnects"
    )


# --------------------------------------------------------------------------
# A VISA problem must never take down routing
# --------------------------------------------------------------------------


def test_discover_still_answers_when_pyvisa_is_not_installed(agent, monkeypatch):
    """pyvisa is an optional extra: a serial-only bench is a valid bench.

    ``None`` in ``sys.modules`` is how the interpreter reports a blocked
    import, so ``import pyvisa`` raises ImportError here exactly as it would on
    a board without it.
    """
    monkeypatch.setitem(sys.modules, "pyvisa", None)
    monkeypatch.setattr(discovery, "scan_serial", lambda: [])
    monkeypatch.setattr(discovery, "scan_driverless_bridges", lambda: [])
    monkeypatch.setattr(discovery, "scan_usbtmc", lambda: [])

    assert agent.resource_manager() is None
    inventory = _discover(agent)
    assert inventory["count"] == 0
    assert inventory["devices"] == []


def test_discover_still_answers_when_the_visa_backend_is_unusable(agent, fake_visa):
    """No libusb, no udev rule, no backend: pyvisa imports and then raises.

    The manager is a convenience for scans, not a precondition for serving
    requests, so this returns None and routing carries on.
    """
    fake = fake_visa(raises=OSError("could not locate a VISA implementation"))

    assert agent.resource_manager() is None
    assert _discover(agent)["count"] == 0

    # Latched, not retried: a broken backend must not cost the agent ~1.6 s of
    # failed construction on every 30 s inventory poll for the life of the
    # agent. Counted across the agent's own accessor only — ``scan_visa`` given
    # None does try to build one itself, which is its documented fallback and
    # harmless here, since a constructor that raises hands out no handle to
    # close.
    before = fake.constructions
    for _ in range(5):
        assert agent.resource_manager() is None
    assert fake.constructions == before


def test_a_raising_resource_manager_does_not_break_unrelated_routing(agent, fake_visa):
    """Routing is not downstream of VISA. ``agent.status`` is the proof.

    ``agent.status`` costs ~5 ms and the dashboard polls it every 5 s; if a
    VISA failure could escape into ``_route``, the panel would lose the safety
    state it exists to show.
    """
    fake_visa(raises=OSError("no backend"))
    handler = types.SimpleNamespace(agent=agent)
    session = types.SimpleNamespace(observer=True)

    _discover(agent)
    status, _ = server_mod._Handler._route(handler, session, "agent.status", {})

    assert "safety" in status and "workers" in status


def test_discover_over_a_real_socket_survives_a_missing_backend(
    tmp_path, monkeypatch, fake_visa
):
    """The dashboard's actual path: an observer client on a real connection.

    Exercises authentication, framing, and dispatch, so a VISA fault cannot be
    shown to be harmless only in a hand-built call.
    """
    fake_visa(raises=OSError("no backend"))
    agent = BenchAgent(
        DeviceRegistry(), token=TOKEN, runs_dir=tmp_path / "runs", heartbeat_s=0.5
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = EndpointConfig(
        host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=0.5
    )
    client = RemoteClient(endpoint, observer=True).connect()
    try:
        assert client.discover()["count"] == 0
        assert "safety" in client.status()
    finally:
        with contextlib.suppress(Exception):  # teardown
            client.close()
        server.stop()


# --------------------------------------------------------------------------
# Shutdown
# --------------------------------------------------------------------------


def test_the_shared_manager_is_closed_at_shutdown_and_only_then(tmp_path, fake_visa):
    """Closing is correct exactly once, on the way out — never on a scan.

    ``shutdown`` already drove the bench safe and closed the devices by the
    time this runs, so invalidating the singleton's sessions harms nothing.
    Ordering is what makes it safe, which is why the close is asserted to
    happen *after* several scans rather than merely to happen.
    """
    fake_visa()
    agent = BenchAgent(DeviceRegistry(), token=TOKEN, runs_dir=tmp_path / "runs")
    _discover(agent)
    rm = agent.resource_manager()
    assert not rm.closed

    agent.shutdown()

    assert rm.close_calls == 1
    # And a scan racing teardown gets nothing rather than a fresh manager.
    assert agent.resource_manager() is None

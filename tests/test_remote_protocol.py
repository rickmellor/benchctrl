"""Remote mode end to end: real socket, real driver, simulated instrument.

The stack under test is complete — MCP-facing proxy, wire protocol, agent
dispatch, device worker, production driver, pty, simulator. Only the silicon
is fake.
"""

from __future__ import annotations

import threading
import time

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.exceptions import BenchCommandError, BenchValueError
from benchctrl.interfaces import SourceMeasurementUnit
from benchctrl.net.client import RemoteClient
from benchctrl.net.errors import PolicyError
from benchctrl.net.proxy import RemoteDevice, RemoteSMU
from benchctrl.sim import SimulatedOtiiArc, Square

TOKEN = "test-token-do-not-use-in-anger"


@pytest.fixture()
def bench():
    """An agent serving a simulated Arc, and a connected client."""
    from benchctrl.drivers.otii_arc import OtiiArc

    sim = SimulatedOtiiArc()
    sim.start()
    smu = OtiiArc.open(sim.port)

    registry = DeviceRegistry()
    registry.register_open("otii_arc", smu)
    agent = BenchAgent(registry, token=TOKEN, deadman_s=2.0, heartbeat_s=0.5)
    server = AgentServer(agent, host="127.0.0.1", port=0).start()

    endpoint = EndpointConfig(
        host="127.0.0.1",
        port=server.port,
        token=TOKEN,
        heartbeat_s=0.5,
        deadman_s=2.0,
    )
    client = RemoteClient(endpoint).connect()
    try:
        yield type(
            "Bench",
            (),
            {"sim": sim, "smu": smu, "agent": agent, "server": server, "client": client},
        )
    finally:
        try:
            client.close()
        finally:
            server.stop()
            sim.close()


@pytest.fixture()
def arc(bench):
    return bench.client.attach("otii_arc")


# --------------------------------------------------------------------------
# Handshake and auth
# --------------------------------------------------------------------------


def test_welcome_describes_the_bench(bench):
    assert bench.client.welcome["agent"] == "benchctrl-agent"
    assert "otii_arc" in bench.client.device_names()
    assert bench.client.welcome["limits"]["max_recording_s"] > 0


def test_bad_token_is_refused(bench):
    endpoint = EndpointConfig(
        host="127.0.0.1", port=bench.server.port, token="wrong-token"
    )
    with pytest.raises(Exception) as excinfo:
        RemoteClient(endpoint).connect()
    assert "auth" in str(excinfo.value).lower()


def test_repeated_failures_tarpit_the_address(bench):
    endpoint = EndpointConfig(host="127.0.0.1", port=bench.server.port, token="nope")
    for _ in range(3):
        with pytest.raises(Exception):
            RemoteClient(endpoint).connect()
    assert bench.agent.failures.is_blocked("127.0.0.1")

    good = EndpointConfig(host="127.0.0.1", port=bench.server.port, token=TOKEN)
    with pytest.raises(Exception) as excinfo:
        RemoteClient(good).connect()
    assert "retry in" in str(excinfo.value)


def test_token_never_crosses_the_wire():
    """A passive listener must not be able to lift the secret."""
    from benchctrl.net import auth

    hello, nonce_c = auth.build_hello()
    challenge, nonce_s = auth.build_challenge()
    payload = auth.build_auth(TOKEN, nonce_c, nonce_s)
    blob = repr((hello, challenge, payload))
    assert TOKEN not in blob
    assert auth.verify_mac(TOKEN, nonce_c, nonce_s, payload["mac"])
    assert not auth.verify_mac("other", nonce_c, nonce_s, payload["mac"])


# --------------------------------------------------------------------------
# The Protocol survives the wire
# --------------------------------------------------------------------------


def test_remote_smu_satisfies_the_protocol(arc):
    assert isinstance(arc, RemoteSMU)
    assert isinstance(arc, SourceMeasurementUnit)


def test_setters_reach_the_device(bench, arc):
    arc.set_voltage(3.3)
    _wait(lambda: bench.sim.params[0x0B] == 3_300_000)
    assert bench.smu.voltage == pytest.approx(3.3)


def test_properties_read_through(bench, arc):
    arc.set_voltage(2.5)
    arc.set_current_limit(0.5)
    assert arc.voltage == pytest.approx(2.5)
    assert arc.current_limit == pytest.approx(0.5)
    assert arc.is_connected is True


def test_identity_queries(arc):
    assert arc.get_device_name() == "Arc"
    assert arc.get_fw_version() == "3.1.3"


# --------------------------------------------------------------------------
# The safety gate — the reason __getattr__ must raise
# --------------------------------------------------------------------------


def test_unknown_attribute_raises_rather_than_returning_a_callable(arc):
    """This is what stops enable_output arming with no current limit."""
    with pytest.raises(AttributeError):
        arc.no_such_method
    with pytest.raises(AttributeError):
        arc.current_limitt  # typo — must not silently become a method


def test_enable_output_refuses_without_a_current_limit(bench, arc):
    """The MCP gate reads `smu.current_limit is None`. It must still work."""
    from benchctrl.drivers.otii_arc import mcp_tools as arc_tools

    assert arc.current_limit is None
    saved = arc_tools._smu
    arc_tools._smu = arc
    try:
        result = arc_tools.enable_output(confirm_dut_attached=True)
        assert "REFUSED" in result["error"]
        assert "current_limit" in result["error"]
    finally:
        arc_tools._smu = saved
    assert bench.sim.params[0x09] == 0  # output never enabled


def test_enable_output_succeeds_once_guards_are_satisfied(bench, arc):
    from benchctrl.drivers.otii_arc import mcp_tools as arc_tools

    arc.set_current_limit(0.5)
    arc.set_voltage(3.3)
    saved = arc_tools._smu
    arc_tools._smu = arc
    try:
        result = arc_tools.enable_output(confirm_dut_attached=True)
        assert "error" not in result
    finally:
        arc_tools._smu = saved
    _wait(lambda: bench.sim.params[0x09] == 1)


def test_state_snapshot_costs_one_round_trip(bench, arc):
    """19 round trips per setter would make remote mode unusable."""
    from benchctrl.drivers.otii_arc import mcp_tools as arc_tools

    arc.set_voltage(1.0)  # primes the piggybacked snapshot
    before = bench.agent.workers.get("otii_arc").calls_served
    state = arc_tools._smu_state(arc)
    after = bench.agent.workers.get("otii_arc").calls_served
    assert state["voltage_V"] == pytest.approx(1.0)
    assert after == before, "property reads must be served from the snapshot"


# --------------------------------------------------------------------------
# Policy
# --------------------------------------------------------------------------


@pytest.mark.parametrize("method", ["close", "calibrate", "firmware_upgrade"])
def test_denied_methods_are_refused(bench, arc, method):
    with pytest.raises((PolicyError, BenchValueError, AttributeError)):
        getattr(arc, method)()


def test_private_methods_are_not_reachable(bench):
    with pytest.raises(PolicyError):
        bench.client.call(
            "device.call",
            {"device": "otii_arc", "method": "_send_payload", "args": [], "kwargs": {}},
        )


def test_dunder_methods_are_not_reachable(bench):
    with pytest.raises(PolicyError):
        bench.client.call(
            "device.call",
            {"device": "otii_arc", "method": "__class__", "args": [], "kwargs": {}},
        )


def test_unknown_device_is_refused(bench):
    with pytest.raises(BenchValueError):
        bench.client.call("agent.open", {"device": "keithley_2400"})


# --------------------------------------------------------------------------
# Errors survive the round trip
# --------------------------------------------------------------------------


def test_client_side_validation_error_round_trips(arc):
    with pytest.raises(BenchValueError) as excinfo:
        arc.set_voltage(400.0)
    assert "5.5" in str(excinfo.value)


def test_bench_command_error_keeps_its_attributes(bench, arc):
    """A rejected SET must arrive as BenchCommandError with error_code."""
    with arc.record("mv"):
        bench.sim.inject_error()
        arc.set_voltage(3.3)
        _wait(lambda: bench.sim.rejected)
        time.sleep(0.2)
        with pytest.raises(BenchCommandError) as excinfo:
            for _ in range(20):
                arc.set_voltage(3.3)
                time.sleep(0.02)
    assert excinfo.value.error_code == -101


def test_unknown_exception_degrades_along_its_mro():
    from benchctrl.exceptions import BenchError
    from benchctrl.net.errors import decode_exception

    exc = decode_exception(
        {
            "c": "SomeFutureDriverError",
            "mro": ["SomeFutureDriverError", "BenchValueError", "BenchError"],
            "msg": "from a newer agent",
            "attrs": {},
        }
    )
    assert isinstance(exc, BenchError)
    assert exc.remote_class == "SomeFutureDriverError"


def test_completely_unknown_exception_becomes_remote_bench_error():
    from benchctrl.net.errors import RemoteBenchError, decode_exception

    exc = decode_exception({"c": "Weird", "mro": ["Weird"], "msg": "?", "attrs": {}})
    assert isinstance(exc, RemoteBenchError)


def test_every_registered_exception_round_trips():
    from benchctrl.net import errors

    for name in errors.known_class_names():
        cls = errors._registry()[name]
        payload = {
            "c": name,
            "mro": [c.__name__ for c in cls.__mro__ if c is not object],
            "msg": f"{name} message",
            "attrs": {"error_code": -101},
        }
        exc = errors.decode_exception(payload)
        assert isinstance(exc, BaseException)
        # Assert on args, not on `in str(exc)`. Containment was the original
        # assertion and it is satisfied by a *re-composed* message that merely
        # quotes the original -- which is how SDM4065AOverloadError shipped
        # doubling its own sentence. args is exact, and it also works for
        # KeyError, whose __str__ is always a repr of its argument.
        assert exc.args == (f"{name} message",), (
            f"{name} did not carry the message verbatim: {exc.args!r}"
        )
        # ...and it must still be the class the agent raised. A message check
        # alone passes on a silent degradation to RemoteBenchError.
        assert type(exc).__name__ == name, (
            f"{name} degraded to {type(exc).__name__} on decode"
        )


def test_a_constructor_that_reinterprets_its_first_argument_does_not_corrupt_the_message():
    """A constructor can *accept* the message and still not store it.

    ``SDM4065AOverloadError(function, range_)`` takes a string first, so
    ``cls(message)`` raises nothing and silently files the whole message under
    ``function``, then composes a new message quoting it. Measured over the real
    RPC wire on the bench: "RESistance input overloaded — ..." came back as
    "RESistance input overloaded — ... input overloaded — ...".

    This is a wire-fidelity bug, not a cosmetic one: the operator reads that text
    to decide what to widen, and ``function`` is meant to name a DMM function.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import SDM4065AOverloadError
    from benchctrl.net.errors import decode_exception, encode_exception

    original = SDM4065AOverloadError("RESistance", 100.0)
    restored = decode_exception(encode_exception(original))

    assert type(restored) is SDM4065AOverloadError
    assert str(restored) == str(original)
    # The give-away the containment check missed: the sentence appearing twice.
    assert str(restored).count("input overloaded") == 1
    # The attrs pass restores the real field values, so `function` is a function.
    assert restored.function == "RESistance"
    assert restored.range == 100.0


# --------------------------------------------------------------------------
# Recordings
# --------------------------------------------------------------------------


def test_record_captures_a_known_waveform(bench, arc):
    bench.sim.waveforms["mc"] = Square(low=0.0, high=0.1, freq_hz=50.0, duty=0.5)
    bench.sim.packed_frame_hz = 400.0
    arc.set_voltage(3.3)
    arc.set_output(True)

    with arc.record("mc", "mv") as rec:
        time.sleep(1.0)

    stats = rec.statistics("mc")
    assert stats.sample_count > 500
    assert stats.average == pytest.approx(0.05, abs=0.01)


def test_recording_is_usable_after_the_with_block(bench, arc, tmp_path):
    """sensor_profiler depends on this: rec.save() after the block exits."""
    arc.set_voltage(2.0)
    arc.set_output(True)
    with arc.record("mv") as rec:
        time.sleep(1.0)

    path = rec.save(tmp_path / "remote.opensmu")
    assert path.exists()
    assert len(rec.data("mv")) > 5

    from benchctrl.recording import Recording

    assert Recording.load(path).channels == rec.channels


def test_recording_data_is_refused_while_running(bench, arc):
    arc.set_output(True)
    with arc.record("mv") as rec:
        # Past the ~0.5 s reader-batch window — see KNOWN_LIMITATIONS A-4.
        time.sleep(1.0)
        with pytest.raises(BenchValueError, match="still running"):
            rec.data("mv")
        # Live queries work without transferring anything.
        assert rec.counts()["mv"] > 0


def test_live_statistics_avoid_transferring_samples(bench, arc):
    arc.set_voltage(3.3)
    arc.set_output(True)
    with arc.record("mv") as rec:
        time.sleep(1.0)  # past the reader-batch window (KNOWN_LIMITATIONS A-4)
        live = rec.live_statistics("mv")
        assert live["sample_count"] > 0
        assert live["average"] == pytest.approx(3.3, rel=0.05)


def test_over_long_recording_is_refused(bench, arc):
    with pytest.raises(BenchValueError, match="max_recording_s"):
        bench.client.call(
            "rec.start",
            {"device": "otii_arc", "channels": ["mv"], "expected_s": 100_000},
        )


def test_blob_transfer_is_checksummed(bench, arc):
    arc.set_output(True)
    with arc.record("mv") as rec:
        time.sleep(1.0)
    data = bench.client.fetch_blob(rec._blob_id)
    import hashlib

    assert hashlib.sha256(data).hexdigest() == rec._described["sha256"]


# --------------------------------------------------------------------------
# read_window's caller-keyed result
# --------------------------------------------------------------------------


def test_read_window_preserves_the_callers_channel_objects(bench, arc):
    from benchctrl.channels import StandardChannel

    requested = [StandardChannel.MAIN_VOLTAGE, "mc"]
    result = arc.read_window(requested, 0.3)
    assert set(result) == set(requested)
    assert StandardChannel.MAIN_VOLTAGE in result


# --------------------------------------------------------------------------
# Safety governor
# --------------------------------------------------------------------------


def test_agent_tracks_arm_state_from_the_wire(bench, arc):
    assert not bench.agent.governor.any_armed
    arc.set_output(True)
    _wait(lambda: bench.agent.governor.any_armed)
    arc.set_output(False)
    _wait(lambda: not bench.agent.governor.any_armed)


def test_deadman_disarms_when_the_client_vanishes(bench, arc):
    """The failure this whole layer exists for."""
    arc.set_voltage(3.3)
    arc.set_output(True)
    _wait(lambda: bench.sim.params[0x09] == 1)
    assert bench.agent.governor.any_armed

    # Kill the socket without a clean close — the host has gone away.
    bench.client._stop.set()
    bench.client._sock.close()

    # Wait on BOTH the hardware effect and the governor's bookkeeping. They are
    # not simultaneous: `Governor._make_safe` runs `safe_state(obj)` on the
    # device worker — which is what clears the sim's output register — and only
    # calls `_clear()` (which resets `output_armed`) after `job.done.wait()`
    # returns on the governor thread. Waiting on the register alone and then
    # asserting `any_armed` immediately is a race that loses under load:
    # measured 2 failures in 12 runs on an unmodified tree.
    _wait(lambda: bench.sim.params[0x09] == 0, timeout=8.0)
    _wait(lambda: not bench.agent.governor.any_armed, timeout=8.0)


def test_shutdown_disarms(bench, arc):
    arc.set_voltage(1.0)
    arc.set_output(True)
    _wait(lambda: bench.sim.params[0x09] == 1)
    bench.agent.shutdown()
    assert bench.sim.params[0x09] == 0


def test_safety_events_reach_the_client(bench, arc):
    arc.set_output(True)
    _wait(lambda: bench.agent.governor.any_armed)
    bench.agent.trip(__import__(
        "benchctrl.agent.safety", fromlist=["TripReason"]
    ).TripReason.OPERATOR)
    _wait(lambda: any(e.get("kind") == "safety_trip" for e in bench.client.events))


# --------------------------------------------------------------------------
# Worker serialization
# --------------------------------------------------------------------------


def test_concurrent_calls_are_serialized_per_device(bench, arc):
    """Transport is not thread-safe; the worker restores that invariant."""
    errors = []

    def hammer():
        try:
            for i in range(20):
                arc.set_voltage(1.0 + (i % 3) * 0.1)
        except Exception as exc:  # noqa: BLE001
            errors.append(exc)

    threads = [threading.Thread(target=hammer) for _ in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join(timeout=30)
    assert not errors
    assert bench.agent.workers.get("otii_arc").calls_served >= 80


def test_blocking_calls_are_clamped_server_side(bench, arc):
    """A long read must not starve the safety lane."""
    started = time.monotonic()
    arc.read_raw(60.0)  # clamped to max_blocking_s
    assert time.monotonic() - started < 15.0


# --------------------------------------------------------------------------
# Discovery over the wire
# --------------------------------------------------------------------------


def test_agent_reports_its_bench_inventory(bench):
    inventory = bench.client.discover()
    assert "devices" in inventory
    assert "count" in inventory


def test_agent_status_reports_workers_and_safety(bench, arc):
    arc.set_voltage(1.0)
    status = bench.client.status()
    assert "safety" in status
    assert "otii_arc" in status["workers"]
    # And which devices are open. Carried on the poll rather than only in
    # WELCOME because it is the one part of the device table that *changes*
    # while a session runs — see the section below for the panel that read
    # STANDBY through a whole test without it.
    assert status["devices"]["otii_arc"]["open"] is True


# --------------------------------------------------------------------------
# Which devices are open, on the fast poll
#
# ``describe()`` rides in WELCOME once and carries each device's whole remote
# surface: every method, property and mutator name. Open state is the only part
# of that table that changes during a session, and until ``sessions()`` existed
# nothing reported it after connect — so a consumer learned which devices were
# open exactly once, at the moment the answer is always "none of them". Measured
# on the bench: a 195s resistance sweep left both instruments reading STANDBY
# ("configured, not opened") for all of it while the agent drove them.
# --------------------------------------------------------------------------


class _Openable:
    """A device object cheap enough to open and close in a unit test."""

    def __init__(self) -> None:
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_the_open_state_table_tracks_a_device_through_open_and_close():
    """Both directions, because a flag that only rises is a latch.

    A registered-but-unopened device is the state a fresh agent is entirely in —
    devices open lazily so a powered-off Rigol does not stop the Arc being
    served — and a closed one is where ``agent.close`` and a run's teardown leave
    it. A consumer that saw only the rise would claim the agent holds a handle to
    an instrument it has let go of.
    """
    registry = DeviceRegistry()
    registry.register("otii_arc", lambda **kw: _Openable())

    assert registry.sessions() == {"otii_arc": {"open": False, "open_error": None}}

    registry.open("otii_arc")
    assert registry.sessions()["otii_arc"]["open"] is True

    registry.close("otii_arc")
    assert registry.sessions()["otii_arc"]["open"] is False, (
        "a closed device still reports an open session"
    )


def test_the_open_state_table_does_not_carry_the_method_surface(bench):
    """The reason this is not just ``describe()`` on the poll.

    The surface is expensive and immutable: it is introspected once per open and
    never changes, and for the Arc alone it is dozens of method and property
    names. Sending it every 5s would pay for the whole allowlist to learn one
    boolean. Asserted by size as well as by key, so a future field that smuggles
    the surface back in under another name still fails here.
    """
    described = bench.agent.registry.describe()
    sessions = bench.agent.registry.sessions()

    assert set(sessions) == {"otii_arc"}
    assert set(sessions["otii_arc"]) == {"open", "open_error"}
    assert "methods" not in sessions["otii_arc"]

    surface = next(d for d in described if d["key"] == "otii_arc")
    assert surface["methods"], "premise: the described form does carry the surface"
    assert len(repr(sessions)) < len(repr(described)) / 4, (
        "the poll's device table is not meaningfully smaller than the WELCOME one"
    )


def test_a_device_that_failed_to_open_reports_why_on_the_poll():
    """"The agent tried and could not" is a different fact from "not attached".

    An operator can act on the first one, and the poll is where they will see it:
    the failure usually happens on first use, minutes after WELCOME went past. The
    agent's own message is the useful part, so it is carried verbatim rather than
    reduced to a flag.
    """
    registry = DeviceRegistry()

    def _refuse(**kwargs):
        raise BenchValueError("no DP2031 found on any VISA resource")

    registry.register("rigol_dp2031", _refuse)
    with pytest.raises(BenchValueError):
        registry.open("rigol_dp2031")

    entry = registry.sessions()["rigol_dp2031"]
    assert entry["open"] is False
    assert "no DP2031 found" in entry["open_error"], (
        "the reason the open failed did not reach the status poll"
    )


@pytest.fixture()
def lazy_bench():
    """An agent that has *not* opened its device yet, and a connected client.

    Deliberately not the ``bench`` fixture: that one calls ``register_open``, so
    its WELCOME already reports the Arc open and a panel built from it could not
    tell a working fold from no fold at all. Lazy registration is also what the
    board actually runs — ``build_default_registry`` only declares openers, so an
    instrument that is powered off does not stop the others being served, and
    "open" genuinely starts False and changes mid-session.
    """
    from benchctrl.drivers.otii_arc import OtiiArc

    sim = SimulatedOtiiArc()
    sim.start()

    registry = DeviceRegistry()
    registry.register("otii_arc", OtiiArc.open, open_kwargs={"port": sim.port})
    agent = BenchAgent(registry, token=TOKEN, deadman_s=2.0, heartbeat_s=0.5)
    server = AgentServer(agent, host="127.0.0.1", port=0).start()

    endpoint = EndpointConfig(
        host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=0.5, deadman_s=2.0
    )
    client = RemoteClient(endpoint).connect()
    try:
        yield type(
            "LazyBench",
            (),
            {"sim": sim, "agent": agent, "server": server, "client": client},
        )
    finally:
        try:
            client.close()
        finally:
            server.stop()
            registry.close_all()
            sim.close()


def test_the_agents_own_status_makes_the_dashboard_read_the_device_open(lazy_bench):
    """The two processes, wired together, on the payload the agent really sends.

    This is the assertion the defect could not survive and its unit tests could.
    The panel folded a device table it invented for itself in one place and the
    agent published one from another, and nothing checked the two were keyed the
    same — the same shape as ``run_started`` versus ``run_start``, where the
    dashboard's tests passed happily for a run readout that stayed empty through
    every real run.

    So: a real agent, a real ``agent.status`` over a real socket, into the real
    state model, asserting the flag the instrument card is drawn from. The device
    is opened *after* the panel folded WELCOME, which is the whole scenario — the
    old panel had no source that could learn about it.
    """
    from benchctrl.dashboards.state import BenchStatus

    client = lazy_bench.client
    panel = BenchStatus()
    panel.apply_connected(client.welcome)
    assert not panel.slots["otii_arc"].opened, (
        "premise: nothing is open at connect, so WELCOME cannot be the source"
    )

    # A poll before anything is open must agree, or the assertion below would
    # pass against a fold that simply says True.
    panel.apply_status(client.status())
    assert not panel.slots["otii_arc"].opened

    client.attach("otii_arc")
    panel.apply_status(client.status())

    assert panel.slots["otii_arc"].opened, (
        "the agent reported this device open and the panel still read it as "
        "merely configured"
    )
    assert panel.to_dict()["slots"]["otii_arc"]["opened"] is True


# --------------------------------------------------------------------------
# Completeness — new driver methods must be classified
# --------------------------------------------------------------------------


def test_every_public_arc_method_is_classified(bench):
    """A new driver method must not silently become remotely callable."""
    from benchctrl.agent import dispatch

    buckets = dispatch.classify(bench.smu, "otii_arc")
    everything = set()
    for names in buckets.values():
        everything.update(names)

    public = {
        name
        for name in dir(type(bench.smu))
        if not name.startswith("_")
    }
    unclassified = public - everything
    assert not unclassified, f"unclassified public members: {sorted(unclassified)}"


def test_special_methods_are_not_generically_callable(bench):
    from benchctrl.agent import dispatch

    surface = bench.agent.registry.surface_of("otii_arc")
    for name in ("record", "stream", "start_recording", "read_window"):
        with pytest.raises(PolicyError, match="dedicated protocol verb"):
            dispatch.check_callable(surface, name)


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------


def _wait(predicate, timeout: float = 5.0, interval: float = 0.02) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        try:
            if predicate():
                return
        except Exception:  # noqa: BLE001
            pass
        time.sleep(interval)
    raise AssertionError("condition not met within timeout")

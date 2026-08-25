"""The PDU41002 in remote mode, end to end.

A driver can pass every local test and still be unreachable through the agent:
a missing entry in one of six registries, a return type the codec silently
drops, an exception the wire cannot reconstruct. None of those failures are
visible to :py:mod:`tests.test_bench_cyberpower_pdu41002`, which never leaves
the process.

The stack under test here is complete — proxy, wire protocol, agent dispatch,
device worker, production driver, pyserial, pty, simulator. Only the silicon is
fake.

Two things matter more for this device than for the instruments:

- **Exception *types* are the actionable payload.** For a DMM, an error
  degraded to ``RuntimeError`` costs a retry. Here, a policy refusal degraded
  to ``RuntimeError`` makes a *refused mains switch* look like a device fault,
  and a single-session collision degraded to ``ConnectionError`` looks like a
  wrong password. So each of those has its own test.
- **The writer-claim gate is what stands between a remote caller and the
  contactors**, and it is driven entirely by method naming. That is asserted
  here against the live agent surface, not just against the local
  introspection.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"
KEY = "cyberpower_pdu41002"


@pytest.fixture()
def remote_pdu():
    """An agent serving a simulated PDU, and an attached remote proxy.

    Built through :py:func:`make_pdu41002`, so the agent holds the
    *production* driver over a pty exactly as it would hold one over the FTDI
    cable.
    """
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3))

    registry = DeviceRegistry()
    registry.register_open(KEY, driver)
    agent = BenchAgent(registry, token=TOKEN, deadman_s=5.0, heartbeat_s=1.0)
    server = AgentServer(agent, host="127.0.0.1", port=0).start()

    endpoint = EndpointConfig(
        host="127.0.0.1",
        port=server.port,
        token=TOKEN,
        heartbeat_s=1.0,
        deadman_s=5.0,
    )
    client = RemoteClient(endpoint).connect()
    try:
        proxy = client.attach(KEY)
        yield type(
            "RemoteBench",
            (),
            {
                "proxy": proxy,
                "driver": driver,
                "sim": driver._benchctrl_sim,
                "client": client,
                "agent": agent,
            },
        )
    finally:
        try:
            client.close()
        finally:
            server.stop()
            driver.close()


# ---------------------------------------------------------------------------
# Reachability — the six registration sites
# ---------------------------------------------------------------------------


def test_the_device_key_is_servable(remote_pdu):
    assert remote_pdu.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    """Omitting this makes ``Config.from_dict`` *silently drop* the device —
    the worst of the six failure modes, because nothing raises."""
    from benchctrl.config import DEVICE_KEYS

    assert KEY in DEVICE_KEYS


def test_the_agent_has_an_opener_for_the_key():
    """The opener table is what lets a remote agent open the PDU itself rather
    than only accepting an already-open one.

    ``build_default_registry`` raises for a key with no opener, so a missing
    entry fails here rather than at the first attach on the board.
    """
    from benchctrl.agent.registry import build_default_registry

    assert KEY in build_default_registry([KEY]).keys


def test_the_agent_can_build_the_key_in_simulate_mode():
    """The other branch of the same function, keyed off the sim factory table.
    Both have to know about the device or ``benchctrl-agent --simulate`` cannot
    serve it."""
    from benchctrl.agent.registry import build_default_registry

    assert KEY in build_default_registry([KEY], simulate=True).keys


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for(KEY) is not None


def test_the_opener_does_not_route_through_autoserial():
    """Deliberate divergence from the QR10x, and worth pinning.

    ``transports.autoserial`` exists for the CH340, whose kernel driver may be
    absent; this FT232R has one. More to the point, autoserial *probes* ports —
    and probing means opening unrelated serial devices on a bench where one of
    them switches mains. The PDU is opened by explicit path only.
    """
    import inspect

    from benchctrl.agent import registry as registry_module

    source = inspect.getsource(registry_module.build_default_registry)
    pdu_opener = source[source.index("def _pdu("):]
    pdu_opener = pdu_opener[: pdu_opener.index("openers = {")]
    assert "autoserial" not in pdu_opener.replace("# ", "").split("return")[1]


def test_the_opener_injects_no_password():
    """``open_kwargs`` crosses the RPC wire, which is authenticated but
    **plaintext**. A password routed through the registry would have crossed the
    LAN in clear, so resolution happens inside ``open()`` from the *agent's own*
    environment instead."""
    import inspect

    from benchctrl.agent import registry as registry_module

    source = inspect.getsource(registry_module.build_default_registry)
    body = source[source.index("def _pdu("): source.index("openers = {")]
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "password" not in code


def test_every_read_method_is_exposed_over_the_wire(remote_pdu):
    for name in (
        "read_identity",
        "read_device_status",
        "read_outlet_config",
        "outlet_state",
        "outlet_states",
        "outlet_name",
        "measure_load_A",
        "measure_voltage_V",
        "measure_frequency_Hz",
    ):
        assert hasattr(remote_pdu.proxy, name), f"{name} not reachable remotely"


# ---------------------------------------------------------------------------
# Return types across the wire
# ---------------------------------------------------------------------------


def test_identity_survives_the_codec(remote_pdu):
    """``PDU41002Info`` must be in the codec's wire-type allowlist, or this
    comes back as a bare dict and attribute access fails."""
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002Info

    info = remote_pdu.proxy.read_identity()
    assert isinstance(info, PDU41002Info)
    assert info.model == "PDU41002"


def test_device_status_survives_the_codec(remote_pdu):
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002Status

    status = remote_pdu.proxy.read_device_status()
    assert isinstance(status, PDU41002Status)
    assert status.voltage_V == pytest.approx(121.7)


def test_a_none_power_factor_survives_the_wire(remote_pdu):
    """``None`` is the meaningful value at zero load, and it has to arrive as
    ``None`` rather than as ``0.0`` or the string ``"----"``.

    A codec that coerced it would report a purely reactive load where there is
    no load at all.
    """
    assert remote_pdu.proxy.read_device_status().power_factor is None


def test_the_outlet_config_map_survives_the_codec(remote_pdu):
    """A ``dict[int, OutletConfig]`` — two things the codec can break at once:
    the nested dataclass, and integer keys (JSON object keys are strings)."""
    from benchctrl.drivers.cyberpower_pdu41002 import OutletConfig

    cfg = remote_pdu.proxy.read_outlet_config()
    assert set(cfg) == set(range(1, 9))
    assert all(isinstance(v, OutletConfig) for v in cfg.values())
    assert cfg[1].on_delay_s == 3


def test_the_outlet_state_map_keeps_integer_keys(remote_pdu):
    """If these arrive as strings, ``states[1]`` raises ``KeyError`` remotely
    while working locally — the kind of asymmetry that only shows up on the
    board."""
    states = remote_pdu.proxy.outlet_states()
    assert set(states) == set(range(1, 9))
    assert all(isinstance(v, bool) for v in states.values())


def test_a_float_measurement_survives_the_wire(remote_pdu):
    assert remote_pdu.proxy.measure_frequency_Hz() == pytest.approx(60.0)


def test_a_bool_survives_the_wire(remote_pdu):
    assert remote_pdu.proxy.outlet_state(1) is True


def test_a_frozenset_property_survives_the_wire(remote_pdu):
    """Regression: ``frozenset`` is **not** a subclass of ``set``.

    The codec listed only ``set``, and ``allowed_outlets`` is a ``frozenset``
    (deliberately — so a caller cannot ``.add()`` an outlet into scope). Because
    a property snapshot rides along on *every* response, that one omission made
    every remote call on this device fail with "cannot encode frozenset", not
    just the getter — a total outage from a type distinction nothing else in the
    tree happened to exercise.

    JSON has no set, so both arrive as a list. That asymmetry is pre-existing
    codec convention and is not corrected here; what matters is that the values
    survive.
    """
    from benchctrl.net.codec import decode_value, encode_value

    assert sorted(decode_value(encode_value(frozenset({3, 1, 2})))) == [1, 2, 3]
    assert sorted(decode_value(encode_value({3, 1, 2}))) == [1, 2, 3]
    assert decode_value(encode_value(frozenset())) == []

    # And through the live stack, which is where it actually broke.
    assert sorted(remote_pdu.proxy.allowed_outlets) == [2, 3]
    assert sorted(remote_pdu.proxy.panic_outlets) == []


# ---------------------------------------------------------------------------
# Exceptions across the wire
# ---------------------------------------------------------------------------


def test_a_policy_refusal_keeps_its_type_remotely(remote_pdu):
    """The most important exception on this device.

    Degraded to ``RuntimeError``, a refused mains switch is indistinguishable
    from a device fault — and the remedy (widen the allowlist) is a deliberate
    human decision, not a retry. Exercised through the enforcement path since
    no public mutator exists yet.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002PolicyError

    with pytest.raises(PDU41002PolicyError):
        remote_pdu.driver._require_allowed(1)

    # And the type survives a wire round trip, not just a local raise.
    from benchctrl.net.errors import decode_exception, encode_exception

    try:
        remote_pdu.driver._require_allowed(1)
    except PDU41002PolicyError as exc:
        rebuilt = decode_exception(encode_exception(exc))
        assert isinstance(rebuilt, PDU41002PolicyError)
        assert "allowed_outlets" in str(rebuilt)


def test_a_command_error_keeps_its_type_and_attributes_remotely(remote_pdu):
    """``PDU41002CommandError.__init__`` takes ``(command, message,
    marker=None)`` — exactly the shape that fails to reconstruct if the codec
    assumes a single message string."""
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002CommandError

    remote_pdu.sim.inject_error("command")
    with pytest.raises(PDU41002CommandError) as exc:
        remote_pdu.proxy.read_identity()
    assert exc.value.command == "sys show"


def test_a_client_side_value_error_round_trips(remote_pdu):
    """Rejected before anything is sent, which also proves the proxy does not
    skip client-side validation — an aggregate target must be inexpressible
    remotely too, not just locally."""
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ValueError

    with pytest.raises(PDU41002ValueError):
        remote_pdu.proxy.outlet_state("all")


def test_bool_is_still_rejected_before_int_remotely(remote_pdu):
    """``True`` must not become outlet 1 over the wire either.

    Worth its own remote test: JSON has no distinct bool-vs-int problem, but a
    codec that normalised ``True`` to ``1`` on the way out would defeat the
    check without touching the driver.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ValueError

    with pytest.raises(PDU41002ValueError):
        remote_pdu.proxy.outlet_state(True)


def test_every_driver_exception_is_reconstructible(remote_pdu):
    """All nine, by name, through the actual encode/decode path.

    ``net/errors.py`` builds its registry by importing named classes from the
    driver module, so a typo there degrades an exception along its MRO
    *silently* — the caller sees ``PDU41002Error`` instead of
    ``PDU41002SessionError`` and cannot tell why.
    """
    from benchctrl.net.errors import decode_exception, encode_exception, known_class_names

    names = known_class_names()
    for name in (
        "PDU41002Error",
        "PDU41002ConnectionError",
        "PDU41002CommandError",
        "PDU41002ProtocolError",
        "PDU41002TimeoutError",
        "PDU41002ValueError",
        "PDU41002AuthError",
        "PDU41002PolicyError",
        "PDU41002SessionError",
    ):
        assert name in names, f"{name} would degrade along its MRO"

    import benchctrl.drivers.cyberpower_pdu41002.driver as mod

    for name in names:
        if not name.startswith("PDU41002"):
            continue
        cls = getattr(mod, name)
        exc = cls("boom") if name != "PDU41002CommandError" else cls("sys show", "boom")
        rebuilt = decode_exception(encode_exception(exc))
        assert type(rebuilt) is cls, f"{name} did not survive the round trip"


def test_the_session_error_does_not_blur_into_a_connection_error():
    """The distinction that makes the device's worst failure diagnosable.

    Single-session contention arrives *after* the password is accepted, so
    without a distinct type every occurrence gets misdiagnosed as bad
    credentials. ``PDU41002SessionError`` must therefore not be a subclass of
    ``PDU41002ConnectionError`` or ``PDU41002AuthError`` — an
    ``except PDU41002AuthError`` that caught it would send the operator to
    check the password, which is exactly the wrong place.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import (
        PDU41002AuthError,
        PDU41002ConnectionError,
        PDU41002SessionError,
    )

    assert not issubclass(PDU41002SessionError, PDU41002ConnectionError)
    assert not issubclass(PDU41002SessionError, PDU41002AuthError)
    assert not issubclass(PDU41002AuthError, PDU41002SessionError)


# ---------------------------------------------------------------------------
# The writer-claim gate
# ---------------------------------------------------------------------------


def test_reads_need_no_writer_claim(remote_pdu):
    """Metering and outlet *reporting* must work for a client that holds no
    claim, or a monitoring dashboard could not watch a bench it does not
    control."""
    surface = remote_pdu.agent.registry.surface_of(KEY)
    for name in ("read_identity", "outlet_states", "measure_load_A"):
        assert name in surface.methods
        assert name not in surface.mutators


def test_the_live_agent_surface_exposes_no_mutators(remote_pdu):
    """The reads-only claim, asserted against the agent rather than local
    introspection — this is the surface a remote caller actually sees.

    When switching lands, this becomes the list of the three switching methods.
    Until then, anything appearing here can move a contactor and was not
    reviewed as such.
    """
    assert remote_pdu.agent.registry.surface_of(KEY).mutators == frozenset()


def test_the_remote_surface_offers_no_way_to_switch_an_outlet(remote_pdu):
    """Belt and braces on the above: no *reachable* method name suggests
    switching, whatever its dispatch classification."""
    surface = remote_pdu.agent.registry.surface_of(KEY)
    for name in surface.methods:
        assert "ctrl" not in name
        assert not name.startswith("set_outlet")
        assert not name.startswith("reset_outlet")


# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------


def test_a_remote_config_round_trips_without_a_password():
    """The documented shape: transport in config, credential in the
    environment. This is the example people copy, so it must be the safe one.
    """
    from benchctrl.config import Config

    cfg = Config.from_dict(
        {
            "devices": {
                KEY: {
                    "mode": "remote",
                    "endpoint": "bench",
                    "open": {"host": "pdu-benchctrl", "allowed_outlets": [2, 3]},
                }
            },
            "endpoints": {"bench": {"host": "192.168.1.86", "port": 8765}},
        }
    )
    device = cfg.devices[KEY]
    assert device.mode == "remote"
    assert device.open["host"] == "pdu-benchctrl"
    assert "password" not in device.open
    assert cfg.to_dict()["devices"][KEY]["open"]["allowed_outlets"] == [2, 3]

"""The SDM4065A in remote mode, end to end.

The goal for this driver was *full remote support*, not a local driver that
happens to import. A driver can pass every local test and still be
unreachable through the agent: a missing entry in one of five registries, a
return type the codec drops, an exception the wire cannot reconstruct. Those
failures are invisible to :py:mod:`tests.test_bench_siglent_sdm4065a`, which
never leaves the process.

So the stack under test here is complete — proxy, wire protocol, agent
dispatch, device worker, production driver, pyvisa, pty, simulator. Only the
silicon is fake.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"


@pytest.fixture()
def remote_dmm():
    """An agent serving a simulated SDM4065A, and an attached remote proxy.

    Built through :py:func:`make_sdm4065a` so the agent holds the *production*
    driver over a pty, exactly as it would hold one over USB-TMC.
    """
    from benchctrl.sim.factories import make_sdm4065a

    driver = make_sdm4065a(sim={"dut_ohm": 100.0, "lead_ohm": 0.2})

    registry = DeviceRegistry()
    registry.register_open("siglent_sdm4065a", driver)
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
        proxy = client.attach("siglent_sdm4065a")
        yield type(
            "RemoteBench",
            (),
            {
                "proxy": proxy,
                "driver": driver,
                "sim": driver._benchctrl_sim,
                "client": client,
            },
        )
    finally:
        try:
            client.close()
        finally:
            server.stop()
            driver.close()


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------


def test_the_device_key_is_servable(remote_dmm):
    """Fails if ``siglent_sdm4065a`` is missing from DEVICE_KEYS or the
    agent's opener table — the two registrations easiest to forget."""
    assert remote_dmm.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    from benchctrl.config import DEVICE_KEYS

    assert "siglent_sdm4065a" in DEVICE_KEYS


def test_the_agent_has_an_opener_for_the_key():
    """The opener table is what lets a *remote* agent open the instrument
    itself, rather than only accepting an already-open one.

    ``build_default_registry`` raises ``BenchValueError`` for a key with no
    opener, so a missing entry in that table fails here rather than at the
    first attach on the board.
    """
    from benchctrl.agent.registry import build_default_registry

    built = build_default_registry(["siglent_sdm4065a"])
    assert "siglent_sdm4065a" in built.keys


def test_the_agent_can_build_the_key_in_simulate_mode():
    """``--simulate`` takes the other branch of the same function, keyed off
    the sim factory table rather than the opener table. Both have to know
    about the device or ``benchctrl-agent --simulate`` cannot serve it."""
    from benchctrl.agent.registry import build_default_registry

    built = build_default_registry(["siglent_sdm4065a"], simulate=True)
    assert "siglent_sdm4065a" in built.keys


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for("siglent_sdm4065a") is not None


def test_measurement_methods_are_exposed_over_the_wire(remote_dmm):
    for name in (
        "measure_resistance",
        "measure_resistance_4wire",
        "read",
        "null_now",
        "set_nplc",
        "configure_resistance",
        "info",
    ):
        assert hasattr(remote_dmm.proxy, name), f"{name} not reachable remotely"


# --------------------------------------------------------------------------
# Return types across the wire
# --------------------------------------------------------------------------


def test_identity_survives_the_codec(remote_dmm):
    """``SDM4065AInfo`` must be in the codec's wire-type allowlist, or this
    comes back as a bare dict and attribute access fails."""
    info = remote_dmm.proxy.info()
    assert info.model == "SDM4065A"
    assert info.manufacturer == "Siglent Technologies"
    assert info.resource.startswith("ASRL")


def test_a_float_reading_survives_the_wire(remote_dmm):
    assert remote_dmm.proxy.measure_resistance_4wire(200) == pytest.approx(100.0)


def test_two_wire_and_four_wire_differ_remotely(remote_dmm):
    """The same physical distinction as locally, proving the *command* and
    not just the return value crossed the wire correctly."""
    assert remote_dmm.proxy.measure_resistance(200) == pytest.approx(100.2)
    assert remote_dmm.proxy.measure_resistance_4wire(200) == pytest.approx(100.0)


def test_a_reading_list_survives_the_wire(remote_dmm):
    """``read()`` returns ``list[float]``; a codec that flattened or dropped
    it would silently lose every sample after the first."""
    remote_dmm.proxy.configure_resistance(200)
    remote_dmm.proxy.set_sample_count(3)
    values = remote_dmm.proxy.read()
    assert isinstance(values, list)
    assert len(values) == 3
    assert all(v == pytest.approx(100.2) for v in values)


def test_a_bool_survives_the_wire(remote_dmm):
    remote_dmm.proxy.configure_resistance(200)
    remote_dmm.proxy.set_autozero(True)
    assert remote_dmm.proxy.get_autozero() is True


def test_last_error_indexes_the_same_remotely_as_locally(remote_dmm):
    """``last_error()`` returns a tuple locally and a list remotely — tuples
    are not a JSON type, so the codec encodes them as lists.

    That asymmetry is pre-existing repo convention (``RigolDP2031`` and
    ``RigolDL3031A.last_error`` do the same), so it is not corrected here.
    What matters is that positional access keeps working either way, which
    is how every caller in the tree uses it. Pinned so a future codec change
    that turned it into a dict would be caught rather than silently breaking
    ``err[0]``.
    """
    assert remote_dmm.proxy.last_error() is None

    remote_dmm.sim.inject_error(-222, "Data out of range")
    remote_dmm.proxy.write("RESistance:RANGe 200")
    err = remote_dmm.proxy.last_error()

    assert err is not None
    assert err[0] == -222
    assert err[1] == "Data out of range"


# --------------------------------------------------------------------------
# Exceptions across the wire
# --------------------------------------------------------------------------


def test_overload_error_keeps_its_type_remotely(remote_dmm):
    """The overload sentinel must not degrade to a bare ``RuntimeError``.

    A remote caller catches this to widen the range and retry; if the type
    is lost, that recovery is impossible and the only signal left is a
    string.
    """
    from benchctrl.drivers.siglent_sdm4065a import SDM4065AOverloadError

    remote_dmm.sim.dut_ohm = 5_000.0
    with pytest.raises(SDM4065AOverloadError):
        remote_dmm.proxy.measure_resistance(200)


def test_overload_error_keeps_its_attributes_remotely(remote_dmm):
    """Not just the type — the fields a caller needs to act on.

    ``SDM4065AOverloadError.__init__`` takes two positional arguments, which
    is exactly the shape that fails to reconstruct if the codec assumes a
    single message string.
    """
    from benchctrl.drivers.siglent_sdm4065a import SDM4065AOverloadError

    remote_dmm.sim.dut_ohm = 5_000.0
    with pytest.raises(SDM4065AOverloadError) as exc:
        remote_dmm.proxy.measure_resistance(200)
    assert "overload" in str(exc.value).lower()


def test_a_client_side_value_error_round_trips(remote_dmm):
    """The 2 MΩ range belongs to the SDM4055A. Rejected before anything is
    sent, so this also proves validation is not skipped by the proxy."""
    from benchctrl.drivers.siglent_sdm4065a import SDM4065AValueError

    with pytest.raises(SDM4065AValueError):
        remote_dmm.proxy.measure_resistance(2e6)


def test_an_off_list_nplc_is_rejected_remotely(remote_dmm):
    from benchctrl.drivers.siglent_sdm4065a import SDM4065AValueError

    with pytest.raises(SDM4065AValueError):
        remote_dmm.proxy.set_nplc(5)


def test_a_command_error_keeps_its_code_remotely(remote_dmm):
    from benchctrl.drivers.siglent_sdm4065a import SDM4065ACommandError

    remote_dmm.sim.inject_error(-113, "Undefined header")
    remote_dmm.proxy.write("NOSUCH:THING 1")
    with pytest.raises(SDM4065ACommandError) as exc:
        remote_dmm.proxy.raise_if_error()
    assert exc.value.code == -113


def test_every_sdm4065a_exception_is_wire_registered():
    """A type absent from the registry degrades along its MRO and the caller
    loses the ability to distinguish it."""
    from benchctrl.net.errors import known_class_names

    names = set(known_class_names())
    for name in (
        "SDM4065AError",
        "SDM4065AConnectionError",
        "SDM4065ACommandError",
        "SDM4065AOverloadError",
        "SDM4065ATimeoutError",
        "SDM4065AValueError",
    ):
        assert name in names, f"{name} missing from the error registry"


# --------------------------------------------------------------------------
# Stateful sequences across the wire
# --------------------------------------------------------------------------


def test_the_null_sequence_works_remotely(remote_dmm):
    """The whole point of the driver, over a socket.

    Several round trips whose ordering matters (state before value, READ?
    rather than MEASure?), so this catches a proxy that reordered or
    coalesced calls — and confirms instrument state persists between
    separate remote calls rather than being reset per request.
    """
    proxy = remote_dmm.proxy
    proxy.configure_resistance(200)
    remote_dmm.sim.dut_ohm = 0.0  # leads shorted

    offset = proxy.null_now(samples=3)
    assert offset == pytest.approx(0.2, abs=1e-6)
    assert proxy.get_null() is True
    assert proxy.get_null_auto() is False

    remote_dmm.sim.dut_ohm = 100.0  # DUT restored
    assert proxy.read_nulled() == pytest.approx(100.0, abs=1e-6)


def test_configuration_persists_between_remote_calls(remote_dmm):
    """Each remote call is a separate message; instrument state must live on
    the device, not in the proxy."""
    proxy = remote_dmm.proxy
    proxy.configure_resistance(200)
    proxy.set_nplc(100)
    assert proxy.get_nplc() == pytest.approx(100.0)
    assert proxy.get_range() == pytest.approx(200.0)


def test_a_38_milliohm_offset_survives_the_wire(remote_dmm):
    """The QR10x cross-validation premise, remotely.

    The real QR101A-1M-R1 reads 100.038 Ω at a 100.0 Ω setpoint. If the
    codec rounded floats for transport — a plausible optimisation — the
    effect being measured would vanish before the caller saw it.
    """
    remote_dmm.sim.dut_ohm = 100.038
    reading = remote_dmm.proxy.measure_resistance_4wire(200)
    assert reading == pytest.approx(100.038, abs=1e-6)
    assert reading != pytest.approx(100.0, abs=1e-3)

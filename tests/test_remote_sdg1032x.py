"""The SDG1032X in remote mode, end to end.

Same premise as :py:mod:`tests.test_remote_sdm4065a`: a driver can pass every
local test and still be unreachable through the agent — a missing entry in one
of the registries, a dataclass the codec degrades to a dict, an exception the
wire hands back as ``RuntimeError``. This generator adds two shapes the DMM
never had: a nested dataclass (``BasicWave`` as the carrier inside
``Modulation``/``Sweep``/``Burst``) and payloads above the 64 KB inline limit
(``read_screen()``'s BMP, a full-length ``ArbData``), which must arrive as
blobs with every byte intact.

The stack under test is complete — proxy, wire protocol, agent dispatch,
device worker, production driver, pyvisa, pty, simulator. Only the silicon is
fake.
"""

from __future__ import annotations

import hashlib
import struct

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

KEY = "siglent_sdg1032x"
TOKEN = "test-token-do-not-use-in-anger"


@pytest.fixture()
def remote_awg():
    """An agent serving a simulated SDG1032X, and an attached remote proxy.

    ``screen_px`` is set so the simulated screen is well above the codec's
    64 KB inline limit: the blob path is the thing under test, and a screen
    small enough to inline would silently skip it.
    """
    from benchctrl.sim.factories import make_sdg1032x

    # 160 px = 76.8 KB: above the 64 KB inline limit (the blob path is the
    # thing under test) but no larger — the screen crosses a pty, and under a
    # loaded full-suite run a 120 KB one took the worker past its 20 s budget.
    driver = make_sdg1032x(sim={"screen_px": 160})

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


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------


def test_the_device_key_is_servable(remote_awg):
    assert remote_awg.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    """``Config.from_dict`` silently *drops* a device whose key is not in
    DEVICE_KEYS, so a missing entry here means agent.json cannot even name
    the generator."""
    from benchctrl.config import DEVICE_KEYS

    assert KEY in DEVICE_KEYS


def test_the_agent_has_an_opener_for_the_key():
    """The hardware path. ``build_default_registry`` raises ``BenchValueError``
    for a key with no opener, so a missing table entry fails here rather than
    at the first attach on the board."""
    from benchctrl.agent.registry import build_default_registry

    assert KEY in build_default_registry([KEY]).keys


def test_the_agent_can_build_the_key_in_simulate_mode():
    """``--simulate`` takes the other branch, keyed off the sim factory table."""
    from benchctrl.agent.registry import build_default_registry

    assert KEY in build_default_registry([KEY], simulate=True).keys


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for(KEY) is not None


def test_the_opener_injects_no_channel_grant_or_amplitude_cap():
    """``allowed_channels`` and ``max_amplitude_vpp`` come from agent.json.

    The opener is the *hardware* path; a default there would let an agent
    config that forgot the grant energise a BNC at 20 Vpp. The closure must
    exist and pass through what it is given, nothing more.
    """
    import inspect

    from benchctrl.agent import registry as registry_module

    source = inspect.getsource(registry_module.build_default_registry)
    body = source[source.index("def _sdg(") : source.index("openers = {")]
    code = "\n".join(line for line in body.splitlines() if not line.strip().startswith("#"))
    assert "allowed_channels" not in code
    assert "max_amplitude_vpp" not in code
    assert "SiglentSDG1032X.open(**kw)" in code


def test_the_control_surface_is_exposed_over_the_wire(remote_awg):
    for name in (
        "info",
        "get_output",
        "set_output",
        "disable_outputs",
        "get_basic_wave",
        "set_basic_wave",
        "set_amplitude",
        "get_modulation",
        "get_sweep",
        "get_burst",
        "list_arbs",
        "write_arb",
        "read_arb",
        "read_counter",
        "get_coupling",
        "get_harmonics",
        "get_clock",
        "get_lan_config",
        "read_screen",
        "trigger_key",
    ):
        assert hasattr(remote_awg.proxy, name), f"{name} not reachable remotely"


# --------------------------------------------------------------------------
# Return types across the wire
# --------------------------------------------------------------------------


def test_identity_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XInfo

    info = remote_awg.proxy.info()
    assert isinstance(info, SDG1032XInfo)
    assert info.model.startswith("SDG10")
    assert info.resource.startswith("ASRL")


def test_output_state_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import OutputState

    st = remote_awg.proxy.get_output(1)
    assert isinstance(st, OutputState)
    assert st.channel == 1
    assert st.enabled is False


def test_basic_wave_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import BasicWave

    wave = remote_awg.proxy.get_basic_wave(1)
    assert isinstance(wave, BasicWave)
    assert wave.channel == 1
    assert wave.wave_type is not None
    assert isinstance(wave.frequency_hz, float)


def test_modulation_carries_its_nested_carrier_typed(remote_awg):
    """The nested case. ``Modulation.carrier`` is a ``BasicWave``; a codec
    that knew ``Modulation`` but not ``BasicWave`` would hand back a
    ``Modulation`` whose carrier is a bare dict, and ``.carrier.frequency_hz``
    would fail one attribute later than the type check."""
    from benchctrl.drivers.siglent_sdg1032x import BasicWave, Modulation

    remote_awg.proxy.set_modulation(1, enabled=True, type="AM")
    mod = remote_awg.proxy.get_modulation(1)
    assert isinstance(mod, Modulation)
    assert mod.enabled is True
    assert isinstance(mod.carrier, BasicWave), "nested carrier degraded to a dict"
    assert mod.carrier.channel is None
    assert isinstance(mod.carrier.frequency_hz, float)


def test_burst_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import Burst

    burst = remote_awg.proxy.get_burst(1)
    assert isinstance(burst, Burst)
    assert burst.channel == 1
    assert isinstance(burst.enabled, bool)


def test_the_arb_catalogue_survives_the_codec(remote_awg):
    """``list_arbs`` returns a tuple of ``ArbInfo``; the tuple arrives as a
    list (JSON has no tuple) but every element must still be typed."""
    from benchctrl.drivers.siglent_sdg1032x import ArbInfo

    arbs = remote_awg.proxy.list_arbs()
    assert len(arbs) > 0
    assert all(isinstance(a, ArbInfo) for a in arbs)
    assert all(a.builtin for a in arbs)


def test_counter_reading_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import CounterReading

    reading = remote_awg.proxy.read_counter()
    assert isinstance(reading, CounterReading)
    assert isinstance(reading.enabled, bool)


def test_coupling_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import Coupling

    coupling = remote_awg.proxy.get_coupling()
    assert isinstance(coupling, Coupling)
    assert isinstance(coupling.trace, bool)


def test_harmonics_survive_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import Harmonic

    harm = remote_awg.proxy.get_harmonics(1)
    assert isinstance(harm, Harmonic)
    assert harm.channel == 1


def test_clock_config_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import ClockConfig

    clock = remote_awg.proxy.get_clock()
    assert isinstance(clock, ClockConfig)
    assert clock.source in ("INT", "EXT")


def test_lan_config_survives_the_codec(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import LanConfig

    lan = remote_awg.proxy.get_lan_config()
    assert isinstance(lan, LanConfig)


def test_every_sdg1032x_dataclass_is_a_wire_type():
    """The allowlist, checked directly, so a dataclass no test above happens
    to read (``Sweep``, ``SyncConfig``, ``NumberFormat``...) is still pinned."""
    from benchctrl.net.codec import wire_type_names

    names = set(wire_type_names())
    for name in (
        "SDG1032XInfo",
        "OutputState",
        "BasicWave",
        "Modulation",
        "Sweep",
        "Burst",
        "ArbInfo",
        "ArbSelection",
        "ArbData",
        "SyncConfig",
        "ClockConfig",
        "CounterReading",
        "Coupling",
        "Harmonic",
        "ProtectionState",
        "LanConfig",
        "NumberFormat",
    ):
        assert name in names, f"{name} missing from the codec allowlist"


# --------------------------------------------------------------------------
# Large payloads: blobs
# --------------------------------------------------------------------------


def test_the_screen_arrives_whole_as_a_blob(remote_awg):
    """``read_screen()`` is a BMP far above the 64 KB inline limit, so it
    crosses as a blob reference the client fetches separately. Every byte
    must survive: compared by digest against the same read made locally
    through the driver, which is the ground truth the sim rendered."""
    remote = remote_awg.proxy.read_screen()
    assert isinstance(remote, bytes)
    assert remote[:2] == b"BM"
    declared = struct.unpack("<I", remote[2:6])[0]
    assert len(remote) == declared
    assert len(remote) > 64 * 1024, "screen too small to exercise the blob path"

    local = remote_awg.driver.read_screen()
    assert hashlib.sha256(remote).hexdigest() == hashlib.sha256(local).hexdigest()


def test_arb_codes_survive_the_wire_intact(remote_awg):
    """``ArbData.codes`` is bytes inside a dataclass: inline below 64 KB and
    a blob above, both through the dataclass path. Both sizes are exercised
    because they take different branches of the encoder."""
    from benchctrl.drivers.siglent_sdg1032x import ARB_MAX_SAMPLES, ArbData

    small = [0, 16383, 32767, 0, -16384, -32768, 0, 100]
    got = remote_awg.proxy.write_arb("rmt_small", small)
    assert isinstance(got, ArbData)
    assert got.name == "rmt_small"
    assert got.samples[: len(small)] == tuple(small)

    back = remote_awg.proxy.read_arb("rmt_small")
    assert isinstance(back, ArbData)
    assert back.codes[: 2 * len(small)] == got.codes[: 2 * len(small)]

    # Full length: 16384 samples * 2 bytes = 32 KB of codes, which is still
    # below the inline limit -- so push the whole dataclass over it by size of
    # the transfer rather than pretending; what matters is that a max-length
    # waveform round-trips bit-for-bit through whichever path it takes.
    big = [((i * 37) % 65536) - 32768 for i in range(ARB_MAX_SAMPLES)]
    uploaded = remote_awg.proxy.write_arb("rmt_big", big)
    # The stored codes are the *escaped* ones (newline bytes moved, counted
    # in ``nudged``); what must survive the wire is the instrument's copy.
    import struct

    from benchctrl.drivers.siglent_sdg1032x import escape_codes

    expected, nudged = escape_codes(struct.pack(f"<{len(big)}h", *big))
    assert uploaded.codes == expected and uploaded.nudged == nudged > 0
    assert (
        hashlib.sha256(uploaded.codes).hexdigest()
        == hashlib.sha256(remote_awg.driver.read_arb("rmt_big").codes).hexdigest()
    )


# --------------------------------------------------------------------------
# Exceptions across the wire
# --------------------------------------------------------------------------


def test_a_verify_error_keeps_its_type_and_fields_remotely(remote_awg):
    """The generator's only rejection signal. Into 50 Ω the amplitude limit
    halves to 10 Vpp; asking for 15 is accepted, clamped, and the read-back
    disagrees (the bench unit clamps rather than errors, and so does the
    sim). That must arrive as ``SDG1032XVerifyError`` *with* the fields a
    caller acts on, not as a bare ``RuntimeError`` carrying a string.

    Not triggered through ``MAX_OUTPUT_AMP``: on the bench that cap is
    accepted but neither echoed nor enforced, and the simulator is faithful
    to that, so the only cap that holds is the driver's own — a
    ``SDG1032XPolicyError``, tested separately.
    """
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XVerifyError

    remote_awg.proxy.set_output_load(1, 50.0)
    with pytest.raises(SDG1032XVerifyError) as excinfo:
        remote_awg.proxy.set_amplitude(1, 15.0)
    err = excinfo.value
    assert err.channel == 1
    assert err.field == "amplitude_vpp"
    assert err.wanted == pytest.approx(15.0)
    assert err.got == pytest.approx(10.0)
    assert remote_awg.sim.rejections, "the sim did not record the clamp"


def test_a_policy_error_keeps_its_type_remotely(remote_awg):
    """An Output key through ``trigger_key`` would energise a channel outside
    the governor's watch. The driver refuses it; the refusal must not look
    like a device fault on the far side."""
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XPolicyError

    with pytest.raises(SDG1032XPolicyError):
        remote_awg.proxy.trigger_key("KB_OUTPUT1")
    assert remote_awg.proxy.get_output(1).enabled is False


def test_a_value_error_round_trips(remote_awg):
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XValueError

    with pytest.raises(SDG1032XValueError):
        remote_awg.proxy.get_output(3)


def test_every_sdg1032x_exception_is_wire_registered():
    from benchctrl.net.errors import known_class_names

    names = set(known_class_names())
    for name in (
        "SDG1032XError",
        "SDG1032XConnectionError",
        "SDG1032XTimeoutError",
        "SDG1032XValueError",
        "SDG1032XProtocolError",
        "SDG1032XVerifyError",
        "SDG1032XPolicyError",
    ):
        assert name in names, f"{name} missing from the error registry"


def test_a_verify_error_reconstructs_without_its_constructor(remote_awg):
    """``SDG1032XVerifyError.__init__`` demands four keyword arguments. The
    decoder never calls it with them — it allocates and restores attributes —
    so this pins that the class survives that path rather than degrading to
    ``RemoteBenchError`` when ``cls(message)`` raises ``TypeError``."""
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XVerifyError
    from benchctrl.net.errors import decode_exception, encode_exception

    original = SDG1032XVerifyError(
        "C1 amplitude_vpp: asked for 3.0", channel=1, field="amplitude_vpp", wanted=3.0, got=2.0
    )
    rebuilt = decode_exception(encode_exception(original))
    assert type(rebuilt) is SDG1032XVerifyError
    assert (rebuilt.channel, rebuilt.field, rebuilt.wanted, rebuilt.got) == (
        1,
        "amplitude_vpp",
        3.0,
        2.0,
    )


# --------------------------------------------------------------------------
# Properties, claims, and the governor
# --------------------------------------------------------------------------


def test_the_property_snapshot_makes_no_instrument_io(remote_awg):
    """``channels`` / ``allowed_channels`` ride along on every response as the
    property snapshot. They are answered from the driver's own state; if one
    of them queried the instrument, every call would cost an extra round trip
    and the snapshot would be a liability rather than a convenience."""
    before = len(remote_awg.sim.command_log)
    assert remote_awg.proxy.channels == [1, 2]
    assert remote_awg.proxy.allowed_channels == [1, 2]
    assert remote_awg.proxy.max_amplitude_vpp is None
    assert len(remote_awg.sim.command_log) == before, "a property snapshot talked to the sim"


def test_reads_still_work_without_the_writer_claim(remote_awg):
    """Watching the generator must not require taking it."""
    remote_awg.client.call("agent.release", {"device": KEY})
    try:
        assert remote_awg.proxy.get_output(1).enabled is False
        assert remote_awg.proxy.get_basic_wave(1).channel == 1
        assert remote_awg.proxy.info().model.startswith("SDG10")
    finally:
        remote_awg.client.call("agent.claim", {"device": KEY})


def test_output_changes_are_refused_without_the_writer_claim(remote_awg):
    """``attach()`` claims automatically, so the claim is released first. The
    refusal is the agent's, before dispatch: the instrument must see nothing."""
    from benchctrl.net.errors import PolicyError

    remote_awg.client.call("agent.release", {"device": KEY})
    try:
        before = len(remote_awg.sim.command_log)
        with pytest.raises(PolicyError) as excinfo:
            remote_awg.proxy.set_output(1, True)
        assert "claim" in str(excinfo.value).lower()
        assert len(remote_awg.sim.command_log) == before, (
            "the sim saw a command despite the refusal"
        )
    finally:
        remote_awg.client.call("agent.claim", {"device": KEY})
    assert remote_awg.proxy.get_output(1).enabled is False


def test_the_governor_arms_on_set_output_and_disarms_on_disable_outputs(remote_awg):
    """``set_output(channel, True)`` energises a BNC, so the safety governor
    must count it as arming — that is what makes the deadman drive the bench
    safe if this client vanishes. ``disable_outputs()`` is the no-argument
    disarm the safe-stop path calls, and it must clear the same state."""
    governor = remote_awg.agent.governor
    assert KEY not in governor.armed_devices

    st = remote_awg.proxy.set_output(1, True)
    assert st.enabled is True
    assert KEY in governor.armed_devices

    states = remote_awg.proxy.disable_outputs()
    assert set(states) == {1, 2}
    assert all(not s.enabled for s in states.values())
    assert KEY not in governor.armed_devices
    assert remote_awg.proxy.get_output(1).enabled is False


def test_an_observer_session_can_read_the_screen_as_a_blob(remote_awg):
    """The dashboard's path: an observer session, ``device.read`` of
    ``read_screen``, a payload above the inline limit. The blob fetch that
    completes it must be open to observers too, or the read is a tease."""
    from benchctrl.config import EndpointConfig
    from benchctrl.net.client import RemoteClient

    host, port = remote_awg.client.endpoint.host, remote_awg.client.endpoint.port
    obs = RemoteClient(
        EndpointConfig(host=host, port=port, token=TOKEN, heartbeat_s=1.0, deadman_s=5.0),
        observer=True,
    ).connect()
    try:
        data = obs.call(
            "device.read",
            {"device": KEY, "method": "read_screen", "args": [], "kwargs": {}, "want_props": False},
        )
        assert isinstance(data, (bytes, bytearray)) and data[:2] == b"BM"
        assert len(data) > 64 * 1024, "the test must exercise the blob path"
        assert (
            hashlib.sha256(data).hexdigest()
            == hashlib.sha256(remote_awg.driver.read_screen()).hexdigest()
        )
    finally:
        obs.close()

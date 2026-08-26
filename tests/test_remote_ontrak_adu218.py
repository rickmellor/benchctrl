"""The ADU218 in remote mode, end to end.

A driver can pass every local test and still be unreachable through the agent:
a missing entry in one of seven registries, a return type the codec silently
drops, an exception the wire cannot reconstruct. None of those failures are
visible to :py:mod:`tests.test_bench_adu218`, which never leaves the process.

**Four of the seven registration sites fail silently.** ``DEVICE_KEYS``
(file path), ``net/errors.py``, the FUI ``INSTRUMENTS`` list and
``discovery.SIGNATURES`` all produce a working-looking driver with a missing
capability, so each one gets a test here rather than being assumed.

The stack under test is complete — proxy, wire protocol, agent dispatch, device
worker, the production USBDEVFS link's framing, the simulator. Only the ioctl is
fake.

Two things matter more for this device than for the instruments:

- **``ADU218TimeoutError`` is the device's *only* error signal.** Silence is
  what an absent command, a bad argument and a write-only command all look like,
  so a timeout degraded to ``RuntimeError`` over the wire erases the single
  diagnostic this device offers.
- **``ADU218PolicyError`` must not read as a device fault.** The remedy for a
  refused relay is widening the allowlist — a deliberate human decision, not a
  retry. Degraded along its MRO it becomes indistinguishable from the hardware
  misbehaving.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"
KEY = "ontrak_adu218"


@pytest.fixture()
def remote_adu():
    """An agent serving a simulated ADU218, and an attached remote proxy.

    Built through :py:func:`make_adu218`, so the agent holds the *production*
    driver over the production link class exactly as it would over real USB —
    only ``_transfer()`` is replaced.
    """
    from benchctrl.sim.factories import make_adu218

    driver = make_adu218(allowed_relays=(0, 1))

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
                "link": driver._benchctrl_sim,
                "model": driver._benchctrl_sim.device_model,
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
# Reachability — the seven registration sites
# ---------------------------------------------------------------------------


def test_the_device_key_is_servable(remote_adu):
    assert remote_adu.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    """Omitting this makes ``Config.from_dict`` *silently drop* the device from
    a config **file** — the worst of the seven failure modes, because nothing
    raises. The ``--device`` flag path does raise, which is why the flag is not
    evidence the file path works."""
    from benchctrl.config import DEVICE_KEYS

    assert KEY in DEVICE_KEYS


def test_the_key_survives_a_config_file_round_trip():
    """The silent path itself, exercised. A key absent from ``DEVICE_KEYS`` is
    dropped here with no error, and the operator sees an agent that simply does
    not serve the device."""
    from benchctrl.config import Config

    config = Config.from_dict({"devices": {KEY: {"open": {"timeout_ms": 200}}}})
    assert KEY in config.devices
    assert config.devices[KEY].open["timeout_ms"] == 200


def test_the_agent_has_an_opener_for_the_key():
    """The opener table is what lets a remote agent open the device itself
    rather than only accepting an already-open one. ``build_default_registry``
    raises for a key with no opener, so a miss fails here rather than at the
    first attach on the board."""
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


def test_the_opener_takes_no_transport_argument():
    """Deliberate divergence from every other opener in the table.

    This device is USB HID: it finds its own node by walking ``/dev/bus/usb``
    and matching VID/PID, so identity comes from the descriptor rather than from
    a path that renumbers on re-plug. Routing it through ``transports.autoserial``
    — the CH340 workaround — would be meaningless *and* would probe unrelated
    serial ports on a bench where one of them switches mains.
    """
    import inspect
    import re

    from benchctrl.agent import registry as registry_module

    source = inspect.getsource(registry_module.build_default_registry)
    # Bounded at the next opener, not at ``openers = {``: correct today only
    # because ``_adu`` happens to be defined last, and this is exactly the slice
    # that broke the PDU's equivalent test when this opener was added after it.
    body = source[source.index("def _adu(") :]
    following = re.search(r"\n    def \w+\(", body)
    body = body[: following.start()] if following else body[: body.index("openers = {")]
    code = "\n".join(
        line for line in body.splitlines() if not line.strip().startswith("#")
    )
    assert "autoserial" not in code
    # Whole words only: ``port`` is a substring of ``import``, and matching it
    # loosely would fail on the lazy import every opener in this table uses.
    for word in ("port", "transport", "device", "baud"):
        assert not re.search(rf"\b{word}\b", code), f"the opener mentions {word}"


def test_the_instrument_appears_on_the_dashboard():
    """A silent site: the FUI reads a hardcoded list, so a driver missing from
    it works perfectly over MCP and is invisible on the panel."""
    from benchctrl.dashboards.fui.view import INSTRUMENTS

    spec = next((row for row in INSTRUMENTS if row["key"] == KEY), None)
    assert spec is not None
    assert spec["kind"] == "switch"


def test_the_device_is_discoverable_by_descriptor():
    """The other silent site. A signature with no scanner, or a scanner with no
    signature, both read as "device not present" rather than as a gap.

    Identification here is entirely passive — VID/PID from sysfs, never a write
    — which is why it is a ``SIGNATURES`` entry rather than a probe.
    """
    from benchctrl.discovery import SIGNATURES

    signature = next((s for s in SIGNATURES if s.device_key == KEY), None)
    assert signature is not None
    assert (signature.vid, signature.pid) == (0x0A07, 0x00DA)
    assert signature.transport == "usbfs"


def test_a_machine_with_no_usb_sysfs_still_discovers_other_transports():
    """Regression, and the reason ``scan_usbfs()`` swallows its own error.

    ``enumerate_devices()`` *raises* for a missing sysfs root — correct for a
    driver about to open a device, wrong for a scan. Because ``discover()``
    merges one list, that exception took out **25 tests** including VISA and
    usbtmc ones that have nothing to do with this device.
    """
    from benchctrl import discovery
    from benchctrl.drivers.ontrak_adu218 import usbfs

    calls = []

    def explode(*args, **kwargs):
        calls.append(1)
        raise usbfs.Adu218LinkError(
            "cannot list /sys/bus/usb/devices: [Errno 2] No such file or directory"
        )

    # ``scan_usbfs`` imports ``enumerate_devices`` from the driver package at
    # call time, so the driver module is the seam -- patching an attribute on
    # ``discovery`` would silently patch nothing and the test would pass while
    # proving nothing.
    original = usbfs.enumerate_devices
    usbfs.enumerate_devices = explode
    try:
        assert discovery.scan_usbfs() == []
        discovery.discover(probe=False)  # must not raise
    finally:
        usbfs.enumerate_devices = original

    # The patched function has to have actually been reached twice, or the
    # assertions above pass without the error path ever running -- the shape
    # where rc=0 means nothing ran.
    assert len(calls) == 2, f"the scan never reached enumerate_devices ({calls})"


def test_every_read_method_is_exposed_over_the_wire(remote_adu):
    for name in (
        "read_identity",
        "relay_state",
        "relay_states",
        "relay_mask",
        "input_state",
        "input_states",
        "input_mask",
        "read_counter",
        "read_counters",
        "read_debounce",
        "read_debounce_ms",
        "read_watchdog",
        "read_watchdog_tripped",
    ):
        assert hasattr(remote_adu.proxy, name), f"{name} not reachable remotely"


def test_every_write_method_is_exposed_over_the_wire(remote_adu):
    for name in (
        "set_relay_state",
        "set_relay_port",
        "reset_relays",
        "clear_counter",
        "set_debounce",
        "set_watchdog",
    ):
        assert hasattr(remote_adu.proxy, name), f"{name} not reachable remotely"


# ---------------------------------------------------------------------------
# Return types across the wire
# ---------------------------------------------------------------------------


def test_identity_survives_the_codec(remote_adu):
    """``ADU218Info`` must be in the codec's wire-type allowlist, or this comes
    back as a bare dict and attribute access fails."""
    from benchctrl.drivers.ontrak_adu218 import ADU218Info

    info = remote_adu.proxy.read_identity()
    assert isinstance(info, ADU218Info)
    assert info.model == "ADU218"
    assert (info.vendor_id, info.product_id) == (0x0A07, 0x00DA)


def test_the_relay_state_map_keeps_integer_keys(remote_adu):
    """If these arrive as strings, ``states[0]`` raises ``KeyError`` remotely
    while working locally — the asymmetry that only shows up on the board.

    Relay 0 also makes this sharper than the PDU's equivalent: JSON turns the
    key ``0`` into ``"0"``, and ``"0"`` is falsy-looking in a way ``"1"`` is not.
    """
    states = remote_adu.proxy.relay_states()
    assert set(states) == set(range(8))
    assert all(isinstance(v, bool) for v in states.values())


def test_the_counter_map_keeps_integer_keys(remote_adu):
    counters = remote_adu.proxy.read_counters()
    assert set(counters) == set(range(8))
    assert all(isinstance(v, int) for v in counters.values())


def test_the_input_map_survives_as_tuples_of_bool(remote_adu):
    """``dict[str, tuple[bool, ...]]`` — two things at once: JSON has no tuple,
    and the MSB-first reversal must have happened driver-side, not been undone
    by a codec that re-sorted anything."""
    remote_adu.model.set_input("B", 3, True)
    states = remote_adu.proxy.input_states()
    assert set(states) == {"A", "B"}
    assert list(states["B"]) == [False, False, False, True]
    assert all(isinstance(v, bool) for v in states["A"])


def test_a_bool_survives_the_wire(remote_adu):
    """And specifically ``False`` rather than ``None``: an unwired input reads
    ``False``, and a codec dropping it would make "not asserted" and "not
    reported" the same value."""
    assert remote_adu.proxy.relay_state(0) is False
    assert remote_adu.proxy.input_state("A", 0) is False


def test_a_frozenset_property_survives_the_wire(remote_adu):
    """Regression inherited from the PDU: ``frozenset`` is **not** a subclass of
    ``set``.

    The codec listed only ``set``, and ``allowed_relays`` is a ``frozenset``
    (deliberately — so a caller cannot ``.add()`` a relay into scope). Because a
    property snapshot rides along on *every* response, that one omission made
    every remote call on the device fail with "cannot encode frozenset", not
    just the getter — a total outage from a type distinction nothing else in the
    tree happened to exercise.

    JSON has no set, so both arrive as a list. That asymmetry is pre-existing
    codec convention and is not corrected here; what matters is that the values
    survive.
    """
    from benchctrl.net.codec import decode_value, encode_value

    assert sorted(decode_value(encode_value(frozenset({3, 1, 2})))) == [1, 2, 3]
    assert decode_value(encode_value(frozenset())) == []

    # And through the live stack, which is where it actually broke.
    assert sorted(remote_adu.proxy.allowed_relays) == [0, 1]


def test_the_default_allowlist_survives_as_all_eight(remote_adu):
    """The permissive default is the *common* case for this device, so the
    empty-vs-full distinction has to cross the wire intact. An encoder that
    dropped a full set would silently look like "nothing permitted"."""
    from benchctrl.sim.factories import make_adu218

    driver = make_adu218()
    try:
        from benchctrl.net.codec import decode_value, encode_value

        assert sorted(decode_value(encode_value(driver.allowed_relays))) == list(range(8))
    finally:
        driver.close()


# ---------------------------------------------------------------------------
# Behaviour across the wire
# ---------------------------------------------------------------------------


def test_a_relay_switch_round_trips(remote_adu):
    """The verified read-back has to be the *remote* read-back, so a switch
    confirmed over the wire was confirmed against the device."""
    assert remote_adu.proxy.set_relay_state(0, True) is True
    assert remote_adu.model.relay_state(0) is True
    assert remote_adu.proxy.relay_states()[0] is True


def test_no_emitted_command_targets_more_than_one_relay_at_a_time(remote_adu):
    """Except ``MKddd``, which is the whole-port write and is what
    ``set_relay_port`` exists to be. Asserted on the actual command log rather
    than on the driver's intent, since the log is what reached the device."""
    import re

    remote_adu.proxy.set_relay_state(1, True)
    remote_adu.proxy.set_relay_state(1, False)
    for command in remote_adu.model.command_log:
        assert re.fullmatch(r"[A-Z]{2,3}\d{0,3}", command), command
        assert "all" not in command.lower()


def test_the_watchdog_can_be_armed_remotely(remote_adu):
    """It is a *hardware* interlock, so arming it over the wire has an effect
    that outlives the connection — which is exactly why the MCP surface is not
    read-only and says so."""
    assert remote_adu.proxy.set_watchdog(3) == 3
    assert remote_adu.model.watchdog == 3


def test_closing_the_client_does_not_de_energise(remote_adu):
    """A relay left conducting stays conducting when the link drops.

    The hardware watchdog is the mechanism for "the agent stopped talking, drop
    the relays" — it works when no software can run at all. A teardown that also
    dropped contacts would make every disconnect a bench event.
    """
    remote_adu.proxy.set_relay_state(0, True)
    remote_adu.client.close()
    assert remote_adu.model.relay_state(0) is True


# ---------------------------------------------------------------------------
# Exceptions across the wire
# ---------------------------------------------------------------------------


def test_a_policy_refusal_keeps_its_type_remotely(remote_adu):
    """Degraded to ``RuntimeError``, a refused relay is indistinguishable from a
    device fault — and the remedy (widen the allowlist) is a deliberate human
    decision, not a retry.

    ``ADU218PolicyError`` is also deliberately **not** a ``ValueError``: the
    index was perfectly valid for the hardware. A caller catching ``ValueError``
    to mean "I passed something wrong" must not swallow this.
    """
    from benchctrl.drivers.ontrak_adu218 import ADU218PolicyError

    with pytest.raises(ADU218PolicyError) as exc:
        remote_adu.proxy.set_relay_state(5, True)
    assert not isinstance(exc.value, ValueError)
    assert "allowed_relays" in str(exc.value)
    # Nothing reached the device.
    assert "SK5" not in remote_adu.model.command_log


def test_a_timeout_keeps_its_type_remotely(remote_adu):
    """The device's only error signal, and the one that must survive intact.

    Silence is what an absent command, a valid command with a bad argument and a
    write-only command all look like on the wire. Degraded to ``RuntimeError``
    the caller loses the single diagnostic this device offers; and because
    ``ADU218TimeoutError`` also subclasses the builtin ``TimeoutError``, code
    catching that generic type has to keep working remotely too.
    """
    from benchctrl.drivers.ontrak_adu218 import ADU218TimeoutError

    remote_adu.model.handle = lambda command: None  # accepts, answers nothing
    with pytest.raises(ADU218TimeoutError) as exc:
        remote_adu.proxy.relay_states()
    assert isinstance(exc.value, TimeoutError)


def test_a_client_side_value_error_round_trips(remote_adu):
    """Rejected before anything is sent, which also proves the proxy does not
    skip validation — an out-of-range input line must be inexpressible remotely
    too, not just locally."""
    from benchctrl.drivers.ontrak_adu218 import ADU218ValueError

    with pytest.raises(ADU218ValueError):
        remote_adu.proxy.input_state("A", 7)


def test_bool_is_still_rejected_before_int_remotely(remote_adu):
    """``True`` must not become relay 1 over the wire either.

    Worth its own remote test: JSON has no distinct bool-vs-int problem, but a
    codec that normalised ``True`` to ``1`` on the way out would defeat the
    check without touching the driver — and ``set_watchdog(True)`` would arm a
    one-second hardware deadman on a bench nobody expected to hold it.
    """
    from benchctrl.drivers.ontrak_adu218 import ADU218ValueError

    with pytest.raises(ADU218ValueError):
        remote_adu.proxy.relay_state(True)
    with pytest.raises(ADU218ValueError):
        remote_adu.proxy.set_watchdog(True)


def test_a_protocol_error_keeps_its_type_remotely(remote_adu):
    """"The device answered and the answer is not believable" is a different
    action from "the device did not answer" — one is a bug or a desync, the
    other may be routine. Collapsing them loses that."""
    from benchctrl.drivers.ontrak_adu218 import ADU218ProtocolError

    original = remote_adu.model._dispatch
    remote_adu.model._dispatch = (
        lambda command: "0" if command == "PK" else original(command)
    )
    with pytest.raises(ADU218ProtocolError):
        remote_adu.proxy.relay_states()


def test_every_driver_exception_is_reconstructible(remote_adu):
    """All six, by name, through the actual encode/decode path.

    ``net/errors.py`` builds its registry by importing named classes from the
    driver module, so a typo there degrades an exception along its MRO
    *silently* — the caller sees ``ADU218Error`` instead of
    ``ADU218TimeoutError`` and cannot tell why.
    """
    from benchctrl.drivers.ontrak_adu218 import driver as driver_module
    from benchctrl.net.errors import (
        decode_exception,
        encode_exception,
        known_class_names,
    )

    names = known_class_names()
    for name in (
        "ADU218Error",
        "ADU218ConnectionError",
        "ADU218ProtocolError",
        "ADU218TimeoutError",
        "ADU218ValueError",
        "ADU218PolicyError",
    ):
        assert name in names, f"{name} is not in the wire registry"
        cls = getattr(driver_module, name)
        rebuilt = decode_exception(encode_exception(cls("test message")))
        assert type(rebuilt) is cls, f"{name} degraded to {type(rebuilt).__name__}"
        assert "test message" in str(rebuilt)


def test_no_command_error_type_is_registered():
    """The absence is deliberate, and asserted so nobody adds one by analogy.

    Every other driver here has a ``CommandError`` carrying the device's error
    *reply*. This device has no error reply to carry — silence is the whole
    vocabulary — so a ``CommandError`` would have nothing to put in it and would
    imply a diagnostic that does not exist.
    """
    from benchctrl.net.errors import known_class_names

    assert "ADU218CommandError" not in known_class_names()


# ---------------------------------------------------------------------------
# The writer-claim gate, against the live agent surface
# ---------------------------------------------------------------------------


def test_the_writer_claim_gate_holds_over_the_wire(remote_adu):
    """Asserted against the agent's own surface, not just local introspection.

    ``agent/dispatch.py`` derives mutators purely from method-name prefixes with
    no driver-declared override, so a rename is all it takes to make relay
    control callable with no claim. This is the remote half of that check.
    """
    surface = remote_adu.agent.registry.surface_of(KEY)
    # Pinned in both directions with an exact set: a missing entry means a relay
    # can be switched with no claim, an extra one means a contact-moving method
    # arrived unreviewed.
    assert surface.mutators == frozenset(
        {
            "set_relay_state",
            "set_relay_port",
            "reset_relays",
            "clear_counter",
            "set_debounce",
            "set_watchdog",
        }
    )


def test_reads_need_no_writer_claim(remote_adu):
    """Observation must work for a client holding no claim, or a monitoring
    dashboard could not watch a bench it does not control."""
    surface = remote_adu.agent.registry.surface_of(KEY)
    for name in ("read_identity", "relay_states", "input_states", "read_counters"):
        assert name in surface.methods
        assert name not in surface.mutators


def test_the_link_sense_of_open_is_what_the_snapshot_publishes(remote_adu):
    """``is_open`` means *link connected* framework-wide, and it is published as
    ``"open"`` — the opposite sign to a relay's "open".

    Both senses live in this device. The snapshot reporting ``open: True`` while
    a relay is de-energised is correct and confusing, so it is pinned.
    """
    assert remote_adu.proxy.is_open is True
    assert remote_adu.proxy.relay_state(0) is False

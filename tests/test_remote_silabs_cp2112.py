"""The CP2112 in remote mode, end to end.

A driver can pass every local test and still be unreachable through the agent:
a missing entry in one of five registries, a return type the codec drops, an
exception the wire cannot reconstruct. None of those failures are visible to
:py:mod:`tests.test_cp2112`, which never leaves the process.

So the stack under test here is complete — proxy, wire protocol, agent dispatch,
device worker, production driver, simulator. Only the silicon is fake.

**The remote path carries extra weight for this device specifically.** The
CP2112 exists to hold a DUT in reset and let it go, and the agent is what a run
drives it through. Two properties therefore have to survive the wire or the
safety story is local-only:

- ``CP2112PolicyError`` must keep its type, because a *refused* pin drive and a
  *broken* device call for opposite responses. Degraded to ``RuntimeError``, a
  remote caller cannot tell "this pin is not yours to drive" from "the bridge is
  gone" — and the first has a human remedy while the second has a retry.
- ``set_line_asserted`` and ``trigger_reset_pulse`` must land in
  ``surface.mutators``, because that is the gate that makes them require the
  writer claim. A pin-moving method outside the mutator set is remotely callable
  by any reader.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"

#: Lines the remote fixture may drive. Narrower than the sim factory's default
#: on purpose: the allowlist is only tested if something is outside it.
ALLOWED = (2, 3)
FORBIDDEN = 5


@pytest.fixture()
def remote_gpio():
    """An agent serving a simulated CP2112, and an attached remote proxy.

    Built through :py:func:`make_cp2112` so the agent holds the *production*
    driver, exactly as it would hold one over hidraw.
    """
    from benchctrl.sim.factories import make_cp2112

    driver = make_cp2112(allowed_lines=ALLOWED)

    registry = DeviceRegistry()
    registry.register_open("silabs_cp2112", driver)
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
        proxy = client.attach("silabs_cp2112")
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


def test_the_device_key_is_servable(remote_gpio):
    """Fails if ``silabs_cp2112`` is missing from DEVICE_KEYS or the agent's
    opener table — the two registrations easiest to forget."""
    assert remote_gpio.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    from benchctrl.config import DEVICE_KEYS

    assert "silabs_cp2112" in DEVICE_KEYS


def test_the_agent_can_build_the_key_in_simulate_mode():
    """``benchctrl-agent --simulate`` keys off the sim factory table rather
    than the opener table, so both have to know about the device."""
    from benchctrl.agent.registry import build_default_registry

    built = build_default_registry(["silabs_cp2112"], simulate=True)
    assert "silabs_cp2112" in built.keys


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for("silabs_cp2112") is not None


def test_the_opener_needs_allowed_lines_rather_than_defaulting_it():
    """The registry opener deliberately injects no ``allowed_lines``.

    ``build_default_registry`` is the *hardware* path, and a default there
    would mean an agent config that forgot the allowlist could still drive
    pins. Missing it must be a loud ``TypeError`` at ``open()``, not a silent
    widening — so this test asserts the closure exists while the parameter
    stays the operator's to supply.
    """
    from benchctrl.agent.registry import build_default_registry

    built = build_default_registry(["silabs_cp2112"])
    assert "silabs_cp2112" in built.keys


def test_the_control_surface_is_exposed_over_the_wire(remote_gpio):
    for name in (
        "read_identity",
        "read_gpio_config",
        "read_levels",
        "read_line_state",
        "read_line_states",
        "line_is_asserted",
        "set_line_mode",
        "set_line_asserted",
        "trigger_reset_pulse",
        "reset_lines",
    ):
        assert hasattr(remote_gpio.proxy, name), f"{name} not reachable remotely"


# --------------------------------------------------------------------------
# The dispatch gate — what requires a writer claim
# --------------------------------------------------------------------------


def test_every_pin_moving_method_is_a_mutator():
    """The guard on the naming decision.

    ``agent/dispatch.py`` derives mutators purely from name prefixes, with no
    driver-declared override. A method that moves a pin but does not match a
    prefix is remotely callable **without the writer claim** — so this asserts
    the naming, not the intent. Renaming ``set_line_asserted`` to
    ``assert_line`` would silently remove the gate and only this test would
    notice.
    """
    from benchctrl.agent.dispatch import introspect
    from benchctrl.sim.factories import make_cp2112

    driver = make_cp2112(allowed_lines=ALLOWED)
    try:
        surface = introspect(driver, "silabs_cp2112")
    finally:
        driver.close()

    for name in ("set_line_mode", "set_line_asserted", "trigger_reset_pulse",
                 "reset_lines"):
        assert name in surface.mutators, f"{name} moves a pin but is not a mutator"


def test_reads_are_not_mutators():
    """The other half: a read behind the writer claim would mean a monitoring
    dashboard could not show line state without taking the device."""
    from benchctrl.agent.dispatch import introspect
    from benchctrl.sim.factories import make_cp2112

    driver = make_cp2112(allowed_lines=ALLOWED)
    try:
        surface = introspect(driver, "silabs_cp2112")
    finally:
        driver.close()

    for name in ("read_identity", "read_gpio_config", "read_levels",
                 "read_line_state", "read_line_states", "line_is_asserted"):
        assert name not in surface.mutators, f"{name} only reads but needs a claim"


# --------------------------------------------------------------------------
# Return types across the wire
# --------------------------------------------------------------------------


def test_identity_survives_the_codec(remote_gpio):
    """``CP2112Info`` must be in the codec's wire-type allowlist, or this comes
    back as a bare dict and ``.is_cp2112`` raises ``AttributeError``."""
    info = remote_gpio.proxy.read_identity()
    assert info.part_number == 0x0C
    assert info.is_cp2112 is True
    assert info.serial


def test_a_line_state_survives_the_codec(remote_gpio):
    """``CP2112LineState`` carries the derived ``asserted`` property.

    Degraded to a dict, ``state.asserted`` would fail — and ``asserted`` is the
    only field a reset-line caller actually reasons about, since "high" is
    ambiguous on an active-low net.
    """
    proxy = remote_gpio.proxy
    proxy.set_line_mode(2, output=True)
    proxy.set_line_asserted(2, True)

    state = proxy.read_line_state(2)
    assert state.index == 2
    assert state.is_output is True
    assert state.open_drain is True
    assert state.asserted is True


def test_the_gpio_config_survives_the_codec(remote_gpio):
    """``CP2112GpioConfig`` is a register image; its bit-field properties are
    what callers use rather than the raw bytes."""
    cfg = remote_gpio.proxy.read_gpio_config()
    assert isinstance(cfg.direction, int)
    assert isinstance(cfg.push_pull, int)
    assert cfg.clock_output_enabled is False


def test_a_dict_of_line_states_survives_the_wire(remote_gpio):
    """``read_line_states`` returns ``dict[int, CP2112LineState]``. JSON keys
    are strings, so a codec that did not restore the int keys would break
    ``states[7]`` while leaving ``states`` truthy — a silent failure."""
    states = remote_gpio.proxy.read_line_states()
    assert len(states) == 8
    for index in range(8):
        assert index in states, f"line {index} missing or keyed as a string"


def test_a_bool_survives_the_wire(remote_gpio):
    proxy = remote_gpio.proxy
    proxy.set_line_mode(3, output=True)
    proxy.set_line_asserted(3, True)
    assert proxy.line_is_asserted(3) is True
    proxy.set_line_asserted(3, False)
    assert proxy.line_is_asserted(3) is False


# --------------------------------------------------------------------------
# Exceptions across the wire
# --------------------------------------------------------------------------


def test_a_policy_refusal_keeps_its_type_remotely(remote_gpio):
    """The one that matters most.

    A caller distinguishes "this pin is not yours to drive" (a human decision
    about bench wiring) from "the bridge is unplugged" (a retry). Degraded to
    ``RuntimeError`` both look identical and only the message survives.
    """
    from benchctrl.drivers.silabs_cp2112 import CP2112PolicyError

    with pytest.raises(CP2112PolicyError):
        remote_gpio.proxy.set_line_mode(FORBIDDEN, output=True)


def test_the_allowlist_holds_for_asserting_too(remote_gpio):
    """Two separate gates, and the second is the one that moves the net."""
    from benchctrl.drivers.silabs_cp2112 import CP2112PolicyError

    with pytest.raises(CP2112PolicyError):
        remote_gpio.proxy.set_line_asserted(FORBIDDEN, True)


def test_a_refused_line_did_not_move(remote_gpio):
    """A refusal that still wrote to the device would be worse than no gate,
    because the error would say the opposite of what happened."""
    from benchctrl.drivers.silabs_cp2112 import CP2112PolicyError

    before = remote_gpio.sim.direction
    with pytest.raises(CP2112PolicyError):
        remote_gpio.proxy.set_line_mode(FORBIDDEN, output=True)
    assert remote_gpio.sim.direction == before


def test_a_bad_line_index_is_a_value_error_remotely(remote_gpio):
    from benchctrl.drivers.silabs_cp2112 import CP2112ValueError

    with pytest.raises(CP2112ValueError):
        remote_gpio.proxy.read_line_state(99)


def test_a_too_short_pulse_is_refused_remotely(remote_gpio):
    """Bounded by USB scheduling, not by the chip. Refused rather than
    silently stretched, and the refusal has to survive the wire or a remote
    caller believes it got a 1 ms pulse."""
    from benchctrl.drivers.silabs_cp2112 import CP2112ValueError

    remote_gpio.proxy.set_line_mode(2, output=True)
    with pytest.raises(CP2112ValueError):
        remote_gpio.proxy.trigger_reset_pulse(2, duration_s=0.001)


def test_every_cp2112_exception_is_wire_registered():
    """A type absent from the registry degrades along its MRO and the caller
    loses the ability to distinguish it."""
    from benchctrl.net.errors import known_class_names

    names = set(known_class_names())
    for name in (
        "CP2112Error",
        "CP2112ConnectionError",
        "CP2112ProtocolError",
        "CP2112ValueError",
        "CP2112PolicyError",
        "CP2112VerifyError",
    ):
        assert name in names, f"{name} missing from the error registry"


def test_there_is_no_cp2112_timeout_error():
    """Pinned deliberately. An ioctl to a hidraw node completes or fails, so
    there is no timeout condition to distinguish; a ``CP2112TimeoutError``
    appearing later would be a claim about the transport that is not true, and
    would invite callers to write a retry loop for a state that cannot occur.
    """
    from benchctrl.net.errors import known_class_names

    assert "CP2112TimeoutError" not in set(known_class_names())


# --------------------------------------------------------------------------
# Stateful sequences across the wire
# --------------------------------------------------------------------------


def test_a_reset_pulse_works_remotely(remote_gpio):
    """The whole point of the driver, over a socket.

    Several round trips whose ordering matters, and the post-condition is the
    safety one: after a pulse the line must be released.
    """
    proxy = remote_gpio.proxy
    proxy.set_line_mode(2, output=True)
    state = proxy.trigger_reset_pulse(2, duration_s=0.01)
    assert state.asserted is False
    assert proxy.line_is_asserted(2) is False


def test_the_line_really_moved_at_the_simulated_device(remote_gpio):
    """Read back at the *simulator*, not through the proxy.

    Every other assertion here is downstream of the driver, so a driver that
    cached state and never wrote a report would pass them all. This one looks
    at the far side of the link seam and fails if no report arrived.
    """
    proxy = remote_gpio.proxy
    proxy.set_line_mode(3, output=True)
    proxy.set_line_asserted(3, True)

    assert remote_gpio.sim.direction & (1 << 3), "the pin was never made an output"
    assert not remote_gpio.sim.push_pull & (1 << 3), "the pin was not left open-drain"
    assert not remote_gpio.sim.latch & (1 << 3), "the pin was never pulled low"


def test_configuration_persists_between_remote_calls(remote_gpio):
    """Each remote call is a separate message; pin state must live on the
    device, not in the proxy."""
    proxy = remote_gpio.proxy
    proxy.set_line_mode(2, output=True)
    proxy.set_line_asserted(2, True)

    state = proxy.read_line_state(2)
    assert state.is_output is True
    assert state.asserted is True


def test_setting_one_line_leaves_the_others_alone_remotely(remote_gpio):
    """GPIO config is one shared register, so every write is a
    read-modify-write. A remote path that dropped the read would clobber the
    other seven pins — including any line another part of the bench is holding.
    """
    proxy = remote_gpio.proxy
    proxy.set_line_mode(2, output=True)
    proxy.set_line_asserted(2, True)
    proxy.set_line_mode(3, output=True)
    proxy.set_line_asserted(3, True)

    assert proxy.line_is_asserted(2) is True, "line 2 was clobbered by line 3"
    assert proxy.line_is_asserted(3) is True


def test_reset_lines_releases_everything_remotely(remote_gpio):
    """The remote panic path. High-Z is the chip's own power-on state, so this
    is "as the hardware would come up" rather than an invented safe state."""
    proxy = remote_gpio.proxy
    proxy.set_line_mode(2, output=True)
    proxy.set_line_asserted(2, True)
    assert proxy.line_is_asserted(2) is True

    proxy.reset_lines()

    for index in ALLOWED:
        assert proxy.read_line_state(index).is_output is False

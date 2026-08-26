"""Hardware tests for the CP2112, against a real chip on a real bench.

Run with ``pytest -m hardware -k cp2112``. Skipped entirely when no CP2112 is
attached, so the file is harmless in the default suite.

Two of these tests need a DMM witnessing the pin, and they are gated on
``BENCHCTRL_CP2112_LINE`` naming which GPIO the meter is clamped to. That gate
is the operator stating what is wired where — the driver cannot know, and per
``AGENTS.md`` an output must never be energised without knowing what is
attached. The env var is deliberately not defaulted: a default would mean the
suite picks a pin to drive, which is exactly the decision it must not make.

Why a DMM is the right witness, and the chip is not: reading a level back
through the same chip that drove it proves the *latch* changed, not that any
voltage moved. An undriven pin latches 1 while a high-impedance meter reads
~0 V on the same net — that is precisely the confusion that made commissioning
this device slow — so the chip's own read-back cannot distinguish "the pin is
driving" from "the register says so". The meter is the only instrument here
that is not downstream of the thing under test.
"""

from __future__ import annotations

import os
import statistics

import pytest

from benchctrl.drivers.silabs_cp2112 import (
    CP2112,
    CP2112PolicyError,
    find_hidraw_nodes,
)

pytestmark = pytest.mark.hardware


#: The GPIO the DMM is clamped to, e.g. "7". Operator-supplied; no default.
LINE_ENV = "BENCHCTRL_CP2112_LINE"

#: A logic level has to be one side or the other of this to count as decided.
#: Wide on purpose: the point of these tests is a clean full-swing transition,
#: not a threshold measurement, and a tight window would fail on lead resistance
#: rather than on anything about the chip.
LOW_MAX_V = 0.8
HIGH_MIN_V = 2.0


def _nodes() -> list[str]:
    try:
        return find_hidraw_nodes()
    except OSError:
        return []


requires_device = pytest.mark.skipif(
    not _nodes(), reason="no CP2112 hidraw node present"
)


def _witnessed_line() -> int:
    raw = os.environ.get(LINE_ENV)
    if raw is None:
        pytest.skip(
            f"{LINE_ENV} is not set. It names the GPIO the DMM is clamped to, and "
            f"it has no default on purpose: defaulting it would mean this suite "
            f"chooses which pin to drive on a bench whose wiring it cannot see."
        )
    try:
        return int(raw)
    except ValueError:
        pytest.fail(f"{LINE_ENV}={raw!r} is not an integer GPIO index")


@pytest.fixture
def dmm():
    """The bench DMM, through the agent, in DC volts.

    Goes through the agent rather than opening the instrument directly because
    the agent holds the writer claim (``KNOWN_LIMITATIONS.md`` §N-4) and a second
    open would fail with "interface is busy".
    """
    import json

    from benchctrl.config import EndpointConfig
    from benchctrl.net.client import RemoteClient

    try:
        cfg = json.load(open("/etc/benchctrl/agent.json"))
    except OSError:
        pytest.skip("no local agent config; this test runs on the bench board")
    ep = EndpointConfig(
        host="127.0.0.1",
        port=cfg["port"],
        token=cfg["token"],
        heartbeat_s=1.0,
        deadman_s=15.0,
    )
    client = RemoteClient(ep).connect()
    try:
        yield client.attach("siglent_sdm4065a")
    finally:
        client.close()


def _volts(dmm, n: int = 3) -> float:
    return statistics.fmean([dmm.measure_dc_voltage() for _ in range(n)])


@requires_device
def test_the_device_identifies_as_a_cp2112():
    """Part 0x0C per datasheet §12. Proves the feature-report path end to end.

    This is the cheapest possible real-hardware assertion: it exercises the
    _IOC encoding, the ioctl, and the report-id echo check, and it drives
    nothing. If this passes and a later test fails, the failure is about pins
    rather than about the transport.
    """
    with CP2112.open(allowed_lines=()) as dev:
        info = dev.read_identity()
        assert info.is_cp2112, f"part number 0x{info.part_number:02X}"
        assert info.serial


@requires_device
def test_opening_changes_no_pin():
    """open() is observational. Nothing is configured and nothing is driven."""
    with CP2112.open(allowed_lines=()) as dev:
        before = dev.read_gpio_config()
    with CP2112.open(allowed_lines=()) as dev:
        after = dev.read_gpio_config()
    assert before == after, "opening the device perturbed its GPIO configuration"


@requires_device
def test_an_unallowlisted_line_cannot_be_driven_on_real_hardware():
    """The allowlist holds against a real chip, not just the simulator.

    Worth running on hardware because the simulator cannot show that the refusal
    happens *before* any report reaches the device.
    """
    with CP2112.open(allowed_lines=()) as dev:
        before = dev.read_gpio_config()
        with pytest.raises(CP2112PolicyError):
            dev.set_line_mode(0, output=True)
        assert dev.read_gpio_config() == before


@requires_device
def test_the_dmm_follows_the_line_low_and_high(dmm):
    """The core hardware claim: asserting the line pulls the measured net low.

    Verified by voltage, not by the chip's own read-back. Asserting drives the
    net to ~0 V; releasing lets the CP2112's internal pull-up take it to VIO,
    which a 10 MΩ meter cannot fight.
    """
    line = _witnessed_line()
    with CP2112.open(allowed_lines=(line,)) as dev:
        dev.set_line_mode(line, output=True, allow_alternate_function=True)

        dev.set_line_asserted(line, True)
        low = _volts(dmm)
        dev.set_line_asserted(line, False)
        high = _volts(dmm)

        assert low < LOW_MAX_V, f"asserted line measured {low:.4f} V"
        assert high > HIGH_MIN_V, f"released line measured {high:.4f} V"
        assert high - low > 1.5, f"swing was only {high - low:.4f} V"


@requires_device
def test_a_reset_pulse_leaves_the_line_released(dmm):
    """After a pulse the net must be high — a pulse that latches is a held DUT."""
    line = _witnessed_line()
    with CP2112.open(allowed_lines=(line,)) as dev:
        dev.set_line_mode(line, output=True, allow_alternate_function=True)
        dev.trigger_reset_pulse(line, duration_s=0.1, settle_s=0.05)
        assert _volts(dmm) > HIGH_MIN_V, "the line was left asserted after a pulse"


@requires_device
def test_close_releases_a_held_line_on_real_hardware(dmm):
    """The property that keeps a DUT out of reset when a test dies.

    Asserts the line, closes *without* releasing it explicitly, and confirms with
    the meter that the net came back up. This is the one behaviour a simulator
    cannot make anybody trust, because the risk it guards against is physical.
    """
    line = _witnessed_line()
    dev = CP2112.open(allowed_lines=(line,))
    dev.set_line_mode(line, output=True, allow_alternate_function=True)
    dev.set_line_asserted(line, True)
    assert _volts(dmm) < LOW_MAX_V, "line did not go low, so the test proves nothing"
    dev.close()
    assert _volts(dmm) > HIGH_MIN_V, "close() left the line asserted"

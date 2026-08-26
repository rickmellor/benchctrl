"""The ADU218 driver against the real device, with the DMM as the witness.

Everything in :py:mod:`tests.test_bench_adu218` runs against the simulator, and
the simulator subclasses the production link so the framing, the whitelist and
the width table are all real code under test. What it cannot prove is anything
about the *device*: the simulator models the hardware's behaviour because the
hardware was measured, so asserting that behaviour there proves only that the
model is self-consistent.

Four claims here are properties of the device and of the USB stack, and each is
load-bearing for a design decision that looks arbitrary without it:

1. **The relays actually move, and the driver's read-back agrees with an
   independent instrument.** ``SKn``/``RKn`` are unacknowledged, so the driver's
   own confirmation is a second read of the same device — self-consistent by
   construction. The SDM4065A on relay K0 is the only witness that is not the
   ADU218 talking about itself. This is the test that would catch a driver
   reporting a switch that never happened.
2. **``USBDEVFS_BULK`` works on an interrupt endpoint.** The whole
   zero-dependency approach rests on it. The kernel source says it is handled;
   this is the run that shows it, on this kernel, on this board.
3. **``usbhid`` does not claim the device**, so ``CLAIMINTERFACE`` succeeds with
   nothing to detach and no udev rule. If a future kernel drops Ontrak from
   ``hid_ignore_list``, this is where it surfaces — as a clear claim failure
   rather than as a mysterious ``EBUSY`` in the field.
4. **Discovery identifies it passively**, from sysfs, with no probe write.

**Not** in here, deliberately: the watchdog trip. Arming it de-energises every
relay after a measured silence, which is correct behaviour and a bad thing to do
unattended in a test run that might be sharing the bench. Its ladder is tested
against a synthetic clock in ``tests/test_bench_adu218.py`` and its trip time was
bisected by hand into ``tests/fixtures/adu218/watchdog.txt``.

Running it
----------

The device is found by USB descriptor; there is no address to supply. Every
test skips if it is not attached.

    BENCHCTRL_ADU218_SERIAL   pick one of several   (default: the only one)
    BENCHCTRL_ADU218_RELAY    the relay to switch   (default 0 — the K0 wired
                              to the SDM4065A on this bench)
    BENCHCTRL_ADU218_DMM      DMM VISA resource     (default: autodetect)

**What is attached matters.** These tests switch exactly one relay, named by
``BENCHCTRL_ADU218_RELAY``, and that relay is also the entire ``allowed_relays``
list, so a bug in this file cannot reach another one. They are 1 A signal SSRs
rather than mains contactors — but per ``AGENTS.md``, never energise an output
without knowing what is attached, and on this bench K0 is a DMM sense loop with
nothing else across it. Point it elsewhere only after checking the wiring.

Why the witness reads 2-wire, and why no threshold is asserted
--------------------------------------------------------------
4-wire needs separate source and sense leads; on this bench the DMM has one
pair across the relay, so ``measure_resistance_4wire()`` returns drifting
negative values — measured, and the reason this file uses the 2-wire function.

The closed-contact figure is **not** compared against a threshold. The same
closed relay measured 6.14 Ω, 10.69 Ω, 10.65 Ω and 9.40 Ω across four sessions,
milliohm-stable *within* each, with the step traced to re-seated screw-clamped
probes rather than to the relay. Any threshold set from one of those numbers
misreads the others. What is asserted is the shape that no probe seating can
change: closed reads *a number*, open reads the DMM's **overload sentinel**.
That sentinel is what makes this a real witness — an open contact is not a large
resistance, it is an unmeasurable one, so the two states cannot be confused by
any amount of contact drift.
"""

from __future__ import annotations

import os
import time

import pytest

from benchctrl.drivers.ontrak_adu218 import (
    ADU218Error,
    ADU218PolicyError,
    OntrakADU218,
)

pytestmark = pytest.mark.hardware


HW_SERIAL = os.environ.get("BENCHCTRL_ADU218_SERIAL") or None

#: The one relay these tests may switch. Deliberately a single int, and
#: deliberately also the whole allowlist passed to ``open()``, so a bug here
#: cannot reach a relay the operator did not nominate.
TEST_RELAY = int(os.environ.get("BENCHCTRL_ADU218_RELAY", "0"))

#: Settling time before the witness reads. The measured relay round trip is
#: ~17 ms and the DMM's own reading takes longer than that, so this is generous
#: rather than load-bearing — but a 0 s wait would make the DMM the thing under
#: test rather than the relay.
SETTLE_S = 0.5


def _open_adu() -> OntrakADU218:
    """Open the real device, or skip with the reason.

    Skips rather than fails on every connection-shaped outcome. "Not plugged in"
    is a fact about the bench, and a red test for it trains people to ignore
    this file.
    """
    try:
        return OntrakADU218.open(
            serial=HW_SERIAL,
            allowed_relays=(TEST_RELAY,),
        )
    except ADU218Error as exc:
        pytest.skip(f"ADU218 not reachable (serial={HW_SERIAL!r}): {exc}")


@pytest.fixture()
def adu():
    """A live session with the watchdog disarmed, one test at a time.

    ``close()`` deliberately does not de-energise, so the teardown does it
    explicitly. That ordering is the point: a test that dies hard leaves the
    relay where it was, and the next test's ``reset_relays()`` recovers it.
    """
    dev = _open_adu()
    try:
        yield dev
    finally:
        try:
            dev.reset_relays()
        finally:
            dev.close()


@pytest.fixture()
def witness():
    """The SDM4065A reading resistance across the relay, or a skip.

    Returns a callable that answers ``float`` for a closed contact and ``None``
    for an open one — the overload sentinel is the signal, not an error, so it
    is caught here rather than propagating.
    """
    try:
        from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A
        from benchctrl.drivers.siglent_sdm4065a.driver import (
            SDM4065AError,
            SDM4065AOverloadError,
        )
    except ImportError as exc:  # pragma: no cover - pyvisa missing
        pytest.skip(f"SDM4065A driver unavailable: {exc}")

    resource = os.environ.get("BENCHCTRL_ADU218_DMM") or None
    try:
        dmm = SiglentSDM4065A.open(resource=resource) if resource else SiglentSDM4065A.open()
    except SDM4065AError as exc:
        pytest.skip(
            f"the DMM witness is not reachable ({exc}); the relay tests here "
            f"are meaningless without an instrument that is not the ADU218"
        )

    def read():
        time.sleep(SETTLE_S)
        try:
            # 2-wire: see the module docstring on why not 4-wire here.
            return dmm.measure_resistance()
        except SDM4065AOverloadError:
            # An open contact is *unmeasurable*, not merely large. This is the
            # sentinel, and it is the half of the witness that cannot be faked
            # by probe drift.
            return None

    try:
        yield read
    finally:
        dmm.close()


# ---------------------------------------------------------------------------
# Claim 1: the relays move, and an instrument that is not the ADU218 agrees
# ---------------------------------------------------------------------------


def test_the_driver_and_an_independent_instrument_agree_on_every_transition(
    adu, witness
):
    """The one test that cannot be satisfied by the driver talking to itself.

    Five transitions, alternating, each checked from both sides. Alternating
    matters: a driver that reported the *commanded* value rather than the read
    value would pass a single on-then-off pair, and a witness stuck on one
    reading would too. Only a device whose state actually follows the commands
    produces this sequence.
    """
    adu.reset_relays()
    assert witness() is None, (
        "the relay reads closed before anything was commanded — either the "
        "DMM is not across the relay or reset_relays() did not reach the "
        "safe state"
    )

    closed_readings = []
    for expected in (True, False, True, False):
        got = adu.set_relay_state(TEST_RELAY, expected)
        assert got is expected
        measured = witness()
        if expected:
            assert measured is not None, (
                f"driver reports relay {TEST_RELAY} energised but the DMM "
                f"still over-ranges; the switch was claimed and did not happen"
            )
            closed_readings.append(measured)
        else:
            assert measured is None, (
                f"driver reports relay {TEST_RELAY} de-energised but the DMM "
                f"measures {measured} Ω; the contact is still conducting"
            )

    # Non-vacuity: the loop must actually have measured closed contacts, or
    # every assertion above was the None branch and this test proved nothing.
    assert len(closed_readings) == 2

    # No threshold — see the module docstring. What is checked is that the two
    # closed readings are the same relay and not two different circuits: the
    # within-session spread is milliohms, so a factor-of-two step would mean
    # something other than contact seating changed.
    lo, hi = min(closed_readings), max(closed_readings)
    assert hi < lo * 2, f"closed-contact readings disagree: {closed_readings}"


def test_reset_relays_reaches_a_genuinely_open_contact(adu, witness):
    """The safe state, confirmed from outside the driver.

    ``reset_relays()`` is the call an operator makes to *believe* the bench is
    de-energised, and its verification is the device's own read-back. This is
    the same claim checked against an instrument that has no stake in it.
    """
    adu.set_relay_state(TEST_RELAY, True)
    assert witness() is not None  # the control: the contact really was closed
    assert adu.reset_relays() == 0
    assert witness() is None


def test_the_allowlist_holds_on_hardware_and_still_permits_de_energising(adu):
    """The asymmetry, against the real device rather than the simulator.

    ``allowed_relays`` is ``(TEST_RELAY,)`` here, so every other relay is
    unlisted. Energising one must be refused *before* anything is sent, and
    de-energising it must still work — the allowlist guards closing contacts, so
    a narrow list must never make the safe state unreachable.
    """
    other = 1 if TEST_RELAY != 1 else 2
    with pytest.raises(ADU218PolicyError):
        adu.set_relay_state(other, True)
    # Not a ValueError: the index was valid for the hardware and the refusal is
    # policy. A caller catching ValueError must not swallow it.
    assert adu.set_relay_state(other, False) is False


# ---------------------------------------------------------------------------
# Claim 2 and 3: the USB stack itself
# ---------------------------------------------------------------------------


def test_interrupt_endpoints_answer_a_bulk_ioctl_on_this_kernel(adu):
    """The premise of the whole zero-dependency design, exercised.

    ``USBDEVFS_BULK`` on an interrupt endpoint is handled by ``devio.c``, which
    rewrites the pipe to ``PIPE_INTERRUPT`` and calls ``usb_fill_int_urb()``
    with the endpoint's own ``bInterval``. Both of this device's endpoints are
    interrupt (measured: EP 0x81 IN and EP 0x01 OUT, ``bInterval`` 10), so every
    read below is that path working. If it ever stops, this driver needs libusb
    and the dependency story changes — so the failure should name the reason.
    """
    from benchctrl.drivers.ontrak_adu218.usbfs import EP_IN, EP_OUT

    # A responsive command: the write goes out EP_OUT, the reply comes in EP_IN.
    # Both are interrupt endpoints, so a reply arriving at all is the evidence.
    assert isinstance(adu.relay_mask(), int)
    assert (EP_OUT, EP_IN) == (0x01, 0x81)

    device = adu.read_identity()
    assert device.vendor_id == 0x0A07
    assert device.product_id == 0x00DA


def test_claiming_the_interface_needs_no_driver_detached(adu):
    """``usbhid`` ignores Ontrak deliberately (``hid_ignore_list``), so there is
    nothing bound to detach and no udev unbind rule to ship.

    The open in the fixture already proves it — ``CLAIMINTERFACE`` would have
    failed with ``EBUSY`` otherwise. What this adds is the *reason*, so that if
    a future kernel drops Ontrak from that list, the failure arrives here with
    an explanation rather than in the field as a mysterious busy device.
    """
    assert adu.is_open  # link connected; see the driver docstring on the word
    # And a second open must fail while the first holds the interface, which is
    # the same mechanism seen from the other side.
    second = None
    try:
        second = OntrakADU218.open(
            serial=HW_SERIAL, allowed_relays=(TEST_RELAY,)
        )
    except ADU218Error:
        pass  # expected: the interface is claimed
    else:
        pytest.fail(
            "a second session claimed the same interface; the exclusive claim "
            "is what stops two writers driving the same contacts"
        )
    finally:
        if second is not None:
            second.close()


# ---------------------------------------------------------------------------
# Claim 4: discovery, by looking rather than by writing
# ---------------------------------------------------------------------------


def test_discovery_identifies_the_device_without_writing_to_it(adu):
    """Identified from sysfs descriptors, never by a probe.

    ``AGENTS.md`` says probe by writing rather than by querying — but the
    correct number of writes to a relay board you have not identified yet is
    zero, and a descriptor match needs none. So this device is a ``SIGNATURES``
    entry, and ``discover(probe=False)`` must still name it exactly.
    """
    from benchctrl import discovery

    found = discovery.discover(probe=False)
    matches = [d for d in found if d.device_key == "ontrak_adu218"]
    assert matches, (
        f"the attached ADU218 was not identified passively; found: "
        f"{[str(d) for d in found]}"
    )
    # Exact, not a guess: the signature is a VID/PID pair, so anything weaker
    # would mean the match came from somewhere else.
    assert [d.confidence for d in matches] == [discovery.EXACT] * len(matches)
    # And the serial, which is what tells two attached ADU218s apart. A
    # signature match alone would be satisfied by any Ontrak device.
    assert all(d.serial_number for d in matches), (
        f"identified by VID/PID but with no serial; open(serial=...) could not "
        f"then pick between two boards: {[str(d) for d in matches]}"
    )

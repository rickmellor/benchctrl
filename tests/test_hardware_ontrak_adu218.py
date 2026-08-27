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
   construction. The SDM4065A across the nominated relay is the only witness
   that is not the ADU218 talking about itself. This is the test that would
   catch a driver reporting a switch that never happened.
2. **``USBDEVFS_BULK`` works on an interrupt endpoint.** The whole
   zero-dependency approach rests on it. The kernel source says it is handled;
   this is the run that shows it, on this kernel, on this board.
3. **``usbhid`` does not claim the device**, so ``CLAIMINTERFACE`` succeeds with
   nothing to detach and no udev rule. If a future kernel drops Ontrak from
   ``hid_ignore_list``, this is where it surfaces — as a clear claim failure
   rather than as a mysterious ``EBUSY`` in the field.
4. **Discovery identifies it passively**, from sysfs, with no probe write.
5. **A driven input line reads high, and only its own counter counts** — and the
   counter counts *cycles, not edges*. Both need an external signal source, so
   they skip without one. The counter-to-input map is documented **only in a
   Table 1 image** that the manual's text layer omits entirely, so it is either
   measured here or it is unverified. **All eight positions are now measured**
   (2026-08-27, the generator walked across every line): PA0-PA3 → counters
   0-3 and PB0-PB3 → counters 4-7, each the only counter of eight to move, and
   each setting its own ``PI`` bit and no other. PORT B is what pins the
   offset — on PORT A the counter index equals the line number, so PA3 → 3 fits
   offsets of 4, 3 and 0 equally.

6. **The watchdog actually trips, and the DMM sees the contact open** — the
   claim the interlock design rests on, since everything else about the watchdog
   is checked against a synthetic clock and can only show the driver's model is
   self-consistent. **Opt-in**: it de-energises a relay by timeout.

Arming the watchdog is off by default (``BENCHCTRL_ADU218_ARM_WATCHDOG``) because
it de-energises every relay after a measured silence — correct behaviour, and a
bad surprise on a bench someone else is using. Its ladder is tested against a
synthetic clock in ``tests/test_bench_adu218.py`` and its trip time was bisected
by hand into ``tests/fixtures/adu218/watchdog_trip.txt``, where the DMM
corroborated the tripped state; the test here codifies that measurement so a
regression would be caught rather than needing to be rediscovered by hand.

Running it
----------

The device is found by USB descriptor; there is no address to supply. Every
test skips if it is not attached.

    BENCHCTRL_ADU218_SERIAL     pick one of several  (default: the only one)
    BENCHCTRL_ADU218_RELAY      the relay to switch  (default 7 — the K7 wired
                                to the SDM4065A on this bench as of 2026-08-27)
    BENCHCTRL_ADU218_DMM        DMM VISA resource    (default: autodetect)
    BENCHCTRL_ADU218_INPUT      the driven input line, e.g. ``A3`` (default
                                ``B2`` — where the SDG1032X sits on this bench
                                as of 2026-08-27; it was ``A3`` before that)
    BENCHCTRL_ADU218_SWEEP_ALL  ``1`` to allow the all-eight-relay sweep
                                (default: skip — see below)
    BENCHCTRL_ADU218_ARM_WATCHDOG  ``1`` to let the watchdog actually trip
                                (default: skip — it de-energises by timeout)

**What is attached matters.** By default these tests switch exactly one relay,
named by ``BENCHCTRL_ADU218_RELAY``, and that relay is also the entire
``allowed_relays`` list, so a bug in this file cannot reach another one. They are
1 A signal SSRs rather than mains contactors — but per ``AGENTS.md``, never
energise an output without knowing what is attached, and on this bench that
relay is a DMM sense loop with nothing else across it. Point it elsewhere only
after checking the wiring.

**Never assert a resistance threshold**, and all eight relays are why. Walking
the meter across every one of them in a single session gave **16.87 / 17.50 /
20.77 / 28.37 / 31.36 / 32.09 / 41.19 / 45.65 Ω** closed — a **2.7× spread with
no relation to index**, on identical PhotoMOS parts, because the excess is lead
and contact resistance outside the relay rather than the relay itself. (Earlier
readings on the same two relays: K0 **9.483 Ω**, K7 **36.02–36.22 Ω** — so the
same relay also moves by 4-5× between wirings.) Any ``< 10 Ω`` rule derived from
one relay would call most of the others open.
So the witness keys on the instrument's **overload sentinel**: closed is "reads a
number at all", open is "out of range", and neither claim depends on the wiring's
series resistance. This is the same reason the K0 reading was allowed to step
+4.55 Ω between sessions without invalidating anything.

The one exception is ``test_all_eight_relays_switch_on_the_real_device``, which
by its nature energises all eight and is therefore **skipped unless
``BENCHCTRL_ADU218_SWEEP_ALL=1``**. Setting that variable is the operator saying
they know what is on every output. It also has no DMM witness — the bench has one
meter and it is across a single relay — so it checks the driver's own read-back
against the whole-port mask command, which is a weaker claim than the witnessed
test's and is labelled as such in the test.

The input and counter tests need a signal the bench cannot generate on its own:
the SDG1032X function generator has no driver yet, so the stimulus is set by hand
and the tests skip when the line is idle. Keep it well under ~25 Hz — host
level-sampling round-trips in ~17 ms, so the cross-check against the device
counter aliases above that and the test skips rather than reporting a false
disagreement.

Why the witness reads 2-wire, and why no threshold is asserted
--------------------------------------------------------------
4-wire needs separate source and sense leads; on this bench the DMM has one
pair across the relay, so ``measure_resistance_4wire()`` returns drifting
negative values — measured, and the reason this file uses the 2-wire function.

The closed-contact figure is **not** compared against a threshold. The same
closed relay measured 6.14 Ω, 10.69 Ω, 10.65 Ω and 9.40 Ω across four sessions,
milliohm-stable *within* each, with the step traced to re-seated screw-clamped
probes rather than to the relay. Any threshold set from one of those numbers
misreads the others.

**Nor is within-session stability something to rely on.** That "milliohm-stable"
observation was K0 on screw clamps; K7 on spring clips is not. Measured over 15
back-to-back runs of the transition sequence, K7 closed drifted **62 → 127 →
61 Ω**, monotonically and then back, and the earlier same-circuit bound of
``hi < lo * 2`` worst-cased at 1.80 — which is why it failed roughly one suite
run in seven. It is now an order of magnitude, and the reasoning is at the
assertion. The load-bearing claim was never the number: it is closed-reads-a-
number versus open-over-ranges, and that survives any amount of clip drift. What is asserted is the shape that no probe seating can
change: closed reads *a number*, open reads the DMM's **overload sentinel**.
That sentinel is what makes this a real witness — an open contact is not a large
resistance, it is an unmeasurable one, so the two states cannot be confused by
any amount of contact drift.

The one thing that sentinel *cannot* distinguish is an open contact from leads
that are not across the contact at all — both are unmeasurable. So the witness
fixture first reads DC volts and skips if the leads are sitting on a powered
net: a dry contact reads ~0 V open or closed, so this cannot mask a stuck relay,
but it does turn "the leads got moved" from a bare ``assert None is not None``
into a sentence naming the actual problem. Measured after the CP2112 work moved
the meter: 3.392 V standing, unmoved by K0.

That gate has a known blind spot, stated because it decides how much the pass is
worth: it catches leads on a *powered* net, not leads across a *different dry
contact*. Another open contact reads ~0 V exactly like the right one, so a stale
``BENCHCTRL_ADU218_RELAY`` still produces a failure rather than a skip. What the
relay assertions therefore do is name **both** causes and print the device's own
read-back — verified by pointing the tests at K5 while the leads sat on K7: both
witnessed tests fail, and their messages say the leads may be the problem instead
of accusing the relay.

Two processes cannot hold the SDM4065A at once, and a running ``benchctrl-agent``
opens it lazily on first claim and then keeps the VISA handle *after* the claim
is released — so a direct open fails with errno 16 while the meter sits idle.
That arrives looking exactly like an unplugged instrument. Rather than require
the agent to be stopped (which safe-stops the whole bench), the witness falls
back to borrowing the meter *through* the agent, as
``tests/test_hardware_cp2112.py`` does by design. Both routes are exercised on
this bench. The overload sentinel survives the remote trip because
``SDM4065AOverloadError`` is registered in ``net/errors.py``; if it were not,
this witness would silently lose the distinction it is built on.
"""

from __future__ import annotations

import os
import re
import time

import pytest

from benchctrl.drivers.ontrak_adu218 import (
    ADU218Error,
    ADU218PolicyError,
    OntrakADU218,
)
from benchctrl.drivers.ontrak_adu218.driver import DEBOUNCE_MS

pytestmark = pytest.mark.hardware


HW_SERIAL = os.environ.get("BENCHCTRL_ADU218_SERIAL") or None

#: The one relay these tests may switch. Deliberately a single int, and
#: deliberately also the whole allowlist passed to ``open()``, so a bug here
#: cannot reach a relay the operator did not nominate.
#:
#: The default tracks where the DMM actually is on this bench, which moves. It
#: was K0; the operator moved the leads to **K7** on 2026-08-27. A stale default
#: does not fail loudly -- it makes the wiring gate below skip three tests -- so
#: this constant is the one place to change, and the gate is what catches it
#: being wrong.
TEST_RELAY = int(os.environ.get("BENCHCTRL_ADU218_RELAY", "7"))

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
def adu_all_relays():
    """A session allowed to switch **every** relay, gated on an explicit opt-in.

    Separate from ``adu`` rather than a parameter on it, so the default session
    keeps its single-relay allowlist. The gate is the operator's statement that
    they know what is on all eight outputs; without it, skip.
    """
    if os.environ.get("BENCHCTRL_ADU218_SWEEP_ALL") != "1":
        pytest.skip(
            "set BENCHCTRL_ADU218_SWEEP_ALL=1 to energise all eight relays. "
            "Only do that when you know what is attached to every one of them"
        )
    try:
        dev = OntrakADU218.open(serial=HW_SERIAL)  # allowlist defaults to all 8
    except ADU218Error as exc:
        pytest.skip(f"ADU218 not reachable (serial={HW_SERIAL!r}): {exc}")
    try:
        yield dev
    finally:
        try:
            dev.reset_relays()
        finally:
            dev.close()


@pytest.fixture()
def armable():
    """Gate on the operator agreeing the watchdog may actually be armed.

    Arming it de-energises relays by timeout, which is correct behaviour and a
    bad surprise on a bench someone else is using. The default for this whole
    file is that the watchdog is never armed; this is the one opt-out.
    """
    if os.environ.get("BENCHCTRL_ADU218_ARM_WATCHDOG") != "1":
        pytest.skip(
            "set BENCHCTRL_ADU218_ARM_WATCHDOG=1 to let the watchdog trip. It "
            "de-energises the relay under test by timeout; only enable it when "
            "the bench is yours"
        )


@pytest.fixture()
def counted(adu):
    """The (port, line, counter) under an external square wave, or a skip.

    Returns the counter index alongside the port and line because the mapping —
    counters 0-3 to PA0-PA3, 4-7 to PB0-PB3 — is what several of these tests
    exist to verify, so it is computed here once from the documented rule rather
    than hardcoded per test. Every position of that rule is now measured — all
    four PORT A lines and all four PORT B lines, each moving its own counter
    alone — so the ``+ 4`` rests on three independent PORT B readings rather
    than on the Table 1 image's word.

    Skips when the line is not toggling. That is a fact about the bench (the
    generator is driven by hand; the SDG1032X has no driver yet), not a driver
    defect, and a red test for it would train people to ignore this file.
    """
    spec = os.environ.get("BENCHCTRL_ADU218_INPUT", "B2")
    # Parse defensively: this guard exists to *skip* on a bad value, so it must
    # not raise on one. ``int(spec[1:])`` on "XY" throws before the check below
    # ever runs, and an empty string never even reaches it.
    if not re.fullmatch(r"[AaBb][0-3]", spec):
        pytest.skip(f"BENCHCTRL_ADU218_INPUT={spec!r} is not a port A/B line 0-3")
    port = spec[0].upper()
    line = int(spec[1])
    counter = line + (0 if port == "A" else 4)

    # Confirm the line is actually moving before any test relies on it.
    seen = {adu.input_state(port, line) for _ in range(40)}
    if seen != {True, False}:
        pytest.skip(
            f"P{port}{line} is not toggling (read only {seen}); connect a square "
            f"wave well under ~25 Hz to it, or set BENCHCTRL_ADU218_INPUT"
        )
    return port, line, counter


def _dmm_via_agent(busy_exc):
    """Borrow the SDM4065A from the running agent, or skip.

    The fallback path when a direct VISA open reports errno 16. Modelled on the
    ``dmm`` fixture in ``tests/test_hardware_cp2112.py``, which goes through the
    agent by design because the agent holds the writer claim
    (``KNOWN_LIMITATIONS.md`` §N-4).

    Returns ``(proxy, close)``. The proxy answers ``measure_resistance`` and
    ``measure_dc_voltage`` like the local driver, so the fixture above does not
    care which route it got.
    """
    import json

    from benchctrl.config import EndpointConfig
    from benchctrl.net.client import RemoteClient

    try:
        # Read inside the process that consumes it, and close the handle at once:
        # this is the agent's shared secret and it should not outlive the parse.
        with open("/etc/benchctrl/agent.json") as fh:
            cfg = json.load(fh)
    except OSError as exc:
        pytest.skip(
            f"the DMM is held by another process ({busy_exc}) and the agent "
            f"config is not readable to borrow it through ({exc}); stop the "
            f"agent, or run this on the bench board"
        )

    ep = EndpointConfig(
        host="127.0.0.1",
        port=cfg["port"],
        token=cfg["token"],
        heartbeat_s=1.0,
        deadman_s=15.0,
    )
    try:
        client = RemoteClient(ep).connect()
    except Exception as exc:  # noqa: BLE001 - any connect failure is a skip
        pytest.skip(
            f"the DMM is held by another process ({busy_exc}) and the agent "
            f"is not reachable to borrow it through ({exc})"
        )
    try:
        return client.attach("siglent_sdm4065a"), client.close
    except Exception as exc:  # noqa: BLE001
        client.close()
        pytest.skip(f"the agent has no siglent_sdm4065a to borrow: {exc}")


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
    close_dmm = None
    try:
        dmm = SiglentSDM4065A.open(resource=resource) if resource else SiglentSDM4065A.open()
        close_dmm = dmm.close
    except SDM4065AError as exc:
        # Measured: a running benchctrl-agent opens the SDM4065A lazily on first
        # claim and keeps the VISA handle after the claim is released, so the
        # meter stays held by a process that is not using it. Two processes
        # cannot hold one USBTMC node, and this arrives as errno 16 -- which
        # reads exactly like an unplugged meter.
        #
        # Stopping the agent works but safe-stops the whole bench, so borrow the
        # meter through the agent instead, the way tests/test_hardware_cp2112.py
        # does. The overload sentinel survives that trip: SDM4065AOverloadError
        # is registered in net/errors.py precisely so a remote caller can still
        # tell "out of range" from "bad command", and this witness depends on
        # that distinction rather than on a bare RuntimeError.
        if "busy" not in str(exc).lower():
            pytest.skip(
                f"the DMM witness is not reachable ({exc}); the relay tests "
                f"here are meaningless without an instrument that is not the "
                f"ADU218"
            )
        dmm, close_dmm = _dmm_via_agent(exc)

    # Are the leads actually across a dry contact? An open contact and a pair of
    # leads clipped somewhere else both answer "unmeasurable" to a resistance
    # read, so the resistance witness alone cannot tell them apart -- and the
    # failure it produces (`assert witness() is not None`) points at the relay
    # when the fault is in the wiring.
    #
    # DC volts is the discriminator, and it does not blur the two states this
    # test exists to distinguish: a dry contact reads ~0 V whether it is open or
    # closed, so a stuck relay still fails red. Only a lead on a *powered* net
    # reads volts. Measured on this bench: leads left on a 3.3 V rail after the
    # CP2112 work read 3.392 V, unmoved by K0 -- which is what this catches.
    try:
        standing_volts = abs(dmm.measure_dc_voltage())
    except SDM4065AError:  # pragma: no cover - a meter that reads R but not V
        standing_volts = 0.0
    if standing_volts > 0.5:
        close_dmm()
        pytest.skip(
            f"the DMM leads are sitting on a powered net ({standing_volts:.3f} V "
            f"DC standing). A relay contact under test is dry and reads ~0 V, "
            f"so a resistance witness here would report 'open' no matter what "
            f"relay K{TEST_RELAY} does. Move the leads across the contact"
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
        close_dmm()


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
            # Name both causes, because this evidence cannot separate them. An
            # SSR that never closed and leads across a *different* dry contact
            # both over-range, and the DC-volts gate in the fixture does not
            # catch the second: another open contact reads ~0 V exactly like the
            # right one. Asserting "the switch did not happen" here accused a
            # working K7 when the leads were still on K0. Report the device's own
            # read-back too -- it is not independent evidence, but it is the fact
            # that tells the operator which of the two to go and look at.
            assert measured is not None, (
                f"driver reports relay {TEST_RELAY} energised (read-back "
                f"{adu.relay_state(TEST_RELAY)}) but the DMM still over-ranges. "
                f"Either that relay is not switching, or the leads are not "
                f"across it -- both over-range, and this test cannot tell them "
                f"apart. Check BENCHCTRL_ADU218_RELAY against where the leads "
                f"actually are"
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

    # No threshold — see the module docstring. What is checked is that both
    # closed readings are the *same circuit*, not two different ones.
    #
    # The bound is deliberately an order of magnitude, and the factor of 2 it
    # replaces is why. That factor came with the comment "the within-session
    # spread is milliohms", which was true of K0's screw clamps and is false of
    # K7's spring clips: measured over 15 back-to-back runs of this exact
    # sequence, K7 closed drifted 62 -> 127 -> 61 Ohm, monotonically and then
    # back, with a worst within-trial ratio of 1.80 against a limit of 2.00. So
    # the old bound was measuring the clips and it failed once in ~7 suite runs.
    #
    # Widening it costs little, because this assertion was never the load-bearing
    # one: the *witness* is the overload sentinel, and closed-vs-open is what the
    # test actually proves.
    #
    # Be honest about what is left, though. At an order of magnitude this no
    # longer catches the leads moving between two *similar* points -- checked
    # against real figures from this bench, a K7 -> K0 move is a ratio of 3.81
    # and a K0 -> K7 move 6.64, and neither trips it; only something like a
    # different net entirely (36 -> 600 Ohm, ratio 16.7) does. That coverage was
    # already illusory rather than real: a bound the clips can breach on their
    # own reports lead movement that did not happen, and there is no threshold
    # that separates 3.81 from a 1.80 the hardware produces unaided. The thing
    # that genuinely catches a lead on the wrong relay is the pair of "either the
    # relay is not switching or the leads are not across it" assertions above,
    # which fire whenever the nominated relay is not the one being measured.
    #
    # All eight relays were later measured in one session by walking the meter
    # across them: 16.87 / 17.50 / 20.77 / 28.37 / 31.36 / 32.09 / 41.19 / 45.65
    # Ohm, a 2.7x spread with no relation to index. So the no-threshold rule is
    # not an inference from two relays any more.
    #
    # **Do not add a ceiling on the closed reading.** During that walk a loose
    # screw terminal made K3 read 336 kOhm closed and this test passed, which
    # looks like a hole and is not one. The claim here is that the relay's state
    # follows the command, and at 336 kOhm that was *true* -- overload -> finite
    # is a real state change. What was wrong was bench wiring quality, which is
    # not this test's claim and is not visible to the driver. A ceiling would
    # also be precisely the threshold-on-a-wiring-property that the paragraphs
    # above rule out: a loose terminal is a wiring property, so it falls inside
    # that rule rather than justifying an exception to it. In service these
    # relays switch a load, where what matters is the voltage drop and the
    # dissipation, not whether the ohms land in a band.
    lo, hi = min(closed_readings), max(closed_readings)
    assert hi < lo * 10, (
        f"the two closed-contact readings are not the same circuit: "
        f"{closed_readings} Ohm. Contact drift alone does not do this — the "
        f"leads most likely moved between the two closures"
    )


def test_reset_relays_reaches_a_genuinely_open_contact(adu, witness):
    """The safe state, confirmed from outside the driver.

    ``reset_relays()`` is the call an operator makes to *believe* the bench is
    de-energised, and its verification is the device's own read-back. This is
    the same claim checked against an instrument that has no stake in it.
    """
    adu.set_relay_state(TEST_RELAY, True)
    # The control: without this, the test below passes on a meter that reads
    # over-range for any reason at all -- including leads on the wrong contact.
    # It needs its own message for the same reason as the transition test: a bare
    # `assert None is not None` reads as a relay fault when it is equally likely
    # to be the wiring, and the two are indistinguishable from a resistance read.
    assert witness() is not None, (
        f"cannot close relay K{TEST_RELAY} onto the meter, so the open-contact "
        f"claim below would be unfalsifiable. Either that relay is not switching "
        f"or the leads are not across it; check BENCHCTRL_ADU218_RELAY"
    )
    assert adu.reset_relays() == 0
    assert witness() is None, (
        f"reset_relays() reported success but K{TEST_RELAY} still conducts — "
        f"the safe state was claimed and not reached"
    )


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
# Claim 1b: all eight relays, opt-in — the sweep that was previously ad-hoc
# ---------------------------------------------------------------------------


def test_all_eight_relays_switch_on_the_real_device(adu_all_relays):
    """Every relay individually, on hardware. **Opt-in**, and here is why.

    The other relay tests above pin ``allowed_relays`` to the single nominated
    ``BENCHCTRL_ADU218_RELAY``, which is what makes them safe to run by default:
    a bug cannot reach a relay whose load the operator has not vouched for. This
    test necessarily energises all eight, so it is gated behind
    ``BENCHCTRL_ADU218_SWEEP_ALL=1`` rather than being on by default. Per
    ``AGENTS.md``: never energise an output without knowing what is attached.
    Setting that variable *is* the operator saying they know.

    All eight plus both port masks had passed on this bench before, but only in
    an ad-hoc RPC sweep that was not codified — so nothing would have caught a
    regression on relays 1-7. That gap is what this closes.

    No DMM witness: the bench has one meter and it is across ``TEST_RELAY``, so
    seven of the eight cannot be independently witnessed. The device's own read-back is
    therefore the check here, which is weaker — deliberately so, and stated. The
    *witnessed* claim stays the single-relay test above; this one proves the driver
    addresses all eight distinctly, and the mask cross-check is what makes
    "distinctly" mean something.
    """
    adu = adu_all_relays
    assert adu.reset_relays() == 0

    for index in range(8):
        assert adu.set_relay_state(index, True) is True, f"relay {index} did not close"
        # The mask is a second, independent command (PK) reading the whole port,
        # so agreeing with it rules out a per-relay read that echoes the write.
        assert adu.relay_mask() == 1 << index, (
            f"commanding relay {index} produced mask {adu.relay_mask():#010b}; "
            f"either it moved the wrong relay or it moved more than one"
        )
        assert adu.set_relay_state(index, False) is False
        assert adu.relay_mask() == 0

    # Both port masks, since MKddd is a different command path than SKn/RKn.
    assert adu.set_relay_port(0b10101010) == 0b10101010
    assert adu.relay_mask() == 0b10101010
    assert adu.set_relay_port(0b01010101) == 0b01010101
    assert adu.relay_mask() == 0b01010101
    assert adu.reset_relays() == 0


# ---------------------------------------------------------------------------
# Claim 1c: the inputs and counters, against a real signal
# ---------------------------------------------------------------------------


def test_a_driven_input_line_reads_high_and_only_its_own_counter_moves(adu, counted):
    """The input/counter half of the device, which needs an external stimulus.

    Requires a square wave on the line named by ``BENCHCTRL_ADU218_INPUT``
    (default PB2, the line wired to the SDG1032X on this bench). Skips without
    it, because no amount of driver code can make an undriven line read high.

    Two claims, and the second is the one that could not be read from the
    manual at all: the counter-to-input map lives **only in a Table 1 image**
    that the PDF's text layer omits entirely. So the mapping the driver uses is
    measured here or it is unverified.

    Run once per port to get the full claim. PA3 pins the PORT A half; PB2 pins
    the ``+ 4`` offset for PORT B, which nothing else can — with the generator
    on PORT A every counter index equals its line number, so an offset of 4, 3
    or 0 are indistinguishable. Confirmed by mutation: forcing the offset to 3
    fails this test and the two below it.
    """
    port, line, counter = counted

    # The line is toggling, so a single read can legitimately catch either
    # level. Sample until both are seen rather than asserting one.
    levels = {adu.input_state(port, line) for _ in range(60)}
    assert levels == {True, False}, (
        f"P{port}{line} read only {levels} across 60 samples — either the "
        f"stimulus is not connected or it is far faster than the ~17 ms round "
        f"trip and every sample aliased onto one level"
    )

    # The mask must agree with the per-line read, via a different command --
    # and it has to be sampled for the same reason the per-line read above is.
    # A single `input_mask()` on a line toggling at 10 Hz catches a low half
    # the time, which made this a coin flip rather than a check (measured: 2
    # passes in 6 consecutive runs against the real device).
    #
    # Sampling the *union* is what keeps it falsifiable. Asserting the line is
    # ever high would pass on a mask that reported every line high, so this
    # also asserts nothing outside the stimulated line ever set a bit -- the
    # bit-position claim, which is the one a wrong shift would break.
    #
    # `counter` doubles as the PI bit index, and that is two separate facts
    # agreeing rather than one: PI packs PORT A into the low nibble (so PORT B
    # line n is bit n+4) and the counter map offsets PORT B by 4. Both were
    # measured on PB2 -- bit 6 set, counter 6 moving -- so the reuse is
    # witnessed, not assumed.
    seen = 0
    for _ in range(60):
        seen |= adu.input_mask()
    assert seen & (1 << counter), (
        f"input_mask() never showed P{port}{line} set across 60 samples, but "
        f"input_state() saw it toggle — the two commands disagree about which "
        f"bit this line occupies"
    )
    assert seen == (1 << counter), (
        f"input_mask() set bits {sorted(i for i in range(8) if seen & (1 << i))} "
        f"but only P{port}{line} (bit {counter}) is being driven — either "
        f"another line is floating high or the bit positions are wrong"
    )

    before = adu.read_counters()
    time.sleep(2.0)
    after = adu.read_counters()

    moved = sorted(i for i in range(8) if (after[i] - before[i]) % 65536)
    assert moved == [counter], (
        f"expected only counter {counter} to move for P{port}{line}, but "
        f"{moved} moved — the counter-to-input map is not what the driver "
        f"assumes (it comes from a manual image, not from text)"
    )


def test_clear_counter_is_read_and_clear_on_the_device(adu, counted):
    """``RCn`` returns the count *and* zeroes it, on real hardware.

    Untestable without a stimulus: on an idle line every counter reads 0 and a
    clear is indistinguishable from a no-op. That is why this needs the signal.
    """
    _, _, counter = counted

    time.sleep(1.0)  # accumulate something to destroy
    before = adu.read_counter(counter)
    assert before > 0, "no events accumulated; is the stimulus running?"

    cleared = adu.clear_counter(counter)
    assert cleared >= before, "RCn must return at least what RE had just read"

    after = adu.read_counter(counter)
    # Not `== 0`: the line is live, so edges arrive during the round trip. What
    # must hold is that the count restarted rather than continued.
    assert after < cleared, (
        f"RC{counter} returned {cleared} but the counter still reads {after}; "
        f"it did not clear"
    )


def test_the_counter_counts_cycles_not_edges(adu, counted):
    """One count per cycle, cross-checked against host level-sampling.

    The manual says "low to high transitions" (§6c), which implies one count per
    cycle rather than one per edge — but the two differ by exactly 2x, and a
    factor of two in a frequency measurement is the kind of error that looks
    plausible forever. So it is measured against an independent method whose
    failure mode is different: the device counter can see edges the host never
    samples, while the host sampler aliases if the wave is fast.

    Only meaningful well below the host's sampling Nyquist limit (~28 Hz for a
    ~17 ms round trip). Above that the sampler under-counts and the comparison
    would fail for a reason that has nothing to do with the device — so this
    skips rather than lies when the signal is too fast.
    """
    port, line, counter = counted

    c0 = adu.read_counter(counter)
    t0 = time.monotonic()
    levels = []
    while time.monotonic() - t0 < 10.0:
        levels.append(adu.input_state(port, line))
    elapsed = time.monotonic() - t0
    c1 = adu.read_counter(counter)

    device_rate = ((c1 - c0) % 65536) / elapsed
    rises = sum(1 for a, b in zip(levels, levels[1:]) if b and not a)
    host_rate = rises / elapsed

    assert device_rate > 0, "the counter did not move; is the stimulus running?"
    assert host_rate > 0, "the host sampler saw no rising edge"

    sample_hz = len(levels) / elapsed
    if device_rate > sample_hz / 4:
        pytest.skip(
            f"stimulus at ~{device_rate:.1f} Hz is too fast for host sampling at "
            f"{sample_hz:.1f} samples/s to cross-check; lower the generator"
        )

    ratio = device_rate / host_rate
    assert 0.8 <= ratio <= 1.25, (
        f"device counted {device_rate:.3f}/s but the host saw {host_rate:.3f} "
        f"rising edges/s (ratio {ratio:.3f}). Near 2.0 would mean the counter "
        f"counts both edges, which would make every frequency derived from it "
        f"twice the truth"
    )


def test_the_debounce_setting_round_trips_on_the_device(adu):
    """All three settings accepted and read back by the real device.

    What this does **not** claim is that the settings have a measurable *effect*.
    Measured on this bench against a clean 10 Hz wave, 20 s per setting: 10.042,
    9.992 and 9.992 counts/s — a 0.5 % spread, i.e. indistinguishable. That is
    the expected result, not a fault: the filter widths are 10 ms, 1 ms and
    100 µs (manual §6c) and all three are far shorter than a 50 ms half-period,
    so none of them has anything to reject.

    Discriminating them needs a period near 10 ms — a few hundred Hz — against a
    counter rated to only 1 kHz, and above that rating the count under-reports
    silently. Not attempted here; see ``fixtures/adu218/counters_live_signal.txt``.
    """
    original = adu.read_debounce()
    try:
        for setting in (0, 1, 2):
            assert adu.set_debounce(setting) == setting
            assert adu.read_debounce() == setting
            # The width, not the setting number — and they run in opposite
            # directions, which is the whole reason read_debounce_ms exists.
            assert adu.read_debounce_ms() == DEBOUNCE_MS[setting]
        assert adu.read_debounce_ms() == 0.1, "setting 2 is the *shortest* filter"
    finally:
        adu.set_debounce(original)


# ---------------------------------------------------------------------------
# Claim 1d: the watchdog actually trips, witnessed — opt-in
# ---------------------------------------------------------------------------


def test_the_watchdog_trips_and_the_dmm_sees_the_contact_open(adu, witness, armable):
    """The interlock firing, witnessed by an instrument that cannot feed it.

    This is the claim the whole watchdog design rests on: on timeout the
    *device* de-energises the relays, with no benchctrl process, no GPIO and no
    kernel driver in the decision path. Everything else about the watchdog is
    tested against a synthetic clock, which can only show the driver's model is
    self-consistent.

    **Opt-in** (``BENCHCTRL_ADU218_ARM_WATCHDOG=1``) because it deliberately
    de-energises a relay by timeout. On a bench shared with a running experiment
    that is a bad surprise, and per ``AGENTS.md`` the operator is the one who
    knows whether it is safe now.

    Why the DMM is the witness and not ``RPK0``: reading the ADU218 at all
    refeeds the timer, so any polling loop prevents the timeout under test. The
    DMM is on a separate bus and sends nothing to the ADU218, so it can observe
    the trip without perturbing it. It is the only independent evidence the
    contact physically moved.

    Uses ``WD1`` (1 s, trip bracketed to (0.90, 1.10] s in
    ``fixtures/adu218/watchdog_trip.txt``) so the silent window is short.
    """
    adu.reset_relays()
    assert adu.set_relay_state(TEST_RELAY, True) is True
    assert witness() is not None, "control failed: the contact did not close"

    try:
        assert adu.set_watchdog(1) == 1

        # SILENCE. Not one command to the ADU218 — including any read — or the
        # timer is refed and the thing under test cannot happen. The DMM read
        # below is what consumes the window, and it takes ~0.7 s on its own, so
        # this sleep plus that latency comfortably exceeds the 1.1 s bound.
        time.sleep(1.5)

        assert witness() is None, (
            "the watchdog was armed at WD1 and 1.5 s of silence elapsed, but "
            "the DMM still measures a closed contact — the interlock did not "
            "fire, and nothing else in this suite would have caught that"
        )

        # Only now talk to the device again. The setting must have self-cleared:
        # the manual says a timeout resets the watchdog setting to 0, which is
        # also why WD=0 is ambiguous and must be read against a held expectation.
        assert adu.read_watchdog() == 0, "a trip must reset the setting to WD0"
        assert adu.read_watchdog_tripped() is True, (
            "the driver held WD1 as expected and the device reports 0, which is "
            "a trip; read_watchdog_tripped() must report it"
        )
        # It clears the held expectation as it reports, so a second call is False
        # — a trip is consumed once, not latched forever.
        assert adu.read_watchdog_tripped() is False
    finally:
        adu.set_watchdog(0)
        adu.reset_relays()


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

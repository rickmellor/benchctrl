"""The governor's opt-in mains cut, and the exemptions around it.

Three separate claims are tested here, and they pull in opposite directions —
which is why they need to be pinned against each other rather than
individually:

1. **The PDU is exempt from the ordinary safe state.** For every other
   instrument "safe" means stop sourcing. Cutting mains is itself disruptive,
   so a lost heartbeat must not turn into a bench-wide power failure.
2. **The PDU is exempt from arm tracking.** Energising an outlet is not arming
   an output. Treating it as one would start a deadman countdown on every
   switch, and would mark the PDU armed with no way to disarm it — because of
   claim 1.
3. **But `panic_outlets` must actually cut.** Claims 1 and 2 together mean the
   PDU never appears in `armed_devices`, so a naive `trip()` would skip it
   entirely and `panic_outlets` would be a setting that silently did nothing.

The trap this file exists to catch is an *inert* governor: a panic cut that
looks configured, reports success, and never moves a contactor. That failure is
indistinguishable from working, so every test here asserts on the simulator's
actual outlet state rather than on a return value.
"""

from __future__ import annotations

import time

import pytest

from benchctrl.agent.safety import (
    PANIC_CUT_CONFIRM_S,
    SAFE_STATE_TIMEOUT_S,
    SafetyGovernor,
    TripOutcome,
    TripReason,
    default_safe_state,
    panic_outlet_safe_state,
    panic_outlets_of,
)
from benchctrl.agent.worker import DeviceWorker

KEY = "cyberpower_pdu41002"


@pytest.fixture()
def pdu():
    """A real driver over a pty to a simulated PDU, with outlets 2-3 panicable."""
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3, 4), panic_outlets=(2, 3))
    try:
        yield driver
    finally:
        driver.close()


@pytest.fixture()
def worker():
    w = DeviceWorker(KEY).start()
    try:
        yield w
    finally:
        w.stop()


def _trip(governor, pdu, worker, *, extra_devices=None, extra_workers=None):
    devices = {KEY: pdu, **(extra_devices or {})}
    workers = {KEY: worker, **(extra_workers or {})}
    return governor.trip(TripReason.HEARTBEAT_LOST, devices, workers)


# ---------------------------------------------------------------------------
# The exemptions
# ---------------------------------------------------------------------------


def test_the_default_safe_state_does_not_touch_an_outlet(pdu):
    """Claim 1, at its narrowest: `default_safe_state` is inert on the PDU.

    It is inert by accident — the PDU implements none of the four methods the
    function tries — so this test is what stops somebody "fixing" the omission
    into a bench-wide power cut. Asserted on outlet state *and* on the emitted
    bytes, because a call that failed silently would leave state unchanged too.
    """
    sim = pdu._benchctrl_sim
    before = dict(sim.outlet_state)
    sim.command_log.clear()

    default_safe_state(pdu)

    assert sim.outlet_state == before
    assert not [c for c in sim.command_log if c.startswith("oltctrl")]


def test_switching_an_outlet_does_not_arm_the_device():
    """Claim 2. `set_outlet_state` must not be in `_ARMING_CALLS`.

    If it were, every switch would start a deadman countdown — so an operator
    who powered up the bench and went to lunch would return to a tripped
    governor — and the PDU would be permanently armed, since claim 1 means
    nothing can ever disarm it.
    """
    governor = SafetyGovernor()
    governor.observe_call(KEY, "set_outlet_state", (3, True), {})
    assert governor.armed_devices == []
    assert not governor.any_armed


def test_energising_an_outlet_does_not_start_the_deadman():
    """The consequence of claim 2 that an operator would actually notice.

    `should_trip` gates on `any_armed`, so a bench where the only recent
    activity was a mains switch must never trip no matter how long contact has
    been lost.
    """
    governor = SafetyGovernor(deadman_s=0.0)
    governor.observe_call(KEY, "set_outlet_state", (3, True), {})
    time.sleep(0.01)
    assert governor.should_trip() is False


def test_the_default_safe_state_still_disarms_a_normal_instrument():
    """The control for the exemption tests: `default_safe_state` is only inert
    on devices that implement none of its methods, not inert generally."""

    class FakeSMU:
        def __init__(self):
            self.output = True

        def set_output(self, on):
            self.output = on

    smu = FakeSMU()
    default_safe_state(smu)
    assert smu.output is False


# ---------------------------------------------------------------------------
# The cut
# ---------------------------------------------------------------------------


def test_panic_outlets_of_reads_the_drivers_authorisation(pdu):
    assert panic_outlets_of(pdu) == frozenset({2, 3})


def test_panic_outlets_of_treats_an_unaware_device_as_unauthorised():
    """Duck-typed so `safety.py` need not import a driver. Anything without the
    property must read as "no authorisation", never as "all outlets"."""

    class Whatever:
        pass

    assert panic_outlets_of(Whatever()) == frozenset()
    assert panic_outlets_of(object()) == frozenset()


def test_the_cut_actually_de_energises_the_authorised_outlets(pdu):
    """Claim 3, directly: the outlets must read off afterwards.

    Asserted on simulator state, not on the absence of an exception. A cut that
    emitted its commands and never verified them would pass a
    "did-not-raise" test while leaving mains live.
    """
    sim = pdu._benchctrl_sim
    panic_outlet_safe_state(pdu, deadline_s=PANIC_CUT_CONFIRM_S)

    assert sim.outlet_state[2] is False
    assert sim.outlet_state[3] is False


def test_the_cut_leaves_unauthorised_outlets_alone(pdu):
    """Outlet 4 is in `allowed_outlets` but not `panic_outlets` — the whole
    point of having two lists. A governor that cut everything it *could* would
    make the narrower list decorative."""
    sim = pdu._benchctrl_sim
    panic_outlet_safe_state(pdu, deadline_s=PANIC_CUT_CONFIRM_S)

    assert sim.outlet_state[4] is True
    assert sim.outlet_state[1] is True  # not even allowed


def test_the_cut_raises_when_an_outlet_does_not_go_off(pdu):
    """The failure that must not be reported as success.

    `oltctrl` acknowledges nothing, so a device that accepts the command and
    moves nothing is byte-identical to one that obeys. Telling an operator a
    DUT is de-powered when its contactor never moved is worse than failing
    loudly, so this raises — and `trip()` turns that into FAILED.
    """
    sim = pdu._benchctrl_sim
    sim.ignore_switches = True

    with pytest.raises(RuntimeError, match="did not read off"):
        panic_outlet_safe_state(pdu, deadline_s=1.0)

    assert sim.outlet_state[2] is True


def test_the_cut_is_a_no_op_without_authorisation():
    """Empty `panic_outlets` is the default, and it must write zero bytes —
    not "cut nothing after connecting", which would still hold the device's
    single CLI session at the worst possible moment."""
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3))
    try:
        sim = driver._benchctrl_sim
        sim.command_log.clear()
        panic_outlet_safe_state(driver, deadline_s=1.0)
        assert sim.command_log == []
        assert all(sim.outlet_state.values())
    finally:
        driver.close()


def test_one_refused_outlet_does_not_strand_the_others(pdu, monkeypatch):
    """A safety path takes every avenue. If outlet 2's command raises, outlet 3
    must still be cut — the alternative leaves live mains on an outlet the
    operator explicitly authorised the governor to kill."""
    sim = pdu._benchctrl_sim
    real = pdu.set_outlet_state

    def flaky(index, on, **kwargs):
        if index == 2:
            raise RuntimeError("simulated link hiccup on outlet 2")
        return real(index, on, **kwargs)

    monkeypatch.setattr(pdu, "set_outlet_state", flaky)

    # The deadline has to clear the outlet's configured `td_off` (3 s as
    # shipped). A tighter one fails this test for the wrong reason: outlet 3's
    # command goes out and the confirm loop gives up before the contactor
    # settles, which looks identical to never having sent it.
    with pytest.raises(RuntimeError, match=r"commands refused for \[2\]"):
        panic_outlet_safe_state(pdu, deadline_s=PANIC_CUT_CONFIRM_S)

    # The point of the test: 3 went off despite 2 failing first.
    assert sim.outlet_state[3] is False
    assert sim.outlet_state[2] is True


# ---------------------------------------------------------------------------
# Through trip()
# ---------------------------------------------------------------------------


def test_a_trip_with_nothing_armed_cuts_nothing(pdu, worker):
    """An idle bench must not power down.

    The panic cut exists to stop an *unattended live output*. With nothing
    armed there is no live output, so a lost heartbeat is a connectivity
    problem, not a hazard — and cutting mains would create one.
    """
    sim = pdu._benchctrl_sim
    outcomes = _trip(SafetyGovernor(), pdu, worker)

    assert outcomes == {}
    assert sim.outlet_state[2] is True
    assert sim.outlet_state[3] is True


def test_a_trip_with_something_armed_cuts_the_panic_outlets(pdu, worker):
    """Claim 3 through the real ladder.

    The PDU is never in `armed_devices` (claims 1 and 2), so this is the test
    that fails if `trip()` only walks armed devices — the exact shape of an
    inert governor: `panic_outlets` set, no error anywhere, no contactor moved.
    """
    sim = pdu._benchctrl_sim
    governor = SafetyGovernor()

    smu_worker = DeviceWorker("smu").start()
    try:
        class FakeSMU:
            def __init__(self):
                self.output = True

            def set_output(self, on):
                self.output = on

        smu = FakeSMU()
        governor.observe_call("smu", "set_output", (True,), {})
        assert governor.armed_devices == ["smu"]

        outcomes = _trip(
            governor,
            pdu,
            worker,
            extra_devices={"smu": smu},
            extra_workers={"smu": smu_worker},
        )
    finally:
        smu_worker.stop()

    assert outcomes["smu"] is TripOutcome.SAFE
    assert outcomes[KEY] is TripOutcome.SAFE
    assert smu.output is False
    assert sim.outlet_state[2] is False
    assert sim.outlet_state[3] is False
    assert sim.outlet_state[4] is True


def test_the_armed_instrument_is_disarmed_before_the_mains_is_cut(pdu, worker):
    """Ordering, and it is not cosmetic: pulling mains out from under a live
    output is how you get an inductive kick into a DUT. Turn off what is
    driving current first, then remove its supply."""
    order: list[str] = []
    governor = SafetyGovernor()

    smu_worker = DeviceWorker("smu").start()
    try:
        class FakeSMU:
            def set_output(self, on):
                order.append("smu_off")

        real_switch = pdu.set_outlet_state

        def note(index, on, **kwargs):
            order.append("outlet_off")
            return real_switch(index, on, **kwargs)

        pdu.set_outlet_state = note  # type: ignore[method-assign]
        governor.observe_call("smu", "set_output", (True,), {})
        _trip(
            governor,
            pdu,
            worker,
            extra_devices={"smu": FakeSMU()},
            extra_workers={"smu": smu_worker},
        )
    finally:
        smu_worker.stop()

    assert order[0] == "smu_off", order
    assert "outlet_off" in order


def test_a_failed_cut_is_reported_as_failed_not_safe(pdu, worker):
    """The outcome an operator acts on. A lying device must not produce SAFE."""
    sim = pdu._benchctrl_sim
    sim.ignore_switches = True
    governor = SafetyGovernor(safe_state_timeout_s=SAFE_STATE_TIMEOUT_S)

    smu_worker = DeviceWorker("smu").start()
    try:
        class FakeSMU:
            def set_output(self, on):
                pass

        governor.observe_call("smu", "set_output", (True,), {})
        outcomes = _trip(
            governor,
            pdu,
            worker,
            extra_devices={"smu": FakeSMU()},
            extra_workers={"smu": smu_worker},
        )
    finally:
        smu_worker.stop()

    assert outcomes[KEY] is TripOutcome.FAILED


def test_the_cut_gets_a_bigger_budget_than_an_instrument_disarm(pdu):
    """Not tuning — a correctness requirement.

    An outlet honours its configured `td_off` (3 s as shipped), so the half
    second an instrument gets is *physically* impossible for a contactor.
    Reusing it would make every trip time out and escalate to a transport
    reset, reconnecting the link mid-cut and reporting RECOVERED at best.
    """
    from benchctrl.agent.safety import _panic_cut_for

    _, budget = _panic_cut_for(pdu)
    assert budget > SAFE_STATE_TIMEOUT_S * 10
    # And it scales with the outlet count, since each one costs a round trip.
    assert budget > PANIC_CUT_CONFIRM_S


def test_a_trip_does_not_reset_the_pdu_transport(pdu, worker):
    """Regression guard on a name collision.

    The PDU's `_transport` attribute is the *string* `"serial"` — its byte pipe
    is `_link`. `_reset_and_retry` duck-types on `_transport`, so checking only
    `is not None` would call `.close()` on a str; the AttributeError would then
    be caught and logged as "transport reset failed", hiding the fact that the
    retry never ran at all.
    """
    sim = pdu._benchctrl_sim
    sim.ignore_switches = True
    governor = SafetyGovernor()

    smu_worker = DeviceWorker("smu").start()
    try:
        class FakeSMU:
            def set_output(self, on):
                pass

        governor.observe_call("smu", "set_output", (True,), {})
        _trip(
            governor,
            pdu,
            worker,
            extra_devices={"smu": FakeSMU()},
            extra_workers={"smu": smu_worker},
        )
    finally:
        smu_worker.stop()

    # Still usable afterwards: the link was never torn down.
    assert pdu.is_open
    assert isinstance(pdu.outlet_state(4), bool)


def test_the_trip_event_names_the_panic_outlet_device(pdu, worker):
    """The dashboard and the run bundle both read these events. A mains cut
    that is not in the record is a gap in the audit trail, and the PDU is not
    in the `devices` list because it is not armed."""
    events: list[dict] = []
    governor = SafetyGovernor(on_event=events.append)

    smu_worker = DeviceWorker("smu").start()
    try:
        class FakeSMU:
            def set_output(self, on):
                pass

        governor.observe_call("smu", "set_output", (True,), {})
        _trip(
            governor,
            pdu,
            worker,
            extra_devices={"smu": FakeSMU()},
            extra_workers={"smu": smu_worker},
        )
    finally:
        smu_worker.stop()

    trips = [e for e in events if e["kind"] == "safety_trip"]
    assert trips, events
    assert trips[0]["devices"] == ["smu"]
    assert trips[0]["panic_outlet_devices"] == [KEY]


def test_panic_outlets_cannot_exceed_allowed_outlets():
    """The invariant that makes the whole mechanism safe to enable: the
    governor can never cut an outlet the driver itself may not touch."""
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ValueError
    from benchctrl.sim.factories import make_pdu41002

    with pytest.raises(PDU41002ValueError, match="panic_outlets"):
        make_pdu41002(allowed_outlets=(2,), panic_outlets=(2, 3))

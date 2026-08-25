"""Mains switching inside an unattended run.

A run that power-cycles a DUT is the first time benchctrl's *declarative* layer
reaches a mains contactor, and it inherits a specific failure mode from the
device: ``oltctrl`` acknowledges nothing, so a switch that did not happen is
byte-identical to one that did. Layered on top of that, a run is unattended —
nobody is watching to notice that the DUT never rebooted.

So the theme of this file is that **a switch that did not happen must never be
reported as a run that went fine**. Three separate things could quietly produce
exactly that, and each gets its own section:

1. the spec silently dropping outlet fields (or re-hashing every archived spec
   by adding them),
2. the engine skipping the switch because no PDU was attached,
3. the engine measuring a DUT that has not finished booting.

Assertions are on the simulator's outlet state and on the persisted run record,
never on the absence of an exception.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.runs import store as store_mod
from benchctrl.agent.runs.engine import RunEngine
from benchctrl.agent.runs.spec import (
    DEFAULT_OUTLET_SETTLE_S,
    Phase,
    RunSpec,
    Safety,
    Sampling,
)
from benchctrl.exceptions import BenchValueError
from benchctrl.sim import SimulatedOtiiArc

#: The sha256 of the canonical spec below, captured **before** any outlet field
#: existed on `Safety` or `Phase`. Hard-coded on purpose: `RunSpec.sha256` is
#: what ties an archived result bundle to the spec that produced it, and a new
#: key emitted unconditionally would re-hash every spec ever archived — for runs
#: that have nothing to do with mains. A computed comparison could not catch
#: that, because both sides would move together.
PRE_OUTLET_SHA256 = "ae4226b066a195f32ca3fc677a77618e8b4835dce0245fa97cb2c06bd93d867a"


def _spec(**overrides) -> RunSpec:
    base = dict(
        name="hash-pin",
        device="otii_arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv", "mc"), chunk_s=60, metric_period_s=0.05),
        phases=(
            Phase(
                name="soak",
                mode="cv",
                setpoints={"voltage_V": 3.0, "current_limit_A": 0.1},
                duration_s=1.0,
            ),
        ),
    )
    base.update(overrides)
    return RunSpec(**base)


def _cycle_spec(*, settle_s=None, outlet=3, allowed=(3,), **overrides) -> RunSpec:
    """A two-phase power cycle: cut outlet, restore it."""
    off = {"outlets": {outlet: False}}
    on = {"outlets": {outlet: True}, "voltage_V": 3.0}
    kwargs = {} if settle_s is None else {"settle_s": settle_s}
    base = dict(
        name="power-cycle",
        device="otii_arc",
        safety=Safety(
            max_voltage_V=4.0,
            max_current_A=0.5,
            max_duration_s=600,
            allowed_outlets=allowed,
        ),
        sampling=Sampling(
            channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False
        ),
        phases=(
            Phase(name="cut", mode="idle", setpoints=off, duration_s=1.0, **kwargs),
            Phase(name="boot", mode="cv", setpoints=on, duration_s=1.0, **kwargs),
        ),
    )
    base.update(overrides)
    return RunSpec(**base)


@pytest.fixture()
def device():
    from benchctrl.drivers.otii_arc import OtiiArc

    sim = SimulatedOtiiArc()
    sim.start()
    smu = OtiiArc.open(sim.port)
    smu._sim = sim
    yield smu
    smu.close()
    sim.close()


@pytest.fixture()
def pdu():
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3, 4))
    try:
        yield driver
    finally:
        driver.close()


def _run(spec, device, tmp_path, clock_scale=0.02, **kwargs):
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=clock_scale, **kwargs)
    engine.start()
    assert engine.join(timeout=90), "run did not finish"
    return engine


def _outlet_events(engine):
    return [e for e in engine.store.events_since(0) if e["kind"] == "run_outlet"]


# ---------------------------------------------------------------------------
# The spec surface, and the hash
# ---------------------------------------------------------------------------


def test_a_spec_with_no_outlet_fields_hashes_exactly_as_before():
    """The regression this whole conditional-emission dance exists to prevent.

    If this fails, every result bundle archived before the PDU landed can no
    longer be tied to its spec — the hash in the manifest stops matching the
    spec that produced it, and there is no way to tell that from tampering.
    """
    assert _spec().sha256 == PRE_OUTLET_SHA256


def test_the_outlet_fields_do_change_the_hash_when_used():
    """The control for the test above: conditional emission must not mean
    *never* emitted, or two materially different runs would share a hash."""
    plain = _spec()
    with_envelope = _spec(safety=Safety(
        max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600,
        allowed_outlets=(3,),
    ))
    assert with_envelope.sha256 != plain.sha256


def test_a_spec_with_outlets_survives_a_json_round_trip():
    """JSON object keys are strings, so ``{3: True}`` comes back as
    ``{"3": true}``. Unnormalised, the same spec would hash differently after
    being written to disk and read back — which is exactly what a resumed or
    re-run experiment does."""
    spec = _cycle_spec()
    again = RunSpec.from_json(spec.to_json())
    assert again.sha256 == spec.sha256
    assert again.phases[0].setpoints["outlets"] == {3: False}


def test_string_and_int_outlet_keys_converge():
    a = _cycle_spec()
    b = RunSpec(
        name="power-cycle",
        device="otii_arc",
        safety=Safety(
            max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600,
            allowed_outlets=(3,),
        ),
        sampling=Sampling(
            channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False
        ),
        phases=(
            Phase(name="cut", mode="idle",
                  setpoints={"outlets": {"3": False}}, duration_s=1.0),
            Phase(name="boot", mode="cv",
                  setpoints={"outlets": {"3": True}, "voltage_V": 3.0},
                  duration_s=1.0),
        ),
    )
    assert b.sha256 == a.sha256


def test_allowed_outlets_normalises_order_and_duplicates():
    """``(3, 2, 3)`` and ``(2, 3)`` are the same envelope and must hash alike,
    for the same reason `_as_float` exists."""
    a = Safety(allowed_outlets=(3, 2, 3))
    assert a.allowed_outlets == (2, 3)
    assert a.to_dict()["allowed_outlets"] == [2, 3]


def test_an_outlet_outside_the_envelope_is_refused():
    with pytest.raises(BenchValueError, match="safety.allowed_outlets"):
        _cycle_spec(outlet=5, allowed=(3,))


def test_switching_off_is_checked_too():
    """The obvious shortcut — "you may always turn things *off*" — is wrong.
    De-powering a DUT mid-experiment is a state change, and an outlet nobody
    listed may be feeding something nobody wants cut."""
    with pytest.raises(BenchValueError, match="safety.allowed_outlets"):
        RunSpec(
            name="cut-only",
            safety=Safety(allowed_outlets=(2,)),
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": {7: False}}, duration_s=1),),
        )


def test_an_empty_envelope_refuses_every_outlet():
    """Empty is the default and it means *none*, not *unrestricted*. Read the
    other way round, a run that never mentioned outlets would inherit the
    ability to cut any of them."""
    assert Safety().allowed_outlets == ()
    with pytest.raises(BenchValueError, match="safety.allowed_outlets is \\[\\]"):
        RunSpec(
            name="no-envelope",
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": {3: True}}, duration_s=1),),
        )


@pytest.mark.parametrize("aggregate", ["all", "b1", "b2"])
def test_aggregate_outlet_targets_are_inexpressible(aggregate):
    """``oltctrl index all act off`` is one line that de-powers the whole bench.
    The driver has no signature that accepts it; the spec must not be a way back
    in."""
    with pytest.raises(BenchValueError, match="not a number"):
        RunSpec(
            name="agg",
            safety=Safety(allowed_outlets=(1, 2, 3)),
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": {aggregate: False}}, duration_s=1),),
        )


def test_a_bool_outlet_index_is_refused_before_int():
    """``True`` is an ``int`` in Python and would silently become outlet 1 — a
    spec that meant "yes, outlets" authorising a cut to hardware nobody named.
    The driver rejects bools before ints for this reason; so does the spec."""
    with pytest.raises(BenchValueError, match="bool"):
        RunSpec(
            name="boolidx",
            safety=Safety(allowed_outlets=(1,)),
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": {True: False}}, duration_s=1),),
        )
    with pytest.raises(BenchValueError, match="plain\\s+ints|plain ints"):
        Safety(allowed_outlets=(True,))


def test_a_non_bool_outlet_state_is_refused():
    """``{"outlets": {3: "off"}}`` is truthy and would *energise* an outlet the
    spec was trying to cut."""
    with pytest.raises(BenchValueError, match="must be a bool"):
        RunSpec(
            name="strstate",
            safety=Safety(allowed_outlets=(3,)),
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": {3: "off"}}, duration_s=1),),
        )


def test_outlets_must_be_a_mapping():
    with pytest.raises(BenchValueError, match="must be a mapping"):
        RunSpec(
            name="listy",
            safety=Safety(allowed_outlets=(3,)),
            phases=(Phase(name="p", mode="idle",
                          setpoints={"outlets": [3]}, duration_s=1),),
        )


def test_switched_outlets_reports_every_outlet_the_run_touches():
    spec = _cycle_spec(allowed=(2, 3))
    assert spec.switched_outlets == frozenset({3})
    wider = RunSpec(
        name="two",
        safety=Safety(allowed_outlets=(2, 3)),
        phases=(
            Phase(name="a", mode="idle", setpoints={"outlets": {2: False}},
                  duration_s=1),
            Phase(name="b", mode="idle", setpoints={"outlets": {3: False}},
                  duration_s=1),
        ),
    )
    assert wider.switched_outlets == frozenset({2, 3})


# ---------------------------------------------------------------------------
# settle_s
# ---------------------------------------------------------------------------


def test_settle_defaults_to_non_zero_on_a_switching_phase():
    """The default has to be the safe one. A DUT coming up on mains draws inrush
    and then boots; a zero default would make the opening samples of every
    power-cycle phase garbage that *looks* like data."""
    phase = Phase(name="boot", mode="cv", setpoints={"outlets": {3: True}},
                  duration_s=1.0)
    assert phase.settle_s is None
    assert phase.effective_settle_s == DEFAULT_OUTLET_SETTLE_S


def test_an_explicit_zero_settle_is_distinct_from_unset():
    """Which is why `settle_s` is Optional rather than a float defaulting to 0:
    absent means "use the safe default", zero means "I know this DUT needs
    none". Conflating them makes the safe default unreachable."""
    phase = Phase(name="boot", mode="cv", setpoints={"outlets": {3: True}},
                  duration_s=1.0, settle_s=0.0)
    assert phase.effective_settle_s == 0.0
    # ...and it has to survive the round trip, or the decision is lost.
    assert phase.to_dict()["settle_s"] == 0.0
    assert Phase.from_dict(phase.to_dict()).settle_s == 0.0


def test_a_phase_that_switches_nothing_settles_for_nothing():
    plain = Phase(name="soak", mode="cv", setpoints={"voltage_V": 3.0},
                  duration_s=1.0)
    assert plain.effective_settle_s == 0.0
    assert "settle_s" not in plain.to_dict()


def test_settle_on_a_non_switching_phase_is_refused():
    """It would be a setting that silently does nothing: the operator waits for
    a delay that never happens and reads the first samples as settled."""
    with pytest.raises(BenchValueError, match="switches no outlet"):
        Phase(name="soak", mode="cv", setpoints={"voltage_V": 3.0},
              duration_s=1.0, settle_s=5.0)


def test_settle_time_counts_against_the_run_envelope():
    """Dead time is still unattended bench time. A run that power-cycles fifty
    times spends real minutes settling, and excluding that would let a spec
    exceed the envelope it declared for itself."""
    spec = _cycle_spec(settle_s=2.0)
    assert spec.total_duration_s == pytest.approx(2 * (1.0 + 2.0))
    with pytest.raises(BenchValueError, match="max_duration_s"):
        _cycle_spec(settle_s=100.0, safety=Safety(
            max_voltage_V=4.0, max_current_A=0.5, max_duration_s=10,
            allowed_outlets=(3,),
        ))


# ---------------------------------------------------------------------------
# The engine
# ---------------------------------------------------------------------------


def test_a_switching_spec_without_a_pdu_is_refused_before_the_run_starts(
    device, tmp_path
):
    """The quiet failure this guards: a spec that power-cycles a DUT, run with
    no PDU attached, completing successfully having measured a DUT that was
    never rebooted. Refused at construction — before a run directory exists, so
    there is no half-run bundle to misread later."""
    with pytest.raises(BenchValueError, match="no PDU was supplied"):
        RunEngine(_cycle_spec(), device, runs_dir=tmp_path)
    assert not list(tmp_path.iterdir())


def test_a_phase_actually_switches_the_outlet(device, pdu, tmp_path):
    """Asserted on the simulator's contactor state, not on the run status. The
    device acknowledges nothing, so "no exception" is not evidence."""
    sim = pdu._benchctrl_sim
    spec = RunSpec(
        name="cut-only",
        device="otii_arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600,
                      allowed_outlets=(3,)),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(Phase(name="cut", mode="idle",
                      setpoints={"outlets": {3: False}},
                      duration_s=1.0, settle_s=0.0),),
    )
    engine = _run(spec, device, tmp_path, pdu=pdu)
    assert engine.status == store_mod.STATUS_COMPLETE
    assert sim.outlet_state[3] is False
    assert sim.outlet_state[4] is True, "an outlet nobody named moved"


def test_a_full_power_cycle_leaves_the_outlet_energised(device, pdu, tmp_path):
    sim = pdu._benchctrl_sim
    engine = _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu)
    assert engine.status == store_mod.STATUS_COMPLETE
    assert sim.outlet_state[3] is True

    switches = [c for c in sim.command_log if c.startswith("oltctrl")]
    assert switches == ["oltctrl index 3 act off", "oltctrl index 3 act on"]


def test_the_transition_is_in_the_run_record(device, pdu, tmp_path):
    """A mains transition that is not in the timeline is a hole in the audit
    trail of a run that power-cycled a DUT. The payload records the *verified*
    state, not the requested one — so the record says what the contactor did."""
    engine = _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu)
    events = _outlet_events(engine)
    assert [e["data"]["state"] for e in events] == [False, True]
    assert [e["data"]["outlet"] for e in events] == [3, 3]
    assert [e["data"]["requested"] for e in events] == [False, True]
    # Attributed to the phase that did it, or the timeline cannot be read.
    assert [e["phase_idx"] for e in events] == [0, 1]


def test_a_switch_that_never_moves_fails_the_phase(device, pdu, tmp_path):
    """`ignore_switches` is the device that lies: it accepts `oltctrl`, answers
    with the same blank line and prompt, and moves nothing. The run must **not**
    complete. Logging it and carrying on is the tempting choice and it produces
    a bundle full of data describing an experiment that did not happen.
    """
    sim = pdu._benchctrl_sim
    sim.ignore_switches = True
    engine = _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu)

    assert engine.status == store_mod.STATUS_ERRORED
    assert sim.outlet_state[3] is True  # never moved, as arranged
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert "run_error" in kinds
    assert "run_outlet" not in kinds, "a switch that did not happen was recorded"
    stop = engine.store.info()["stop_reason"]
    assert "outlet" in stop.lower()


def test_mains_is_energised_before_the_instrument_setpoint(device, pdu, tmp_path):
    """Ordering, and it is not cosmetic in either direction: a setpoint applied
    while the DUT is de-powered lands nowhere, and energising mains under an
    already-live output is how an inductive kick reaches a DUT."""
    order: list[str] = []
    real_switch = pdu.set_outlet_state

    def note_switch(index, on, **kwargs):
        order.append(f"outlet_{'on' if on else 'off'}")
        return real_switch(index, on, **kwargs)

    real_set_output = device.set_output

    def note_output(on, *a, **kw):
        order.append(f"output_{'on' if on else 'off'}")
        return real_set_output(on, *a, **kw)

    pdu.set_outlet_state = note_switch  # type: ignore[method-assign]
    device.set_output = note_output  # type: ignore[method-assign]

    _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu)

    # Phase 2 ("boot") is the one that both energises and drives.
    assert "outlet_on" in order and "output_on" in order
    assert order.index("outlet_on") < order.index("output_on"), order


def test_the_settle_window_delays_the_first_sample(device, pdu, tmp_path):
    """The failure without it: the opening samples of a power-cycle phase
    describe a supply settling and a DUT booting, and nothing distinguishes them
    from steady-state data afterwards.

    Asserted on the measured *gap* between the switch and the next read, not on
    the ``run_outlet_settle`` event. An engine that emitted the event and never
    waited would be the inert-setting shape all over again: configured, logged,
    and doing nothing. The comparison is one-sided — a missing wait makes the gap
    too small, and nothing about the test makes it spuriously small — so it is
    not the usual flaky timing assertion.
    """
    import time as _time

    settle = 0.5
    marks: list[tuple[float, str]] = []
    real_read = device.read_value

    def note_read(code, *a, **kw):
        marks.append((_time.monotonic(), "read"))
        return real_read(code, *a, **kw)

    real_switch = pdu.set_outlet_state

    def note_switch(index, on, **kwargs):
        result = real_switch(index, on, **kwargs)
        marks.append((_time.monotonic(), "switch"))
        return result

    device.read_value = note_read  # type: ignore[method-assign]
    pdu.set_outlet_state = note_switch  # type: ignore[method-assign]

    engine = _run(_cycle_spec(settle_s=settle), device, tmp_path, clock_scale=1.0,
                  pdu=pdu)

    settles = [
        e for e in engine.store.events_since(0) if e["kind"] == "run_outlet_settle"
    ]
    assert len(settles) == 2, "a switching phase did not settle"
    assert all(s["data"]["settle_s"] == pytest.approx(settle) for s in settles)

    # Both switches, and for each the next read after it.
    gaps = []
    for i, (at, kind) in enumerate(marks):
        if kind != "switch":
            continue
        following = next((t for t, k in marks[i + 1:] if k == "read"), None)
        if following is not None:
            gaps.append(following - at)
    assert len(gaps) == 2, marks
    assert all(gap >= settle * 0.9 for gap in gaps), gaps


def test_an_abort_during_a_settle_window_is_not_delayed(device, pdu, tmp_path):
    """A 30-second DUT boot must not become 30 seconds of an unresponsive abort.
    The settle uses the stop event, not `sleep`."""
    import time

    spec = _cycle_spec(settle_s=30.0, safety=Safety(
        max_voltage_V=4.0, max_current_A=0.5, max_duration_s=100_000,
        allowed_outlets=(3,),
    ))
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=1.0, pdu=pdu)
    engine.start()
    # Wait for the first switch to have gone out, so the abort lands inside the
    # settle rather than before the phase started.
    deadline = time.monotonic() + 30.0
    while not _outlet_events(engine) and time.monotonic() < deadline:
        time.sleep(0.05)
    assert _outlet_events(engine), "first switch never happened"

    engine.abort("operator changed their mind")
    started = time.monotonic()
    assert engine.join(timeout=20), "abort waited out the settle window"
    assert time.monotonic() - started < 15.0
    assert engine.status == store_mod.STATUS_ABORTED


def test_a_run_never_cuts_mains_on_its_own_way_out(device, pdu, tmp_path):
    """`_idle_device` idles the *instrument*. Outlets stay where the last phase
    put them: the operator may well be about to inspect a live DUT, and a run
    that de-powered the bench at every phase boundary would measure boot
    behaviour and nothing else. Cutting on a trip is the governor's separate,
    opt-in `panic_outlets` decision."""
    sim = pdu._benchctrl_sim
    engine = _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu)
    assert engine.status == store_mod.STATUS_COMPLETE
    assert sim.outlet_state[3] is True
    assert device._sim.params[0x09] == 0, "the instrument was left armed"


def test_an_ordinary_run_never_touches_the_pdu(device, pdu, tmp_path):
    """A PDU on the bench must not mean a run that switches mains. Only a spec
    that names outlets does."""
    sim = pdu._benchctrl_sim
    sim.command_log.clear()
    _run(_spec(), device, tmp_path, pdu=pdu)
    assert not [c for c in sim.command_log if c.startswith("oltctrl")]


def test_the_engine_rechecks_the_envelope_before_switching(device, pdu, tmp_path):
    """`_apply_setpoints` re-validates even though the spec was checked at
    construction — it is the last gate before mains moves. Proven by mutating a
    validated spec's setpoints out of envelope, which is what a bug elsewhere in
    the engine would effectively do."""
    sim = pdu._benchctrl_sim
    spec = _cycle_spec(settle_s=0.0)
    # Frozen dataclass, mutable dict: exactly the hole the recheck covers.
    spec.phases[0].setpoints["outlets"] = {4: False}

    engine = _run(spec, device, tmp_path, pdu=pdu)
    assert engine.status == store_mod.STATUS_ERRORED
    assert sim.outlet_state[4] is True
    assert "allowed_outlets" in engine.store.info()["stop_reason"]


def test_outlets_are_switched_on_the_pdus_own_worker(device, pdu, tmp_path):
    """Two devices, two queues. Serialising a 3-second contactor delay behind
    the instrument's worker would stall the measurement path for no reason, and
    the agent needs the PDU's own worker to serialise its single CLI session."""
    from benchctrl.agent.worker import DeviceWorker

    labels: list[str] = []
    pdu_worker = DeviceWorker("cyberpower_pdu41002").start()
    real_submit = pdu_worker.submit

    def note(fn, **kwargs):
        labels.append(kwargs.get("label", ""))
        return real_submit(fn, **kwargs)

    pdu_worker.submit = note  # type: ignore[method-assign]
    try:
        engine = _run(_cycle_spec(settle_s=0.0), device, tmp_path, pdu=pdu,
                      pdu_worker=pdu_worker)
    finally:
        pdu_worker.stop()

    assert engine.status == store_mod.STATUS_COMPLETE
    assert labels == ["outlet_3", "outlet_3"], labels

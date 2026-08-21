"""The run engine: specs, safety envelope, durability, resume.

Runs against a simulated Arc on a compressed clock, so a six-phase
experiment completes in seconds while exercising the same code path a
24-hour battery discharge would.
"""

from __future__ import annotations

import json
import time

import pytest

from benchctrl.agent.runs import store as store_mod
from benchctrl.agent.runs.engine import RunEngine, RunManager
from benchctrl.agent.runs.rules import ConditionState, RuleEngine
from benchctrl.agent.runs.spec import (
    Condition,
    LLMConfig,
    Phase,
    RunSpec,
    Safety,
    Sampling,
)
from benchctrl.exceptions import BenchValueError
from benchctrl.sim import SimulatedOtiiArc


# --------------------------------------------------------------------------
# Spec validation — the envelope is declared before anything is energised
# --------------------------------------------------------------------------


def _spec(**overrides) -> RunSpec:
    base = dict(
        name="test-run",
        device="otii_arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv", "mc"), chunk_s=60, metric_period_s=0.05),
        phases=(
            Phase(name="soak", mode="cv", setpoints={"voltage_V": 3.0,
                  "current_limit_A": 0.1}, duration_s=1.0),
        ),
    )
    base.update(overrides)
    return RunSpec(**base)


def test_spec_round_trips_and_hashes():
    spec = _spec()
    again = RunSpec.from_json(spec.to_json())
    assert again.sha256 == spec.sha256
    assert again.name == spec.name


def test_spec_hash_changes_with_content():
    a = _spec()
    b = _spec(name="different")
    assert a.sha256 != b.sha256


def test_phase_setpoints_must_respect_the_envelope():
    """A phase cannot ask for more than the run declared it was allowed."""
    with pytest.raises(BenchValueError, match="max_voltage_V"):
        _spec(phases=(Phase(name="p", mode="cv",
                            setpoints={"voltage_V": 99.0}, duration_s=1),))
    with pytest.raises(BenchValueError, match="max_current_A"):
        _spec(phases=(Phase(name="p", mode="cv",
                            setpoints={"current_limit_A": 9.0}, duration_s=1),))


def test_phase_without_end_is_rejected():
    """A phase with neither duration nor exit condition runs forever."""
    with pytest.raises(BenchValueError, match="neither a duration nor an exit"):
        Phase(name="forever", mode="cv", duration_s=0)


def test_duplicate_phase_names_rejected():
    with pytest.raises(BenchValueError, match="unique"):
        _spec(phases=(Phase(name="a", duration_s=1), Phase(name="a", duration_s=1)))


def test_total_duration_must_fit_the_envelope():
    with pytest.raises(BenchValueError, match="max_duration_s"):
        _spec(
            safety=Safety(max_duration_s=5),
            phases=(Phase(name="long", duration_s=100),),
        )


def test_oversized_chunk_is_rejected():
    """Chunks are held in RAM; an hour at full rate would OOM the board."""
    with pytest.raises(BenchValueError, match="too large"):
        Sampling(chunk_s=3600)


def test_condition_needs_exactly_one_source():
    with pytest.raises(BenchValueError):
        Condition(channel="mv", metric="soc")
    with pytest.raises(BenchValueError):
        Condition()


def test_emulator_phase_never_records():
    """A-1: the emulator loop and the reader thread deadlock together."""
    phase = Phase(
        name="emu", mode="emulator", emulator={"profile": "CR2032.json"}, duration_s=1
    )
    assert phase.records is False
    assert Phase(name="cv", mode="cv", duration_s=1).records is True


def test_llm_prompt_budget_is_bounded():
    with pytest.raises(BenchValueError, match="max_prompt_tokens"):
        LLMConfig(max_prompt_tokens=100_000)
    with pytest.raises(BenchValueError, match="min_interval_s"):
        LLMConfig(min_interval_s=1)


# --------------------------------------------------------------------------
# Dwell semantics
# --------------------------------------------------------------------------


def test_condition_requires_sustained_breach():
    """One noisy sample must not abort a 24-hour run."""
    state = ConditionState(Condition(channel="mc", op=">", value=1.0, for_s=1.0))
    assert not state.update(2.0, now=0.0)  # breach starts
    assert not state.update(2.0, now=0.5)  # not long enough
    assert state.update(2.0, now=1.1)  # sustained -> fires
    assert not state.update(2.0, now=2.0)  # edge-triggered, fires once


def test_condition_resets_when_the_breach_clears():
    state = ConditionState(Condition(channel="mc", op=">", value=1.0, for_s=1.0))
    state.update(2.0, now=0.0)
    state.update(0.0, now=0.5)  # back in range — clock restarts
    assert not state.update(2.0, now=1.2)  # dwell restarts here, not at 0.0
    assert state.update(2.0, now=2.3)


def test_zero_dwell_fires_immediately():
    state = ConditionState(Condition(channel="mv", op="<", value=1.0))
    assert state.update(0.5, now=0.0)


def test_missing_value_does_not_fire():
    state = ConditionState(Condition(channel="mv", op="<", value=1.0, for_s=0))
    assert not state.update(None, now=0.0)


@pytest.mark.parametrize(
    "op,value,sample,expected",
    [("<", 1.0, 0.5, True), ("<=", 1.0, 1.0, True), (">", 1.0, 2.0, True),
     (">=", 1.0, 1.0, True), ("==", 1.0, 1.0, True), ("!=", 1.0, 2.0, True),
     ("<", 1.0, 2.0, False), (">", 1.0, 0.5, False)],
)
def test_operators(op, value, sample, expected):
    assert Condition(channel="x", op=op, value=value).matches(sample) is expected


# --------------------------------------------------------------------------
# Execution
# --------------------------------------------------------------------------


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


def _run(spec, device, tmp_path, clock_scale=0.02, **kwargs):
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=clock_scale, **kwargs)
    engine.start()
    assert engine.join(timeout=60), "run did not finish"
    return engine


def test_multi_phase_run_completes(device, tmp_path):
    spec = _spec(
        phases=tuple(
            Phase(name=f"p{i}", mode="cv",
                  setpoints={"voltage_V": 1.0 + i * 0.5, "current_limit_A": 0.1},
                  duration_s=5.0)
            for i in range(6)
        )
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_COMPLETE

    phases = engine.store.phases()
    assert len(phases) == 6
    assert all(p["status"] == store_mod.STATUS_COMPLETE for p in phases)

    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert kinds[0] == "run_start"
    assert kinds[-1] == "run_end"
    assert kinds.count("phase_start") == 6
    assert kinds.count("phase_end") == 6


def test_run_start_declares_the_devices_the_run_will_drive(device, tmp_path):
    """``run_start`` names its cast up front, so a panel can enrol them.

    This is the agent half of a cross-process contract, and it is asserted here
    rather than only in the dashboard's tests on purpose: a display cannot learn
    which instruments a run owns by watching traffic, because a device only
    reveals its involvement by being *called*. A supply set once at phase entry
    and held through a ten-minute dwell is in use for the whole of it while
    looking idle for all but 200 ms.

    The declared key must equal ``spec.device`` — the name the registry, the
    governor, and the panel's own rails are all keyed by. A run declaring its
    device under any other spelling would enrol an instrument nobody can see.
    """
    engine = _run(_spec(), device, tmp_path)
    start = next(e for e in engine.store.events_since(0) if e["kind"] == "run_start")

    declared = start["data"]["devices"]
    # A list even though a run currently drives exactly one device: the shape has
    # to survive the first run that coordinates a supply and a load, and a bare
    # string would be silently iterable as characters.
    assert isinstance(declared, list)
    assert declared == [engine.spec.device]


def test_run_start_declares_whichever_device_the_spec_names(device, tmp_path):
    """The declaration follows the spec rather than being a fixed string.

    Without this, a hard-coded ``["otii_arc"]`` would satisfy every other
    assertion in this file — the simulated bench device is an Arc — while
    enrolling the wrong instrument on every other bench.
    """
    engine = _run(_spec(device="rigol_dp2031"), device, tmp_path)
    start = next(e for e in engine.store.events_since(0) if e["kind"] == "run_start")
    assert start["data"]["devices"] == ["rigol_dp2031"]


def test_run_leaves_the_device_idle(device, tmp_path):
    _run(_spec(), device, tmp_path)
    assert device._sim.params[0x09] == 0, "output left enabled after the run"


def test_event_sequence_is_contiguous(device, tmp_path):
    """A reconnecting client uses since_seq — gaps would lose events."""
    engine = _run(_spec(), device, tmp_path)
    seqs = [e["seq"] for e in engine.store.events_since(0)]
    assert seqs == list(range(1, len(seqs) + 1))


def test_events_since_replays_exactly_the_gap(device, tmp_path):
    engine = _run(_spec(), device, tmp_path)
    everything = engine.store.events_since(0)
    cut = len(everything) // 2
    resumed = engine.store.events_since(everything[cut - 1]["seq"])
    assert [e["seq"] for e in resumed] == [e["seq"] for e in everything[cut:]]


def test_safety_envelope_stops_the_run(device, tmp_path):
    """A sustained breach must stop the run and disarm the device."""
    device._sim.waveforms["mv"] = _Fixed(9.0)
    spec = _spec(
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        phases=(Phase(name="overvolt", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=30.0),),
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_SAFE_STOPPED
    assert "max_voltage_V" in engine.store.info()["stop_reason"]
    assert device._sim.params[0x09] == 0


def test_abort_if_condition_stops_the_run(device, tmp_path):
    device._sim.waveforms["mv"] = _Fixed(1.0)
    spec = _spec(
        safety=Safety(
            max_voltage_V=4.0,
            max_current_A=0.5,
            max_duration_s=600,
            abort_if=(Condition(channel="mv", op="<", value=2.0,
                                reason="DUT collapsed"),),
        ),
        phases=(Phase(name="hold", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=30.0),),
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_SAFE_STOPPED
    assert "DUT collapsed" in engine.store.info()["stop_reason"]


def test_exit_condition_ends_a_phase_early(device, tmp_path):
    device._sim.waveforms["mc"] = _Fixed(0.0)
    spec = _spec(
        phases=(
            Phase(name="wait_for_sleep", mode="cv",
                  setpoints={"voltage_V": 3.0}, duration_s=600.0,
                  exit=(Condition(channel="mc", op="<", value=0.001,
                                  reason="DUT slept"),)),
        )
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_COMPLETE
    assert engine.store.phases()[0]["exit_reason"] == "DUT slept"


def test_rules_emit_events_without_stopping_the_run(device, tmp_path):
    from benchctrl.agent.runs.spec import Rule

    device._sim.waveforms["mc"] = _Fixed(0.2)
    spec = _spec(
        # record=False so metrics come from read_value rather than a
        # recording buffer, which only fills every ~0.5 s (A-4).
        sampling=Sampling(channels=("mc",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        rules=(
            Rule(when=Condition(channel="mc", op=">", value=0.05),
                 kind="rule_fired", severity="warn", text="sustained draw"),
        ),
        phases=(Phase(name="watch", mode="cv",
                      setpoints={"voltage_V": 3.0, "current_limit_A": 0.4},
                      duration_s=3.0),),
    )
    engine = _run(spec, device, tmp_path, clock_scale=1.0)
    assert engine.status == store_mod.STATUS_COMPLETE
    fired = [e for e in engine.store.events_since(0) if e["kind"] == "rule_fired"]
    assert fired, "rule never fired"
    assert fired[0]["data"]["text"] == "sustained draw"


def test_chunks_are_written_and_verifiable(device, tmp_path):
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=10, metric_period_s=0.05),
        phases=(Phase(name="capture", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=60.0),),
    )
    engine = _run(spec, device, tmp_path)
    chunks = engine.store.chunks()
    assert chunks, "no chunks written"

    import hashlib
    from pathlib import Path

    from benchctrl.recording import Recording

    for chunk in chunks:
        data = Path(chunk["path"]).read_bytes()
        assert hashlib.sha256(data).hexdigest() == chunk["sha256"]
        assert Recording.from_bytes(data) is not None


def test_artifact_bundle_is_self_describing(device, tmp_path):
    engine = _run(_spec(), device, tmp_path)
    run_dir = engine.store.run_dir
    assert (run_dir / "spec.json").is_file()
    assert (run_dir / "run.db").is_file()
    assert (run_dir / "events.ndjson").is_file()
    assert (run_dir / "manifest.json").is_file()

    manifest = json.loads((run_dir / "manifest.json").read_text())
    assert manifest["run"]["status"] == store_mod.STATUS_COMPLETE
    assert manifest["run"]["spec_sha256"] == engine.spec.sha256


def test_ndjson_mirror_matches_the_database(device, tmp_path):
    """The mirror exists so the narrative survives a corrupted database."""
    engine = _run(_spec(), device, tmp_path)
    lines = [
        json.loads(line)
        for line in engine.store.events_path.read_text().splitlines()
        if line.strip()
    ]
    db_events = engine.store.events_since(0)
    assert [e["seq"] for e in lines] == [e["seq"] for e in db_events]
    assert [e["kind"] for e in lines] == [e["kind"] for e in db_events]


def test_abort_stops_a_running_run(device, tmp_path):
    spec = _spec(
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=100_000),
        phases=(Phase(name="long", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=10_000.0),),
    )
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=0.02)
    engine.start()
    time.sleep(0.5)
    engine.abort("operator changed their mind")
    assert engine.join(timeout=30)
    assert engine.status == store_mod.STATUS_ABORTED
    assert device._sim.params[0x09] == 0


def test_metrics_are_recorded_per_channel(device, tmp_path):
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(Phase(name="measure", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=2.0),),
    )
    engine = _run(spec, device, tmp_path, clock_scale=1.0)
    window = engine.store.metric_window("mv", 3600)
    assert window, "no metrics recorded"
    assert all("mean" in row for row in window)


# --------------------------------------------------------------------------
# Sequence stages — the bench reports where the run is
# --------------------------------------------------------------------------
#
# The panel's "Test Sequence" row used to be inferred from a run's coarse status
# field: every ``running`` run mapped onto one node, so two of the five could
# never light and the lit one claimed "analysing" from the first setpoint. These
# tests pin the replacement — the engine emitting real transitions — because a
# sequence that is merely plausible is indistinguishable from one that is true.


def _stages(engine) -> list[str]:
    """The stage names this run announced, in order."""
    return [
        e["data"]["stage"]
        for e in engine.store.events_since(0)
        if e["kind"] == "run_stage"
    ]


def test_a_run_walks_INIT_through_its_phases_to_DONE(device, tmp_path):
    """The whole sequence, on a run whose phases span two stages.

    ``record=False`` keeps chunks out of it so ANALYZE — which reports the
    manifest work, not a phase — has its own test rather than muddying this one.
    INIT has to arrive before any phase because it means "the engine is up and
    nothing is energised yet", and DONE has to be last because it means the
    opposite.
    """
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(
            Phase(name="settle", mode="idle", duration_s=1.0),
            Phase(name="soak", mode="cv",
                  setpoints={"voltage_V": 3.0}, duration_s=1.0),
        ),
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_COMPLETE
    assert _stages(engine) == ["INIT", "PREPARE", "EXECUTE", "DONE"]


def test_the_first_stage_lands_between_run_start_and_the_first_phase(device, tmp_path):
    """INIT is ordered against the events either side of it, not just present.

    After ``run_start``, so a consumer learns the run exists before it hears where
    the run is — a stage event for an unknown run is unroutable. Before
    ``phase_start``, because a sequence whose starting node arrives after the run
    has already entered a phase has nothing truthful to draw in between.
    """
    engine = _run(_spec(), device, tmp_path)
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert kinds.index("run_start") < kinds.index("run_stage")
    assert kinds.index("run_stage") < kinds.index("phase_start")


def test_consecutive_phases_sharing_a_stage_announce_it_once(device, tmp_path):
    """Every ``run_stage`` event is a real transition, never a heartbeat.

    Six ``cv`` phases all derive EXECUTE. Re-announcing it per phase would leave a
    consumer unable to tell "the run advanced" from "the run is still going",
    which is the distinction the whole event exists to carry — and would make a
    dropped transition undetectable in a stream of repeats.
    """
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=tuple(
            Phase(name=f"p{i}", mode="cv", setpoints={"voltage_V": 1.0 + i * 0.5},
                  duration_s=1.0)
            for i in range(6)
        ),
    )
    engine = _run(spec, device, tmp_path)
    assert _stages(engine) == ["INIT", "EXECUTE", "DONE"]


def test_a_trailing_settle_phase_does_not_walk_the_sequence_back(device, tmp_path):
    """``[idle, cv, idle]`` must not return to PREPARE — a real regression.

    The third phase derives PREPARE from its mode, and emitting it would un-light
    every node the run had already been through, reading as "the test restarted"
    on a run that is simply cooling down. A trailing settle is part of the
    measurement body as far as the sequence is concerned.
    """
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(
            Phase(name="pre", mode="idle", duration_s=1.0),
            Phase(name="measure", mode="cv",
                  setpoints={"voltage_V": 3.0}, duration_s=1.0),
            Phase(name="post", mode="idle", duration_s=1.0),
        ),
    )
    engine = _run(spec, device, tmp_path)
    assert _stages(engine) == ["INIT", "PREPARE", "EXECUTE", "DONE"]
    assert engine.store.phases()[2]["status"] == store_mod.STATUS_COMPLETE


def test_each_transition_names_the_stage_it_came_from(device, tmp_path):
    """``from`` makes a dropped transition detectable.

    With only ``to``, a consumer that missed an event cannot tell it missed one —
    a jump straight to DONE looks identical to a run that never had a middle. The
    chain is asserted end to end, and the first ``from`` is empty because nothing
    preceded INIT. ``index`` is checked against ``STAGES`` so a consumer can order
    stages without hardcoding the vocabulary.
    """
    from benchctrl.agent.runs.spec import STAGES

    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(
            Phase(name="settle", mode="idle", duration_s=1.0),
            Phase(name="soak", mode="cv",
                  setpoints={"voltage_V": 3.0}, duration_s=1.0),
        ),
    )
    engine = _run(spec, device, tmp_path)
    events = [
        e["data"] for e in engine.store.events_since(0) if e["kind"] == "run_stage"
    ]
    assert [d["from"] for d in events] == [""] + [d["stage"] for d in events[:-1]]
    assert [d["index"] for d in events] == [STAGES.index(d["stage"]) for d in events]


def test_analysis_is_reported_only_when_there_was_data_to_hash(device, tmp_path):
    """ANALYZE reports the manifest work, which is real only if chunks exist.

    Hashing every recorded chunk is the slowest thing the engine does on a long
    run and the one part where it works on the *data* rather than the DUT. On a
    run that recorded nothing there is no such work, and lighting a node for a few
    microseconds of bookkeeping would be the same decoration the old renderer was.
    Both halves are asserted together: a build that always emitted it, and one
    that never did, each satisfy exactly one of them.
    """
    recording = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=10, metric_period_s=0.05),
        phases=(Phase(name="capture", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=60.0),),
    )
    engine = _run(recording, device, tmp_path)
    assert engine.store.chunks(), "fixture wrote no chunks"
    assert _stages(engine) == ["INIT", "EXECUTE", "ANALYZE", "DONE"]

    dry = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(Phase(name="watch", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=1.0),),
    )
    engine = _run(dry, device, tmp_path)
    assert not engine.store.chunks()
    assert "ANALYZE" not in _stages(engine)


def test_the_manifest_is_written_while_ANALYZE_is_on_the_glass(device, tmp_path):
    """The stage has to precede the work it describes, not follow it.

    Announced after the hashing finished, the node would light for zero duration
    on exactly the runs where the wait is longest — an operator watching a bench
    grind through an hour of chunks would see nothing happening. The manifest's
    own ``event_count`` is the proof: it counts events at the moment it is
    written, so it includes the ANALYZE transition and excludes only DONE and
    ``run_end``, which are emitted after it by design.
    """
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=10, metric_period_s=0.05),
        phases=(Phase(name="capture", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=60.0),),
    )
    engine = _run(spec, device, tmp_path)
    manifest = json.loads((engine.store.run_dir / "manifest.json").read_text())
    total = len(engine.store.events_since(0))
    assert _stages(engine)[-2:] == ["ANALYZE", "DONE"]
    assert manifest["event_count"] == total - 2


def test_a_store_that_cannot_count_chunks_still_reports_DONE(device, tmp_path):
    """Losing a node is cosmetic; losing DONE strands the display mid-flow.

    A run aborted hard can reach ``_finish`` with a store that will not answer.
    The degradation has to be one-way: no ANALYZE, but DONE regardless, because a
    finished run stuck showing EXECUTE reads as a test still in progress on a
    bench nobody is watching.
    """
    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=10, metric_period_s=0.05),
        phases=(Phase(name="capture", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=60.0),),
    )
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=0.02)

    def _broken() -> int:
        raise RuntimeError("database is closed")

    engine.store.next_chunk_index = _broken
    engine.start()
    assert engine.join(timeout=60), "run did not finish"
    stages = _stages(engine)
    assert "ANALYZE" not in stages
    assert stages[-1] == "DONE"
    assert [e["kind"] for e in engine.store.events_since(0)][-1] == "run_end"


def test_DONE_is_reported_on_an_aborted_run(device, tmp_path):
    """An operator abort is a terminal path, so the sequence must terminate.

    Without this the display would hold EXECUTE forever on a run that stopped
    minutes ago — the failure mode is not a missing node but a stale one, which
    reads as truth.
    """
    spec = _spec(
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=100_000),
        phases=(Phase(name="long", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=10_000.0),),
    )
    engine = RunEngine(spec, device, runs_dir=tmp_path, clock_scale=0.02)
    engine.start()
    time.sleep(0.5)
    engine.abort("operator changed their mind")
    assert engine.join(timeout=30)
    assert engine.status == store_mod.STATUS_ABORTED
    assert _stages(engine)[-1] == "DONE"


def test_DONE_is_reported_on_a_safety_stop(device, tmp_path):
    """The path where a stranded sequence would be most misleading.

    A safe stop is the one outcome where an operator most needs to know the bench
    has stopped: the rail is down and the run is over. The old inference could not
    express that at all, since it only ever mapped ``running``.
    """
    device._sim.waveforms["mv"] = _Fixed(9.0)
    spec = _spec(
        phases=(Phase(name="overvolt", mode="cv",
                      setpoints={"voltage_V": 3.0}, duration_s=30.0),),
    )
    engine = _run(spec, device, tmp_path)
    assert engine.status == store_mod.STATUS_SAFE_STOPPED
    assert _stages(engine)[-1] == "DONE"


def test_DONE_is_reported_when_the_run_raises(device, tmp_path):
    """The error path routes through ``_finish`` too, or it would strand as well.

    Raised from ``_apply_setpoints`` because that is inside the engine's own
    ``try`` and is where a real device fault lands — the last gate before the
    output is energised.
    """
    engine = RunEngine(_spec(), device, runs_dir=tmp_path, clock_scale=0.02)

    def _explode(phase):
        raise RuntimeError("supply refused the setpoint")

    engine._apply_setpoints = _explode
    engine.start()
    assert engine.join(timeout=30)
    assert engine.status == store_mod.STATUS_ERRORED
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert "run_error" in kinds
    assert _stages(engine)[-1] == "DONE"


def test_a_phases_stage_is_announced_before_its_output_is_energised(tmp_path):
    """The stage must be on the glass *while* the rail comes up, not after.

    The window matters because that is when an operator is looking: a supply
    coming up under a display still showing the previous stage is the moment the
    sequence is least trustworthy. Asserted against the first device call of the
    phase rather than ``set_output`` alone, since the current limit and voltage
    are committed before the output is enabled.

    Uses a recording stand-in for the device so device calls and run events land
    on one ordered list — the real driver's ordering is not observable from the
    event log alone.
    """
    timeline: list[str] = []

    class _Recorder:
        """Enough of a supply for ``_apply_setpoints``; records the order."""

        def set_current_limit(self, amps):
            timeline.append("set_current_limit")

        def set_current_limit_enabled(self, on):
            timeline.append("set_current_limit_enabled")

        def set_voltage(self, volts):
            timeline.append("set_voltage")

        def set_power_regulation(self, mode):
            timeline.append("set_power_regulation")

        def set_output(self, on):
            timeline.append(f"set_output={on}")

    spec = _spec(
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.2,
                          record=False),
        phases=(Phase(name="soak", mode="cv",
                      setpoints={"voltage_V": 3.0, "current_limit_A": 0.1},
                      duration_s=1.0),),
    )
    engine = RunEngine(
        spec,
        _Recorder(),
        runs_dir=tmp_path,
        clock_scale=0.02,
        on_event=lambda e: (
            timeline.append(f"stage:{e['data']['stage']}")
            if e["kind"] == "run_stage"
            else None
        ),
    )
    engine.start()
    assert engine.join(timeout=30), "run did not finish"

    first_device_call = next(
        i for i, mark in enumerate(timeline) if mark.startswith("set_")
    )
    assert timeline.index("stage:EXECUTE") < first_device_call


def test_an_unorderable_stage_is_emitted_rather_than_dropped(device, tmp_path):
    """A stage outside the vocabulary must not be silently swallowed.

    A contract test on ``_emit_stage``, not a reachability test: nothing in the
    spec can produce such a stage today, because ``Phase`` validates against
    ``PHASE_STAGES``. It is asserted anyway because the ordering guard is written
    in terms of ``STAGES.index``, which raises rather than returning a sentinel —
    so the *reason* an unknown stage survives is one caught exception, and a
    future stage name added on one side of the wire before the other would
    otherwise vanish. Silently dropping transitions is the exact failure this
    whole change exists to remove; the display already renders an unplaceable
    stage as unplaceable.
    """
    engine = RunEngine(_spec(), device, runs_dir=tmp_path)
    engine._emit_stage("EXECUTE")
    engine._emit_stage("CALIBRATE")
    engine._emit_stage("DONE")

    events = [
        e["data"] for e in engine.store.events_since(0) if e["kind"] == "run_stage"
    ]
    assert [d["stage"] for d in events] == ["EXECUTE", "CALIBRATE", "DONE"]
    # ``-1`` rather than an omitted key: a consumer must be able to tell "cannot
    # be ordered" from "position 0", which would sort it before INIT.
    assert events[1]["index"] == -1
    # And it does not become the floor for what follows: DONE is still emitted.
    assert events[2]["from"] == "CALIBRATE"


# --------------------------------------------------------------------------
# Durability
# --------------------------------------------------------------------------


def test_interrupted_run_is_detected_not_resumed(tmp_path):
    """After a power cut the DUT's state is unknown — a human decides."""
    from benchctrl.agent.runs.store import RunStore, new_run_id

    run_id = new_run_id("interrupted")
    store = RunStore(tmp_path / run_id, run_id)
    store.create(_spec())
    store.set_status(store_mod.STATUS_RUNNING)
    store._conn.execute(
        "UPDATE run SET boot_id=? WHERE run_id=?", ("a-previous-boot", run_id)
    )
    store._conn.commit()
    store.close()

    touched = store_mod.reconcile_interrupted(tmp_path)
    assert run_id in touched

    reopened = RunStore(tmp_path / run_id, run_id)
    assert reopened.info()["status"] == store_mod.STATUS_INTERRUPTED
    kinds = [e["kind"] for e in reopened.events_since(0)]
    assert "run_interrupted" in kinds
    reopened.close()


def test_chunk_numbering_continues_after_resume(tmp_path):
    from benchctrl.agent.runs.store import RunStore, new_run_id
    from benchctrl.drivers.otii_arc.channels import OtiiArcChannel
    from benchctrl.recording import Recording

    run_id = new_run_id("chunked")
    store = RunStore(tmp_path / run_id, run_id)
    store.create(_spec())

    rec = Recording(name="c")
    rec._append(OtiiArcChannel.MAIN_VOLTAGE, 3.3, 1000)
    assert store.write_chunk(rec, 0)["idx"] == 0
    assert store.write_chunk(rec, 0)["idx"] == 1
    store.close()

    reopened = RunStore(tmp_path / run_id, run_id)
    assert reopened.next_chunk_index() == 2
    assert reopened.write_chunk(rec, 1)["idx"] == 2
    reopened.close()


def test_manager_refuses_two_runs_on_one_device(device, tmp_path):
    manager = RunManager(tmp_path)
    spec = _spec(
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=100_000),
        phases=(Phase(name="long", mode="cv",
                      setpoints={"voltage_V": 1.0}, duration_s=10_000.0),),
    )
    first = manager.submit(spec, device, clock_scale=0.02)
    first.start()
    try:
        with pytest.raises(BenchValueError, match="already has run"):
            manager.submit(spec, device)
    finally:
        first.abort("test teardown")
        first.join(timeout=30)


def test_manager_lists_runs(device, tmp_path):
    manager = RunManager(tmp_path)
    engine = manager.submit(_spec(), device, clock_scale=0.02)
    engine.start()
    engine.join(timeout=30)
    listed = manager.list()
    assert any(r["run_id"] == engine.run_id for r in listed)


class _Fixed:
    """A waveform pinned to one value, for driving conditions in tests."""

    def __init__(self, value: float) -> None:
        self.v = value

    def value(self, t: float) -> float:
        return self.v

    def mean_over(self, t0: float, t1: float) -> float:
        return self.v

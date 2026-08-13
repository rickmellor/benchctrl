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

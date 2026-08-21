"""Sequence stages: what a spec may declare, and what the bench derives.

The dashboard's "Test Sequence" row used to be a renderer's guess — it invented
stage names from a run's coarse status field, so two of its five nodes were
unreachable and the third claimed "analysing" from the first setpoint onwards.
The fix moves the vocabulary here, into the spec, so a *test* declares its own
transitions and the bench reports them.

These tests sit in their own file because they are pure spec algebra: no device,
no threads, no compressed clock. The engine half — which transitions actually get
emitted, and in what order — is in ``test_run_engine.py``.
"""

from __future__ import annotations

import pytest

from benchctrl.agent.runs.spec import (
    MODES,
    PHASE_STAGES,
    STAGES,
    Phase,
    RunSpec,
    Safety,
    Sampling,
)
from benchctrl.exceptions import BenchValueError

#: A spec written before stages existed: not one phase carries a ``stage``.
#: Reconstructed here rather than loaded from a fixture so the fields are visible
#: next to the hash they produce.
def _legacy_spec(**phase_overrides) -> RunSpec:
    settle = dict(name="settle", mode="idle", duration_s=2.0)
    settle.update(phase_overrides)
    return RunSpec(
        name="legacy-run",
        device="otii_arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv", "mc"), chunk_s=60, metric_period_s=0.05),
        phases=(
            Phase(**settle),
            Phase(name="soak", mode="cv", setpoints={"voltage_V": 3.0},
                  duration_s=5.0),
        ),
    )


#: The hash of :py:func:`_legacy_spec` as computed by the build *before* stages
#: were added, taken from ``git show HEAD:...spec.py``. Hardcoded on purpose: a
#: hash the test recomputes from the current code can never detect that the
#: current code changed it.
LEGACY_SHA256 = "b508822464867fdf44b0bafa128db707fdcce46abd4a8bad9325fe9943fb057a"


# --------------------------------------------------------------------------
# Derivation — what an existing spec gets for free
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "mode,expected",
    [("idle", "PREPARE"), ("cv", "EXECUTE"), ("cc", "EXECUTE"),
     ("emulator", "EXECUTE")],
)
def test_stage_is_derived_from_what_the_mode_does(mode, expected):
    """An undeclared stage comes from the mode, not from a flat default.

    This is what lets every spec already on disk drive a truthful sequence: none
    of them carry a stage, and if the fallback were a constant ``EXECUTE`` the
    PREPARE node would only ever light for specs written after this release. An
    ``idle`` phase energises nothing, so it is setup or settling — calling it the
    test proper is the same class of lie the old renderer told.
    """
    kwargs = {"emulator": {"profile": "CR2032.json"}} if mode == "emulator" else {}
    phase = Phase(name="p", mode=mode, duration_s=1.0, **kwargs)
    assert phase.sequence_stage == expected


def test_every_declarable_mode_has_its_own_derivation():
    """No mode reaches the sequence by way of the fallback today.

    The fallback exists for the mode added tomorrow, not for one shipping now. If
    a mode in :py:data:`MODES` were missing from the mapping it would silently
    render as EXECUTE, which is exactly how a settling phase gets mislabelled as
    the measurement — invisible, because the display would still look plausible.
    """
    from benchctrl.agent.runs.spec import _STAGE_BY_MODE

    assert set(MODES) == set(_STAGE_BY_MODE)


def test_an_unmapped_mode_still_yields_a_drawable_stage():
    """The fallback must be a real stage, not an empty string.

    Reached by a mode that passed validation but has no mapping — a future mode
    added to ``MODES`` and forgotten here. The panel draws a fixed row of nodes
    and matches on name, so ``""`` would leave a phase with no node lit at all:
    the run would look stalled rather than mislabelled, which is worse.
    """
    phase = Phase(name="p", mode="cv", duration_s=1.0)
    object.__setattr__(phase, "mode", "some-future-mode")
    assert phase.sequence_stage == "EXECUTE"
    assert phase.sequence_stage in PHASE_STAGES


# --------------------------------------------------------------------------
# Declaration — what the test author overrides
# --------------------------------------------------------------------------


def test_a_declared_stage_beats_the_mode_derivation():
    """Only the author knows a ``cv`` phase is settling rather than measuring.

    Both directions are asserted: a declaration that happens to agree with the
    derivation proves nothing, because a ``sequence_stage`` that ignored ``stage``
    entirely would still pass.
    """
    settling = Phase(name="rail", mode="cv", setpoints={"voltage_V": 3.0},
                     duration_s=1.0, stage="PREPARE")
    assert settling.sequence_stage == "PREPARE"

    measuring = Phase(name="dwell", mode="idle", duration_s=1.0, stage="EXECUTE")
    assert measuring.sequence_stage == "EXECUTE"


def test_a_lowercase_declaration_is_normalised():
    """Specs are hand-written JSON, so case is a typo not an error.

    Normalised at construction rather than at comparison so the stored value, the
    serialised value, and the emitted event all carry one spelling — a display
    matching ``"prepare"`` against ``STAGES`` would light nothing.
    """
    phase = Phase(name="p", mode="cv", duration_s=1.0, stage="prepare")
    assert phase.stage == "PREPARE"
    assert phase.sequence_stage == "PREPARE"


def test_a_stage_outside_the_vocabulary_is_rejected():
    """The panel draws a fixed row, so a run cannot invent a sixth node.

    Rejected at construction, before anything is energised, rather than dropped
    at render time: a spec whose stages silently do not draw is harder to
    diagnose than one that refuses to load.
    """
    with pytest.raises(BenchValueError, match="CALIBRATE"):
        Phase(name="p", mode="cv", duration_s=1.0, stage="CALIBRATE")


@pytest.mark.parametrize("bracket", ["INIT", "DONE"])
def test_a_phase_cannot_claim_the_engines_own_brackets(bracket):
    """A phase declaring DONE could light DONE while still driving the output.

    That is the one lie this display must not tell: an operator reading DONE walks
    away from a bench whose rail is live. INIT is refused for the mirror reason —
    it means "nothing is energised yet", which a phase applying setpoints cannot
    honestly claim. Both are in ``STAGES`` and neither is in ``PHASE_STAGES``, and
    this test is what holds those two tuples apart.
    """
    assert bracket in STAGES
    with pytest.raises(BenchValueError, match=bracket):
        Phase(name="p", mode="cv", duration_s=1.0, stage=bracket)


def test_the_stage_vocabulary_is_ordered_and_free_of_duplicates():
    """``STAGES.index`` is the ordering used to reject backwards transitions.

    A duplicated name would give two positions for one stage and make that
    comparison meaningless; it is also what makes the engine's idempotence check
    and its monotonicity check non-overlapping rather than redundant.
    """
    assert len(set(STAGES)) == len(STAGES)
    assert STAGES[0] == "INIT"
    assert STAGES[-1] == "DONE"
    assert set(PHASE_STAGES) < set(STAGES)


# --------------------------------------------------------------------------
# Serialisation — the hash has to survive this feature
# --------------------------------------------------------------------------


def test_an_undeclared_stage_is_not_serialised():
    """A derived stage must never reach ``to_dict``.

    The spec hash ties a result bundle to the spec that produced it. Writing the
    derived value would bake this release's mode mapping into every hash, so
    re-tuning the defaults later would look like every archived spec had been
    edited — and a bundle could no longer be traced to its spec at all.
    """
    spec = _legacy_spec()
    assert all("stage" not in p for p in spec.to_dict()["phases"])


def test_adding_stages_did_not_move_an_existing_specs_hash():
    """The pre-stage hash of a pre-stage spec, still reproducible.

    Asserted against a literal captured from the previous build rather than
    against a freshly computed hash: the point is that *this* code produces what
    *that* code did.
    """
    assert _legacy_spec().sha256 == LEGACY_SHA256


def test_a_declared_stage_is_serialised_and_changes_the_hash():
    """Declaring a stage is an edit to the test, so the hash must move.

    The counterpart to the test above: silence for a derived value, and a
    recorded, hashed difference for an authored one. Without the hash assertion a
    ``to_dict`` that dropped ``stage`` altogether would satisfy both.
    """
    declared = _legacy_spec(stage="EXECUTE")
    phases = declared.to_dict()["phases"]
    assert phases[0]["stage"] == "EXECUTE"
    assert declared.sha256 != LEGACY_SHA256


def test_a_stage_round_trips_through_json():
    """A spec is re-read on the bench, months later, to reproduce a result.

    Both the hash and the field are checked: a ``from_dict`` that dropped
    ``stage`` would still round-trip the hash of a spec that never declared one.
    """
    original = _legacy_spec(stage="prepare")
    again = RunSpec.from_json(original.to_json())
    assert again.phases[0].stage == "PREPARE"
    assert again.phases[0].sequence_stage == "PREPARE"
    assert again.sha256 == original.sha256

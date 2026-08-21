"""Runs, as the panel folds them — and as the *agent* actually spells them.

This file exists because of a defect a single-process test structurally could
not see. :py:class:`~benchctrl.agent.runs.engine.RunEngine` emits ``run_start``
and ``run_end``; the dashboard model folded ``run_started``/``run_finished``,
spellings nothing in the agent has ever sent. So
:py:attr:`~benchctrl.dashboards.state.BenchStatus.running_runs` was empty for
the whole of every real run, while the model's own tests passed — because they
fed it the dashboard's invented vocabulary. A test that both writes and reads
one side of a cross-process contract proves only that it is self-consistent.

The tests below therefore split deliberately into two kinds, and the split is
the point:

- **Vocabulary tests** feed hand-built events. Cheap, fast, and able to isolate
  one property each — but they can only ever assert that the model folds what
  they were told to send.
- **End-to-end tests** run a real engine against a simulated Arc, capture the
  event dicts it *actually* emits through ``on_event``, and feed those into a
  real :py:class:`BenchStatus`. Nothing in them names an event kind or a payload
  key that the agent did not produce, which is the only way this class of bug
  shows up.

Two of the end-to-end assertions are :py:func:`pytest.mark.xfail` with
``strict=True``, because they currently fail against real events. That is not
test scaffolding — it is a live defect, recorded where it will be noticed, and
the strict marker means fixing the source turns the xfail into a failure that
says "delete this marker". See ``test_real_run_end_surfaces_its_outcome`` for
the details.
"""

from __future__ import annotations

import tempfile

import pytest

from benchctrl.agent.runs import store as store_mod
from benchctrl.agent.runs.engine import RunEngine
from benchctrl.agent.runs.spec import Phase, RunSpec, Safety, Sampling
from benchctrl.dashboards.state import (
    IN_FLIGHT_RUN_STATES,
    RUN_EVENT_KINDS,
    RUN_FINISHED_KINDS,
    RUN_STARTED_KINDS,
    BenchStatus,
)
from benchctrl.sim import SimulatedOtiiArc

RUN = "20260101T000000Z-demo-abc123"


def start(run_id=RUN, *, devices=("otii_arc",), **extra):
    """A ``run_start`` shaped the way the *engine* shapes it.

    Built by a helper rather than inline so no test can quietly invent a field:
    every hand-built start in this file goes through here, and the shape here is
    checked against a real engine by ``test_hand_built_start_matches_the_engine``.
    """
    event = {"kind": "run_start", "run_id": run_id}
    if devices is not None:
        event["devices"] = list(devices)
    event.update(extra)
    return event


def end(run_id=RUN, *, status="complete", **extra):
    event = {"kind": "run_end", "run_id": run_id, "status": status}
    event.update(extra)
    return event


def stage(run_id=RUN, *, to="EXECUTE", frm=None, index=2):
    """A ``run_stage`` shaped the way the *engine* shapes it: payload nested.

    ``store.Event.to_dict`` puts every payload field under ``data``, and nothing
    between the engine and the panel flattens it, so this is the shape that
    actually arrives over the wire. Built by a helper for the same reason
    :py:func:`start` is — and pinned to a real engine's own event by
    ``test_hand_built_stage_matches_the_engines_own_run_stage``, so it cannot
    quietly drift into a fiction the way the ``run_started`` spelling did.
    """
    return {
        "kind": "run_stage",
        "run_id": run_id,
        "data": {"stage": to, "from": frm, "index": index},
    }


# --------------------------------------------------------------------------
# Vocabulary: the spelling the agent actually uses
# --------------------------------------------------------------------------


def test_the_agents_real_spelling_drives_the_model():
    """``run_start``/``run_end`` — the pair the engine emits — move the model.

    The regression test for the defect this file was written for. It deliberately
    does not mention ``run_started``/``run_finished``: those were the dashboard's
    own invention, and a test that used them passed while the panel read IDLE
    through every real run on the bench.
    """
    bench = BenchStatus()

    bench.apply_event(start())
    assert bench.running_runs == [RUN], (
        "the engine's own run_start spelling did not put the run in flight"
    )

    bench.apply_event(end())
    assert bench.running_runs == []


def test_the_legacy_spelling_still_folds():
    """``run_started``/``run_finished`` keep working, for replayed stores.

    Not dead weight: run events are persisted by ``store.append_event`` and
    replayed to a reconnecting client through ``run.events``/``since_seq``, so a
    store written by another version of the agent can hand this model either
    vocabulary. Dropping one would silently lose runs from the panel again — the
    same failure, from the other direction.
    """
    bench = BenchStatus()

    bench.apply_event({"kind": "run_started", "run_id": RUN})
    assert bench.running_runs == [RUN]

    bench.apply_event({"kind": "run_finished", "run_id": RUN, "state": "complete"})
    assert bench.running_runs == []


def test_both_spellings_are_declared_as_the_agents_and_the_legacy_one():
    """The constants name the engine's spelling, not only the legacy pair.

    Asserted against the module's constants as well as through behaviour because
    these two sets are the written-down half of the cross-process contract, and
    the engine's spelling is the half that was missing.
    """
    assert "run_start" in RUN_STARTED_KINDS
    assert "run_end" in RUN_FINISHED_KINDS
    assert "run_started" in RUN_STARTED_KINDS
    assert "run_finished" in RUN_FINISHED_KINDS


def test_run_end_surfaces_the_outcome_it_carried():
    """``safe_stopped`` reaches the rail as itself, not as a flat "finished".

    The outcome that matters most: the agent stopped the run because it could not
    keep the bench inside the declared envelope. A panel that rendered that
    identically to a clean completion would hide the one run outcome an operator
    must go and look at. ``run_end`` carries it under ``status``.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(end(status=store_mod.STATUS_SAFE_STOPPED))

    assert bench.runs[RUN].state == "safe_stopped"
    assert bench.running_runs == []


def test_run_finished_reads_the_legacy_state_field():
    """The legacy pair carried its outcome under ``state``. Still read it.

    The mirror of the test above, and the reason ``_apply_run_event`` consults
    both fields: reading only one of them loses the outcome for half the events
    this model can be handed, and loses it *quietly* — the run still leaves the
    in-flight set, so nothing looks wrong except the word on the rail.
    """
    bench = BenchStatus()
    bench.apply_event({"kind": "run_started", "run_id": RUN})
    bench.apply_event(
        {"kind": "run_finished", "run_id": RUN, "state": store_mod.STATUS_SAFE_STOPPED}
    )
    assert bench.runs[RUN].state == "safe_stopped"


def test_a_finish_with_no_outcome_at_all_still_ends_the_run():
    """Neither field present: the run is over, and says so in general terms.

    "finished" is a weaker claim than the two tests above produce, and that is
    correct — it is what is actually known. What must not happen is the run
    staying in flight because its outcome was unreadable.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event({"kind": "run_end", "run_id": RUN})
    assert bench.runs[RUN].state == "finished"
    assert bench.running_runs == []


def test_run_error_leaves_the_run_not_in_flight():
    """A dead run must not pin the bench to "running" forever.

    ``run_error`` is emitted from the engine's own ``except`` block, so it is the
    last thing a crashed run ever says: no ``run_end`` follows it. If this kind
    were not folded, the run would keep whatever state ``run_start`` gave it, and
    :py:attr:`BenchStatus.any_busy` would report a working bench indefinitely —
    the panel asserting the bench is doing something it has stopped doing.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    assert bench.running_runs == [RUN]

    bench.apply_event({"kind": "run_error", "run_id": RUN, "error": "RuntimeError()"})

    assert bench.runs[RUN].state not in IN_FLIGHT_RUN_STATES
    assert bench.running_runs == []
    assert bench.any_busy is False, "a crashed run left the bench reading busy"


def test_run_aborted_leaves_the_run_not_in_flight():
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event({"kind": "run_aborted", "run_id": RUN})
    assert bench.runs[RUN].state not in IN_FLIGHT_RUN_STATES
    assert bench.running_runs == []


def test_run_step_does_not_end_the_run():
    """A progress event moves the step and leaves the run in flight.

    The complement of the two tests above: they check that terminal kinds end a
    run, and this checks that a non-terminal one does not. Without it, a fold
    that treated every run event as terminal would satisfy both of them.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event({"kind": "run_step", "run_id": RUN, "step": "soak", "progress": 0.5})

    assert bench.running_runs == [RUN]
    assert bench.runs[RUN].step == "soak"
    assert bench.runs[RUN].progress == 0.5


def test_an_event_with_no_run_id_is_ignored():
    """Nothing to attribute it to, so it creates no run.

    A run keyed by the empty string would show on the rail as a nameless run in
    flight, and would never end — no later event could match it.
    """
    bench = BenchStatus()
    bench.apply_event({"kind": "run_start", "devices": ["otii_arc"]})
    assert bench.runs == {}
    assert bench.running_runs == []


# --------------------------------------------------------------------------
# Enrollment: which instruments a live run has committed
# --------------------------------------------------------------------------


def test_enrolled_devices_maps_declared_devices_to_the_run():
    bench = BenchStatus()
    bench.apply_event(start(devices=("otii_arc", "rigol_dl3031a")))

    assert bench.runs[RUN].devices == ("otii_arc", "rigol_dl3031a")
    assert bench.enrolled_devices == {"otii_arc": RUN, "rigol_dl3031a": RUN}


def test_enrollment_covers_the_dwell_when_no_call_is_in_flight():
    """Enrollment holds while the run is between calls — the point of it.

    ``busy_devices`` comes from the worker table and means "a call is executing
    right now"; a supply held at a setpoint through a ten-minute dwell is in
    neither that table nor the action stream, and read as idle. Enrollment is the
    claim that survives the gap, so this asserts the state ``busy_devices``
    cannot: nothing busy, and the instrument still committed.
    """
    bench = BenchStatus()
    bench.apply_event(start())

    assert bench.busy_devices == {}
    assert bench.enrolled_devices == {"otii_arc": RUN}


@pytest.mark.parametrize(
    "terminal",
    [
        {"kind": "run_end", "status": "complete"},
        {"kind": "run_end", "status": store_mod.STATUS_SAFE_STOPPED},
        {"kind": "run_finished", "state": "complete"},
        {"kind": "run_error", "error": "boom"},
        {"kind": "run_aborted", "reason": "operator"},
    ],
    ids=["end", "safe_stopped", "legacy_finished", "error", "aborted"],
)
def test_enrollment_is_released_when_the_run_stops(terminal):
    """Every way a run can stop releases its instruments, with no clearing call.

    Enrollment is derived from the in-flight set rather than stored, so its
    lifetime is exactly the run's. That is what keeps a crashed or aborted run
    from pinning an instrument to IN RUN forever — a state nothing would ever
    clear, because a crashed run sends nothing more.

    Parametrized across all five terminal shapes on purpose: an implementation
    that released on a clean ``run_end`` and leaked on ``run_error`` would be the
    dangerous half working and the safe half not.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    assert bench.enrolled_devices == {"otii_arc": RUN}

    bench.apply_event({"run_id": RUN, **terminal})

    assert bench.enrolled_devices == {}, "a stopped run kept its instruments enrolled"


def test_only_the_stopped_runs_devices_are_released():
    """One run ending does not release another run's instruments.

    Guards against the shortcut of clearing the whole enrollment map on any
    terminal event, which would pass every single-run test in this file.
    """
    bench = BenchStatus()
    bench.apply_event(start("run-a", devices=("otii_arc",)))
    bench.apply_event(start("run-b", devices=("rigol_dl3031a",)))
    assert bench.enrolled_devices == {"otii_arc": "run-a", "rigol_dl3031a": "run-b"}

    bench.apply_event(end("run-a"))

    assert bench.enrolled_devices == {"rigol_dl3031a": "run-b"}


def test_a_contested_device_resolves_to_the_lowest_run_id():
    """Two live runs claiming one device answer deterministically.

    ``RunManager.submit`` refuses a second run on a device, so a tie means the
    view is stale rather than the bench being double-booked. A stable answer is
    readable; one that flickered with dict ordering is not.
    """
    bench = BenchStatus()
    bench.apply_event(start("run-b", devices=("otii_arc",)))
    bench.apply_event(start("run-a", devices=("otii_arc",)))

    assert bench.enrolled_devices == {"otii_arc": "run-a"}


def test_a_run_whose_start_was_never_seen_declares_nothing():
    """Mid-run connect: ``devices`` is empty, and empty means "not known".

    A panel that attaches while a run is already going gets its first run event
    somewhere in the middle, so it never saw the declaration. The honest answer
    is to claim nothing — not to render an empty tuple as "this run uses no
    instruments", which is a confident statement about a bench nobody asked, and
    not to guess at the cast from whichever device happens to be called next.

    The run is tracked (so a later ``run_end`` has something to land on) with
    ``state`` left at its ``unknown`` default, which is deliberately *not* in
    :py:data:`IN_FLIGHT_RUN_STATES`: this session has no evidence the run is
    live, only that it existed. Asserted rather than left implicit because it is
    the honest-uncertainty rule, and because an implementation that defaulted an
    unseen run to "running" would pass every other test here.
    """
    bench = BenchStatus()
    bench.apply_event({"kind": "run_step", "run_id": RUN, "step": "soak"})

    assert bench.runs[RUN].devices == ()
    assert bench.enrolled_devices == {}, (
        "a run that never declared its cast must not be rendered as claiming one"
    )
    assert bench.runs[RUN].state == "unknown"
    assert bench.runs[RUN].state not in IN_FLIGHT_RUN_STATES


def test_a_start_with_no_devices_key_leaves_the_declaration_unknown():
    """An older agent sends no ``devices`` at all. Same rule: claim nothing."""
    bench = BenchStatus()
    bench.apply_event(start(devices=None))

    assert bench.running_runs == [RUN]
    assert bench.runs[RUN].devices == ()
    assert bench.enrolled_devices == {}


@pytest.mark.parametrize(
    "declared",
    [
        "otii_arc",
        {"otii_arc": True},
        42,
        None,
    ],
    ids=["bare_string", "dict", "int", "null"],
)
def test_a_devices_payload_that_is_not_a_list_is_ignored(declared):
    """Wrong type: no enrollment, no exception.

    A bare string is the sharp case. It is iterable, so a fold without the list
    guard would enrol eight single-character devices named ``o``, ``t``, ``i``…
    — plausible-looking garbage on the rail rather than an obvious failure.

    This runs on the feed's receive path (``AgentFeed._on_event``), folding an
    event built in another process. Raising here would drop the session and blank
    the panel over one malformed optional field.
    """
    bench = BenchStatus()
    event = start(devices=None)
    event["devices"] = declared
    bench.apply_event(event)

    assert bench.runs[RUN].devices == ()
    assert bench.enrolled_devices == {}


def test_non_string_and_empty_entries_are_filtered_out():
    """A good list with bad entries keeps the good ones and drops the rest.

    Empty strings are dropped with the wrong types because a device keyed by
    ``""`` is not a device: it would occupy a row on the rail that no registry
    entry, no worker, and no status snapshot could ever match, and nothing would
    remove it while the run lived.
    """
    bench = BenchStatus()
    bench.apply_event(
        start(devices=("otii_arc", "", None, 7, {"k": "v"}, "rigol_dl3031a"))
    )

    assert bench.runs[RUN].devices == ("otii_arc", "rigol_dl3031a")
    assert bench.enrolled_devices == {"otii_arc": RUN, "rigol_dl3031a": RUN}


def test_a_malformed_devices_payload_does_not_cost_the_rest_of_the_event():
    """The run still starts, and still ends, around an unusable declaration.

    The failure mode worth ruling out is a fold that bails early on the bad
    field: the run would never enter the in-flight set, and the panel would read
    IDLE through a live run — exactly the defect this file exists for, reached by
    a different route.
    """
    bench = BenchStatus()
    event = start(devices=None)
    event["devices"] = "otii_arc"
    bench.apply_event(event)

    assert bench.running_runs == [RUN]
    assert bench.any_busy is True

    bench.apply_event(end())
    assert bench.running_runs == []


def test_to_dict_reports_the_enrollment():
    """The renderer reads ``to_dict``, so the fact has to survive the trip.

    Asserted separately from :py:attr:`enrolled_devices` because a property no
    snapshot exposes is a fact the panel cannot draw.
    """
    bench = BenchStatus()
    bench.apply_event(start(devices=("otii_arc", "rigol_dl3031a")))

    snapshot = bench.to_dict()
    assert snapshot["enrolled"] == {"otii_arc": RUN, "rigol_dl3031a": RUN}
    assert snapshot["runs"] == {RUN: "running"}

    bench.apply_event(end())
    assert bench.to_dict()["enrolled"] == {}


# --------------------------------------------------------------------------
# The ``data`` unwrap: the payload shape that actually arrives over the wire
# --------------------------------------------------------------------------


def test_a_nested_payload_is_read_the_way_the_engine_sends_it():
    """The engine's real shape moves the model, not just the hand-built one.

    ``store.Event.to_dict`` nests every payload field under ``data``, and nothing
    downstream flattens it — ``Session.send_event`` adds only seq/ts and
    ``AgentFeed._on_event`` passes the dict straight to ``apply_event``. A fold
    that read only the top level saw an event with a kind, a run id and no content
    at all, which is how a real run enrolled no devices and a safety stop rendered
    as a clean pass. This is the unit-level statement of that contract.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(stage(to="EXECUTE"))
    assert bench.runs[RUN].stage == "EXECUTE"


def test_a_flat_payload_still_folds():
    """The flat shape must keep working, because it still arrives.

    Events are persisted by ``store.append_event`` and replayed on reconnect, and
    a store written by another agent version — or any hand-built/legacy frame —
    can carry these fields flat. The unwrap therefore *merges* rather than
    replaces: reading only ``data`` would swap one blind spot for the mirror
    image of it.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event({"kind": "run_stage", "run_id": RUN, "stage": "ANALYZE"})
    assert bench.runs[RUN].stage == "ANALYZE"


def test_where_both_shapes_carry_a_field_the_outer_one_wins():
    """A field present flat *and* nested resolves to the flat one.

    Both are the same event, so this only decides which statement is the more
    specific one. The outer level is: nesting is what the store does to a payload
    on its way through, while a top-level key was put there by whatever built or
    rewrote this particular frame. Pinned because the merge order is a one-token
    difference that no other test in this file can see — both shapes work either
    way round, and only a frame carrying both distinguishes them.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    event = stage(to="PREPARE")
    event["stage"] = "EXECUTE"
    bench.apply_event(event)
    assert bench.runs[RUN].stage == "EXECUTE"


@pytest.mark.parametrize("payload", ["EXECUTE", ["EXECUTE"], None, 7, True])
def test_a_data_field_that_is_not_a_dict_costs_the_event_nothing(payload):
    """A ``data`` that is not a mapping falls back to the flat read.

    ``apply_event`` runs on the feed's receive path, so ``{**data, **event}`` on a
    string would raise there and take the session — and the screen — down over one
    malformed optional field. The flat fields beside it are still perfectly good
    information and must still be folded, which is the difference between this and
    merely not crashing.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(
        {"kind": "run_stage", "run_id": RUN, "stage": "ANALYZE", "data": payload}
    )
    assert bench.runs[RUN].stage == "ANALYZE"


def test_a_nested_devices_declaration_actually_enrolls():
    """Defect one, at unit level: enrollment read "not known" through real runs.

    ``test_a_real_run_enrolls_its_declared_device`` proves this against a live
    engine, which is the test that could have caught it; this one states the same
    contract in isolation so a regression names the payload shape rather than
    arriving as a run that mysteriously drove nothing.
    """
    bench = BenchStatus()
    bench.apply_event(
        {
            "kind": "run_start",
            "run_id": RUN,
            "data": {"spec_sha256": "abc", "devices": ["otii_arc", "rigol_dl3031a"]},
        }
    )
    assert bench.runs[RUN].devices == ("otii_arc", "rigol_dl3031a")
    assert bench.enrolled_devices == {"otii_arc": RUN, "rigol_dl3031a": RUN}


def test_a_nested_run_end_outcome_is_not_flattened_to_finished():
    """Defect two, and the more serious one: a safety stop must read as one.

    ``run_end``'s outcome lives in the payload, so with the top level alone the
    outcome branch fell through to its ``"finished"`` default — and the single run
    outcome an operator has to go and look at rendered identically to a clean
    pass. The run is out of flight either way, so nothing but the *word* carries
    this fact.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(
        {
            "kind": "run_end",
            "run_id": RUN,
            "data": {"status": "safe_stopped", "reason": "voltage envelope"},
        }
    )
    assert bench.runs[RUN].state == "safe_stopped"
    assert bench.running_runs == []


def test_the_unwrap_reaches_every_field_the_fold_reads():
    """Name, step and progress come out of the nest too, not only the ones tested.

    Asserted together because the unwrap is one line serving every read below it:
    a fix applied field-by-field would leave whichever field nobody wrote a test
    for still reading the top level, and a run rail with a blank name and a frozen
    progress bar is the same defect wearing different clothes.
    """
    bench = BenchStatus()
    bench.apply_event({"kind": "run_start", "run_id": RUN, "data": {"name": "soak"}})
    bench.apply_event(
        {
            "kind": "run_step",
            "run_id": RUN,
            "data": {"step": "ramp", "progress": 0.25},
        }
    )
    assert bench.runs[RUN].name == "soak"
    assert bench.runs[RUN].step == "ramp"
    assert bench.runs[RUN].progress == pytest.approx(0.25)


# --------------------------------------------------------------------------
# The stage fold: the bench says where the run is, verbatim
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad",
    [
        {"stage": ""},
        {"stage": None},
        {"stage": 3},
        {"stage": ["EXECUTE"]},
        {"from": "PREPARE"},
        {},
    ],
)
def test_a_malformed_stage_does_not_clear_the_one_already_reported(bad):
    """An unusable transition is not news that the run left its stage.

    The reason there is deliberately no ``else`` clearing ``stage``. Blanking it
    would take the row from "the run is in EXECUTE" — which is still the last
    thing the bench actually said — to "no stage known", on the strength of a
    frame that said nothing at all. Losing information to a malformed event is
    the one outcome worse than ignoring it.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(stage(to="EXECUTE"))
    bench.apply_event({"kind": "run_stage", "run_id": RUN, "data": bad})
    assert bench.runs[RUN].stage == "EXECUTE"


def test_a_stage_name_this_model_does_not_know_is_recorded_verbatim():
    """The vocabulary lives with the bench, so nothing is validated here.

    This module imports nothing from ``benchctrl.agent`` on purpose — the panel
    talks to a *remote* agent, which may be a release ahead. Checking an arriving
    name against a list held here would make the panel go blank precisely when a
    newer bench had more to say, which is the inverse of the failure the stage
    events were added to fix. The renderer decides how to draw a name it cannot
    place; the model's job is to carry it.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(stage(to="CALIBRATE", index=-1))
    assert bench.runs[RUN].stage == "CALIBRATE"
    assert bench.to_dict()["run_stages"] == {RUN: "CALIBRATE"}


def test_a_reported_stage_survives_later_unrelated_events():
    """A stage is the run's standing position, not a one-frame flash.

    Real runs emit a stage transition and then hundreds of samples, actions and
    heartbeats before the next one, so a stage that only survived until the next
    event would be off the glass for effectively the whole run. Includes a
    terminal event: the last stage a run reached stays readable after it stops,
    which is what lets the row say where a failed run got to.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(stage(to="EXECUTE"))
    bench.apply_event({"kind": "run_step", "run_id": RUN, "step": "soak"})
    bench.apply_event({"kind": "action", "device": "otii_arc", "action": "read_value"})
    bench.apply_event({"kind": "link", "heartbeat_s": 5.0})
    bench.apply_event(end(status="failed"))
    assert bench.runs[RUN].stage == "EXECUTE"
    assert bench.runs[RUN].state == "failed"


def test_one_runs_stage_does_not_leak_into_another():
    """Stage is per run, and two runs at once is a state the panel must survive.

    ``RunManager.submit`` refuses to give one device to two runs, but nothing stops
    two runs on different instruments, and a reconnect replays both their event
    streams interleaved. A stage stored anywhere but on the run it belongs to
    would have the second run's transition silently rewrite the first's node.
    """
    bench = BenchStatus()
    bench.apply_event(start("r1", devices=("otii_arc",)))
    bench.apply_event(start("r2", devices=("rigol_dl3031a",)))
    bench.apply_event(stage("r1", to="EXECUTE"))
    bench.apply_event(stage("r2", to="PREPARE"))
    assert bench.runs["r1"].stage == "EXECUTE"
    assert bench.runs["r2"].stage == "PREPARE"


def test_to_dict_reports_the_stage_beside_the_run_state():
    """The renderer reads ``to_dict``, and reads the two maps separately.

    ``runs`` stays ``{id: state}`` — a bare string that four consumers index
    directly — so the stage arrives as its own parallel map keyed by the same run
    ids. Asserting both shapes here is the point: widening ``runs`` into a dict of
    fields would satisfy any test that only checked the stage was present, while
    breaking every consumer that indexes it.
    """
    bench = BenchStatus()
    bench.apply_event(start())
    bench.apply_event(stage(to="EXECUTE"))

    snapshot = bench.to_dict()
    assert snapshot["runs"] == {RUN: "running"}
    assert snapshot["run_stages"] == {RUN: "EXECUTE"}


def test_to_dict_omits_a_run_that_never_reported_a_stage():
    """Absent, not present-and-empty, so "the bench has not said" stays sayable.

    An entry of ``""`` would be a stage name as far as a consumer is concerned,
    and the whole reason this field exists is that the display must be able to
    light no node rather than guess one. Only the run that reported is listed, so
    a mid-run panel and an older agent are both distinguishable from a run in
    INIT.
    """
    bench = BenchStatus()
    bench.apply_event(start("r1", devices=("otii_arc",)))
    bench.apply_event(start("r2", devices=("rigol_dl3031a",)))
    bench.apply_event(stage("r2", to="PREPARE"))

    snapshot = bench.to_dict()
    assert snapshot["run_stages"] == {"r2": "PREPARE"}
    assert set(snapshot["runs"]) == {"r1", "r2"}


# --------------------------------------------------------------------------
# End to end: a real engine's own events, folded by the real model
# --------------------------------------------------------------------------


def _spec(**overrides) -> RunSpec:
    base = dict(
        name="panel",
        device="otii_arc",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        # record=False so metrics come from read_value rather than a recording
        # buffer; nothing here cares about chunks.
        sampling=Sampling(
            channels=("mv",), chunk_s=60, metric_period_s=0.05, record=False
        ),
        phases=(
            Phase(name="soak", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=1.0),
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


def _real_events(spec, device) -> tuple[str, list[dict]]:
    """Run a real engine on a compressed clock; return its run id and events.

    ``on_event`` is the same sink the agent gives the engine in production
    (``_run_submit`` passes ``agent._broadcast_event``), so these are the exact
    dicts the event bus fans out to a dashboard session — not a reconstruction.
    """
    captured: list[dict] = []
    with tempfile.TemporaryDirectory() as runs_dir:
        engine = RunEngine(
            spec,
            device,
            runs_dir=runs_dir,
            clock_scale=0.02,
            on_event=captured.append,
        )
        engine.start()
        assert engine.join(timeout=60), "run did not finish"
        return engine.run_id, captured


def test_a_real_runs_events_put_the_bench_in_flight_and_release_it(device):
    """The test that would have caught the defect.

    Nothing here names an event kind: the model is fed whatever the engine
    emitted, in the order it emitted it, and the assertion is about what the
    panel would have shown. Under the original model this failed at the first
    assertion — ``running_runs`` was empty at ``run_start`` — while every
    hand-built test in the suite passed.
    """
    run_id, events = _real_events(_spec(), device)

    bench = BenchStatus()
    in_flight_after_start = None
    for event in events:
        bench.apply_event(event)
        if event["kind"] == "run_start":
            in_flight_after_start = list(bench.running_runs)

    assert in_flight_after_start == [run_id], (
        "the model did not see a real run start; the agent and the dashboard "
        "disagree about how a run announces itself"
    )
    assert bench.running_runs == [], "a finished real run stayed in flight"
    assert bench.enrolled_devices == {}


def test_a_real_run_is_busy_for_its_whole_duration(device):
    """Between the real start and the real end, the panel reads ACTIVE.

    Stronger than the run-id assertion above and closer to what an operator sees:
    with nothing armed and no worker pool, a live run is the *only* thing that
    can make this bench read busy, so this is the headline the panel was getting
    wrong for the whole of every run.
    """
    _, events = _real_events(_spec(), device)

    bench = BenchStatus()
    bench.apply_event({"kind": "link", "heartbeat_s": 5.0})
    bench.apply_connected({"agent": "bench", "observer": True, "heartbeat_s": 5.0})

    busy_by_kind = []
    for event in events:
        bench.apply_event(event)
        busy_by_kind.append((event["kind"], bench.any_busy))

    assert ("run_start", True) in busy_by_kind
    assert ("phase_end", True) in busy_by_kind, "the bench went idle mid-run"
    assert busy_by_kind[-1] == ("run_end", False)


def test_every_run_kind_a_real_engine_emits_is_one_the_model_folds(device):
    """The vocabularies match, asserted as sets rather than by example.

    The contract test for the mismatch itself. Any run lifecycle kind the engine
    learns to emit that this model does not know about would be silently ignored
    — which is precisely how ``run_start`` came to be dropped — so this compares
    what was actually emitted against
    :py:data:`~benchctrl.dashboards.state.RUN_EVENT_KINDS` and names the gap.
    """
    _, complete = _real_events(_spec(), device)
    device._sim.waveforms["mv"] = _Fixed(9.0)
    _, breached = _real_events(
        _spec(
            phases=(
                Phase(
                    name="overvolt",
                    mode="cv",
                    setpoints={"voltage_V": 3.0},
                    duration_s=30.0,
                ),
            )
        ),
        device,
    )

    emitted = {e["kind"] for e in complete + breached}
    run_kinds = {k for k in emitted if k.startswith("run_")}
    assert run_kinds, "no run lifecycle events were emitted at all"
    # Named explicitly so the sweep cannot pass by finding nothing: these are the
    # kinds a real run is known to produce today, and this test caught run_stage
    # being emitted by the engine while the model folded no such kind.
    assert {"run_start", "run_end", "run_stage"} <= run_kinds

    unknown = run_kinds - set(RUN_EVENT_KINDS)
    assert not unknown, (
        f"the engine emits run kinds the dashboard model ignores: {sorted(unknown)}"
    )


def test_hand_built_start_matches_the_engines_own_run_start(device):
    """The helper at the top of this file is shaped like the real thing.

    Every hand-built test above rests on :py:func:`start`, so if that drifted
    from the engine they would all keep passing while testing a fiction. This
    pins the helper to a real event's keys — the guard that stops this file from
    re-inventing the vocabulary it was written to police.
    """
    run_id, events = _real_events(_spec(), device)
    real = next(e for e in events if e["kind"] == "run_start")

    mine = start(run_id)
    assert real["kind"] == mine["kind"]
    assert real["run_id"] == mine["run_id"]
    # The engine nests its payload under ``data``; see the xfail below.
    assert real["data"]["devices"] == mine["devices"]


def test_a_real_run_enrolls_its_declared_device(device):
    """A real engine's own events must enrol its device — not a hand-built shape.

    Regression test for the second layer of the cross-process bug this file was
    written to catch. The spelling was fixed first (``run_start``, not
    ``run_started``), and this still failed: the engine nests every payload field
    under ``data`` (``store.Event.to_dict``) and nothing between it and the panel
    flattens it — ``Session.send_event`` adds only seq/ts, ``AgentFeed._on_event``
    passes the dict straight through. So ``event.get("devices")`` missed every real
    run and enrollment read "not known" on the bench while its unit tests passed.

    Two vocabulary mismatches in the same path, each invisible to a test that
    builds its own input. This one drives a real engine end to end instead.
    """
    run_id, events = _real_events(_spec(), device)

    bench = BenchStatus()
    enrolled_after_start = None
    for event in events:
        bench.apply_event(event)
        if event["kind"] == "run_start":
            enrolled_after_start = dict(bench.enrolled_devices)

    assert enrolled_after_start == {"otii_arc": run_id}


def test_real_run_end_surfaces_its_outcome(device):
    """A safety stop must reach the panel as a safety stop.

    Same root cause as the enrollment test above, and the more serious symptom:
    ``run_end``'s outcome is inside ``data``, so the outcome branch fell through to
    its ``"finished"`` default and the one run outcome an operator must go and look
    at rendered identically to a clean pass.
    """
    device._sim.waveforms["mv"] = _Fixed(9.0)
    spec = _spec(
        phases=(
            Phase(
                name="overvolt", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=30.0
            ),
        )
    )
    run_id, events = _real_events(spec, device)

    bench = BenchStatus()
    for event in events:
        bench.apply_event(event)

    assert bench.runs[run_id].state == store_mod.STATUS_SAFE_STOPPED


def test_hand_built_stage_matches_the_engines_own_run_stage(device):
    """The :py:func:`stage` helper is shaped like the real event.

    The same guard ``test_hand_built_start_matches_the_engines_own_run_start``
    provides for ``start``, and needed for the same reason: every stage test above
    rests on this helper, so a helper that drifted from the engine would leave them
    all passing against a shape nothing sends. In particular it pins the *nesting*
    — the one detail that made the previous fold read empty against real runs.
    """
    run_id, events = _real_events(_spec(), device)
    real = next(e for e in events if e["kind"] == "run_stage")

    mine = stage(run_id)
    assert real["kind"] == mine["kind"]
    assert real["run_id"] == mine["run_id"]
    assert set(real["data"]) == set(mine["data"])
    assert "stage" not in real, "the engine does not send this field flat"


def test_a_real_runs_stages_reach_the_panel_in_order(device):
    """The row reports the bench's own transitions, start to finish.

    The end-to-end statement of the change these events were added for: no kind,
    key or stage name here is invented by the test — the engine's events are
    replayed into a real model and the assertions are about what the panel would
    have shown. The old renderer *derived* a stage from run status, which has three
    interesting values, so most nodes were unreachable and the lit one was wrong
    for nearly all of the run.

    The final stage is asserted separately from the sequence because it is the one
    an operator sees after the run: a run that ends without reaching DONE leaves
    the row lit mid-flow on a bench that has stopped.

    The sequence is INIT/EXECUTE/DONE rather than all five because this spec has
    one ``cv`` phase and ``record=False``: PREPARE needs an ``idle`` phase and
    ANALYZE needs a persisted chunk. That is the point of asserting the observed
    list rather than the vocabulary — the row shows the stages this run had, and
    the nodes it never entered stay dark instead of being invented.
    """
    run_id, events = _real_events(_spec(), device)

    bench = BenchStatus()
    seen = []
    for event in events:
        bench.apply_event(event)
        reported = bench.runs.get(run_id)
        if reported is not None and (not seen or seen[-1] != reported.stage):
            seen.append(reported.stage)

    # Leading "" is the honest gap between run_start and the first transition.
    assert seen[0] == ""
    assert seen[1:] == ["INIT", "EXECUTE", "DONE"], seen
    assert bench.to_dict()["run_stages"] == {run_id: "DONE"}


# What the run is, and what it is testing on
#
# Both travel on ``run_start`` and both are asserted against a real engine, for
# the reason this file exists: the panel's two worst defects were a key nobody
# sent (``run_started`` vs ``run_start``) and a key sent one level down (the
# ``data`` nesting), and a hand-built event cannot see either.
# --------------------------------------------------------------------------


def test_a_real_run_tells_the_panel_what_it_is_testing_on(device):
    """The DUT reaches the display from the spec, through the real wire.

    The panel has a section titled DEVICE UNDER TEST that, until this went in, had
    never been told what the device under test was: ``RunSpec.dut`` was persisted
    in its own store column and sent to nobody.
    """
    run_id, events = _real_events(_spec(dut="room-temp-sensor"), device)

    bench = BenchStatus()
    for event in events:
        bench.apply_event(event)

    assert bench.runs[run_id].dut == "room-temp-sensor"
    assert bench.to_dict()["run_dut"] == {run_id: "room-temp-sensor"}


def test_a_real_runs_name_survives_its_whole_event_stream(device):
    """Why the engine sends ``run_name`` under a key of its own.

    ``name`` is folded on *every* run event, and ``phase_start`` already sends the
    phase's name under ``name``. That collision is latent rather than live today —
    ``phase_start`` is not in ``RUN_EVENT_KINDS``, so nothing folds it — which is
    exactly why the separate key is worth having now: the run's name would start
    being overwritten the moment phase events were routed into this fold, and the
    breakage would look like a display bug, not a routing change.

    Fed the whole stream in order so any later event that does reach the fold has
    its chance to clobber the name.
    """
    run_id, events = _real_events(_spec(name="cr2032-assoc-24h"), device)

    bench = BenchStatus()
    for event in events:
        bench.apply_event(event)

    assert bench.runs[run_id].run_name == "cr2032-assoc-24h"
    assert bench.to_dict()["run_names"] == {run_id: "cr2032-assoc-24h"}


def test_a_phase_name_arriving_under_name_cannot_rename_the_run(device):
    """The separation, asserted directly rather than via today's routing.

    Built by hand on purpose: this pins the *contract* — that a ``name`` field on a
    later run event does not touch ``run_name`` — so it holds whether or not phase
    events are ever folded here. Without it the guard above is only as good as the
    kind list, and the two vocabulary defects this file exists for were both a kind
    list that quietly did not match.
    """
    run_id, events = _real_events(_spec(name="cr2032-assoc-24h"), device)

    bench = BenchStatus()
    for event in events:
        bench.apply_event(event)
    bench.apply_event({"kind": "run_step", "run_id": run_id, "name": "soak"})

    assert bench.runs[run_id].run_name == "cr2032-assoc-24h"
    assert bench.runs[run_id].name == "soak", "premise: name *is* folded"


def test_a_run_that_declared_no_dut_is_distinguishable_from_no_run(device):
    """``RunSpec.dut`` defaults to ``""``, so this is the common case, not an edge.

    The empty string cannot carry the difference by itself, which is what
    ``dut_known`` is for. A panel that could not tell these apart would show an
    idle bench and a run with an unnamed DUT identically.
    """
    run_id, events = _real_events(_spec(), device)

    bench = BenchStatus()
    for event in events:
        bench.apply_event(event)

    assert bench.runs[run_id].dut == ""
    assert bench.runs[run_id].dut_known is True
    # Present-with-an-empty-value, not absent: absent is reserved for a run whose
    # start this session never saw.
    assert bench.to_dict()["run_dut"] == {run_id: ""}
    assert bench.to_dict()["run_names"] == {run_id: "panel"}


def test_a_dut_that_is_not_a_string_is_treated_as_undeclared():
    """``str(None)`` would put the word "None" in a panel titled DEVICE UNDER TEST.

    Reachable from a replayed store row, a hand-built event, or an agent version
    that models the field differently. Coercion is the tempting shortcut and the
    wrong one: a garbage value must degrade to "not declared", which the display
    already renders honestly, rather than to a string that looks like a DUT name.
    """
    for bad in (None, 3, ["room-temp-sensor"], {"name": "x"}, True):
        bench = BenchStatus()
        bench.apply_event({"kind": "run_start", "run_id": "r1", "dut": bad})
        assert bench.runs["r1"].dut == "", bad
        # Still *known*: we saw the start, so "this run declared nothing usable"
        # is the honest reading, not "we never heard about the run".
        assert bench.runs["r1"].dut_known is True, bad


def test_a_name_field_is_not_accepted_as_the_runs_name():
    """``run_name`` and ``name`` must stay separate at the fold, not just on the wire.

    A ``run_start`` carrying only ``name`` — which is what a phase event looks
    like, and what an older agent would send — must leave ``run_name`` empty rather
    than adopt it. Without this the separate key buys nothing: the fold would
    quietly re-merge the two nouns the engine took care to split.
    """
    bench = BenchStatus()
    bench.apply_event({"kind": "run_start", "run_id": "r1", "name": "soak"})

    assert bench.runs["r1"].run_name == ""
    assert bench.runs["r1"].name == "soak", "premise: ``name`` is still folded"
    assert "r1" not in bench.to_dict()["run_names"]


def test_a_run_whose_start_was_missed_claims_no_dut_at_all(device):
    """A panel that connected mid-run must not answer the DUT question.

    Reached whenever a dashboard attaches to a run already in flight. Nothing here
    may default to empty-and-known, which would read as "the run declared no DUT"
    — a statement about the spec that this session has no basis for.
    """
    run_id, events = _real_events(_spec(dut="room-temp-sensor"), device)

    bench = BenchStatus()
    for event in events:
        if event["kind"] == "run_start":
            continue
        bench.apply_event(event)

    assert bench.runs[run_id].dut_known is False
    assert run_id not in bench.to_dict()["run_dut"]


class _Fixed:
    """A waveform pinned to one value, for driving the safety envelope."""

    def __init__(self, value: float) -> None:
        self.v = value

    def value(self, t: float) -> float:
        return self.v

    def mean_over(self, t0: float, t1: float) -> float:
        return self.v

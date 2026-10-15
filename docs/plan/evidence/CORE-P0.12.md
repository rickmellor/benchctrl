---
id: CORE-P0.12
commit: e738ab5b6278
date: 2026-10-14
rig: docs
operator_role: ai-assistant
measurements:
  safety_changes: 8
  safety_changes_with_changelog_entry: 8
  runs_md_promise_fixed: 1
  known_limitations_states_best_effort: 1
---

A documentation audit, not a bench measurement. **It was done by the AI assistant that
wrote Phase 0, so it is a self-check: a human reviewer should confirm it or replace it with
their own** (`operator_role` is the honest label until then).

**`docs/runs.md` promise.** "Hand the bench a declarative experiment, disconnect" was
untrue before e6fa88c: about `deadman_s` after the last client left, the run's device
tripped `heartbeat_lost`. The new section "Who keeps a run's output alive" says what now
keeps it alive, that a wedged engine trips `run_watchdog`, that slow waits are not wedges,
and that the deadman is damage limitation. `tests/test_unattended_run.py` (CORE-P0.4)
is the test behind the promise.

**KNOWN_LIMITATIONS § N-1** already said a software deadman "cannot guarantee an output
goes off" and that "the only real guarantee is a hardware interlock". e6fa88c adds that
the run watchdog is the same layer, not a stronger one.

**CHANGELOG coverage.** Each Phase 0 change that can change when or whether an output is
driven safe, or who may act on one, matched to its entry under `[Unreleased]`:

| # | Change | CHANGELOG entry |
|---|---|---|
| 1 | Run engine owns and touches `run:<id>` | The deadman is per owner… |
| 2 | `RUN_WATCHDOG` on a wedged engine | same |
| 3 | A trip aborts the run it lands on | same |
| 4 | Contact is per owner | same |
| 5 | Targeted trips leave other devices (and panic outlets) alone | same |
| 6 | `agent.close` trips only the closed device | same |
| 7 | `max_duration_s` enforced inside a phase | same |
| 8 | Claims gate `agent.open`/`agent.close`, `run.abort`, releases | Claims now gate… |

Method: read `git log 2d33602..e738ab5b6278` and each diff to safety.py, engine.py and server.py,
listed every behaviour change, and matched each to an entry.

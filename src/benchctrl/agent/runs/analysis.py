"""Evaluating a run's acceptance criteria, once, after the measurements.

Separate from :py:mod:`benchctrl.agent.runs.rules` on purpose, and the split is
the same one :py:class:`~benchctrl.agent.runs.spec.Check` argues for against
:py:class:`~benchctrl.agent.runs.spec.Condition`. Rules are control: they run on
the tick loop, against a live sample, while the output is energised, and one of
them can abort the run. This runs once, after the device has been idled, against
rows already committed to disk, and it cannot change anything the bench did. It
is the only part of a run that is allowed to be slow, and the only part where
being wrong is a reporting error rather than a safety one.

The verdict it produces answers a question no earlier field did. A run's terminal
status says how the *engine* exited — ``complete`` is equally true of a board
that met spec and one that drew twice its budget — so until this existed someone
had to open the bundle to learn which. Three outcomes, not two: ``inconclusive``
is what a check whose channel recorded nothing gets, because collapsing that into
either a pass or a fail is the most damaging thing this module could do.
"""

from __future__ import annotations

import logging
from typing import Any, Optional

from benchctrl.agent.runs.spec import (
    VERDICT_FAIL,
    VERDICT_INCONCLUSIVE,
    VERDICT_PASS,
    Analysis,
    Check,
)

log = logging.getLogger("benchctrl.agent.runs.analysis")

#: Why a check could not be judged. Recorded on the result so the panel can say
#: which of the two it is: no rows at all is usually a misspelled channel or a
#: phase that never ran, and a read that raised is a store problem.
NO_DATA = "no data recorded"


def evaluate(analysis: Analysis, store, *, phase_index=None) -> Optional[dict]:
    """Judge every check in ``analysis`` against what ``store`` recorded.

    Returns None when there is nothing to judge, which keeps "this run declared no
    criteria" out of the result shape entirely rather than expressing it as an
    empty verdict. Callers gate on the return value being None.

    ``phase_index`` maps a check's phase *name* to the store's phase index; it is
    the spec's phase order, passed in rather than read back from the store so a
    check against a phase that never started is inconclusive rather than a crash.
    Defaults to reading the store's own phase table, which is what a post-hoc
    caller re-analysing a finished bundle has.

    Never raises. This runs inside the engine's ``_finish``, after the output has
    been idled but before the manifest is written and before ``run_end`` is
    emitted. An exception escaping here would take down the run's terminal
    bookkeeping — losing DONE, the manifest, and the event that tells every
    consumer the run is over — to report on data that is already safely on disk.
    A check that cannot be read is inconclusive, which is exactly what it is.
    """
    if not analysis:
        return None
    if phase_index is None:
        phase_index = _phase_index_from_store(store)

    results = [_judge(check, store, phase_index) for check in analysis.checks]
    return {
        "verdict": _verdict(results, strict=analysis.strict),
        "strict": bool(analysis.strict),
        "checks": results,
        # Counts, so a consumer can render "2 of 5 FAILED" without walking the
        # list, and a log line can say what happened in one field.
        "passed": sum(1 for r in results if r["verdict"] == VERDICT_PASS),
        "failed": sum(1 for r in results if r["verdict"] == VERDICT_FAIL),
        "inconclusive": sum(
            1 for r in results if r["verdict"] == VERDICT_INCONCLUSIVE
        ),
        "total": len(results),
    }


def _judge(check: Check, store, phase_index: dict) -> dict:
    """One check's result, as a dict the wire and the manifest both carry."""
    result: dict[str, Any] = {
        "name": check.name,
        "label": check.label,
        "ch": check.channel,
        "agg": check.agg,
        "op": check.op,
        "value": check.value,
        "phase": check.phase,
        # The criterion in words, rendered here rather than by each consumer.
        # The dashboard, the manifest and a log line must not each re-derive it
        # and disagree about what the test was.
        "describe": check.describe(),
    }
    # A named phase the run never started has no index. Inconclusive rather than
    # falling back to the whole run: a soak limit silently evaluated over every
    # phase is a different test than the one the author wrote, and it would report
    # a confident PASS or FAIL against criteria nobody set.
    phase_idx: Optional[int] = None
    if check.phase:
        phase_idx = phase_index.get(check.phase)
        if phase_idx is None:
            return {
                **result,
                "verdict": VERDICT_INCONCLUSIVE,
                "measured": None,
                "why": f"phase {check.phase!r} did not run",
            }
    try:
        measured = store.metric_aggregate(
            check.channel, check.agg, phase_idx=phase_idx
        )
    except Exception as exc:  # noqa: BLE001
        # Inconclusive, with the reason kept. See the note in ``evaluate`` about
        # never raising: a store that cannot answer must not cost the run its
        # terminal events.
        log.warning("could not read %s for analysis: %r", check.describe(), exc)
        return {
            **result,
            "verdict": VERDICT_INCONCLUSIVE,
            "measured": None,
            "why": f"could not read {check.channel}: {exc!r}",
        }
    if measured is None:
        return {
            **result,
            "verdict": VERDICT_INCONCLUSIVE,
            "measured": None,
            "why": NO_DATA,
        }
    passed = check.passes(measured)
    return {
        **result,
        "verdict": VERDICT_PASS if passed else VERDICT_FAIL,
        "measured": measured,
        # Empty on a pass. On a failure the sentence *is* the finding, and it
        # carries the measurement so a log line or a panel row stands alone.
        "why": "" if passed else f"measured {measured:.6g}, required {check.describe()}",
    }


def _verdict(results: list, *, strict: bool) -> str:
    """Roll per-check results into one word.

    Any failure fails the run: these are acceptance criteria, so they are a
    conjunction, and a run that met four of five requirements did not meet the
    requirements. Checked before inconclusiveness, because a measured failure is a
    stronger statement than an unmeasured check — a DUT that demonstrably drew too
    much current has failed whether or not a second criterion could be read.

    An unevaluable check then yields ``inconclusive`` unless ``strict``, where it
    fails. Both readings are defensible and the spec author chooses; the default
    keeps FAIL meaning "look at the DUT" and INCONCLUSIVE meaning "look at the
    test", which is the distinction that makes either word actionable.

    No results at all cannot reach here — :py:func:`evaluate` returns None for an
    analysis with no checks — but it is handled rather than indexed into, because
    "passed because nothing was checked" is the exact shape of a green light that
    means nothing.
    """
    if not results:
        return VERDICT_INCONCLUSIVE
    verdicts = [r["verdict"] for r in results]
    if VERDICT_FAIL in verdicts:
        return VERDICT_FAIL
    if VERDICT_INCONCLUSIVE in verdicts:
        return VERDICT_FAIL if strict else VERDICT_INCONCLUSIVE
    return VERDICT_PASS


def _phase_index_from_store(store) -> dict:
    """``{phase_name: idx}`` from the store's own phase table.

    Only phases that actually started have rows, which is the behaviour a check
    against an unrun phase needs: it comes back inconclusive rather than being
    judged over data from a phase of the same name that never executed.
    """
    try:
        rows = store.phases()
    except Exception:  # noqa: BLE001
        log.debug("could not read the phase table for analysis", exc_info=True)
        return {}
    index = {}
    for row in rows:
        name = row.get("name")
        if isinstance(name, str) and name:
            index.setdefault(name, row.get("idx"))
    return index

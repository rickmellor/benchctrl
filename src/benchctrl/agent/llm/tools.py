"""The bounded tool set the on-board model may use.

Eight tools. Four read, two annotate, two move the run toward its end. That
is the entire surface, and it is an allowlist enforced in code rather than a
suggestion made in a prompt.

What is deliberately absent: ``set_voltage``, ``set_output``, and every
other driver method. The model cannot energise anything, cannot widen the
safety envelope, and cannot restart a phase. A 1.5B model at two minutes per
turn is a useful narrator and a plausible judge of "does this look like it
finished early"; it is not something to hand a power supply to.

The two actions that do change the run are both *monotone toward safety*:
``advance_phase`` only moves forward through a list declared before the run
started, and ``abort_run`` only stops things. Neither can extend a run,
raise a limit, or re-energise a DUT.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Optional

log = logging.getLogger("benchctrl.agent.llm.tools")

#: Every tool the model may call. Anything else is a policy violation.
TOOL_NAMES = frozenset(
    {
        "run_status",
        "phase_summary",
        "recent_events",
        "metric_window",
        "annotate",
        "raise_alert",
        "advance_phase",
        "abort_run",
    }
)

#: Tools that change anything. Kept separate so the read-only set can be
#: offered during a phase and the acting set only at decision points.
ACTION_TOOLS = frozenset({"annotate", "raise_alert", "advance_phase", "abort_run"})

MAX_EVENTS = 20
MAX_METRIC_SECONDS = 600.0


def tool_schemas(*, allow_actions: bool = True) -> list[dict]:
    """OpenAI-style function schemas for the allowed tools."""
    schemas = [
        {
            "type": "function",
            "function": {
                "name": "run_status",
                "description": "Current run status: phase, elapsed time, and state.",
                "parameters": {"type": "object", "properties": {}},
            },
        },
        {
            "type": "function",
            "function": {
                "name": "phase_summary",
                "description": "Aggregate measurements for one phase.",
                "parameters": {
                    "type": "object",
                    "properties": {
                        "phase_idx": {"type": "integer", "description": "0-based index"}
                    },
                    "required": ["phase_idx"],
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "recent_events",
                "description": f"The most recent events (max {MAX_EVENTS}).",
                "parameters": {
                    "type": "object",
                    "properties": {"n": {"type": "integer"}},
                },
            },
        },
        {
            "type": "function",
            "function": {
                "name": "metric_window",
                "description": (
                    f"Aggregated samples for one channel over the last N "
                    f"seconds (max {MAX_METRIC_SECONDS:g})."
                ),
                "parameters": {
                    "type": "object",
                    "properties": {
                        "channel": {"type": "string"},
                        "seconds": {"type": "number"},
                    },
                    "required": ["channel"],
                },
            },
        },
    ]
    if not allow_actions:
        return schemas

    schemas.extend(
        [
            {
                "type": "function",
                "function": {
                    "name": "annotate",
                    "description": "Attach a short note to the run log.",
                    "parameters": {
                        "type": "object",
                        "properties": {"text": {"type": "string"}},
                        "required": ["text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "raise_alert",
                    "description": "Flag something an operator should look at.",
                    "parameters": {
                        "type": "object",
                        "properties": {
                            "severity": {
                                "type": "string",
                                "enum": ["info", "warn", "alarm"],
                            },
                            "text": {"type": "string"},
                        },
                        "required": ["severity", "text"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "advance_phase",
                    "description": (
                        "End the current phase early and move to the next. "
                        "Forward only; cannot repeat or skip backwards."
                    ),
                    "parameters": {
                        "type": "object",
                        "properties": {"reason": {"type": "string"}},
                        "required": ["reason"],
                    },
                },
            },
            {
                "type": "function",
                "function": {
                    "name": "abort_run",
                    "description": "Stop the run and drive the device to a safe state.",
                    "parameters": {
                        "type": "object",
                        "properties": {"reason": {"type": "string"}},
                        "required": ["reason"],
                    },
                },
            },
        ]
    )
    return schemas


class ToolExecutor:
    """Executes allowlisted tool calls against a live run.

    Every entry point clamps its own arguments. The model is not trusted to
    respect a documented maximum, so ``recent_events(n=10_000)`` returns 20
    and says so rather than pulling an entire run's history into a prompt
    that then takes twenty minutes to process.
    """

    def __init__(self, engine, *, on_violation: Optional[Callable[[str], None]] = None):
        self.engine = engine
        self.store = engine.store
        self.on_violation = on_violation
        self.violations = 0
        self.calls: list[str] = []

    def execute(self, name: str, arguments: dict) -> dict:
        if name not in TOOL_NAMES:
            return self._violation(
                name, f"tool {name!r} is not available; allowed: {sorted(TOOL_NAMES)}"
            )
        handler = getattr(self, f"_tool_{name}", None)
        if handler is None:  # pragma: no cover - TOOL_NAMES kept in step
            return self._violation(name, f"tool {name!r} has no implementation")
        self.calls.append(name)
        try:
            return handler(arguments or {})
        except Exception as exc:  # noqa: BLE001
            log.warning("tool %s raised: %r", name, exc)
            return {"error": repr(exc)}

    def _violation(self, name: str, message: str) -> dict:
        self.violations += 1
        log.warning("llm policy violation: %s", message)
        self.store.append_event(
            "policy_violation",
            severity="warn",
            source="llm",
            payload={"tool": name, "message": message},
        )
        if self.on_violation is not None:
            self.on_violation(message)
        return {"error": message}

    # --- read-only ------------------------------------------------------

    def _tool_run_status(self, args: dict) -> dict:
        status = self.engine.status_dict()
        return {
            "status": status.get("status"),
            "phase_idx": status.get("phase_idx"),
            "phase_name": status.get("phase_name"),
            "phase_count": status.get("phase_count"),
            "started_utc": status.get("started_utc"),
        }

    def _tool_phase_summary(self, args: dict) -> dict:
        idx = int(args.get("phase_idx", self.engine._phase_idx))
        phases = self.store.phases()
        match = next((p for p in phases if p["idx"] == idx), None)
        if match is None:
            return {"error": f"no phase {idx}"}
        channels = self.engine.spec.sampling.channels
        return {
            "phase": match,
            "channels": {
                code: self.store.latest_metric(code) for code in channels
            },
        }

    def _tool_recent_events(self, args: dict) -> dict:
        n = min(int(args.get("n", 10) or 10), MAX_EVENTS)
        events = self.store.recent_events(limit=n)
        return {
            "events": [
                {
                    "kind": e["kind"],
                    "severity": e["severity"],
                    "phase_idx": e["phase_idx"],
                    "data": e["data"],
                }
                for e in events
            ]
        }

    def _tool_metric_window(self, args: dict) -> dict:
        channel = str(args.get("channel", ""))
        seconds = min(float(args.get("seconds", 60.0) or 60.0), MAX_METRIC_SECONDS)
        rows = self.store.metric_window(channel, seconds)
        if not rows:
            return {"channel": channel, "seconds": seconds, "n": 0}
        values = [r["mean"] for r in rows if r["mean"] is not None]
        if not values:
            return {"channel": channel, "seconds": seconds, "n": 0}
        return {
            "channel": channel,
            "seconds": seconds,
            "n": len(values),
            "min": min(values),
            "max": max(values),
            "mean": sum(values) / len(values),
            "last": values[-1],
        }

    # --- additive actions -----------------------------------------------

    def _tool_annotate(self, args: dict) -> dict:
        text = str(args.get("text", "")).strip()[:1000]
        if not text:
            return {"error": "annotate needs text"}
        self.store.append_event(
            "llm_note", source="llm", payload={"text": text},
            phase_idx=self.engine._phase_idx,
        )
        self.store.append_note(text, heading=f"phase {self.engine._phase_idx}")
        return {"ok": True}

    def _tool_raise_alert(self, args: dict) -> dict:
        severity = str(args.get("severity", "warn"))
        if severity not in ("info", "warn", "alarm"):
            # Not a violation — clamp and continue. The model reaching for
            # "critical" is a judgement call it is not entitled to make, but
            # it is not misbehaviour either.
            severity = "warn"
        text = str(args.get("text", "")).strip()[:1000]
        self.store.append_event(
            "llm_alert",
            severity=severity,
            source="llm",
            payload={"text": text},
            phase_idx=self.engine._phase_idx,
        )
        return {"ok": True, "severity": severity}

    # --- monotone actions ------------------------------------------------

    def _tool_advance_phase(self, args: dict) -> dict:
        reason = str(args.get("reason", "")).strip()[:500] or "model requested"
        idx = self.engine._phase_idx
        if idx < 0 or idx >= len(self.engine.spec.phases) - 1:
            return self._violation(
                "advance_phase",
                f"cannot advance past the last phase (at {idx} of "
                f"{len(self.engine.spec.phases)})",
            )
        self.store.append_event(
            "llm_action",
            severity="warn",
            source="llm",
            payload={"action": "advance_phase", "reason": reason},
            phase_idx=idx,
        )
        self.engine.request_advance(reason)
        return {"ok": True, "advanced_from": idx}

    def _tool_abort_run(self, args: dict) -> dict:
        reason = str(args.get("reason", "")).strip()[:500] or "model requested"
        self.store.append_event(
            "llm_action",
            severity="alarm",
            source="llm",
            payload={"action": "abort_run", "reason": reason},
            phase_idx=self.engine._phase_idx,
        )
        self.engine.abort(f"llm: {reason}")
        return {"ok": True}

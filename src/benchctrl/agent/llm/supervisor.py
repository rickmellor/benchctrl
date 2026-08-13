"""The on-board model as a slow advisor.

Runs on its own thread. Wakes at phase boundaries and on warnings, reads
numbers the tick loop already computed, and writes a sentence or two into
the run log. It may also request one of two bounded actions — advance the
phase, or abort — both of which only move the run toward its end.

What it never does: participate in a control loop, see raw samples, widen
the safety envelope, or delay an abort. At roughly two minutes per turn it
is physically incapable of the first, and the rest are prevented in code
rather than in the prompt.

The design consequence worth stating plainly: **the deterministic rules are
the safety system, and the model is commentary.** If the model is
unavailable, stalled, or talking nonsense, the run is unaffected — it just
has fewer annotations. Every test in this layer asserts that.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

from benchctrl.agent.llm.client import LLMClient, LLMUnavailable, estimate_tokens
from benchctrl.agent.llm.tools import ToolExecutor, tool_schemas

log = logging.getLogger("benchctrl.agent.llm.supervisor")

SYSTEM_PROMPT = """You are monitoring an automated electronics bench test.

You receive pre-computed summaries, never raw samples. Deterministic rules
already handle safety and have already fired if anything was wrong — you do
not need to catch faults, and you must not assume your judgement is the last
line of defence.

Your job:
- Note in one or two sentences whether the phase behaved as expected.
- Call raise_alert only for something a human should actually look at.
- Call abort_run only if continuing would clearly waste the run.

Be brief. Long answers are slow on this hardware and get truncated."""

#: Disable the model for the rest of a run after this many policy violations
#: in one phase. A model that keeps reaching for tools it does not have is
#: not going to start behaving, and each attempt costs two minutes.
MAX_VIOLATIONS_PER_PHASE = 3


@dataclass
class SupervisorStats:
    turns: int = 0
    failures: int = 0
    violations: int = 0
    total_seconds: float = 0.0
    skipped_rate_limit: int = 0
    disabled_reason: str = ""

    def to_dict(self) -> dict:
        return {
            "turns": self.turns,
            "failures": self.failures,
            "violations": self.violations,
            "total_seconds": round(self.total_seconds, 1),
            "skipped_rate_limit": self.skipped_rate_limit,
            "disabled_reason": self.disabled_reason,
        }


class LLMSupervisor:
    """Advisory commentary on a running experiment."""

    def __init__(
        self,
        engine,
        client: LLMClient,
        *,
        config=None,
    ) -> None:
        self.engine = engine
        self.client = client
        self.config = config or engine.spec.llm
        self.stats = SupervisorStats()
        self.executor = ToolExecutor(engine, on_violation=self._on_violation)

        self._queue: list[tuple[str, int]] = []
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._last_turn_at = 0.0
        self._phase_violations = 0
        self.enabled = bool(self.config.enabled)

    # --- lifecycle ------------------------------------------------------

    def start(self) -> "LLMSupervisor":
        if not self.enabled or self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="llm-supervisor", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        self._wake.set()
        if self._thread is not None:
            self._thread.join(timeout=timeout)
            self._thread = None

    def notify(self, trigger: str, phase_idx: int) -> None:
        """Queue a wake-up. Called from the tick loop — must not block.

        This is the entire coupling between the engine and the model: append
        to a list and set an event. Whatever the model is doing, the tick
        loop returns immediately.
        """
        if not self.enabled or trigger not in self.config.call_on:
            return
        with self._lock:
            if len(self._queue) > 8:
                # A backlog means the model is slower than the run. Drop
                # rather than accumulate — stale commentary is worthless.
                self._queue.pop(0)
            self._queue.append((trigger, phase_idx))
        self._wake.set()

    # --- the thread -----------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=1.0)
            self._wake.clear()
            while not self._stop.is_set():
                with self._lock:
                    if not self._queue:
                        break
                    trigger, phase_idx = self._queue.pop(0)
                self._maybe_turn(trigger, phase_idx)

    def _maybe_turn(self, trigger: str, phase_idx: int) -> None:
        if not self.enabled:
            return
        elapsed = time.monotonic() - self._last_turn_at
        if self._last_turn_at and elapsed < self.config.min_interval_s:
            self.stats.skipped_rate_limit += 1
            log.debug(
                "llm: skipping %s (only %.0fs since the last turn, minimum %.0fs)",
                trigger,
                elapsed,
                self.config.min_interval_s,
            )
            return
        self._last_turn_at = time.monotonic()
        try:
            self._turn(trigger, phase_idx)
        except LLMUnavailable as exc:
            self.stats.failures += 1
            log.info("llm unavailable (%s) — the run continues without commentary", exc)
        except Exception:  # noqa: BLE001
            self.stats.failures += 1
            log.exception("llm turn failed — the run continues")

    def _turn(self, trigger: str, phase_idx: int) -> None:
        prompt = self._build_prompt(trigger, phase_idx)
        tokens = estimate_tokens(prompt)
        if tokens > self.config.max_prompt_tokens:
            prompt = prompt[: self.config.max_prompt_tokens * 4]
            log.debug("llm: prompt truncated to the %d-token budget", self.config.max_prompt_tokens)

        started = time.monotonic()
        completion = self.client.chat(
            [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": prompt},
            ],
            tools=tool_schemas(allow_actions=True),
            max_tokens=self.config.max_output_tokens,
            timeout_s=self.config.max_call_s,
        )
        self.stats.turns += 1
        self.stats.total_seconds += time.monotonic() - started

        for call in completion.tool_calls:
            result = self.executor.execute(call.name, call.arguments)
            log.debug("llm tool %s -> %s", call.name, result)

        if completion.text:
            self.engine.store.append_event(
                "llm_note",
                source="llm",
                phase_idx=phase_idx,
                payload={
                    "text": completion.text,
                    "trigger": trigger,
                    "elapsed_s": round(completion.elapsed_s, 1),
                },
            )
            self.engine.store.append_note(
                completion.text, heading=f"{trigger} (phase {phase_idx})"
            )

    # --- prompt ---------------------------------------------------------

    def _build_prompt(self, trigger: str, phase_idx: int) -> str:
        """Assemble a compact, pre-computed summary.

        The model never sees raw samples. Aggregates are cheap for the
        engine and would cost minutes of prompt processing as raw numbers.
        """
        status = self.executor.execute("run_status", {})
        summary = self.executor.execute("phase_summary", {"phase_idx": phase_idx})
        events = self.executor.execute("recent_events", {"n": 6})

        lines = [
            f"Trigger: {trigger}",
            f"Run: {self.engine.spec.name} on {self.engine.spec.device}",
            f"Status: {json.dumps(status, separators=(',', ':'))}",
            f"Phase: {json.dumps(summary.get('phase', {}), separators=(',', ':'))}",
        ]
        channels = summary.get("channels") or {}
        for code, metric in channels.items():
            if metric:
                lines.append(
                    f"{code}: last={_fmt(metric.get('last'))} "
                    f"mean={_fmt(metric.get('mean'))} "
                    f"min={_fmt(metric.get('min'))} max={_fmt(metric.get('max'))}"
                )
        recent = events.get("events") or []
        if recent:
            lines.append("Recent events:")
            for event in recent[-6:]:
                lines.append(f"  [{event['severity']}] {event['kind']}: {event['data']}")

        if trigger == "phase_end":
            lines.append(
                "\nThe phase just ended. In one or two sentences: did it behave "
                "as expected?"
            )
        else:
            lines.append(
                "\nA warning fired. In one or two sentences: what does it mean, "
                "and does a human need to act?"
            )
        return "\n".join(lines)

    # --- policy ---------------------------------------------------------

    def _on_violation(self, message: str) -> None:
        self.stats.violations += 1
        self._phase_violations += 1
        if self._phase_violations >= MAX_VIOLATIONS_PER_PHASE:
            self.enabled = False
            self.stats.disabled_reason = (
                f"{self._phase_violations} policy violations in one phase"
            )
            log.warning(
                "llm disabled for this run after %d policy violations",
                self._phase_violations,
            )
            self.engine.store.append_event(
                "llm_disabled",
                severity="warn",
                source="llm",
                payload={"reason": self.stats.disabled_reason},
            )

    def reset_phase_violations(self) -> None:
        self._phase_violations = 0

    def status_dict(self) -> dict:
        return {"enabled": self.enabled, **self.stats.to_dict()}


def _fmt(value) -> str:
    if value is None:
        return "n/a"
    try:
        return f"{float(value):.6g}"
    except (TypeError, ValueError):
        return str(value)


def build_supervisor(engine, *, base_url: str = "", model: str = "") -> Optional[LLMSupervisor]:
    """Create a supervisor if the run asks for one and a backend answers."""
    config = engine.spec.llm
    if not config.enabled:
        return None
    from benchctrl.agent.llm.client import DEFAULT_BASE_URL

    client = LLMClient(
        base_url=base_url or DEFAULT_BASE_URL,
        model=model or config.model,
        timeout_s=config.max_call_s,
    )
    if not client.available():
        log.warning(
            "run %s requested LLM commentary but no backend is reachable at %s "
            "— continuing without it",
            engine.run_id,
            client.base_url,
        )
        return None
    return LLMSupervisor(engine, client, config=config)

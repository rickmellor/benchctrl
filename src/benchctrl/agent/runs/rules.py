"""Deterministic condition evaluation with dwell times.

Rules fire synchronously in the engine's tick loop, before the LLM ever sees
the data. That ordering is the whole safety argument: detection is exact,
immediate, and cheap; the model's contribution is a sentence of explanation
that arrives a minute or two later and cannot delay an abort.

Dwell (``for_s``) is what makes a rule usable on real measurements. Noise
crosses any threshold occasionally, and a run that aborted on a single stray
sample would be worse than no rule at all.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import Optional

from benchctrl.agent.runs.spec import Condition

log = logging.getLogger("benchctrl.agent.runs.rules")


@dataclass
class ConditionState:
    """Tracks how long a condition has held continuously."""

    condition: Condition
    since: Optional[float] = None
    fired: bool = False
    last_value: Optional[float] = None

    def update(self, value: Optional[float], now: Optional[float] = None) -> bool:
        """Feed a sample. Returns True on the tick the condition *becomes* true.

        Edge-triggered, not level-triggered: a sustained overcurrent emits
        one event, not one per tick for an hour.
        """
        now = time.monotonic() if now is None else now
        self.last_value = value

        if value is None or not self.condition.matches(value):
            self.since = None
            self.fired = False
            return False

        if self.since is None:
            self.since = now

        if self.fired:
            return False

        if (now - self.since) >= self.condition.for_s:
            self.fired = True
            return True
        return False

    @property
    def held_for(self) -> float:
        return 0.0 if self.since is None else time.monotonic() - self.since

    def reset(self) -> None:
        self.since = None
        self.fired = False


@dataclass
class RuleEngine:
    """Evaluates a set of conditions against the latest metrics."""

    states: dict[int, ConditionState] = field(default_factory=dict)

    def track(self, key: int, condition: Condition) -> ConditionState:
        state = self.states.get(key)
        if state is None or state.condition is not condition:
            state = ConditionState(condition=condition)
            self.states[key] = state
        return state

    def evaluate(
        self,
        conditions: list[Condition],
        values: dict[str, Optional[float]],
        *,
        offset: int = 0,
        now: Optional[float] = None,
    ) -> list[tuple[Condition, float]]:
        """Return the conditions that fired on this tick, with their values."""
        fired: list[tuple[Condition, float]] = []
        for i, condition in enumerate(conditions):
            state = self.track(offset + i, condition)
            value = values.get(condition.source)
            if state.update(value, now=now):
                fired.append((condition, value if value is not None else float("nan")))
                log.info(
                    "rule fired: %s (value=%s)", condition.describe(), value
                )
        return fired

    def reset_all(self) -> None:
        for state in self.states.values():
            state.reset()

    def reset_range(self, offset: int, count: int) -> None:
        """Clear state for one block — used at phase boundaries."""
        for i in range(offset, offset + count):
            state = self.states.get(i)
            if state is not None:
                state.reset()


#: Offsets keep the three condition families from colliding in one state map.
SAFETY_OFFSET = 0
EXIT_OFFSET = 10_000
RULE_OFFSET = 20_000

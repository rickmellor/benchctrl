"""Deterministic signal sources for simulated instruments.

Every waveform is a pure function of time, so a test can assert exact
expected statistics rather than "some number arrived". That property is what
makes the loopback tests meaningful: if the simulator emits a 100 mA square
wave at 50% duty, ``rec.statistics("mc").mean`` must come back at 50 mA
within float tolerance, and any bug in framing, demux, or buffering shows up
as a wrong number instead of a silent pass.

Time is always *seconds since the recording started*, supplied by the caller.
Nothing here reads the clock itself.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Protocol, Sequence, runtime_checkable


@runtime_checkable
class Waveform(Protocol):
    """Anything that can produce a value for a point in time."""

    def value(self, t: float) -> float:
        """Return the signal value at ``t`` seconds."""

    def mean_over(self, t0: float, t1: float) -> float:
        """Analytic mean over ``[t0, t1)`` — what a test should expect."""


@dataclass(frozen=True)
class Constant:
    """A flat line. The simplest thing that can possibly be asserted."""

    level: float = 0.0

    def value(self, t: float) -> float:
        return self.level

    def mean_over(self, t0: float, t1: float) -> float:
        return self.level


@dataclass(frozen=True)
class Sine:
    """``offset + amplitude * sin(2*pi*freq*t + phase)``."""

    amplitude: float = 1.0
    freq_hz: float = 1.0
    offset: float = 0.0
    phase: float = 0.0

    def value(self, t: float) -> float:
        return self.offset + self.amplitude * math.sin(
            2.0 * math.pi * self.freq_hz * t + self.phase
        )

    def mean_over(self, t0: float, t1: float) -> float:
        if t1 <= t0 or self.freq_hz == 0.0:
            return self.value(t0)
        w = 2.0 * math.pi * self.freq_hz
        integral = (
            -self.amplitude / w * (math.cos(w * t1 + self.phase) - math.cos(w * t0 + self.phase))
        )
        return self.offset + integral / (t1 - t0)


@dataclass(frozen=True)
class Square:
    """A duty-cycled pulse train — the classic IoT current signature.

    ``high`` for ``duty`` of each period, ``low`` for the remainder.
    """

    low: float = 0.0
    high: float = 1.0
    freq_hz: float = 1.0
    duty: float = 0.5

    def value(self, t: float) -> float:
        if self.freq_hz <= 0.0:
            return self.low
        phase = (t * self.freq_hz) % 1.0
        return self.high if phase < self.duty else self.low

    def mean_over(self, t0: float, t1: float) -> float:
        # Exact only over whole periods; tests should span many periods.
        return self.low + (self.high - self.low) * self.duty


@dataclass(frozen=True)
class Ramp:
    """Linear sweep from ``start`` to ``stop`` over ``duration_s``, then holds."""

    start: float = 0.0
    stop: float = 1.0
    duration_s: float = 1.0

    def value(self, t: float) -> float:
        if self.duration_s <= 0.0:
            return self.stop
        frac = min(max(t / self.duration_s, 0.0), 1.0)
        return self.start + (self.stop - self.start) * frac

    def mean_over(self, t0: float, t1: float) -> float:
        if t1 <= t0:
            return self.value(t0)
        n = 512
        step = (t1 - t0) / n
        return sum(self.value(t0 + i * step) for i in range(n)) / n


class Steps:
    """A piecewise-constant schedule: ``[(duration_s, level), ...]``.

    Holds the final level once the schedule is exhausted. Useful for
    simulating a discharge sweep or a phased test scenario.
    """

    def __init__(self, steps: Sequence[tuple[float, float]]) -> None:
        if not steps:
            raise ValueError("Steps requires at least one (duration_s, level) pair")
        self._steps = list(steps)
        self._total = sum(d for d, _ in self._steps)

    def value(self, t: float) -> float:
        if t < 0:
            return self._steps[0][1]
        acc = 0.0
        for duration, level in self._steps:
            acc += duration
            if t < acc:
                return level
        return self._steps[-1][1]

    def mean_over(self, t0: float, t1: float) -> float:
        if t1 <= t0:
            return self.value(t0)
        n = 1024
        step = (t1 - t0) / n
        return sum(self.value(t0 + i * step) for i in range(n)) / n

    @property
    def total_duration_s(self) -> float:
        return self._total


class OhmicLoad:
    """A resistive DUT: current follows the applied voltage.

    Lets a test close the loop — set 3.3 V on the simulated SMU through a
    330 Ω load and the current channel reports 10 mA. That is what makes the
    battery ``Emulator`` exercisable end to end without hardware.
    """

    def __init__(self, resistance_ohm: float = 330.0) -> None:
        if resistance_ohm <= 0:
            raise ValueError("resistance must be positive")
        self.resistance_ohm = resistance_ohm
        self._voltage = 0.0

    def set_voltage(self, volts: float) -> None:
        self._voltage = volts

    def value(self, t: float) -> float:
        return self._voltage / self.resistance_ohm

    def mean_over(self, t0: float, t1: float) -> float:
        return self.value(t0)

"""Battery life calculator — duty-cycle discharge simulator.

Replaces Qoitech's Battery Life Calculator on top of benchctrl's open
profile format. Pure-Python; no SMU connection required.

Two estimators:

- :func:`estimate_life_constant_current` — analytic. Treats the battery
  as a flat-voltage reservoir of ``capacity_mAh``. Optionally subtracts
  a self-discharge fraction. Good for first-pass back-of-envelope
  numbers.

- :func:`estimate_life_from_profile` — iterative against a
  :class:`benchctrl.battery.BatteryProfile`. Tracks used capacity per
  duty cycle, looks up open-circuit voltage from the profile's
  discharge curve, stops when OCV drops below cutoff. Accounts for the
  declining voltage over discharge, which a simple ``capacity ÷ current``
  calculation misses.

Both estimators accept a :class:`DutyCycle` describing an active/sleep
load pattern. :func:`duty_cycle_from_recording` extracts a DutyCycle
from an benchctrl :class:`Recording` by averaging main current over two
user-selected time windows — the same workflow as Otii's "Get from
selection" button.

Example:

    >>> from benchctrl.battery import BatteryProfile
    >>> from benchctrl.battery.calculator import DutyCycle, estimate_life_from_profile
    >>> profile = BatteryProfile.load("CR2032-Energizer-(25).json")
    >>> duty = DutyCycle(
    ...     active_current_A=0.020,     # 20 mA when transmitting
    ...     active_time_s=0.5,           # half a second
    ...     sleep_current_A=0.000005,    # 5 µA when idle
    ...     sleep_time_s=60.0,           # one minute
    ... )
    >>> est = estimate_life_from_profile(profile=profile, duty_cycle=duty)
    >>> est.runtime_human
    '...'
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from benchctrl.exceptions import BenchValueError

from benchctrl.battery.profile import BatteryProfile

if TYPE_CHECKING:
    from benchctrl.recording import Recording


# ---------------------------------------------------------------------------
# Duty cycle
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class DutyCycle:
    """Alternating active + sleep load pattern.

    Attributes:
        active_current_A: average current draw during the active phase.
        active_time_s: duration of the active phase per cycle.
        sleep_current_A: average current draw during the sleep phase.
        sleep_time_s: duration of the sleep phase per cycle.
    """

    active_current_A: float
    active_time_s: float
    sleep_current_A: float
    sleep_time_s: float

    def __post_init__(self) -> None:
        if self.active_time_s < 0 or self.sleep_time_s < 0:
            raise BenchValueError("durations must be >= 0")
        if self.active_time_s == 0 and self.sleep_time_s == 0:
            raise BenchValueError("at least one of active_time_s / sleep_time_s must be > 0")

    @property
    def cycle_time_s(self) -> float:
        """Total time for one active+sleep cycle (s)."""
        return self.active_time_s + self.sleep_time_s

    @property
    def cycle_charge_C(self) -> float:
        """Charge consumed per cycle (Coulombs = A·s)."""
        return (
            self.active_current_A * self.active_time_s
            + self.sleep_current_A * self.sleep_time_s
        )

    @property
    def average_current_A(self) -> float:
        """Cycle-averaged current draw (A)."""
        if self.cycle_time_s <= 0:
            return 0.0
        return self.cycle_charge_C / self.cycle_time_s


# ---------------------------------------------------------------------------
# Result
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LifeEstimate:
    """Output of a battery-life simulation.

    Attributes:
        runtime_s: estimated battery life (s).
        runtime_human: same, formatted as "5 days 3 hours 22 minutes".
        iterations: number of complete duty cycles before depletion.
        capacity_consumed_mAh: total capacity used (active + sleep + self-discharge).
        average_current_A: duty-cycle-averaged current.
        average_current_mA: same, in mA.
        self_discharge_loss_mAh: capacity lost to self-discharge over the run.
        safety_margin_loss_mAh: capacity reserved by the safety margin.
        final_voltage_V: voltage at end of run (None for constant-current method).
        method: which estimator produced this result.
        stop_reason: short string explaining why the simulation terminated.
    """

    runtime_s: float
    runtime_human: str
    iterations: int
    capacity_consumed_mAh: float
    average_current_A: float
    average_current_mA: float
    self_discharge_loss_mAh: float
    safety_margin_loss_mAh: float
    final_voltage_V: Optional[float]
    method: str
    stop_reason: str

    def to_dict(self) -> dict:
        return {
            "runtime_s": self.runtime_s,
            "runtime_human": self.runtime_human,
            "iterations": self.iterations,
            "capacity_consumed_mAh": self.capacity_consumed_mAh,
            "average_current_A": self.average_current_A,
            "average_current_mA": self.average_current_mA,
            "self_discharge_loss_mAh": self.self_discharge_loss_mAh,
            "safety_margin_loss_mAh": self.safety_margin_loss_mAh,
            "final_voltage_V": self.final_voltage_V,
            "method": self.method,
            "stop_reason": self.stop_reason,
        }


# ---------------------------------------------------------------------------
# Formatting helper
# ---------------------------------------------------------------------------


def _humanize_seconds(seconds: float) -> str:
    """Render a duration as ``5 days 3 hours 22 minutes 14 seconds``.

    Always at least one term; truncates trailing zeros.
    """
    if seconds < 0:
        return "0 seconds"
    s = int(seconds)
    parts: list[str] = []
    for value, label in (
        (60 * 60 * 24 * 365, "year"),
        (60 * 60 * 24, "day"),
        (60 * 60, "hour"),
        (60, "minute"),
        (1, "second"),
    ):
        if s >= value:
            n, s = divmod(s, value)
            suffix = "" if n == 1 else "s"
            parts.append(f"{n} {label}{suffix}")
    if not parts:
        return "0 seconds"
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Constant-current estimator
# ---------------------------------------------------------------------------


def estimate_life_constant_current(
    *,
    capacity_mAh: float,
    duty_cycle: DutyCycle,
    self_discharge_per_month_pct: float = 0.0,
    safety_margin_pct: float = 0.0,
) -> LifeEstimate:
    """Analytic estimator — treats the cell as a flat-voltage charge reservoir.

    Args:
        capacity_mAh: cell nominal capacity in mAh.
        duty_cycle: the load pattern.
        self_discharge_per_month_pct: cell's self-discharge as a percentage
            of total capacity per month (typical: 0.5% for Li-ion,
            20-30% for NiMH). Default 0 (no self-discharge term).
        safety_margin_pct: percentage of nominal capacity to reserve as a
            safety buffer. Default 0.

    Doesn't use a discharge curve — fastest, suitable when you only know
    a capacity and average current.
    """
    if capacity_mAh <= 0:
        raise BenchValueError(f"capacity_mAh must be > 0, got {capacity_mAh}")
    if safety_margin_pct < 0 or safety_margin_pct >= 100:
        raise BenchValueError(
            f"safety_margin_pct must be in [0, 100), got {safety_margin_pct}"
        )
    if self_discharge_per_month_pct < 0:
        raise BenchValueError("self_discharge_per_month_pct must be >= 0")

    safety_loss_mAh = capacity_mAh * (safety_margin_pct / 100.0)
    usable_mAh = capacity_mAh - safety_loss_mAh
    avg_I_mA = duty_cycle.average_current_A * 1000.0

    if avg_I_mA == 0 and self_discharge_per_month_pct == 0:
        # No drain at all — infinite runtime
        return LifeEstimate(
            runtime_s=float("inf"),
            runtime_human="infinite",
            iterations=0,
            capacity_consumed_mAh=0.0,
            average_current_A=0.0,
            average_current_mA=0.0,
            self_discharge_loss_mAh=0.0,
            safety_margin_loss_mAh=safety_loss_mAh,
            final_voltage_V=None,
            method="constant_current",
            stop_reason="no drain",
        )

    # Self-discharge expressed as an equivalent constant current (mA)
    sd_mA = (
        self_discharge_per_month_pct / 100.0 * capacity_mAh / (30.0 * 24.0)
    )  # mAh/month -> mAh/h = mA
    total_drain_mA = avg_I_mA + sd_mA

    runtime_h = usable_mAh / total_drain_mA
    runtime_s = runtime_h * 3600.0

    sd_loss_mAh = sd_mA * runtime_h
    iterations = (
        int(runtime_s / duty_cycle.cycle_time_s)
        if duty_cycle.cycle_time_s > 0
        else 0
    )

    return LifeEstimate(
        runtime_s=runtime_s,
        runtime_human=_humanize_seconds(runtime_s),
        iterations=iterations,
        capacity_consumed_mAh=usable_mAh,
        average_current_A=duty_cycle.average_current_A,
        average_current_mA=avg_I_mA,
        self_discharge_loss_mAh=sd_loss_mAh,
        safety_margin_loss_mAh=safety_loss_mAh,
        final_voltage_V=None,
        method="constant_current",
        stop_reason="usable capacity exhausted",
    )


# ---------------------------------------------------------------------------
# Profile-based estimator
# ---------------------------------------------------------------------------


def estimate_life_from_profile(
    *,
    profile: BatteryProfile,
    duty_cycle: DutyCycle,
    temperature: Optional[float] = None,
    self_discharge_per_month_pct: float = 0.0,
    safety_margin_pct: float = 0.0,
    cutoff_voltage: Optional[float] = None,
    max_iterations: int = 10_000_000,
) -> LifeEstimate:
    """Iterative estimator using the battery's discharge curve.

    Steps the discharge one duty cycle at a time, accumulates used
    capacity, and stops when the open-circuit voltage from the profile
    drops to or below ``cutoff_voltage`` (or when the usable capacity is
    exhausted, or when ``max_iterations`` is reached).

    Args:
        profile: a :class:`BatteryProfile`.
        duty_cycle: the load pattern.
        temperature: which temperature table to use (defaults to the
            single table if there's only one).
        self_discharge_per_month_pct: self-discharge rate (% of nominal
            capacity per month). Default 0.
        safety_margin_pct: percentage of nominal capacity reserved as a
            safety buffer. Default 0.
        cutoff_voltage: stop when OCV drops to or below this. Defaults to
            the profile's own cutoff (``profile.cutoff_voltage``).
        max_iterations: hard cap on duty cycles (default 10 million).

    More accurate than the constant-current estimator when the cell's
    voltage drops significantly over discharge or when ESR rises near
    end-of-life. Matches Otii's Battery Life Calculator semantics.
    """
    if duty_cycle.cycle_time_s <= 0:
        raise BenchValueError("DutyCycle.cycle_time_s must be > 0")
    if safety_margin_pct < 0 or safety_margin_pct >= 100:
        raise BenchValueError(
            f"safety_margin_pct must be in [0, 100), got {safety_margin_pct}"
        )
    if self_discharge_per_month_pct < 0:
        raise BenchValueError("self_discharge_per_month_pct must be >= 0")

    table = profile.select_table(temperature=temperature)
    if not table.table:
        raise BenchValueError("selected discharge table is empty")

    if cutoff_voltage is None:
        cutoff_voltage = (
            profile.cutoff_voltage
            if profile.cutoff_voltage is not None
            else 0.0
        )

    nominal_mAh = profile.nominal_capacity_mAh
    if nominal_mAh <= 0:
        raise BenchValueError(f"profile nominal capacity must be > 0 mAh, got {nominal_mAh}")

    safety_loss_mAh = nominal_mAh * (safety_margin_pct / 100.0)
    usable_mAh = nominal_mAh - safety_loss_mAh

    # Charge per cycle (C) -> mAh
    cycle_charge_mAh = duty_cycle.cycle_charge_C / 3.6
    sd_per_second_mAh = (
        self_discharge_per_month_pct / 100.0 * nominal_mAh / (30.0 * 24.0 * 3600.0)
    )
    sd_per_cycle_mAh = sd_per_second_mAh * duty_cycle.cycle_time_s

    used_mAh = 0.0
    sd_loss_mAh = 0.0
    total_time_s = 0.0
    iterations = 0
    stop_reason = "max_iterations reached"

    # Cap below the cell's last measured capacity — we can't model beyond it
    last_measurable_cap = table.capacity_extent[1]

    for _ in range(max_iterations):
        used_mAh += cycle_charge_mAh
        used_mAh += sd_per_cycle_mAh
        sd_loss_mAh += sd_per_cycle_mAh
        total_time_s += duty_cycle.cycle_time_s
        iterations += 1

        if used_mAh >= usable_mAh:
            stop_reason = "usable capacity exhausted"
            break

        # Check voltage cutoff via the profile's curve
        sample_cap = min(used_mAh, last_measurable_cap)
        ocv_now = table.ocv_at(sample_cap)
        if ocv_now <= cutoff_voltage:
            stop_reason = f"OCV dropped to cutoff ({cutoff_voltage:.3f} V)"
            break

    final_voltage = table.ocv_at(min(used_mAh, last_measurable_cap))

    return LifeEstimate(
        runtime_s=total_time_s,
        runtime_human=_humanize_seconds(total_time_s),
        iterations=iterations,
        capacity_consumed_mAh=used_mAh,
        average_current_A=duty_cycle.average_current_A,
        average_current_mA=duty_cycle.average_current_A * 1000.0,
        self_discharge_loss_mAh=sd_loss_mAh,
        safety_margin_loss_mAh=safety_loss_mAh,
        final_voltage_V=final_voltage,
        method="profile",
        stop_reason=stop_reason,
    )


# ---------------------------------------------------------------------------
# DutyCycle extraction from a Recording
# ---------------------------------------------------------------------------


def duty_cycle_from_recording(
    rec: "Recording",
    *,
    active_window: tuple[float, float],
    sleep_window: tuple[float, float],
    channel: str = "mc",
) -> DutyCycle:
    """Derive a :class:`DutyCycle` from a captured Recording.

    Averages the channel's current (defaults to ``mc`` — main current)
    over the two user-selected time windows. Direct analogue of Otii's
    "Get from selection" workflow: you mark the active region and the
    sleep region in a captured run, the tool computes the average current
    in each.

    Args:
        rec: an benchctrl :class:`Recording`.
        active_window: ``(start_s, end_s)`` for the active phase.
        sleep_window: ``(start_s, end_s)`` for the sleep phase.
        channel: which channel to read (defaults to ``"mc"``).

    Times in the returned DutyCycle reflect the *windows' durations*
    — i.e. the cycle structure you observed in the recording, not an
    abstract duty cycle from a datasheet.
    """
    from benchctrl import Channel

    ch = Channel.coerce(channel)
    if ch not in rec:
        raise BenchValueError(f"channel {ch.code!r} not present in recording")

    a_stats = rec.statistics(ch, start=active_window[0], end=active_window[1])
    s_stats = rec.statistics(ch, start=sleep_window[0], end=sleep_window[1])

    if a_stats.sample_count == 0:
        raise BenchValueError(
            f"active_window {active_window} has no samples on {ch.code}"
        )
    if s_stats.sample_count == 0:
        raise BenchValueError(
            f"sleep_window {sleep_window} has no samples on {ch.code}"
        )

    return DutyCycle(
        active_current_A=a_stats.average,
        active_time_s=active_window[1] - active_window[0],
        sleep_current_A=s_stats.average,
        sleep_time_s=sleep_window[1] - sleep_window[0],
    )

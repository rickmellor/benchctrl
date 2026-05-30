"""Battery profiler — orchestrates a real-cell discharge to build a profile.

Replaces Qoitech's Battery Profiler on top of benchctrl's existing wire
vocabulary (CC mode, recording, voltage measurement). The result is a
:class:`benchctrl.battery.BatteryProfile` that round-trips through Otii
and benchctrl's other battery tools.

How it works
------------
The cell is alternately loaded between two states:

- **High load** — draws ``discharge_profile.high.value`` for
  ``discharge_profile.high.time`` seconds. During this step we measure
  the on-load voltage and current, used to compute ESR.
- **Low load** — draws ``discharge_profile.low.value`` for
  ``discharge_profile.low.time`` seconds. After a short relaxation
  window we measure the (near-)open-circuit voltage = the cell's OCV.

Each iteration produces one ``(voltage, resistance, capacity)`` sample.
The loop stops when an exit condition fires:

- ``exitConditions.iterations`` reached
- OCV drops to or below ``exitConditions.ocv``
- On-load voltage drops to or below ``exitConditions.voltage``
- The caller calls :py:meth:`Profiler.abort`

Timing
------
Step transitions and measurements are issued over USB and bounded by
~ms-scale latency. Phase 3 enforces a minimum step duration of 50 ms
to keep measurements reliable; profiles with shorter high-pulse times
(e.g. Otii's CR2032 default of 2 ms) need lower-level firmware timing
and are rejected with a clear error. Use the matching duty-cycle
equivalent at slower rates for now.

Hardware
--------
The profiler issues only commands benchctrl already speaks (``set_main_current``,
``set_output``, recording, statistics). It does NOT require the licensed
Battery Toolbox features and is interoperable with both Arc and Ace Pro.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Callable, Optional

from benchctrl.battery.profile import (
    Battery,
    BatteryProfile,
    DeviceInfo,
    DischargeProfile,
    DischargeSample,
    DischargeTable,
)
from benchctrl.exceptions import SMUValueError

if TYPE_CHECKING:
    from benchctrl.device import SMU

log = logging.getLogger("benchctrl.battery.profiler")

# Minimum supported step duration; below this, USB-driven step changes
# are not reliably bounded.
MIN_STEP_TIME_S = 0.05

# Number of seconds to relax at low load before measuring the
# (near-)open-circuit voltage.
DEFAULT_RELAXATION_S = 0.5

# Window over which to average voltage/current readings on each step.
DEFAULT_MEAS_WINDOW_S = 0.1


@dataclass
class ProfilerConfig:
    """All inputs for one profiler run.

    Attributes:
        discharge_profile: high/low load definition + exit conditions
            (an :class:`benchctrl.battery.DischargeProfile`).
        battery: physical metadata for the cell being profiled (capacity,
            nominal voltage, manufacturer, model, size).
        temperature: ambient temperature during measurement (°C).
        temperature_unit: kept for round-trip compatibility.
        relaxation_time_s: rest at low load before measuring OCV. Bigger
            = better OCV accuracy, but a longer profiling run.
        measurement_window_s: window over which to average V/I readings
            for stable per-step values.
        progress_throttle_s: minimum interval between progress callbacks.
        initial_settle_time_s: rest at zero load before the first cycle.
        sample_cap: hard cap on number of samples (= cycles) to capture
            in case exit conditions never fire.
    """

    discharge_profile: DischargeProfile
    battery: Battery
    temperature: float = 25.0
    temperature_unit: str = "°C"
    relaxation_time_s: float = DEFAULT_RELAXATION_S
    measurement_window_s: float = DEFAULT_MEAS_WINDOW_S
    progress_throttle_s: float = 1.0
    initial_settle_time_s: float = 2.0
    sample_cap: int = 50_000


@dataclass(frozen=True)
class ProfilerSample:
    """One iteration of a profiling run.

    Attributes:
        iteration: 1-based cycle number.
        timestamp_s: wall time since profile start when the cycle ended.
        voltage_ocv: relaxed open-circuit voltage at end of low step.
        voltage_loaded: average voltage during the high-load step.
        current_loaded: average current during the high-load step.
        resistance: ESR computed from (OCV - V_loaded) / I_loaded.
        capacity_consumed_mAh: cumulative used capacity at end of cycle.
    """

    iteration: int
    timestamp_s: float
    voltage_ocv: float
    voltage_loaded: float
    current_loaded: float
    resistance: float
    capacity_consumed_mAh: float


@dataclass
class ProfilerResult:
    """Final output of a profiling run."""

    profile: BatteryProfile
    samples: list[ProfilerSample]
    runtime_s: float
    stop_reason: str
    aborted: bool = False


# ---------------------------------------------------------------------------
# Profiler
# ---------------------------------------------------------------------------


class Profiler:
    """Drives a connected battery through a discharge profile and records
    voltage / resistance / capacity samples.

    Usage:

        from benchctrl import SMU
        from benchctrl.battery import (
            Battery, DischargeProfile, DischargeStep, ExitConditions,
        )
        from benchctrl.battery.profiler import Profiler, ProfilerConfig

        config = ProfilerConfig(
            discharge_profile=DischargeProfile(
                low=DischargeStep("current", 0.001, 60.0),
                high=DischargeStep("current", 0.020, 0.5),
                exit_conditions=ExitConditions(iterations=0, ocv=2.7, voltage=2.5),
            ),
            battery=Battery(
                capacity=1000.0, capacity_unit="mAh",
                voltage=3.7, voltage_unit="V",
                manufacturer="Cellmaker", model="LP-1000",
            ),
            temperature=25.0,
        )

        with SMU.open() as smu:
            profiler = Profiler(smu, config)
            result = profiler.run(progress=lambda s: print(s))
            result.profile.save("LP-1000-(25).json")
    """

    def __init__(self, smu: "SMU", config: ProfilerConfig) -> None:
        self.smu = smu
        self.config = config
        self._aborted = False
        self._validate()

    # --- public lifecycle ------------------------------------------------

    def abort(self) -> None:
        """Request the running profiler to stop after the current cycle.

        Thread-safe to call from another thread or from a progress
        callback. The run will return a :class:`ProfilerResult` with
        ``aborted=True`` and ``stop_reason="aborted"``.
        """
        self._aborted = True

    def run(
        self,
        progress: Optional[Callable[[ProfilerSample], None]] = None,
    ) -> ProfilerResult:
        """Execute the discharge cycle and return the resulting profile."""
        from benchctrl import Channel

        dp = self.config.discharge_profile
        high = dp.high
        low = dp.low
        exit_conditions = dp.exit_conditions

        samples: list[ProfilerSample] = []
        t_start = time.monotonic()
        last_progress = 0.0
        capacity_consumed_C = 0.0  # total charge drawn (Coulombs)

        # Settle the cell at zero load before starting
        self.smu.set_range("low")  # alkaline / Li-ion etc. all fit
        self.smu.set_current_limit_enabled(True)
        self.smu.set_power_regulation("current")  # CC mode for sink
        self.smu.set_main_current(0.0)
        self.smu.set_output(True)
        time.sleep(self.config.initial_settle_time_s)

        stop_reason = "max iterations reached"
        iteration = 0
        try:
            for iteration in range(1, self.config.sample_cap + 1):
                if self._aborted:
                    stop_reason = "aborted"
                    break

                # --- High load step ---------------------------------
                self._apply_step(high)
                # Wait the full step time, then read measurement window
                self._wait_at_least(high.time - self.config.measurement_window_s)
                v_loaded, i_loaded = self._measure_v_i(
                    Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT,
                    window_s=self.config.measurement_window_s,
                )
                # Total charge from this step
                capacity_consumed_C += abs(i_loaded) * high.time

                # --- Low load step ---------------------------------
                self._apply_step(low)
                self._wait_at_least(
                    low.time - self.config.relaxation_time_s - self.config.measurement_window_s
                )
                # Relax then measure OCV
                self._wait_at_least(self.config.relaxation_time_s)
                v_ocv, i_low = self._measure_v_i(
                    Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT,
                    window_s=self.config.measurement_window_s,
                )
                capacity_consumed_C += abs(i_low) * (
                    low.time - self.config.relaxation_time_s - self.config.measurement_window_s
                )

                # --- ESR -------------------------------------------
                # i_loaded is negative (sinking from cell), so use |I| in
                # the divider. OCV > V_loaded under positive sink draw.
                if abs(i_loaded) < 1e-9:
                    esr = 0.0
                else:
                    esr = max(0.0, (v_ocv - v_loaded) / abs(i_loaded))

                capacity_consumed_mAh = capacity_consumed_C / 3.6  # C -> mAh

                sample = ProfilerSample(
                    iteration=iteration,
                    timestamp_s=time.monotonic() - t_start,
                    voltage_ocv=v_ocv,
                    voltage_loaded=v_loaded,
                    current_loaded=i_loaded,
                    resistance=esr,
                    capacity_consumed_mAh=capacity_consumed_mAh,
                )
                samples.append(sample)
                log.debug(
                    "iter=%d  OCV=%.4f V  V_load=%.4f V  I_load=%.4f A  ESR=%.4f ohm  cap=%.3f mAh",
                    iteration, v_ocv, v_loaded, i_loaded, esr, capacity_consumed_mAh,
                )

                # --- Progress callback (throttled) -----------------
                if progress is not None:
                    now = time.monotonic()
                    if now - last_progress >= self.config.progress_throttle_s:
                        try:
                            progress(sample)
                        except Exception:
                            log.warning("progress callback raised; continuing", exc_info=True)
                        last_progress = now

                # --- Exit conditions -------------------------------
                reason = self._check_exit(
                    sample, exit_conditions, iteration_count=iteration
                )
                if reason is not None:
                    stop_reason = reason
                    break
            else:
                stop_reason = "sample_cap reached"
        finally:
            try:
                self.smu.set_main_current(0.0)
                self.smu.set_output(False)
            except Exception:
                log.warning("failed to disable output during profiler teardown", exc_info=True)

        # Build the profile
        profile = self._build_profile(samples)
        return ProfilerResult(
            profile=profile,
            samples=samples,
            runtime_s=time.monotonic() - t_start,
            stop_reason=stop_reason,
            aborted=self._aborted,
        )

    # --- helpers --------------------------------------------------------

    def _validate(self) -> None:
        dp = self.config.discharge_profile
        for step_name, step in (("low", dp.low), ("high", dp.high)):
            if step.time < MIN_STEP_TIME_S:
                raise SMUValueError(
                    f"discharge_profile.{step_name}.time ({step.time}s) is below the "
                    f"profiler's minimum reliable step duration ({MIN_STEP_TIME_S}s). "
                    "Use a slower-cycling profile or capture this characterisation "
                    "in firmware-level mode (not yet supported)."
                )
            if step.value < 0:
                raise SMUValueError(
                    f"discharge_profile.{step_name}.value ({step.value}) must be >= 0"
                )
            if step.mode != "current":
                raise SMUValueError(
                    f"discharge_profile.{step_name}.mode is {step.mode!r}; "
                    "phase 3 of benchctrl's profiler supports only 'current' mode. "
                    "Power and resistance modes are tracked for later."
                )
        if self.config.battery.capacity <= 0:
            raise SMUValueError("battery.capacity must be > 0")

    def _apply_step(self, step) -> None:
        """Set the SMU to draw the configured step's load.

        The Arc Pro's CC wire command treats positive ``set_main_current``
        values as *source* (push current INTO the load) and negative
        values as *sink* (draw current FROM the load). Battery profiling
        is always sinking from the cell, so we negate the user-supplied
        positive "load magnitude" here.
        """
        self.smu.set_main_current(-abs(step.value))

    def _wait_at_least(self, seconds: float) -> None:
        if seconds > 0:
            time.sleep(seconds)

    def _measure_v_i(self, v_channel, i_channel, window_s: float) -> tuple[float, float]:
        """Average voltage and current over a short measurement window.

        Calls :py:meth:`SMU.read_window` to drain a window of inbound
        samples, then returns the mean of the **most recent half** on
        each channel. The most-recent-half trick gives the device's
        measurement time to settle after the prior load step took
        effect, avoiding stale carry-over.
        """
        try:
            samples = self.smu.read_window(
                [v_channel, i_channel], max(0.02, window_s)
            )
        except Exception:
            return float("nan"), float("nan")

        def _avg_latest(ch) -> float:
            vals = samples.get(ch, [])
            if not vals:
                return float("nan")
            tail = vals[max(0, len(vals) // 2):]
            return sum(tail) / len(tail)

        return _avg_latest(v_channel), _avg_latest(i_channel)

    def _check_exit(
        self,
        sample: ProfilerSample,
        exit_conditions,
        iteration_count: int,
    ) -> Optional[str]:
        """Return a stop reason string if any exit condition fires, else None."""
        if exit_conditions.iterations > 0 and iteration_count >= exit_conditions.iterations:
            return f"iteration limit ({exit_conditions.iterations}) reached"
        if exit_conditions.ocv > 0 and sample.voltage_ocv <= exit_conditions.ocv:
            return f"OCV cutoff ({exit_conditions.ocv:.3f} V)"
        if exit_conditions.voltage > 0 and sample.voltage_loaded <= exit_conditions.voltage:
            return f"loaded-voltage cutoff ({exit_conditions.voltage:.3f} V)"
        return None

    def _build_profile(self, samples: list[ProfilerSample]) -> BatteryProfile:
        """Assemble a BatteryProfile from the captured samples."""
        from benchctrl._version import __version__ as benchctrl_version

        device_info = DeviceInfo(
            type="Arc",
            id="",
            hardware_id="",
            firmware_version="",
        )
        # Best-effort: query the device for its real version + serial
        try:
            device_info.firmware_version = self.smu.get_fw_version()
            device_info.id = self.smu.get_device_id()
        except Exception:
            pass

        table = DischargeTable(
            table=[
                DischargeSample(
                    voltage=s.voltage_ocv,
                    resistance=s.resistance,
                    capacity=s.capacity_consumed_mAh,
                )
                for s in samples
            ],
            discharge_profile=self.config.discharge_profile,
            temperature=self.config.temperature,
            temperature_unit=self.config.temperature_unit,
            device=device_info,
            software_version=f"benchctrl/{benchctrl_version}",
        )
        return BatteryProfile(
            battery=self.config.battery,
            discharge_tables=[table],
        )

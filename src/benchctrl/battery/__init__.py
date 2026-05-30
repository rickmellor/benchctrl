"""Battery emulation, profiling, and life-estimation features.

A four-piece battery workflow built on benchctrl's existing wire
vocabulary and the :py:class:`SourceMeasurementUnit` Protocol — any
conforming driver can run these.

Phases:

- ``benchctrl.battery.profile`` — battery profile JSON format
  (Otii-compatible interchange) (v0.5.0)
- ``benchctrl.battery.calculator`` — battery life duty-cycle estimator
  (v0.6.0)
- ``benchctrl.battery.profiler`` — hardware discharge orchestration to
  build profiles from real batteries (v0.7.0)
- ``benchctrl.battery.emulator`` — host-side control loop that drives the
  SMU as a battery with realistic OCV + ESR sag (v0.8.0)
"""

from benchctrl.battery.calculator import (
    DutyCycle,
    LifeEstimate,
    duty_cycle_from_recording,
    estimate_life_constant_current,
    estimate_life_from_profile,
)
from benchctrl.battery.profile import (
    Battery,
    BatteryProfile,
    DeviceInfo,
    DischargeProfile,
    DischargeSample,
    DischargeStep,
    DischargeTable,
    ExitConditions,
)
from benchctrl.battery.emulator import (
    Emulator,
    EmulatorConfig,
    EmulatorState,
)
from benchctrl.battery.profiler import (
    Profiler,
    ProfilerConfig,
    ProfilerResult,
    ProfilerSample,
)

__all__ = [
    # profile (v0.5.0)
    "Battery",
    "BatteryProfile",
    "DeviceInfo",
    "DischargeProfile",
    "DischargeSample",
    "DischargeStep",
    "DischargeTable",
    "ExitConditions",
    # calculator (v0.6.0)
    "DutyCycle",
    "LifeEstimate",
    "duty_cycle_from_recording",
    "estimate_life_constant_current",
    "estimate_life_from_profile",
    # profiler (v0.7.0)
    "Profiler",
    "ProfilerConfig",
    "ProfilerResult",
    "ProfilerSample",
    # emulator (v0.8.0)
    "Emulator",
    "EmulatorConfig",
    "EmulatorState",
]

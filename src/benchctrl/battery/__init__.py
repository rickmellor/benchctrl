"""Battery emulation, profiling, and life-estimation features.

This subpackage replaces Qoitech's licensed "Battery Toolbox" entirely
on top of benchctrl's existing wire vocabulary — no extra licensing
required.

Phases:

- ``benchctrl.battery.profile`` — Otii-compatible JSON profile format
  (v0.5.0)
- ``benchctrl.battery.calculator`` — battery life duty-cycle estimator
  (v0.6.0, deferred)
- ``benchctrl.battery.profiler`` — hardware discharge orchestration to
  build profiles from real batteries (v0.7.0, deferred)
- ``benchctrl.battery.emulator`` — host-side control loop that drives the
  SMU as a battery with realistic OCV + ESR sag (v0.8.0, deferred)
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

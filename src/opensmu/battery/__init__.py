"""Battery emulation, profiling, and life-estimation features.

This subpackage replaces Qoitech's licensed "Battery Toolbox" entirely
on top of opensmu's existing wire vocabulary — no extra licensing
required.

Phases:

- ``opensmu.battery.profile`` — Otii-compatible JSON profile format
  (v0.5.0)
- ``opensmu.battery.calculator`` — battery life duty-cycle estimator
  (v0.6.0, deferred)
- ``opensmu.battery.profiler`` — hardware discharge orchestration to
  build profiles from real batteries (v0.7.0, deferred)
- ``opensmu.battery.emulator`` — host-side control loop that drives the
  SMU as a battery with realistic OCV + ESR sag (v0.8.0, deferred)
"""

from opensmu.battery.profile import (
    Battery,
    BatteryProfile,
    DeviceInfo,
    DischargeProfile,
    DischargeSample,
    DischargeStep,
    DischargeTable,
    ExitConditions,
)

__all__ = [
    "Battery",
    "BatteryProfile",
    "DeviceInfo",
    "DischargeProfile",
    "DischargeSample",
    "DischargeStep",
    "DischargeTable",
    "ExitConditions",
]

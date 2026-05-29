# Battery features

OpenSMU's `opensmu.battery` subpackage replaces Qoitech's licensed
**Battery Toolbox** entirely on top of the wire vocabulary we already
have. No extra licensing required — no separate server, no
"Cannot read properties of undefined (reading 'something')" errors,
no per-feature dongle.

## Why this exists

Otii ships a "Battery Toolbox" as a paid add-on covering four features:

| Otii feature | What it does |
|---|---|
| Battery Profile Manager | Browse / import / export battery profile JSON files |
| Battery Life Calculator | Duty-cycle simulator: estimate runtime given active/sleep currents |
| Battery Profiler | Discharge a real battery, measure V/I, build a profile |
| Battery Emulator | Make the device act as a battery (OCV + ESR sag) for DUT testing |

All four sit on top of capabilities the device exposes through wire
commands opensmu already speaks. The Battery Toolbox license gates
**access via the Otii server's API**, not the device itself — so we
can ship the same features as opensmu Python (and MCP tools) entirely
without it.

## Status — phased rollout

| Phase | Module | Status |
|---|---|---|
| 1 | `opensmu.battery.profile` — Otii-compatible JSON I/O + interpolation | **shipped (v0.5.0)** |
| 2 | `opensmu.battery.calculator` — duty-cycle life estimator | **shipped (v0.6.0)** |
| 3 | `opensmu.battery.profiler` — orchestrated hardware discharge | next |
| 4 | `opensmu.battery.emulator` — host-side OCV + ESR control loop | next |

## Phase 1 — `opensmu.battery.profile`

### Reads Otii's profile format directly

Tested against every profile that ships with Otii 3.7.2 (8 cells:
AA, AAA, CR123A, CR2, CR2032, plus a LiPo at three temperatures).
All eight load, round-trip through opensmu, and re-save as
bit-identical JSON. They re-import into Otii without complaint.

```python
from opensmu.battery import BatteryProfile

# Otii ships these at:
# C:\Users\<user>\AppData\Local\otii3\app-*\resources\batteryprofiles
profile = BatteryProfile.load("CR2032-Energizer-(25).json")

print(profile.battery.manufacturer)       # "Energizer"
print(profile.nominal_voltage)            # 3.0 V
print(profile.nominal_capacity_mAh)       # 230.2 mAh
print(profile.cutoff_voltage)             # 1.8 V

# OCV + ESR interpolation
print(profile.ocv_at(used_capacity_mAh=50.0))      # ~2.97 V
print(profile.esr_at(used_capacity_mAh=50.0))      # ~14.1 Ω
```

### Programmatically build profiles

```python
from opensmu.battery import (
    Battery, BatteryProfile, DischargeProfile, DischargeStep,
    DischargeSample, DischargeTable, ExitConditions,
)

profile = BatteryProfile(
    battery=Battery(
        capacity=1000.0, capacity_unit="mAh",
        voltage=3.7, voltage_unit="V",
        manufacturer="Cellmaker", model="LP-1000",
    ),
    discharge_tables=[
        DischargeTable(
            table=[
                DischargeSample(voltage=4.2, resistance=0.05, capacity=0.0),
                DischargeSample(voltage=3.7, resistance=0.10, capacity=500.0),
                DischargeSample(voltage=3.0, resistance=0.30, capacity=1000.0),
            ],
            discharge_profile=DischargeProfile(
                low=DischargeStep(mode="current", value=0.001, time=60.0),
                high=DischargeStep(mode="current", value=0.5, time=1.0),
                exit_conditions=ExitConditions(iterations=0, ocv=3.0, voltage=3.0),
            ),
            temperature=25.0,
        ),
    ],
)
profile.save("synthetic.json")
```

### Multi-temperature profiles

Otii ships multi-temperature batteries as **separate files** (LiPo at
−10/5/20 °C is three files). The data model supports both — you can
either keep them as separate `BatteryProfile` objects or merge into a
single profile with multiple `DischargeTable`s:

```python
ten_below = BatteryProfile.load("LiPo_ICP632136HPST-Renata-(-10).json")
plus_five = BatteryProfile.load("LiPo_ICP632136HPST-Renata-(5).json")
plus_twenty = BatteryProfile.load("LiPo_ICP632136HPST-Renata-(20).json")

# Merge into a single profile with three tables
merged = BatteryProfile(
    battery=plus_twenty.battery,
    discharge_tables=(
        ten_below.discharge_tables
        + plus_five.discharge_tables
        + plus_twenty.discharge_tables
    ),
)

# Select-by-temperature does the right thing
v = merged.ocv_at(used_capacity_mAh=200.0, temperature=0)   # nearest = 5°C
```

### MCP tools

Two file-based tools (no SMU connection needed):

- `battery_profile_summary(path)` — load + return summary
- `battery_profile_lookup(path, used_capacity_mAh, temperature=None)` —
  return interpolated OCV / ESR at a point

### File format

`opensmu.battery.profile` writes JSON in the exact shape Otii produces,
key for key:

```json
{
  "id": "<uuid>",
  "battery": {
    "capacity": 230.24, "capacityunit": "mAh",
    "voltage": 3.0, "voltageunit": "V",
    "manufacturer": "Energizer", "model": "CR2032",
    "size": "", "sizeunit": "mm"
  },
  "dischargetables": [
    {
      "id": "<uuid>",
      "temperature": 25, "temperatureunit": "°C",
      "dischargeprofile": {
        "low":  {"mode": "current", "value": 0.00025, "time": 60},
        "high": {"mode": "current", "value": 0.01,    "time": 0.002},
        "exitConditions": {"iterations": 0, "ocv": 1.8, "voltage": 1.8}
      },
      "device": {"type": "Arc", "id": "<uuid>", "hardwareId": "...", "firmwareVersion": "3.2.2"},
      "softwareVersion": "3.5.2",
      "table": [
        {"voltage": 3.223, "resistance": 8.904, "capacity": 0.004},
        ...
      ]
    }
  ]
}
```

Profiles produced by opensmu round-trip bit-identically through Otii,
so existing measurement data is interchangeable in both directions.

## Phase 2 — `opensmu.battery.calculator`

Pure-Python duty-cycle life estimator. Two methods:

### Constant-current estimator

Treats the cell as a flat-voltage charge reservoir. Fastest, no profile
needed.

```python
from opensmu.battery import DutyCycle, estimate_life_constant_current

duty = DutyCycle(
    active_current_A=0.020,    # 20 mA when transmitting
    active_time_s=0.1,          # for 100 ms
    sleep_current_A=5e-6,       # 5 µA idle
    sleep_time_s=60.0,          # for 60 s
)
est = estimate_life_constant_current(
    capacity_mAh=230.0,                # CR2032
    duty_cycle=duty,
    self_discharge_per_month_pct=1.0,  # typical lithium primary
    safety_margin_pct=10.0,
)
print(est.runtime_human)               # "208 days 5 hours 14 minutes 53 seconds"
print(est.average_current_mA)          # 0.0383 mA
print(est.iterations)                  # 360,388
print(est.self_discharge_loss_mAh)     # 15.98 mAh
```

### Profile-based estimator

Iterates the duty cycle against a battery profile's OCV-vs-capacity
curve. Stops when OCV drops to cutoff. More accurate when the cell's
voltage sags significantly over discharge.

```python
from opensmu.battery import BatteryProfile, DutyCycle, estimate_life_from_profile

profile = BatteryProfile.load("CR2032-Energizer-(25).json")
duty = DutyCycle(0.020, 0.1, 5e-6, 60.0)
est = estimate_life_from_profile(
    profile=profile,
    duty_cycle=duty,
    self_discharge_per_month_pct=1.0,
    safety_margin_pct=10.0,
    # cutoff_voltage defaults to profile.cutoff_voltage (1.8 V for CR2032)
)
print(est.runtime_human)               # similar to CC for flat profiles
print(est.final_voltage_V)             # voltage at end of run
print(est.stop_reason)                 # "OCV dropped to cutoff (1.800 V)"
                                       # or "usable capacity exhausted"
```

### Extracting a DutyCycle from a captured recording

Otii's "Get from selection" workflow: you observed an active region
and a sleep region in a captured run, the tool averages main current
over each window.

```python
from opensmu import Recording
from opensmu.battery import duty_cycle_from_recording, estimate_life_from_profile

rec = Recording.load("device-under-test.opensmu")
duty = duty_cycle_from_recording(
    rec,
    active_window=(1.250, 1.500),   # seconds — when the TX pulse fires
    sleep_window=(2.000, 60.000),   # the long idle period
)
est = estimate_life_from_profile(profile=profile, duty_cycle=duty)
```

### MCP tools

- `battery_life_estimate(capacity_mAh, active_current_A, active_time_s, sleep_current_A, sleep_time_s, ...)` —
  constant-current estimator
- `battery_life_estimate_from_profile(profile_path, ...)` — profile-based estimator
- `battery_life_from_recording(recording_path, active_window, sleep_window, profile_path=None, capacity_mAh=None, ...)` —
  end-to-end: load a saved capture, extract active/sleep currents, estimate

## Coming next

- **Phase 3 (`opensmu.battery.profiler`)** — orchestrate a real-cell
  discharge using opensmu's existing wire commands (`set_main_current`,
  recording, voltage measurement). Builds a profile JSON from a
  connected battery. ESR computed from step response.
- **Phase 4 (`opensmu.battery.emulator`)** — host-side control loop:
  read main current at native rate, compute
  `V = OCV(SoC) − I · ESR(SoC)` from the profile, write
  `set_main_voltage`. Makes the SMU behave as a battery for DUT testing.

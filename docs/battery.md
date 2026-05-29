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
| 2 | `opensmu.battery.calculator` — duty-cycle life estimator | next |
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

## Coming next

- **Phase 2 (`opensmu.battery.calculator`)** — pure-Python duty-cycle
  life estimator. Takes a profile + active/sleep current + active/sleep
  time, simulates discharge until cutoff. Matches Otii's Battery Life
  Calculator semantics. From-recording variant extracts active/sleep
  currents from a captured run.
- **Phase 3 (`opensmu.battery.profiler`)** — orchestrate a real-cell
  discharge using opensmu's existing wire commands (`set_main_current`,
  recording, voltage measurement). Builds a profile JSON from a
  connected battery. ESR computed from step response.
- **Phase 4 (`opensmu.battery.emulator`)** — host-side control loop:
  read main current at native rate, compute
  `V = OCV(SoC) − I · ESR(SoC)` from the profile, write
  `set_main_voltage`. Makes the SMU behave as a battery for DUT testing.

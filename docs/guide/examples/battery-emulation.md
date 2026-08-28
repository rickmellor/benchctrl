# Battery emulation

**The job:** make a DUT believe it is running on a specific cell, at a specific
state of charge and a specific temperature — without waiting for a real cell to
get there, and without wrecking one to find out.

This is the technique that answers questions a bench supply cannot:

- Does the device still transmit at 40 % state of charge, when the rail sags
  180 mV during every burst?
- Does its low-battery warning fire at the right point, or at the point where it
  has already stopped working?
- Does it brown out on a cold morning, when internal resistance is ten times
  what it was on your desk?

A bench supply says yes to all of these, because a bench supply does not sag.

## How it works

A host-side control loop drives a source-measure unit as a cell. Each tick, by
default at **100 Hz**:

1. read the current the DUT is drawing
2. integrate `I × dt` into used capacity, and from that a state of charge
3. look up `OCV(SoC)` and `ESR(SoC)` from a real cell's discharge curve
4. apply series/parallel multipliers
5. compute `V = OCV − I × ESR`, clamp to the safety cap
6. write that voltage

So the DUT sees a supply whose voltage depends on what the DUT itself is doing —
which is the only property that distinguishes a battery from a power supply.

## A profile is a measured cell, not a model

The profile is a JSON file holding a discharge table: open-circuit voltage and
internal resistance versus consumed capacity. Eight cell profiles ship with the
Otii desktop app (AA, AAA, CR123A, CR2, CR2032, and a LiPo at three
temperatures), all of which load, round-trip and re-save bit-identically here. You
can also generate your own against a real cell — see
[the profiler](#generating-a-profile-from-a-real-cell).

```python
from benchctrl.battery import BatteryProfile

p = BatteryProfile.load("CR2032-Energizer-(25).json")
p.nominal_capacity_mAh        # 230.2
p.cutoff_voltage              # 1.8
p.ocv_at(used_capacity_mAh=50.0)   # ~2.97 V
p.esr_at(used_capacity_mAh=50.0)   # ~14.1 Ω
```

That `esr_at` is where the interesting behaviour comes from. A CR2032's internal
resistance is around 9 Ω fresh and climbs as it discharges; that is what makes it
a poor choice for a radio.

## Running one

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.battery import BatteryProfile
from benchctrl.battery.emulator import Emulator, EmulatorConfig

profile = BatteryProfile.load("CR2032-Energizer-(25).json")
config = EmulatorConfig(
    profile=profile,
    initial_soc=1.0,             # start fresh
    series=1, parallel=1,
    soc_tracking=True,           # the cell drains as the DUT draws
    safety_max_voltage_V=3.5,    # hard cap on the output
    update_interval_s=0.01,      # 100 Hz
)

with OtiiArc.open() as smu:
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(600.0)                 # run the DUT against the cell
        st = emu.state()
        print(f"SoC {st.soc*100:.1f}%  used {st.used_capacity_mAh:.3f} mAh")
    finally:
        emu.stop()
```

Or from the command line, which is the right form for a quick check:

```bash
benchctrl --yes framework battery-emulator-start CR2032-Energizer-\(25\).json \
    --initial-soc 1.0 --safety-max-voltage-V 3.5
benchctrl framework battery-emulator-state
benchctrl framework battery-emulator-stop
```

Only one emulator runs at a time. `stop()` disables the output in a `finally`,
including on an exception.

### The four knobs that change the experiment

| Knob | Use it to |
|---|---|
| `initial_soc` | **start part-worn.** `initial_soc=0.4` is how you test low-battery behaviour in ten minutes instead of over a month |
| `soc_tracking=False` | hold state of charge fixed while still applying resistance sag — steady-state characterisation with one variable removed |
| `series` / `parallel` | packs. `series=N` multiplies OCV and ESR; `parallel=N` divides ESR and multiplies capacity |
| `temperature` (via profile choice) | the cold case, which is usually the failing one |

`initial_soc` is the one that changes what you can find out. Most low-battery
bugs live in the last 20 % of a cell's life, and that is exactly the region
nobody tests because getting there honestly takes weeks.

## What it reproduces, measured

Validated on this bench against a CR2032 profile (fresh OCV 3.224 V, ESR ≈ 9 Ω)
with a programmable resistance standard as the load:

| Load | Current drawn | Output voltage | Sag | Predicted (9 Ω · I) |
|---|---|---|---|---|
| 1 kΩ | 3.2 mA | 3.195 V | 28 mV | 29 mV |
| 100 Ω | 31 mA | 2.936 V | 288 mV | 279 mV |
| 12 Ω | 160 mA | **1.660 V** — under cutoff | 1.56 V | 1.44 V |
| 100 kΩ (recovery) | 30 µA | 3.146 V | — | new OCV at 99.9 % SoC |

Sag tracks the prediction within about 3 % across two decades of current, and the
12 Ω row is the useful one: the emulated CR2032 **collapses below its own cutoff
voltage**, which is what a real CR2032 does at 130 mA and what makes it the wrong
cell for a transmitting device.

Cell choice matters more than any of this, and it is visible directly. Same
harness, three cells, one 100 Ω burst:

| Cell | Sleep V | V at burst | Sag | Recovery |
|---|---|---|---|---|
| CR2032 | 3.224 | 3.038 | **186 mV** | ~5 s |
| CR123A | 3.200 | 3.190 | 10 mV | < 0.1 s |
| LiPo @ +20 °C | 4.20 * | 4.20 * | clamped | n/a |

A CR2032 and a CR123A are both "3 V lithium primaries" and they are not
interchangeable for anything that transmits. That comparison took an afternoon
and destroyed no cells.

### The cold case

The single most striking result on this bench is a temperature sweep of one LiPo
profile at a fixed load. Internal resistance rises roughly **10×** from +20 °C to
−10 °C, and at a 12 Ω load the cold cell sits at **3.24 V drawing 324 mA** —
nearly 500 mV below the warm case, and well into the bottom of the usable range.

If your product ships anywhere with a winter, run the cold profile. It is the
same command with a different file.

## Four limits to know before you trust it

**The output voltage ceiling.** The source-measure unit's high range caps at
about **4.2 V under load**, so a fresh single-cell LiPo (OCV ~4.31 V) clamps and
the emulated cell reads flat at the top of its curve. Single-cell chemistry at
moderate current is fine; multi-cell packs at high load are out of reach, and the
clamp is silent rather than an error — check `safety_max_voltage_V` against your
profile's fresh OCV before believing a flat top.

**100 Hz is not fast.** The loop handles steady state, slow transients and
ordinary sleep/wake cycles. It does **not** track resistance sag on a
sub-millisecond edge — a switching converter's ripple is invisible to it. That
needs a device-side regulator, which is not something a host-side loop can be.

**The emulator and a recording cannot run at once.** The control loop writes at
100 Hz while a recording's reader thread consumes the transport at ~4 kHz, and
the combination deadlocks within about 100 ms
([`KNOWN_LIMITATIONS.md`](../../../KNOWN_LIMITATIONS.md) A-1). The run engine
refuses the combination structurally rather than discovering it at 3 a.m.

The workaround, when you need a high-resolution capture of a burst under
emulation, is to **let the emulator settle, read the current OCV, stop the
emulator, pin the output at that voltage, then record.** Cell dynamics are off
during the capture window, which is acceptable for a short one — over ten seconds
at a 30 mA peak the state-of-charge change is about 0.08 mAh, under 0.04 % of a
CR2032 — and the saved artifact records the pinned voltage so the compromise is
recoverable from the data rather than remembered.

**It does not work across a network.** Each tick is two round trips; at 100 Hz
that is 0.6–2.0 s of traffic per second of wall clock, so the loop cannot keep
time, and a battery emulator that cannot keep time is not emulating a battery. Run
it where the instrument is — locally, or as a run spec with `"mode": "emulator"`
submitted to the bench host. The fix is not a faster network.

## Safety

Three stops, all worth setting:

- `safety_max_voltage_V` — an absolute clamp on the output. Set it just above your
  profile's fresh OCV, never at the instrument's ceiling.
- `safety_max_used_mAh` — stop when cumulative consumption reaches it. This is
  your protection against a DUT fault quietly draining an emulated cell all night.
- `soc_floor` — stop at a state of charge. Useful when you want the run to end at
  a defined point rather than at collapse.

Any of them stops the loop and disables the output.

## Generating a profile from a real cell

If you need a cell that is not in the bundled set, discharge one and record what
it does:

```python
from benchctrl.battery import (
    Battery, DischargeProfile, DischargeStep, ExitConditions,
    Profiler, ProfilerConfig,
)

config = ProfilerConfig(
    discharge_profile=DischargeProfile(
        low=DischargeStep("current", 0.001, 60.0),    # 1 mA relax, 60 s
        high=DischargeStep("current", 0.020, 0.5),    # 20 mA load, 500 ms
        exit_conditions=ExitConditions(iterations=0, ocv=2.7, voltage=2.5),
    ),
    battery=Battery(capacity=1000.0, capacity_unit="mAh",
                    voltage=3.0, voltage_unit="V",
                    manufacturer="…", model="…"),
    temperature=25.0,
)
with OtiiArc.open() as smu:
    result = Profiler(smu, config).run()
result.profile.save("my-cell-(25).json")
```

Each iteration loads the cell, measures the loaded voltage, relaxes it, measures
open-circuit voltage, and derives resistance from the difference. The output is
the same JSON format the bundled profiles use, so it loads in either tool.

**Check the duration before you start**:

```bash
benchctrl framework battery-profiler-estimate-duration 1000 0.020 0.5 0.001 60
```

A full discharge of a 1000 mAh cell at a low duty cycle is **days**, not hours.
That command exists so you find that out before committing the bench, and it is
also why profiling is a poor fit for an interactive agent session.

This is destructive: it discharges the cell to cutoff. Use one you are willing to
lose, and characterise at the temperature you care about — a profile is only
valid at its capture temperature, which is why the bundled LiPo ships as three
separate files.

## Next

- [Sleep and duty-cycle current](sleep-current.md) — measuring what the DUT draws
- [Power consumption characterization](power-characterization.md) — turning it into a life projection
- [Unattended runs](unattended-runs.md) — `"mode": "emulator"` in a phase, on the bench
- [`battery.md`](../../battery.md) — the full API, and the profile format

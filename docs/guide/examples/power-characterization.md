# Power consumption characterization

**The job:** turn *"how long will this run on a coin cell?"* from an argument
into a measured number, with the assumptions written down where someone can
disagree with them.

This page describes a campaign that has been run on this bench end to end: a
900 MHz FSK wireless room-temperature and presence sensor, normally powered by a
CR2477 coin cell, characterised over 24 hours from its first pairing burst
onward. The instrument settings below are the real ones. Where a figure is
illustrative rather than measured, it says so.

## The shape of the problem

A device like this spends more than 99 % of its life asleep and almost all of its
energy in short radio bursts. So the answer is decided by four numbers:

1. the **sleep current** — usually microamps, and hard to measure honestly
2. the **burst current and duration** — milliamps for milliseconds
3. how **often** a burst happens
4. the **cell's** usable capacity, which is not its printed capacity

Get sleep current wrong by 5 µA and a one-year estimate becomes six months. That
is why the whole method is built around measuring the floor properly rather than
around clever analysis.

## Why a battery emulator, not a battery

Power the device from a source-measure unit configured as a constant-voltage
source at the cell's nominal voltage, and measure current continuously.

Three reasons this beats a real cell:

- **The supply does not sag as the test runs**, so a change in current is the
  device changing behaviour rather than the cell running down. One variable at a
  time.
- **You can measure current in series without a shunt** to size, and without a
  burden voltage that browns out the DUT during a burst.
- **It is repeatable.** A second run is the same experiment; two real cells are
  not.

The cost is that you are *not* characterising the cell — you are characterising
the **load**. Cell behaviour comes back in at the projection step (below) and,
if you want the DUT to actually experience it, in
[Battery emulation](battery-emulation.md).

## The capture

Constant voltage, generous current limit, over-voltage protection just above,
low measurement range, and let it run:

```bash
python applications/sensor_profiler/capture.py \
    --dut room-temp-sensor \
    --label assoc-test \
    --voltage 3.0 \
    --current-limit 0.100 \
    --ovp 3.5 \
    --range low \
    --hours 24 \
    --chunk-minutes 60
```

Each setting is a decision:

| Setting | Why that value |
|---|---|
| `--voltage 3.0` | the cell's nominal, not its fresh open-circuit voltage. Characterise the device where it spends its life |
| `--current-limit 0.100` | comfortably above the radio burst, low enough to bound a fault |
| `--ovp 3.5` | just above the setpoint — catches a mis-typed voltage before the DUT does |
| `--range low` | the low range is what resolves microamps. This is the setting that decides whether the sleep number is real |
| `--hours 24` | long enough to catch a real duty cycle, including whatever happens hourly |
| `--chunk-minutes 60` | one file per hour, so a crash at hour 23 costs you an hour |

Current is captured at the instrument's native rate — about **4 kHz** — which is
what makes millisecond radio bursts resolvable at all. A 1 Hz logger would show
you an average and hide the entire mechanism.

**Trigger pairing after the capture starts**, not before. Association is usually
the most expensive thing the device ever does, and it is worth having in chunk 0
rather than reconstructing it from a datasheet.

The chunking is deliberate rather than incidental: 24 hours at 4 kHz is a lot of
samples, and one file per hour bounds both memory and the cost of a failure. It
is also why this application is **local-only by default** — rolling a 60-minute
chunk across a network is a very large transfer at every roll
([`KNOWN_LIMITATIONS.md`](../../../KNOWN_LIMITATIONS.md) § N-3). Run it on the
machine holding the cables, or lower `--chunk-minutes`.

## The analysis

```bash
python applications/sensor_profiler/analyze.py \
    --run applications/sensor_profiler/runs/<run_id>
```

Two decisions inside this step are the ones worth arguing about.

### Sleep current is the 10th percentile, not the minimum or the mean

The **mean** includes every burst, so it is not a sleep current at all. The
**minimum** is one sample, so it is whatever the noise floor did once. The
**median** gets dragged if the device is busy.

The 10th percentile of each chunk's current trace is a robust floor: it excludes
burst transients by construction, and it cannot be moved by a single outlier. The
run-level figure is the **median of the per-chunk values**, so one anomalous hour
does not set the number.

Say which statistic you used whenever you quote a sleep current. "3 µA" is not a
measurement until you say how you reduced 14 million samples to it.

### Bursts are detected with hysteresis, and the thresholds are the definition

- **enter at 1.0 mA, exit at 0.5 mA.** A single threshold chatters: current
  crossing 1 mA on the way down produces a dozen spurious events per real burst.
- **merge gaps under 5 ms.** A radio burst is often several packets with brief
  gaps; counting them separately triples the burst count and quarters the mean
  duration.
- **drop events under 0.5 ms.** Below that you are counting sampling artefacts.

These thresholds *define* what "a burst" means for your device. A device whose
sleep current is 2 mA needs different ones. Change them if you must, but record
what you used — two runs analysed with different thresholds are not comparable.

Per event you get start time, duration, peak, mean and charge. Per chunk you get
average, peak and sleep current, voltage min/max, charge and energy consumed, and
a burst count.

## From charge to a life projection

The honest version of this step is short, because most of the error is in the
assumptions rather than the arithmetic.

```bash
benchctrl framework battery-life-estimate \
    1000 0.020 0.1 5e-6 60 \
    --self-discharge-per-month-pct 1.0 \
    --safety-margin-pct 10
```

That is capacity in mAh, then active current and duration, then sleep current and
duration. The output includes average current, iteration count, and the
self-discharge loss broken out separately.

For a cell whose voltage sags meaningfully over its life, iterate against a real
discharge curve instead:

```bash
benchctrl framework battery-life-estimate-from-profile \
    CR2477-profile.json 0.020 0.1 5e-6 60 \
    --self-discharge-per-month-pct 1.0 \
    --safety-margin-pct 10
```

Same positional order as above with the profile path in place of the capacity —
the capacity comes from the profile.

and it stops when the open-circuit voltage reaches the profile's cutoff rather
than when the coulombs run out — with `stop_reason` saying which happened.

You can also go straight from the capture, which is the least hand-typed path:

```python
from benchctrl import Recording
from benchctrl.battery import duty_cycle_from_recording, estimate_life_from_profile

rec = Recording.load("data/sensor_004.opensmu")
duty = duty_cycle_from_recording(rec,
                                 active_window=(12.340, 12.415),   # a burst
                                 sleep_window=(20.0, 300.0))       # a quiet stretch
est = estimate_life_from_profile(profile=profile, duty_cycle=duty)
```

### Four caveats that belong next to the number

State these every time. A projection quoted without them will be treated as a
specification.

- **Cutoff voltage decides the answer more than capacity does.** A cell's printed
  1000 mAh is to some cutoff; if your device browns out above that cutoff, the
  capacity above it is all you get. This is frequently the largest single error in
  a life estimate.
- **Self-discharge is not negligible on multi-year claims.** At roughly 1 %/year
  for a lithium primary, a three-year projection loses about 3 % before the device
  draws anything — and the estimator reports that loss separately so you can see
  it.
- **Temperature changes everything.** Capacity and internal resistance are both
  temperature-dependent, and a coin cell in an unheated space in winter is not the
  cell you characterised at 20 °C. The measured LiPo case on this bench shows
  internal resistance rising about **10×** from +20 °C to −10 °C.
- **You measured one duty cycle.** If the device transmits more when a room is
  occupied, or retries when the link is poor, your 24 hours captured one
  behaviour. Say which.

## What good output looks like

The analysis writes a self-contained bundle per run — `summary.md`,
`per_chunk.csv`, `burst_events.csv`, and static plots. The point of it being
self-contained is that a projection is only useful if someone can check it: the
raw capture, the reduction and the conclusion travel together, and a reviewer who
disagrees with the burst thresholds can rerun with their own.

Report it as a **range with its assumptions**, not a single figure:

> ~14 months at the observed duty cycle (one report per 60 s, association at
> power-up), on a 1000 mAh CR2477 to a 2.0 V cutoff, at 20 °C, with 1 %/year
> self-discharge and a 10 % reserve. Sleep current 10th-percentile
> per hour, median across 24 hours.

*(That sentence is the shape to aim for; the specific numbers in it are
illustrative — the captured data for this campaign lives outside the repository.)*

Anyone can now argue with a specific assumption instead of with the conclusion,
which is the only kind of argument that improves the answer.

## Choosing the instrument for the regime

The instrument that is right for a sleep current is wrong for a load sweep, and
neither errors in the other's regime — the wrong one just returns a plausible
number. Measured on this bench:

| Regime | Use | Because |
|---|---|---|
| below ~1 mA | the passive resistance standard | an active electronic load loses regulation and reads near-open |
| ~30 mA steady | either | both regulate cleanly |
| above ~100 mA | the electronic load | the resistance standard's ~1 W cap forces too high a resistance |
| sub-millisecond transients | the electronic load | relay switching is 30–95 ms |

The concrete case: at a 10 kΩ step against a 3.20 V cell — 0.32 mA expected — the
resistance standard measured **0.322 mA** and the electronic load measured
**0.012 mA**, its own noise floor. Nothing raised an error. Wrong by 27×,
silently. Instrument choice is part of the measurement.

## Next

- [Sleep and duty-cycle current](sleep-current.md) — the microamp end in detail
- [Battery emulation](battery-emulation.md) — making the DUT experience the cell
- [Unattended runs](unattended-runs.md) — running this overnight as a declarative spec
- [`battery.md`](../../battery.md) — the profile, calculator, profiler and emulator APIs

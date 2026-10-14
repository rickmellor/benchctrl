# Sleep and duty-cycle current

**The job:** measure the microamps that decide whether a product lasts a year or
three months — and be able to defend the number.

This is the hardest measurement on the bench, not because the instrument is
difficult but because a plausible wrong answer is so easy to get. Almost every
sleep-current figure that turns out to be wrong is wrong for one of five reasons,
and all five are avoidable.

## The five ways this goes wrong

**1. The wrong measurement range.** A range that resolves amps does not resolve
microamps. Set `--range low` before you believe any small number. This is the
single most common cause of a sleep current that is "about zero" or that looks
like noise centred on nothing.

**2. Measuring the mean.** A 24-hour mean current includes every radio burst. It
is a useful number — it is what sets battery life — but it is not a sleep current,
and using it as one makes the sleep floor look ten times worse than it is.

**3. Measuring the minimum.** One sample, which is whatever the noise floor did
once. It flatters the device and does not reproduce.

**4. Not waiting for the device to actually sleep.** Many devices take seconds to
minutes to enter their deepest state — a radio finishing a retry, a filesystem
sync, a watchdog interval. Whatever you measure in the first few seconds after
boot is a device settling, not a device sleeping.

**5. Something else on the rail.** A programmer, a debug probe, a pull-up to
another board's supply, a level shifter. At the microamp level, a 100 kΩ path to
another rail is a large current. If your floor will not go below a few hundred
microamps, unplug things before you debug firmware.

## What to measure instead

**The 10th percentile of the current trace**, per chunk. It excludes burst
transients by construction and cannot be moved by a single outlier. Over a long
capture, take the **median of the per-chunk values**, so one anomalous hour does
not set the number.

Then quote the statistic with the number. "3.1 µA (10th percentile per hour,
median over 24 h)" is a measurement. "3 µA" is a claim.

## The capture

Continuous current at the instrument's native rate, at the voltage the device
actually runs at:

```bash
benchctrl arc set-range low
benchctrl arc set-current-limit 0.1
benchctrl arc set-voltage 3.0
benchctrl --yes arc enable-output --confirm-dut-attached on
benchctrl capture 300 sleep.opensmu -c mc mv
```

`--yes` is a global flag and goes before the device group; a trailing one is a
parse error rather than an authorisation.

Or in Python, when you want to reduce it in the same script:

```python
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.set_range("low")
    smu.set_current_limit(0.1)
    smu.set_voltage(3.0)
    smu.set_output(True)
    with smu.record(OtiiArcChannel.MAIN_CURRENT, OtiiArcChannel.MAIN_VOLTAGE) as rec:
        time.sleep(300.0)
    rec.save("sleep.opensmu")

vals = sorted(rec.data(OtiiArcChannel.MAIN_CURRENT))
p10 = vals[len(vals) // 10]
print(f"sleep floor: {p10*1e6:.2f} µA")
print(rec.statistics(OtiiArcChannel.MAIN_CURRENT))     # mean, min, max for context
```

**Discard the front of the trace.** Crop past whatever the device's settling time
is — `rec.crop(start, end)` does it in place — and say in your notes how much you
dropped and why.

Five minutes is enough to see a floor. Twenty-four hours is what you need to
catch a duty cycle, because devices do things hourly, and an hourly 40 mA burst
you did not capture is the difference between a one-year and a six-month product.

## Establishing your own noise floor first

Before you trust a microamp figure, measure the bench with **no DUT attached**.
Same voltage, same range, leads open.

Whatever that reads is your floor. A DUT reading below it has not been measured;
it has been rounded. This takes thirty seconds and it is the step that makes the
difference between a number you can defend and a number you hope is right.

The measured instrument-regime case from this bench makes the point concretely: at
a 10 kΩ load against a 3.20 V cell — 0.32 mA expected — a passive resistance
standard read **0.322 mA** while an active electronic load read **0.012 mA**,
which was its own noise floor. Neither instrument raised an error. The wrong
choice was wrong by 27× and looked entirely plausible.

**Below about 1 mA, use the passive resistance standard, not the active load.**
An active load's closed-loop regulation needs current to work with; below ~1 mA
its constant-resistance mode loses regulation and it reads as effectively open.

## Getting the duty cycle, not just the floor

Battery life is set by the average, and the average is the floor plus the bursts.
Detect bursts with **hysteresis**, never a single threshold:

- enter at 1.0 mA, exit at 0.5 mA
- merge gaps under 5 ms
- drop events under 0.5 ms

A single threshold chatters — current crossing it on the way down generates a
dozen spurious events per real burst. The merge rule matters because a radio
burst is often several packets with short gaps between them; counting them
separately triples the count and quarters the mean duration. Both numbers then
look precise and are wrong.

Those thresholds are the *definition* of a burst for your device. Record them.
Two runs analysed with different thresholds are not comparable.

Then take it straight to a life estimate:

```python
from benchctrl.battery import duty_cycle_from_recording, estimate_life_constant_current

duty = duty_cycle_from_recording(rec,
                                 active_window=(12.340, 12.415),
                                 sleep_window=(20.0, 300.0))
est = estimate_life_constant_current(capacity_mAh=1000.0, duty_cycle=duty,
                                     self_discharge_per_month_pct=1.0,
                                     safety_margin_pct=10.0)
print(est.runtime_human, est.average_current_mA)
```

`duty_cycle_from_recording` averages current over each window, so pick the windows
from a plot rather than from memory — `benchctrl framework plot-recording` renders
one.

For a long unattended capture with the reduction already built in, the profiler
application does all of the above per hour and writes a report bundle; see
[Power consumption characterization](power-characterization.md).

## Cross-checking with a second instrument

At the microamp level, one instrument's word is thin evidence. If the number
matters — a specification, a product decision, a datasheet claim — get a second,
independent path to it.

A bench multimeter in series, with `null-now` taken on the leads first so lead and
contact resistance is out of the reading, is an independent measurement with a
different failure mode. Two instruments agreeing is meaningfully stronger than one
instrument being precise.

One trap to know: on that multimeter, the `measure-*` commands **reconfigure the
instrument before triggering**, which discards an active null. After nulling, use
`read` or `read-nulled`. This is the class of mistake that matters most with a
meter — it returns a plausible wrong number rather than an error.

Nulling is also how the bench's own consistency was checked: a 100 Ω standard
reads 100.038 Ω on the multimeter, 4-wire, against 100.0425 Ω from the
programmable resistance standard's own reporting. Agreement at that level is what
lets you believe either one alone later.

## Reporting it

Everything a reader needs to disagree with you:

> **Sleep current 3.1 µA** — 10th percentile per hour, median across 24 h, at
> 3.0 V, low range, 20 °C, first 60 s of each chunk discarded. Bench floor with
> leads open: 0.4 µA. Duty cycle over the same window: one 18 ms burst peaking at
> 22 mA per 60 s.

That paragraph is longer than "3 µA" and it is the difference between a
measurement and an assertion. Someone who thinks your burst thresholds are wrong
can now say so specifically, and rerun with their own.

## Next

- [Power consumption characterization](power-characterization.md) — the 24-hour campaign this feeds
- [Battery emulation](battery-emulation.md) — measuring the same device on a sagging cell
- [Unattended runs](unattended-runs.md) — a 24-hour capture you can walk away from
- [Bringing up a board](board-bringup.md) — if the floor is higher than it should be

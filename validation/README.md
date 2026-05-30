# Bench validation scenarios

End-to-end hardware tests for the OpenSMU battery emulator. Each
scenario drives an Otii Arc Pro (acting as a virtual cell) into an
Eastwood QR10x programmable resistor (acting as the DUT) and saves
the captured response to disk as a self-describing artifact pair.

The intent is **regression-quality reference data**: the same scenario
re-run on a future build should produce the same V/I/SoC curves to
within hardware noise. Each scenario embeds a copy of the battery
profile JSON it was driven by, so a saved scenario is fully reproducible
regardless of changes to the bundled profiles.

## What's in here

```
validation/
  run_validation.py        # the harness — see "Running" below
  scenarios/               # captured artifacts (one set per run)
    <profile>_<kind>_<utc>.json   # full scenario record
    <profile>_<kind>_<utc>.csv    # tabular samples
    <profile>_<kind>_<utc>.png    # quick plot (if matplotlib installed)
    <profile>_<kind>_<utc>_profile.json   # snapshot of the input profile
```

Scenario JSON layout (`schema_version: 2` since v0.9.4)::

```json
{
  "scenario": "static_load_sweep" | "dynamic_load_pattern",
  "captured_utc": "...",
  "opensmu_version": "0.9.2",
  "profile":          { ...battery metadata + path... },
  "emulator_config":  { initial_soc, safety_max_voltage_V, ... },
  "bench":            { Eastwood QR10x device_type / serial / ... },
  "r_steps_ohm" | "pattern": [ ... ],
  "steps" | "samples": [ ... ]
}
```

## Running

Requires an Arc Pro on USB-CDC-ACM and a QR10x programmable load on
``COM7`` (override with ``--qr-port``). The Arc is auto-discovered.

```powershell
# Single profile static sweep
set PYTHONIOENCODING=utf-8
python validation\run_validation.py --scenario static --profile "CR2032-Energizer-(25)"

# Full matrix
python validation\run_validation.py --scenario static --all

# Dynamic IoT-pattern (sleep / wake / TX burst), 3 cycles
python validation\run_validation.py --scenario dynamic --profile "CR2032-Energizer-(25)" --cycles 3
```

`--profile` accepts either the bare stem (resolved against Otii's bundled
profile dir) or a full path to a profile JSON.

## Scenario kinds

### `static` — load sweep
Step through a fixed list of QR resistances, let each step settle, and
record one snapshot per step. Verifies the emulator's static V/I curve
matches the profile's predicted ESR sag.

Default sweep: ``[100k, 10k, 1k, 100, 50, 25, 12]Ω`` plus a recovery
step back at 100k.

### `dynamic` — IoT load pattern
Drive the QR through a time-varying pattern simulating an IoT
end-device: a "sleep" baseline interrupted by a brief "wake" and a
short "TX burst". Captures the emulator's transient response at a
fixed poll rate.

Default pattern (one cycle, repeat with ``--cycles``)::

| phase | R (Ω)   | duration (s) | approximate I (mA) |
|-------|---------|--------------|--------------------|
| sleep | 320 000 | 5.0          | ~0.01              |
| wake  | 3 200   | 0.6          | ~1                 |
| sleep | 320 000 | 1.5          | ~0.01              |
| tx    | 100     | 0.4          | ~30                |
| sleep | 320 000 | 5.0          | ~0.01              |

The QR's relay-switching time (30–95 ms) sets the lower bound on the
phase durations we can resolve cleanly; sub-millisecond pulse shapes
need a faster electronic load (e.g. Rigol DL3031A — coming soon).

## Per-profile safety overrides

`run_validation.py` keys safety caps off the profile filename stem.
Notable ones (see `PROFILE_OVERRIDES` in the script):

| Profile family            | safety_max_V | QR R_min | Why |
|---------------------------|--------------|----------|------|
| CR2032 / CR2 / CR123A     | 3.5          | 12 Ω     | nominal ~3 V, plenty of headroom |
| AA / AAA                  | 2.0          | 6 Ω      | 1.5 V nominal — cap below to protect |
| LiPo (all temps)          | 4.2          | 20 Ω     | Arc Pro high-range maxes ≈ 4.2 V under load; fresh OCV (~4.31 V) clamps |

## Headline results (v0.9.2 baseline)

Captured 2026-05-29 with Arc Pro on COM6, QR101A-1M-R1 on COM7, cable + cable-end probe resistance ~0.6 Ω.

### Static — voltage at 100 Ω load (initial sweep)
| Profile             | OCV (V) | V at 100 Ω | I at 100 Ω | ESR signature |
|---------------------|---------|------------|------------|---------------|
| CR2032-Energizer    | 3.224   | 2.953      | 29.4 mA    | high ESR — 270 mV sag |
| CR123A-GP           | 3.201   | 3.190      | 31.8 mA    | very low — 10 mV sag |
| CR2-Panasonic       | 3.209   | 3.206      | 32.0 mA    | very low — 3 mV sag |
| AA-Varta            | 1.603   | 1.595      | 15.9 mA    | low — 8 mV sag |
| AAA-Duracell        | 1.611   | 1.603      | 16.0 mA    | low — 8 mV sag |
| LiPo @ +20 °C       | 4.20 *  | 4.20 *     | 40.6 mA    | clamped — sag below clamp |
| LiPo @ +5 °C        | 4.20 *  | 4.20 *     | 40.6 mA    | clamped at light load |
| LiPo @ −10 °C       | 4.20 *  | 4.18       | 40.6 mA    | cold ESR shows already |

`*` LiPo OCV is clamped by Arc Pro's high-range hardware limit; see "Per-profile safety overrides" above.

### Static — cell collapse / current capability at 12 Ω
| Profile             | V at 12 Ω | I at 12 Ω | Behavior |
|---------------------|-----------|-----------|----------|
| CR2032-Energizer    | 1.853     | 139.5 mA  | **collapse** — real CR2032 fails here |
| CR123A-GP           | 3.107     | 258.6 mA  | sustains high pulse — designed for it |
| CR2-Panasonic       | 3.159     | 262.8 mA  | sustains |
| AA-Varta            | 1.542     | 127.4 mA  | minor sag |
| AAA-Duracell        | 1.547     | 128.8 mA  | minor sag |
| LiPo @ +20 °C       | 4.20 *    | 196.8 mA  | clamped (QR @ 20 Ω) |
| LiPo @ +5 °C        | 4.11      | 196.8 mA  | small sag |
| LiPo @ −10 °C       | 3.70      | 185.4 mA  | **500 mV sag** — cold ESR ≈ 2.7 Ω |

The LiPo temperature sweep is the most striking result — same profile,
same chemistry, ESR rises ~10× going from +20 °C to −10 °C, exactly
what a real Li-Po cell does.

### Dynamic — IoT pattern, 100 Ω TX burst
| Profile             | V (sleep) | V (TX min) | TX sag | Recovery |
|---------------------|-----------|------------|--------|----------|
| CR2032-Energizer    | 3.224     | 3.038      | 186 mV | ~5 s     |
| CR123A-GP           | 3.200     | 3.190      |  10 mV | < 0.1 s  |
| LiPo @ +20 °C       | 4.20 *    | 4.20 *     | (clamped) | n/a   |

These three rows together are why "battery emulation" is interesting
in the first place — a CR2032 makes for a terrible LoRaWAN TX battery
because of the sag/recovery dynamics, and the emulator reproduces that
without anyone having to wreck a real cell.

## DL3031A vs QR10x — same matrix, two loads (v0.9.4)

The harness now accepts `--load {qr10x,dl3031a}`. Both loads were run
through the full static matrix and the dynamic IoT pattern. Each
instrument has a regime where it shines and a regime where the other
takes over — the point of having both on the bench.

### Static @ 12 Ω — high-current LiPo behavior previously hidden

QR10x set `R_min = 20 Ω` for LiPo profiles (1 W cap at 4.2 V).
The DL3031A has no such constraint and reveals what the cell actually
does at 12 Ω with full 4.2 V applied — a higher-current regime where
ESR-driven sag is much more dramatic.

| LiPo °C | V @ 12 Ω (QR10x, clamped to 20 Ω) | V @ 12 Ω (DL3031A, actual) | Δ |
|---------|------------------------------------|----------------------------|---|
| +20     | 4.20 V (clamped, 197 mA)           | 4.15 V (355 mA)            | DL3031A pulled 1.8× more current |
| +5      | 4.11 V (197 mA)                    | 3.94 V (350 mA)            | 170 mV deeper sag |
| −10     | 3.70 V (185 mA)                    | **3.24 V (324 mA)**        | nearly 500 mV deeper sag |

The QR10x's safety-limited measurement at 20 Ω was already meaningful
("ESR rises ~10× cold") — but the DL3031A's unrestricted 12 Ω reading
on −10 °C LiPo shows the cell hitting **3.24 V at 324 mA**, well into
the lower part of LiPo's usable range. That's what a real cold-soaked
LiPo does and it was sitting beyond our reach with the QR alone.

### Static @ light load — QR10x wins below ~1 mA

The DL3031A is an active electronic load: closed-loop regulation needs
enough current to work. Below roughly **1 mA target current**, CR mode
loses regulation and the load reads as effectively open. The QR10x is
a passive resistor network, so it stays correct down to whatever the
SMU can measure.

Concrete: at the 10 kΩ step against CR123A (3.20 V, expected 0.32 mA),
the QR10x measured 0.322 mA cleanly; the DL3031A measured 0.012 mA
(SMU current noise floor).

Result: **QR10x is the right tool for sleep-current and quiescent
characterization**; DL3031A is the right tool for active / TX / load
phases.

### Dynamic — the polling-rate bottleneck shows up

With the QR10x's 30–95 ms relay switching, the emulator's ~100 Hz
control loop is fast enough to keep up with phase transitions, so the
saved samples align with the phase labels.

The DL3031A switches in microseconds. The emulator polls current via
`read_value(MAIN_CURRENT)` which has its own ~10–100 ms latency on top
of the 100 Hz loop. So when we tag a sample as "TX phase 50 ms after
the load went to 100 Ω", the current value we just read might still
be the previous phase's value. Result: the dl3031a dynamic phase
summary reports near-zero TX current. The raw V/I traces tell the
real story — but the per-phase summary is misleading at the QR10x's
phase durations.

This is what motivates step 2 of the v0.9.4+ roadmap: read directly
from the DL3031A's own `:MEASure:` queries (or its 400-point cached
`:MEASure:WAVedata?`) instead of the emulator's lagged copy.

### Per-load takeaway

| Use case | Best load | Why |
|---|---|---|
| Quiescent / sleep current (< 1 mA) | QR10x | passive, correct at any current |
| Steady-state mid-load (~30 mA) | either | both regulate cleanly here |
| Heavy DC pulse (> 100 mA) | DL3031A | QR10x's W rating caps the safe R |
| Sub-ms transients | DL3031A | QR10x relays are too slow |
| Built-in battery-discharge sequences | DL3031A | firmware does it |

## High-resolution dynamic capture (v0.9.5)

`--pattern hires` switches to a dedicated runner that uses the Arc Pro's
native streaming (`SMU.record`) instead of state polling. Three things
fall out:

### Architecture: emulator-off during the recording window

`Emulator._loop` writes `set_voltage` at 100 Hz while the recording
reader thread is consuming the transport at ~4 kHz. The combination
deadlocks consistently within ~100 ms.

The hires runner sidesteps the deadlock: it lets the emulator settle to
fresh OCV, captures that value, stops the emulator, pins the SMU at
that voltage manually, then opens the recording. Battery dynamics
(OCV-drop with SoC) are off during the 10 s capture — fine since the
SoC delta over 10 s × 30 mA peak is ~0.08 mAh, sub-0.04 % of CR2032.

Saved scenarios include `"recording": {"pinned_voltage_V": …}` so this
is recoverable from the JSON.

### Sample rates from Arc Pro native streaming

| Channel | Rate |
|---|---|
| `MAIN_CURRENT` (mc, subtype-4 packed) | ~4 000 Hz |
| `MAIN_VOLTAGE` (mv, subtype-1) | ~1 000 Hz |

The harness merges to the I channel's timebase (interpolated V) so the
saved samples are at the higher rate.

### DL3031A switching latency: ~700 ms input-toggle

The first hires runs used 100 ms TX phases (sleep → R=100 → sleep). At
4 kHz sampling, the recording showed the current spike landing **700 ms
after** the TX command — well into the post-TX sleep phase. Cause:
toggling `:SOUR:INP:STAT` between sleeps cycles the regulation loop's
internal settling.

The default hires pattern was widened to **300 ms TX** windows to keep
the spike inside its labeled phase. For sub-300 ms work, the right
approach is "leave the input ON, toggle CC current setpoint instead"
— left for a future scenario.

### Headline results (v0.9.5 hires, 4 kHz)

3 cycles × 2 TX bursts each = 6 TX events per scenario. Per-phase I
peaks via the recording:

| Profile             | TX I peak  | TX I steady | V at TX terminals  |
|---------------------|-----------|-------------|--------------------|
| CR2032-Energizer    | 71 mA     | ~30 mA      | 3.198 .. 3.227 V   |
| CR123A-GP           | 70 mA     | ~30 mA      | 3.176 .. 3.204 V   |
| LiPo @ +20 °C       | 86 mA     | ~40 mA      | 4.195 .. 4.210 V   |

The DL3031A's inrush peak is consistently ~2× steady-state across all
chemistries (V/100 Ω = ~30-40 mA expected steady; ~70-90 mA observed
peak). That's an artifact of the DL3031A's CR-mode closed-loop
catching up to a step change, not a property of the simulated battery.

## `--scenario dynamic-list` (v0.9.6) — firmware-timed sequences

The hires runner still drives the load from the host. For
sub-300 ms transients we move the sequence into the DL3031A's
firmware: `--scenario dynamic-list` programs the DL3031A's
`:SOURce:LIST:*` and triggers it via BUS while `SMU.record(...)`
captures the response.

```
python validation/run_validation.py --scenario dynamic-list \
    --profile "CR2032-Energizer-(25)" --load dl3031a --cycles 3
```

### Pattern (CC mode, in amps)

`DEFAULT_LIST_TX_PATTERN_A` is `(label, current_A, duration_s)`:

| label | I (A)    | duration |
|-------|----------|----------|
| sleep | 0.0001   | 1.000 s  |
| tx    | 0.0300   | 0.050 s  |
| sleep | 0.0001   | 1.000 s  |

3 cycles × 3 steps = 9 steps total played by firmware.

### Why 3 steps and not 5

Empirically, LIST programs with ≥ 5 steps don't fire cleanly via
BUS trigger in our setup — the trigger fires but the LIST's
playback alignment slips (current arrives ~3 s late in some
captures). 3-step programs (sleep / TX / sleep) executed `count=N`
times work reliably. Tagged as a v0.9.7 investigation item; the
SCPI surface is correct, the firmware/trigger interaction is the
suspect.

### Why no emulator during the recording window

Same as `--pattern hires`: the emulator's `_loop` writing
`set_voltage` at 100 Hz contends with the recording reader thread
at 4 kHz and deadlocks. For the dynamic-list runner we go further
and skip the emulator entirely — fresh OCV is read directly from
the profile (`profile.ocv_at(0.0)`), clamped to
`safety_max_voltage_V`, and pinned for the recording window.
Saved scenarios carry `recording.pinned_voltage_V` so the
condition is recoverable.

### Headline results

| Profile             | TX setpoint | TX I observed | Notes                |
|---------------------|------------|----------------|----------------------|
| CR2032-Energizer    | 30 mA      | 35 mA peak     | clean firmware-timed TX |
| CR123A-GP           | 30 mA      | 34 mA peak     | clean                |
| LiPo @ +20 °C       | 30 mA      | did not capture | high-V-range flow is flaky; investigation TBD |

## Notes / known limits

- Arc Pro high range tops out at ≈ 4.2 V under load. LiPo profiles
  with fresh OCV > 4.2 V are clamped — useful behavior to be aware of
  when interpreting clamped V_out values at high SoC.
- DL3031A CR mode regulation breaks down below ~1 mA target current.
  Light-load steps appear as input-off in the captures (which is
  arguably the better physical model for a sleeping IoT device).
- DL3031A's electronic switching outpaces the emulator's polling
  rate; the dynamic-scenario phase summary is misleading for that
  load. Use the raw CSV / future direct-from-DL3031A measurements.
- DL3031A 5+ step LIST programs sometimes fail to align with the
  harness's expected phase events — the LIST plays but with delayed
  onset (~3 s after `:TRIGger`). 3-step LIST + `count=N` is the
  reliable shape. Under investigation for v0.9.7.
- Emulator + recording deadlock: `Emulator._loop` writes
  `set_voltage` at 100 Hz while the recording reader thread consumes
  the transport at ~4 kHz; the combination deadlocks within ~100 ms.
  hires / dynamic-list runners sidestep by pinning V manually.
- The QR10x's mechanical-relay switching (30–95 ms) is too slow to
  resolve real GSM/Wi-Fi pulse trains. Use Rigol DL3031A (or similar)
  for sub-ms transient work.
- The dynamic-scenario phase summary in the console reports mean V/I
  across *all* samples in a phase, including settling at phase
  boundaries. Use the raw CSV for accurate per-phase analysis.
- Profile snapshots (`*_profile.json`) are byte-identical copies of
  Otii's bundled JSON — preserved with the scenario so future reruns
  remain reproducible even if Otii ships an updated profile.

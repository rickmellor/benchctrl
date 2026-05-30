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

Scenario JSON layout (`schema_version: 1`)::

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

## Notes / known limits

- Arc Pro high range tops out at ≈ 4.2 V under load. LiPo profiles
  with fresh OCV > 4.2 V are clamped — useful behavior to be aware of
  when interpreting clamped V_out values at high SoC.
- The QR10x's mechanical-relay switching (30–95 ms) is too slow to
  resolve real GSM/Wi-Fi pulse trains. Use Rigol DL3031A (or similar)
  for sub-ms transient work.
- The dynamic-scenario phase summary in the console reports mean V/I
  across *all* samples in a phase, including settling at phase
  boundaries. Use the raw CSV for accurate per-phase analysis.
- Profile snapshots (`*_profile.json`) are byte-identical copies of
  Otii's bundled JSON — preserved with the scenario so future reruns
  remain reproducible even if Otii ships an updated profile.

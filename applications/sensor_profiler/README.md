# sensor_profiler

A power-profile capture + analysis app for DUTs that normally run on a
fixed-voltage primary battery (CR2477 coin cell, AA pair, single 18650,
etc.). The Otii Arc Pro emulates the battery as a constant-voltage
source while we record current at native ~4 kHz for as long as you
want, then post-process the trace into TX-burst events, sleep-current
estimates, charge-consumption metrics, and battery-life projections.

A second Streamlit app browses the saved runs, drills into individual
chunks, and renders interactive Plotly plots.

This is a **separate application inside the benchctrl repo** — it uses
`benchctrl.drivers.otii_arc` and `benchctrl.Recording` as a library
but ships its own CLI / UI / dependency set.

## Layout

```
applications/sensor_profiler/
├── README.md           ← this file
├── requirements.txt    ← Streamlit + Plotly + Pandas
├── capture.py          ← CLI: start a new run, drive the Arc, save chunks
├── analyze.py          ← library + CLI: chunk → metrics + report bundle
├── app.py              ← Streamlit run browser
└── runs/
    └── <run_id>/
        ├── metadata.json            ← DUT label, capture config, status, notes
        ├── data/
        │   └── sensor_*.opensmu     ← native Arc chunks (one per hour)
        └── report/
            ├── summary.md
            ├── per_chunk.csv
            ├── burst_events.csv
            └── *.png                ← static plots for archival / sharing
```

Each `<run_id>` looks like `2026-05-31T19-01-34Z_room-temp-sensor_assoc-test`
(UTC start + DUT slug + optional label). Every run is fully
self-contained — copy the folder anywhere, all data + report sticks
with it.

## Quick start

Install the extras:

```powershell
pip install -r applications/sensor_profiler/requirements.txt
```

Run a capture:

```powershell
python applications/sensor_profiler/capture.py `
    --dut room-temp-sensor `
    --label assoc-test `
    --voltage 3.0 `
    --hours 24
```

Generate the report bundle:

```powershell
python applications/sensor_profiler/analyze.py `
    --run applications/sensor_profiler/runs/<run_id>
```

Browse all runs in the Streamlit app:

```powershell
streamlit run applications/sensor_profiler/app.py
```

## Capture workflow

1. Wire the DUT's battery clips to the Arc's main output (red `+` to
   battery `+`, black `-` to battery `-`). Confirm polarity before
   running `capture.py`.
2. Start `capture.py` with `--dut` and `--label` set. Output goes ON
   immediately at the configured voltage.
3. (For wireless / hub-paired devices) trigger pairing now so the
   association burst lands in chunk 0.
4. Capture rolls a fresh `.opensmu` file every `--chunk-minutes`
   (default 60) for up to `--hours` (default 24). Ctrl+C → finishes
   the current chunk, marks the run `aborted`, exits.

## Analysis

- **Sleep current**: 10th-percentile of the per-chunk current trace —
  robust floor that excludes burst transients.
- **TX bursts**: hysteresis detector at 1 mA enter / 0.5 mA exit
  thresholds; merges sub-5 ms gaps; drops sub-0.5 ms events.
- **Per-event metrics**: start time, duration, peak, mean, charge.
- **Per-chunk summary**: avg/peak/sleep current, voltage min/max,
  charge & energy consumed, burst count.
- **CR2477 life projection** (configurable battery model in `analyze.py`).

## Status

Genesis: 2026-05-31. First DUT under test: a 900 MHz FSK wireless
room-temperature + presence sensor.

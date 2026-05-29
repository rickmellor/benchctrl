# Output formats

OpenSMU recordings can be exported to several formats. Pick the one that
matches what you're going to do with the data.

## Quick chooser

| Format | Best for | Lossless? | Compact? | Universal? | Extra install? |
|---|---|---|---|---|---|
| `.opensmu` (native binary) | Long-term archive, round-trip in opensmu | yes | yes | opensmu only | none |
| Parquet (`.parquet`) | Share with colleagues, load in pandas/polars/duckdb/Excel | yes | **yes** (~10-20× CSV) | pandas/polars/Arrow tooling | `opensmu[parquet]` |
| CSV long (`timestamp,channel,value,unit`) | Spreadsheets, shell pipelines, ad-hoc inspection | yes | no (huge for long captures) | universal | none |
| CSV wide (one column per channel) | Quick plots, drag into Excel | lossy at lower-rate channels | medium | universal | none |
| JSON (samples + statistics + metadata) | Human-readable inspection, shareable summaries | yes | no | universal | none |
| numpy `.ndarray` | In-process analysis, plotting, math | yes (float32) | n/a (in-memory) | scientific Python | `opensmu[numpy]` |
| pandas `Series`/`DataFrame` | Notebooks, statistical analysis, resampling | yes | n/a (in-memory) | scientific Python | `opensmu[pandas]` |
| matplotlib `Figure` | Quick-look plotting | n/a | n/a | scientific Python | `opensmu[plot]` |
| Raw bytes (`.raw`) | Protocol debugging | yes | yes | opensmu only | none |

**Rules of thumb:**

- **Archive a measurement for later opensmu use** → `.opensmu`
- **Send it to a teammate who doesn't have opensmu** → `.parquet`
- **Open in Excel right now** → `.csv` (wide format)
- **Analyze in a Jupyter notebook** → `.to_pandas()` (in-memory) or `.parquet` (from disk)
- **Plot once and see what happened** → `.plot()` (matplotlib quick-look)
- **Custom numpy/scipy analysis** → `.to_numpy(channel)` + `.timestamps_numpy(channel)`

## Sizing example

Same 5-second recording of `mc` (4 kHz) + `mv` (1 kHz) — that's 25,000
samples total — across formats:

| Format | Approximate size |
|---|---|
| `.opensmu` (native) | ~40 KB |
| `.parquet` (snappy) | ~50 KB |
| CSV wide | ~200 KB |
| JSON | ~600 KB |
| CSV long | ~750 KB |

For a 30-minute battery-profiling capture (7.2 M `mc` samples), the
linear scaling makes the picture sharper — parquet stays around ~10 MB,
CSV crosses ~250 MB, JSON breaks 1 GB. Use parquet (or native
`.opensmu`) for anything past a few seconds.

## Optional dependencies

The data-science methods are **strictly optional**. opensmu itself
imports cleanly without any of `numpy`, `pandas`, `pyarrow`, or
`matplotlib` installed — they're only loaded the moment you call the
matching method.

| Method | Install with |
|---|---|
| `Recording.to_numpy(channel)` / `Recording.timestamps_numpy(channel)` | `pip install opensmu[numpy]` |
| `Recording.to_pandas(channel)` / `Recording.to_pandas()` | `pip install opensmu[pandas]` |
| `Recording.save_parquet(path)` | `pip install opensmu[parquet]` |
| `Recording.plot(channels=None)` | `pip install opensmu[plot]` |
| Everything science-related at once | `pip install opensmu[science]` |

If you call a method without the dependency installed you get a clear
error pointing at the right extras key — e.g.:

```
ImportError: save_parquet() requires pyarrow.
Install with: pip install 'opensmu[parquet]'
```

The MCP server install (`opensmu[mcp]`) and these extras are
independent — combine them as needed: `pip install
'opensmu[mcp,science]'` gets you everything.

## Usage

### Save to disk

```python
rec.save("run.opensmu")           # native binary — canonical lossless
rec.save_parquet("run.parquet")   # portable columnar — opens anywhere
rec.save_csv("run.csv")           # long form (default)
rec.save_csv("run.csv", format="wide")
rec.save_json("run.json")         # samples + statistics + metadata
rec.save_raw("run.raw", bytes_)   # raw inbound USB bytes (escape hatch)
```

### Load from disk

```python
from opensmu import Recording
rec = Recording.load("run.opensmu")   # reconstructs an equivalent Recording

# For Parquet, load directly with your tool of choice:
import pandas as pd
df = pd.read_parquet("run.parquet")
```

### In-memory data

```python
# numpy
arr = rec.to_numpy("mc")              # float32 ndarray of values
ts = rec.timestamps_numpy("mc")       # float64 ndarray of timestamps

# pandas
series = rec.to_pandas("mc")          # Series indexed by timestamp
df = rec.to_pandas()                  # wide DataFrame, all channels
df_filled = df.ffill()                # forward-fill lower-rate channels
```

### Quick plot

```python
fig = rec.plot()                                    # all channels, subplots
fig = rec.plot(["mc", "mv"])                        # selected channels
fig = rec.plot(show=False)                          # headless / batch
fig.savefig("run.png", dpi=150, bbox_inches="tight")
```

## Notes on the wide DataFrame

When you ask for `rec.to_pandas()` (no channel argument), the result is
a wide DataFrame indexed at the **union of all channel timestamps**.
Lower-rate channels carry `NaN` at timestamps that don't align with
their native rate — e.g. with `mc` at 4 kHz and `mv` at 1 kHz, `mv`
has `NaN` at three out of every four rows.

This is intentional: the wide DataFrame is lossless. If you want
forward-filled values for plotting, do `df.ffill()`. If you want a
resampled common-rate frame, do `df.resample('1ms').mean()` (pandas
syntax) on a DatetimeIndex, or use `Recording.downsample(channel,
factor)` to downsample a single channel in place.

## Decision tree

```
Need to:
  Send to a colleague who doesn't have opensmu?
    → Parquet (compact, portable, opens in pandas/polars/duckdb/Excel)
  Archive for future opensmu round-trip?
    → .opensmu (native binary)
  Open in Excel right now?
    → CSV (wide format)
  Analyse in a Jupyter notebook?
    → .to_pandas() or load Parquet
  Custom numpy analysis?
    → .to_numpy() + .timestamps_numpy()
  See what the data looks like in 1 line?
    → .plot()
  Inspect the bytes from the wire?
    → .raw via .save_raw()
```

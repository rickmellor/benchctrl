---
name: opensmu
description: Use when controlling a Qoitech Otii Arc Pro source-measurement unit, writing code with the opensmu Python library, analysing captured .opensmu recordings, or building measurement automation. Covers connection patterns, safety guards for output enable, the choice between MCP tools and the Python API, and common anti-patterns.
---

# OpenSMU library guidance

OpenSMU is a Python library that drives a Qoitech Otii Arc Pro SMU directly
over its USB CDC-ACM port. **No vendor server, no Automation Toolbox license,
no GUI.** The wire protocol is fully reverse-engineered — see
[`docs/protocol.md`](../../docs/protocol.md).

Repo: `C:\Users\rickm\Desktop\opensmu`.

## Two integration paths — pick the right one

| Task style | Use |
|---|---|
| Interactive: "set 3.3 V, record for 5 s, what's the peak current?" | **MCP server** (`opensmu-mcp`) |
| One-off live read: "what's mv right now?" | **MCP server** (`live`, `take_snapshot`) |
| Custom analysis: FFT, plotting, anomaly detection | **Python API** |
| Batch: process every `.opensmu` file in a folder | **Python API** |
| CI / scheduled measurements | **Python API** |
| Long-running streaming with custom logic | **Python API** |
| Voltage sweep + I-V plot | **Both**: MCP for sweep, Python for plot |

If MCP tools cover the task end-to-end, prefer them — no Python needed. Reach
for the Python API the moment custom logic enters the picture.

The 23 MCP tools are documented in [`docs/mcp.md`](../../docs/mcp.md). The
Python API is documented in [`docs/api_reference.md`](../../docs/api_reference.md).

## Core Python pattern — always use the context manager

```python
import time
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.enable_channels(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE)
    with smu.record(name="run-1") as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    print(rec.statistics(Channel.MAIN_CURRENT))
    rec.save("run-1.opensmu")
```

`SMU.open()` auto-discovers the first connected Arc by USB VID/PID
(`0x0FCE` / `0xD1E6`). Pass a port string explicitly for multi-device setups:
`SMU.open("COM6")`.

`smu.record()` is the idiomatic recording form. It sends per-channel enables,
starts a background reader thread that buffers samples into the `Recording`
object, and on exit sends per-channel disables + the `0x7E` flush + the `0x7C`
cleanup. Native rates are delivered (4 kHz mc/mp, 1 kHz mv) — see
[`docs/protocol.md`](../../docs/protocol.md) for the packed sample frame
format.

## Safety pattern — always before `set_output(True)`

```python
# All three must be true before driving the output:
assert smu.current_limit is not None, "set_current_limit() first — bounds DUT damage"
assert smu.voltage is not None, "set_voltage() first — so you know what you're driving"
# Verify the DUT can tolerate smu.voltage before this point.

smu.set_output(True)
try:
    # ... do work ...
    pass
finally:
    smu.set_output(False)   # always disable, even on exception
```

The MCP `enable_output` tool enforces these same three guards.

## Channel codes (quick reference)

| Code | Wire id | Label | Native rate | Unit | Notes |
|---|---|---|---|---|---|
| `mc` | 0x00 | Main Current | 4 kHz | A | auto-co-enables `mp` |
| `mv` | 0x01 | Main Voltage | 1 kHz | V | |
| `mp` | 0x06 | Main Power | 4 kHz | W | |
| `ac` | 0x02 | ADC Current | 1 kHz | A | auto-co-enables `ap` |
| `av` | 0x03 | ADC Voltage | 1 kHz | V | |
| `ap` | 0x07 | ADC Power | 1 kHz | W | |
| `sp` | 0x05 | Sense+ | 1 kHz | V | |
| `sn` | 0x04 | Sense− | 1 kHz | V | |
| `vb` | 0x10 | VBUS | 1 kHz | V | |
| `vj` | 0x11 | DC Jack | 1 kHz | V | |
| `tp` | 0x14 | Temperature | 1 kHz | °C | always-on; not toggleable |
| `i1`/`i2` | 0x16 | GPI 1/2 | 1 kHz | digital | shares wire id |
| `rx` | 0x17 | UART log | text | — | deferred parsing |

Strings work at API boundaries (`smu.enable_channels("mc", "mv")`), but the
enum is canonical (`Channel.MAIN_CURRENT`).

## Recording analysis

```python
from opensmu import Channel, Recording

rec = Recording.load("run-1.opensmu")

# Slice by time
stats = rec.statistics(Channel.MAIN_CURRENT, start=0.5, end=2.0)
print(f"avg I: {stats.average*1000:.2f} mA, peak: {stats.max*1000:.2f} mA")
print(f"charge: {stats.charge*1000:.3f} mC over {stats.duration:.3f}s")

# Raw access for custom analysis
data = rec.data(Channel.MAIN_CURRENT)      # list[float]
timestamps = rec.timestamps(Channel.MAIN_CURRENT)
# or directly the buffer:
buf = rec.buffer(Channel.MAIN_CURRENT)     # ChannelBuffer
print(f"sample_rate={buf.sample_rate}, t0={buf.t0}, len={len(buf)}")
```

`Statistics.charge` is populated for current channels (A); `Statistics.energy`
for power channels (W). Both are integrated over the selected window. Voltage
channels return `None` for charge/energy — by design, those quantities aren't
meaningful for a voltage stream alone.

## File formats

| Extension | Content | Round-trip lossless? |
|---|---|---|
| `.opensmu` | Native binary, msgpack-like | yes |
| `.csv` | Long form (default) or wide (`format="wide"`) | no (text formatting) |
| `.json` | Samples + statistics + metadata | yes |
| `.raw` | Raw inbound USB bytes (escape hatch) | yes |

```python
rec.save("run.opensmu")           # canonical
rec.save_csv("run.csv")           # long: timestamp_s,channel,value,unit
rec.save_csv("run.csv", format="wide")  # one column per channel
rec.save_json("run.json")         # human-readable
Recording.load("run.opensmu")     # reconstructs an equivalent Recording
```

## Exception hierarchy

```
SMUError
├── SMUConnectionError       — port can't open / lost mid-stream
├── SMUProtocolError         — bad frame / wrong magic / bad checksum
├── SMUCommandError          — device rejected a SET
│       .error_code          — signed int (e.g. -101 for out-of-range)
│       .last_good_value     — the value it reverted to
├── SMUValueError            — client-side range check failed (extends ValueError)
├── SMUTimeoutError          — no samples within deadline (extends TimeoutError)
└── SMUNotImplementedError   — deferred feature (extends NotImplementedError)
```

Async device errors (e.g. setting `4.0 V` in low range) surface as
`SMUCommandError` on the next API call — the background reader thread
parses error frames and queues them.

## Anti-patterns — don't do these

- **Don't reach for the Otii server / Automation Toolbox / TCP port 1905.**
  opensmu talks to the device directly via pyserial. There is no server.
  Code that imports `otii_tcp_client` is the wrong path.
- **Don't call `smu.calibrate()`, `smu.firmware_upgrade()`, `smu.set_supply_battery_emulator()`,
  `smu.enable_battery_profiling()`, `smu.wait_for_battery_data()`,
  `smu.set_battery_profile()`.** They all raise `SMUNotImplementedError` by
  design — calibration and battery emulation are deferred (see
  [`ROADMAP.md`](../../ROADMAP.md)); firmware upgrade is deferred indefinitely
  (bricking risk).
- **Don't call `smu.set_channel_samplerate()`.** No wire command exists —
  sample rates are hardware-fixed. Use `Recording.downsample(channel, factor)`
  for client-side downsampling after capture.
- **Don't write your own wire framing.** Use `opensmu.protocol.encode_*` and
  `iter_frames` / `iter_samples`. Wire details (checksum, packed vs baseline
  envelopes, the `0x7E` flush before disable, the `0x7C` cleanup) have subtle
  ordering requirements.
- **Don't assume sample rates from `Channel.sample_rate` alone.** That's the
  native max. If the device is in baseline streaming (no recording), all
  channels stream at **~6 Hz** regardless. Native rates are delivered only
  while a recording is active (i.e. inside `smu.record()` or after
  `smu.start_recording()`).
- **Don't call `smu.set_output(True)` without a current limit.** Bricks DUTs
  on fault conditions. Always `set_current_limit()` first.
- **Don't call `smu.get_param()` while a recording is active.** The reader
  thread owns the byte stream during recording. Stop the recording first.

## Quick recipes

### Voltage sweep + I-V curve

```python
import time
from opensmu import SMU, Channel

VOLTAGES = [0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.25]
DWELL = 1.0

with SMU.open() as smu:
    smu.set_current_limit(1.0)
    smu.enable_channels(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE)
    smu.set_voltage(0.0)
    smu.set_output(True)
    try:
        with smu.record(name="sweep") as rec:
            for v in VOLTAGES:
                smu.set_voltage(v)
                time.sleep(DWELL)
    finally:
        smu.set_output(False)

    # Extract average current at each dwell
    for i, v in enumerate(VOLTAGES):
        t0, t1 = i * DWELL + 0.2, (i + 1) * DWELL - 0.1   # skip transitions
        stats = rec.statistics(Channel.MAIN_CURRENT, start=t0, end=t1)
        print(f"V={v:.2f}  I={stats.average*1000:8.3f} mA")
```

### Transient detection (peak current threshold)

```python
import time
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.set_current_limit(0.5)
    smu.set_voltage(3.3)
    smu.enable_channels(Channel.MAIN_CURRENT)
    smu.set_output(True)
    try:
        with smu.record() as rec:
            time.sleep(10)
    finally:
        smu.set_output(False)

    buf = rec.buffer(Channel.MAIN_CURRENT)
    threshold = 0.100  # 100 mA
    above = [(t, v) for t, v in zip(buf.timestamps(), buf.values) if v > threshold]
    print(f"{len(above)} samples above {threshold*1000} mA")
    if above:
        first = above[0]
        print(f"first crossing at t={first[0]:.4f}s with I={first[1]*1000:.2f} mA")
```

### Live monitor (no recording)

```python
from opensmu import SMU, Channel

with SMU.open() as smu:
    for sample in smu.stream(seconds=10.0):
        if sample.channel is Channel.MAIN_VOLTAGE and sample.value > 3.5:
            print(f"V crossing 3.5V at t={sample.timestamp:.3f}s -> {sample.value:.4f}V")
            break
```

### Batch-process saved recordings

```python
from pathlib import Path
from opensmu import Channel, Recording

for p in Path("captures").glob("*.opensmu"):
    rec = Recording.load(p)
    stats = rec.statistics(Channel.MAIN_CURRENT)
    print(f"{p.name}: avg I={stats.average*1000:.2f} mA, charge={stats.charge*1000:.3f} mC")
```

## Pointers

- [`docs/getting_started.md`](../../docs/getting_started.md) — tutorial
- [`docs/api_reference.md`](../../docs/api_reference.md) — every class + method
- [`docs/protocol.md`](../../docs/protocol.md) — USB wire protocol
- [`docs/mcp.md`](../../docs/mcp.md) — MCP server config + tools
- [`docs/AGENTS.md`](../../docs/AGENTS.md) — AI-agent briefing
- [`ROADMAP.md`](../../ROADMAP.md) — deferred features + rationale
- [`examples/`](../../examples/) — copy-paste-friendly scripts

---
name: benchctrl
description: Use when controlling a Qoitech Otii Arc Pro source-measurement unit, writing code with the benchctrl Python library, analysing captured .opensmu recordings, or building measurement automation. Covers connection patterns, safety guards for output enable, the choice between MCP tools and the Python API, and common anti-patterns.
---

# benchctrl library guidance

benchctrl is a Python library that drives a Qoitech Otii Arc Pro SMU directly
over its USB CDC-ACM port. **No vendor server, no Automation Toolbox license,
no GUI.** The wire protocol is fully reverse-engineered — see
[`docs/protocol.md`](../../docs/protocol.md).

Repo: `C:\Users\rickm\Desktop\benchctrl`.

## Two integration paths — pick the right one

| Task style | Use |
|---|---|
| Interactive: "set 3.3 V, record for 5 s, what's the peak current?" | **MCP server** (`benchctrl-mcp`) |
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
from benchctrl import SMU, Channel

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
from benchctrl import Channel, Recording

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

## Output formats

Full chooser table + sizing examples in [`docs/output_formats.md`](../../docs/output_formats.md).
Quick rules:

| Need | Reach for |
|---|---|
| Archive for future benchctrl round-trip | `.save("run.opensmu")` |
| Share with colleagues (compact, portable) | `.save_parquet("run.parquet")` — needs `benchctrl[parquet]` |
| Open in Excel right now | `.save_csv("run.csv", format="wide")` |
| Notebook / custom analysis | `.to_pandas()` / `.to_numpy(ch)` — need `benchctrl[pandas]` / `benchctrl[numpy]` |
| Quick-look plot | `.plot()` — needs `benchctrl[plot]` |
| Inspect wire bytes | `.save_raw("run.raw", buf)` |

```python
# Always available (no extras):
rec.save("run.opensmu")                           # native binary, canonical
rec.save_csv("run.csv")                           # long: timestamp,channel,value,unit
rec.save_csv("run.csv", format="wide")            # one column per channel
rec.save_json("run.json")                         # samples + stats + metadata
Recording.load("run.opensmu")                     # round-trip

# With benchctrl[numpy]:
arr = rec.to_numpy("mc")                          # float32 ndarray
ts = rec.timestamps_numpy("mc")                   # float64 ndarray

# With benchctrl[pandas]:
series = rec.to_pandas("mc")                      # Series indexed by timestamp
df = rec.to_pandas()                              # wide DataFrame (NaN where rates differ)

# With benchctrl[parquet]:
rec.save_parquet("run.parquet")                   # ~10-20× smaller than CSV

# With benchctrl[plot]:
fig = rec.plot()                                  # one subplot per channel
fig.savefig("run.png")                            # standard matplotlib

# Or install everything at once:
#   pip install benchctrl[science]
```

**Optional-dependency rule** — benchctrl's base install pulls only pyserial.
The data-science methods import their deps lazily; calling one without
the dep raises a clear `ImportError` pointing at the right extras key
(e.g. `pip install 'benchctrl[parquet]'`). The base library never loads
numpy/pandas/pyarrow/matplotlib at import time.

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

## Battery features

`benchctrl.battery` ships a clean-room replacement for Qoitech's licensed
Battery Toolbox. See [`docs/battery.md`](../../docs/battery.md) for the
full feature plan.

### Profile I/O (v0.5.0)

```python
from benchctrl.battery import BatteryProfile

# Load Otii's bundled profile (or any compatible JSON)
profile = BatteryProfile.load("CR2032-Energizer-(25).json")
profile.nominal_voltage              # 3.0 V
profile.nominal_capacity_mAh         # 230.2 mAh
profile.ocv_at(used_capacity_mAh=50) # ~2.97 V
profile.esr_at(used_capacity_mAh=50) # ~14.1 ohm
```

Profiles produced here round-trip bit-identically through Otii — fully
interchangeable. **Don't write your own profile JSON shape**; use the
`BatteryProfile` / `DischargeTable` / `DischargeSample` dataclasses.

### Life calculator (v0.6.0)

Duty-cycle simulator. Two estimators:

```python
from benchctrl.battery import (
    BatteryProfile, DutyCycle,
    estimate_life_constant_current,
    estimate_life_from_profile,
    duty_cycle_from_recording,
)

duty = DutyCycle(
    active_current_A=0.020, active_time_s=0.1,    # 20 mA pulse for 100 ms
    sleep_current_A=5e-6,   sleep_time_s=60.0,    # 5 uA idle for 60 s
)

# Quick analytic estimate (flat-voltage assumption):
est = estimate_life_constant_current(capacity_mAh=230.0, duty_cycle=duty)
print(est.runtime_human)  # "250 days 16 hours 28 minutes ..."

# Profile-based iterative estimate (curve-aware, matches Otii's calculator):
profile = BatteryProfile.load("CR2032-Energizer-(25).json")
est = estimate_life_from_profile(
    profile=profile,
    duty_cycle=duty,
    self_discharge_per_month_pct=1.0,   # typical lithium primary
    safety_margin_pct=10.0,
)
print(est.runtime_human, est.stop_reason, est.final_voltage_V)

# Pull a duty cycle straight out of a recording (Otii's "Get from selection"):
from benchctrl import Recording
rec = Recording.load("device-under-test.opensmu")
duty = duty_cycle_from_recording(
    rec,
    active_window=(1.25, 1.5),   # seconds — when your TX pulse fires
    sleep_window=(2.0, 60.0),    # the long idle period
)
```

`LifeEstimate` carries `runtime_s`, `runtime_human` ("5 days 3 hours
22 minutes"), `iterations`, `capacity_consumed_mAh`, `average_current_A`,
`self_discharge_loss_mAh`, `safety_margin_loss_mAh`, `final_voltage_V`
(profile estimator only), `method`, `stop_reason`.

### Emulator (v0.8.0)

Host-side control loop drives the SMU as a battery (OCV + ESR sag).

```python
from benchctrl import SMU
from benchctrl.battery import BatteryProfile
from benchctrl.battery.emulator import Emulator, EmulatorConfig

profile = BatteryProfile.load("CR2032-Energizer-(25).json")
config = EmulatorConfig(
    profile=profile,
    initial_soc=1.0, series=1, parallel=1,
    soc_tracking=True,
    safety_max_voltage_V=3.5,     # MUST match your DUT's tolerance
    update_interval_s=0.01,        # 100 Hz host loop
)

with SMU.open() as smu:
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(60.0)   # let your DUT run
        st = emu.state()   # snapshot any time, thread-safe
        print(f"SoC: {st.soc*100:.1f}%, V_out: {st.output_voltage_V:.3f}")
    finally:
        emu.stop()         # always disables output in a finally
```

Bandwidth: ~100 Hz host-side, suitable for IoT loads with > 10 ms
response. Sub-ms ESR tracking would need firmware-level access. Otii's
licensed device-side emulator handles that regime; for everything
else, the benchctrl emulator works.

Safety: `safety_max_voltage_V` is a hard cap on output. Always set
this to your DUT's max tolerable voltage. The loop also stops on
`safety_max_used_mAh` and `soc_floor` if configured.

### Profiler (v0.7.0)

Orchestrates a real-cell discharge → builds a `BatteryProfile`.

```python
from benchctrl import SMU
from benchctrl.battery import (
    Battery, DischargeProfile, DischargeStep, ExitConditions,
)
from benchctrl.battery.profiler import Profiler, ProfilerConfig

config = ProfilerConfig(
    discharge_profile=DischargeProfile(
        low=DischargeStep("current", 0.001, 60.0),
        high=DischargeStep("current", 0.020, 0.5),
        exit_conditions=ExitConditions(iterations=0, ocv=2.7, voltage=2.5),
    ),
    battery=Battery(
        capacity=1000.0, capacity_unit="mAh",
        voltage=3.7, voltage_unit="V",
        manufacturer="Cellmaker", model="LP-1000",
    ),
    temperature=25.0,
)

with SMU.open() as smu:
    profiler = Profiler(smu, config)
    result = profiler.run(progress=lambda s: print(s.iteration, s.voltage_ocv))
    result.profile.save("LP-1000-(25).json")
```

Phase 3 limitations:
- **Step duration**: minimum 50 ms (USB-driven step changes have ms latency).
  Short-pulse profiles like Otii's CR2032 (2 ms) are rejected.
- **Mode**: `"current"` only. `"power"` / `"resistance"` rejected for now.
- **Real-battery hardware required** to validate; mock-SMU tests cover
  orchestration logic.

### MCP tools

Profile, calculator, and profiler tools (most work on saved files; profiler
needs an SMU and a real battery):

- `battery_profile_summary(path)`
- `battery_profile_lookup(path, used_capacity_mAh, temperature=None)`
- `battery_life_estimate(capacity_mAh, active_current_A, active_time_s, sleep_current_A, sleep_time_s, ...)`
- `battery_life_estimate_from_profile(profile_path, active_current_A, ...)`
- `battery_life_from_recording(recording_path, active_window_start_s, ..., profile_path=None, capacity_mAh=None, ...)`
- `battery_profiler_estimate_duration(capacity_mAh, high_current_A, high_time_s, low_current_A, low_time_s)` — pre-run estimate
- `battery_profiler_run(output_path, high_current_A, ..., capacity_mAh, nominal_voltage_V, ...)` — full discharge (LONG — hours typically)
- `battery_emulator_start(profile_path, initial_soc=1.0, ..., safety_max_voltage_V=5.0, ...)` — start the emulator
- `battery_emulator_state()` — snapshot
- `battery_emulator_stop()` — stop + disable output

## Bench instruments

`benchctrl.bench` hosts drivers for other lab instruments wired alongside
the Arc. Currently:

- `benchctrl.bench.QR10x` — Eastwood Tech QR10x programmable resistance
  substitution box (1 Ω-8.4 MΩ, USB-Serial, AT command set). Useful
  for sleep / quiescent / low-current scenarios where a passive load
  is the right model.
- `benchctrl.bench.RigolDL3031A` — Rigol DL3031A programmable electronic
  load (150 V / 60 A / 350 W, USB-TMC via pyvisa). CC / CV / CR / CP
  modes plus firmware-side LIST / transient / battery-discharge
  sequences. Right for active / TX-burst / high-current loads.

```python
from benchctrl.bench import QR10x, RigolDL3031A

# Passive resistor — ideal for sleep / quiescent current
with QR10x.open("COM7") as qr:
    qr.set_safety_limit(12.0)             # device-enforced min R
    qr.set_resistance(10_000)              # 10 kΩ

# Active electronic load — ideal for high-current and transient
with RigolDL3031A.open() as dl:            # auto-discover by VID/PID
    dl.set_mode("CC"); dl.set_current(0.030)
    dl.set_input(True)
```

**QR10x safety rule of thumb**: `qr.set_safety_limit(V**2 / P_max)`
where V is the source voltage and P_max ≤ 1 W. At 3.2 V → 12 Ω, at
5 V → 25 Ω.

**DL3031A firmware modes** (v0.9.6) for sub-100 ms transients that
host-driven setpoint changes can't keep up with:

```python
# Program a CC LIST and play it in firmware
dl.program_list(
    steps=[(0.0001, 1.0), (0.030, 0.050), (0.0001, 1.0)],
    mode="CC", count=3, range_value=6.0,
    slew_A_per_us=0.5, end_behavior="LAST",
    trigger_source="BUS",
)
dl.set_input(True); dl.trigger_now()       # BUS trigger fires the sequence

# CC transient toggle pulse
dl.configure_transient_pulse(
    a_level_A=0.030, b_level_A=0.0001,
    a_width_s=0.050, b_width_s=1.0, mode="TOGGle",
)
dl.transient_enable(True); dl.trigger_now()

# Built-in battery discharge test with stop conditions
dl.configure_battery_test(
    current_A=0.050, v_stop_V=2.7, capacity_stop_mAh=200.0,
)
dl.set_input(True)
# poll dl.battery_stats() — {capacity_mAh, energy_Wh, discharge_time_s, V, I}
```

**DL3031A manual gotchas** (compensated by the driver):
- `:SOUR:LIST:STEP` is highest-index, not count (3-step list = STEP 2).
- `:SOUR:LIST:SLEW` is per-step, not global.
- `:SOUR:LIST:END` accepts `LAST|OFF`, not `NORMal|LAST`.

**Pick load by use case:**

| Use case | Best load |
|---|---|
| Quiescent / sleep current (< 1 mA) | QR10x (passive, correct at any I) |
| Steady-state ~30 mA | either |
| Heavy DC pulse > 100 mA | DL3031A (QR10x's 1 W cap limits min R) |
| Sub-ms / sub-100 ms transients | DL3031A LIST or transient mode |
| Built-in battery discharge characterization | DL3031A |

## Anti-patterns — don't do these

- **Don't reach for the Otii server / Automation Toolbox / TCP port 1905.**
  benchctrl talks to the device directly via pyserial. There is no server.
  Code that imports `otii_tcp_client` is the wrong path.
- **Don't call `smu.calibrate()`, `smu.firmware_upgrade()`, `smu.set_supply_battery_emulator()`,
  `smu.enable_battery_profiling()`, `smu.wait_for_battery_data()`,
  `smu.set_battery_profile()`** on the `SMU` class — they raise
  `SMUNotImplementedError`. Battery emulation lives in
  `benchctrl.battery.emulator` (phased rollout — see
  [`docs/battery.md`](../../docs/battery.md)); calibration is deferred
  (see [`ROADMAP.md`](../../ROADMAP.md)); firmware upgrade is deferred
  indefinitely (bricking risk).
- **Don't call `smu.set_channel_samplerate()`.** No wire command exists —
  sample rates are hardware-fixed. Use `Recording.downsample(channel, factor)`
  for client-side downsampling after capture.
- **Don't write your own wire framing.** Use `benchctrl.protocol.encode_*` and
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
from benchctrl import SMU, Channel

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
from benchctrl import SMU, Channel

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
from benchctrl import SMU, Channel

with SMU.open() as smu:
    for sample in smu.stream(seconds=10.0):
        if sample.channel is Channel.MAIN_VOLTAGE and sample.value > 3.5:
            print(f"V crossing 3.5V at t={sample.timestamp:.3f}s -> {sample.value:.4f}V")
            break
```

### Batch-process saved recordings

```python
from pathlib import Path
from benchctrl import Channel, Recording

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

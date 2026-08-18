# benchctrl API reference

Every public name exported from `benchctrl` and its sub-packages.
Driver-internal modules (`drivers/otii_arc/transport.py`,
`drivers/otii_arc/protocol.py`, etc.) are documented for contributors
but are not part of the stability surface.

This document is structured by sub-package:

- [`OtiiArc` — the Otii Arc / Arc Pro driver](#otiiarc--the-device) (this layer)
- [`Recording`](#recording), [`Sample`](#sample), [`Statistics`](#statistics) — captured data
- [`benchctrl.battery` — battery characterisation + emulation](#benchctrlbattery--battery-characterisation--emulation)
- [`benchctrl.drivers` — companion instrument drivers](#benchctrldrivers--companion-instrument-drivers)
- [`benchctrl.session` / `.config` / `.discovery` — the local/remote/sim seam](#benchctrlsession--the-localremotesim-seam)
- [`benchctrl.sim` — simulators](simulation.md) (separate doc)
- [`benchctrl.net` / `benchctrl.agent` — remote mode](remote.md) (separate doc)
- [`benchctrl.agent.runs` — unattended runs](runs.md) (separate doc)
- [`benchctrl.mcp` — Model Context Protocol server](mcp.md) (separate doc — 226 tools)

Every driver lives under `benchctrl.drivers.<vendor_model>`; there is
no top-level `SMU` class and no `benchctrl.bench` package — both were
removed in 1.0. Vendor-agnostic code depends on the
`benchctrl.interfaces.SourceMeasurementUnit` Protocol instead.

## `OtiiArc` — the device

### Construction

```python
OtiiArc.discover() -> list[OtiiArcInfo]
OtiiArc.open(port=None, *, baudrate=9600) -> OtiiArc
```

`port` accepts `None` (auto-discover first device), a port string
(`"COM6"`, `"/dev/ttyACM0"`), an `OtiiArcInfo`, or a `PortInfo`. The
returned `OtiiArc` has its transport open and the session-init handshake
sent. Use as a context manager (`with OtiiArc.open() as smu:`) or call
`smu.close()` explicitly.

### State properties (read-only, host-side cache)

| Property | Type | Notes |
|---|---|---|
| `info` | `OtiiArcInfo \| None` | Discovery-time descriptor |
| `is_connected` | `bool` | Transport open |
| `voltage` | `float \| None` | Last set main V |
| `main_current` | `float \| None` | Last set CC-mode current (A) |
| `current_limit` | `float \| None` | Last set OC threshold (A) |
| `exp_voltage` | `float \| None` | Last set EXP-port V |
| `adc_resistor` | `float \| None` | Last set shunt Ω |
| `output_enabled` | `bool \| None` | Last set output state |
| `four_wire_enabled` | `bool \| None` | Last set 4-wire state |
| `current_limit_enabled` | `bool \| None` | CC mode on/off |
| `range` | `str \| None` | `"low"` / `"high"` |
| `uart_enabled` | `bool \| None` | |
| `uart_baudrate` | `int \| None` | |
| `exp_5v_enabled` | `bool \| None` | |
| `legacy_sink_enabled` | `bool \| None` | |
| `supply_mode` | `str` | `"power-box"` / `"battery-emulator"` |
| `power_regulation` | `str \| None` | |
| `gpo` | `dict[int, bool]` | Latest GPO pin states |
| `enabled_channels` | `set[Channel]` | Channels enabled for next recording |
| `four_wire_state` | `str` | `"active"` / `"disabled"` |

### `version() -> dict`

Best-effort hardware/firmware version. Returns `{}` if no metadata has
been observed in the inbound stream yet (the Arc only emits version
strings in some response messages, not on demand).

### Setters

Every setter that sends a wire command is a method that may raise
`BenchValueError` (client-side range check) or `BenchCommandError`
(device-side rejection delivered asynchronously).

| Method | Wire command |
|---|---|
| `set_voltage(volts: float)` | SET_MAIN_VOLTAGE (uV) |
| `set_current_limit(amps: float)` | SET_OC_PROTECTION (mA) |
| `set_max_current(amps: float)` | alias of `set_current_limit` |
| `set_main_current(amps: float)` | SET_MAIN_CURRENT (uA) for CC mode |
| `set_output(enable: bool)` | SET_MAIN_OUTPUT |
| `set_range("low" \| "high")` | SET_RANGE |
| `set_four_wire(enable: bool)` | SET_4WIRE |
| `set_current_limit_enabled(enable: bool)` | SET_SRC_CUR_LIMIT_ENABLED |
| `set_exp_voltage(volts: float)` | SET_DIGITAL_VOLTAGE (uV) |
| `set_exp_5v(enable: bool)` | ENABLE_5V (5_000_000 / 0) |
| `enable_5v(enable: bool)` | alias of `set_exp_5v` |
| `set_adc_resistor(ohms: float)` | SET_ADC_RESISTOR (uOhm) |
| `set_uart(enable, *, baudrate=None)` | SET_UART_ENABLE (+ baud) |
| `enable_uart(enable: bool)` | alias of `set_uart` |
| `set_uart_baudrate(baud: int)` | SET_UART_BAUDRATE |
| `set_gpo(pin: int, state: bool)` | SET_GPO (encoded bit pattern) |
| `set_legacy_sink(enable: bool)` | ENABLE_LEGACY_SINK |
| `enable_legacy_sink(enable: bool)` | alias |
| `set_supply_power_box()` | sets `supply_mode` (host-side) |

### Channel management

```python
smu.enable_channel(channel)           # accept Channel or str
smu.disable_channel(channel)
smu.enable_channels(*channels)
smu.disable_all_channels()
```

Enabling `MAIN_CURRENT` auto-enables `MAIN_POWER`; enabling `ADC_CURRENT`
auto-enables `ADC_POWER` — matching the device's behaviour.

`smu.get_channel_samplerate(channel)` returns the channel's documented
native rate. `smu.set_channel_samplerate(...)` raises
`BenchNotImplementedError` (deferred).

### Measurement / reads

```python
smu.read_value(channel, timeout=1.5) -> float
```

Returns the next sample for `channel`. Blocks up to `timeout` seconds.
Works whether or not a recording is active.

### Recordings

```python
rec = smu.start_recording(name="run", channels=None) -> Recording
rec = smu.stop_recording() -> Recording

with smu.record(*channels, name="run") as rec:
    ...
```

The `record()` context manager is the idiomatic form. If no channels are
passed, uses currently `enable_channels`'d set.

### Streaming

```python
for sample in smu.stream(seconds=10.0):
    ...
```

Yields `Sample(timestamp, value, channel)`. Cannot run concurrently with
a recording (raises `BenchValueError`).

### Raw access

```python
buf: bytes = smu.read_raw(seconds)
```

Drains the inbound stream for `seconds`. Escape hatch for unparsed bytes.

### GPIO

```python
smu.set_gpo(pin, state)
smu.get_gpi(pin) -> bool        # reads from latest GPI bitmap sample
```

### Deferred

All raise `BenchNotImplementedError` with a pointer to `ROADMAP.md`:

- `calibrate()`
- `firmware_upgrade(filename=None)`
- `enable_battery_profiling(enable)`
- `set_battery_profile(value)`
- `set_supply_battery_emulator(...)`
- `wait_for_battery_data(timeout)`
- `set_channel_samplerate(channel, value)`
- `iter_uart_log()`
- `write_tx(value)`
- `set_tx(value)`
- `get_rx()`

### Generic property passthrough

```python
smu.get_property(name) -> Any       # reads host-side cache
smu.set_property(name, value)
smu.commit()                        # no-op (every SET is sent immediately)
```

## `Channel` — enum of measurement channels

See [Getting started — Channels](getting_started.md#channels) for the
full table.

### Methods

- `Channel.from_code("mc")` — look up by short code
- `Channel.coerce(value)` — accept either enum or string

### Properties

`code`, `wire_id`, `subtype`, `sample_rate`, `unit`, `label`,
`toggleable`, `co_enables` — see `ChannelInfo` for descriptions.

## `Recording`

Immutable-after-stop container for captured samples.

### State

- `name: str`
- `offset: float` — seconds, applied to all queries
- `device_info: dict`
- `is_running: bool`
- `start_time / end_time: datetime | None`
- `channels: list[Channel]`

### Queries

```python
rec.info(channel) -> ChannelInfoResult
rec.statistics(channel, start=None, end=None) -> Statistics
rec.data(channel, start=None, end=None) -> list[float]
rec.timestamps(channel) -> list[float]
rec.index_at(channel, timestamp) -> int
rec.count(channel) -> int
rec.buffer(channel) -> ChannelBuffer
```

### Mutations

```python
rec.crop(start, end)
rec.downsample(channel, factor)
rec.rename(name)
rec.log(text, timestamp=0.0)
```

### Export / load

```python
rec.save_csv(path, format="long"|"wide", decimals=9) -> Path
rec.save_json(path) -> Path
rec.save_raw(path, buf) -> Path           # raw byte buffer
rec.save(path) -> Path                    # native .opensmu binary
Recording.load(path) -> Recording

# science extras (lazy imports — see output_formats.md)
rec.save_parquet(path) -> Path
rec.to_numpy(channel); rec.timestamps_numpy(channel)
rec.to_pandas(channel=None)
rec.plot(channels=None, show=True)
```

The `.opensmu` encoding is also available without a filesystem path.
These are the same codec, not a second format — remote mode transfers
recordings as `.opensmu` blobs, so they are the wire format too:

```python
rec.save_to_stream(fh) -> int             # bytes written
rec.to_bytes() -> bytes
Recording.load_from_stream(fh) -> Recording
Recording.from_bytes(data) -> Recording
```

`save()` / `load()` are thin wrappers over these. The only difference:
`load()` names the offending path in its error message, which
`load_from_stream()` cannot know.

### Deferred

`get_log_offset`, `set_log_offset`, `import_log`, `append_user_log` —
all raise `BenchNotImplementedError`.

## `Sample`

```python
@dataclass(frozen=True)
class Sample:
    timestamp: float
    value: float
    channel: Channel
```

## `Statistics`

```python
@dataclass(frozen=True)
class Statistics:
    channel: Channel
    sample_count: int
    duration: float
    min: float
    max: float
    average: float
    rms: float
    energy: float | None     # populated for power channels
    charge: float | None     # populated for current channels
```

## `ChannelInfoResult`

```python
@dataclass(frozen=True)
class ChannelInfoResult:
    offset: float
    from_time: float       # alias for the official `from` field
    to: float
    sample_rate: int
    count: int
    channel: Channel
```

Has `as_dict()` returning a dict in the official client's format.

## Exception hierarchy

```
BenchError
├── BenchConnectionError       (also OSError? no — distinct)
├── BenchProtocolError
├── BenchCommandError          (RuntimeError) — error_code, last_good_value, command_code
├── BenchValueError            (ValueError)
├── BenchTimeoutError          (TimeoutError)
└── BenchNotImplementedError   (NotImplementedError)
```

## Logging

The library uses the standard `logging` module under the `benchctrl`
logger. Sub-loggers:

- `benchctrl.device` — SET commands, recording lifecycle, reader stats
- `benchctrl.protocol` — frame hex dumps (DEBUG only)
- `benchctrl.battery.emulator` — emulator loop tick + warnings
- `benchctrl.battery.profiler` — profiler step transitions
- `benchctrl.drivers.eastwood_qr10x` — QR10x AT command traffic
- `benchctrl.drivers.rigol_dl3031a` — DL3031A SCPI traffic
- `benchctrl.mcp` — MCP server lifecycle + tool warnings

Enable hex dumps:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("benchctrl.protocol").setLevel(logging.DEBUG)
```

---

## `benchctrl.battery` — battery characterisation + emulation

Four phased modules: profile I/O, life calculator, profiler, emulator.
Detailed walkthrough in [`battery.md`](battery.md); this section is
the API surface.

```python
from benchctrl.battery import (
    BatteryProfile, Battery, DischargeTable, DischargeSample,
    DischargeProfile, DischargeStep, ExitConditions, DeviceInfo,
    LifeEstimate, DutyCycle,
    estimate_life_constant_current, estimate_life_from_profile,
    duty_cycle_from_recording,
    Profiler, ProfilerConfig, ProfilerResult, ProfilerSample,
    Emulator, EmulatorConfig, EmulatorState,
)
```

### Battery profile I/O

```python
BatteryProfile.load(path: str | Path) -> BatteryProfile
BatteryProfile.from_json(data: str | dict) -> BatteryProfile
profile.save(path: str | Path) -> None
profile.to_json() -> str
profile.ocv_at(used_capacity_mAh: float, temperature: float = None) -> float
profile.esr_at(used_capacity_mAh: float, temperature: float = None) -> float
```

`BatteryProfile` is a dataclass with `battery: Battery`,
`discharge_tables: list[DischargeTable]`, `device: DeviceInfo`, and
some lookup helpers. Round-trip is bit-identical with Otii's bundled
profile JSON format.

### Life calculator

```python
estimate_life_constant_current(
    profile: BatteryProfile, current_A: float, temperature: float = None,
) -> LifeEstimate

estimate_life_from_profile(
    battery_profile: BatteryProfile, recording: Recording,
    *, sample_rate_hz: float = None, temperature: float = None,
) -> LifeEstimate

duty_cycle_from_recording(recording: Recording) -> DutyCycle
```

`LifeEstimate` carries `runtime_s`, `used_capacity_mAh`,
`drained_to_cutoff: bool`, and trace data. Used both standalone and
as the analysis side of profiler / emulator workflows.

### Profiler

```python
class Profiler:
    def __init__(self, smu: SourceMeasurementUnit, config: ProfilerConfig): ...
    def run(self) -> ProfilerResult: ...

ProfilerConfig(
    discharge_steps: list[DischargeStep],
    sample_window_s: float = 1.0,
    settle_s: float = 0.1,
    ocv_relax_s: float = 60.0,
    safety_max_voltage_V: float = 5.0,
    safety_min_voltage_V: float = 0.5,
    voltage_channel: Channel = Channel.MAIN_VOLTAGE,
    current_channel: Channel = Channel.MAIN_CURRENT,
)
```

`Profiler` drives a discharge sequence against a real cell and
records V/I to produce a `ProfilerResult`. Result can be written to
a `BatteryProfile` for use with the life calculator or emulator.

### Emulator

```python
class Emulator:
    def __init__(self, smu: SourceMeasurementUnit, config: EmulatorConfig): ...
    def start(self) -> None: ...
    def stop(self) -> None: ...
    def state(self) -> EmulatorState: ...

EmulatorConfig(
    profile: BatteryProfile,
    initial_soc: float = 1.0,
    series: int = 1,
    parallel: int = 1,
    temperature: Optional[float] = None,
    soc_tracking: bool = True,
    safety_max_voltage_V: float = 5.0,
    current_limit_A: float = 0.5,
    voltage_range: Optional[str] = None,   # auto-select if None
    update_interval_s: float = 0.01,
    safety_max_used_mAh: Optional[float] = None,
    soc_floor: float = 0.0,
)

EmulatorState(
    soc: float,
    used_capacity_mAh: float,
    ocv_V: float,
    esr_ohm: float,
    output_voltage_V: float,
    measured_current_A: float,
    runtime_s: float,
    iteration: int,
    stop_reason: Optional[str],
)
```

`Emulator.start()` arms the SMU (range, current limit, CV regulation)
and spawns a daemon thread running the 100 Hz control loop
`V = OCV(SoC) − I·ESR(SoC)`. Failures during the configuration
sequence propagate (no silent swallow — see CONTRIBUTING.md § 4).
`stop()` is idempotent and disables the output.

`state()` returns a thread-safe snapshot — call it from any thread.

---

## `benchctrl.drivers` — companion instrument drivers

```python
from benchctrl.drivers.eastwood_qr10x import QR10x, QR10xInfo, QR10xError
from benchctrl.drivers.rigol_dl3031a import RigolDL3031A, RigolDLInfo, RigolDLError
from benchctrl.drivers.rigol_dp2031 import RigolDP2031, RigolDP2031Info, RigolDP2031Error
```

Each driver lives in its own subpackage, so importing one never pulls
in another's dependencies — no pyvisa import error just for using the
QR10x.

The DP2031 driver has the largest surface in the codebase (134 MCP
tools) covering the Arb timer sequencer, the IoT power analyzer,
trigger I/O and the device filesystem. It is documented in full in
[`drivers.md`](drivers.md#rigol-dp2031--triple-output-programmable-psu)
rather than duplicated here.

### `QR10x` — Eastwood Tech programmable resistor

```python
QR10x.open(port: str, baudrate: int = 115200) -> QR10x
qr.close() -> None
qr.is_open: bool
qr.port: str

qr.info() -> QR10xInfo

qr.set_resistance(ohms: float) -> dict             # returns full state dump
qr.get_setpoint() -> float                          # commanded R (Ω)
qr.actual_resistance() -> float                     # achieved R, PV (Ω)
qr.incr(delta_ohm: float) -> dict
qr.decr(delta_ohm: float) -> dict

qr.set_safety_limit(ohms: float) -> dict            # RLIMIT — clamps min R
qr.get_safety_limit() -> float
qr.get_temperature() -> float                       # °C
```

`QR10xInfo(device_type, serial, hardware_version, firmware_version,
production_date, temperature_coefficient_ppm)`.

`QR10xError` hierarchy: `QR10xError` → `QR10xConnectionError` /
`QR10xProtocolError` / `QR10xTimeoutError` / `QR10xValueError`.

Wire format: 115200 8N1, AT command set, ~60 ms quiet-window
end-of-response detection. Details in [`drivers.md`](drivers.md).

### `RigolDL3031A` — Rigol DL3000-series electronic load

```python
RigolDL3031A.open(resource: Optional[str] = None, *,
                  timeout_ms: int = 2000,
                  read_termination: str = "\n",
                  write_termination: str = "\n") -> RigolDL3031A
dl.close() -> None

dl.info() -> RigolDLInfo                            # parsed *IDN?
dl.reset() -> None                                  # *RST
dl.clear_status() -> None                           # *CLS
dl.last_error() -> Optional[tuple[int, str]]        # :SYST:ERR? (None if clean)
dl.raise_if_error() -> None                         # raises on non-zero
```

**Low-level transport** (for SCPI commands not yet wrapped):

```python
dl.write(command: str) -> None
dl.query(command: str) -> str
dl.query_float(command: str) -> float
dl.query_int(command: str) -> int
```

**Mode and input**:

```python
dl.set_mode("CC" | "CV" | "CR" | "CP") -> None
dl.get_mode() -> str
dl.set_input(on: bool) -> None
dl.get_input() -> bool
```

**Per-mode setpoints + getters**:

```python
dl.set_current(amps) / get_current()
dl.set_voltage(volts) / get_voltage()
dl.set_resistance(ohms) / get_resistance()
dl.set_power(watts) / get_power()
```

**Ranges + slew**:

```python
dl.set_current_range(amps) / get_current_range()
dl.set_voltage_range(volts) / get_voltage_range()
dl.set_slew(amps_per_us) / get_slew()
```

**Measurements**:

```python
# Triggers a fresh 10 PLC (~200 ms) integration per call.
dl.measure_voltage() / measure_current() / measure_power() / measure_resistance()
dl.measure_all() -> dict[str, float]

# Non-blocking reads of the device's continuously-updated register.
# All four fetch_* reads share the same ~200 ms integration window.
dl.fetch_voltage() / fetch_current() / fetch_power() / fetch_resistance()
dl.fetch_all() -> dict[str, float]
```

**Function mode** (top-level regulation source):

```python
dl.set_function_mode("FIXed" | "LIST" | "WAVe" | "BATTery" | "OCP" | "OPP") -> None
dl.get_function_mode() -> str
```

Note: `:SOUR:FUNC:MODE FIXed` is one-way under the current firmware
— once the device enters LIST / WAV / BATT / OCP / OPP, only a
power-cycle restores FIX. See KNOWN_LIMITATIONS § F-3.

**Trigger system**:

```python
dl.set_trigger_source("BUS" | "EXTernal" | "MANUal") -> None
dl.get_trigger_source() -> str
dl.trigger_now() -> None                            # :TRIGger (software trigger)
```

**LIST sequence mode** (firmware-timed, sub-100 µs widths):

```python
dl.program_list(
    steps: list[tuple[float, float]],               # (level, width_s)
    *, mode: str = "CC", count: int = 1,
    range_value: Optional[float] = None,
    slew_A_per_us: Optional[float] = None,
    end_behavior: str = "OFF",                       # "OFF" | "LAST"
    trigger_source: str = "BUS",
) -> None

# Granular SCPI wrappers (also available, see drivers.md):
dl.list_set_mode / list_set_range / list_set_count / list_set_step_count /
   list_set_step / list_set_slew / list_set_end
```

`steps` must contain 2 to 512 entries, **but not 4** — STEP=4 is a
firmware bug that fires no steps; the driver rejects 4-step programs
at the SDK boundary (KNOWN_LIMITATIONS § F-1).

**CC transient mode** (A↔B pulse generator):

```python
dl.configure_transient_pulse(
    *, a_level_A: float, b_level_A: float,
    a_width_s: float, b_width_s: float,
    mode: str = "CONTinuous",                        # CONT / PULS / TOGG
) -> None
dl.transient_enable(on: bool) -> None

# Granular setters: transient_set_mode / set_a_level / set_b_level /
#                   set_a_width / set_b_width / set_frequency / set_duty
```

`transient_set_frequency(hz)` takes **Hz** (not kHz despite the
manual's claim — bench-verified, KNOWN_LIMITATIONS § F-4).

**Battery discharge mode** (firmware-side test with stop conditions):

```python
dl.configure_battery_test(
    *, current_A: float,
    v_stop_V: Optional[float] = None,
    capacity_stop_mAh: Optional[float] = None,
    time_stop_s: Optional[float] = None,
    von_V: Optional[float] = None,
    range_A: Optional[float] = None,
) -> None

dl.battery_stats() -> dict[str, float]
# {capacity_mAh, energy_Wh, discharge_time_s, voltage_V, current_A}
```

The `discharge_time_s` field is parsed from the device's actual
return format (H:MM:SS), not the float documented in the manual.

**Exception hierarchy**:

```
RigolDLError
├── RigolDLConnectionError    open / VISA transport failure
├── RigolDLCommandError       device -101 etc. from :SYST:ERR?
├── RigolDLTimeoutError       VI_ERROR_TMO
└── RigolDLValueError         client-side range / type check failed
```

`RigolDLInfo(manufacturer, model, serial, firmware, resource)`.

Details + firmware quirks in [`drivers.md`](drivers.md). Hardware-marked
tests in `tests/test_bench_rigol_dl3031a.py` exercise every wire
format against the real device.

## `benchctrl.session` — the local/remote/sim seam

One function decides, per device, what a driver singleton actually
gets. Everything above it — the 226 MCP tools, the battery emulator,
the scenario harness — is unaware.

```python
from benchctrl import session

session.resolve(
    device_key: str,                     # one of config.DEVICE_KEYS
    *,
    opener: Callable[..., Any],          # normally the driver's .open
    open_kwargs: dict | None = None,
) -> Any
```

Returns a real driver instance, a simulated instrument, or a remote
proxy. All three satisfy the same duck type. For a remote device
`open_kwargs` is forwarded to the *agent*, so the bench opens the
device the same way you would locally.

## `benchctrl.config` — layered configuration

```python
from benchctrl import config

config.DEVICE_KEYS   # ("otii_arc", "eastwood_qr10x",
                     #  "rigol_dl3031a", "rigol_dp2031")
config.MODES         # ("local", "remote", "sim")
config.DEFAULT_PORT  # 9737

config.resolve(path=None, cli=None, env=None) -> Config
config.build(*, remote=None, token=None,
             local_devices=(), sim_devices=()) -> Config
config.load_file(path) -> Config
config.load_env(env=None) -> Config | None
```

Precedence: **explicit > CLI > env > file > all-local**. With nothing
set, every device is local and behaviour is identical to a build
without any of this.

### `Config`

```python
cfg.device(key) -> DeviceConfig          # defaults to local
cfg.mode_for(key) -> str
cfg.endpoint_for(key) -> EndpointConfig
cfg.is_all_local() -> bool
cfg.to_dict() / Config.from_dict(d)
```

`endpoint_for()` raises `BenchValueError` if a device is marked remote
but names no reachable endpoint. That is deliberate — the alternative
is silently falling back to local and driving the wrong hardware.

`EndpointConfig` rejects `deadman_s <= heartbeat_s` at construction,
which would otherwise make a healthy link trip the safety governor.

### Environment variables

| Variable | Effect |
|---|---|
| `BENCHCTRL_REMOTE=host[:port]` | Bind every device to that agent |
| `BENCHCTRL_TOKEN=...` | Shared secret for the handshake |
| `BENCHCTRL_LOCAL_DEVICES=a,b` | Force these keys back to local |
| `BENCHCTRL_SIM_DEVICES=a,b` | Force these keys to simulated |
| `BENCHCTRL_CONFIG=/path.json` | Alternate config file |

### File format

JSON rather than TOML, because `tomllib` is 3.11+ and the package
supports 3.9.

```json
{
  "endpoints": {
    "bench": { "host": "bench.local", "port": 9737,
               "heartbeat_s": 2.0, "deadman_s": 10.0 }
  },
  "devices": {
    "otii_arc":      { "mode": "remote", "endpoint": "bench" },
    "rigol_dp2031":  { "mode": "sim" },
    "eastwood_qr10x": { "mode": "local" }
  }
}
```

## `benchctrl.discovery` — what is on this bench

One signature table replacing three ad-hoc mechanisms. Answers "what
is on this bench", not "where is my device".

```python
from benchctrl import discovery

discovery.discover() -> list[DiscoveredDevice]
discovery.find_for(device_key, **kw) -> list[DiscoveredDevice]
discovery.unidentified(**kw) -> list[DiscoveredDevice]
discovery.inventory(**kw) -> dict
discovery.format_inventory(devices) -> str

# individual transports, if you want just one
discovery.scan_serial() / scan_usbtmc() / scan_visa(rm=None)
discovery.probe_serial_identity(path, timeout=1.0) -> str | None
```

Each result carries a **confidence level**. Devices sitting behind
generic USB-serial bridges (CH340, FTDI, CP210x) are never claimed
outright, because those VID/PIDs are shared by thousands of unrelated
products and a guess produces confident false positives. The QR10x
has no recorded VID/PID at all and is identified by AT probe.

A test asserts that no driver signature collides with a known bridge.

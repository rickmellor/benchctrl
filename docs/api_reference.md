# OpenSMU API reference

Every public name exported from `opensmu`. Internal modules
(`opensmu.transport`, `opensmu.protocol`, etc.) are documented for
contributors but not part of the stability surface.

## `SMU` — the device

### Construction

```python
SMU.discover() -> list[SMUInfo]
SMU.open(port=None, *, baudrate=9600) -> SMU
```

`port` accepts `None` (auto-discover first device), a port string
(`"COM6"`, `"/dev/ttyACM0"`), an `SMUInfo`, or a `PortInfo`. The
returned `SMU` has its transport open and the session-init handshake
sent. Use as a context manager (`with SMU.open() as smu:`) or call
`smu.close()` explicitly.

### State properties (read-only, host-side cache)

| Property | Type | Notes |
|---|---|---|
| `info` | `SMUInfo \| None` | Discovery-time descriptor |
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
`SMUValueError` (client-side range check) or `SMUCommandError`
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
`SMUNotImplementedError` (deferred).

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
a recording (raises `SMUValueError`).

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

All raise `SMUNotImplementedError` with a pointer to `ROADMAP.md`:

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
```

### Deferred

`get_log_offset`, `set_log_offset`, `import_log`, `append_user_log` —
all raise `SMUNotImplementedError`.

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
SMUError
├── SMUConnectionError       (also OSError? no — distinct)
├── SMUProtocolError
├── SMUCommandError          (RuntimeError) — error_code, last_good_value, command_code
├── SMUValueError            (ValueError)
├── SMUTimeoutError          (TimeoutError)
└── SMUNotImplementedError   (NotImplementedError)
```

## Logging

The library uses the standard `logging` module under the `opensmu`
logger. Sub-loggers:

- `opensmu.device` — SET commands, recording lifecycle, reader stats
- `opensmu.protocol` — frame hex dumps (DEBUG only)

Enable hex dumps:

```python
import logging
logging.basicConfig(level=logging.DEBUG)
logging.getLogger("opensmu.protocol").setLevel(logging.DEBUG)
```

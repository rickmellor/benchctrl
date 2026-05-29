# OpenSMU design

## Goals

1. **Replace the Qoitech vendor stack** for everyday measurement workflows.
   Connect, configure, record, analyse, export — all without `otii_server`,
   no Automation Toolbox license, no TCP socket.
2. **Modern Python**. Type hints everywhere. Dataclasses for value objects.
   Enums for choices. Context managers for resource lifecycles. Iterators
   for streams. No surprising mutable defaults.
3. **One obvious way** for each task.
4. **Hardware-free testable**. Framing, parsing, statistics — all unit-testable
   without a device. A hardware-required suite covers integration.
5. **Cross-platform**. pyserial-only. No platform-specific code paths.

## Non-goals

- Network protocol parity with the official server. We do **not** speak the
  Qoitech TCP protocol. Anyone running the official server gets no benefit
  from OpenSMU; the value proposition is *removing* that dependency.
- Project file compatibility. Recordings serialise to our own self-contained
  format plus standard CSV/JSON.
- Async API. The hardware is inherently sequential; the simple sync API is
  easier to use correctly. The single streaming iterator covers the
  "what's the value right now" use case without needing async machinery.

## Architecture

```
+----------------------------------------------------+
|  Application / examples / CLI                      |
+----------------------------------------------------+
|  opensmu.device.SMU      (public API)              |  <-- user-facing
|  opensmu.recording.Recording                       |
|  opensmu.channels.Channel                          |
|  opensmu.exceptions                                |
+----------------------------------------------------+
|  opensmu.samples         (sample parsing,          |  <-- pure functions
|                           statistics, exports)     |
+----------------------------------------------------+
|  opensmu.protocol        (frame encode/decode,     |  <-- pure functions
|                           command codes,           |
|                           channel-config payload)  |
+----------------------------------------------------+
|  opensmu.transport.Transport  (pyserial wrapper:   |  <-- I/O boundary
|                                open/close/         |
|                                read/write/probe)   |
+----------------------------------------------------+
|  pyserial                                          |
+----------------------------------------------------+
```

Layers do not skip. `Recording` never reaches into `Transport`. Tests are
written at every layer.

## Public API style

### Property reads, method writes

State that we know from the host-side cache (because we just wrote it) is
a **property**. State changes are **methods** with side effects.

```python
smu.set_voltage(3.3)        # method — may raise SMUCommandError
v = smu.voltage             # property — pure cache read, never raises
```

Rationale: writes can fail (device may reject out-of-range); reads of cached
state cannot. Mixing them as `smu.voltage = 3.3` makes it feel like attribute
assignment, which by convention is total. Methods make the failure mode honest.

### Channels are an enum

```python
from opensmu import Channel

smu.enable_channels(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE)
smu.read_value(Channel.MAIN_VOLTAGE)
```

The enum carries metadata: the official two-letter code (`"mc"`), the wire id,
the subtype, the native sample rate, the unit, and a human-friendly label.
Strings are accepted at API boundaries for backwards-friendly use
(`smu.enable_channels("mc", "mv")`), but the canonical form is the enum.

### Recordings are context managers and stand-alone files

```python
with smu.record() as rec:
    smu.set_output(True)
    time.sleep(5)
    smu.set_output(False)

# rec is now a complete, immutable-ish Recording
print(rec.statistics(Channel.MAIN_VOLTAGE))
rec.save_csv("run.csv")
rec.save("run.opensmu")     # native binary, lossless round-trip
```

`SMU.start_recording()` / `recording.stop()` work too, for cases where
the `with` block doesn't fit.

### Discovery returns rich descriptors, not raw paths

```python
infos = SMU.discover()                # list[SMUInfo]
for info in infos:
    print(info.port, info.name, info.serial)

smu = SMU.open(infos[0])              # accepts SMUInfo
smu = SMU.open(port="COM6")           # or by port
smu = SMU.open()                      # or auto-pick the first one
```

### Exceptions form a hierarchy

```
SMUError                       — base
├── SMUConnectionError         — port can't be opened / lost mid-stream
├── SMUProtocolError           — bad magic / bad checksum / unexpected frame
├── SMUCommandError            — device explicitly rejected a SET
│       .error_code            — signed int from device
│       .last_good_value       — what the parameter reverted to
│       .command_code          — which SET was rejected
├── SMUValueError              — client-side range check failed before send
├── SMUTimeoutError            — no samples within deadline
└── SMUNotImplementedError     — deferred feature (battery, calibration, ...)
```

`SMUCommandError` extends `RuntimeError`; `SMUValueError` extends
`ValueError`; `SMUNotImplementedError` extends `NotImplementedError` — so
existing exception-handling idioms still work.

## Wire protocol notes

OpenSMU implements the exact wire protocol reverse-engineered in the
parent project (see `docs/protocol.md`). Key points the code relies on:

- Frame: `A3 2C B5 7F` magic + `u16` length + `u16` checksum + payload.
- Checksum is `sum(payload) & 0xFFFF`.
- SET-parameter payload: `[seq:u32][type=0x66:u32][cmd:u32][value:u32]`.
- Session must be primed with three init frames before the device streams.
- Sample frames have type 0x0002 and pack channel id + value.
- Error frames have header `0e 03 99 ff 04 10 00 00` and carry a signed
  i32 error code + the value the parameter reverted to.

## Sample / statistics implementation

The parsing layer is pure functions. Inputs:

```python
parse_frames(buf: bytes) -> Iterator[Frame]
parse_samples(frames: Iterable[Frame]) -> dict[int, list[Sample]]
```

`Sample` is a frozen dataclass: `timestamp: float, value: float, channel_id: int`.

Statistics on a per-channel slice:

```python
@dataclass(frozen=True)
class Statistics:
    min: float
    max: float
    average: float
    sample_count: int
    duration: float
    rms: float | None         # numeric channels only
    energy: float | None      # only when 'me' / 'ae' computable
    charge: float | None      # only on current channels
```

`energy` (J) is computed by `sum(v_i * i_i) * dt` when both voltage and
current are present in the recording at matching timebases. `charge` (C)
is `sum(i_i) * dt`. Both are best-effort.

## Native file format

`.opensmu` is a small self-describing binary:

```
magic: b"OSMU\0\0\0\1"                # 8 B — version 1
header: msgpack-encoded dict
    {
        "schema_version": 1,
        "created_at": ISO-8601 string,
        "device": {"hw": "...", "fw": "...", "serial": "..."},
        "channels": [{"code": "mc", "wire_id": 0, "sample_rate": 4000, ...}, ...],
        "stat_summary": {"mc": {"min": ..., ...}, ...},
    }
header_len: u32 LE (precedes the header for streaming)
per-channel blocks:
    block_header: msgpack {"code": "mc", "count": N, "dtype": "f32" | "f64", "t0": 0.0, "dt": 0.00025}
    block_header_len: u32 LE
    block_data: N * sizeof(dtype) bytes (little-endian)
```

The format is self-contained: a `.opensmu` file plus the library is enough
to reproduce the statistics and chart shown in the desktop client.

If `msgpack` isn't installed, a JSON sidecar fallback is used (`.opensmu.json`
+ per-channel `.bin` blocks). Both round-trip identically through `Recording`.

## Threading model

`SMU` is **not thread-safe**. A single instance is owned by a single thread.
Multi-device use is fine when each device gets its own thread and its own
`SMU` instance — the underlying pyserial port is per-instance.

The streaming iterator (`SMU.stream(...)`) runs on the calling thread and
calls `select()` on the serial port. Recording uses an internal background
reader thread to drain the bulk-IN stream without blocking command writes.

## Logging

Standard `logging` under the `opensmu` logger. The library never configures
handlers — that's the application's job. Hex dumps of frames are logged at
DEBUG when `logging.getLogger("opensmu.protocol").setLevel(logging.DEBUG)`.

## Versioning

Semver. v0.x is unstable. v1.0 is the first stability commitment.

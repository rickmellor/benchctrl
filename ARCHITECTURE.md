# Architecture

One-page tour of the benchctrl codebase. Read this before
[`docs/design.md`](docs/design.md) — design.md covers the SMU layer in
depth; this file is the wide view.

## The five subsystems

```
                          ┌──────────────────────────────────────┐
                          │   MCP server  (benchctrl.mcp)          │
                          │   93 tools — thin wrappers around    │
                          │   the SDK methods below              │
                          └──────────────────────────────────────┘
                              │           │            │
                              ▼           ▼            ▼
              ┌───────────────────┐ ┌──────────┐ ┌──────────────┐
              │   benchctrl.SMU     │ │ battery  │ │   bench      │
              │   (Arc / Arc Pro) │ │ profile/ │ │ QR10x        │
              │                   │ │ profiler/│ │ RigolDL3031A │
              │ source • measure  │ │ emulator/│ │              │
              │ record • stream   │ │ life-calc│ │              │
              └───────────────────┘ └──────────┘ └──────────────┘
                        ▲                ▲            ▲
                        │                │            │
                        │       ┌────────┴────────────┘
                        │       │
                        │       │  (load adapter abstracts
                        │       │   QR10x vs DL3031A)
                        │       │
                        │       ▼
                ┌───────────────────────────────────┐
                │   validation/run_validation.py    │
                │   reproducible scenario harness   │
                │   (static / dynamic / dynamic-list)│
                └───────────────────────────────────┘
```

Five subsystems, three layers. The bottom layer (SMU + bench) talks
directly to instruments. The middle layer (battery) builds on SMU but
not on bench. The top layer (MCP, validation) consumes everything but
doesn't expose it to lower layers.

## Layer 1 — instrument I/O

### `benchctrl.SMU` and friends

The original library. Drives the Otii Arc / Arc Pro over USB CDC-ACM.
Internal layering (covered in detail in [`docs/design.md`](docs/design.md)):

```
SMU + Recording        public API
        ↓
samples + protocol     pure functions (frame encode/decode, stats, exports)
        ↓
transport              pyserial wrapper (open/close/read/write/probe)
        ↓
pyserial
```

Public surface: `SMU`, `Recording`, `Channel`, the `BenchError`
hierarchy. Wire-protocol details and channel IDs are internal.

Key files:
- `src/benchctrl/device.py` — `SMU` class
- `src/benchctrl/recording.py` — `Recording` class
- `src/benchctrl/protocol.py` — frame framing, command codes
- `src/benchctrl/samples.py` — sample parsing, statistics, exports
- `src/benchctrl/transport.py` — pyserial wrapper
- `src/benchctrl/channels.py` — `Channel` enum

Hardware constants (USB VID/PID, channel wire-IDs, command opcodes)
live in `protocol.py` and `channels.py`. The session-init handshake
the device requires is implemented in `SMU.open()`.

### `benchctrl.bench` — companion instrument drivers

Parallel to `SMU`, not built on top of it — both subsystems are
peers at the I/O boundary. Bench drivers exist so a battery emulator
running on the Arc can have a real DUT load attached without users
having to write their own RS232 / VISA glue.

Each driver is independent:

- `benchctrl.bench.QR10x` — Eastwood Tech programmable resistor.
  USB-Serial via pyserial. Private AT command set. ~280 lines.
- `benchctrl.bench.RigolDL3031A` — Rigol DL3000-series electronic
  load. USB-TMC + SCPI via pyvisa (or LAN/RS232). LIST sequence
  mode + transient + battery-discharge + trigger system. ~900
  lines.

`benchctrl.bench.__init__` uses PEP 562 lazy attribute lookup so
`from benchctrl.bench import QR10x` doesn't pull in pyvisa for users
who only have the QR10x.

Drivers expose their own exception hierarchies
(`QR10xConnectionError`, `RigolDLCommandError`, etc.) so callers can
catch instrument-specific errors without depending on the SMU's
hierarchy.

Manufacturer-specific quirks (Rigol's `:SOUR:LIST:STEP=4` firmware
bug, `:SOUR:LIST:STEP` actually being "play N steps" not "highest
index", `:FETCh:DISChargingTime?` returning H:MM:SS not float,
`transient_set_frequency` taking Hz not kHz despite the manual) are
documented in driver docstrings and in
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) § F-1 through F-4.
The driver rejects known-bad inputs at the SDK boundary.

## Layer 2 — battery toolbox

`benchctrl.battery` replaces Qoitech's licensed Battery Toolbox in four
phased modules:

```
profile.py          BatteryProfile dataclass + Otii JSON round-trip
        │
        ├─→ life_calculator.py    predict runtime given profile + load
        │
        ├─→ profiler.py           drive a discharge sweep; emit a fresh profile
        │
        └─→ emulator.py           run a 100 Hz control loop:
                                  V = OCV(SoC) − I·ESR(SoC)
```

The emulator is the most complex piece. It runs a daemon thread that
polls the SMU's main current channel, integrates charge to update
SoC, looks up OCV and ESR from the discharge table, and writes the
result back via `set_voltage`. Safety: explicit clamp to
`safety_max_voltage_V` at startup (the Arc Pro's high range tops out
at ~4.2 V under load — see KNOWN_LIMITATIONS § H-1) and a watchdog
that stops on `safety_max_used_mAh` or `soc_floor`.

The profiler runs in the opposite direction — drives load steps and
records V/I to characterize a real cell into the Otii JSON format.

## Layer 3 — composition

### `benchctrl.mcp` — Model Context Protocol server

Wraps the entire SDK as MCP tools so LLM agents can drive a real
bench. 93 tools at v0.9.7:

| Subsystem | Tools |
|---|---|
| SMU (connect, configure, source/measure, record) | ~30 |
| Battery (profile lookup, life calc, emulator) | ~10 |
| QR10x bench driver | 11 |
| RigolDL3031A bench driver | 45 |
| Recording I/O (export, summary, plot) | ~8 |

The MCP layer is intentionally thin: each tool wraps one SDK method,
coerces JSON-friendly argument types where needed, returns a dict.
**Single-client serialization** is assumed — running two concurrent
MCP clients against the same server is unsupported (documented in
the module docstring).

The SDK ↔ MCP parity principle is load-bearing: every public SDK
method has a matching MCP tool. The v0.9.7 review caught ~20 missing
parity entries on the DL3031A driver, all fixed in the same release.

### `validation/` — reproducible scenario harness

Top-level directory, not under `src/benchctrl/`. Contains a CLI that
drives the emulator against a programmable load and saves the
captured response as a self-describing artifact bundle.

```
validation/
├── run_validation.py          CLI + scenario runners
├── README.md                  harness docs + headline results
└── scenarios/
    └── <profile>_<scenario>_<load>_<utc>.{json,csv,png}
        plus a copy of the input battery profile JSON
```

The harness uses an internal `_LoadAdapter` abstraction so a single
scenario runner can target either QR10x or DL3031A. Three scenario
kinds:

- `static` — load sweep at QR's relay timing, V/I snapshot per step
- `dynamic` — host-driven IoT pattern at 20 Hz polling
- `dynamic-list` — DL3031A's firmware LIST mode + Arc Pro's native
  ~4 kHz `SMU.record()` streaming

The harness is meant to be **regression-quality**: re-running a
saved scenario on a new build should produce the same V/I curves
modulo hardware noise. Each saved scenario embeds a copy of the
input battery profile so it's fully reproducible without the
external Otii profile bundle.

## Cross-cutting concerns

### Output formats

`benchctrl.samples` knows how to export `Recording` data in:

- Native `.opensmu` binary (lossless, the canonical format)
- CSV / JSON
- pandas DataFrame
- numpy arrays
- Parquet (via pyarrow)
- matplotlib PNG

Each non-native format is a lazy import — `pip install benchctrl`
gives you `.opensmu` + CSV + JSON; the rest gate behind `[science]`
extras. Format selection happens at the call site
(`rec.save_csv(...)` / `rec.save_parquet(...)`).

### Threading

Only one piece of the system runs background threads:

- `Emulator._loop` — daemon thread at 100 Hz reading current and
  writing voltage to the SMU
- `SMU._reader_thread` (during `start_recording`) — daemon thread
  consuming inbound bytes and demuxing into channel buffers

These two cannot run concurrently against the same Arc Pro — they
deadlock at the transport layer (`KNOWN_LIMITATIONS` § A-1). The
hires validation runner sidesteps the deadlock by stopping the
emulator before opening the recording.

### Error model

Two-level exception hierarchy:

```
BenchError                                 (in benchctrl.exceptions)
├── BenchConnectionError                   transport / open failure
├── BenchValueError                        client-side validation
├── BenchTimeoutError                      no expected response
├── BenchCommandError                      device rejected with -101 etc.
└── BenchNotImplementedError               vendor-only methods we don't expose

QR10xError                               (in benchctrl.bench.qr10x)
├── QR10xConnectionError
├── QR10xProtocolError
├── QR10xTimeoutError
└── QR10xValueError

RigolDLError                             (in benchctrl.bench.rigol_dl3031a)
├── RigolDLConnectionError
├── RigolDLCommandError                  device -101 from :SYSTem:ERRor?
├── RigolDLTimeoutError                  VI_ERROR_TMO
└── RigolDLValueError
```

Each driver has its own hierarchy so user code can catch
instrument-specific errors without coupling to SMU's hierarchy. The
SCPI error queue is drained explicitly via `raise_if_error()` on
DL3031A; the Arc Pro's async error frames are detected in-band by
the transport reader.

### Testing pattern

`tests/` has two kinds of tests:

- **Hardware-free** (default): use `FakeInstrument` / mock objects
  that record SCPI writes and reply to queries from a scripted dict.
  Run in ~3 minutes, no device needed. 292 tests at v0.9.7.
- **Hardware-marked** (`@pytest.mark.hardware`): require real
  instruments. Skip gracefully if hardware is absent. ~90 tests at
  v0.9.7.

The mocks mirror real-instrument method names exactly so a renamed
method breaks tests. (The v0.9.7 release was prompted in part by a
mock that omitted v0.9.1's new SMU methods, which combined with a
silent try/except to hide a real bug.)

## What this isn't

A few intentional non-goals worth knowing:

- **Not a vendor protocol bridge.** We don't speak Otii's TCP
  protocol or implement its server. Use the official client +
  server if you need that.
- **Not async.** Hardware is sequential. The streaming iterator
  covers "what's the value right now" without async machinery.
- **Not a GUI.** The MCP server is the closest thing — agents drive
  through it. Plot output is matplotlib PNG.
- **Not multi-device coordinated.** One Arc per `SMU` instance.
  Coordinating multiple Arcs is a v1.0+ item.

## See also

- [`docs/design.md`](docs/design.md) — SMU-layer architecture in depth
- [`docs/getting_started.md`](docs/getting_started.md) — tutorial
- [`docs/api_reference.md`](docs/api_reference.md) — exhaustive API
- [`docs/protocol.md`](docs/protocol.md) — USB wire protocol
- [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) — caps, quirks,
  workarounds
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, conventions

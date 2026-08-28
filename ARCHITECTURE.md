# Architecture

One-page tour of the benchctrl codebase. Read this before
[`docs/design.md`](docs/design.md) — design.md covers the Otii Arc
driver in depth; this file is the wide view.

## Driver-symmetric layout

Every instrument benchctrl can talk to is a peer driver under
`benchctrl.drivers.<vendor_model>/`. The Otii Arc / Arc Pro is one
driver among others; battery emulation, scenarios, analytics, and the
MCP server build on top of a Protocol the drivers conform to, not on
any concrete driver.

```
                           ┌──────────────────────────────────────────┐
                           │   MCP server  (benchctrl.mcp)            │
                           │   324 tools — orchestrator that calls    │
                           │   each driver's register_mcp_tools(mcp)  │
                           └──────────────────────────────────────────┘
                                          │
                           ┌──────────────┴───────────────────────────┐
                           │  benchctrl.session — local / remote / sim│
                           │  resolves each device key independently  │
                           └──────────────────────────────────────────┘
                              │                │                 │
                              ▼                ▼                 ▼
              ┌──────────────────────┐  ┌──────────────┐  ┌──────────────┐
              │ drivers.otii_arc     │  │ drivers.     │  │ drivers.     │
              │   OtiiArc            │  │ eastwood_    │  │ rigol_       │
              │   OtiiArcChannel     │  │ qr10x.QR10x  │  │ dl3031a.     │
              │   mcp_tools          │  │   mcp_tools  │  │ RigolDL3031A │
              │                      │  │              │  │   mcp_tools  │
              └──────────────────────┘  └──────────────┘  └──────────────┘
                        ▲                       ▲                ▲
                        │                       │                │
              ┌─────────┴───────────────────────┴────────────────┘
              │            SourceMeasurementUnit Protocol
              │            (benchctrl.interfaces)
              │            StandardChannel (benchctrl.channels)
              ▼
       ┌────────────────────┐         ┌────────────────────────┐
       │ benchctrl.battery  │         │ scenarios/             │
       │   Profiler         │         │   run.py harness       │
       │   Emulator         │         │   reproducible bench   │
       │   BatteryProfile   │         │   experiments          │
       │   life calculator  │         │                        │
       └────────────────────┘         └────────────────────────┘
```

Three layers. The bottom layer is drivers. The middle layer is the
Protocol contract + framework primitives (Recording, samples,
StandardChannel, BenchError). The top layer is vendor-agnostic
subsystems that depend only on the Protocol (battery emulator,
scenarios harness) plus the MCP orchestrator that wires it all
together.

## Layer 1 — drivers

Every driver lives in `src/benchctrl/drivers/<vendor_model>/`. Each
subpackage owns its wire protocol, transport, channels (if any), and
MCP tool surface.

### Conventions

- `__init__.py` — re-exports the driver's public surface (class,
  info dataclass, exception hierarchy) and nothing else.
- `device.py` or `driver.py` — the main class.
- `transport.py`, `protocol.py`, `channels.py` — driver-internal
  modules. Only the Arc driver has all three today.
- `mcp_tools.py` — driver-specific MCP tool functions plus
  `register_mcp_tools(mcp)`. Connection singletons live here so
  tests can inject fakes by mutating the driver module directly.

### Current drivers

- `benchctrl.drivers.otii_arc.OtiiArc` — Qoitech Otii Arc / Arc Pro
  source-measurement unit. USB CDC-ACM, reverse-engineered binary
  wire protocol (see [`docs/otii_arc_protocol.md`](docs/otii_arc_protocol.md)).
  Conforms to `SourceMeasurementUnit` Protocol.
- `benchctrl.drivers.eastwood_qr10x.QR10x` — Eastwood Tech
  programmable resistor. USB-Serial via pyserial. AT command set.
  Concrete class; doesn't fit the SMU Protocol (it's pure load).
- `benchctrl.drivers.rigol_dl3031a.RigolDL3031A` — Rigol DL3000-series
  electronic load. USB-TMC + SCPI via pyvisa (or LAN/RS232). LIST
  sequence + transient + battery-discharge + trigger system.
  Concrete class.
- `benchctrl.drivers.rigol_dp2031.RigolDP2031` — Rigol DP2000-series
  three-channel programmable PSU. USB-TMC + SCPI via pyvisa. Concrete
  class; the largest tool surface in the codebase.
- `benchctrl.drivers.siglent_sdm4065a.SiglentSDM4065A` — Siglent
  SDM4000-series 6½-digit bench DMM. USB-TMC + SCPI via pyvisa (or
  LXI). Concrete class; the first *measurement-only* driver, so it
  doesn't fit the SMU Protocol either — it sources nothing. No
  `DigitalMultimeter` Protocol was introduced: per `CONTRIBUTING.md`
  rule 3, that waits for the second DMM.
- `benchctrl.drivers.cyberpower_pdu41002.CyberPowerPDU41002` —
  CyberPower 8-outlet switched PDU. One line-oriented CLI engine over
  either of two byte pipes: pyserial, or `/usr/bin/ssh` under
  `pty.fork()`. Concrete class; the first driver that switches rather
  than sources or measures, and the first needing a credential.
- `benchctrl.drivers.silabs_cp2112.CP2112` — Silicon Labs CP2112,
  8 open-drain control lines for hardware reset and boot-strap pins. USB
  HID *feature reports* over `hidraw`, with `os`/`fcntl`/`ctypes` only.
  Concrete class. No `ControlLine` Protocol was introduced: this is the
  bench's first digital-control device, and a Protocol generalised from
  one sample would bake in CP2112 specifics — whole-port configuration
  registers, a USB round trip per transition — that a GPIO expander or a
  relay board would not share. Only the GPIO half of the chip is
  implemented; the SMBus/I²C bridge is out of scope.
- `benchctrl.drivers.ontrak_adu218.OntrakADU218` — Ontrak ADU218,
  8 relays and 8 digital inputs with event counters and a hardware
  watchdog. USB HID, spoken through raw `USBDEVFS` ioctls with
  `fcntl`/`ctypes`/`os` — **the only driver with no dependency at all**,
  not even pyserial. Concrete class. No `Switch` Protocol was
  introduced: the PDU switches mains outlets and this switches signal
  contacts, and generalising one shape from two that disagree would
  produce an interface that fits neither.

Each driver exposes its own exception hierarchy
(`QR10xConnectionError`, `RigolDLCommandError`, etc.) so callers can
catch instrument-specific errors without coupling to other drivers.
Each has exactly four types — Connection / Command / Timeout / Value —
except the SDM4065A, which adds `SDM4065AOverloadError` for the
`9.9E37` out-of-range sentinel. That is a distinct *recoverable*
condition (widen the range and retry), and the sentinel is a valid
float that would otherwise propagate into arithmetic as a believable
reading.

## Layer 2 — Protocol + framework primitives

### `benchctrl.interfaces.SourceMeasurementUnit`

`@runtime_checkable` Protocol describing the surface any vendor-agnostic
subsystem needs from an SMU: source-side setters
(`set_voltage`, `set_main_current`, `set_output`, `set_range`,
`set_power_regulation`, `set_current_limit`,
`set_current_limit_enabled`), measurement (`read_value`, `read_window`,
`record`), and identity (`get_fw_version`, `get_device_id`).

Today only the Arc driver implements it. New SMUs (Keithley 24xx,
Keysight modules, future DACs) implement the Protocol and slot in.

### `benchctrl.channels.StandardChannel`

Minimal channel inventory framework subsystems depend on:
`MAIN_CURRENT` (`"mc"`), `MAIN_VOLTAGE` (`"mv"`), `MAIN_POWER` (`"mp"`).
Driver-specific enums (`OtiiArcChannel`) extend this with the device's
full channel inventory. Where a driver implements the standard subset,
its members carry the same two-letter `code` so a string code can be
used interchangeably across drivers.

### Framework primitives

`benchctrl.Recording` — captured measurement data per session, with
statistics, export, in-memory slicing, file load. Driver-agnostic at
the surface but presently delegates channel coercion to the Arc enum.

`benchctrl.samples` — pure functions for statistics, CSV/JSON/Parquet
write, sample buffer mechanics.

`benchctrl.exceptions` — generic `BenchError` hierarchy
(`BenchConnectionError`, `BenchValueError`, `BenchTimeoutError`,
`BenchCommandError`, `BenchProtocolError`, `BenchNotImplementedError`).

## Layer 3 — vendor-agnostic subsystems

### `benchctrl.battery`

Four phased modules for battery characterisation and emulation:

```
profile.py          BatteryProfile dataclass + Otii JSON round-trip
        │
        ├─→ calculator.py         predict runtime given profile + load
        │
        ├─→ profiler.py           drive a discharge sweep against any
        │                         SourceMeasurementUnit; emit a fresh
        │                         profile
        │
        └─→ emulator.py           run a 100 Hz control loop on any
                                  SourceMeasurementUnit:
                                  V = OCV(SoC) − I·ESR(SoC)
```

Both `Emulator` and `Profiler` type-hint
`SourceMeasurementUnit` (Protocol), reference `StandardChannel.MAIN_*`
for measurement, and don't import any concrete driver. The Arc driver
is what binds at runtime today.

Safety: explicit clamp to `safety_max_voltage_V` at emulator startup
(the Arc Pro's high range tops out at ~4.2 V under load — see
KNOWN_LIMITATIONS § H-1) and a watchdog that stops on
`safety_max_used_mAh` or `soc_floor`.

### `scenarios/`

Top-level directory, not under `src/benchctrl/`. CLI that drives the
emulator against a programmable load and saves the captured response
as a self-describing artifact bundle.

```
scenarios/
├── run.py                      CLI + scenario runners
├── README.md                   harness docs + headline results
└── saved/
    └── <profile>_<scenario>_<load>_<utc>.{json,csv,png}
        plus a copy of the input battery profile JSON
```

Internal `_LoadAdapter` abstraction lets one scenario runner target
either QR10x or DL3031A. Three scenario kinds:

- `static` — load sweep at QR's relay timing, V/I snapshot per step
- `dynamic` — host-driven IoT pattern at 20 Hz polling
- `dynamic-list` — DL3031A's firmware LIST mode + Arc Pro's native
  ~4 kHz `record()` streaming

### `benchctrl.mcp` — orchestrator

Each driver owns its MCP surface and exposes a
`register_mcp_tools(mcp)` function. `benchctrl/mcp.py` instantiates the
shared `FastMCP` server, calls each driver's `register_mcp_tools`, and
defines the cross-driver tools (battery analytics, recording I/O on
saved files, emulator).

Connection singletons (`_smu`, `_qr10x`, `_dl3031a`) live in each
driver's `mcp_tools` module — tests inject fakes by mutating that
module.

Tool inventory:

| Subsystem | Tools |
|---|---|
| Otii Arc SMU | 23 |
| QR10x | 11 |
| Rigol DL3031A | 45 |
| Rigol DP2031 | 134 |
| Siglent SDM4065A | 54 |
| CyberPower PDU41002 | 15 |
| Silicon Labs CP2112 | 10 |
| Ontrak ADU218 | 19 |
| Cross-driver (recording I/O, battery, emulator) | 13 |
| **Total** | **324** |

The MCP layer is intentionally thin: each tool wraps one SDK method,
coerces JSON-friendly argument types where needed, returns a dict.
**Single-client serialization** is assumed — running two concurrent
MCP clients against the same server is unsupported (documented in
the module docstring).

The SDK ↔ MCP parity principle is load-bearing: every public SDK
method has a matching MCP tool.

### `benchctrl.analysis/` and `benchctrl.dashboards/`

Placeholder packages reserved for v1.x. Analytics features that span
drivers (anomaly detection, multi-recording comparison, custom
statistics) go in `analysis/`. Live graphical UIs (matplotlib live
plots, web dashboards) go in `dashboards/`. Both ship empty at v1.0
with a README explaining their intended role.

## Cross-cutting concerns

### Output formats

`benchctrl.samples` knows how to export `Recording` data in:

- Native `.opensmu` binary (lossless, the canonical format —
  filename suffix preserved for backward read of v0.x captures)
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

Only two pieces of the system run background threads:

- `Emulator._loop` — daemon thread at 100 Hz reading current and
  writing voltage to the SMU
- `OtiiArc._reader_thread` (during `start_recording`) — daemon thread
  consuming inbound bytes and demuxing into channel buffers

These two cannot run concurrently against the same Arc Pro — they
deadlock at the transport layer (`KNOWN_LIMITATIONS` § A-1). The
hires scenarios runner sidesteps the deadlock by stopping the
emulator before opening the recording.

### Error model

Generic + driver-specific hierarchies:

```
BenchError                                 (in benchctrl.exceptions)
├── BenchConnectionError                   transport / open failure
├── BenchValueError                        client-side validation
├── BenchTimeoutError                      no expected response
├── BenchCommandError                      device rejected with -101 etc.
├── BenchProtocolError                     malformed frame / unparseable
└── BenchNotImplementedError               vendor-only methods we don't expose

QR10xError                                 (in benchctrl.drivers.eastwood_qr10x)
├── QR10xConnectionError
├── QR10xProtocolError
├── QR10xTimeoutError
└── QR10xValueError

RigolDLError                               (in benchctrl.drivers.rigol_dl3031a)
├── RigolDLConnectionError
├── RigolDLCommandError                    device -101 from :SYSTem:ERRor?
├── RigolDLTimeoutError                    VI_ERROR_TMO
└── RigolDLValueError
```

The Arc driver raises `BenchError` subclasses. Each non-SMU driver has
its own hierarchy so user code can catch instrument-specific errors
without coupling to the SMU's hierarchy. The SCPI error queue is
drained explicitly via `raise_if_error()` on DL3031A; the Arc's async
error frames are detected in-band by the transport reader.

### Testing pattern

`tests/` has two kinds of tests:

- **Hardware-free** (default): **2450 tests**, running in ~22 minutes
  with no device attached. Most drive `benchctrl.sim` simulators rather
  than mocks, so the transport, binary framing, session handshake and
  reader threads are all genuinely exercised — see
  [`docs/simulation.md`](docs/simulation.md).
- **Hardware-marked** (`@pytest.mark.hardware`): require real
  instruments. Skip gracefully if hardware is absent. 213 tests across
  Arc + DL3031A + DP2031 + QR10x + SDM4065A + PDU41002 + ADU218 +
  CP2112, each set skipped when its instrument isn't connected. Two of
  them use a *second* instrument as a witness, and for the same reason:
  the device's own read-back is the device talking about itself. The
  ADU218 switches a relay and the SDM4065A reads the contact, because
  the ADU218's writes are unacknowledged (12 tests, 10 passed / 1
  skipped against the real device with a live 10 Hz stimulus). The
  CP2112 needs it more starkly — an undriven pin reads back as a latched
  1 regardless of what is attached, so the DMM is the only thing that
  can tell an asserted line from an idle one (6 tests). The SDM4065A sets: the driver suite is 13 passed /
  1 skipped, and the QR10x cross-validation 4 passed / 3 skipped — the
  cross-validation skips are the 4-wire comparisons, which need sense
  leads that are not currently attached. See `TEST_PLAN.md`.

Mock SMUs in battery tests partially implement the
`SourceMeasurementUnit` Protocol — the methods the subsystem under
test actually calls. A renamed method on the Protocol therefore breaks
the relevant mock test.

## What this isn't

A few intentional non-goals worth knowing:

- **Not a vendor protocol bridge.** We don't speak Otii's TCP
  protocol or implement its server. Use the official client +
  server if you need that.
- **Not async.** Hardware is sequential. The streaming iterator
  covers "what's the value right now" without async machinery. This holds
  for the network layer too — the agent is threads and blocking sockets,
  because the board can install nothing beyond the standard library.
- **Not a GUI** (yet). `dashboards/` is reserved for v1.x. Plot
  output today is matplotlib PNG.
- **Not multi-device coordinated.** One Arc per `OtiiArc` instance.
  Coordinating multiple Arcs is v1.x work.

## Layer 4 — remote mode

`benchctrl.session` resolves each device key to `local`, `remote`, or `sim`
independently, so one MCP server can drive an Arc on a remote bench and a
Rigol plugged into the laptop it runs on. With nothing configured every key
is `local` and behaviour is unchanged.

```
   host laptop                                    bench (e.g. Uno Q)
   ┌────────────────────────┐                     ┌──────────────────────────┐
   │ benchctrl.mcp          │                     │ benchctrl-agent          │
   │   324 tools, unchanged │                     │   registry / dispatch    │
   │        │               │                     │   DeviceWorker per device│
   │        ▼               │                     │   SafetyGovernor         │
   │ session.resolve()      │   length-prefixed   │   RunManager             │
   │        │               │   frames over TCP   │        │                 │
   │        ▼               │◄───────────────────►│        ▼                 │
   │ net.proxy.RemoteSMU    │   HMAC handshake    │ real drivers ── USB ── DUT│
   └────────────────────────┘                     └──────────────────────────┘
```

- `benchctrl.net` — framing, codec, error mapping, auth, beacon, client,
  proxies. Shared by both ends; stdlib only.
- `benchctrl.agent` — the board-side server, plus `runs/` (unattended
  experiments) and `llm/` (advisory commentary).
- `benchctrl.sim` — device simulators behind ptys, so all of the above is
  developable and testable with no instruments attached.

The network boundary sits *above* the driver, not below it. A transport-level
proxy would put wifi latency inside the Arc's timed wake handshake and its
4 kHz sample demux, and would only remote one of five drivers.

Closed-loop subsystems (`Emulator`, `Profiler`) run on the bench, not across
the link: 100 Hz × 2 round trips per tick is 0.6–2.0 s of network per second
of wall clock.

## See also

- [`docs/design.md`](docs/design.md) — Arc driver internals in depth
- [`docs/getting_started.md`](docs/getting_started.md) — tutorial
- [`docs/api_reference.md`](docs/api_reference.md) — exhaustive API
- [`docs/otii_arc_protocol.md`](docs/otii_arc_protocol.md) — USB wire protocol
- [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) — caps, quirks,
  workarounds
- [`CONTRIBUTING.md`](CONTRIBUTING.md) — dev setup, conventions

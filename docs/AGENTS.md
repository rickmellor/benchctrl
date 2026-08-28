# benchctrl — for AI assistants

If you're an AI agent reading this, here's the briefing you need to
be useful to a user working on benchctrl.

Companion docs to read alongside this:

- [`README.md`](../README.md) — the project's front door
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — one-page tour of the subsystems
- [`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) — hardware caps, firmware quirks, harness workarounds. **Read this before debugging a new failure.**
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — conventions you're expected to follow
- [`skills/benchctrl/SKILL.md`](../skills/benchctrl/SKILL.md) — Claude Code skill briefing (more usage-focused than this)

## What this library is

**One sentence**: benchctrl drives a bench of instruments — a Qoitech
Otii Arc / Arc Pro SMU, programmable loads, a programmable supply —
directly over USB from Python, with battery emulation, an MCP server,
a reproducible scenarios harness, and the option to put the
instruments on another machine or replace them with simulators.

**Why it exists**: a single pure-Python entry point for an entire
bench — talk to every instrument through one library, drive
reproducible experiments from scripts, and expose the whole surface
to LLM agents through MCP. The Arc wire protocol is documented in
[`otii_arc_protocol.md`](otii_arc_protocol.md).

### Subsystems

- **`benchctrl.drivers`** — instrument drivers, all peers: Otii Arc
  (SMU), Eastwood QR10x (programmable resistor), Rigol DL3031A
  (electronic load), Rigol DP2031 (triple-output PSU), Siglent
  SDM4065A (6½-digit DMM — the only measurement-only one)
- **`benchctrl.interfaces`** — the `SourceMeasurementUnit` Protocol
  that drivers conform to; vendor-agnostic subsystems depend on this,
  never on a concrete driver
- **`benchctrl.battery`** — battery characterisation + emulation:
  profile I/O, life calculator, hardware profiler, 100 Hz host-side
  emulator
- **`benchctrl.mcp`** — MCP server, **324 tools**, orchestrator that
  calls each driver's `register_mcp_tools(mcp)`
- **`benchctrl.session`** — the local/remote/sim seam. `resolve()`
  decides *per device key* what a driver singleton actually gets
- **`benchctrl.config`** — layered JSON configuration behind that seam
- **`benchctrl.discovery`** — one signature table answering "what is
  on this bench", with a confidence level
- **`benchctrl.sim`** — wire-protocol simulators over ptys, so
  production drivers run with no hardware attached
- **`benchctrl.net`** — the remote wire protocol: typed frames, HMAC
  challenge-response auth, allowlisted value codec
- **`benchctrl.agent`** — the bench-side server, plus `agent.runs`
  (unattended declarative experiments) and `agent.llm` (advisory
  supervisor)
- **`scenarios/`** — top-level harness for reproducible scenarios
- **`applications/`** — standalone apps built on the SDK
  (`sensor_profiler`)

## Where things live

```
benchctrl/
├── README.md, ARCHITECTURE.md          entry points
├── CHANGELOG.md, KNOWN_LIMITATIONS.md  changelog + caps/quirks
├── CONTRIBUTING.md, PROGRESS.md        dev guide + live status
├── AGENTS.md                           sub-agent pattern for a new driver
├── ROADMAP.md                          deferred features
├── TEST_PLAN.md, VALIDATION_REPORT.md  test strategy + results
├── pyproject.toml                      extras list, entry points
├── docs/
│   ├── getting_started.md              tutorial for new users
│   ├── api_reference.md                every public name
│   ├── design.md                       Arc-layer architecture decisions
│   ├── otii_arc_protocol.md            Arc USB wire protocol reference
│   ├── battery.md                      battery subsystem walkthrough
│   ├── drivers.md                      QR10x / DL3031A / DP2031 / SDM4065A + quirks
│   ├── mcp.md                          MCP server setup + tools
│   ├── remote.md                       remote mode, security, deployment
│   ├── runs.md                         unattended runs, spec format
│   ├── simulation.md                   hardware-free simulators
│   ├── output_formats.md               .opensmu / Parquet / CSV / pandas / numpy
│   ├── official_api_inventory.md       what we replicate from otii_tcp_client
│   └── AGENTS.md                       this file
├── src/benchctrl/
│   ├── __init__.py                     public re-exports
│   ├── exceptions.py                   BenchError + subclasses
│   ├── interfaces.py                   SourceMeasurementUnit Protocol
│   ├── channels.py                     StandardChannel (driver-agnostic)
│   ├── samples.py                      parsing, ChannelBuffer, exports
│   ├── recording.py                    Recording + .opensmu file/stream codec
│   ├── session.py                      resolve() — local | remote | sim
│   ├── config.py                       layered config, DEVICE_KEYS
│   ├── discovery.py                    bench-wide device identification
│   ├── cli.py                          benchctrl CLI entry
│   ├── mcp.py                          MCP orchestrator (13 cross-driver tools)
│   ├── drivers/
│   │   ├── otii_arc/                   protocol.py, transport.py, device.py,
│   │   │                               channels.py, mcp_tools.py  (23 tools)
│   │   ├── eastwood_qr10x/             driver.py, mcp_tools.py    (11 tools)
│   │   ├── rigol_dl3031a/              driver.py, mcp_tools.py    (45 tools)
│   │   ├── rigol_dp2031/               driver.py, mcp_tools.py    (134 tools)
│   │   └── siglent_sdm4065a/           driver.py, mcp_tools.py    (54 tools)
│   ├── battery/
│   │   ├── profile.py                  profile JSON I/O
│   │   ├── calculator.py               life calculator
│   │   ├── profiler.py                 fresh-profile generator
│   │   └── emulator.py                 100 Hz emulator loop
│   ├── dashboards/                     read-only status display
│   │   ├── feed.py                     observer session + event subscription
│   │   ├── state.py                    BenchStatus snapshot (no renderer)
│   │   └── fui/                        server.py, view.py, static/
│   ├── sim/
│   │   ├── base.py, loopback.py        SimDevice + pty pair
│   │   ├── otii_arc.py, qr10x.py       per-instrument simulators
│   │   ├── scpi.py                     both Rigols, via pyvisa-py ASRL
│   │   ├── sdm4065a.py                 Siglent DMM, incl. its quirks
│   │   ├── waveforms.py                analytically-known signals
│   │   └── factories.py                production driver + simulator
│   ├── net/
│   │   ├── frames.py, codec.py         typed frames, allowlisted values
│   │   ├── auth.py                     HMAC-SHA256 challenge-response
│   │   ├── client.py, proxy.py         host side
│   │   ├── beacon.py                   UDP discovery
│   │   └── errors.py                   exception marshalling
│   └── agent/
│       ├── main.py, server.py          benchctrl-agent entry + server
│       ├── worker.py, dispatch.py      one owning thread per device
│       ├── registry.py, safety.py      device table + safety governor
│       ├── blobs.py, recordings.py     chunked transfer
│       ├── runs/                       spec.py, engine.py, rules.py, store.py
│       └── llm/                        supervisor.py, tools.py, client.py
├── tests/                              1333 hw-free + 173 hardware-marked
├── scenarios/                          harness + saved captures
├── applications/sensor_profiler/       DUT power profiling + Streamlit browser
├── examples/                           copy-paste-friendly scripts
├── bugs/                               vendor firmware bug reports
├── skills/benchctrl/SKILL.md           Claude Code skill briefing
└── .github/                            PR + issue templates, CI
```

## Default starting points by task

| Task | Where to look first |
|---|---|
| Add a new Arc SET command | `drivers/otii_arc/protocol.py` for the encoding, `drivers/otii_arc/device.py` for the public method |
| Decode a new Arc wire feature | `docs/otii_arc_protocol.md`, then `drivers/otii_arc/protocol.py` |
| Understand a behavior | `ARCHITECTURE.md` for the wide view, `docs/design.md` for Arc-layer decisions |
| Add an instrument driver | new package under `src/benchctrl/drivers/`, modeled on `rigol_dl3031a/`. Follow the sub-agent pattern in `../AGENTS.md` — the failure modes are research failures, not coding failures |
| Touch the battery emulator | `battery/emulator.py`. The explicit "no try/except" comments in `start()` are load-bearing |
| Add an MCP tool | the owning driver's `mcp_tools.py`, or `mcp.py` for cross-driver tools |
| Change local/remote/sim behaviour | `session.py` first — it is the only seam |
| Add a simulator behaviour | `sim/<instrument>.py`; SCPI devices share `sim/scpi.py`'s register model |
| Touch the wire protocol | `net/frames.py` + `net/codec.py`. The codec allowlist is a security boundary |
| Add a run-spec field | `agent/runs/spec.py` — remember it is content-hashed |
| Add a validation scenario | `scenarios/run.py`. Three kinds: static, dynamic, dynamic-list |
| Find out what's deferred | `ROADMAP.md` |
| Resume mid-task | `PROGRESS.md` |
| Find out why something doesn't work | `KNOWN_LIMITATIONS.md` |

## API shape you should produce

When generating code for benchctrl users:

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel
from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.drivers.rigol_dl3031a import RigolDL3031A
from benchctrl.drivers.rigol_dp2031 import RigolDP2031
from benchctrl.battery import BatteryProfile, Emulator, EmulatorConfig

with OtiiArc.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.enable_channels(OtiiArcChannel.MAIN_VOLTAGE, OtiiArcChannel.MAIN_CURRENT)
    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    rec.save_csv("out.csv")
```

Things that look right:

- `OtiiArc.open()` / `QR10x.open(port)` / `RigolDL3031A.open(resource=None)` /
  `RigolDP2031.open(resource=None)` as context managers
- `set_*(...)` methods for writes; properties for cached state reads
- `OtiiArcChannel` for Arc channels, `StandardChannel` for
  driver-agnostic code (strings accepted at both boundaries)
- `with smu.record() as rec:` for time-bounded captures
- `Emulator(smu, EmulatorConfig(...))` for battery emulation; `.start()` / `.stop()` / `.state()`
- For DL3031A LIST mode: `dl.program_list(steps=[(level, width), ...], mode="CC", count=N, trigger_source="BUS")` + `dl.set_input(True)` + `dl.trigger_now()`
- `SimulatedOtiiArc()` + `OtiiArc.open(sim.port)` for hardware-free work

Things that look wrong (would surprise a maintainer):

- `smu.voltage = 3.3` — properties are read-only; use `smu.set_voltage(3.3)`
- `try: smu.set_voltage(x); except Exception: pass` — silent fallbacks are bugs; see CONTRIBUTING.md § 4
- Importing a driver from the wrong package — each lives in its own
  subpackage under `benchctrl.drivers`; there is no top-level `SMU`
  and no `benchctrl.bench` (both were removed in 1.0)
- Calls to deferred methods (calibration, firmware upgrade,
  `set_supply_battery_emulator`) — these raise `BenchNotImplementedError`. The battery emulator lives in `benchctrl.battery`, not on the driver
- Manual wire-byte construction — go through `protocol.py` helpers
- `subprocess` to drive an external Otii server — there is no vendor
  server in the path. (`benchctrl-agent` is *our* server, and only for
  remote mode)
- Reaching around `session.resolve()` to decide local-vs-remote
- 4-step DL3031A LIST programs — firmware bug; driver rejects them. Use 3 or 5 steps with appropriate `count`
- `dl.set_function_mode("FIXed")` to escape a stuck LIST/WAV/BATT mode — doesn't work; only power-cycle restores FIX

## Important constants and quirks

### Arc / Arc Pro

- **USB IDs**: VID `0x0FCE`, PID `0xD1E6`
- **Wire magic**: `A3 2C B5 7F`
- **Sequence start**: `0x1000` per session
- **Baseline stream rate**: ~6 Hz across all channels. Native rates (~4 kHz on `MAIN_CURRENT`, ~1 kHz on `MAIN_VOLTAGE`) require `start_recording(...)` to enable per-channel high-rate streaming
- **Voltage range cap**: low range tops out at ~3.5 V; high range tops out at ~4.2 V under load (LiPo emulation matters here — KNOWN_LIMITATIONS § H-1)
- **Sample record header**: `02 00 08 00` followed by `chan:u32 value:f32`
- **Error frame header**: `0e 03 99 ff 04 10 00 00` followed by `err:i32 last_good:u32`. Errors arrive asynchronously and are queued for the next SET to surface

### Rigol DL3031A

- **USB IDs**: VID `0x1AB1`, PID `0x0E11`
- **VISA resource pattern**: `USB0::0x1AB1::0x0E11::<SN>::INSTR`
- **Measurement integration**: fixed 10 PLC (~200 ms); `:FETCh:` is fast but multiple reads share the same integration window
- **`:SOUR:LIST:STEP N`**: plays N total steps (not N+1 as manual hints). STEP=4 is a firmware bug (no steps fire) — driver rejects 4-step programs
- **`:SOUR:LIST:SLEW`**: per-step, not global
- **`:SOUR:LIST:END`**: `LAST | OFF` (not `NORMal | LAST` as some places in manual say)
- **`:FETCh:DISChargingTime?`**: returns `H:MM:SS` not a float
- **`:SOUR:CURR:TRAN:FREQuency`**: takes Hz (manual says kHz)
- **`:SOUR:FUNC:MODE FIXed`**: one-way; only power-cycle restores FIX

### Rigol DP2031

- **USB IDs**: VID `0x1AB1`, PID `0xA4A8` (DP2000 family)
- **Envelope**: CH1/CH2 0–32 V / 3 A; CH3 0–6 V / 5 A
- **`:OUTPut:PAIR`** SERies/PARallel survives `*RST`; the PARallel
  transition takes ≥ 1 s and reads back stale `OFF` meanwhile
- **`:OUTPut:OVP:CLEar`** clears the latch but does NOT re-enable the
  output; the `:SOURce<n>:VOLT:PROT:CLEar` form does
- **`:ANALyzer:COMMon:MEASure:TYPE`** write triggers
  `VI_ERROR_SYSTEM_ERROR` over USB-TMC — firmware defect, see `bugs/`
- **`:OCP:DELay?`** returns a string with a `"ms"` suffix
- **`:SOURce<n>:VOLT? MAX`** includes ~5% headroom over nominal
- Full list in `KNOWN_LIMITATIONS.md § F-3.5`

### Eastwood QR10x

- **Wire**: USB-Serial CH340, 115200 8N1, AT command set
- **No recorded VID/PID** — identified by AT probe in `discovery.py`,
  because the CH340 bridge VID/PID is shared by thousands of products
- **End-of-response**: no delimiter; driver uses ~60 ms quiet window heuristic
- **Safety**: device-enforced `RLIMIT` clamps any setpoint at or above the configured value. Set it for the source voltage you're working with: `V**2 / P_max` (1 W rating gives ~12 Ω at 3.2 V, ~25 Ω at 5 V)

### CyberPower PDU41002

- **Not SNMP.** One line-oriented CLI, reached over serial *or* SSH, and
  the two are byte-identical in output — measured, which is what licenses
  one engine over two links
- **The device permits exactly one CLI session across all transports,**
  and the second one fails *after* the password is accepted. It reads as
  an auth error and is not one
- **`close()` must send `exit`** — closing the port leaves the device
  session held, so the *other* transport then fails that same way
- **SSH needs a forced KEX** (`diffie-hellman-group14-sha256`; group
  *exchange* is broken on FW 1.3.4) and offers only
  `keyboard-interactive`, so pubkey auth and `BatchMode` can never work
- **Read to the prompt, never to a blank line** — the unknown-verb error
  carries a ~30-line verb dump and the blank-line count varies by error
  shape
- **`menumode` is a one-way trap**: returning to the CLI needs a full
  logout. No method emits it; the emitted-command assertion rejects it
- **Password comes from `BENCHCTRL_PDU_PASSWORD`** in the agent's
  environment, never from config — `DeviceConfig.to_dict()` emits `open`
  verbatim and the RPC wire is authenticated but not encrypted

### Silicon Labs CP2112

- **USB IDs**: VID `0x10C4`, PID `0xEA90`. Transport is **`hidraw`**,
  not usbfs — measured both ways: `usbhid` *does* claim this chip, unlike
  the ADU218 below
- **GPIO commands are HID *feature reports***, so the node needs `O_RDWR`
  even to read a pin — a feature *get* is a `GET_REPORT` over the control
  pipe. Read-only would be a `PermissionError` that reads as a udev
  problem
- **Compute the ioctl request numbers with `_IOC`.** `HIDIOCSFEATURE`
  embeds the payload length, so a constant lifted from a header is wrong
  at another length or another word size
- **A level identifies nothing.** An undriven pin is high-impedance:
  `read_levels()` returns `0xFF` regardless of wiring, while a 10 MΩ
  meter reads ~0 V on the same net. Both are correct. A pin is identified
  only by a level you can make *move* — so never diagnose CP2112 wiring
  from `cp2112_line_states`
- **Open-drain is the only drive mode, deliberately.** Push-pull is
  unreachable through the public API and enforced three ways; a 3.3 V
  push-pull pin on a 1.8 V reset net back-feeds the target's rail. There
  is no `push_pull` parameter to find
- **All eight pins revert to inputs on reset or re-plug**, so every write
  is a read-modify-write against the live config register, never a cached
  copy. Configuration does not survive a re-enumeration
- **Pulses below 5 ms are refused.** Every transition is its own USB
  transfer, so the datasheet rules the pins out for real-time signalling;
  refusing beats silently stretching one and reporting success
- **Never write a blanket `SUBSYSTEM=="hidraw"` udev rule** — on the
  bench board that also matches the USB keyboard. The shipped rule is
  scoped to `10c4:ea90`. Note `10c4:ea60` is the CP210x *UART* bridge, a
  different chip
- **`hidrawN` numbering is not stable**; use the udev symlink or let the
  driver find the device by VID/PID

### Ontrak ADU218

- **USB IDs**: VID `0x0A07`, PID `0x00DA`. EP `0x81` IN / `0x01` OUT,
  both **interrupt**, `wMaxPacketSize` 8, `bInterval` 10
- **Zero dependencies.** USB HID via raw `USBDEVFS` ioctls —
  `fcntl`/`ctypes`/`os` only. `usbhid` ignores Ontrak on purpose
  (`hid_ignore_list` in `hid-quirks.c`), so `CLAIMINTERFACE` succeeds
  with no driver to detach
- **Compute the ioctl numbers, never hardcode them.** `USBDEVFS_BULK`
  embeds `sizeof(struct usbdevfs_bulktransfer)`: 24 on 64-bit, 16 on
  32-bit. A literal works on the laptop and fails on the board
- **`USBDEVFS_BULK` on an interrupt endpoint is contractual** —
  `devio.c` rewrites the pipe to `PIPE_INTERRUPT` and calls
  `usb_fill_int_urb()` with `bInterval`
- **The device never reports an error.** Absent command, valid command
  with a bad argument, and write-only command are byte-identical:
  silence. Hence an explicit per-command `responsive` flag and reply
  width, never inferred from the mnemonic — `RKn` starts with `R` and is
  write-only, and is the most-called command
- **Three index ranges**: relays `0..7`, input *lines* `0..3` (ports A
  and B are four bits each), counters `0..7`
- **`RPy` replies MSB-first** — the leftmost character is line 3. Reads
  plausibly wrong if you assume otherwise
- **Any command refeeds the watchdog**, so a polling loop silently
  prevents it ever tripping. `WD` reads 0 for both "timed out" and
  "never enabled"
- **Relay state survives a host reset** — USB suspend holds the outputs

### Remote mode

- **Default port**: 9737. Device keys: `otii_arc`, `eastwood_qr10x`,
  `rigol_dl3031a`, `rigol_dp2031`, `siglent_sdm4065a`,
  `cyberpower_pdu41002`, `silabs_cp2112`, `ontrak_adu218`
- **Auth is HMAC-SHA256 challenge-response** — the token never
  crosses the wire, but traffic is **not encrypted**. SSH tunnel on an
  untrusted network
- **`deadman_s` must exceed `heartbeat_s`** or a healthy link trips
  the safety governor; config rejects it
- A remote device naming no reachable endpoint is a **loud error**,
  never a silent fall back to local

## Common tasks

### Run the tests

```bash
pytest -m "not hardware" -q              # 1333 hardware-free, ~10 min
pytest -m hardware -q                     # 173 hardware-marked
pytest -q                                  # all (needs the bench on USB)
```

### Add a SET command (Arc)

1. Add `CMD_SET_*` constant to `drivers/otii_arc/protocol.py`
2. If a new value-units helper is needed, add it
3. Add a method on `OtiiArc` in `drivers/otii_arc/device.py` that:
   - validates the input range (client side)
   - calls `self._send_set(CMD_..., encoded_value)`
   - updates `self._state.<field>` with the new value
4. Add a hardware-free test to `tests/test_protocol_commands.py`
5. Add a hardware-required test to `tests/test_smu_setters.py`
6. **Add a matching tool in `drivers/otii_arc/mcp_tools.py`** and
   append it to that module's `_TOOLS` tuple — parity is checked in review
7. Teach `sim/otii_arc.py` to answer it, so the hardware-free path stays honest

### Add an MCP tool

1. Find the corresponding SDK method (if it doesn't exist, add it first)
2. In the owning driver's `mcp_tools.py`, add the function and list it
   in `_TOOLS`. Cross-driver tools go in `mcp.py`
3. Coerce JSON-friendly types; return a dict (never a custom dataclass)
4. For setters that change state, read back via the corresponding `get_*` and include in the return dict for observability
5. Do **not** reach for the driver directly — go through the
   module's `_get_<device>()` singleton so `session.resolve()` stays
   in the path and the tool works remote and simulated too

### Add an instrument driver

1. Create `src/benchctrl/drivers/<vendor_model>/` with `driver.py`,
   `__init__.py`, `mcp_tools.py`
2. Define an exception hierarchy: `<Vendor>Error` → `<Vendor>ConnectionError` / `<Vendor>CommandError` / `<Vendor>TimeoutError` / `<Vendor>ValueError`.
   Register every class in `net/errors.py` or it degrades to its nearest known
   ancestor over the wire. If a constructor takes something other than a message
   first — `SDM4065AOverloadError(function, range_)`, `BenchCommandError(error_code, …)` —
   that is fine and handled, **but a first parameter that merely *accepts* a
   string is the trap**: `cls(message)` then succeeds, files the whole message
   under that field, and re-composes a new one. `_instantiate()` guards it
   generally now (each strategy must store the message unchanged), and
   `test_every_registered_exception_round_trips` asserts `args` exactly rather
   than by containment, because containment is what a re-composed message passes.
3. Implement an `open(...)` class method that returns the instance with the transport open
4. Implement `close()` and `__enter__` / `__exit__` for context-manager use; `__exit__` should also disable any output for safety
5. Use the same property-read-method-write convention as the other drivers
6. Add `register_mcp_tools(mcp)` + a `_TOOLS` tuple in `mcp_tools.py`,
   and a `_get_<device>()` singleton that populates via `session.resolve()`
7. Add a key to `config.DEVICE_KEYS` and a signature to `discovery.py`
8. Add a simulator under `sim/` (SCPI instruments can extend `ScpiDevice`)
   and a factory in `sim/factories.py`
9. Document in `docs/drivers.md`

### Document a firmware bug

1. Add an entry to `KNOWN_LIMITATIONS.md` under the appropriate section (`Hardware`, `Driver-firmware interactions`, `Harness`, or `Network`)
2. If the driver should reject the known-bad input, implement the rejection at the SDK boundary with a clear error message pointing to the workaround. See `list_set_step_count(4)` in the DL3031A driver for the pattern
3. Add a test that exercises the rejection
4. CHANGELOG entry under the current version's "Discovered" subsection
5. If it's worth filing with the vendor, add a report under `bugs/`

### Decode something new from the Arc wire

1. Capture USB traffic of the vendor stack performing the operation
2. Look for new `0x66` cmd codes, or new payload types
3. Add the decoded format to `docs/otii_arc_protocol.md`
4. Add `parse_*` / `encode_*` helpers to `drivers/otii_arc/protocol.py`
5. Expose via an `OtiiArc` method

### Skip-and-mark a blocker

1. Add a clear `# TODO(deferred):` comment in the affected file
2. Add an entry in `ROADMAP.md` with the "why" and "scope when picked up"
3. If the user-facing surface still needs to be present, raise `BenchNotImplementedError("...short reason... — see ROADMAP.md")`
4. Add a test confirming the stub raises

## Operating mode for AI agents

- **Be honest about what's not in this version**. ROADMAP.md and
  KNOWN_LIMITATIONS.md are comprehensive and current. Don't claim a
  stub works; don't claim a documented limitation has been resolved.
- **Match the existing API shape**. New methods follow the
  property-reads-method-writes convention. New drivers follow the
  established exception hierarchy + context manager pattern.
- **Cite line numbers** when discussing internal code:
  `drivers/otii_arc/device.py:123` beats "in the device class".
- **Don't replace the wire protocol with a guess**. Every constant
  in `protocol.py` was observed in real captures. Adding a new
  constant needs evidence.
- **Default to propagating errors**. Silent try/except is a bug —
  see CONTRIBUTING.md § 4 for the one acceptable pattern (teardown
  cleanup that logs at warning level).
- **Maintain SDK ↔ MCP parity**. Adding a public SDK method without
  the matching MCP tool will fail review.
- **Respect the safety boundaries.** In `agent/runs`, deterministic
  rules are the safety system and the LLM is commentary — do not add
  a model-reachable path that energises anything or widens a declared
  envelope. In `net/codec.py`, the value allowlist is closed on
  purpose; resolving a dotted class path from the wire is RCE.
- **Prefer simulators to mocks.** If you need a driver in a test,
  reach for `benchctrl.sim` so the real transport and framing stay in
  the path.
- **Update PROGRESS.md** if you make non-trivial changes during a
  long-running session — that's how future-you (or another agent)
  picks up the thread.

## What this library is not

- Not affiliated with Qoitech AB, Rigol Technologies, or Eastwood Tech
- Not a desktop app for end-users — it's a programmatic interface for
  automation and scripting from Python
- Not a sniffer / man-in-the-middle (talks to each device directly)
- Not a firmware update tool (intentionally deferred for safety)
- Not async — hardware is sequential; the streaming iterator covers
  "what's the value right now". `benchctrl.net` is threaded, not
  asyncio
- Not encrypted on the wire. Remote mode authenticates; it does not
  provide confidentiality (see `docs/remote.md`)
- Not multi-Arc coordinated — one Arc per `OtiiArc` instance is
  the supported topology. Multiple *different* instruments on one
  bench is fully supported, and they can be split across machines

# benchctrl — for AI assistants

If you're an AI agent reading this, here's the briefing you need to
be useful to a user working on benchctrl.

Companion docs to read alongside this:

- [`README.md`](../README.md) — the project's front door
- [`ARCHITECTURE.md`](../ARCHITECTURE.md) — one-page tour of the five subsystems
- [`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) — hardware caps, firmware quirks, harness workarounds. **Read this before debugging a new failure.**
- [`CONTRIBUTING.md`](../CONTRIBUTING.md) — conventions you're expected to follow
- [`skills/benchctrl/SKILL.md`](../skills/benchctrl/SKILL.md) — Claude Code skill briefing (more usage-focused than this)

## What this library is

**One sentence**: benchctrl drives a Qoitech Otii Arc / Arc Pro
source-measurement unit directly over its USB CDC-ACM port from
Python, with built-in battery emulation, companion-instrument
drivers, an MCP server, and a reproducible validation harness.

**Why it exists**: Qoitech's automation Python client requires a
paid-licensed local server. benchctrl re-implements the same
capability by talking to the hardware directly. The wire protocol is
fully reverse-engineered (see [`protocol.md`](protocol.md)) and the
device imposes no license check at the wire level.

The project has grown beyond just the SMU driver:

- **`benchctrl.battery`** — Battery Toolbox replacement: profile I/O,
  life calculator, profiler, 100 Hz host-side emulator
- **`benchctrl.bench`** — companion instrument drivers (QR10x
  programmable resistor, Rigol DL3031A electronic load)
- **`benchctrl.mcp`** — MCP server, 93 tools, exposes every SDK method
- **`validation/`** — top-level harness for reproducible scenarios

## Where things live

```
benchctrl/
├── README.md, ARCHITECTURE.md          entry points
├── CHANGELOG.md, KNOWN_LIMITATIONS.md  changelog + caps/quirks
├── CONTRIBUTING.md, PROGRESS.md        dev guide + live status
├── ROADMAP.md                           deferred features
├── pyproject.toml                       extras list
├── docs/
│   ├── getting_started.md               tutorial for new users
│   ├── api_reference.md                 every public name (all subpackages)
│   ├── design.md                        SMU-layer architecture decisions
│   ├── protocol.md                      USB wire protocol reference
│   ├── battery.md                       battery subsystem walkthrough
│   ├── bench.md                         bench drivers + firmware modes
│   ├── mcp.md                           MCP server setup + tools
│   ├── output_formats.md                .opensmu / Parquet / CSV / pandas / numpy
│   ├── official_api_inventory.md        what we replicate from otii_tcp_client
│   └── AGENTS.md                        this file
├── src/benchctrl/
│   ├── __init__.py                      public re-exports
│   ├── exceptions.py                    BenchError + subclasses
│   ├── channels.py                      Channel enum + ChannelInfo
│   ├── protocol.py                      pure framing + command encoding
│   ├── transport.py                     pyserial wrapper, discovery
│   ├── samples.py                       parsing, ChannelBuffer, exports
│   ├── recording.py                     Recording class + .opensmu format
│   ├── device.py                        SMU class (public)
│   ├── cli.py                           benchctrl CLI entry
│   ├── battery/
│   │   ├── profile.py                   profile JSON I/O
│   │   ├── calculator.py                life calculator
│   │   ├── profiler.py                  fresh-profile generator
│   │   └── emulator.py                  100 Hz emulator loop
│   ├── bench/
│   │   ├── __init__.py                  lazy-imports DL3031A (PEP 562)
│   │   ├── qr10x.py                     Eastwood programmable resistor
│   │   └── rigol_dl3031a.py             Rigol electronic load
│   └── mcp.py                           MCP server (93 tools)
├── tests/                               282 hw-free + 90 hw-marked
├── validation/
│   ├── run_validation.py                scenario harness
│   ├── README.md                        harness docs + results
│   └── scenarios/                       saved captures (JSON / CSV / PNG)
├── skills/benchctrl/SKILL.md              Claude Code skill briefing
└── .github/                             PR + issue templates, CI
```

## Default starting points by task

| Task | Where to look first |
|---|---|
| Add a new SMU SET command | `protocol.py` for the encoding, `device.py` for the public method |
| Decode a new wire feature | `docs/protocol.md`, then `protocol.py` |
| Understand a behavior | `ARCHITECTURE.md` for the wide view, `docs/design.md` for SMU-layer decisions |
| Add bench instrument support | `src/benchctrl/bench/__init__.py` (lazy export pattern), then a new module modeled on `qr10x.py` or `rigol_dl3031a.py` |
| Touch the battery emulator | `src/benchctrl/battery/emulator.py`. Pay attention to the explicit "no try/except" comments in `start()` — those are load-bearing |
| Add an MCP tool | `src/benchctrl/mcp.py`. Every SDK public method should have a matching tool (SDK ↔ MCP parity — see CONTRIBUTING.md § 1) |
| Add a validation scenario | `validation/run_validation.py`. Three existing kinds: static, dynamic, dynamic-list — model new ones on whichever is closest |
| Find out what's deferred | `ROADMAP.md` |
| Resume mid-task | `PROGRESS.md` |
| Find out why something doesn't work | `KNOWN_LIMITATIONS.md` |

## API shape you should produce

When generating code for benchctrl users:

```python
from benchctrl import SMU, Channel
from benchctrl.battery import BatteryProfile, Emulator, EmulatorConfig
from benchctrl.bench import QR10x, RigolDL3031A

with SMU.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    rec.save_csv("out.csv")
```

Things that look right:

- `SMU.open()` / `QR10x.open(port)` / `RigolDL3031A.open(resource=None)` as context managers
- `set_*(...)` methods for writes; properties for cached state reads
- `Channel` enum for channels (strings accepted at boundaries)
- `with smu.record() as rec:` for time-bounded captures
- `Emulator(smu, EmulatorConfig(...))` for battery emulation; `.start()` / `.stop()` / `.state()`
- For DL3031A LIST mode: `dl.program_list(steps=[(level, width), ...], mode="CC", count=N, trigger_source="BUS")` + `dl.set_input(True)` + `dl.trigger_now()`

Things that look wrong (would surprise a maintainer):

- `smu.voltage = 3.3` — properties are read-only; use `smu.set_voltage(3.3)`
- `try: smu.set_voltage(x); except Exception: pass` — silent fallbacks are bugs; see CONTRIBUTING.md § 4
- Calls to deferred methods (calibration, firmware upgrade,
  `set_supply_battery_emulator`) — these raise `BenchNotImplementedError`. The battery emulator lives in `benchctrl.battery`, not on `SMU`
- Manual wire-byte construction — go through `protocol.py` helpers
- `subprocess` to drive an external Otii server — there is no server
- TCP / port 1905 usage — there is no TCP layer
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

### Eastwood QR10x

- **Wire**: USB-Serial CH340, 115200 8N1, AT command set
- **End-of-response**: no delimiter; driver uses ~60 ms quiet window heuristic
- **Safety**: device-enforced `RLIMIT` clamps any setpoint at or above the configured value. Set it for the source voltage you're working with: `V**2 / P_max` (1 W rating gives ~12 Ω at 3.2 V, ~25 Ω at 5 V)

## Common tasks

### Run the tests

```bash
pytest -m "not hardware" -q              # 282 hardware-free, ~3 min
pytest -m hardware -q                     # ~90 hardware-marked
pytest -q                                  # all (~5 min with hardware)
```

### Add a SET command (SMU)

1. Add `CMD_SET_*` constant to `protocol.py`
2. If a new value-units helper is needed, add it
3. Add a method on `SMU` in `device.py` that:
   - validates the input range (client side)
   - calls `self._send_set(CMD_..., encoded_value)`
   - updates `self._state.<field>` with the new value
4. Add a hardware-free test to `tests/test_protocol_commands.py`
5. Add a hardware-required test to `tests/test_smu_setters.py`
6. **Add a matching `@mcp.tool()` in `src/benchctrl/mcp.py`** — parity is checked in review

### Add an MCP tool

1. Find the corresponding SDK method (if it doesn't exist, you need to add it first)
2. In `src/benchctrl/mcp.py`, add a `@mcp.tool()` function that calls the SDK method
3. Coerce JSON-friendly types; return a dict (never a custom dataclass)
4. For setters that change state, read back via the corresponding `get_*` and include in the return dict for observability

### Add a bench instrument driver

1. Create `src/benchctrl/bench/<vendor_model>.py`
2. Define an exception hierarchy: `<Vendor>Error` → `<Vendor>ConnectionError` / `<Vendor>CommandError` / `<Vendor>TimeoutError` / `<Vendor>ValueError`
3. Implement an `open(...)` class method that returns the instance with the transport open
4. Implement `close()` and `__enter__` / `__exit__` for context-manager use; `__exit__` should also disable any output for safety
5. Use the same property-read-method-write convention as the SMU class
6. Add MCP tools in `src/benchctrl/mcp.py`
7. Lazy-export in `src/benchctrl/bench/__init__.py` if the driver pulls in heavy dependencies (PEP 562 pattern used for `RigolDL3031A`)
8. Document in `docs/bench.md`

### Document a firmware bug

When you find an instrument behavior that contradicts the manufacturer's docs:

1. Add an entry to `KNOWN_LIMITATIONS.md` under the appropriate section (`Hardware`, `Driver-firmware interactions`, or `Harness`)
2. If the driver should reject the known-bad input, implement the rejection at the SDK boundary with a clear error message pointing to the workaround. See `list_set_step_count(4)` in `rigol_dl3031a.py` for the pattern
3. Add a test that exercises the rejection
4. CHANGELOG entry under the current version's "Discovered" subsection

### Decode something new from the SMU wire

1. Capture USB traffic of the vendor stack performing the operation
2. Look for new `0x66` cmd codes, or new payload types
3. Add the decoded format to `docs/protocol.md`
4. Add `parse_*` / `encode_*` helpers to `protocol.py`
5. Expose via `SMU` method

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
  `device.py:123` is more useful than "in the device class".
- **Don't replace the wire protocol with a guess**. Every constant
  in `protocol.py` was observed in real captures. Adding a new
  constant needs evidence.
- **Default to propagating errors**. Silent try/except is a bug —
  see CONTRIBUTING.md § 4 for the one acceptable pattern (teardown
  cleanup that logs at warning level).
- **Maintain SDK ↔ MCP parity**. Adding a public SDK method without
  the matching MCP tool will fail review.
- **Update PROGRESS.md** if you make non-trivial changes during a
  long-running session — that's how future-you (or another agent)
  picks up the thread.

## What this library is not

- Not a competitor to the Qoitech Otii desktop app for end-users — it's a programmatic interface for automation
- Not affiliated with Qoitech AB or Rigol Technologies
- Not licensed under their automation toolboxes (we don't use their servers)
- Not a sniffer / man-in-the-middle (talks to the device directly)
- Not a firmware update tool (intentionally deferred for safety)
- Not async — hardware is sequential; the streaming iterator covers "what's the value right now"
- Not multi-device coordinated — one Arc per `SMU` instance is the supported topology

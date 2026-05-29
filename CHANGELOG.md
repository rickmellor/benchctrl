# Changelog

All notable changes to OpenSMU. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

## [0.5.0] — battery profile format (phase 1 of Battery Toolbox replacement)

First of four phases replacing Qoitech's licensed Battery Toolbox on
top of opensmu's existing wire vocabulary. This release nails the
**profile JSON format** — the data structure every subsequent battery
feature reads/writes.

### Added — `opensmu.battery.profile`

- **`BatteryProfile`** dataclass with nested `Battery`,
  `DischargeTable`, `DischargeProfile`, `DischargeStep`,
  `DischargeSample`, `ExitConditions`, `DeviceInfo` types — covers every
  field the Otii format uses.
- **`BatteryProfile.load(path)` / `.save(path)`** — JSON I/O. Output
  format is bit-identical to Otii's bundled profiles (`%LOCALAPPDATA%\\otii3\\app-*\\resources\\batteryprofiles`).
- **`profile.ocv_at(used_capacity_mAh, temperature=None)`** — linearly
  interpolate the open-circuit voltage at a given used capacity.
- **`profile.esr_at(used_capacity_mAh, temperature=None)`** — same for
  the equivalent series resistance.
- **`profile.select_table(temperature=None)`** — picks the nearest
  discharge table for multi-temperature profiles.
- **`profile.summary()`** — JSON-friendly summary (nominal V/C, cutoff,
  temperatures, per-table extents).

### Verified

- All 8 profiles bundled with Otii 3.7.2 (AA, AAA, CR123A, CR2, CR2032,
  LiPo at three temperatures) load, round-trip, and re-save as
  bit-identical JSON — tested in-suite via a hardware-free test that
  walks the Otii install directory.
- CR2032 interpolation matches the bundled profile's first sample to
  3 decimal places (V) and 3 decimal places (Ω).

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_profile_summary(path)`** — load + return summary
- **`battery_profile_lookup(path, used_capacity_mAh, temperature=None)`** —
  return interpolated OCV / ESR at a point

Both work on saved files; no SMU connection required.

### Documentation

- **`docs/battery.md`** — feature plan, phased status, JSON schema,
  code recipes (loading bundled profiles, building synthetic ones,
  merging multi-temperature data).
- Skill ([`skills/opensmu/SKILL.md`](skills/opensmu/SKILL.md)) gains a
  "Battery features" section pointing at the new subpackage.

### Tests

- 18 new tests in `tests/test_battery_profile.py`:
  - 13 synthetic-data tests (interpolation, clamping, JSON round-trip,
    multi-table selection, empty-profile edge cases, unit normalisation)
  - 5 real-profile tests (gated by Otii install presence): load every
    bundled profile, bit-identical round-trip, known-value
    interpolation checks, LiPo power-mode detection, multi-temperature
    file enumeration

Total tests: 212 → **230 passing**.

## [0.4.1] — MCP server synced with v0.4.0 output formats

Pure additive change to keep the MCP server in lock-step with the SDK
(see the new ["SDK ↔ MCP parity principle"](docs/mcp.md#sdk--mcp-parity-principle)
section). Three new tools, plus extensions to `record`.

### Added — MCP tools

- **`record(..., save_path="run.parquet")`** — the existing `record`
  tool now handles `.parquet` save paths via the v0.4.0 Parquet output.
  Requires `opensmu[parquet]`.
- **`record(..., plot_png="run.png")`** — new optional param; when
  given, also renders a matplotlib quick-look PNG in the same call.
  Requires `opensmu[plot]`.
- **`plot_recording(input_path, output_png, channels=None, title=None)`** —
  load a saved `.opensmu` file and render a PNG. No SMU connection
  required.
- **`recording_summary(input_path)`** — load a saved `.opensmu` and
  return its name, start/end times, offset, device metadata, and
  per-channel statistics. No SMU connection required.
- **`export_recording(input_path, output_path)`** — convert a saved
  `.opensmu` to another format (CSV / JSON / Parquet / opensmu)
  based on the output extension. No SMU connection required.

MCP tool count: 23 → **26**.

### Documentation

- New section in [`docs/mcp.md`](docs/mcp.md#sdk--mcp-parity-principle):
  the SDK ↔ MCP parity principle with a table mapping SDK features to
  their MCP equivalents (and explicit exceptions — `to_numpy()` /
  `to_pandas()` don't cross the MCP serialisation boundary; use
  `save_parquet` / `plot_recording` instead).

### Tests

- 6 new hardware-free tests covering `recording_summary`,
  `plot_recording`, and `export_recording` against synthetic recordings.
- 3 new hardware-required tests covering `record(save_path=".parquet")`,
  `record(plot_png=…)`, and combined save+plot in one call.

Total: 203 → **212 tests passing**.

## [0.4.0] — output formats: numpy, pandas, parquet, matplotlib

Recordings now offer first-class export to the scientific-Python stack,
all gated by **strictly optional** dependencies. The base install pulls
only pyserial; you only need the extras for the features you actually
use.

### Added

- **`Recording.to_numpy(channel)`** → 1D float32 `numpy.ndarray` of values.
  Install with `pip install 'opensmu[numpy]'`.
- **`Recording.timestamps_numpy(channel)`** → 1D float64 `numpy.ndarray`
  of synthesised timestamps (offset-adjusted).
- **`Recording.to_pandas(channel=None)`** — returns a `pandas.Series`
  if a channel is given, or a wide `pandas.DataFrame` (one column per
  channel, NaN-padded where rates differ) if not.
  Install with `pip install 'opensmu[pandas]'`.
- **`Recording.save_parquet(path, compression="snappy")`** → Apache
  Parquet file. Wide form, columnar, ~10-20× smaller than the equivalent
  CSV. Opens cleanly in pandas, polars, duckdb, Excel via Power Query,
  and Apache Arrow tooling. Embeds channel units/labels/wire-ids and
  the recording name as column-level metadata.
  Install with `pip install 'opensmu[parquet]'`.
- **`Recording.plot(channels=None, show=True, title=None)`** →
  matplotlib `Figure` with one subplot per channel and shared x-axis.
  Install with `pip install 'opensmu[plot]'`.
- **`opensmu[science]`** umbrella extras key installs parquet + plot
  (which pull in pandas, numpy, pyarrow, matplotlib).
- **`docs/output_formats.md`** — full chooser table covering every
  format (native / parquet / CSV long / CSV wide / JSON / numpy /
  pandas / matplotlib / raw), sizing comparisons, a decision tree, and
  the optional-dependency rule.

### Notes on the optional model

opensmu imports cleanly without any of `numpy`, `pandas`, `pyarrow`, or
`matplotlib` installed — verified by an in-suite test that blocks these
modules in a child interpreter and confirms `import opensmu` succeeds
with none of them loaded.

Each method's import is lazy: the dependency is only imported the
moment the method is called. If the dep is missing, the method raises
a clear `ImportError`:

```
ImportError: save_parquet() requires pyarrow.
Install with: pip install 'opensmu[parquet]'
```

### Sizing example

Same 5 s recording of `mc` (4 kHz) + `mv` (1 kHz) across formats:

| Format | Size |
|---|---|
| `.opensmu` (native) | ~40 KB |
| `.parquet` (snappy) | ~50 KB |
| `.csv` (wide) | ~200 KB |
| `.json` | ~600 KB |
| `.csv` (long) | ~750 KB |

For a 30-minute battery-profiling capture (7.2 M samples on `mc`), the
gap widens — parquet stays around ~10 MB while CSV crosses 250 MB.

### Tests

16 new tests in `tests/test_recording_export_extras.py`:
- numpy: 5 tests (shape/dtype/values/timestamps/offset/empty-buffer)
- pandas: 3 tests (Series, wide DataFrame, empty)
- parquet: 3 tests (round-trip, embedded metadata, compaction)
- plot: 3 tests (subplots-per-channel, channel subset, empty-rejection)
- lazy import: 2 tests (clean opensmu import without deps, friendly
  ImportError when calling a method without its dep)

Total tests: 187 → **203 passing**.

## [0.3.1] — Claude Code skill

### Added

- **`skills/opensmu/SKILL.md`** — a Claude Code skill that complements
  the MCP server. The MCP server lets Claude *drive* the device; the
  skill guides Claude when *writing opensmu Python code* (custom
  analysis, batch processing, plotting, transient detection, anything
  beyond the 23 tool surface).
- The skill covers: the two integration paths (MCP vs Python),
  context-manager + safety patterns, the channel-code quick reference,
  recording analysis recipes, exception hierarchy, anti-patterns
  (don't use the Otii server, don't call deferred methods, don't write
  your own framing), and copy-paste recipes for voltage sweep,
  transient detection, live monitoring, and batch processing.
- Install instructions added to [`docs/mcp.md`](docs/mcp.md) — symlink
  (recommended, stays in sync with repo) or static copy. Lands at
  `~/.claude/skills/opensmu/SKILL.md`.

### When does the skill activate?

Whenever the user is doing anything with opensmu beyond what MCP tools
cover. Frontmatter description: "Use when controlling a Qoitech Otii
Arc Pro source-measurement unit, writing code with the opensmu Python
library, analysing captured .opensmu recordings, or building
measurement automation."

## [0.3.0] — MCP server

Open the Arc Pro to any MCP-aware client (Claude Code, Claude Desktop,
Cursor, custom agents) as a set of structured tools.

### Added

- **`opensmu.mcp`** — a FastMCP server exposing 23 tools covering every
  user-facing capability of the library: device info, every setpoint,
  output enable (with safety guards), live reads, snapshot, synchronous
  recording with statistics, GPIO/UART, connection management.
- **`opensmu-mcp` console script** — `pip install opensmu[mcp]` and run
  `opensmu-mcp` to start the stdio server.
- **`docs/mcp.md`** — install, configuration (Claude Code + Claude Desktop
  JSON snippets), full tool reference, safety model, troubleshooting.
- **Safety model** for `enable_output`: refuses unless all three guards
  pass (current_limit set, voltage set, `confirm_dut_attached=True`).
  Returns structured `{"error": ..., "guidance": ...}` responses so the
  LLM gets clear feedback on how to proceed.
- **23 new tests** (6 hardware-free + 17 hardware-required) covering tool
  surface, schemas, state snapshots, and round-trips against the device.

### Verified end-to-end

- `opensmu-mcp` initializes MCP protocol v2024-11-05 over stdio.
- `tools/list` enumerates all 23 tools with descriptions sourced from
  Python docstrings.
- `tools/call info` returns live device metadata: name=Arc, fw=3.1.3,
  serial=442032203546324D3230353235313033.

### Optional dependency

- `mcp >= 1.0` — installed automatically with `pip install opensmu[mcp]`.

## [0.2.0] — 100% decoding sweep

A systematic decode pass across every captured trace exposed the rest of the
wire vocabulary. Result: opensmu now understands every distinct frame
type the device emits or accepts in the captured corpus.

### Added — newly decoded wire commands

- **GET-parameter interface** (`type=0x64`) — every parameter we can SET, we
  can now read back. New `SMU.get_param(cmd_code)` returns a unified
  :class:`Response` with status + data. Convenience methods:
  `get_device_name()`, `get_hw_version()`, `get_fw_version()`,
  `get_device_id()`, `get_main_voltage_setpoint()`,
  `get_max_current_setpoint()`, `get_exp_voltage_setpoint()`,
  `get_uart_baudrate_setpoint()`, `get_channel_inventory()`.
- **SET_POWER_REGULATION** (`type=0x66 cmd=0x0A`) — `set_power_regulation()`
  now sends a real wire command. Modes map to `voltage=0`, `current=1`,
  `inline=10`, `off=100`.
- **write_tx** (`type=0x82 cmd=0x19` + UTF-8 text) — `SMU.write_tx()` no
  longer raises; sends a variable-length text payload distinct from the
  SET/GET vocabulary.
- **set_tx** (decoded as `set_gpo(3, state)`) — no longer raises. The TX
  pin is the third GPO slot in the GPO bit pattern (bits 6/7 of
  `SET_GPO`).
- **set_gpo(3, state)** — previously rejected, now valid.
- **Prepare-stop (`type=0x7E`)** — `stop_recording()` now sends this 8-byte
  flush frame before the per-channel disable burst, matching vendor
  behaviour. Eliminates a small streaming-mode-switch lag.
- **POLL (`type=0x0A`)** — `encode_poll(seq, timestamp_us)` available for
  the optional ~1 Hz host heartbeat (not sent by default; the device
  works without it).

### Added — decoded inbound formats

- **Unified `Response` parser** — `parse_response(payload)` decodes any
  `0e 03 99 ff`-prefixed frame into `Response(response_seq, status, data)`
  with status conventions `0=OK / -3=N/A / negative=rejected`.
  `Response.as_u32() / as_int() / as_float() / as_text() / as_u32_array()`
  pull typed values from the data field.
- **180-byte `ce f2 2f ff` baseline envelope** — confirmed as a simple
  container holding one `02 00 08 00`-prefixed sample record per channel.
  Already correctly parsed by `iter_samples`' baseline byte-scan; now
  documented in `docs/protocol.md`.
- **Packed sample frame** — already implemented in v0.1.1, now formally
  documented alongside the GET response format.

### Changed — protocol model

- `parse_error_frame` / `parse_set_ack_frame` now key off the response
  status word instead of the legacy `04 10 00 00` / `0x10XX` "discriminator"
  bytes — which we discovered in cap #16 were actually just sequence
  numbers, not type discriminators. The legacy convenience APIs are
  preserved for backwards compatibility, but new code should use
  `parse_response()` directly.

### Defer notes (no change in scope, just clarified)

- **`set_channel_samplerate`** — fails at the Otii server's JavaScript
  layer before reaching the device (cap #42). Most plausible
  interpretation: there is no wire command for this — sample rates are
  hardware-fixed and "sample rate" in the GUI is a post-processing
  downsample. Marked architecturally not-a-wire-command.
- **`calibrate()`** — fires zero wire commands via the API (cap #41).
  The vendor's calibration flow lives somewhere else (likely the
  Desktop GUI's service-mode path). Stub kept.
- **Battery emulation** — blocked at the Otii server by a separate
  "Battery Toolbox" license we don't hold (cap #40 reached the gate but
  not the wire). Decoding requires manually driving the Otii Desktop
  GUI with DMS capturing in parallel. Stubs kept.

### Internal

- `protocol.py` constants: `CMD_SET_POWER_REGULATION`, `CMD_WRITE_TX`,
  `CMD_GET_DEVICE_NAME`, `CMD_GET_HW_VERSION`, `CMD_GET_FW_VERSION`,
  `CMD_GET_DEVICE_ID`, `CMD_GET_CHANNEL_INVENTORY`, `POWER_REGULATION_MAP`,
  `TYPE_PREPARE_STOP`, `TYPE_WRITE_TEXT`.
- `device.py` GET round-trip: `SMU.get_param()` sends a type=0x64 request,
  drains inbound bytes, matches on response_seq, returns the parsed
  `Response`. Requires no active recording (reader thread owns the
  byte stream during recording).
- Hardware-free test coverage expanded from 89 to 105 tests; hardware
  tier from 43 to 59 (132 -> 164 total).

## [0.1.1] — full-rate streaming

### Fixed

- **Full-rate sample streaming unlocked.** Decoded from a fresh wire capture
  of the Otii vendor server: recording is set up by a *per-channel* enable
  command (`[seq][0x78][wire_id][1]`) followed by an 8-byte cleanup
  (`[seq][0x7C]`) — not the 76-byte `69 83 2a ff …` payload v0.1 sent (which
  turned out to be a misread of the device's *inbound* packed-sample
  frame).
- Verified rates on the Arc Pro: mc 4042 sps, mp 4042 sps, mv 1015 sps —
  ~670× improvement on mc/mp and ~170× on mv.
- Sub-millisecond transients on the DUT are now resolvable.

### Added

- `protocol.encode_channel_enable_for_recording(seq, wire_id, enable)` —
  builds the 16-byte per-channel enable / disable payload.
- `protocol.iter_samples()` now auto-detects and unpacks the **packed
  sample frame** format (`69 83 2a ff` + per-channel sub-1 / sub-4
  records + sentinel). Sub-4 records yield 4 samples per frame at
  4× the frame rate.
- Constants `PACKED_FRAME_MAGIC`, `PACKED_FRAME_SENTINEL` for the
  inbound packed frame envelope.
- `TYPE_CHANNEL_ENABLE` constant (alias of the now-misnomered
  `TYPE_STOP_RECORDING`).
- Tightened hardware test asserts `>=1500` mv samples and `>=6000` mc
  samples in a 2 s recording (was `>=5`).

### Internal

- `SMU.start_recording()` now sends a per-channel `type=0x78` enable burst
  + cleanup instead of the legacy 76-byte payload.
- `SMU.stop_recording()` sends symmetric per-channel disables.
- Removed unused `encode_start_recording` and `RecordingChannel` imports
  from `device.py`.

## [0.1.0] — initial release

First public release. Drives the Qoitech Otii Arc / Arc Pro directly
over USB CDC-ACM with no vendor server, license, or GUI.

### Added

- `SMU` class with full device lifecycle (`open` / `close` / context
  manager) and the three-step session-init handshake
- Setters for every wire command in the v0.1 scope: main voltage,
  current limit, main current (CC), output enable, range, 4-wire,
  source-current-limit enable, expansion-port voltage, EXP-5V,
  legacy sink, ADC shunt resistor, UART enable + baud rate, GPO pin
  state
- Cached state properties for every setter
- `Channel` enum carrying code, wire id, subtype, sample rate, unit,
  label, and co-enables metadata for all 14 channels
- Per-channel enable / disable, with auto-co-enable for the
  `mc → mp` and `ac → ap` pairs
- `Recording` class — context-managed via `SMU.record()` with a
  background reader thread, or manual `start_recording` / `stop_recording`
- `Recording.statistics` returning `Statistics` (min, max, average,
  rms, sample_count, duration, charge for current channels, energy
  for power channels)
- `Recording.info`, `.data`, `.timestamps`, `.index_at`, `.count`,
  `.crop`, `.downsample`, `.rename`, `.log`
- `Recording.save_csv` (long + wide), `.save_json`, `.save_raw`, and
  `.save` (native `.opensmu` binary) with `Recording.load` for round-trip
- Real-time streaming iterator (`SMU.stream`) yielding typed `Sample`s
- `SMU.read_value` and `SMU.read_raw` escape hatches
- Asynchronous device-error frame surfacing via `SMUCommandError` on
  next API call
- Full pyserial-based discovery (`SMU.discover()`)
- Comprehensive exception hierarchy: `SMUError`, `SMUConnectionError`,
  `SMUProtocolError`, `SMUCommandError`, `SMUValueError`,
  `SMUTimeoutError`, `SMUNotImplementedError`
- CLI: `opensmu discover / info / set-voltage / set-output /
  set-range / set-current-limit / set-exp-voltage / set-gpo /
  capture / stream`
- 132 tests (89 hardware-free + 43 hardware-required)
- Documentation: getting started, API reference, wire-protocol
  reference, AGENTS.md, design doc, official API inventory,
  ROADMAP, TEST_PLAN, VALIDATION_REPORT
- 4 example scripts: `basic`, `streaming`, `voltage_sweep`,
  `save_and_load`

### Deferred (raises `SMUNotImplementedError`)

- Battery emulation: `set_supply_battery_emulator`,
  `set_battery_profile`, `enable_battery_profiling`,
  `wait_for_battery_data`, and the entire `BatteryEmulator` class
- Calibration: `calibrate()`
- Firmware upgrade: `firmware_upgrade()` (deferred indefinitely —
  bricking risk)
- Channel-level sample rate control: `set_channel_samplerate()`
- UART log channel: `iter_uart_log()`
- TX / RX as GPO / GPI: `set_tx()`, `get_rx()`, `write_tx()`

See `ROADMAP.md` for rationale and pick-up notes for each.

### Known limitations

- Device's baseline streaming rate is ~6 Hz across all channels until
  the (not-yet-decoded) full-rate command is sent. The channel-capability
  rates (1 kHz / 4 kHz) are theoretical maxima.
- Single-device support tested; multi-device API present but exercised
  only with one Arc Pro.
- Windows / Linux / macOS via pyserial — only Windows has been
  hardware-validated in this release.

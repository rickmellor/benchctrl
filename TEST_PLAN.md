# benchctrl test plan

Validation strategy for the whole package. Tests are organised in two
tiers:

- **Hardware-free** — protocol encoding and parsing, statistics, file
  and stream I/O, exception behaviour, the config/session seam, the
  remote wire protocol, and the run engine. Always runs in CI. Lives
  in `tests/` without the `hardware` marker.
- **Hardware-required** — needs real instruments on USB. Marked
  `@pytest.mark.hardware`, and skips cleanly with a useful message
  when the device isn't present.

```bash
pytest -m "not hardware"     # 956 pass, 23 skip, ~7 min, no hardware
pytest -m hardware           # 152, needs the bench
pytest                       # both
```

## Simulators, not mocks

The most important change since the original plan: most hardware-free
tests now drive a **device simulator** from `benchctrl.sim` rather
than a mock of a benchctrl class.

A simulator speaks its instrument's real wire protocol over a
pseudo-terminal, so the production driver connects to it unmodified.
That means `Transport`, the binary framing, the timed session-init
handshake, the error-frame queue and the recording reader thread are
all genuinely exercised in a run with no hardware attached — none of
it monkeypatched. The SCPI simulators go through pyvisa-py's ASRL
backend, so the real pyvisa stack is in the path too.

This exists because mocks drift. The v0.9.2 emulator mock had diverged
from the hardware in a way that made a broken emulator look healthy.
A simulator can drift from the *device*, but it cannot drift from the
*driver* without a test going red.

Reach for a mock only when the assertion is specifically "this call
was made". Otherwise use a simulator. See
[`docs/simulation.md`](docs/simulation.md).

Waveforms are analytically known (`Constant`, `Sine`, `Square`,
`Ramp`, `Steps`, `OhmicLoad`), so tests assert exact statistics rather
than "a number arrived".

## Coverage matrix — hardware-free

### Framework primitives

| File | Target | Coverage |
|---|---|---|
| `test_imports.py` | `benchctrl` | every public symbol importable from the package root |
| `test_exceptions.py` | `exceptions` | hierarchy, attribute carrying, isinstance against std types |
| `test_interfaces.py` | `interfaces` | `SourceMeasurementUnit` Protocol conformance |
| `test_channels.py` | `channels` | enum constants, `code`/`wire_id`/`subtype`/`sample_rate`/`unit`, `from_code`, `coerce`, reverse lookup |
| `test_samples.py` | `samples` | parse by id / by channel, `ChannelBuffer` slicing and timestamps, statistics on synthetic data |

### Arc driver

| File | Target | Coverage |
|---|---|---|
| `test_protocol_framing.py` | `otii_arc.protocol` | frame round-trips, checksums, `iter_frames` validation, resync after garbage, truncated tails |
| `test_protocol_commands.py` | `otii_arc.protocol` | every SET encoding, GPO bit pattern, recording channel records (subtype-1 12 B, subtype-4 24 B), start/stop/cleanup, init payloads |
| `test_protocol_inbound.py` | `otii_arc.protocol` | `parse_error_frame` positive and negative, `parse_set_ack_frame` discrimination, `iter_samples` extraction |
| `test_protocol_v02.py` | `otii_arc.protocol` | the v0.2 decode set — GET interface, unified response, POLL, power regulation, channel inventory |
| `test_transport_discovery.py` | `otii_arc.transport` | port discovery returns a list (empty allowed), `PortInfo` formatting |

### Recording and export

| File | Coverage |
|---|---|
| `test_recording_lifecycle.py` | construction, info / statistics / data / timestamps / index_at / count, crop, downsample, rename, log |
| `test_recording_io.py` | `save_csv` long and wide, `save_json`, native save+load round-trip (empty buffers, single channel, multi-channel), and the stream codecs — `save_to_stream` / `load_from_stream` / `to_bytes` / `from_bytes`, including that they produce byte-identical output to `save()` |
| `test_recording_export_extras.py` | numpy / pandas / parquet / matplotlib paths, and the clear ImportError when the extra isn't installed |

### Battery subsystem

| File | Coverage |
|---|---|
| `test_battery_profile.py` | profile JSON I/O, round-trip of the bundled CR2032 / CR123A / LiPo profiles |
| `test_battery_calculator.py` | predicted runtime across duty cycles and chemistries |
| `test_battery_profiler.py` | discharge sweep drive against a simulated SMU |
| `test_battery_emulator.py` | the 100 Hz control loop, including `OhmicLoad` closing the V→I loop so the emulator runs without a cell. The propagating-error model is asserted explicitly — a hardware fault must not present as a working emulator sourcing zero current |

### Companion drivers

| File | Coverage |
|---|---|
| `test_bench_qr10x.py` | AT command surface, relay-ladder quantisation, safety limit enforcement |
| `test_bench_rigol_dl3031a.py` | SCPI surface, LIST / transient / battery-discharge modes, and rejection of the known-bad 4-step LIST program |
| `test_bench_rigol_dp2031.py` | the largest suite in the repo — source/measure, protection, IEEE 488.2 status, pairing and tracking, the Arb timer sequencer, analyzer, trigger I/O, memory, block-format parsers |

### The local / remote / sim seam

| File | Coverage |
|---|---|
| `test_session_config.py` | config precedence (explicit > CLI > env > file > all-local), per-device mode resolution, rejection of `deadman_s <= heartbeat_s`, and the loud failure when a remote device names no reachable endpoint |
| `test_discovery.py` | the signature table and confidence levels. Asserts **no driver signature collides with a known USB-serial bridge** (CH340 / FTDI / CP210x) — the test that stops confident false positives |
| `test_sim_loopback.py` | the pty pair in raw mode, bounded tx queue reporting overruns rather than dropping samples, and end-to-end capture through the production driver |

### Remote mode

| File | Coverage |
|---|---|
| `test_remote_protocol.py` | frame encode/decode, blob chunking interleaved with heartbeats, HMAC challenge-response (including rejection of a wrong token), the value codec allowlist rejecting arbitrary dotted paths, and exception marshalling across all four driver hierarchies with degradation to the nearest known ancestor |
| `test_beacon.py` | UDP beacon encode/decode; asserts the payload carries a token *fingerprint* and a device count, never models or serials |

### Run engine

| File | Coverage |
|---|---|
| `test_run_engine.py` | spec validation before energising, content hashing (including that `60` and `60.0` hash identically after the coercion fix), tick ordering as a safety property, dwell times on conditions, envelope immutability, SQLite+ndjson durability, `since_seq` replay exactness, and that a run marked `running` under a previous boot id becomes `interrupted` rather than resuming |
| `test_llm_supervisor.py` | the eight-tool allowlist, forward-only `advance_phase`, `abort_run` being stop-only, the three-violation lockout, and that a 3-second run finishes on time against a 30-second model stall |

### MCP surface

| File | Coverage |
|---|---|
| `test_mcp.py` | tool registration, argument coercion, dict returns, safety-guard behaviour on `enable_output` |
| `test_mcp_hw.py` | the same tools end-to-end against real devices (hardware-marked) |

## Coverage matrix — hardware-required

152 tests across four instruments. They exercise every wire command
and SCPI string at least once against the real device, with nothing
connected to the output terminals unless the test says otherwise.

| File | Instrument | Verifies |
|---|---|---|
| `test_smu_connect.py` | Arc Pro | discovery, open/close cycle, `version()` after streaming |
| `test_smu_setters.py` | Arc Pro | every setter: voltage (incl. low-range cap raising `BenchCommandError` and high-range unlock), current limit, exp voltage, exp 5 V, output, 4-wire, CC enable, ADC resistor, UART, GPO pins, legacy sink |
| `test_smu_channels.py` | Arc Pro | enable/disable round-trip, co-enables, varargs form |
| `test_smu_recording.py` | Arc Pro | context-manager capture, two-channel capture, CSV long/wide, native round-trip, charge and energy statistics |
| `test_smu_stream.py` | Arc Pro | finite-duration streaming, typed `Sample` yields |
| `test_smu_errors.py` | Arc Pro | client-side range rejection, device rejection carrying `err_code` and `last_good` |
| `test_smu_v02_hw.py` | Arc Pro | the v0.2 decode set against hardware |
| `test_bench_qr10x.py` | QR10x | AT round-trips, resistance setting, safety limit |
| `test_bench_rigol_dl3031a.py` | DL3031A | SCPI round-trips, LIST playback, transient mode, battery discharge |
| `test_bench_rigol_dp2031.py` | DP2031 | OVP trip + clear on CH3, multi-channel setpoint round-trip, tracking and pair state, `program_timer` + readback via the IEEE 488.2 block parser, screenshot BMP capture |
| `test_mcp_hw.py` | bench | MCP tools against real devices |

## Deferred features — explicit no-op assertions

For each `BenchNotImplementedError`-raising method,
`test_deferred_features.py` confirms that exact exception is raised
and that the message points at `ROADMAP.md`:

- `calibrate`, `firmware_upgrade`
- `enable_battery_profiling`, `set_battery_profile`,
  `set_supply_battery_emulator`, `wait_for_battery_data`
- `set_channel_samplerate`
- `iter_uart_log`, `write_tx`, `set_tx`, `get_rx`
- `Recording.get_log_offset`, `Recording.set_log_offset`,
  `Recording.import_log`, `Recording.append_user_log`

## Coverage targets

| Layer | Target |
|---|---|
| `otii_arc.protocol` | 100% line; 100% branch on `iter_frames` |
| `net.codec` / `net.frames` | 100% line — the allowlist is a security boundary |
| `net.auth` | 100% line, including every rejection path |
| `samples` | 95% line, including degenerate empties |
| `recording` | 90% line; file and stream codecs must be proven byte-identical |
| `config` / `session` | 95% line — every precedence rule and every loud-failure path |
| `agent.runs.spec` | 100% on validation and hashing |
| `agent.runs.engine` | 90%, with tick ordering asserted explicitly |
| Driver public API (hardware-free) | 85% — every setter range-check, every stub |
| Driver public API (hardware) | exercised end-to-end against the instrument |

## Things a test must not do

- **Assert against a mock where a simulator exists.** See above.
- **Assert "a number arrived".** Simulator waveforms are
  analytically known; assert the value.
- **Leave an output energised.** Hardware tests disable outputs in
  `finally`, and driver `__exit__` does too.
- **Depend on wall-clock timing for correctness.** The run engine
  tests use the simulator's clock. Timing assertions that *are* the
  point (the 30-second stall test) say so explicitly.

## Validation procedure

1. **Hardware-free**: `pytest -m "not hardware" -q` — must be green
   before moving on. No device needed.
2. **Hardware-required**: `pytest -m hardware --tb=short` — with the
   bench on USB and nothing connected to the output terminals.
3. **Remote**: bring up `benchctrl-agent` on the bench machine and run
   the MCP suite against it with `BENCHCTRL_REMOTE` set. The tools are
   unchanged, so a pass here proves the seam is transparent.

Results land in [`VALIDATION_REPORT.md`](VALIDATION_REPORT.md).

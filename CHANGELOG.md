# Changelog

All notable changes to OpenSMU. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Known limitations across all versions are tracked in
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) (hardware caps,
firmware quirks, harness workarounds). Read it before debugging a
new failure — it's likely a documented limit.

## [0.9.7] — adversarial-review fix-batch + LIST/battery firmware bugs unearthed

### Fixed — compound silent-failure chain in `Emulator`

Three independent bugs that compounded to produce "emulator runs
silently, state() looks fine, but no DUT integration is happening":

1. `Emulator.start()` wrapped every SMU config call (`set_range`,
   `set_current_limit`, `set_current_limit_enabled`,
   `set_power_regulation`) in `try/except Exception: log + pass`.
   A typo, a wrong arg, or a missing mock method would silently
   skip configuration and proceed to `set_output(True)` with the
   device in an unknown state. **Now**: exceptions propagate;
   start() aborts before enabling the output.
2. `Emulator._read_current` had a bare `except Exception: return 0.0`
   that v0.9.2 claimed to fix but didn't. Any read failure (queued
   SCPI error, transport hiccup, etc.) produced "I = 0 forever,
   v_out = OCV". The outer `_loop` already catches and logs+retries,
   so the inner swallow was both redundant and harmful. **Now**:
   exceptions propagate to the outer handler, which logs at warning
   and continues.
3. The test mock SMU (`_MockSMU` in `tests/test_battery_emulator.py`)
   was missing the v0.9.1 methods. Combined with (1), the calls
   silently no-op'd in tests. **Now**: the mock has all four
   methods + matching `*_calls` lists, and new tests assert
   `start()` actually calls them in order.

New tests: `test_emulator_start_configures_smu_cv_mode`,
`test_emulator_start_propagates_config_failures`,
`test_emulator_loop_logs_and_retries_on_read_failure`.

### Discovered — `:SOUR:LIST:STEP` semantics + STEP=4 firmware bug

Bench investigation of the "5-step LIST onset slip" found two
distinct issues:

1. **`:SOUR:LIST:STEP N` means N total steps**, not N+1 as the
   manual's application instance suggested. v0.9.6's driver was
   sending `STEP N-1`, executing one fewer step than intended.
   Captures for CR2032/CR123A still showed TX bursts because
   `end_behavior="LAST"` held the (mis-played) TX step.
2. **`STEP=4` is a firmware bug** (verified on fw 00.01.05.00.01):
   regardless of how many steps are programmed, ``STEP=4`` fires
   no steps at all. ``STEP=2``, ``STEP=3``, ``STEP=5``, etc. all
   work. The driver now rejects 4-step programs at
   `list_set_step_count(4)` and `program_list(steps=[...4...])`
   with a clear error pointing to the workaround (use 3 or 5
   steps with appropriate `count`).

### Discovered — DL3031A `:SOUR:FUNC:MODE FIXed` is one-way

Once the device enters LIST / WAV / BATTery / OCP / OPP mode,
`:SOUR:FUNC:MODE FIXed` is silently rejected. `*RST`, `*WAI`,
`*OPC?`, `*CLS`, input toggling, and cycling through other modes
all fail to bring it back. **Only power-cycling** restores FIX
mode. Documented in `KNOWN_LIMITATIONS.md` § F-3. The runner's
`dynamic-list` teardown reads back `get_function_mode()` and logs
*"DL3031A stuck in {MODE} mode after teardown — power-cycle before
reuse"* so the operator sees the failure.

### Discovered — `:FETCh:DISChargingTime?` returns H:MM:SS, not float

The manual documents the return type as "a real number" but the
device returns colon-delimited time (e.g. `"0:0:15"`).
`battery_stats()` now parses both formats — H:M:S preferred, plain
float fallback if a future firmware change matches the manual.
4 new parser tests including malformed-input handling.

### Discovered (workaround pending) — LiPo dynamic-list unreliable in high range

With the Arc Pro in high range (LiPo profiles, 4.2 V), LIST
playback fires a partial / misordered sequence — one TX burst at a
non-deterministic time, then stops, instead of the expected
`cycles × n_steps` repeats. CR2032 / CR123A in low range work
correctly. Cause appears to interact with the FUNC:MODE-FIXed
one-way bug (above); not isolated. Documented as
`KNOWN_LIMITATIONS.md` § F-2. Use `--pattern hires` for LiPo
transient validation in the meantime.

### Discovered — `transient_set_frequency` takes Hz, not kHz

Manual says kHz; bench-verified the device takes Hz. Sending
`:SOUR:CURR:TRAN:FREQ 10` reads back as `0.1 s` period. The driver
parameter name `hz` was already correct; the docstring was
misleading and has been corrected. New hardware-marked test
asserts period = 1/freq.

### Fixed — `RigolDL3031A._autodiscover` ambiguity

Now deterministic (sorted resource list). If multiple Rigol
DL3000 devices are connected, raises with the list of candidate
VISA resource strings so the caller can pass an explicit one.

### Fixed — MCP exception types match driver hierarchies

`_get_dl3031a()` now raises `RigolDLConnectionError` (was bare
`RuntimeError`); `_get_qr10x()` raises `QR10xConnectionError`.
Per-driver hierarchies stay consistent across SDK and MCP surfaces.

### Fixed — `dl3031a_close` surfaces input-off failures

Previously returned `{"closed": true}` even if `set_input(False)`
raised, leaving the load potentially sinking current with the
operator unaware. Now returns
`{"closed": true, "input_off_failed": "...", "warning": "..."}`
with a clear safety note when the input couldn't be disabled.

### Added — SDK ↔ MCP parity catch-up for DL3031A

The v0.9.6 review surfaced that the DL3031A had ~25 SDK methods
without MCP equivalents — primarily `get_*` queryable state and
individual `measure_*` / `fetch_*` methods. **Now**: 45 DL3031A
MCP tools (was 25). Granular LIST / transient / battery setters
remain SDK-only (the convenience wrappers `program_list` /
`configure_transient_pulse` / `configure_battery_test` are the
MCP-facing surface) — documented as SDK-only in the bench docs.

### Added — `--profile-dir` + `OPENSMU_BATTERY_PROFILE_DIR`

`validation/run_validation.py` no longer hardcodes a
user-specific Otii install path. Resolution order: CLI
`--profile-dir`, then `$OPENSMU_BATTERY_PROFILE_DIR`, then
auto-detect under `%LOCALAPPDATA%/otii3/app-*/resources/batteryprofiles`,
then repo-local `validation/profiles/`.

### Added — `KNOWN_LIMITATIONS.md`

Aggregated list of hardware caps, firmware quirks, and harness
workarounds, in one file rather than buried under per-version
CHANGELOG entries. Linked from the top of CHANGELOG.

### Smaller fixes / tightenings

- `dl3031a_program_list` MCP docstring now matches the driver's
  validator (was `2-512`, driver enforces `2-512 \\ {4}`).
- LIST test assertions are now exact wire-format matches (was
  `startswith` which would let `:RANGE` typos pass).
- Validation `time.sleep(min(0.005, end - now))` clamps to
  non-negative (was a `ValueError` when the loop fell behind).
- `--cycles 0` is rejected with a clear error (was infinite LIST
  + zero sleep → load running indefinitely).
- Ctrl-C during `--all` matrix runs now breaks cleanly (was
  swallowed by the `except Exception` and continued to the next
  profile).
- Validation hires/dynamic-list runners reject `pinned_V < 0.1V`
  before pinning (was a silent zero-output capture).
- Loop pile-up / fetch_all aliasing now documented in docstrings.
- Concurrency note added to MCP module docstring (single-client
  serialization assumed).

### Tests

292 hardware-free tests passing (was 282 in v0.9.6, +10):
- 4 emulator hardening tests (config-error propagation, mock
  parity, read-failure retry)
- 4 battery_stats parser tests (H:M:S, hour-minute-seconds,
  float fallback, malformed input)
- Tighter LIST wire-format assertions
- MCP-level coercion and connection-error tests
- Driver-exception type assertions

Hardware-marked tests: 90+ (was 89; +2 new — battery discharge
smoke + transient_set_frequency unit check).

## [0.9.6] — DL3031A built-in modes: LIST, transient, battery discharge

### Added — `RigolDL3031A.program_list` and friends (LIST mode)

Wraps `:SOURce:LIST:*` so a programmable CC/CV/CR/CP sequence can be
pushed to the DL3031A and executed entirely in firmware with
deterministic timing (step widths from 50 µs to 3600 s). The right
tool for sub-100 ms TX bursts and other transients where USB-TMC
round-trips can't keep up.

API:

- `list_set_mode` / `list_set_range` / `list_set_count` /
  `list_set_step_count` / `list_set_step` / `list_set_slew` /
  `list_set_end` — granular SCPI wrappers
- `program_list(steps=..., mode='CC', count=1, range_value=...,
  slew_A_per_us=..., end_behavior='OFF'|'LAST',
  trigger_source='BUS'|'MANUal'|'EXTernal')` — convenience that
  pushes a whole sequence in one call and switches the device to
  LIST regulation mode

Two manual-misread bugs ironed out:

- `:SOUR:LIST:STEP N` means steps 0..N inclusive (so N+1 total).
  The driver accepts the **total** step count and subtracts 1
  internally to match the firmware's convention.
- `:SOUR:LIST:SLEW <step>,<value>` is **per-step**, not global.
  `program_list` applies the same `slew_A_per_us` to every step
  when provided.

Documented LIST-end behavior: `LAST` (hold final step's value) or
`OFF` (disable input). The manual incorrectly suggests `NORMal|LAST`
in places — the firmware accepts only `LAST|OFF`.

### Added — trigger system (`:TRIGger`)

- `set_trigger_source` ({BUS|EXTernal|MANUal}) / `get_trigger_source`
- `trigger_now` — issues `:TRIGger` (software / BUS trigger)

LIST and transient sequences default to MANUal trigger after `*RST`;
the driver sets BUS as the default in `program_list` so a software
trigger fires the sequence.

### Added — CC transient mode (`:SOURce:CURRent:TRANsient:*`)

- `transient_set_mode` (CONTinuous / PULSe / TOGGle)
- `transient_set_a_level` / `transient_set_b_level` /
  `transient_set_a_width` / `transient_set_b_width` /
  `transient_set_frequency` / `transient_set_duty`
- `transient_enable` — arm / disarm via `:SOURce:TRANsient[:STATe]`
- `configure_transient_pulse(a_level_A, b_level_A, a_width_s,
  b_width_s, mode)` — convenience

### Added — battery discharge mode (`:SOURce:BATTary:*`)

- `battery_set_range` / `battery_set_level` /
  `battery_set_voltage_stop` (V_stop + VEN enabstop) /
  `battery_set_capacity_stop` / `battery_set_time_stop` /
  `battery_set_von`
- `configure_battery_test(current_A, v_stop_V?, capacity_stop_mAh?,
  time_stop_s?, von_V?, range_A?)` — convenience that pushes the
  full setup and switches to `BATTery` function mode
- `battery_stats()` — returns capacity (mAh), energy (Wh), discharge
  time (s), and instantaneous V/I

### Added — `set_function_mode` / `get_function_mode`

Switch the top-level regulation source between FIXed / LIST / WAVe /
BATTery / OCP / OPP. Implicitly set by the various
`program_list` / `configure_*` helpers.

### Added — `:FETCh:` query methods

Already in v0.9.5 but consolidated here for clarity. `fetch_voltage`
/ `fetch_current` / `fetch_power` / `fetch_resistance` / `fetch_all`
read the device's continuously-updated measurement register
(non-blocking ~1 ms each, vs `measure_*` which trigger a fresh
~200 ms integration).

### Added — `:SYSTem:KEY` 0 (CC) through 25 (numeric) etc

Not exposed in this release; the driver leaves front-panel-key
simulation as the user's job via `dl.write(":SYSTem:KEY <code>")`
if needed.

### Added — MCP tools for the new methods

New tools (`dl3031a_*`):

- `dl3031a_set_function_mode` / `dl3031a_get_function_mode`
- `dl3031a_program_list` — push a LIST from a JSON `[level, width]` list
- `dl3031a_set_trigger_source` / `dl3031a_trigger`
- `dl3031a_configure_transient_pulse` / `dl3031a_transient_enable`
- `dl3031a_configure_battery_test` / `dl3031a_battery_stats`
- `dl3031a_fetch`

DL3031A MCP tool count: 17 → **25**. Total MCP tools: 65 → **73**.

### Added — `--scenario dynamic-list` validation runner

New scenario type: programs a CC LIST on the DL3031A, fires it via
software trigger inside an `SMU.record(...)` window, captures the
response at the Arc Pro's native streaming rate (~4 kHz on I,
~1 kHz on V). The DL3031A executes the steps entirely in firmware
with deterministic timing — no per-step USB-TMC round-trip.

`DEFAULT_LIST_TX_PATTERN_A` is currently 3 steps (sleep / 50 ms TX
/ sleep). **Capability ceiling** (see `KNOWN_LIMITATIONS.md` § F-1):
LIST programs with ≥ 5 steps under BUS trigger have a ~3 s onset
slip — the LIST fires but the waveform doesn't start until well
after `:TRIGger`. The SCPI strings the driver sends are individually
correct; the cause is in the firmware/trigger interaction and is
not yet isolated. Use 3 steps + `count=N` repeats as the reliable
shape.

Saved scenarios for CR2032 and CR123A. **LiPo dynamic-list is not
supported in v0.9.6** (`KNOWN_LIMITATIONS.md` § F-2): with the Arc
Pro in high range, the LIST playback shows zero current during
capture even though the SCPI surface accepts the program. Use
`--pattern hires` for LiPo transient validation in the meantime.

### Tests

72 hardware-free tests in `tests/test_bench_rigol_dl3031a.py`
(was 36 in v0.9.3) — covering wire-format for every new method
plus end-to-end mocks. Two hardware-marked tests including a
real LIST program push.

### Discovered

- `:SOUR:LIST:STEP N` is highest-index, not total-count
  (3-step list → STEP 2). Driver compensates.
- `:SOUR:LIST:SLEW` is per-step, not global.
- `:SOUR:LIST:END` accepts `LAST|OFF`, not `NORMal|LAST` as I
  initially misread.
- `:SOUR:FUNC:MODE` doesn't always honor `FIXed` after switching
  away. Workaround: re-program the desired subsystem instead.
- LIST integration timing inside `SMU.record()` is misaligned vs
  the harness's phase events — the LIST plays but with delayed
  onset (~3 s after `:TRIGger` in some flows). Tagged as a known
  limit in `validation/README.md`; the captured waveforms still
  show real LIST behavior.

## [0.9.5] — high-resolution dynamic capture via Arc Pro native streaming

### Added — `--pattern hires` dynamic scenario

`validation/run_validation.py --pattern hires` switches the dynamic
runner to a dedicated implementation that uses `SMU.record(...)` for
the actual V/I capture instead of host-side state polling. The Arc
Pro streams ~4 kHz on `MAIN_CURRENT` (subtype-4 packed) and ~1 kHz on
`MAIN_VOLTAGE`, which is enough to resolve the DL3031A's actual TX
response shape — the standard pattern's 20 Hz polling could not.

Three hires scenarios captured (CR2032, CR123A, LiPo @ +20 °C):

- 4 000 Hz I time series, ~36 k samples in 10 s
- TX I peaks: 71 mA (CR2032), 70 mA (CR123A), 86 mA (LiPo)
- TX steady: ~30 mA across all chemistries
- DL3031A inrush is consistently ~2× steady — closed-loop catch-up
  artifact, documented for future LIST/transient-mode comparison

### Discovered — emulator loop + recording reader deadlock

The `Emulator._loop` thread writes `set_voltage` at 100 Hz while
`SMU.record` consumes the transport at ~4 kHz. Running both
concurrently against the same Arc Pro consistently deadlocks within
~100 ms (transport-level contention).

Workaround in the hires runner: settle the emulator at fresh OCV,
read the resulting voltage, stop the emulator, pin the SMU at that
voltage manually, then open the recording. SoC tracking is off for
the duration. For 10 s × 30 mA peak, the SoC delta is ~0.08 mAh
(< 0.04 % of CR2032's capacity) — meaningless for the hires use case.

Saved scenarios document this with `"recording.pinned_voltage_V"` so
it's recoverable from the JSON.

### Discovered — DL3031A input-toggle latency ~700 ms

The first hires runs used 100 ms TX phases (cycle: sleep → R=100 →
sleep). At 4 kHz sampling the recording showed current spikes
landing 700 ms after the `:SOUR:INP:STAT 1` command, well outside
the labeled TX window. Toggling input state cycles the regulation
loop's internal settling — much slower than just changing a
setpoint.

`DEFAULT_HIRES_IOT_PATTERN` widened to 300 ms TX. Sub-300 ms work
needs "input always ON, toggle CC current setpoint" — left for a
future scenario.

### Added — `:FETCh:` query methods on `RigolDL3031A`

`fetch_voltage` / `fetch_current` / `fetch_power` / `fetch_resistance`
/ `fetch_all`. Non-blocking reads of the device's continuously-updated
measurement registers, vs the existing `measure_*` which trigger a
fresh 10 PLC (~200 ms) integration per call. Distinct use cases:
`measure_*` for high-accuracy single-shot, `fetch_*` for fast loops.

The DL3031A adapter (and the standard dynamic scenario's
`load.measure()` hook) now use `fetch_*` so they don't get throttled
by the 200 ms integration time.

## [0.9.4] — validation harness supports both loads; full matrix on DL3031A

### Added — `--load {qr10x,dl3031a}` in the validation harness

`validation/run_validation.py` now accepts either programmable load
via a single CLI switch. The harness wires through a thin
`_LoadAdapter` abstraction that owns each instrument's lifecycle
(open / setup / set_resistance / teardown / close) so the scenario
logic stays load-agnostic.

DL3031A specifics handled in the adapter:

- Out-of-range R (> DL3031A CR max, ~16 kΩ) translates to
  `set_input(False)` — open circuit. Better physical model for a
  sleeping IoT device than a finite 100 kΩ anyway.
- CR min and max are queried at runtime from the device, not hardcoded.
- `*RST` settles for 200 ms before subsequent commands.
- `__exit__` (and `teardown`) always disables the load input.

Bench dict in saved scenarios now carries an additional `load_kind`
field so post-hoc analysis can group / filter by which load produced
each capture. Scenario filenames are now `<profile>_<scenario>_<load>_<utc>.{json,csv,png}`,
schema_version bumped to 2.

### Captured — 11 new DL3031A scenarios

Full static matrix (8 profiles) + dynamic IoT pattern (CR2032, CR123A,
LiPo @ +20 °C) re-captured with the DL3031A as the load. Headline
findings vs the QR10x v0.9.2 baseline:

- **LiPo at 12 Ω**: QR10x was capped at 20 Ω (1 W safety). DL3031A
  pulled true high-current behavior — at −10 °C, V sagged to **3.24 V
  at 324 mA**, vs 3.70 V at 185 mA on QR10x. Real cold-soaked LiPo
  behavior, previously beyond our reach.
- **Light loads (< 1 mA)**: DL3031A's CR-mode regulation breaks
  down. The 10 kΩ / 1 kΩ steps appear as input-off in the captures
  because the load can't sink that little. QR10x remains correct at
  any current.
- **Dynamic phase alignment**: DL3031A switches in microseconds; the
  emulator's ~100 Hz polling can't keep up. The dl3031a dynamic phase
  summary reports near-zero TX current. Documented; sets up the next
  pass (read directly from DL3031A `:MEASure:` queries).

See `validation/README.md` for the full per-load takeaway table.

### Tests / parity

No new test files (harness changes covered by existing scenario
captures); existing test suite still passes (248 hardware-free).

## [0.9.3] — Rigol DL3031A driver (`opensmu.bench.RigolDL3031A`)

### Added — SCPI-over-USB-TMC driver for the Rigol DL3000 series

`opensmu.bench.RigolDL3031A` — pyvisa-based driver for the Rigol
DL3021A / DL3031A programmable DC electronic load. Auto-discovers by
USB VID/PID (`0x1AB1`/`0x0E11`) or accepts an explicit VISA resource
string (USB-TMC or LXI).

Public API (mirrors the QR10x pattern):

- `open(resource=None)` / `close()` (context-manager safe; `__exit__`
  disables the load input)
- `info()` returns `RigolDLInfo` from `*IDN?`
- `reset()` / `clear_status()` / `last_error()` / `raise_if_error()`
- `set_mode(...)` / `get_mode()` — CC / CV / CR / CP
- `set_input(bool)` / `get_input()`
- Per-mode setpoints: `set_current` / `set_voltage` / `set_resistance` / `set_power`
- Ranges: `set_current_range` / `set_voltage_range`
- `set_slew(A/µs)` — symmetric CC/transient slew rate
- `measure_voltage` / `measure_current` / `measure_power` /
  `measure_resistance` / `measure_all()`

Exception hierarchy:

- `RigolDLError` (base)
  - `RigolDLConnectionError` — VISA open / transport failure
  - `RigolDLCommandError` — device returned non-zero from `:SYSTem:ERRor?`
  - `RigolDLValueError` — client-side range / type check failed
  - `RigolDLTimeoutError` — VISA `VI_ERROR_TMO`

Lives behind the `bench-visa` extra (already in `pyproject.toml`); the
top-level `opensmu.bench` module lazy-imports `RigolDL3031A` via PEP 562
so the QR10x path stays usable without pyvisa.

### Added — 17 MCP tools for DL3031A

Per the SDK ↔ MCP parity principle, every public method has a matching
tool. The MCP server holds one DL3031A connection across calls until
`dl3031a_close()`. Tool count: 48 → **65**.

Tools: `dl3031a_open` / `dl3031a_close` / `dl3031a_info` /
`dl3031a_reset` / `dl3031a_last_error` / `dl3031a_set_mode` /
`dl3031a_get_mode` / `dl3031a_set_input` / `dl3031a_get_input` /
`dl3031a_set_current` / `dl3031a_set_voltage` /
`dl3031a_set_resistance` / `dl3031a_set_power` /
`dl3031a_set_current_range` / `dl3031a_set_voltage_range` /
`dl3031a_set_slew` / `dl3031a_measure`.

### Verified — talks to real hardware

Bench setup: DL3031A on USB-TMC via USB hub (the VISA resource string
is `USB0::0x1AB1::0x0E11::DL3D232300106::INSTR`), Rigol Ultra Sigma's
VISA backend loaded. `*IDN?` parses cleanly; mode / setpoint /
measurement round-trip; hardware-marked test passes.

### Tests

- 36 hardware-free unit tests using a `FakeInstrument` that records
  every `write()` and replies to `query()` from a scripted dict.
- 1 hardware-marked test that hits the real device (auto-discover or
  override via `OPENSMU_DL3031A_RESOURCE`).
- Total tests: 248 hardware-free passing (was 212).

## [0.9.2] — bench validation harness + multi-profile matrix + LiPo support

### Added — `validation/` harness with reusable scenarios

New top-level `validation/` directory holds an end-to-end test harness
(`run_validation.py`) plus 11 saved scenarios captured against real
hardware (Arc Pro + Eastwood QR10x on COM7). Each scenario is a
self-describing JSON + CSV + PNG bundle that embeds a copy of the
input battery profile, so a saved scenario is fully reproducible
regardless of changes to the bundled profiles.

Two scenario kinds:

- **`static`** — step through a fixed list of QR resistances (100 kΩ
  down to 12 Ω, plus a recovery step), record one snapshot per step.
- **`dynamic`** — drive the QR through a time-varying IoT pattern
  (sleep / wake / TX burst), poll the emulator's state at 20 Hz.

CLI::

    python validation/run_validation.py --scenario static --all
    python validation/run_validation.py --scenario dynamic \
        --profile "CR2032-Energizer-(25)" --cycles 3

See `validation/README.md` for the full results table.

### Added — multi-profile validation matrix

Static sweep captured for all 8 bundled Otii battery profiles
(CR2032, CR123A, CR2, AA-Varta, AAA-Duracell, LiPo @ +20/+5/−10 °C).
Confirms the emulator faithfully reproduces chemistry-specific
behavior:

- CR2032 collapses at 12 Ω (1.85 V at 140 mA) — real coin cells do
  exactly this.
- CR123A sustains 258 mA at 12 Ω with 95 mV sag — designed for high
  pulse currents, reproduced.
- LiPo temperature sweep: same chemistry, ESR rises ~10× from +20 °C
  to −10 °C (12 Ω: V drops to 4.20 → 4.11 → 3.70 V respectively).

### Added — dynamic IoT-pattern scenarios

Captured for CR2032, CR123A, LiPo @ 20 °C. The CR2032/CR123A pair is
the most striking comparison — a 100 Ω, 400 ms "TX burst" sags a
CR2032 by 186 mV with multi-second recovery, vs. 10 mV with instant
recovery on a CR123A. This is the whole point of battery emulation
in the first place, and the emulator nails it.

### Fixed — emulator startup clamp to safety_max_voltage_V

`Emulator.start()` now clamps the initial OCV setpoint to
`safety_max_voltage_V` before sending `set_voltage(initial_v)`.
Previously, a profile whose fresh OCV exceeded what the SMU can
physically deliver (e.g. LiPo at 4.31 V on Arc Pro high range, capped
at ~4.2 V under load) would be silently rejected by the device with
error -101 and "reverted to last_good_value". The error queued and
poisoned every subsequent `read_value(MAIN_CURRENT)` call (each
returning 0.0 via the read-error swallow path), so the emulator's
loop would tick along reporting v_out = OCV and I = 0 forever.

Now the clamp happens before the rejection can occur, and the
emulator logs a warning. Tested with all three LiPo profiles.

### Added — `EmulatorConfig.voltage_range` with auto-detection

New `Optional[str]` field. Default `None` auto-selects `"low"` for
cells whose fresh OCV (× series multiplier) is ≤ 3.4 V, `"high"`
otherwise. This is what makes LiPo and other multi-cell stacks work
out-of-the-box — previously the emulator was hardcoded to
`set_range("low")` which caps voltage at ~3.5 V.

Override with `voltage_range="high"` or `"low"` for explicit control.

### Discovered — Arc Pro high-range output caps at ≈ 4.2 V under load

Bench-measured. `set_voltage(4.31)` is silently rejected with error
-101 / `last_good_value=4200000`. Documented in `validation/README.md`
under "Known limits" and `PROFILE_OVERRIDES` in the harness sets
LiPo's `safety_max_V` to 4.2 V to work with this constraint.

## [0.9.1] — emulator CV-mode fix + end-to-end validation

### Fixed — emulator CV-mode regulation

`Emulator.start()` now explicitly configures the SMU for
**constant-voltage** regulation before enabling output: `set_range("low")`
+ `set_current_limit(config.current_limit_A)` +
`set_current_limit_enabled(True)` + `set_power_regulation("voltage")`.

Without this, the device would inherit whatever mode the last test left
it in — typically `current` mode at 0 A target after a profiler run.
The Arc then prioritized current regulation (holding I=0 A) over the
voltage setpoint and refused to source current into the load, so the
emulator looked like an open circuit at the terminals.

### Added — `EmulatorConfig.current_limit_A`

New field, default 0.5 A. Sets the OC trip threshold during emulation —
the SMU acts as an ideal voltage source up to this current, then
protects itself. Tune up for higher-current DUTs (LiPo etc.).

### Fixed — `set_voltage` method name

The emulator was calling `self.smu.set_main_voltage(...)` which doesn't
exist on the real SMU — it's `set_voltage(...)`. Mock tests had a
matching `set_main_voltage` method so didn't catch this. Renamed
throughout (emulator + mock) so they match the real SMU surface.

### Verified — end-to-end emulator validation against the QR10x

Bench setup: Arc Pro emulating an Energizer CR2032 (fresh OCV 3.224 V,
ESR ≈ 9 Ω), QR10x as the programmable load. Stepped through
100 kΩ → 10 kΩ → 3 kΩ → 1 kΩ → 300 Ω → 100 Ω → 50 Ω → 25 Ω → 12 Ω,
then recovery back to 100 kΩ.

Highlights:

- Voltage sag tracks the ESR curve exactly: at 1 kΩ load the predicted
  9 Ω × 3.2 mA = 28.8 mV drop matched the measured 28 mV; at 100 Ω,
  predicted 279 mV vs measured 288 mV.
- The CR2032 emulator **collapses at 12 Ω load to 1.66 V** — below the
  profile's 1.8 V cutoff. That's exactly how a real CR2032 would
  behave at 160 mA draw.
- SoC integration verified: after the sweep SoC dropped to 99.925%,
  OCV came back at 3.146 V on the high-impedance recovery step — the
  profile's discharge curve correctly produced the new OCV.
- The ~14% R_err at low impedances tracks ~1.5 Ω of cable + plug
  series resistance in the measurement loop — physics, not an
  emulator bug.

### Tests

Existing test count unchanged (300 / 300 pass). The CV-mode fix is
covered by both mock-SMU tests and the real-hardware validation
above; no new unit tests beyond what already exists.

## [0.9.0] — bench subpackage + measurement stabilization

Two themes:

1. **Stabilize the profiler's measurement path** so step-response
   readings are accurate on real hardware (the previous implementation
   could return stale baseline samples).
2. **Open the door to bench instruments** via a new
   ``opensmu.bench`` subpackage, starting with the Eastwood Tech QR10x
   programmable resistance.

### Added — `SMU.read_window(channels, duration_s)`

New public primitive on the SMU: drains a window of inbound samples
and returns them grouped by channel. Like ``read_raw`` but parses
samples; like a brief recording but without the orchestration cost.

```python
samples = smu.read_window([Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT], 0.5)
# {Channel.MAIN_VOLTAGE: [3.2071, 3.2080, ...], Channel.MAIN_CURRENT: [-0.020, -0.020, ...]}
```

Surfaces queued device errors as ``SMUCommandError`` on the next SET,
matching ``read_value``'s semantics. Refuses when a recording is
active (the reader thread owns the byte stream then).

### Fixed — battery profiler

- **Sink-sign convention**: ``set_main_current(positive)`` is *source*
  (push into load), ``set_main_current(negative)`` is *sink* (draw
  from load). The profiler now negates user-supplied positive load
  magnitudes internally — the API stays clean.
- **Stable step-response measurement**: ``_measure_v_i`` now uses
  ``SMU.read_window`` and averages the most-recent half of the window.
  Previously it called ``read_value``, which could return the *first*
  sample seen in its drain window — often a stale pre-step baseline
  value. This made V_loaded read as ~0 V (the output-disabled noise
  floor) on real hardware.
- ``run()`` now explicitly sets the SMU into CC sink mode at start:
  ``set_range("low")`` + ``set_current_limit_enabled(True)`` +
  ``set_power_regulation("current")``.

### Verified — real AA pair

End-to-end profiler run against two AA alkalines in series:

- OCV: 3.187-3.191 V (per-cell 1.594-1.596 V — fresh)
- V_loaded at 20 mA sink: 3.11-3.16 V
- ESR pack: 2.7-3.8 Ω (mean 3.18 Ω), per-cell 1.4-1.9 Ω
  (includes banana plug + cable + Arc internal sense path)
- 10-cycle short profile completed in 15.2 s, used 10.6 µAh total

### Added — `opensmu.bench` subpackage + `QR10x` driver

New subpackage for non-Arc lab instruments. First driver:
**Eastwood Tech QR10x programmable resistance** — 1 Ω-8.4 MΩ depending
on model, ±0.02-0.05% accuracy, AT commands over USB-Serial (CH340
chip, 115200 8N1).

```python
from opensmu.bench import QR10x

with QR10x.open("COM7") as qr:
    print(qr.info())
    qr.set_safety_limit(12.0)
    qr.set_resistance(10_000)
    qr.actual_resistance()    # 10000.xxx (PV)
```

Public surface: ``info()``, ``set_resistance(ohms)`` /
``get_setpoint()`` / ``actual_resistance()``, ``set_safety_limit(ohms)``
/ ``get_safety_limit()``, ``get_temperature()``, ``incr(delta_ohm)`` /
``decr(delta_ohm)``. Exception hierarchy: ``QR10xError`` ->
``QR10xConnectionError`` / ``QR10xProtocolError`` /
``QR10xTimeoutError`` / ``QR10xValueError``.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- `qr10x_open(port="COM7", baudrate=115200)` / `qr10x_close()`
- `qr10x_info()`
- `qr10x_set_resistance(ohms)` / `qr10x_get_setpoint()` /
  `qr10x_actual_resistance()`
- `qr10x_set_safety_limit(ohms)` / `qr10x_get_safety_limit()`
- `qr10x_get_temperature()`
- `qr10x_incr(delta_ohm)` / `qr10x_decr(delta_ohm)`

Only one QR10x at a time; server holds the connection across tool
calls until `qr10x_close()`.

### Optional dependencies

- `pip install opensmu[bench]` — declares intent (QR10x driver uses
  pyserial which is already a base dep)
- `pip install opensmu[bench-visa]` — for future SCPI/VISA-based
  instruments (Rigol DL3031A etc.), pulls in pyvisa

### Tests

- 3 new hardware tests for `SMU.read_window` in `test_smu_stream.py`.
- 19 tests in `test_bench_qr10x.py` (13 hardware-free + 6 hardware on
  COM7). Override port via `OPENSMU_QR10X_PORT`.

Total tests: 278 → **300** (assuming all hardware tests run).

### Verified — QR10x driver on live device

First-try success against the connected QR101B-AM-1R on COM7:

- `info()`: serial 00000248, HW 5.1N, FW 5.967KS, TCR 25 ppm/°C
- `set_resistance(100)` → PV 100.038 Ω (0.04% deviation)
- `set_safety_limit(12.0)` accepted device-side
- `incr(900)` → 1000.027 Ω; `decr(900)` → 100.038 Ω
- Internal temp: 24.7 °C

## [0.8.0] — battery emulator (phase 4 of Battery Toolbox replacement) — FOUR PHASES DONE

Host-side control loop that drives the SMU as a battery with OCV + ESR
sag. Completes the Battery Toolbox replacement: opensmu now covers all
four features Qoitech's licensed product offers.

### Added — `opensmu.battery.emulator`

- **`EmulatorConfig`** dataclass — profile + initial SoC + series + parallel
  + temperature + soc_tracking + update_interval_s + safety_max_voltage_V
  + safety_max_used_mAh + soc_floor + current_read_timeout_s.
- **`EmulatorState`** dataclass — SoC, used capacity (mAh), OCV (V),
  ESR (Ω), output voltage (V), measured current (A), runtime, iteration,
  running flag, stop reason.
- **`Emulator(smu, config)`** with `start()`, `stop()`, `state()`,
  `run_for(seconds)` lifecycle. Background daemon thread runs the
  control loop at the configured update interval (default 100 Hz).
- Control loop algorithm: read mc, integrate I·dt → SoC, look up
  OCV(SoC) + ESR(SoC) from the profile, apply series/parallel
  multipliers, write `V = OCV − I·ESR` clamped to safety_max_voltage_V.
- Safety stops on `safety_max_used_mAh` and `soc_floor`. Output is
  always disabled in a `finally` block before `stop()` returns.

### Modes

- **`soc_tracking=True`** (default) — cell drains as DUT draws current.
- **`soc_tracking=False`** — SoC pinned at initial value. ESR sag still
  applies; cell doesn't appear to run down. Steady-state characterisation.

### Series / parallel

- `series=N` multiplies OCV and ESR.
- `parallel=N` divides ESR and multiplies effective capacity.

### Bandwidth

USB-driven host loop tops out around ~ms-scale latency per read+write
cycle. 100 Hz default update rate handles steady-state and slow
transients well (anything > ~10 ms response). Sub-ms ESR tracking
needs firmware-level regulation — out of reach for the host loop.
That's the one regime Otii's licensed device-side emulator covers
that this doesn't.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_emulator_start(profile_path, initial_soc=1.0, series=1, parallel=1, temperature=None, soc_tracking=True, safety_max_voltage_V=5.0, update_interval_s=0.01, safety_max_used_mAh=None, soc_floor=0.0)`** —
  start the emulator. Only one at a time. Refuses with structured
  guidance if one's already running.
- **`battery_emulator_state()`** — snapshot.
- **`battery_emulator_stop()`** — stop + disable output. Idempotent.

### Tests

14 new tests in `tests/test_battery_emulator.py` against a `_MockSMU`
that models a DUT drawing constant current:

- Input validation (4): rejects bad SoC, zero capacity profile, zero
  series/parallel, zero update interval.
- Run behaviour (10): seeds output at total OCV on start, disables
  output on stop, rejects double-start, idempotent stop, applies ESR
  sag (V = OCV − I·ESR), tracks SoC over time, `soc_tracking=False`
  pins SoC, safety_max_voltage clamps output, safety_max_used_mAh
  triggers stop, `run_for(seconds)` returns final state, `parallel`
  scales bank capacity, `state()` is thread-safe under load.

Total tests: 262 → **276 passing**.

### Hardware validation

Phase 4 ships green against a mock SMU. Real-DUT validation (connect a
load drawing variable current, verify voltage sag tracks ESR curve) is
a separate hardware task — tracked for when bench hardware is wired
up.

### What this completes

The Battery Toolbox replacement is now feature-complete on top of
opensmu's existing wire vocabulary:

| Otii Battery Toolbox feature | opensmu module |
|---|---|
| Battery Profile Manager | `opensmu.battery.profile` (v0.5.0) |
| Battery Life Calculator | `opensmu.battery.calculator` (v0.6.0) |
| Battery Profiler | `opensmu.battery.profiler` (v0.7.0) |
| Battery Emulator | `opensmu.battery.emulator` (v0.8.0) |

No vendor license required. No Otii server. Profile JSONs interchange
bit-for-bit between opensmu and Otii in both directions.

## [0.7.0] — battery profiler (phase 3 of Battery Toolbox replacement)

Hardware orchestration: drive a real battery through a configured
discharge profile, measure V/I per cycle, build a profile JSON.

### Added — `opensmu.battery.profiler`

- **`ProfilerConfig`** dataclass — discharge profile + battery
  metadata + temperature + measurement-window / relaxation /
  initial-settle timing + progress throttle + sample cap.
- **`ProfilerSample`** dataclass — one captured cycle:
  iteration, timestamp, OCV, loaded voltage, loaded current, ESR,
  cumulative capacity consumed (mAh).
- **`ProfilerResult`** dataclass — final BatteryProfile + samples +
  runtime + stop reason + aborted flag.
- **`Profiler(smu, config).run(progress=None)`** — synchronous
  orchestrated discharge. Alternates between high and low current
  steps, measures OCV after relaxation, computes ESR from step
  response. Stops on exit conditions (iteration limit, OCV cutoff,
  loaded-voltage cutoff), `Profiler.abort()` from another thread or
  the progress callback, or `sample_cap` reached.
- Profile auto-tags `software_version="opensmu/<ver>"` and queries the
  connected SMU for `firmware_version` + `device_id` to populate
  the `device` metadata block (round-trip-compatible with Otii's
  format).

### Constraints

- **Step duration**: minimum 50 ms. Profiles with extremely fast high
  pulses (e.g. Otii's CR2032 default of 2 ms) are rejected with a clear
  error.
- **Mode**: `"current"` only in v0.7.0. `"power"` and `"resistance"`
  modes raise on construction; tracked for v0.7.x.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_profiler_estimate_duration(capacity_mAh, high_current_A, high_time_s, low_current_A, low_time_s)`** —
  pre-run estimate. Returns cycle count, total seconds, human-readable
  duration. Use before kicking off a long run.
- **`battery_profiler_run(output_path, ..., capacity_mAh, nominal_voltage_V, ...)`** —
  full synchronous run; writes profile to disk. Documented warning that
  most MCP clients will time out before a real profile completes —
  Python `Profiler` API recommended for anything beyond a short demo.

### Tests

14 new tests in `tests/test_battery_profiler.py` using a `_MockSMU`
that models a linear-decay cell with configurable ESR:

- Input validation (4): rejects too-short steps, negative currents,
  unsupported modes, zero capacity.
- Run behaviour (10): produces correct sample count, stops on OCV
  cutoff, disables output at end, alternates high/low steps,
  computes non-negative ESR, progress callback fires + survives
  exceptions, abort halts after current cycle, built profile
  round-trips through JSON, runtime is recorded.

Total tests: 248 → **262 passing**.

### Hardware validation

Phase 3 ships green tests against a mock SMU. Real-battery validation
is a separate hardware task (needs an actual cell on the output
terminals) — tracked for a follow-up when bench hardware is wired up.

## [0.6.0] — battery life calculator (phase 2 of Battery Toolbox replacement)

Pure-Python duty-cycle life estimator. Drop-in replacement for Qoitech's
Battery Life Calculator using opensmu's open profile format from v0.5.0.

### Added — `opensmu.battery.calculator`

- **`DutyCycle`** dataclass — active/sleep load pattern with computed
  `cycle_time_s`, `cycle_charge_C`, `average_current_A` properties.
- **`LifeEstimate`** dataclass — runtime + iterations + capacity
  consumed + self-discharge loss + safety margin loss + final voltage
  (profile method only) + method + stop reason.
- **`estimate_life_constant_current(capacity_mAh, duty_cycle, ...)`** —
  analytic estimator. Treats the cell as a flat-voltage reservoir.
  Optional self-discharge (% per month) and safety margin (%). Returns
  infinite runtime for zero drain.
- **`estimate_life_from_profile(profile, duty_cycle, temperature=None, ...)`** —
  iterative estimator against a `BatteryProfile`. Looks up OCV at each
  cycle's used-capacity point. Stops at cutoff voltage (defaults to
  profile's own) or usable-capacity exhaustion. Matches Otii's Battery
  Life Calculator semantics.
- **`duty_cycle_from_recording(rec, active_window, sleep_window, channel="mc")`** —
  extract a DutyCycle from a captured Recording by averaging main
  current over user-selected time windows. Otii's "Get from selection"
  workflow.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_life_estimate(capacity_mAh, active_current_A, active_time_s, sleep_current_A, sleep_time_s, ...)`** —
  constant-current estimator
- **`battery_life_estimate_from_profile(profile_path, ...)`** —
  profile-based estimator
- **`battery_life_from_recording(recording_path, active_window_start_s, ..., profile_path=None, capacity_mAh=None, ...)`** —
  end-to-end: load saved capture, extract windows, estimate

### Verified

CR2032-Energizer with a typical IoT load (20 mA / 100 ms / 60 s cycle ≈
38 µA average): ~250 days from the constant-current estimator; ~208
days with 1%/month self-discharge + 10% safety margin. Real numbers,
match physical intuition.

### Tests

18 new tests in `tests/test_battery_calculator.py`:
- DutyCycle properties (cycle time, cycle charge, average current)
  + input validation
- Constant-current estimator: simple math, safety margin scaling,
  self-discharge, zero-drain infinity, input validation
- Profile-based estimator: flat-profile equivalence to CC, voltage
  cutoff, safety margin scaling, real CR2032 sanity check
- DutyCycle extraction from synthetic Recording + edge cases
- Humanize formatter (seconds → "5 days 3 hours …")

Total tests: 230 → **248 passing**.

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

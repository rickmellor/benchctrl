# Known limitations

Aggregated list of hardware, firmware, and harness limits we've
bumped into. Kept here rather than buried under per-version
CHANGELOG entries so future contributors find them in one place.
Each entry: what fails, where it surfaces, what the workaround is,
and where (if anywhere) it's documented in code.

## Hardware

### H-1. Arc Pro high-range output caps at ≈ 4.2 V under load
Bench-measured 2026-05-29. `set_voltage(>4.2)` in high range with
the current limit armed silently queues an asynchronous SCPI -101
("revert to last_good_value=4200000") and poisons subsequent
`read_value` calls — the next `_send_set` raises stale.

Affects: LiPo emulation. Profiles whose fresh OCV exceeds 4.2 V
(Renata HPST: 4.31 V at full SoC) need `safety_max_voltage_V=4.2`
or lower.

Workaround: `Emulator.start()` clamps the seed voltage to
`safety_max_voltage_V` before sending (v0.9.2). The validation
harness sets per-profile overrides.

Code reference: `src/benchctrl/battery/emulator.py` — see the
"Seed the output" block in `start()`.

### H-2. DL3031A CR mode regulation breaks down below ~1 mA target
The DL3031A is an active electronic load — at very small target
currents the closed loop can't maintain the resistance setpoint
and the load reads as effectively open.

Affects: any QR10x-replacement scenario where the load is asked to
emulate light currents (10 kΩ at 3.2 V = 0.32 mA → DL3031A reads 0).

Workaround: use QR10x for light loads, DL3031A for active /
TX-burst loads. The validation harness's adapter does this
automatically by translating R > `r_max` to `set_input(False)`.

Code reference: `scenarios/run.py` `_DL3031AAdapter.set_resistance`.

### H-3. DL3031A measurement integration is fixed at 10 PLC (~200 ms)
Per the programming guide, `:MEASure:TIME?` returns 200 ms and is
not user-settable. So:

- `:MEASure:*` calls all take ~200 ms each (they trigger fresh
  integration).
- `:FETCh:*` calls return immediately but four sequential `:FETCh:`
  reads see the *same* underlying register sample. Multi-channel
  snapshots that look simultaneous are one-shot-aliased.

Workaround: use `fetch_*` in fast polling loops where 200 ms
latency is the dominant concern; understand that the four values in
`fetch_all` are not strictly simultaneous but read from the same
~200 ms integration window. For genuinely synchronous high-rate
V/I capture, use `SMU.record()` on the Arc Pro.

Code reference: `src/benchctrl/bench/rigol_dl3031a.py` — `measure_*`
and `fetch_*` docstrings.

### H-4. DL3031A input toggle takes ~700 ms to settle
Toggling `:SOUR:INP:STAT` cycles the regulation loop's internal
settling. After `set_input(True)` the current spike lands ~700 ms
later, not immediately.

Affects: any dynamic scenario where the harness toggles input
between phases.

Workaround: keep input ON and change setpoints instead. The hires
dynamic pattern uses 300 ms TX phases (long enough to catch the
spike inside the labeled window). The dynamic-list scenario keeps
input on for the whole list and changes only the LIST setpoints.

Code reference: `scenarios/README.md` § "DL3031A switching latency".

## Driver / firmware interactions

### F-1. DL3031A `:SOUR:LIST:STEP 4` fires no steps (firmware bug)
Bench-verified on firmware 00.01.05.00.01:
``:SOUR:LIST:STEP 4`` specifically — irrespective of how many
steps are programmed — does not fire any steps. ``STEP 2`` plays 2
steps, ``STEP 3`` plays 3, ``STEP 5`` plays 5. The semantics is
"play N total steps starting from index 0"; the manual's
application instance suggesting STEP 2 = 3 steps is incorrect (the
comment in the manual reads "Sets the total steps to be 3" but the
device plays only 2). The earlier v0.9.6 driver compounded this by
sending ``STEP N-1``, masking the underlying STEP=4 bug.

The "~3 s onset slip" earlier reported was actually the symptom of
the v0.9.6 driver sending an incorrect STEP value combined with the
STEP=4 firmware bug producing intermittent behavior. With the v0.9.7
fix (send STEP N directly, reject 4-step programs at the driver
level), LIST programs of any other size play correctly.

Affects: 4-step LIST programs only.

Workaround: use 3 or 5 steps with appropriate ``count`` for the
same total play time. The driver rejects 4-step programs at
``list_set_step_count(4)`` / ``program_list(steps=[...4...])`` with
a clear error pointing to the workaround.

Code reference: `src/benchctrl/bench/rigol_dl3031a.py`
`list_set_step_count` and `program_list`.

### F-2. DL3031A LIST in Arc Pro high range fires partially / unpredictably
Bench-verified on firmware 00.01.05.00.01 (v0.9.7 investigation):
with the Arc Pro in high range (LiPo profiles, 4.2 V), the LIST
playback under dynamic-list captures one TX burst at a
non-deterministic time, then stops — instead of the expected
`cycles` × `n_steps` repeats. The `:SOUR:LIST:STEP` value reads
back correct, `:SOUR:FUNC:MODE` reports LIST, `*OPC?` settles
cleanly, but the firmware only executes a fraction of the
programmed sequence.

The same SCPI sequence in low range plays the full LIST correctly.

Cause: appears to be related to the high-V range plus the
device's stuck-in-FUNC:MODE behavior (F-3) — *RST from a stuck
state doesn't return to FIX so LIST programming starts from a bad
state.

Affects: LiPo @ +20/+5/−10 °C dynamic-list. Static sweeps, hires
capture, and CR2032/CR123A dynamic-list work fine.

Workaround: LiPo dynamic-list scenarios are excluded from the
shipped scenario set. Use `--pattern hires` (which uses host-side
load control, not LIST) for LiPo transient validation. A
power-cycle of the DL3031A before each LiPo run sometimes
restores correct behavior — sometimes not.

Code reference: `scenarios/README.md` § "Headline results" notes
the LiPo "did not capture" cell.

### F-3. DL3031A FUNC:MODE only escapable by power-cycle once stuck
Bench-verified v0.9.7: the DL3031A's `:SOUR:FUNC:MODE FIXed` is
silently rejected once the device has entered LIST / WAV /
BATTery / OCP / OPP modes. `*RST`, `*WAI`, `*OPC?`, `*CLS`,
toggling `:SOUR:INP`, and even cycling through other modes
(BATT → FIX, OCP → FIX) all fail to bring the device back to
FIX. Only **power-cycling the DL3031A** restores FIX mode.

**v1.0 follow-up**: bench-discovered while building Phase A of
the DP2031 closed-loop tests — `*RST` on its own can leave the
device in WAV mode too (the post-reset default seems to vary by
firmware / prior state). When stuck in WAV:
- CC-mode setpoints are silently ignored (input only sinks the
  ~10 mA bias from the input MOSFET);
- CR mode partially engages (presents a real impedance) but the
  load's `:MEASure:CURRent:DC?` returns 0 regardless of actual
  current flow;
- the FIXed-write workaround above remains the only fix that
  doesn't require a power cycle.

Affects: any workflow that programs LIST or transient mode and
then expects the next test to start in a clean state, AND any
DP2031 closed-loop test that relies on the load actually drawing
its configured CC setpoint.

Workaround:
1. The runner's `finally` block tries `set_function_mode("FIXed")`
   and reads back via `get_function_mode()`. If the device is
   stuck, it logs a clear warning: *"DL3031A stuck in {MODE} mode
   after teardown (expected FIX) — power-cycle before reuse"*.
2. The operator power-cycles the DL3031A between tests where
   FIX-mode start is required.
3. DP2031 Phase A closed-loop tests were deferred until this
   quirk is better understood — Phase A verifies the PSU side
   only.

Code reference: `scenarios/run.py` — see the
`run_dynamic_list` `finally` block. `tests/test_bench_rigol_dp2031.py`
— see the deferred-closed-loop comment block.

### F-3.5. DP2031 bench-discovered quirks (firmware 01.00.01.00.16)

Found while bringing up the DP2031 driver in Phase A–D against a
DP2A243500269 unit. Documented here so future drivers / phases
don't waste time rediscovering them. Reports prepared for the
vendor for the most severe items live in the `bugs/` directory.

- **`:OUTPut:PAIR PARallel` write-then-query returns stale `OFF`
  for ≥ 1 s** before the mode transition completes. Originally
  read this as "silent no-op" — Phase C bench data was wrong.
  PARallel does engage (verified Phase D: querying ≥ 2 s after
  the write returns `PARALLEL` correctly). SERies transitions
  faster (~300 ms). Driver: pass-through, but callers should
  wait ≥ 2 s before verifying via `get_channel_pair()`.

- **`:OUTPut:PAIR` state survives `*RST`** — `*RST` does not
  restore PAIR to OFF. Sessions that ran prior SERies / PARallel
  tests will start with the device still in that topology.
  Significant safety implication: a fresh session that assumes
  independent channels can inadvertently drive CH1+CH2 paired.
  Driver workaround: the hw-test fixture explicitly
  `set_channel_pair("OFF")` after `reset()` and waits 2 s.

- **OVP latch settles in ~150–250 ms after the trip condition.**
  Querying `:OUTPut:OVP:ALAR?` immediately after `:OUTPut:STATe ON`
  with V > OVP returns 0; wait at least 300 ms before checking.
  Output state flips to OFF a bit later (the latch fires the
  trip; the output drop is downstream).

- **`:OUTPut:OVP:CLEar` clears the latch but does NOT re-enable
  the output**, despite some manual wording suggesting it does.
  The `:SOURce<n>:VOLTage:PROTection:CLEar` form may behave
  differently; the driver picked the OUTPut form for predictable
  semantics.

- **`:OUTPut:TRACk ON` writes the tracking state register
  (`TRACk?` = 1, `:SYSTem:TMODe?` = `SYNCHRONOUS`) but the
  expected setpoint-mirroring effect** (set CH1 voltage → CH2
  voltage follows) **does NOT engage with both outputs off and
  no load.** The state round-trip works perfectly, so the driver
  surfaces it; the analog mirroring behaviour remains
  bench-unverified. Likely requires outputs enabled + a load.

- **`:OUTPut:OCP:DELay?` returns a string with `ms` suffix**
  (e.g. `"200ms"`) — parsed by `_parse_delay_ms` helper.

- **`:SYSTem:LANGuage:TYPE?` returns the long form** (e.g.
  `"ENGLISH"`) even though the input takes the short form (`EN`).
  Driver accepts both on input; query returns whatever the
  device replies.

- **`:SYSTem:POWEron?`** same — returns `"DEFAULT"` (long form).

- **`:SOURce<n>:VOLTage? MAX` and `:CURRent? MAX` return ~5%
  over the nominal envelope** (e.g. 33.6 V on CH1, nominal 32 V).
  The driver's static `_CHANNEL_LIMITS` matches nominal; the
  device accepts setpoints up to its reported MAX. Use
  `voltage_bounds()` / `current_bounds()` for the device's real
  limits.

- **`*OPT?` returns `"NONE"`** (literal string) when no options
  are installed, not an empty string. `installed_options()` maps
  both to `[]`.

- **Boolean queries return with a trailing space** on some
  paths (e.g. `:OUTPut:TRACk?` returns `"1 "`). `query_int` /
  `query_float` strip whitespace before parsing.

- **`:ANALyzer:COMMon:MEASure:TYPE` writes trigger
  `VI_ERROR_SYSTEM_ERROR` over USB-TMC** (Phase D discovery).
  The wire-form is per-spec but the device-side handler hangs;
  this leaves the VISA session in a half-broken state that may
  cause subsequent writes to fail. The SDK method
  `set_analyzer_common_objects` is kept for completeness but is
  effectively unusable on this firmware. Front-panel UI for the
  same feature works correctly. Reproduction: `bugs/` directory.

- **`:SYSTem:PRINt?` screenshot reply is wrapped in IEEE 488.2
  arbitrary-block format** (`#NX...X<bmp>`), even though the
  documented payload type is "binary bitmap". The `screenshot_bytes()`
  driver method strips the header via `_strip_block_header_bytes`
  before returning raw BMP. Read takes 5+ seconds; driver temporarily
  bumps the VISA timeout to 10 s.

- **`:TIMEr:CYCLEs?` returns `"N, 5"` with a space after the comma**
  (or `"I"` for infinite). Driver `_parse` tolerates both
  `"N,5"` and `"N, 5"` variants.

- **`:TIMEr:GROUP:PARAmeter?` returns IEEE 488.2 block format with
  trailing NUL byte** inside the declared payload window. Driver's
  `_query_block_payload` tolerates the NUL.

- **`:TRIGger:IN:SOURce <line>,NONE` writes are rejected with
  `-141,"Invalid character data"`** even though the query of the
  same field returns `NONE` when nothing is set. `NONE` is a
  read-only sentinel. To "clear" a trigger source, disable the line
  via `:TRIGger:IN:ENABle <line>,OFF`.

### F-4. Manual misreads compensated by the driver

Rigol's DL3000 programming guide is inconsistent in a few places.
We follow the firmware's actual behavior (see F-1 for the LIST:STEP
semantics, established v0.9.7):

- `:SOUR:LIST:STEP N` plays **N total steps** (manual incorrectly
  suggests N+1 in an application instance comment). Plus the
  STEP=4 firmware bug — see F-1.
- `:SOUR:LIST:SLEW <step>,<value>` is per-step, not global. The
  driver applies the same slew to every step in `program_list`.
- `:SOUR:LIST:END` accepts `LAST|OFF`, not `NORMal|LAST` as the
  manual suggests in passing.
- `:FETCh:DISChargingTime?` returns `H:MM:SS` (e.g. `"0:0:15"`),
  not a "real number" as the manual documents. Driver parses both
  H:M:S and plain float as a fallback.
- `:SOUR:CURR:TRAN:FREQuency` takes **Hz**, not kHz as the manual
  documents (bench-verified by period queries returning 1/freq).

Code reference: `src/benchctrl/bench/rigol_dl3031a.py` —
`list_set_step_count`, `list_set_slew`, `_LIST_END_VALUES`,
`_fetch_discharging_time_s`, `transient_set_frequency`.

## Harness

### A-1. Emulator + `SMU.record()` deadlock
`Emulator._loop` writes `set_voltage` at 100 Hz while the recording
reader thread consumes the transport at ~4 kHz. The combination
deadlocks consistently within ~100 ms — transport-level
contention.

Affects: hires and dynamic-list runners.

Workaround: settle the emulator to fresh OCV, capture that
voltage, stop the emulator, pin the SMU at that V manually, then
open the recording. SoC tracking is off during the recording
window (the runner records this fact in the scenario JSON's
`recording.pinned_voltage_V` field).

For 10 s × 30 mA peak, SoC drift is 0.08 mAh — negligible vs CR2032's
230 mAh capacity. For longer captures, consider running the
emulator and recording in alternating windows.

Code reference: `scenarios/run.py` —
`run_dynamic_hires` and `run_dynamic_list`.

### A-2. Validation phase-summary aggregates include settling
The standard dynamic scenario's per-phase summary reports mean V/I
across *all* samples in a phase, including settling at the phase
boundaries. The DL3031A's slow input-toggle (H-4) means the first
~700 ms of a labeled TX phase may be settling rather than the TX
level.

Workaround: use the raw CSV / JSON samples for accurate per-phase
analysis, or use `--scenario dynamic-list` which uses firmware
timing and avoids the toggle.

Code reference: `scenarios/README.md` § "Notes / known limits".

### A-3. `start_recording()` does not flush stale inbound samples
The Arc streams baseline samples (~6 Hz) continuously from the moment
the port is opened. `start_recording()` creates the `Recording` and
starts the reader thread without calling `reset_input_buffer()`, so any
baseline samples already sitting in the OS serial buffer are consumed
and appended as if they belonged to the recording.

Consequence: the first few samples of a recording can predate it — and
therefore predate whatever setup (`set_voltage`, `set_output`) happened
just before. On a 1 s capture this pulls the mean off by ~0.5 %.

Not fixed because a blind flush would also discard legitimate in-flight
samples, and the correct boundary is ambiguous without a device-side
timestamp. Workaround: discard the leading samples, or sleep briefly
after the final setup command so the stale window is dominated by
correct values.

Reproduced deterministically by
`tests/test_sim_loopback.py::test_recording_captures_a_known_waveform`.

Code reference: `src/benchctrl/drivers/otii_arc/device.py` —
`start_recording`.

### A-4. Live recording reads lag by up to 0.5 s
`Transport.read_chunk()` calls `serial.read(8192)`, which blocks until
either 8192 bytes arrive or pyserial's 0.5 s timeout expires. At normal
sample rates the byte count is never reached, so the recording reader
thread delivers samples in ~0.5 s batches.

Consequence: `rec.statistics()` and `rec.data()` on a *running* recording
can report zero samples for the first half-second, and progress reporting
is quantised to that interval. After `stop_recording()` everything is
present — the samples are not lost, only late.

This bounds live progress reporting in remote mode: a `rec_progress`
event stream cannot be more granular than ~0.5 s without changing the
read strategy to `read(max(1, in_waiting))`, which trades the efficient
blocking read for a busier loop. Not changed here: it alters timing on
the hot path for every recording, and that is not a change to make
without hardware to verify against.

Reproduced by `tests/test_remote_protocol.py` — the recording tests sleep
past this window deliberately.

Code reference: `src/benchctrl/drivers/otii_arc/transport.py` —
`read_chunk`, and `device.py` — `_reader_loop`.

## Network (remote mode)

### N-1. A software deadman cannot guarantee an output goes off
The agent drives armed devices to their safe state when contact is lost,
escalating from a priority command to a transport reset. Neither is a
guarantee. If the driver thread is wedged inside a blocking read the
governor cannot get a command out, and closing the serial port does not
command an Arc's output off — it holds its last commanded state.

For unattended overnight runs the only real guarantee is a hardware
interlock: a relay on the DUT rail driven from a GPIO, or the Arc's own GPO
(`device.py` `set_gpo`). Treat the governor as damage limitation.

Code reference: `src/benchctrl/agent/safety.py` — `SafetyGovernor.trip`.

### N-2. No confidentiality on the wire
The HMAC handshake authenticates; it does not encrypt. Everything after it
is plaintext on the LAN. Anyone who can sniff the link reads setpoints and
measurements; anyone who can inject packets can interfere.

Workaround: `ssh -L 9737:localhost:9737 arduino@<board>.local`.

### N-3. Recordings are capped at ~5 minutes in remote mode
`ChannelBuffer.values` is a Python `list[float]` at roughly 40 bytes per
sample. At 9 ksps that is ~110 MB for five minutes and ~1.3 GB for an hour,
on a board with 3.6 GB total and an LLM potentially holding 1.1 GB.

The agent enforces `max_recording_s` (default 300 s). Longer captures go
through the run engine, which chunks to disk. Note that
`applications/sensor_profiler/capture.py` defaults to 60-minute chunks
(`capture.py:354`) — that app must run in local mode or have its default
lowered.

A future `array('f')` buffer would cut this 10x, but it touches statistics,
every writer, crop/downsample, and every test — a separate change.

### N-4. Two clients cannot both drive one device
One session holds the writer claim per device; others are read-only
observers and get a `PolicyError` on any mutator. This preserves the
single-client serialization `benchctrl.mcp` already assumes.

### N-5. Rigol drivers need three extra wheels on the board
`pyvisa`, `pyvisa-py` and `pyusb` are pure-Python but are not part of the
base install. Without them the two Rigol drivers import fine (the pyvisa
import is lazy, inside `open()`) but cannot open a device. Per-device mode
resolution is the workaround: run the Rigols local and the Arc remote in the
same MCP process.

## What's not in this list

Things we **don't** consider limits — they're just facts:

- The Arc Pro's baseline streaming rate is ~6 Hz; high-rate
  streaming (~4 kHz on MAIN_CURRENT) is enabled by `SMU.record()`.
  This is documented in `PROGRESS.md` as a discovered behavior,
  not a limitation.
- The QR10x's relay-switching delay (30–95 ms) is documented as a
  hardware spec, not a limit.
- The DL3031A's 60 A / 350 W rating is a hardware spec.

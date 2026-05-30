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

Code reference: `src/opensmu/battery/emulator.py` — see the
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

Code reference: `validation/run_validation.py` `_DL3031AAdapter.set_resistance`.

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

Code reference: `src/opensmu/bench/rigol_dl3031a.py` — `measure_*`
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

Code reference: `validation/README.md` § "DL3031A switching latency".

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

Code reference: `src/opensmu/bench/rigol_dl3031a.py`
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

Code reference: `validation/README.md` § "Headline results" notes
the LiPo "did not capture" cell.

### F-3. DL3031A FUNC:MODE only escapable by power-cycle once stuck
Bench-verified v0.9.7: the DL3031A's `:SOUR:FUNC:MODE FIXed` is
silently rejected once the device has entered LIST / WAV /
BATTery / OCP / OPP modes. `*RST`, `*WAI`, `*OPC?`, `*CLS`,
toggling `:SOUR:INP`, and even cycling through other modes
(BATT → FIX, OCP → FIX) all fail to bring the device back to
FIX. Only **power-cycling the DL3031A** restores FIX mode.

Affects: any workflow that programs LIST or transient mode and
then expects the next test to start in a clean state.

Workaround:
1. The runner's `finally` block tries `set_function_mode("FIXed")`
   and reads back via `get_function_mode()`. If the device is
   stuck, it logs a clear warning: *"DL3031A stuck in {MODE} mode
   after teardown (expected FIX) — power-cycle before reuse"*.
2. The operator power-cycles the DL3031A between tests where
   FIX-mode start is required.

Code reference: `validation/run_validation.py` — see the
`run_dynamic_list` `finally` block.

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

Code reference: `src/opensmu/bench/rigol_dl3031a.py` —
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

Code reference: `validation/run_validation.py` —
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

Code reference: `validation/README.md` § "Notes / known limits".

## What's not in this list

Things we **don't** consider limits — they're just facts:

- The Arc Pro's baseline streaming rate is ~6 Hz; high-rate
  streaming (~4 kHz on MAIN_CURRENT) is enabled by `SMU.record()`.
  This is documented in `PROGRESS.md` as a discovered behavior,
  not a limitation.
- The QR10x's relay-switching delay (30–95 ms) is documented as a
  hardware spec, not a limit.
- The DL3031A's 60 A / 350 W rating is a hardware spec.

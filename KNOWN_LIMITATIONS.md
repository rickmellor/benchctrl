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

### H-5. SDM4065A 2-wire resistance carries ~79 mΩ of lead error on our leads
Per the SDM4000A datasheet note [6], the 2-wire (`RESistance`)
function's accuracy spec assumes lead and contact resistance has been
nulled out; unnulled it contributes up to **0.2 Ω** in series with the
DUT.

**Bench-measured on our bench, 2026-08-19: 78.9 mΩ.** The same 100 Ω
QR10x setpoint read both ways on the same leads — 2-wire 100.12209 Ω,
4-wire 100.04321 Ω, against the QR10x's own PV of 100.03800 Ω. So the
4-wire reading lands 5.2 mΩ from the QR10x's measurement while the
2-wire reading is 84 mΩ away: 4-wire is doing exactly what it is for.

79 mΩ is well inside the datasheet's 0.2 Ω, but it is still **2x the
38 mΩ offset** the cross-validation is trying to resolve, so the
conclusion is unchanged: an unnulled 2-wire reading cannot see that
offset. At 100 Ω this is a 0.08 % error, an order of magnitude larger
than the meter's own accuracy contribution.

Affects: any 2-wire reading below a few hundred ohms.

Workaround: use `measure_resistance_4wire()` with sense leads, or
short the leads and call `null_now()` before measuring. For a
4½-digit reading of a 100 Ω part, 2-wire without a null is not a
measurement. Both paths are now hardware-verified.

The 0.2 Ω figure is retained as the budget bound in
`TWO_WIRE_LEAD_OHM`, deliberately: 78.9 mΩ is a property of *these*
cables and contacts, not of the meter, and tightening the tolerance to
it would fail the suite for anyone with longer leads without indicating
any driver defect.

Note what the cross-validation *cannot* settle: the QR10x's ±0.05%
spec dominates the meter's, so two-instrument agreement at 100 Ω is
budgeted at ~0.07 Ω — wider than the 38 mΩ offset, and comparable to
the 79 mΩ of lead error itself. Two instruments agreeing therefore
proves the absence of *gross* errors (units, scaling, swapped
2-/4-wire, a null of the wrong sign) and nothing finer.

The lead-resistance measurement escapes that limit because it
differences two readings from the *same* meter on the *same* leads, so
the QR10x term cancels entirely and 5 mΩ of resolution is meaningful.
That is why 78.9 mΩ is a real number where a 0.07 Ω agreement is only
a bound.

Code reference:
`src/benchctrl/drivers/siglent_sdm4065a/driver.py` —
`measure_resistance` docstring; `docs/drivers.md` § "2-wire vs 4-wire";
`tests/test_cross_validate_sdm4065a_qr10x.py`.

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

### F-5. SDM4065A command semantics that make the obvious call wrong
From the SDM4000A remote manual, verified against the extracted text
section by section rather than taken on trust, then checked against the
instrument — which is how the last two turned out to be firmware or
manual defects rather than merely surprising semantics. All four are
handled by the driver; they are recorded here because each one silently
produces a *plausible* wrong number, which is the hardest class of
defect to notice on a meter. See also F-7 (error queue) and F-8 (the
undefined-header wedge), which are failures of the instrument rather
than traps in its command set.

- **`MEASure:<fn>?` is `CONFigure` + `READ?` in one command**, and
  `CONFigure` resets that function's NPLC, null state, null value and
  range to defaults. So `null_now()` followed by
  `measure_resistance()` silently discards the null — the API's most
  natural call sequence is the broken one. The driver samples the
  offset with `READ?`, and `read_nulled()` raises rather than quietly
  returning an un-nulled number.
- **Enabling `NULL:STATe` arms `NULL:VALue:AUTO`** (§7.4.2), so the
  instrument overwrites the offset with its own next reading. The state
  must therefore go on *first* and the value second; the natural
  value-then-state order nulls by the wrong number. §7.4.3 says writing
  a value disarms AUTO — bench-measured on firmware 0.0.0.20, **it does
  not**: after `STATe ON` then `VALue`, `NULL:VALue:AUTO?` still
  answered `1`. So the null silently becomes a no-op that leaves every
  result *looking* nulled. `null_now()` disarms AUTO explicitly rather
  than relying on the documented side effect. Reported to Siglent;
  `docs/vendor-issues/SDM4065A-firmware-bug-report-1-null-value-auto.md`.
- **The documented default resistance range is wrong**, and the range
  readback is ambiguous. §7.4.5 says "default 2 kΩ"; bench-measured on
  firmware 0.0.0.20, `*RST` and a bare `CONFigure:RESistance` both
  leave **autoranging on with `RANGe?` reporting 200 Ω**. Worse,
  `RANGe?` returns the same number whether the range is pinned or
  merely currently selected by autorange, so `RANGe:AUTO?` must be read
  alongside it to know whether the range is stable. Accuracy work must
  pin the range with an explicit numeric argument —
  `CONFigure:RESistance DEF` selects 2 kΩ but leaves autoranging *on*,
  where `RESistance:RANGe DEF` correctly turns it off. Reported to
  Siglent; `docs/vendor-issues/SDM4065A-firmware-bug-report-4-range-defaults.md`.
- **One manual covers the SDM4045A / 4055A / 4065A**, and the columns
  differ: the 4065A tops out at 1 MΩ where the 4055A has 2 MΩ, and
  accepts six NPLC values where the 4055A accepts three. Resistance
  autozero is 4065A-only and defaults **off** (§7.4.7) — though the
  mnemonic §7.4.7 gives for it does not exist on the instrument, see
  F-8. A constant taken from the wrong column yields a driver that
  works and reports plausible numbers; both lists are validated against
  the 4065A column and the rejection message names the sibling model.

Also worth knowing, though not a limitation: Siglent documents SCPI
headers **without** the leading root colon (`SYSTem:ERRor?` where
Rigol writes `:SYSTem:ERRor?`). Both work on the instrument. Our
simulator accepts both deliberately — matching only the prefixed form
would send error queries to the generic register model, which answers
`0`, i.e. a permanently clean error queue.

Code reference:
`src/benchctrl/drivers/siglent_sdm4065a/driver.py` — `null_now`,
`read_nulled`, `_validate_range`, `set_nplc`, `set_autozero`;
`src/benchctrl/sim/sdm4065a.py` — `_install_rootless_aliases`.

### F-6. USB-TMC instruments are *invisible*, not unopenable, without a udev rule
Bench-verified on the Uno Q board. Its kernel has no `usbtmc` module,
so pyvisa-py drives USB-TMC instruments over libusb, which needs write
access to `/dev/bus/usb/BBB/DDD` — created `root:root 0664`.

The surprise is the symptom. A permission problem would normally raise;
here the device silently vanishes:

```
discover() -> []
SDM4065AConnectionError: no SDM4065A found (0xF4EC:0x1220). VISA
reported: ['ASRL/dev/ttyACM0::INSTR', ...]
```

pyvisa-py must read the USB **string descriptors** to build a resource
name, and that read is a control transfer needing write access. It
fails, so the device never appears in `list_resources()` at all. The
error message is then indistinguishable from an unplugged instrument.

Confirmed as permissions, not cabling: `usb.core.find()` locates
`f4ec:1220` and reads its bus/address, but `os.access(node, os.W_OK)`
is `False` where `os.R_OK` is `True`, and reading `device.manufacturer`
raises `ValueError: The device has no langid (permission issue, ...)`.

Affects: SDM4065A and both Rigols, on any board without a kernel
`usbtmc` module.

Workaround: install `deploy/udev/61-benchctrl-usbtmc.rules`, then
`udevadm control --reload-rules && udevadm trigger --action=add` — the
trigger is required, since udev sets permissions at event time and an
already-plugged device is not re-evaluated by a reload alone.

Not fixable in the driver: no amount of Python can widen a file mode
it does not own. The driver's `open()` error message names the
pyusb/libusb requirement for this reason.

Code reference:
`deploy/udev/61-benchctrl-usbtmc.rules`; `deploy/README.md` §
"USB-TMC instruments on a kernel without `usbtmc`";
`src/benchctrl/drivers/siglent_sdm4065a/driver.py` — `open`, `discover`.

### F-7. SDM4065A `*CLS` does not clear the error queue, which can latch silent
Bench-measured on serial SDM46A0CA00021, firmware 0.0.0.20.
IEEE 488.2 § 10.3 requires `*CLS` to empty the error queue. On this
unit it does not: queued entries survive `*CLS`, survive `*RST`, and
one survived closing and reopening the VISA session. Only *reading*
`SYSTem:ERRor?` removes an entry.

Two consequences, in escalating order:

1. Left undrained the queue fills and answers `-350 "Queue overflow"`
   to everything, so an error check attributes a stale failure to
   whichever command happened to be running.
2. After an overflow the queue can **latch permanently silent** —
   measured, it answered `0,"No Error"` to a deliberately bogus header
   that `*ESR?` correctly flagged as a Command Error. The only tell is
   a change in the reply's capitalisation (`No error` → `No Error`), so
   nothing a caller can reasonably branch on. A front-panel power cycle
   restores it.

Affects: any error check on this meter. `last_error() is None` does
**not** prove the previous command was accepted.

Workaround, and what the driver does: read `*ESR?` bit 5 instead.
`command_error()` is the authoritative "was that rejected?" — it stayed
correct throughout, including while the queue was latched silent, and
it is read-and-clear so it does not accumulate. `clear_status()` drains
the queue by reading rather than trusting `*CLS`. The cost is detail:
`*ESR?` reports *that* a command was rejected, not which error it was,
so anything needing the code (`-113` vs `-224`) still depends on the
queue.

The bench unit is currently in the latched-silent state.
`test_hw_error_reporting_is_reachable` warns rather than fails on it,
deliberately: a silent queue is the instrument misbehaving, and failing
there would mask the plumbing bug the test exists to catch.

Reported to Siglent;
`docs/vendor-issues/SDM4065A-firmware-bug-report-3-error-queue.md`.

Code reference:
`src/benchctrl/drivers/siglent_sdm4065a/driver.py` —
`standard_event_status`, `command_error`, `last_error`, `clear_status`;
`src/benchctrl/sim/sdm4065a.py` — `_on_cls`, `_on_error_query`,
`error_queue_silent`.

### F-8. Querying an undefined header on the SDM4065A wedges it until power cycle
Bench-measured, and the most serious defect found on this meter.
*Writing* an undefined header is harmless — it is rejected with `-113`
and the meter carries on. *Querying* one produces **no response at
all**: the read blocks to timeout, and the aborted USB-TMC transfer
leaves the bulk endpoints stranded. Every subsequent operation times
out, including `*IDN?`.

Recovery is a front-panel power cycle. Nothing softer works:
USBTMC `INITIATE_CLEAR` reports `STATUS_SUCCESS` while not actually
clearing, and a libusb port reset does not help either.

The trap is that this is not limited to typos. **Any** aborted read
does it, including a perfectly legitimate slow measurement that outruns
its timeout — 100 NPLC with `SAMPle:COUNt 5` takes 10.14 s under a 10 s
default and wedges the meter reproducibly.

How it was found: § 7.4.7 documents an `AZ` autozero mnemonic that
**does not exist**. Every spelling (`RESistance:AZ`, `:AZ:STATe`,
`SENSe:`-prefixed, `AZERo`) is rejected with `-113` for writes *as well
as* queries, on all four functions. The node that actually works is
`ZERO:AUTO`, which round-trips correctly — so autozero is fully
controllable and only the manual is wrong. Probing the documented
spelling cost two power cycles.

Affects: any code that queries a header the meter may not implement,
and any long integration that could outrun its timeout.

Workaround: never *query* to probe for a header's existence — write it
and check `command_error()` (F-7), which is safe. Size read timeouts
from the actual integration time; the driver derives them from NPLC and
sample count rather than using a fixed default.

Reported to Siglent;
`docs/vendor-issues/SDM4065A-firmware-bug-report-2-autozero-query.md`.

Code reference:
`src/benchctrl/drivers/siglent_sdm4065a/driver.py` — `set_autozero`,
`get_autozero`, `reading_timeout_ms`, `_resize_timeout`.

### F-9. PDU41002 allows exactly one CLI session, device-wide
Bench-measured on firmware 1.3.4, and undocumented by the vendor. The
PDU permits **one management session at a time across all transports** —
serial and SSH are not independent channels into it.

Three measurements:

- SSH alone logs in, reaches the prompt and survives 30 s idle. Fine.
- SSH attempted *while serial is logged in* **completes
  authentication** — the banner prints in full — and the device then
  immediately hangs up. **The incumbent wins**; the serial session is
  unaffected and keeps working.
- After `exit` on serial, SSH connects and runs commands normally.

Two consequences, neither obvious:

1. **Closing the serial port does not end the device session.** CLI
   session state outlives the port, so a driver that merely closes the
   port leaves the device occupied and every later SSH attempt dies
   *after* a successful login. `close()` therefore **must** send `exit`.
   This is the only `close()` in the repo with a required side effect on
   the device, and skipping it is a correctness bug rather than
   impoliteness.
2. **The failure is indistinguishable from bad credentials** unless the
   driver looks closely, because it arrives *after* the password is
   accepted. The driver raises `PDU41002SessionError`, which is
   registered in `net/errors.py` so the type survives the RPC wire — a
   remote caller that saw a generic error would misdiagnose this as an
   auth problem every time.

Affects: "network alongside serial" means **alternating, not
concurrent**. `open()` takes exactly one transport and holds one
session; there is no supported configuration with both links live. An
abandoned session (a process killed between login and `exit`) locks the
device out until the idle timeout expires (`devcfg idletime`, default
3 min).

Workaround: none, and none attempted — this is a device limit, not
something to engineer around. Always use the context manager, and if the
device appears to reject a correct password, wait out the idle timeout
before concluding the credentials are wrong.

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/driver.py` — `close`,
`_login_ssh`, `_raise_if_hungup`.

### F-10. PDU41002 SSH needs three non-default options, and a pty
Bench-measured on firmware 1.3.4. Each of these reads like a sloppy
security default and is not:

- **Group-exchange KEX is broken.** The OpenSSH default
  `diffie-hellman-group-exchange-sha256` fails with
  `key exchange failed!`. Forcing the fixed group
  `diffie-hellman-group14-sha256` completes the handshake. The defect is
  specific to group *exchange*, where the client asks the server to
  propose a group.
- **Pubkey auth is refused.** The device offers only
  `keyboard-interactive`, even from a host holding the private key
  matching the `id_rsa.pub` uploaded to the device through its own web
  UI. So the uploaded public key is not usable for client auth,
  `BatchMode=yes` can never work, and the link needs a **pty**
  (`pty.fork()` + `ssh -tt`) to type the password into the client's
  prompt. Unattended runs therefore depend on
  `BENCHCTRL_PDU_PASSWORD`, not on a board key.
- **The exported ed25519 host key is all zeros.** `ssh-keyscan` reads a
  null key, so host-key verification is worthless here. The driver pins
  `StrictHostKeyChecking=no` with `UserKnownHostsFile=/dev/null` —
  accepting an unverifiable key is unavoidable, but poisoning the
  operator's real `known_hosts` with a null entry is not.

Also: over SSH the password prompt comes from the **ssh client**
(`(admin@host) password:`) and the device never shows its own
`Login Name :` / `Login Password :`. The two transports' login sequences
are genuinely different, so sharing one login loop is actively wrong,
not merely untidy — the serial path pokes with a bare CR to discover
session state, which over SSH would submit an empty password.

Affects: the network transport only. Login over SSH takes ~7.5 s; it is
slow, not stuck.

Workaround: none needed — the flags are fixed in the link and commented.
Firmware 1.4.0 is on hand and might fix the KEX defect, but flashing is
a separate decision and is not a prerequisite for anything.

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/links.py` — `SSH_OPTIONS`,
`SshLink`; `driver.py` — `_login_ssh`.

### F-11. PDU41002 CLI has no interrupt character, and `menumode` is one-way
Bench-measured. Three CLI behaviours that each break an assumption
carried over from the SCPI drivers:

- **`\x03` is not an interrupt.** It is echoed and consumed as part of
  the command — a stray one yields `Command not found` at a *constant*
  column regardless of command length. It also does **not** clear a
  dirty input line. A bare `CR` does, and that is the resync the driver
  sends after a timeout. (Sending `\x03` as a prefix broke every command
  in the driver's first end-to-end run; the simulator caught it by
  reproducing the hardware's behaviour rather than agreeing with the
  driver's assumption.)
- **`menumode` is a one-way trap.** Sending it switches the session to a
  menu interface, and the manual is explicit that returning to the CLI
  requires a full logout and login. Every parser would then fail against
  menu output while the link still looked healthy. No driver method
  emits it.
- **`console telnet enable` silently disables SSH.** The two are
  mutually exclusive in firmware, so that verb can kill the network
  transport from underneath a running test. Not in the method surface.

Parsing traps in the same family: read until the **prompt**, never until
a blank line — the unknown-verb error carries a ~30-line verb dump and
the number of blank lines before the prompt varies by error shape
(2, 3, 0), so blank-line termination truncates mid-error and desyncs the
session. Line endings are not uniform (caret lines are introduced by a
bare `\n\r`), so the engine parses bytes rather than trusting
`splitlines()`. And serial echoes the command while SSH does not, which
is the largest textual difference between the transports.

Also: **idle logout is a safety hazard, not an annoyance.** After
`devcfg idletime` (default 3 min) expires, a command is consumed as a
*username* and silently swallowed while the operator believes it ran.
`_cmd()` detects a login prompt in any response, and read-back
verification is the second line of defence.

Affects: anything extending the driver's command surface.

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/driver.py` — `_round_trip`,
`_resync`, `_read_until`, `_raise_for_error`, `_strip_echo`.

### F-12. PDU41002 out of scope, deliberately
Not defects — capabilities the driver refuses to expose, recorded so the
omissions read as decisions rather than oversights.

- **Aggregate outlet targeting.** `oltctrl index all act off` is one
  line that de-powers the entire bench. No signature accepts `"all"`,
  `"b1"`, `"b2"` or a collection, and this is enforced twice: non-`int`
  rejected at coercion, and the rendered command asserted against a
  single-index regex before the write.
- **Cold-start configuration** (`devcfg coldstasta` / `coldstadly`).
  Getting it wrong energises the bench unattended, with nobody present.
- **Daisy chain** (`guest 1|2|3`, accepted by every verb). An off-by-one
  switches a *different physical box*, which is the worst available
  failure mode for a mains switch.
- **Per-outlet metering.** Not a driver limit — the PDU41002 meters the
  device total only, so there is no per-outlet current to read.
  `power_factor` is `None` at zero load (the device prints `----`).
- **SNMP**, the web UI (`login_pass.cgi` posts credentials in
  cleartext), and UDP 3052 (undocumented PDNU discovery).

Also note that **self-protection is a cabling invariant, not a software
guarantee**: the driver cannot know which outlet feeds what. What makes
self-kill impossible on this bench is that the agent host and the
network gear are not plugged into the PDU. That assumption is stated in
`docs/drivers.md`, and `allowed_outlets` / `panic_outlets` are the
controls to revisit *before* anyone changes the cabling.

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

### N-6. The Uno Q kernel has no `ch341`, so the QR10x needs a udev rule
Arduino's Uno Q kernel is built `# CONFIG_USB_SERIAL_CH341 is not set` with no
generic fallback, so the CH340 bridge the QR10x speaks through enumerates but
binds no driver and no `/dev/ttyUSB*` appears. `benchctrl.transports.ch341`
drives the chip from userspace over libusb instead and exposes it as a pty, so
the QR10x driver itself is unchanged.

Which transport gets used is decided by `benchctrl.transports.autoserial`, not
by the operator: an explicitly configured port wins, else a kernel-bound tty for
the CH340 wins, and the userspace driver is used only when the kernel bound
nothing. So the same config works on desktop Linux and on the Uno Q, and a host
that later gains a `ch341` module silently starts using it. Pass `port="auto"`
(or leave it unset) to get that; name a port to force one.

A *failed* open on a kernel tty does not fall back to userspace — that would
turn "another process holds the port" into a different working transport
measuring something else. Transport is chosen by what the host has, never by
what failed.

The catch is permissions: libusb writes to `/dev/bus/usb/BBB/DDD`, which the
kernel creates `root:root 0664`, so control transfers fail with `[Errno 13]
Access denied` for a non-root user. Install
`deploy/udev/60-benchctrl-ch341.rules` (needs root once) to make those nodes
`root:dialout 0660`. A one-off `chmod` is not enough — the node is recreated on
every replug, and the device number changes.

Not fixable without root, and not fixable in-kernel on this board: force-loading
a prebuilt `ch341.ko` fails (`CONFIG_MODULE_FORCE_LOAD` off, vermagic mismatch),
the 7.0.0 kernel package contains no `ch341.ko` either, and nothing can be
compiled on-board (no toolchain, no headers for `6.16.7-g0dd6551ae96b`).

Note that a driverless CH340 is invisible to `list_ports.comports()` by
construction — it enumerates ttys, and the whole problem is that no tty exists.
`discovery.scan_driverless_bridges()` covers that blind spot, reporting the
adapter with `path="auto"`; without it the QR10x reads as "not plugged in" on
this board when it is plugged in and working.

Verified end to end on real hardware: QR101A-1M-R1, serial 00000248 — opened,
closed and reopened through the auto-selected userspace transport, the reopen
proving the USB claim is released rather than leaked.

**Two validation gaps, both needing a host this bench doesn't have.** Neither
is a known defect; they are untested paths, which is a different and lesser
claim than "works". Tracked in [`ROADMAP.md`](ROADMAP.md) § *Revalidate serial
transport selection on a desktop Linux host*, to be closed when we move back
to big-iron Linux hosts.

- **The kernel-first branch has never run on a host that has the module.** It
  is covered by `tests/test_autoserial.py`, including a mutation check that
  inverting the precedence fails a test, but the Uno Q is built without
  `ch341` and WSL has no CH340 passed through. So "the kernel driver is
  preferred where it exists" is asserted, not observed. The negative case
  matters most: a kernel tty that fails to open must raise rather than
  silently falling back to the userspace driver.
- **`serial_number=` selection cannot work on our adapter.** This CH340G
  reports `iSerialNumber=0` — no serial-number descriptor at all — so
  `CH341Device.open(serial_number=...)` has nothing to match and `index=` is
  the only way to choose. Other CH340 variants do carry one. Multi-adapter
  selection is untested on hardware regardless: only one CH340 has ever been
  attached here at a time.

## What's not in this list

Things we **don't** consider limits — they're just facts:

- The Arc Pro's baseline streaming rate is ~6 Hz; high-rate
  streaming (~4 kHz on MAIN_CURRENT) is enabled by `SMU.record()`.
  This is documented in `PROGRESS.md` as a discovered behavior,
  not a limitation.
- The QR10x's relay-switching delay (30–95 ms) is documented as a
  hardware spec, not a limit.
- The DL3031A's 60 A / 350 W rating is a hardware spec.
- The SDM4065A's `9.9E37` overload sentinel is documented instrument
  behaviour, not a defect. The driver raising `SDM4065AOverloadError`
  instead of returning it is a deliberate choice: the sentinel is a
  valid float and would otherwise propagate into arithmetic as a
  believable reading.

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

### H-6. ADU218 relays are 1 A signal switches, not power contactors

The eight relays are solid-state (PhotoMOS) devices rated **1 A at 120 V
AC or DC**. They are not a substitute for the PDU41002's mains outlets and
must not be treated as one: switching a bench instrument's supply through
one would exceed the rating, and inrush on an inductive or capacitive load
exceeds it by more than the steady-state figure suggests.

They are also **solid-state**, so there is no audible click and no
mechanical confirmation that a switch happened. The only confirmation is
the read-back the driver performs — and because the device acknowledges no
write, a switch without a read-back is unfalsifiable.

Related consequence: relay switching is deliberately absent from
`agent/safety.py`'s `_ARMING_CALLS`. Closing a signal relay is not arming
an output, and treating it as one would start a governor countdown on every
switch — a second, weaker software deadman layered over the device's own
hardware watchdog.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `set_relay_state`;
`src/benchctrl/agent/safety.py` — the comment after `_ARMING_CALLS`;
`docs/drivers.md` § "Safety" under the ADU218.

### H-7. ADU218 relay state survives a host reset

Power-on relay state is undocumented by Ontrak, and USB autosuspend holds
the outputs in whatever state they were last commanded to. So a relay can
be conducting before any software runs — after a host reboot, after the
agent is killed, after the cable is unplugged from the *host* end.

`open()` therefore **reads and warns** rather than assuming, naming any
relay it found energised. It does not de-energise them: the driver cannot
know whether an energised relay is holding something that must not be
interrupted.

The mitigation that actually works across a host reset is the device's own
watchdog (`set_watchdog`), which drops the relays without any host
involvement — see F-22 and F-23 for what it costs.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `_connect`;
`tests/test_bench_adu218.py` — `test_open_reports_relays_it_found_energised`.


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
device out until the idle timeout expires — **5 minutes** on this unit,
not the manual's 3 (see F-16).

There is also a shorter tail on an *orderly* close: for roughly 15 s
after `exit`, a new login is refused with output identical to a wrong
password. That is F-15, and it is why the driver retries a refusal
instead of classifying it.

Workaround: none, and none attempted — this is a device limit, not
something to engineer around. Always use the context manager, and if the
device appears to reject a correct password, retry for a minute before
concluding the credentials are wrong.

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

### F-13. PDU41002 cannot be identified by a discovery probe
Bench-measured, and it invalidates the obvious design. The FT232R's
`0403:6001` is in `GENERIC_BRIDGES`, so VID/PID cannot name this device
and the natural move is the same probe mechanism the QR10x uses: write
something harmless, match the reply. The planned probe was a bare `\r` at
9600, on the assumption that a CR merely re-prompts an idle console.

**It does not.** A CR submits an *empty line* to whichever login field is
current, so successive probes walk the authentication state machine:

```
CR 1 -> \r\n\r\nLogin Name :          (the only identifiable state)
CR 2 -> \r\nLogin Password :          (empty username submitted)
CR 3 -> Please wait for authentication....   ~15 s, then Login Failed
```

Three measured consequences, any one of which disqualifies probing:

- **Unreliable.** The vendor string appears in one of three states, so
  the answer depends on what touched the console beforehand. Five
  consecutive `probe_serial_identity()` calls against the real device
  returned `None, cyberpower_pdu41002, None, None, None`.
- **Not read-only.** The device recorded `Login authorization failure via
  Console` in its own event log for probe traffic — a bench sweep writing
  auth failures into a mains switch's audit trail, which also makes real
  intrusion attempts harder to spot.
- **Disruptive.** The console answers nothing during the ~15 s
  authentication delay, so probing can lock out the driver behind it.

No inert alternative exists: opening the port, and writing `?`, `DEL` or
`NUL` without a terminator, all produce **no reply at all**.

So the PDU is identified *passively*, by the udev symlink from
`deploy/udev/62-benchctrl-ftdi.rules` (`discovery.identify_by_symlink`,
wired into `scan_serial`). That is better than a probe rather than a
fallback — exact instead of heuristic, stable across re-enumeration
because the rule keys on the adapter's serial number, and it writes zero
bytes to the device.

**The limitation that remains:** without that udev rule installed the PDU
comes back unidentified. That is the correct failure (an unidentified
port is visible; a probe naming the wrong device is not), but it means
`benchctrl-discover` on a host with no rules installed will not find it.

A related defect was found in the same pass and **fixed**: the QR10x
probe matched on `DEV.TYPE`, a substring of its own request
`AT+DEV.TYPE?`, so any device that echoes its input matched — and the PDU
echoes. The marker is now `+DEV.TYPE=`, which an echo cannot produce. On
hardware the 115200-vs-9600 baud mismatch happened to mask this, but the
marker was wrong regardless of which devices it spared.

Affects: `benchctrl-discover` output, and any future device behind a
generic bridge that authenticates on its console.

Code reference:
`src/benchctrl/discovery.py` — `SERIAL_PROBES` (note), `SYMLINK_KEYS`,
`identify_by_symlink`, `scan_serial`.

### F-14. `oltctrl` acknowledges nothing, so a switch is unfalsifiable without a read-back
Bench-measured on firmware 1.3.4, and it is the constraint that shapes the
whole switching surface. `oltctrl index N act off` answers with a **blank
line and a re-prompt** — byte-identical whether the contactor moved, the
index was out of the device's range in a way the parser missed, or the
session had silently timed out and consumed the command as a username.
There is no success marker, no failure marker and no error code. The
transcript is checked in at `tests/fixtures/pdu41002/outlet_switch.txt`.

Consequences that are not obvious from the vendor manual:

- **`set_outlet_state` reads the outlet back and returns *that*, not
  `None` and not the requested state.** `verify=False` exists but logs a
  warning and returns the request unconfirmed; there is no honest way to
  make it return anything better.
- **The read-back budget must be derived from the device.** Each outlet's
  `td_on` / `td_off` is operator-configurable, so a hardcoded wait sized
  from the measured ~0.62 s round trip or ~1.5 s settle time flakes on a
  unit whose delay someone raised. The driver reads `oltcfg` and adds a
  margin.
- **`reset_outlet` cannot be verified at all.** A reboot ends where it
  started, so no read-back distinguishes "cycled" from "never moved". It
  returns `None` rather than implying a guarantee it cannot make. Drive
  `set_outlet_state(n, False)` then `True` if you need the cut proved.
- **A read within ~0.5 s of `act reboot` still returns the pre-cut
  state.** Measured 3 times in 6 trials: `reset_outlet` returns, and the
  next `oltsta` read still says `True` because the cut has not landed yet.
  So a caller polling for "energised again" can stop on the *pre-cut*
  `True` and conclude the cycle finished before it started. Wait out
  `off_delay_s + reboot_duration_s` before believing any `True`. This
  hazard is unique to `reset_outlet`: it is the only call whose wanted end
  state equals its start state, so `set_outlet_state`'s verify — which
  polls for the *opposite* of what it read — can never be satisfied by a
  stale value. The hardware test made exactly this mistake and failed
  about half the time in a full run while passing in isolation.
- **A simulator that always obeys cannot test any of this.** So
  `SimulatedPDU41002` has an `ignore_switches` flag: it accepts and
  acknowledges `oltctrl` byte-for-byte and moves nothing. Without a
  device that can lie, the read-back path has no failing case to catch.

Affects: every switching call, and any caller tempted to treat a returned
value of `None` from `reset_outlet` as "it didn't work".

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/driver.py` —
`set_outlet_state`, `reset_outlet`, `_verify_outlet`, `_verify_budget_s`;
`src/benchctrl/sim/pdu41002.py` — `ignore_switches`, `_blank_ack`.

### F-15. `Login Failed` cannot be told apart from a wrong password
Bench-measured on firmware 1.3.4, and found only because the hardware
suite was written: the login has **four** outcomes, not two.

| What arrives | Means |
|---|---|
| `CyberPower > ` | authenticated |
| `Login Name :` again | the credential was rejected |
| nothing at all | the device did not answer — slow or wedged mid-auth |
| ~15 s of dots, `Login Failed`, then silence with no re-prompt | **either** a wrong password **or** a correct one submitted too soon |

That fourth shape is the problem. Because the CLI is single-session
(F-9), a session that has just closed keeps the device busy for roughly
15 more seconds, and a **correct** password offered inside that window is
refused with output byte-identical to a wrong one. Nothing on the wire
separates the two cases, so no amount of parsing can classify them.

**The driver therefore retries rather than deciding.** `_LOGIN_TIMEOUT_S`
is 75 s — several attempts' worth — and `PDU41002AuthError` is raised
only when that budget is spent, at which point the message names both
possibilities instead of asserting the credential is wrong.

Two consequences worth knowing:

- **A login can legitimately take over a minute** after another session
  closed. It is slow, not stuck, and not a bad password.
- **A device that never answers is a different fault** and must not be
  reported as an auth failure — it sends the operator to check a
  credential that was correct. The driver raises
  `PDU41002TimeoutError` saying "the password was not rejected — the
  device did not answer", and `SimulatedPDU41002.stall_next_auth` exists
  to pin that.

Affects: `open()` over both transports, and any operator debugging a
refused login.

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/driver.py` — `_login_serial`,
`_LOGIN_TIMEOUT_S`, `_AUTH_WAIT_S`, `_LOGIN_RETRY_S`, `_LOGIN_FAILED`;
`src/benchctrl/sim/pdu41002.py` — `refuse_next_logins`, `refusal_count`,
`stall_next_auth`.

### F-16. An idle SSH session is dead, not merely logged out
The idle timeout is not what the manual implies and does not behave the
same on both transports. Both facts are measured, not documented.

- The device reports `Idle Time : 5 Minutes`, **not** the 3 minutes the
  vendor default suggests. Read `devcfg show` rather than assuming.
- Over **serial** the timeout drops the session to `Login Name :` with
  the port still open, so the driver re-authenticates in place and the
  caller never sees it.
- Over **SSH** the device closes the connection at ~180 s — earlier than
  its own stated idle time. The ssh client prints
  `Received disconnect from … user close and disconnect!` and
  `Disconnected from …`, then exits. There is no longer a far end.

**So the SSH case is not recoverable and the caller must `open()`
again.** The driver matches the client's notice and raises
`PDU41002ConnectionError` naming the reopen. Before that check existed
the symptom was `PDU41002TimeoutError: no prompt after 'sys show' within
12.0s (got 130 bytes)` — which reads as a slow device and invites a retry
that can never succeed, on a link that no longer exists.

**ssh has two wordings for this, and the second one collides with F-9.**
Depending on how the client notices the close it prints either
`Received disconnect from … : user close and disconnect!` +
`Disconnected from …`, or `Connection to … closed by remote host.` The
second matches `_HANGUP_MARKERS`, which is how the single-session hangup
is detected — so recognising only the first left an idle logout reported
as `PDU41002SessionError`: *"another session is logged in — send 'exit' on
it."* Wrong twice over: nothing else was logged in, and that advice
cannot be followed on a link that no longer exists.

The fix is not a longer list of wordings, which a future OpenSSH release
would defeat. **Position disambiguates where wording cannot:** the hangup
markers only mean "someone else holds the CLI" *during login*. Past a
successful login, anything that ends the session means the link is dead
and the recovery is to reopen. So `_LINK_GONE_MARKERS` is matched in
`_round_trip` (post-login only) and `_raise_if_hungup` is no longer
called from `_cmd`.

Affects: any long-lived SSH session with gaps between commands. A bench
sweep polling more often than every ~3 minutes never sees it, which is
exactly why it is easy to ship broken.

Code reference:
`src/benchctrl/drivers/cyberpower_pdu41002/driver.py` —
`_SSH_DISCONNECT_MARKERS`, `_raise_if_disconnected`, `_round_trip`;
`src/benchctrl/sim/pdu41002.py` — `drop_ssh_session`, `_gone`;
`tests/test_hardware_cyberpower_pdu41002.py` —
`test_an_idle_logout_is_recovered_on_serial_and_fatal_over_ssh`.

### F-17. A CP2112 level identifies nothing; only a level you can change does
Bench-measured, and it is the fact that made commissioning this device
slow. Two readings on the same net looked contradictory:

- the CP2112 reported **every** pin as an input latching `1`
- a DMM clamped to one of those pins read a flat **0.0002 V**

Both were correct and nothing was broken. An undriven CP2112 pin is
**high-impedance**, so a ~10 MOhm voltmeter drags the floating net to
nearly 0 V while the chip's own input buffer still latches a 1. Neither
instrument was lying; the pin was not being driven by anything.

**The consequence is that `read_levels()` cannot be used to work out what
is attached.** On an unconfigured device it returns `0xFF` regardless of
wiring, regardless of whether anything is connected at all. A pin is
identified only by a level you can make *move*: drive one line at a time
as open-drain, and watch which one the meter follows.

This is also why the hardware tests are witnessed by a DMM rather than by
the chip. Reading a pin back through the CP2112 that just drove it proves
the **latch** changed, not that any voltage did -- the read-back is
downstream of the thing under test. `set_line_asserted(verify=True)` is
still worth having, because a read-back that *disagrees* is a real
failure, but a read-back that agrees is not evidence the net moved.

Affects: any attempt to identify or diagnose CP2112 wiring from software
alone, including `cp2112_line_states` output shown to a model.

Code reference:
`src/benchctrl/drivers/silabs_cp2112/driver.py` -- `read_levels` docstring;
`src/benchctrl/sim/cp2112.py` -- `_effective_levels` models it;
`tests/test_cp2112.py` -- `TestLevelsAreNotIdentity`.

### F-18. CP2112 SMBus is out of scope, and push-pull is deliberately unreachable
Two capability gaps that are decisions rather than omissions, recorded so
neither is later "fixed" into a regression.

**SMBus / I2C is not implemented**, despite being the chip's headline
feature -- benchctrl wants the eight GPIO pins. A bus master would need
device addressing, clock configuration and transfer-status polling: a
second protocol with its own failure modes and its own simulator, for a
capability nothing on the bench currently needs. The report IDs (`0x06`
config, `0x10`-`0x17` transfers) are documented in `driver.py` if that
changes.

**Push-pull output is unreachable through the public API**, and that is a
safety property rather than a style preference. It is enforced three ways:
`set_line_mode` clears the push-pull bit on every call even if another
program had set it, `set_line_asserted` refuses a pin configured
push-pull, and a test asserts no public method takes a `push_pull`
parameter. The reasons are physical:

- An open-drain pin pulls a net low and releases it but never *sources*
  into it. A push-pull output at 3.3 V wired to a 1.8 V reset net
  back-feeds the target's rail through its ESD diodes.
- On unplug the chip reverts every pin to an input, which for an
  open-drain reset line *is* released. A push-pull pin holding 0 V would
  leave the DUT in reset with no software left to notice.

So a device needing a driven-high control line is not supported. That is
the correct trade for the job this driver exists to do -- and a line that
must be driven both ways is a different instrument's job.

Affects: anyone reaching for the CP2112 as a general-purpose I2C bridge or
as a two-way logic driver.

Code reference:
`src/benchctrl/drivers/silabs_cp2112/driver.py` -- `set_line_mode`,
`set_line_asserted`;
`src/benchctrl/drivers/silabs_cp2112/__init__.py` -- scope statement;
`tests/test_cp2112.py` -- `TestOpenDrainIsTheOnlyDriveMode`.

### F-19. CP2112 GPIO is not real-time, and the pins default to inputs
Two datasheet facts with practical consequences.

**Every transition is a separate USB control transfer**, so edge timing is
bounded by bus scheduling rather than by the chip; the datasheet says the
GPIO pins are "not recommended for real-time signaling." A requested 1 ms
pulse would be neither 1 ms nor reliably repeatable, so
`trigger_reset_pulse` **refuses durations below 5 ms** rather than
silently stretching them and reporting success. Sub-millisecond edges need
a different instrument. For reset lines, whose hold times are specified in
milliseconds, this is not a constraint that bites.

**All eight pins revert to inputs after any reset or re-plug** (datasheet
section 7). This is benign for an open-drain reset line -- high-Z is
released -- but it means configuration does not survive a USB
re-enumeration, and code that configured a pin minutes ago cannot assume
it is still an output. Every write in the driver is a read-modify-write
against the live config register rather than against a cached copy, for
this reason.

A third, smaller one: `hidraw` numbering is not stable. `hidrawN` shifts
whenever another HID device is replugged, and on the bench board the
CP2112's neighbours are the USB keyboard. Use the udev symlink
(`/dev/benchctrl/cp2112-<serial>`) or let the driver find the device by
VID/PID rather than pinning a node path in config.

Affects: pulse-timing expectations, and any assumption that pin
configuration persists.

Code reference:
`src/benchctrl/drivers/silabs_cp2112/driver.py` -- `MIN_PULSE_S`,
`trigger_reset_pulse`;
`deploy/udev/64-benchctrl-cp2112.rules`.

### F-20. A blanket hidraw udev rule would expose the keyboard
Not a device limitation but a deployment trap specific to `hidraw`, and it
has no equivalent for the serial drivers next to it.

The CP2112 needs its node readable *and writable* by the bench user --
writable even for reads, because a HID feature *get* is a `GET_REPORT`
over the control pipe. The tempting one-liner is:

```
SUBSYSTEM=="hidraw", MODE="0660", GROUP="dialout"      # DO NOT
```

On the bench board that also matches `hidraw0` and `hidraw1`, which are
the attached **USB keyboard** (`04d9:1818`). That is a keylogging surface,
not a bench instrument. `deploy/udev/64-benchctrl-cp2112.rules` is
therefore scoped to `10c4:ea90` specifically.

Two related traps: `10c4:ea60` is the CP210x *UART* bridge, a different
chip, and must not match. And `discovery.scan_hidraw()` filters to known
signatures rather than reporting every hidraw node -- verified against the
real board, where it finds the CP2112 and skips both keyboard nodes -- so
`benchctrl-discover --probe` can never be pointed at the operator's
keyboard.

**Until the rule is installed the node is `root:root 0600`** and the agent
cannot open it. That is the correct failure (loud, and it names the rule
in the `PermissionError`), but installing it is a privileged operation and
therefore the operator's to run, not the agent's.

Affects: first-time CP2112 setup on any host.

Code reference:
`deploy/udev/64-benchctrl-cp2112.rules`;
`src/benchctrl/drivers/silabs_cp2112/hidraw.py` -- `HidrawLink.open`;
`src/benchctrl/discovery.py` -- `scan_hidraw`.

### F-21. ADU218 reports no error, ever

The Ontrak ADU218 has **no error reply**. Three different failures are
byte-identical on the wire — nothing comes back:

- an unknown command
- a valid command with an out-of-range argument (`RPK8`, `RPA4`, `MK256`)
- a write-only command working exactly as intended (`SKn`, `RKn`, `MKddd`,
  `DBn`, `WDn`)

So `ADU218TimeoutError` is **ambiguous by construction** and its message
says so. It cannot distinguish "the command was wrong" from "the device is
gone", and no amount of driver work can make it: the information is not on
the wire.

What the driver does instead of pretending:

- a whitelist of every command it can render, checked before the write, so
  a format-string slip is caught host-side rather than becoming silence
- an explicit per-command `responsive: bool` table, never inferred — the
  trap is `RKn`, which is write-only despite starting with `R` while every
  other `R` command answers, and is the most-called command on the device
- an explicit per-command reply *width*, so a desynced reply raises
  `ADU218ProtocolError` instead of returning a plausible number
- read-back verification on every relay write, since an accepted-and-ignored
  command is otherwise invisible

**The mitigating half:** unlike the SDM4065A, an ignored command does not
poison the session. There is no error queue to surface on the next read —
the next valid command answers normally.

Affects: every command. Diagnosing a silent ADU218 means checking the
command against the manual by hand; the driver's whitelist is the closest
thing to a syntax error available.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `_COMMAND_SPECS`,
`command_spec`, `_send`; `tests/fixtures/adu218/errors.txt`;
`tests/test_bench_adu218.py` — `TestSilence`.

### F-22. ADU218 `WD` cannot distinguish "timed out" from "never enabled"

Reading `WD` returns `0` in both cases. A watchdog that fired self-clears
to 0, which is also exactly what a watchdog that was never armed reports.
So a trip leaves **no trace the device can be asked about** — the only
evidence is a disagreement between the device's answer and what the host
last commanded.

Consequences the driver accepts rather than hides:

- it holds its own armed state, and `read_watchdog_tripped()` compares the
  two. Latched once: detecting a trip clears the held expectation, so the
  same trip is not re-reported.
- it writes `WD0` at `open()` unless told otherwise, because a fresh
  process has no expectation to compare against and would inherit the
  ambiguity from whatever ran before it. `disarm_watchdog=False` preserves
  an inherited setting at the cost of that ambiguity, and the driver's held
  expectation is then 0 — which is a lie it cannot avoid telling.
- **a trip that happens while no benchctrl process is running is
  undetectable.** The relays will have dropped and nothing will say why.

Affects: any use of the hardware watchdog across a process restart.

**The interlock itself is now witnessed on hardware** (2026-08-27), which is
worth separating from the ambiguity above: the ambiguity is about *reading* a
trip, not about whether the trip happens. Armed at `WD2`, the DMM held
17.471–17.474 Ω across 24 consecutive readings spanning 9.86 s after arming,
then read the overload sentinel — bracketing the trip to **(9.86, 10.83] s**
against a 10.0 s nominal, the gap being one meter read. So the *device*
de-energises its own relays with no benchctrl process, no GPIO and no kernel
driver in the decision path.

That measurement also closed a hole in the test that asserted it. The original
form armed `WD1` (1 s), waited 1.5 s and asserted the contact was open — which
is satisfied both by "opens on timeout" and by "opens as a side effect of
arming". The second would make the interlock useless, since it would drop the
load the moment it was enabled, and `WD1` is too short to sample between the two
(one meter read costs ~0.41 s). The test now uses `WD2` and asserts the contact
is **still closed** early in the window, with a guard that the early sample
really landed early. Confirmed by mutation in both directions: de-energising
right after arming fails the `WD2` form and **passes** the `WD1` form.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `_watchdog_setting`,
`read_watchdog_tripped`, `_connect`; `tests/test_bench_adu218.py` —
`TestWatchdog`; `tests/fixtures/adu218/watchdog_trip.txt` — both brackets.

### F-23. Any ADU218 command refeeds the watchdog, so a poller neuters it

The hardware watchdog is fed by **any** command reaching the device —
including a plain state read, and including a command the device rejects.
There is no dedicated keep-alive command and no way to read state without
feeding.

That makes two reasonable-looking patterns silently wrong:

1. **A status-polling loop keeps an armed watchdog alive indefinitely.**
   Measured on the simulator's synthetic clock: with `WD2` (10 s) armed,
   ten rounds of *advance 9 s, then read the relay states* held a relay
   energised across 90 s with **zero** trips. Eleven seconds of real
   silence dropped it. A dashboard refreshing a panel is enough to defeat
   the interlock it is displaying.
2. **A background keep-alive thread is worse than no watchdog.** It would
   keep the timer fed precisely while the failure the watchdog guards
   against was happening — wedged control logic, a hung test, a process
   that is alive but no longer doing anything. The interlock would be inert
   and indistinguishable from a working one.

So benchctrl ships **no keep-alive helper for this device**, and a test
asserts no such method exists. The feed has to come from whatever is
actually controlling the test, which is the only thing whose silence means
something.

Affects: any use of the watchdog alongside monitoring. If a dashboard or
an agent presence sweep is polling this device, the watchdog is not
protecting you.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — module docstring,
`set_watchdog`; `src/benchctrl/drivers/ontrak_adu218/mcp_tools.py` —
`adu218_set_watchdog`, `adu218_watchdog`; `tests/test_bench_adu218.py` —
`test_any_command_refeeds_the_timer`.

### F-24. ADU218 out of scope, deliberately

Three capabilities the device has that this driver does not expose, each
because getting it wrong is worse than not having it:

**Power-on relay state.** Undocumented, and USB suspend holds outputs in
their last state, so a relay can be conducting before any software runs.
`open()` reads the port and **warns**, naming any energised relay, rather
than driving `MK000` — the driver cannot know whether an energised relay is
holding something that must not be interrupted. `reset_relays()` is one
explicit call away.

**No firmware version.** `bcdDevice` is `0000` on the bench unit, so there
is nothing to report and `ADU218Info` has no firmware field. An
always-`None` field would invite a caller to read its absence as "old
firmware"; deriving one from the product string would be a guess wearing a
measurement's name.

**No `interfaces.Switch` Protocol.** Per `CONTRIBUTING.md` convention 3, a
Protocol lands with the *second* instance. The PDU41002 is 1-indexed mains
outlets with configurable switch delays; this is 0-indexed signal relays
with a hardware watchdog. A Protocol generalised from those two would fit a
third device — a real signal multiplexer — badly.

Related: the device key is deliberately absent from
`registry.SWITCHED_PDU_KEYS` and the FUI's `PDU_KEYS`, both of which mean
"switches mains". Tests pin both exclusions.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `_connect`,
`ADU218Info`; `src/benchctrl/drivers/ontrak_adu218/__init__.py`;
`tests/test_bench_adu218.py` — `TestLifecycle`, `TestDispatchGate`.

### F-25. ADU218 de-bounce settings run backwards, and are unobservable below a few hundred Hz

Two separate traps in one setting, and the first is the kind of thing that
silently does the opposite of what an operator intended.

**A higher setting is a *shorter* filter.** Manual §6c: `0 = 10ms`,
`1 = 1ms` (default), `2 = 100us`. Both intuitive readings are wrong — `0`
is not "off", it is the *longest* filter, and `2` does not filter hardest,
it filters least. Somebody chasing maximum contact de-bounce on a noisy
input reaches for `2` and gets 100 µs, the weakest setting available. The
driver therefore exposes `read_debounce_ms()` and `DEBOUNCE_MS` alongside
the raw setting, and the MCP tools return `debounce_ms` next to `debounce`,
because handing a caller the bare number invites the wrong inference.

**The setting has no observable effect on a clean slow signal.** Measured
on a 10 Hz square wave into PA3, 20 s per setting: `DB0` 10.042, `DB1`
9.992, `DB2` 9.992 counts/s — 0.5 % spread, i.e. indistinguishable. That
is not a fault: every filter width is far shorter than the 50 ms
half-period, so none has anything to reject. The consequence for testing is
that **a passing de-bounce round-trip proves acceptance, not effect**, and
the hardware test says so rather than implying more.

Discriminating the three needs a period approaching 10 ms — a few hundred
Hz — against event counters rated to only **1 kHz** ("Max Frequency 1KHz").
Above that rating the count under-reports **silently**; there is no
overrun flag and the driver cannot detect it, so a frequency read off a
counter driven past 1 kHz is wrong with no indication. The usable
discrimination window is therefore roughly 100–500 Hz.

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `DEBOUNCE_MS`,
`COUNTER_MAX_FREQUENCY_HZ`, `read_debounce_ms`;
`tests/fixtures/adu218/counters_live_signal.txt`;
`tests/test_bench_adu218.py` — `TestDebounce`.

### F-26. ADU218 counter-to-input map exists only as an image, and counters wrap silently

**The map is not in the manual's text.** Counter-to-input assignments live
in a "Table 1: Event Counter Port Assignments" **image**, which `pdftotext`
drops entirely — the extraction renders blank space between the caption and
the following paragraph. So the mapping the driver relies on (counters 0-3
→ PA0-PA3, 4-7 → PB0-PB3) could not be read from the document at all. It
is measured instead — and as of 2026-08-27 **all eight positions are
measured**, so the image is redundant rather than merely corroborated. The
generator was walked across every line in turn:

| line | counter that moved | rate (vs 10 Hz) | `PI` bit |
|---|---|---|---|
| PA0 | 0 | 9.972/s | 0 |
| PA1 | 1 | 9.972/s | 1 |
| PA2 | 2 | 10.071/s | 2 |
| PA3 | 3 | 9.972/s | 3 |
| PB0 | 4 | 10.071/s | 4 |
| PB1 | 5 | 10.063/s | 5 |
| PB2 | 6 | 9.973/s | 6 |
| PB3 | 7 | 10.071/s | 7 |

At each position the named counter was the **only** one of eight to move, and
the `PI` union across 60 reads set that bit and no other. Every rate lands
within ±0.5 % of the stimulus, which is ±1 event of quantisation in a 10 s
window — no counter drops or doubles anywhere.

**PORT B is what pins the offset, and PORT A cannot.** On PORT A the counter
index simply *equals* the line number, so PA3 → 3 is equally consistent with
offsets of 4, 3 and 0. The offset is confirmed at three PORT B lines
independently (PB0 → 4, PB2 → 6, PB3 → 7), and as a mutation: forcing it to 3
fails all three counter tests. The general shape: a reading taken where two
hypotheses coincide confirms neither, so measure at the coordinate where they
diverge.

Point `BENCHCTRL_ADU218_INPUT` at whichever line the generator is on; the
fixture skips with the line named if it is not toggling, so a stale setting
cannot false-pass. **It can still quietly cost coverage, though, and did.**
The default tracks the bench, so it goes stale whenever the bench moves: the
walk left the generator on PB3 while the default still said `B2`, and the
suite ran 7 passed / 5 skipped instead of 10 / 2 with nothing misconfigured
and nothing red. "PB2 is not toggling" is equally true when the generator is
unplugged and when it is one terminal over. The skip now sweeps the whole
port and names the line that *is* toggling, which is the only version of
that message that distinguishes the two. Worth generalising: a
bench-tracking default needs a diagnostic that separates "moved" from
"absent", or an accurate skip becomes a silent reduction in what the suite
actually checks.

**Counters count cycles, not edges** — one count per low-to-high
transition. Verified rather than assumed, because the two hypotheses differ
by exactly 2× and a factor-of-two frequency error looks plausible forever:
at 10 Hz the device counted 10.030/s while host sampling independently saw
9.997 rising *and* 9.997 falling edges/s (ratio 1.003, not 2.0).

**They wrap from 65535 to 0 with no flag.** The only correct use is
differencing successive reads *modulo 65536*; a naive `after - before` goes
sharply negative exactly once per 65536 events. `clear_counter()` (`RCn`)
is read-and-clear and must never be retried — a lost reply loses the count
permanently and a retry returns 0, indistinguishable from "no events".

Code reference:
`src/benchctrl/drivers/ontrak_adu218/driver.py` — `COUNTER_COUNT`,
`COUNTER_MAX`, `clear_counter`;
`tests/fixtures/adu218/counters_live_signal.txt`;
`tests/test_bench_adu218.py` — `TestCounters`.

### F-27. ADU218 closed-contact resistance is a property of the wiring, not the relay

**No test may assert a resistance threshold, and no test may assume the
reading is stable within a session.** Both were tried and both produced
flaky tests on this bench.

*Across relays.* Same meter, same session, identical PhotoMOS parts: **K0
closed reads 9.483 Ω, K7 closed reads 36.02–36.22 Ω** — nearly 4× apart. The
excess is lead and clip resistance outside the relay. Any `< 10 Ω` "closed"
rule derived from K0 calls a perfectly good K7 open.

*Across sessions.* The same closed relay measured 6.14, 10.69, 10.65 and
9.40 Ω across four sessions, with the step traced to re-seated probes.

*Within a session.* K7 on spring clips drifted **62 → 127 → 61 Ω** over 15
back-to-back runs of one test, monotonically and then back. A
same-circuit bound of `hi < lo * 2` worst-cased at **1.80** and failed about
one suite run in seven.

**Consequence.** The witness keys on the instrument's **overload
sentinel**: closed is "reads a number at all", open is "out of range".
That distinction is categorical rather than quantitative and survives any
amount of contact drift. The residual same-circuit check is an order of
magnitude, which by measurement does *not* catch a K7↔K0 lead move (ratio
3.81–6.64) — accepted deliberately, because no threshold separates that
from the 1.80 the clips produce unaided. Leads on the wrong relay are
caught instead by the "either the relay is not switching or the leads are
not across it" assertions, which name both causes because a resistance read
cannot tell them apart.

**Also unfixable by the volts gate.** The fixture skips when the leads sit
on a *powered* net (measured 3.392 V after the meter was left on the
CP2112), but a *different dry* contact reads ~0 V exactly like the right
one — so a stale `BENCHCTRL_ADU218_RELAY` fails rather than skips.

**All eight measured in one session (2026-08-27), which settles the
question.** The meter was walked across every relay in turn, one screw
terminal pair at a time, and each was independently witnessed:

| relay | closed Ω | within-position spread |
|---|---|---|
| K0 | 45.65 | 0.035 |
| K1 | 20.77 | 0.004 |
| K2 | 28.37 | 0.024 |
| K3 | 31.36 | 0.062 |
| K4 | 32.09 | 0.042 |
| K5 | 41.19 | 0.544 |
| K6 | 16.87 | 0.002 |
| K7 | 17.50 | 0.002 |

**16.9–45.7 Ω, a 2.7× spread, with no relation to index** — on identical
PhotoMOS parts, one meter, one hour. Within-position spread is ≤ 0.06 Ω
everywhere except K5, so the drift that broke `hi < lo * 2` is clip seating
rather than anything about the bench or the device.

**A ceiling on the closed reading was considered and rejected.** During this
walk a loose screw terminal made K3 read **336 kΩ closed**, and the suite
passed — a closed contact is asserted to be "a number", never bounded above.
That is the correct behaviour, not a gap. The test's claim is that the
relay's state follows the command, and at 336 kΩ that claim was *true*: the
DMM saw overload → finite, a real state change. What was broken was bench
wiring quality, which is not the driver's claim and is not visible to it. A
ceiling would also be exactly the threshold-on-a-wiring-property this entry
exists to forbid — a loose terminal *is* a wiring property, so it is squarely
inside the rule rather than an exception to it. In service these relays
switch a load rather than a meter, where what matters is that the drop and
the dissipation are acceptable, not that the reading falls in a band.

Code reference:
`tests/test_hardware_ontrak_adu218.py` — module docstring, the `witness`
fixture, and `test_the_driver_and_an_independent_instrument_agree_on_every_transition`.


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

### A-5. No simulator exercises the transport its device really uses
Every simulator models its instrument's wire protocol faithfully, and
none of them reaches that instrument's real transport. The SCPI sims
answer over pyvisa-py's ASRL backend while the hardware is USB-TMC, so
`SimulatedSDM4065A` proves the SCPI grammar and proves nothing about the
USB-TMC endpoint pair — which is why `clear_device_buffers()`, the
wedged-endpoint recovery for F-8, has no hardware-free test at all.

The ADU218 is the sharpest case, because its transport *is* most of the
driver. `SimulatedAdu218Link` subclasses the production
`Adu218UsbfsLink` and overrides `_transfer()` — the one method that calls
`fcntl.ioctl` — plus the three lifecycle members that would otherwise
open a real device node (`open`, `close`, `is_open`). Everything else is
shipping code under test: the 8-byte report framing, the mandatory `0x01`
prefix, the NUL trim, the desync check, the `ETIMEDOUT` →
`Adu218LinkTimeout` mapping, `drain()`. Everything below the seam is the
kernel, and that is where the coverage stops.

Consequence, stated precisely so it is not over-read: a hardware-free run
cannot fail because `USBDEVFS_BULK` was the wrong request number, because
the `usbdevfs_bulktransfer` struct was laid out wrongly for the running
word size, because an interrupt endpoint rejected a bulk-shaped ioctl, or
because the interface could not be claimed. Those are exactly the
failures that separate a 64-bit laptop from a 32-bit board.

Two things narrow it rather than close it. The ioctl request numbers are
**computed** by `_ioc()` and pinned by test to all three measured
constants (`0xC0185502` on 64-bit, `0xC0105502` for a 16-byte struct,
plus the claim/release codes), so a struct-layout slip fails on the
laptop instead of on the bench. And the kernel side is contractual rather
than assumed: `devio.c` branches on `USB_ENDPOINT_XFER_INT` and reissues
the transfer as an interrupt URB, so "bulk ioctl on an interrupt
endpoint" is a documented kernel path, not a happy accident.

Not fixed because closing it means simulating `fcntl.ioctl` itself, which
would assert that the model of the kernel matches the model of the kernel.
The honest substitute is the hardware tier — six tests in
`tests/test_hardware_ontrak_adu218.py`, one of which checks a relay with
the SDM4065A rather than with the device's own read-back.

Code reference: `src/benchctrl/sim/adu218.py` — `SimulatedAdu218Link`;
`src/benchctrl/drivers/ontrak_adu218/usbfs.py` — `_ioc`, `_transfer`;
`tests/test_usbfs_adu218.py` — its module docstring states the same
boundary, and `TestIoctlConstants` is the part that guards it.

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

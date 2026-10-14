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
pytest -m "not hardware"     # 2316 collected, ~22 min, no hardware
pytest -m hardware           # 201, needs the bench
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
| `test_bench_siglent_sdm4065a.py` | the SDM4065A's measurement surface *and its traps*: that `MEASure?` discards a null, that the naive value-then-state null ordering genuinely fails, that a 4-wire read resolves a 38 mΩ offset a 2-wire read cannot, and that the 2 MΩ range is rejected because it belongs to the SDM4055A |
| `test_remote_sdm4065a.py` | the same driver through the full remote stack — proxy, wire protocol, agent dispatch, production driver, pyvisa, pty, simulator. Only the silicon is fake. Catches the registry entries and codec/exception round-trips that local tests cannot see |
| `test_cross_validate_sdm4065a_qr10x.py` | 8 of its 15 tests need no hardware at all: they **pin the tolerance budgets** derived from the two datasheets — that the range term dominates at low resistance, that only the meter (not the two-instrument comparison) resolves the 38 mΩ offset, that a null buys back the 2-wire lead term, and that the budget is a linear sum rather than a quadrature sum. A tolerance cannot be quietly loosened to make a bench run pass |
| `test_bench_cyberpower_pdu41002.py` | 149 tests over the CLI engine both links share: the three error shapes, reading to the prompt rather than to a blank line, the conditional command echo that differs between serial and SSH, `menumode` unreachable, and that a password appears in neither `repr()` nor `to_dict()` |
| `test_remote_cyberpower_pdu41002.py` | the PDU through the full remote stack, one test per registration site |
| `test_usbfs_adu218.py` | 61 tests on the USB layer alone, with no device: that the ioctl numbers are **computed** from `sizeof(struct usbdevfs_bulktransfer)` rather than hardcoded (a literal that works on a 64-bit laptop is wrong on the 32-bit board), that the eight-byte report framing is exact, and that a desync is detected rather than returned |
| `test_bench_adu218.py` | 96 tests against the simulator, whose link **is** the production link. The load-bearing ones are about *silence*: that a write-only command is not waited on, that a command outside the whitelist never reaches the wire, and that the argument check and the whitelist are distinguishable even though both raise `ADU218ValueError`. Plus the watchdog ladder against a synthetic clock, and that `reset_relays()` verifies it actually reached the safe state |
| `test_remote_ontrak_adu218.py` | the ADU218 through the remote stack, including that every relay-switching method lands in `surface.mutators` — the guard on the naming decision, since `agent/dispatch.py` derives mutators from name prefixes alone and a miss would make mains-adjacent switching callable without a writer claim |

### The local / remote / sim seam

| File | Coverage |
|---|---|
| `test_session_config.py` | config precedence (explicit > CLI > env > file > all-local), per-device mode resolution, rejection of `deadman_s <= heartbeat_s`, and the loud failure when a remote device names no reachable endpoint |
| `test_discovery.py` | the signature table and confidence levels. Asserts **no driver signature collides with a known USB-serial bridge** (CH340 / FTDI / CP210x) — the test that stops confident false positives. Also that a CH340 with *no* kernel tty is still reported (`comports()` lists ttys, so a driverless adapter is invisible to it by construction) and is *not* double-reported once the kernel does bind it |
| `test_autoserial.py` | transport precedence: an explicit port wins and probes nothing, a kernel tty beats the userspace CH341 driver, userspace runs only when the kernel bound nothing, and an unrelated FTDI tty does not suppress the fallback. Plus the two things that leak hardware if wrong — bridge closed when the driver closes (**even if the driver's `close()` raises**), and not leaked when the driver's open fails. Pins that a *failed* kernel open raises rather than silently falling back to a different transport |
| `test_ch341.py` | the userspace CH340 driver: baud/LCR registers pinned against Linux's `ch341.c`, the pty bridge moving bytes both ways binary-safely, a chunked reply not truncated by poll gaps, and the real QR10x driver end to end over a loopback-backed bridge. Also adapter selection — that asking for a serial number when **no** adapter publishes one says so (`iSerialNumber=0`, use `index=`) rather than reporting a lookup miss, while a genuine miss still lists what was found |
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

173 tests across five instruments. They exercise every wire command
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
| `test_bench_siglent_sdm4065a.py` | SDM4065A | the manual's quirks proven on silicon rather than against my own simulator: `CONFigure` resetting NPLC to 10 *and* re-enabling autorange, `NULL:STATe` arming `NULL:VALue:AUTO`, that writing a value does **not** disarm AUTO as §7.4.3 claims, that the two `DEF` forms disagree about autoranging, and that autozero answers to `ZERO:AUTO` rather than the manual's `AZ`. Plus all six 4065A NPLC values and every resistance range read back to catch silent coercion, the 2 MΩ range rejected as the sibling model's, the overload sentinel raising, and a 100 NPLC × 10-sample read finishing inside `reading_timeout_ms` |
| `test_cross_validate_sdm4065a_qr10x.py` | SDM4065A **+** QR10x | the meter and the programmable resistance measuring the same physical ohms. Catches errors no single-instrument test can see — units, range scaling, swapped 2-/4-wire, a null with the wrong sign. Tolerances are derived from both datasheets in-file and pinned by hardware-free tests, and the file is explicit that this resolves *gross* errors only: the QR10x's ±0.05% dominates, so the agreement budget (~0.07 Ω at 100 Ω) is wider than the 38 mΩ offset the meter alone can see. The lead-resistance test escapes that limit by differencing 2-wire against 4-wire on the *same* meter, which cancels the QR10x term — that is how it can report 78.9 mΩ meaningfully |
| `test_hardware_cyberpower_pdu41002.py` | PDU41002 | the CLI over both transports against the real device, including the cross-check that only real hardware can do: switch an outlet over SSH, read it back over serial |
| `test_hardware_ontrak_adu218.py` | ADU218 **+** SDM4065A | the only set that uses a second instrument as a **witness**. The ADU218's writes are unacknowledged, so `set_relay_state()` confirms by re-reading the same device — self-consistent by construction, and blind to a driver whose read-back is secretly its own commanded value. The DMM across relay K0 is not the ADU218 talking about itself: a closed contact reads a number, an open one raises the `9.9E37` overload sentinel, and no amount of probe-contact drift can confuse those two. No resistance *threshold* is asserted (the same closed relay measured 6.14–10.69 Ω across sessions, all of it probe seating) — only that shape. Proved able to fail: patching `set_relay_state` to return its own argument without touching the device fails 3 of the 6 |
| `test_mcp_hw.py` | bench | MCP tools against real devices |

### SDM4065A — last hardware run

The two SDM4065A sets have been run against the real meter (firmware
0.0.0.20, serial SDM46A0CA00021):

| Set | Result |
|---|---|
| `test_bench_siglent_sdm4065a.py` | 13 passed / 1 skipped / 0 failed |
| `test_cross_validate_sdm4065a_qr10x.py` | 7 passed / 0 skipped |

The sense leads are attached, so the run set
`BENCHCTRL_SDM4065A_WIRING=4` (also the default) and every
cross-validation test ran. 4-wire (`FRESistance`) is now validated on
silicon, not just against the simulator. The lead-resistance test
printed the number `KNOWN_LIMITATIONS § H-5` was waiting for:

```
measured lead+contact resistance: 78.9 mΩ
(2-wire 100.12209 Ω, 4-wire 100.04321 Ω, QR10x PV 100.03800 Ω)
```

The 4-wire reading lands 5.2 mΩ from the QR10x's own measurement where
the 2-wire reading is 84 mΩ away. 78.9 mΩ is well inside the
datasheet's 0.2 Ω bound, but it is still about 2x the 38 mΩ offset the
cross-validation is trying to resolve — so the standing conclusion
holds: an un-nulled 2-wire read cannot see that offset. The tolerance
constant `TWO_WIRE_LEAD_OHM` deliberately stays at the datasheet's
0.2 Ω; lead resistance is a property of *these* cables and contacts, not
of the meter, and tightening the budget to our measurement would fail
the suite for anyone with longer leads without indicating a driver
defect.

The driver suite's single skip is a different mechanism, and worth
understanding because it is *self-balancing*. Two of its tests are
exact complements on the state of the input terminals:
`test_hw_overload_raises_on_a_deliberately_narrow_range` needs an
**open** input to overload, and
`test_hw_null_now_leaves_auto_disarmed_on_real_hardware` needs a
**connected** one to null against. Whichever condition holds, exactly
one of the two skips and the other runs — so 13 passed / 1 skipped is
the expected shape, not a coverage gap. With the 100 Ω DUT attached it
was the *overload* test that skipped, saying so explicitly: `input read
[100.121975] on the 200 Ω range rather than overloading — something is
connected across the inputs, so this test cannot produce the condition
it checks`. Both skip messages name the input state, so the log says
which case ran.

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

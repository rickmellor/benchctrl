# benchctrl roadmap

What's planned, what's deferred, and why. Entries follow a consistent
format: **status** (current state) + **scope when picked up** (what
landing this would mean) + **why deferred** (the blocker / rationale).

For shipped features, see [`CHANGELOG.md`](CHANGELOG.md). For known
hardware/firmware caps and workarounds, see
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

## Near-term

### Resolve DL3031A LIST timing in Arc Pro high range

**Status**: documented in [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) § F-2.
With the Arc Pro in high range (LiPo profiles, 4.2 V), the DL3031A's
LIST playback fires a partial / misordered sequence instead of the
expected `cycles × n_steps` repeats. CR2032 / CR123A in low range
work cleanly.

**Why deferred**: cause appears to interact with the FUNC:MODE-FIXed
one-way firmware bug (§ F-3). Isolating it cleanly needs an
oscilloscope on the trigger line.

**Scope when picked up**:
1. Scope-capture the DL3031A `:TRIGger` line and the input current
   simultaneously to see what the device actually does at trigger time
   vs LIST playback time
2. If the cause is recoverable in software, build a workaround into
   `run_dynamic_list` (e.g. dummy LIST cycle to "prime" the state
   machine in high range)
3. If not, document it as a hardware limit and move on

### Validation harness: extract sub-second TX bursts from native streaming

**Status**: hires runner captures at ~4 kHz but phase tagging is
host-clock-aligned. Sub-100 ms TX bursts in `--scenario dynamic-list`
work in firmware but phase-tagging accuracy depends on the host
correlating record timestamps with the trigger instant.

**Scope when picked up**:
1. Use the DL3031A's `:TRIGger` as a digital event that the Arc Pro
   can latch on its GPIO line (rear-panel I/O)
2. Embed the trigger instant in the recording's sample stream instead
   of relying on host-clock correlation
3. Achievable phase-tagging accuracy: ±1 sample (~250 µs at 4 kHz),
   vs. ±200 ms today

### Validation harness: DP2031 as a source

**Status**: the DP2031 driver shipped in v1.1.0 with full MCP parity,
but the scenario harness in `scenarios/` still only models the *load*
side. Cell-charging scenarios need the supply as a first-class actor.

**Scope when picked up**:
1. Extend `_LoadAdapter` to also be a `_SourceAdapter`
2. A charge-profile scenario kind alongside static / dynamic /
   dynamic-list
3. Reference captures committed like the existing 27

### Hardware interlock for unattended runs

**Status**: `KNOWN_LIMITATIONS.md § N-1` states the honest position —
the agent's software deadman drives outputs off when contact is lost,
but nothing in software can guarantee an output goes off through a
wedged driver. For genuinely unattended overnight runs that is not
good enough.

The software half has landed: `benchctrl.agent.eventbus` fans events out
without a producer ever touching a socket, and the board's HDMI panel
(`benchctrl.dashboards.fui`, `docs/dashboard.md`) reports bench state
read-only. The **physical** interlock is what remains. An e-stop button
was ordered 2026-08-19 to hang on the board's GPIO; `docs/dashboard.md`
records the four design questions not yet guessed at — chiefly where the
watcher lives so it fires while the agent is blocked in a pyvisa call,
and whether a trip latches until an explicit reset.

**Scope when picked up**:
1. Drive a relay or contactor from the agent host's GPIO on the same
   deadman timer
2. Expose its state in the run's event stream so the artifact bundle
   records whether the interlock ever fired
3. Document the wiring; make it opt-in but loudly recommended in
   `docs/runs.md`

### Revalidate serial transport selection on a desktop Linux host

**Status**: `benchctrl.transports.autoserial` prefers a kernel `ch341`
driver over our userspace one, falling back only where the kernel bound
nothing (`KNOWN_LIMITATIONS.md § N-6`). The **fallback** path is verified on
silicon — QR101A-1M-R1 serial 00000248 on the Uno Q, open → close → reopen,
the reopen proving the USB claim is released rather than leaked. The
**kernel-first** path is not: it is covered by `tests/test_autoserial.py`
(including a mutation check that inverting the precedence fails a test), but
has never run against a host that actually has the module.

**Why deferred**: no host on this bench can exercise it. The Uno Q is built
`# CONFIG_USB_SERIAL_CH341 is not set`, and WSL has no CH340 passed through
to it. This needs a "big iron" Linux host with the QR10x plugged in
directly — not a code change, just hardware we don't currently have on the
bench.

**Scope when picked up**, on a host where `/dev/ttyUSB*` appears for
`1a86:7523`:

1. `resolve_ch341_port(port=None)` returns `how="kernel"` and the tty path,
   and **no pty is created** — the userspace driver must not be touched. Also
   assert no `_benchctrl_bridge` attribute on the driver, since the kernel
   path should carry no bridge machinery.
2. A QR10x round-trip over the kernel tty: `info()` matches what the
   userspace path reports for the same unit (device type, serial, firmware).
   Same instrument, same answers, different transport.
3. Open → close → reopen, as on the Uno Q. The failure this catches is a
   half-released tty rather than a leaked USB claim.
4. `discovery.discover()` reports the adapter via `scan_serial()` with a real
   device path, and `scan_driverless_bridges()` returns `[]` — the
   self-suppression that stops it being double-reported.
5. The negative case, which is the one that matters most: hold the tty open
   from another process, then open through `autoserial`. It must **raise**,
   not fall back to the userspace driver and silently succeed on a different
   transport.
6. Then the hardware suites end to end — `test_bench_qr10x.py` and
   `test_cross_validate_sdm4065a_qr10x.py` — with no port configured, to
   confirm one config genuinely works unmodified on both hosts.

**Also unresolved, and cheap to settle on the same host**: whether
`serial_number=` selection works at all. Our CH340G reports
`iSerialNumber=0` — no serial-number descriptor — so `CH341Device.open(
serial_number=...)` cannot match it and the `index=` path is the only way to
pick among several. Adapters differ here; some CH340 variants do carry one.
Worth confirming on a second adapter before relying on serial selection, and
worth a clearer error than "no CH340 with serial None" if the descriptor is
simply absent. Multi-adapter selection is untested on real hardware either
way — only one CH340 has ever been attached to this bench at a time.

### Transport-layer encryption for remote mode

**Status**: `benchctrl.net` authenticates with HMAC-SHA256
challenge-response, so the token never crosses the wire — but the
traffic itself is plaintext. `docs/remote.md` recommends an SSH
tunnel, which works and is what we use.

**Why deferred**: TLS means certificate management on a board that
someone re-flashes regularly, and the SSH tunnel is a complete answer
for the deployments we have. Worth revisiting if benchctrl ends up on
a network where a tunnel isn't practical.

## Foundation hardening

### Strict mypy

**Status**: `mypy src` runs with `check_untyped_defs = true` but not
strict. CI has it as `continue-on-error: true`.

**Scope when picked up**: Audit the existing `# type: ignore` set,
add return-type annotations on the few functions still missing them,
flip `--strict`. Mostly mechanical.

### Multi-device coordination

**Status**: multiple independent `OtiiArc` instances can already open
on different ports concurrently. Each gets its own thread-safe
protocol session, and as of v1.2 they can live on different machines.
**What's missing**: fan-out helpers (`set_all_main(...)`),
cross-device sync (shared timebase, simultaneous trigger), aggregated
recording.

**Why deferred**: only one Arc Pro available for hardware validation;
designing the API responsibly needs a multi-device rig. Note that
`benchctrl.sim` now makes the *API* design testable without one — a
second simulated Arc costs nothing — so the blocker is narrower than
it was: it is validation, not design.

**Scope when picked up**:
1. Prototype `MultiSMU` against two simulated Arcs
2. Fan `set_*` / `enable_channels` / recording out across devices
3. Decide on timebase strategy (host clock vs designated leader).
   Remote mode makes this harder and more interesting — the honest
   answer may be that cross-machine sync needs a hardware trigger line
4. Acquire a second Arc / Arc Pro and validate
5. End-to-end demo: synchronized capture on 2+ devices

### Project save/load

**Status**: a `Recording` instance can be serialised to a
self-contained `.opensmu` (msgpack-style binary) or `.csv` / `.json`
via `Recording.save_*()`. The official Otii Desktop's `Project` /
`Otii.open_project()` / `Project.save_as()` produces an opaque
server-managed format we don't replicate.

**Why deferred**: their project format is server-internal; opening
project files cross-vendor isn't a priority. The scenario harness in
`scenarios/` covers the "save a captured experiment" use case
cleanly.

### UART log channel parsing

**Status**: the `rx` channel produces text fragments with
per-fragment timestamps. Raw bytes can be retrieved via
`SMU.read_raw()` and parsed manually.

**Why deferred**: needs a focused capture pass to characterize the
wire-level type-0x0003 records.

**Scope when picked up**:
1. Capture USB traffic of the Otii Desktop displaying a UART feed
2. Add a `RxFrame` parser to `protocol.py`
3. Expose via `Recording.uart_messages()` or similar

## Indefinite — hardware-side constraints

### Internal calibration

**Status**: `SMU.calibrate()` raises `BenchNotImplementedError`. The
Otii TCP API's `Arc.calibrate()` sends **zero wire commands** to the
device — the actual calibration flow lives somewhere other than the
documented API (likely the Desktop GUI's service-mode path or a USB
control transfer outside the bulk endpoint).

**Why deferred indefinitely**: calibration writes persistent state to
the device; an incorrect implementation can degrade measurement
accuracy. The wire format has not been observed, and speculative
bytes are too risky.

**Scope when picked up**: Capture the Desktop GUI's calibration flow,
decode the trigger and progress responses, expose with a clear "this
writes to NVM" warning.

### Firmware upgrade

**Status**: `SMU.firmware_upgrade()` raises
`BenchNotImplementedError` and points users at the vendor app.

**Why deferred indefinitely**: bricking risk. The official
`Arc.firmware_upgrade()` ships an image which puts the device in a
bootloader speaking a proprietary protocol. Interrupted or malformed
upload can leave the device unrecoverable without vendor tooling.
Not worth the risk to replicate.

### Channel sample-rate control as a wire command

**Status**: `SMU.set_channel_samplerate()` raises
`BenchNotImplementedError`. The Otii server's
`set_channel_samplerate` errors at the JavaScript layer before any
bytes reach the device — the most plausible interpretation is that
there is no wire command for this, and "sample rate" in the vendor
GUI is a post-processing downsample applied after capture.

**What works today**: client-side downsampling on `Recording` data
via `Recording.downsample(channel, factor)`. Hardware always streams
at native rates (1 kHz subtype-1 / 4 kHz subtype-4) when recording is
enabled.

## Resolved — historical context

(Kept here briefly so future readers see what was deferred and how it
got picked up. Full release notes in [`CHANGELOG.md`](CHANGELOG.md).)

- **v0.1.1**: Full-rate sample streaming — the "start recording" unlock is a per-channel `[seq:u32][0x78][wire_id][1]` command. Native ~4 kHz on MAIN_CURRENT verified.
- **v0.6.0** through **v0.8.0**: Battery profile I/O, life calculator, profiler, and emulator landed in `benchctrl.battery`. Implemented as a host-side stack on top of benchctrl's existing wire vocabulary.
- **v0.9.0** through **v0.9.6**: Bench instrument drivers (QR10x, RigolDL3031A) including firmware-side LIST / transient / battery-discharge modes; MCP server expanded to 93 tools; validation harness with three scenario kinds.
- **v0.9.7**: Adversarial-review fix-batch — propagating-error model in the emulator, parity catch-up, KNOWN_LIMITATIONS.md, several firmware bugs discovered and worked around.
- **v1.0.0**: Driver-symmetric architecture. Package renamed `opensmu` → `benchctrl`; the Arc stopped being a privileged top-level class and became a peer under `drivers/`, with the `SourceMeasurementUnit` Protocol as the contract.
- **v1.1.0**: Rigol DP2031 driver over four phases — the "hardware test for the DP2031 power supply" item from this roadmap. Landed larger than the ~25 tools estimated here: 134 MCP tools covering the Arb timer sequencer, the IoT power analyzer, trigger I/O and the device filesystem.
- **v1.2.0**: Three items that were never on this roadmap but became obvious once the driver surface stabilised — `benchctrl.sim` (wire-protocol simulators, so the whole stack runs hardware-free), `benchctrl.net` + `benchctrl.agent` (remote mode, instruments on one machine and the agent on another), and `agent/runs` (declarative unattended experiments with a durable event log). All three sit behind `session.resolve()`, so the 226 MCP tools were unchanged.

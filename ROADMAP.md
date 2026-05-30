# benchctrl roadmap

What's planned, what's deferred, and why. Entries follow a consistent
format: **status** (current state) + **scope when picked up** (what
landing this would mean) + **why deferred** (the blocker / rationale).

For shipped features, see [`CHANGELOG.md`](CHANGELOG.md). For known
hardware/firmware caps and workarounds, see
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

## Near-term (v0.10 / v1.0)

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

### Hardware test for the DP2031 power supply

**Status**: user has expressed interest in adding a Rigol DP2031
programmable supply for advanced battery emulation (high-V cells the
Arc can't drive, charge profile drives, etc.). SCPI surface is in the
same family as DL3031A.

**Scope when picked up** (~2 days with hardware):
1. New `benchctrl.bench.RigolDP2031` driver modeled on RigolDL3031A
2. CV/CC/CR modes, OVP/OCP, LIST sequence, timer/wave modes
3. MCP parity (~25 tools)
4. Validation harness: extend `_LoadAdapter` to also be a `_SourceAdapter`
   for cell-charging scenarios
5. Hardware-marked tests
6. KNOWN_LIMITATIONS section if firmware quirks shake out

## Foundation hardening

### Strict mypy

**Status**: `mypy src` runs with `check_untyped_defs = true` but not
strict. CI has it as `continue-on-error: true`.

**Scope when picked up**: Audit the existing `# type: ignore` set,
add return-type annotations on the few functions still missing them,
flip `--strict`. Mostly mechanical.

### Multi-device coordination

**Status**: multiple independent `SMU` instances can already open on
different ports concurrently. Each gets its own thread-safe protocol
session. **What's missing**: fan-out helpers
(`set_all_main(...)`), cross-device sync (shared timebase, simultaneous
trigger), aggregated recording.

**Why deferred**: only one Arc Pro available for hardware validation;
designing the API responsibly needs a multi-device rig.

**Scope when picked up**:
1. Acquire a second Arc / Arc Pro
2. Add a `MultiSMU` class that fans `set_*` / `enable_channels` /
   recording out across multiple devices
3. Decide on timebase strategy (host clock vs designated leader)
4. End-to-end demo: synchronized capture on 2+ devices

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

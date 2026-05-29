# OpenSMU roadmap

Features intentionally deferred from v0.1, with rationale and pointers
so the next pass can pick them up cleanly.

## Deferred for v0.3 — Battery emulation

**Blocked at the Otii server (cap #40, 2026-05-29):** the API call
`otii_get_battery_profiles` fails with `"You need a battery toolbox
license for this feature."` — Otii's separate paid product gates the
flow at the server layer before bytes reach the device.

**Path forward (Desktop GUI capture):** the Otii Desktop application
exposes the Battery Toolbox feature directly and speaks the same wire
protocol regardless of automation licensing. Capturing a manual flow
via the GUI with DMS recording in parallel will yield the wire bytes.

**Scope when picked up:**
1. Capture USB traffic while the Otii Desktop GUI runs a full battery
   profile flow (load profile → set SoC → enable emulator → record →
   disable).
2. Decode profile-upload protocol (likely a multi-frame transfer of
   JSON / discharge tables).
3. Add `opensmu.battery` submodule with `BatteryProfile` dataclass and
   `SMU.set_supply_battery_emulator()` that consumes a local profile
   rather than a server UUID.
4. Wire the streamed battery-state samples (new channel id TBD — capture
   will reveal).
5. End-to-end demo: charge-curve replay against a test load.

**Stub:** `SMU.enable_battery_profiling()`, `SMU.set_supply_battery_emulator()`,
`SMU.wait_for_battery_data()` raise `SMUNotImplementedError("battery
emulation deferred — see ROADMAP.md")`. `SMU.set_supply_power_box()` is
host-side cache-only (no wire command needed in default supply mode).

## Deferred for v0.3 — Internal calibration

**Updated status (cap #41, 2026-05-29):** Calling `Arc.calibrate()` via
the Otii TCP API sends **zero wire commands** to the device. The actual
calibration flow lives somewhere other than the documented API — most
likely the Desktop GUI's service-mode path or a USB control transfer
outside the bulk endpoint.

**Why deferred:** Calibration writes persistent state to the device and
an incorrect implementation can degrade measurement accuracy. The wire
format has not been observed, and speculative bytes are too risky.

**Stub:** `SMU.calibrate()` raises `SMUNotImplementedError`.

**Scope when picked up:** Capture the Desktop GUI's calibration flow,
decode the trigger and progress responses, expose with a clear "this
writes to NVM" warning.

## Deferred indefinitely — Firmware upgrade

**Why deferred:** Bricking risk. The official `Arc.firmware_upgrade()`
ships an image to the device which then enters a bootloader. The bootloader
protocol is proprietary, and an interrupted or malformed upload can leave the
device unrecoverable without vendor tooling.

**Stub:** `SMU.firmware_upgrade()` raises `SMUNotImplementedError` and
points the user at the vendor app for firmware updates.

## Architecturally not a wire command — Channel sample-rate control

**Decoded conclusion (cap #42, 2026-05-29):** the Otii server's
`set_channel_samplerate` errors at the JavaScript layer before any bytes
reach the device. The most plausible interpretation is that there is no
wire command for this — the device always streams at hardware-fixed
native rates (1 kHz subtype-1 / 4 kHz subtype-4), and "sample rate" in
the vendor's GUI is a post-processing downsample applied after capture.

**Stub:** `SMU.set_channel_samplerate()` raises `SMUNotImplementedError`.
A future opensmu release may instead implement client-side downsampling
on captured `Recording` data via `Recording.downsample(channel, factor)`,
which already exists.

## Deferred for v0.3 — Multi-device coordination

**Why deferred:** Only one Arc Pro is available for hardware validation.
The opening API (`SMU.discover()`, `SMU.open(port=...)`) already supports
multiple devices, but `Otii.set_all_main()`-equivalent fan-out and
cross-device sync features (shared timebase, simultaneous trigger) need a
multi-device rig to design responsibly.

**What works today:** Multiple independent `SMU` instances can be opened on
different ports concurrently. Each gets its own thread-safe protocol session.

## Deferred for v0.3 — Project save/load

**Why deferred:** The official `Project` / `Otii.open_project()` /
`Project.save_as()` produces an opaque server-managed file format. We do
not have access to that format and creating a competing one isn't a v0.1
priority.

**What works today:** A `Recording` instance can be serialised to a
self-contained `.opensmu` (msgpack-style binary) or `.csv` / `.json` via
`Recording.save_*()`. Loading is symmetrical (`Recording.load()`).

## DONE in v0.1.1 — Full-rate sample streaming

**Resolved 2026-05-29.** Decoded from capture #33 (`33-otii-full-rate.raw`
in the parent usb-sniffer project):

- The "start recording" mechanism is *not* the 76-byte `69 83 2a ff …`
  payload v0.1 sent — that payload was a misread of the device's
  **inbound** packed-sample frame.
- The actual unlock is a per-channel command: `[seq:u32][0x78][wire_id][1]`
  to enable streaming, `…[0]` to disable. Sent once per channel,
  followed by an 8-byte `[seq:u32][0x7C]` cleanup.
- Once enabled, the device delivers a 76-byte packed-sample frame every
  1 ms (1 kHz frame rate). Each frame carries one sample per sub-1
  channel and four packed samples per sub-4 channel — yielding native
  rates of 1 kHz / 4 kHz.

**Verified rates after fix**: mc 4042 sps, mp 4042 sps, mv 1015 sps —
a ~670× improvement on mc/mp and ~170× on mv versus v0.1.

## Deferred for v0.3 — UART log channel parsing

**Why deferred:** The `rx` channel produces a stream of text fragments
with per-fragment timestamps. The wire-level format we observed maps to
type-0x0003 records (TBC) and needs a focused capture pass. Today the
raw bytes can be retrieved via `SMU.read_raw()` and parsed manually.

**Stub:** `SMU.iter_uart_log()` raises `SMUNotImplementedError`.

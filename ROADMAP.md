# OpenSMU roadmap

Features intentionally deferred from v0.1, with rationale and pointers
so the next pass can pick them up cleanly.

## Deferred for v0.2 — Battery emulation

**Why deferred:** The on-wire encoding for battery profiles, SoC, used-capacity,
SoC-tracking, and the runtime emulator state machine has not yet been
reverse-engineered. The official `Arc.set_supply_battery_emulator()` round-trips
a server-side `battery_profile_id` (UUID) — the device-side wire format that
backs it is unknown to us.

**Scope when picked up:**
1. Capture USB traffic while the Qoitech Battery Toolbox runs through a full
   profile (load profile → set SoC → enable → wait for battery data → disable).
2. Decode profile-upload protocol (probably a multi-frame transfer of the JSON).
3. Add `opensmu.battery` submodule with `BatteryProfile` dataclass and
   `SMU.set_supply_battery_emulator()` that mirrors the official API shape
   but consumes a local profile rather than a server UUID.
4. Wire the streamed battery-state samples (channel TBD — capture will reveal).
5. End-to-end demo: charge curve replay against a test load.

**Stub:** `SMU.enable_battery_profiling()`, `SMU.set_supply_battery_emulator()`,
`SMU.wait_for_battery_data()`, `SMU.set_supply_power_box()` raise
`SMUNotImplementedError("battery emulation deferred — see ROADMAP.md")`.

## Deferred for v0.2 — Internal calibration

**Why deferred:** Calibration writes persistent state to the device and an
incorrect implementation can degrade measurement accuracy. The wire format
for calibration trigger + completion notification has not been reverse
engineered, and we don't want to risk the user's hardware on speculative
bytes.

**Stub:** `SMU.calibrate()` raises `SMUNotImplementedError`.

**Scope when picked up:** Capture the Otii GUI's calibration flow, decode the
trigger command and the multi-stage progress responses, expose with a clear
"this writes to NVM" warning.

## Deferred indefinitely — Firmware upgrade

**Why deferred:** Bricking risk. The official `Arc.firmware_upgrade()`
ships an image to the device which then enters a bootloader. The bootloader
protocol is proprietary, and an interrupted or malformed upload can leave the
device unrecoverable without vendor tooling.

**Stub:** `SMU.firmware_upgrade()` raises `SMUNotImplementedError` and
points the user at the vendor app for firmware updates.

## Deferred for v0.3 — Channel sample-rate control

**Why deferred:** The official `Arc.set_channel_samplerate()` triggers a
server-side bug in current Otii server builds (`Cannot read properties of
undefined (reading 'id')`), so we never captured its wire-level command.
The device always streams at the channel's native rate (1 kHz for subtype-1,
4 kHz for subtype-4) by default.

**Stub:** `SMU.set_channel_samplerate()` raises `SMUNotImplementedError`.

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

## Deferred for v0.3 — UART log channel parsing

**Why deferred:** The `rx` channel produces a stream of text fragments
with per-fragment timestamps. The wire-level format we observed maps to
type-0x0003 records (TBC) and needs a focused capture pass. Today the
raw bytes can be retrieved via `SMU.read_raw()` and parsed manually.

**Stub:** `SMU.iter_uart_log()` raises `SMUNotImplementedError`.

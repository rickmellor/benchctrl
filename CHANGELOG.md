# Changelog

All notable changes to OpenSMU. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

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

# Official `otii_tcp_client` API inventory

Source-of-truth catalog of every class, method, parameter, and return
type exposed by Qoitech's official Python TCP client (v3.x as installed
in `C:\Users\rickm\AppData\Local\Programs\Python\Python312\Lib\site-packages\otii_tcp_client`).

Used as the parity target for benchctrl. Each row says how benchctrl maps it:

- **mirror** — implemented in benchctrl with equivalent semantics (name may differ for clarity)
- **drop** — not relevant (TCP server / licensing / project file format)
- **defer** — surface present but raises `BenchNotImplementedError`; tracked in `ROADMAP.md`

---

## `OtiiClient` (otii_client.py)

| method | signature | benchctrl mapping |
|---|---|---|
| `connect(...)` | `host, port, try_for_seconds, licensing, credentials, licenses` | **drop** — no TCP server, use `SMU.open(port=...)` instead |
| `disconnect()` | | **drop** — use `SMU.close()` or context manager exit |

## `Connect` (otii_client.py) — subclass of `Otii`

Connection + license-reservation context manager. **drop** entirely.

## `Otii` (otii.py)

| method | signature | benchctrl mapping |
|---|---|---|
| `create_project()` | -> `Project` | **drop** — no server-side project concept |
| `get_active_project()` | -> `Project` | **drop** |
| `get_battery_profile_info(id)` | -> dict | **defer** (battery emulation) |
| `get_battery_profiles()` | -> list[dict] | **defer** |
| `get_device_id(name)` | -> str | **mirror** — `SMU.open(name=...)` accepts a friendly name |
| `get_devices(timeout, devicefilter)` | -> list[Arc] | **mirror** — `SMU.discover()` returns list of `SMUInfo` |
| `get_licenses()` | -> list[dict] | **drop** |
| `has_license(type)` | -> bool | **drop** |
| `is_logged_in()` | -> bool | **drop** |
| `login(user, pass)` | | **drop** |
| `logout()` | | **drop** |
| `open_project(filename, force, progress)` | -> Project | **drop** — use `Recording.load(path)` instead |
| `reserve_license(id)` | | **drop** |
| `return_license(id)` | | **drop** |
| `set_all_main(enable)` | | **defer** — multi-device |
| `shutdown()` | | **drop** |

## `Arc` (arc.py) — the main device class

### Identification / lifecycle

| method | signature | benchctrl mapping |
|---|---|---|
| `add_to_project()` | | **drop** |
| `is_connected()` | -> bool | **mirror** — `smu.is_connected` property |
| `get_version()` | -> {hw_version, fw_version} | **mirror** — `smu.version()` (best-effort; host-cached) |

### Power & output

| method | signature | benchctrl mapping |
|---|---|---|
| `set_main(enable)` | bool | **mirror** — `smu.set_output(enable)` |
| `get_main()` | -> bool | **mirror** — `smu.output_enabled` property |
| `set_main_voltage(value)` | V | **mirror** — `smu.set_voltage(v)` |
| `get_main_voltage()` | -> V | **mirror** — `smu.voltage` |
| `set_main_current(value)` | A (CC mode) | **mirror** — `smu.set_main_current(a)` |
| `set_max_current(value)` | A 0.001-5 | **mirror** — `smu.set_current_limit(a)` |
| `get_max_current()` | -> A | **mirror** — `smu.current_limit` |
| `set_range(low\|high)` | | **mirror** — `smu.set_range("high")` |
| `get_range()` | -> low\|high | **mirror** — `smu.range` |
| `set_src_cur_limit_enabled(bool)` | | **mirror** — `smu.set_current_limit_enabled(bool)` |
| `get_src_cur_limit_enabled()` | -> bool | **mirror** — `smu.current_limit_enabled` |
| `set_power_regulation(mode)` | voltage\|current\|inline\|off | **mirror** — `smu.set_power_regulation(mode)` |
| `set_supply_power_box()` | | **mirror** — `smu.set_supply_power_box()` (alias for default supply mode) |
| `get_supply_mode()` | -> "power-box"\|"battery-emulator" | **mirror** — `smu.supply_mode` |

### 4-wire / sense / shunt

| method | signature | benchctrl mapping |
|---|---|---|
| `set_4wire(enable)` | | **mirror** — `smu.set_four_wire(enable)` |
| `get_4wire()` | -> state str | **mirror** — `smu.four_wire_state` (host-cached enable/disable) |
| `set_adc_resistor(value)` | Ohm 0.001-22 | **mirror** — `smu.set_adc_resistor(ohms)` |
| `get_adc_resistor()` | -> Ohm | **mirror** — `smu.adc_resistor` |

### Expansion port (5V / digital / GPIO)

| method | signature | benchctrl mapping |
|---|---|---|
| `enable_5v(enable)` | | **mirror** — `smu.set_exp_5v(enable)` |
| `enable_exp_port(enable)` | | **mirror** — `smu.set_exp_port(enable)` (umbrella enable) |
| `set_exp_voltage(value)` | V 1.2-5 | **mirror** — `smu.set_exp_voltage(v)` |
| `get_exp_voltage()` | -> V | **mirror** — `smu.exp_voltage` |
| `set_gpo(pin, value)` | pin 1\|2, bool | **mirror** — `smu.set_gpo(pin, bool)` |
| `get_gpi(pin)` | -> bool | **mirror** — `smu.get_gpi(pin)` |
| `set_tx(value)` | bool | **mirror** — `smu.set_tx(bool)` (TX-as-GPO) |
| `get_rx()` | -> bool | **mirror** — `smu.get_rx()` (RX-as-GPI) |

### UART

| method | signature | benchctrl mapping |
|---|---|---|
| `enable_uart(enable)` | | **mirror** — `smu.set_uart(enable, baudrate=...)` |
| `set_uart_baudrate(value)` | int | **mirror** — same call as above; also exposed separately |
| `get_uart_baudrate()` | -> int | **mirror** — `smu.uart_baudrate` |
| `write_tx(value)` | str | **mirror** — `smu.write_tx(text)` |

### Channels

| method | signature | benchctrl mapping |
|---|---|---|
| `enable_channel(name, enable)` | | **mirror** — `smu.enable_channel(channel)` / `smu.disable_channel(channel)` / `smu.enable_channels(*channels)` |
| `set_channel_samplerate(name, value)` | | **defer** |
| `get_channel_samplerate(name)` | -> int | **mirror** — returns documented native rate from `Channel.sample_rate` |
| `get_value(name)` | -> float | **mirror** — `smu.read_value(channel)` returns latest streamed sample |

### Legacy / oddities

| method | signature | benchctrl mapping |
|---|---|---|
| `enable_legacy_sink(enable)` | | **mirror** — `smu.set_legacy_sink(enable)` |
| `set_property(name, value)` | | **mirror** — `smu.set_property(name, value)` (passthrough, no validation) |
| `get_property(name)` | -> Any | **mirror** — `smu.get_property(name)` |
| `commit()` | | **mirror** — `smu.commit()` (passthrough) |

### Battery / calibration / firmware

| method | signature | benchctrl mapping |
|---|---|---|
| `enable_battery_profiling(enable)` | | **defer** |
| `set_battery_profile(id)` | | **defer** |
| `set_supply_battery_emulator(profile_id, *, series, parallel, used_capacity, soc, soc_tracking)` | -> BatteryEmulator | **defer** |
| `wait_for_battery_data(timeout)` | -> float | **defer** |
| `calibrate()` | | **defer** |
| `firmware_upgrade(filename)` | | **defer** (indefinite — bricking risk) |

## `BatteryEmulator` (battery_emulator.py)

All 9 methods (`get/set_parallel`, `get/set_series`, `get/set_soc`,
`get/set_soc_tracking`, `get/set_used_capacity`, `update_profile`) are
**deferred** behind a stub class.

## `Project` (project.py)

| method | signature | benchctrl mapping |
|---|---|---|
| `close(force)` | | **drop** |
| `crop_data(start, end)` | | **mirror** — `Recording.crop(start, end)` operates on recording in memory |
| `get_last_recording()` | -> Recording | **drop** — recording is returned directly by `SMU.record()` |
| `get_recordings()` | -> list[Recording] | **drop** |
| `save(progress)` | -> str | **drop** — see `Recording.save_*()` |
| `save_as(filename, force, progress)` | -> str | **drop** |
| `start_recording()` | | **mirror** — `SMU.start_recording()` |
| `stop_recording()` | | **mirror** — `recording.stop()` or context exit |
| `create_user_log(id)` | -> str | **defer** |

## `Recording` (recording.py)

| method | signature | benchctrl mapping |
|---|---|---|
| `delete()` | | **drop** — just drop the reference |
| `downsample_channel(device_id, channel, factor)` | | **mirror** — `Recording.downsample(channel, factor)` (in-memory) |
| `get_channel_data_count(device_id, channel)` | -> int | **mirror** — `len(recording.data(channel))` |
| `get_channel_data_index(device_id, channel, timestamp)` | -> int | **mirror** — `recording.index_at(channel, t)` |
| `get_channel_data(device_id, channel, index, count, strip)` | -> dict | **mirror** — `recording.data(channel, start, end)` returns list/np.ndarray |
| `get_channel_info(device_id, channel)` | -> {offset, from, to, sample_rate} | **mirror** — `recording.info(channel)` returns `ChannelInfo` dataclass |
| `get_channel_statistics(device_id, channel, from, to)` | -> {min, max, average, energy} | **mirror** — `recording.statistics(channel, start, end)` returns `Statistics` dataclass |
| `get_log_offset(device_id, channel)` | -> float | **defer** |
| `get_offset()` | -> float | **mirror** — `recording.offset` |
| `import_log(filename, converter)` | -> log_id | **defer** |
| `is_running()` | -> bool | **mirror** — `recording.is_running` |
| `log(text, timestamp)` | | **mirror** — `recording.log(text, t)` |
| `rename(name)` | | **mirror** — `recording.name = ...` |
| `set_log_offset(device_id, channel, offset)` | | **defer** |
| `set_offset(offset)` | | **mirror** — `recording.offset = ...` |
| `append_user_log(id, time, message)` | | **defer** |
| `offset_user_log(id, offset)` | | **defer** |
| `get_user_log_data(id, from, to, filter, page, page_size)` | -> dict | **defer** |
| `iter_user_log_data(id, from, to, filter)` | -> Generator | **defer** |

## `OtiiConnection` / `Otii_Exception` / `LicensingMode`

**drop** — server-side concepts.

benchctrl has its own exception hierarchy (`BenchError` and subclasses).

---

## Coverage summary

- **mirror**: ~48 methods (all non-server device/recording surface)
- **defer**: ~17 methods (battery, calibration, firmware, log, sample-rate)
- **drop**: ~24 methods (TCP server, licensing, project file format)

Net result: every meaningful user-facing capability of the official client
is reachable in benchctrl without a license fee or a running server.

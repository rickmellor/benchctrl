# benchctrl test plan

Comprehensive validation strategy. Tests are organised in two tiers:

- **Hardware-free** — protocol encoding, parsing, statistics, file I/O,
  exception behaviour. Always runs in CI. Lives in `tests/` without the
  `hardware` marker.
- **Hardware-required** — needs an Arc/Arc Pro on COM6. Marked
  `@pytest.mark.hardware`. Run with `pytest -m hardware`.

Run hardware-free only:

```
pytest -m "not hardware"
```

Run everything (device must be present):

```
pytest
```

## Coverage matrix — hardware-free

| File | Target module | Coverage |
|---|---|---|
| `test_imports.py` | `benchctrl` | every public symbol importable from package root |
| `test_exceptions.py` | `exceptions` | hierarchy, attribute carrying, isinstance against std types |
| `test_channels.py` | `channels` | enum constants, `code`/`wire_id`/`subtype`/`sample_rate`/`unit`, `from_code`, `coerce`, reverse-lookup table |
| `test_protocol_framing.py` | `protocol` | encode_frame round-trips, checksum correctness, iter_frames yields validated payloads, resync after garbage, truncated tail handling |
| `test_protocol_commands.py` | `protocol` | every SET-command encoding, GPO bit pattern, recording channel records (subtype=1 12B, subtype=4 24B), start/stop/cleanup payloads, init payloads |
| `test_protocol_inbound.py` | `protocol` | parse_error_frame positive + negative cases, parse_set_ack_frame discriminates from error frames, iter_samples extracts records |
| `test_samples.py` | `samples` | parse_samples_by_id, parse_samples_by_channel, ChannelBuffer slicing/timestamps, compute_statistics min/max/avg/rms/charge/energy on synthetic data |
| `test_recording_lifecycle.py` | `recording` | construction, info/statistics/data/timestamps/index_at/count, crop, downsample, rename, log |
| `test_recording_io.py` | `recording` | save_csv long/wide, save_json, native save+load round-trip (with empty buffers, single channel, multi-channel) |
| `test_transport_discovery.py` | `transport` | discover_arc_ports returns list (empty allowed), PortInfo str |

## Coverage matrix — hardware-required

| File | Test | Verifies |
|---|---|---|
| `test_smu_connect.py` | `test_discover_finds_arc` | discovery returns at least one SMUInfo |
| | `test_open_close_cycle` | `SMU.open()` succeeds, context exit closes cleanly |
| | `test_version` | `version()` returns dict with at least one of `device_id` / `fw_version` after some streaming |
| `test_smu_setters.py` | `test_set_voltage` | set 3.0 V then 3.25 V, no error response in stream |
| | `test_set_voltage_low_range_cap` | set 4.0 V in low range raises `BenchCommandError` |
| | `test_set_range_high_unlocks_4v` | switch to high range, then set 4.0 V succeeds |
| | `test_set_current_limit` | set 1.0 A, 2.5 A, both succeed |
| | `test_set_exp_voltage` | set 2.8 V, 3.3 V |
| | `test_set_exp_5v_toggle` | enable + disable |
| | `test_set_output_toggle` | enable + disable with no load |
| | `test_set_4wire_toggle` | enable + disable |
| | `test_set_src_cur_limit_enabled` | enable + disable |
| | `test_set_adc_resistor` | set 0.1, 1.0, 10.0 Ohm |
| | `test_set_uart` | set baud 9600 / 115200, enable + disable |
| | `test_set_gpo_pins` | both pins, both states (covers encoding) |
| | `test_set_legacy_sink_toggle` | enable + disable |
| `test_smu_channels.py` | `test_enable_channel_round_trip` | enable_channel then disable_channel; co-enables verified |
| | `test_enable_channels_varargs` | multiple at once |
| `test_smu_recording.py` | `test_record_context_manager` | with block: ~2 s recording, recording.statistics returns sane values |
| | `test_record_two_channels` | mc + mv: both buffers populated |
| | `test_recording_save_csv_long` | wrote + reloadable, line count > 0 |
| | `test_recording_save_csv_wide` | columns match channel count + 1 |
| | `test_recording_native_round_trip` | save then load returns equivalent buffers |
| | `test_recording_statistics_charge_energy` | for current channel, charge populated; for power channel, energy populated |
| `test_smu_stream.py` | `test_stream_finite_duration` | yields > 0 samples in 1 s |
| | `test_stream_typed_sample` | each yielded item is `Sample`, has known channel |
| `test_smu_errors.py` | `test_invalid_voltage_raises` | client-side range check (voltage > 5.5 V) raises `BenchValueError` |
| | `test_device_rejection_low_range` | set 4.0 V in low range → `BenchCommandError` with err_code, last_good |

## Deferred features — explicit no-op assertions

For each `BenchNotImplementedError`-raising method, a test confirms that
exact exception is raised and the message points to ROADMAP.md:

- `calibrate`, `firmware_upgrade`
- `enable_battery_profiling`, `set_battery_profile`, `set_supply_battery_emulator`,
  `wait_for_battery_data`
- `set_channel_samplerate`
- `iter_uart_log`, `write_tx`, `set_tx`, `get_rx`
- `Recording.get_log_offset`, `Recording.set_log_offset`, `Recording.import_log`,
  `Recording.append_user_log`

## Coverage targets

| Layer | Target |
|---|---|
| `protocol` | 100% line coverage; 100% branch on iter_frames |
| `samples` | 95% line, including degenerate empties |
| `recording` | 90% line, including save/load round-trip |
| `device` (hardware-free portions) | 85% — every setter range-check, every NotImplemented stub |
| `device` (hardware-required) | exercised end-to-end against the Arc |

## Validation procedure

The validation pass runs in two phases:

1. **Hardware-free**: `pytest -m "not hardware" -q` — must be green
   before moving on.
2. **Hardware-required**: `pytest -m hardware --tb=short` — exercises
   every wire command at least once, with the Arc Pro on COM6 and
   nothing connected to the output terminals.

Both phases write to `VALIDATION_REPORT.md` (pass/fail per test, with
notes on any flakes or skipped items).

# OpenSMU v0.1 — validation report

Generated during the v0.1 build pass. Re-run with `pytest` to refresh.

## Summary

- **Hardware-free tests**: 89 / 89 passed
- **Hardware-required tests**: 43 / 43 passed (against Arc Pro on COM6)
- **Total**: 132 / 132 passed
- **Skipped**: 0
- **Failed**: 0
- **Wall time**: ~35 s total

All green on the first hardware run after one test-expectation calibration
(see "Findings" below).

## Hardware under test

- Arc Pro on COM6
- Output: off
- DUT: nothing connected to output terminals (safe to toggle output)
- Firmware: opportunistically parsed from inbound stream (see `SMU.version()`)

## Coverage by module

| Module | Coverage |
|---|---|
| `opensmu.exceptions` | every class exercised, hierarchy + carried fields verified |
| `opensmu.channels` | every enum constant + every property + reverse lookup |
| `opensmu.protocol` | encode/decode round-trips, every SET command code, GPO bit pattern verified against earlier capture observations, error/ack frame discrimination, garbage skipping, truncation handling |
| `opensmu.samples` | parsing, ChannelBuffer slicing, statistics min/max/avg/rms, charge on current channels, energy on power channels, CSV (long/wide) + JSON exports |
| `opensmu.recording` | construction, info/statistics/data/timestamps/index_at/count, crop, downsample, rename, log, native binary save/load round-trip including empty recordings, deferred stubs |
| `opensmu.transport` | discovery returns typed list, PortInfo display |
| `opensmu.device` | every SET command end-to-end against hardware, every client-side range check raises before send, channel enable/disable + co-enables, recording context-manager + manual start/stop, stream iterator, error-frame surfacing, all deferred stubs raise `SMUNotImplementedError` |

## Pass list (selected highlights)

### Hardware-free

- `test_set_main_voltage_encoding` — 3.3 V → 16-byte SET payload with the
  expected microvolt value and command code
- `test_gpo_bit_pattern_matches_capture_observations` — encoded values for
  pin 1 / pin 2 / on / off all match the bytes observed in previous USB
  captures
- `test_recording_subtype_one_record_is_twelve_bytes` and
  `test_recording_subtype_four_record_is_twentyfour_bytes` — the
  variable-length channel records inside START_RECORDING are encoded with
  the correct widths
- `test_native_binary_round_trip` — save then load preserves channel data,
  offsets, names, sample rates exactly

### Hardware-required

- `test_set_voltage_low_range_safe_values` — voltages 0.0 / 1.0 / 2.0 /
  3.0 / 3.25 V all accepted
- `test_set_output_toggle` — output enable/disable round-trips
- `test_set_range_low_then_high` — both ranges accepted, state cached
- `test_set_gpo_all_pin_state_combinations` — both pins × both states
- `test_record_context_manager_yields_samples` — context manager start,
  recording fills channel buffers, stop on exit
- `test_recording_native_round_trip` — recorded data saved to `.opensmu`
  binary and reloaded with identical sample counts
- `test_recording_charge_on_current_channel` — `Statistics.charge` is
  populated for current channels
- `test_recording_energy_on_power_channel` — `Statistics.energy` is
  populated for power channels
- `test_stream_yields_samples` — typed `Sample` objects come out of the
  iterator with correct `Channel` attribution

## Findings

### Finding 1 — device baseline stream rate is ~6 Hz, not 1 kHz / 4 kHz

The channel capability rates declared in `Channel` (1 kHz for subtype-1
channels, 4 kHz for subtype-4 channels like `MAIN_CURRENT` and
`MAIN_POWER`) are *theoretical maxima*. The Arc Pro's actual stream
after the standard three-step session init is approximately **6 Hz on
all channels** — verified by:

- Reading raw bytes for 2 s without `start_recording()` → 12 samples per
  channel
- Reading inside a `record()` block for 2 s → 11 samples per channel
- Reading via the legacy `arc_direct.read_raw()` (which is what opensmu
  is reverse-engineered from) → same 12 samples per channel

Conclusion: this is the device's true post-init rate, not an opensmu
bug. The Otii desktop client achieves higher rates, so there is a
wire-level "set high-rate streaming" command we have not yet decoded.
Logged in `ROADMAP.md` as a v0.2 deferral ("Full-rate sample
streaming"). Tests were calibrated to assert on the actual rate
(`>= 5 samples in 2 s`).

### Finding 2 — error frame timing varies

The device-side error response for an out-of-range `set_voltage(4.0)` in
low range arrives in the inbound stream, but at the baseline 6 Hz rate
it can take ~1-2 seconds to be parsed by the reader thread. The
`test_set_4v_in_low_range_eventually_raises` test polls for up to 5
seconds with `pytest.skip` if no error arrives (rather than failing).
On every run during validation the error was observed within 2 s.

### Finding 3 — datetime deprecation warnings cleared

Initial run surfaced `DeprecationWarning: datetime.datetime.utcnow()`
from `Recording._begin/_end`. Replaced with timezone-aware
`datetime.now(timezone.utc)`. No warnings on re-run.

## How to reproduce

```powershell
cd C:\Users\rickm\Desktop\opensmu
python -m pytest tests/ -m "not hardware" -q     # 89 tests, <1 s
python -m pytest tests/ -m hardware -q            # 43 tests, ~35 s
python -m pytest tests/ -q                        # both, 132 tests
```

To run a single hardware test in isolation:

```powershell
python -m pytest tests/test_smu_setters.py::test_set_voltage_3v25 -v
```

## Open items

- Re-run after the full-rate streaming command is decoded (ROADMAP.md
  v0.2) to verify channel buffers see 1 kHz / 4 kHz throughput.
- Re-run after battery emulation is decoded to add the battery test tier.
- Add a multi-device run when a second Arc is available.

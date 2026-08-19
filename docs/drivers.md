# Drivers — `benchctrl.drivers`

Every instrument benchctrl can talk to lives under
`benchctrl.drivers.<vendor_model>/`. The Qoitech Otii Arc / Arc Pro
SMU is a peer driver alongside other bench instruments — programmable
loads, programmable resistors, DMMs, DACs, switches. They share
benchctrl's connection patterns and ship MCP tools alongside the Python
API.

Each driver is independent and optional. Import only what you need.

## Currently available

| Driver | Class | Wire stack | Status |
|---|---|---|---|
| Eastwood Tech QR10x programmable resistance | `benchctrl.drivers.eastwood_qr10x.QR10x` | USB-Serial (CH340) | **shipped (v0.9.0)** |
| Rigol DL3031A electronic load | `benchctrl.drivers.rigol_dl3031a.RigolDL3031A` | USB-TMC + SCPI via pyvisa | **shipped (v0.9.3)** |
| Rigol DP2031 triple-output programmable PSU | `benchctrl.drivers.rigol_dp2031.RigolDP2031` | USB-TMC + SCPI via pyvisa | **shipped (v1.1.0)** |
| Siglent SDM4065A 6½-digit bench DMM | `benchctrl.drivers.siglent_sdm4065a.SiglentSDM4065A` | USB-TMC + SCPI via pyvisa | **shipped (unreleased)** |

## QR10x — programmable resistance

[Eastwood Tech QR100/QR101](https://www.eastwood.tech) — pocket
programmable resistance substitution box, 1 Ω to 8.4 MΩ depending on
model, ±0.02% to ±0.05% accuracy. Talks AT commands over USB-Serial.

### Quick start

```python
from benchctrl.drivers.eastwood_qr10x import QR10x

with QR10x.open("COM7") as qr:
    print(qr.info())                  # device identity
    qr.set_safety_limit(12.0)         # device-enforced minimum (Ω)
    qr.set_resistance(10_000)         # set 10 kΩ
    print(qr.actual_resistance())     # PV: what the device actually achieved
```

### Safety

The QR10x has a built-in `RLIMIT` (safety minimum-resistance limit)
that clamps any setpoint at or above the configured value. Set this
before driving real loads:

| Source voltage | 1 W safe minimum R |
|---|---|
| 3.2 V (AA pair) | ~12 Ω |
| 3.7 V (Li-ion) | ~14 Ω |
| 5.0 V (USB) | ~25 Ω |
| 12 V | ~150 Ω |

Power = V² / R; below 1 W keeps within the device's continuous rating.

### API

| Method | Returns | Wire form |
|---|---|---|
| `QR10x.open(port, baudrate=115200)` | `QR10x` | opens pyserial port |
| `qr.close()` / `qr.is_open` | — / bool | |
| `qr.info()` | `QR10xInfo` | `AT+DEV.TYPE?`/`SN?`/`HW?`/`FW?`/`PROD?`/`TCR?` |
| `qr.set_resistance(ohms)` | dict (state dump) | `AT+USER.SP=<float>` |
| `qr.get_setpoint()` | float (Ω) | `AT+USER.SP?` |
| `qr.actual_resistance()` | float (Ω) | `AT+USER.PV?` |
| `qr.set_safety_limit(ohms)` | dict | `AT+USER.RLIMIT=<float>` |
| `qr.get_safety_limit()` | float (Ω) | `AT+USER.RLIMIT?` |
| `qr.get_temperature()` | float (°C) | `AT+USER.T_SENSOR?` |
| `qr.incr(delta_ohm)` | dict | `AT+USER.SP+=<float>` |
| `qr.decr(delta_ohm)` | dict | `AT+USER.SP-=<float>` |

### MCP tools

Per the SDK ↔ MCP parity principle, every public method has a matching
MCP tool. Only one QR10x connection at a time — the server holds it
across tool calls until `qr10x_close()`.

- `qr10x_open(port="COM7", baudrate=115200)` — connect
- `qr10x_close()` — disconnect
- `qr10x_info()` — identity
- `qr10x_set_resistance(ohms)` / `qr10x_get_setpoint()` / `qr10x_actual_resistance()`
- `qr10x_set_safety_limit(ohms)` / `qr10x_get_safety_limit()`
- `qr10x_get_temperature()`
- `qr10x_incr(delta_ohm)` / `qr10x_decr(delta_ohm)`

### Wire format reference

Per the QR10x spec:

- 115200 baud, 8 data bits, no parity, 1 stop bit
- Commands terminated with `\r` or `\n`
- Responses use `+KEY=VALUE` form or `+OK.` for ack
- SET commands return a multi-line state dump:
  ```
  +OK.
  SP(R)=100.0
  PV(R)=100.038
  UMax(V)=12.9
  RLimit(R)=12.0
  InnerT(C)=24.72
  ```
- No documented end-of-response marker; the driver infers
  end-of-burst from a ~60 ms quiet window.

### Installation

The QR10x driver uses `pyserial` only — already a base benchctrl
dependency. No extras required:

```bash
pip install benchctrl        # QR10x driver included
```

SCPI/VISA-based instruments live under the `bench-visa` extra:

```bash
pip install benchctrl[bench-visa]   # adds pyvisa
```

PyVISA needs a backend. Options:
- **Rigol Ultra Sigma** — bundles a working VISA backend; install once and pyvisa picks it up automatically. Easiest path on Windows.
- **NI-VISA** (free from ni.com)
- **Keysight IO Libraries** (free)
- **pyvisa-py** (pure Python) + a USB backend like libusb (per-device Zadig setup on Windows)

## Rigol DL3031A — electronic load

[Rigol DL3000 series](https://www.rigolna.com/products/dc-electronic-load-dl3000-series/)
SCPI-controlled programmable DC electronic load. The DL3031A sinks up
to 150 V / 60 A / 350 W and operates in CC / CV / CR / CP modes plus
built-in transient, LIST sequence, and battery-discharge test modes.

### Quick start

```python
from benchctrl.drivers.rigol_dl3031a import RigolDL3031A

with RigolDL3031A.open() as load:        # auto-discover by USB VID/PID
    print(load.info())
    load.reset()
    load.set_mode("CC")                  # constant-current mode
    load.set_current_range(6.0)          # low range = 6 A
    load.set_current(0.030)              # 30 mA
    load.set_input(True)
    print(load.measure_voltage(), "V")
    print(load.measure_current(), "A")
    load.set_input(False)
```

### Safety

The DL3031A is a real load — verify your DUT can deliver / withstand
the configured setpoint and limits before calling `set_input(True)`.
The driver doesn't enforce DUT-side ratings. The `__exit__` of the
context manager calls `set_input(False)` automatically so an unhandled
exception won't leave the load sinking current.

### API

| Method | Returns | Wire form |
|---|---|---|
| `RigolDL3031A.open(resource=None)` | `RigolDL3031A` | opens a VISA session; auto-discovers Rigol USB VID/PID if `resource` omitted |
| `load.close()` | — | |
| `load.info()` | `RigolDLInfo` | `*IDN?` |
| `load.reset()` / `load.clear_status()` | — | `*RST` / `*CLS` |
| `load.last_error()` | `Optional[tuple[int, str]]` | `:SYSTem:ERRor?` |
| `load.set_mode("CC"\|"CV"\|"CR"\|"CP")` | — | `:SOURce:FUNCtion <mode>` |
| `load.get_mode()` | `str` (CC/CV/CR/CP) | `:SOURce:FUNCtion?` |
| `load.set_input(on)` / `load.get_input()` | — / bool | `:SOURce:INPut:STATe` |
| `load.set_current(A)` / `load.get_current()` | — / float | `:SOURce:CURRent:LEVel:IMMediate` |
| `load.set_voltage(V)` / `load.get_voltage()` | — / float | `:SOURce:VOLTage:LEVel:IMMediate` |
| `load.set_resistance(Ω)` / `load.get_resistance()` | — / float | `:SOURce:RESistance:LEVel:IMMediate` |
| `load.set_power(W)` / `load.get_power()` | — / float | `:SOURce:POWer:LEVel:IMMediate` |
| `load.set_current_range(A)` | — | `:SOURce:CURRent:RANGe` |
| `load.set_voltage_range(V)` | — | `:SOURce:VOLTage:RANGe` |
| `load.set_slew(A/µs)` | — | `:SOURce:CURRent:SLEW:BOTH` |
| `load.measure_voltage()` | float (V) | `:MEASure:VOLTage:DC?` |
| `load.measure_current()` | float (A) | `:MEASure:CURRent:DC?` |
| `load.measure_power()` | float (W) | `:MEASure:POWer:DC?` |
| `load.measure_resistance()` | float (Ω) | `:MEASure:RESistance:DC?` |
| `load.measure_all()` | dict | four sequential `:MEASure:*` (each ~200 ms NPLC integration) |
| `load.fetch_voltage/current/power/resistance/all()` | float / dict | `:FETCh:*:DC?` — non-blocking reads of the device's continuously-updated register; ~1 ms each |

### Firmware modes (v0.9.6+)

The DL3000 series has several built-in subsystems that execute in
firmware with deterministic timing. The driver wraps each:

#### LIST mode — programmable step sequence

```python
load.program_list(
    steps=[(0.0001, 1.0), (0.030, 0.050), (0.0001, 1.0)],  # (level, width_s)
    mode="CC", count=3, range_value=6.0,
    slew_A_per_us=0.5, end_behavior="LAST",
    trigger_source="BUS",
)
load.set_input(True)
load.trigger_now()          # BUS trigger fires the sequence
```

Step widths from 50 µs to 3600 s. Up to 513 total steps. The device
runs the sequence internally — no USB-TMC round-trip per step. The
right tool for sub-100 ms TX bursts and other transients where
host-driven setpoint changes can't keep up.

Granular SCPI wrappers also available: `list_set_mode` /
`list_set_range` / `list_set_count` / `list_set_step_count` /
`list_set_step` / `list_set_slew` / `list_set_end`.

#### CC transient mode — A↔B pulse generator

```python
load.configure_transient_pulse(
    a_level_A=0.030, b_level_A=0.0001,
    a_width_s=0.050, b_width_s=1.0,
    mode="CONTinuous",          # or PULSe / TOGGle
)
load.transient_enable(True)
load.trigger_now()
```

CONTinuous = periodic A↔B stream, PULSe = single A pulse on trigger,
TOGGle = alternate A/B on each trigger.

#### Battery discharge mode — built-in test

```python
load.configure_battery_test(
    current_A=0.050,            # discharge current
    v_stop_V=2.7,                # cell-voltage cutoff
    capacity_stop_mAh=200.0,     # capacity cap
    time_stop_s=3600,            # wall-clock cap
    range_A=6.0,
)
load.set_input(True)
# poll while discharging
while load.get_input():
    stats = load.battery_stats()
    # {capacity_mAh, energy_Wh, discharge_time_s, voltage_V, current_A}
```

Firmware tracks capacity, energy, and discharge time; stops on any
configured cutoff. Useful for real-cell characterization or pack
acceptance tests without writing host-side integration.

#### Trigger system

| Method | Wire form |
|---|---|
| `set_trigger_source({BUS\|EXTernal\|MANUal})` | `:TRIGger:SOURce` |
| `get_trigger_source()` | `:TRIGger:SOURce?` |
| `trigger_now()` | `:TRIGger` (software trigger) |

#### Function mode

`set_function_mode({FIXed\|LIST\|WAVe\|BATTery\|OCP\|OPP})` switches
the top-level regulation source. Set implicitly by `program_list` /
`configure_*` helpers; rarely needs to be called directly.

### MCP tools

Per parity, every public method has a matching MCP tool. The server
holds one DL3031A connection across tool calls until `dl3031a_close()`.

**Core (v0.9.3):**
- `dl3031a_open(resource=None)` / `dl3031a_close()`
- `dl3031a_info()` / `dl3031a_reset()` / `dl3031a_last_error()`
- `dl3031a_set_mode(mode)` / `dl3031a_get_mode()`
- `dl3031a_set_input(on)` / `dl3031a_get_input()`
- `dl3031a_set_current(A)` / `dl3031a_set_voltage(V)` / `dl3031a_set_resistance(Ω)` / `dl3031a_set_power(W)`
- `dl3031a_set_current_range(A)` / `dl3031a_set_voltage_range(V)` / `dl3031a_set_slew(A/µs)`
- `dl3031a_measure()` — `:MEAS:*` (200 ms integration each)

**Firmware modes (v0.9.6):**
- `dl3031a_fetch()` — `:FETCh:*` (non-blocking, fast)
- `dl3031a_set_function_mode(mode)` / `dl3031a_get_function_mode()`
- `dl3031a_program_list(steps, mode, count, range_value, slew_A_per_us, end_behavior, trigger_source)`
- `dl3031a_configure_transient_pulse(a_level_A, b_level_A, a_width_s, b_width_s, mode)`
- `dl3031a_transient_enable(on)`
- `dl3031a_configure_battery_test(current_A, v_stop_V, capacity_stop_mAh, time_stop_s, von_V, range_A)`
- `dl3031a_battery_stats()`
- `dl3031a_set_trigger_source(source)` / `dl3031a_trigger()`

Total: 25 DL3031A MCP tools.

### Manual-misread bugs ironed out in v0.9.6

For anyone diving into the DL3000 programming guide — three places
where the manual is misleading and the driver compensates:

- **`:SOUR:LIST:STEP N` is highest-index, not total count.** A
  3-step list sends `:SOUR:LIST:STEP 2`. `list_set_step_count`
  accepts the **total** and subtracts 1 internally.
- **`:SOUR:LIST:SLEW <step>,<value>` is per-step, not global.**
  `program_list` applies the same `slew_A_per_us` to every step
  when provided.
- **`:SOUR:LIST:END` accepts `LAST|OFF`,** not `NORMal|LAST` as
  the manual suggests in passing.

### DL3031A vs. QR10x

Both serve as "the load" in our battery-emulator validation harness.
Pick whichever fits the test:

| | QR10x | DL3031A |
|---|---|---|
| Resistance range | 1 Ω – 1 MΩ (QR101A-1M-R1) | ~0.1 Ω – 7.5 kΩ effective in CR |
| Switching | mechanical relays, 30–95 ms | electronic, ≤ ms |
| Sub-ms transients | no | yes |
| Native current mode (CC) | no | yes |
| Power rating | 1–2 W (depends on R) | 350 W |
| Voltage rating | 200 V | 150 V |
| Built-in safety limit | RLIMIT (min R) | none on the wire; use `set_*_range` |
| Cost | $$ | $$$$ |

## Rigol DP2031 — triple-output programmable PSU

[Rigol DP2031](https://www.rigolna.com) — triple-output linear DC
power supply (CH1/CH2 0–32 V × 0–3 A, CH3 0–6 V × 0–5 A, 222 W total),
with built-in OVP/OCP per channel, channel-pair SERies/PARallel
modes, tracking, a 1–512 step arbitrary-waveform Timer sequencer,
power-energy Analyzer, four programmable digital trigger lines, and
internal/USB filesystem for state save/recall.

This driver speaks USB-TMC via pyvisa. LAN / RS232 / GPIB are
deliberately out of scope.

### Quick start

```python
from benchctrl.drivers.rigol_dp2031 import RigolDP2031, DP2031Channel

with RigolDP2031.open() as psu:        # auto-discover by Rigol VID + DP2000 PID
    print(psu.info())
    psu.reset()

    # Configure CH1 with safe envelope + OVP
    psu.set_voltage(DP2031Channel.CH1, 3.3)
    psu.set_current(DP2031Channel.CH1, 0.5)
    psu.set_ovp_level(DP2031Channel.CH1, 4.0)
    psu.set_ovp_enabled(DP2031Channel.CH1, True)

    psu.set_output(DP2031Channel.CH1, True)
    m = psu.measure_all(DP2031Channel.CH1)
    print(f"V={m['voltage_V']:.3f} I={m['current_A']*1000:.2f} mA")
    psu.set_output(DP2031Channel.CH1, False)
```

The `with` context manager disables all three outputs on exit. To
leave an output enabled across a script's lifetime, open without the
context manager (`psu = RigolDP2031.open()` / `psu.close()`).

### Safety

- Each channel's output drives voltage onto its terminals immediately
  when `set_output(ch, True)` is called. Confirm DUT-side ratings and
  arm OVP/OCP before enabling.
- **Channel pair SERies** ties CH1+ to CH2- internally for up to 64 V
  composite. **PARallel** ties CH1 and CH2 in parallel for up to 6 A
  composite. Disconnect any external load between CH1 and CH2 before
  switching pair modes. PAIR state survives `*RST` — see
  `KNOWN_LIMITATIONS.md § F-3.5`.
- The Timer (Arb sequencer) can drive arbitrary V/I patterns at
  millisecond timing. `program_timer()` pre-validates every step
  against the channel envelope, but doesn't know about your DUT's
  ratings.
- **Remote sense** (`set_remote_sense(ch, on=True)`) with floating
  sense leads will drive the output toward OVP — wire the sense leads
  to the DUT before enabling.

### API

Surface organised by subsystem:

| Subsystem | Methods (representative) |
|---|---|
| Identity / housekeeping | `info`, `reset`, `clear_status`, `last_error`, `raise_if_error`, `self_test`, `installed_options`, `scpi_version` |
| Channel selection | `select_channel`, `current_channel` |
| Source setpoints | `set_voltage`, `get_voltage`, `set_current`, `get_current`, `set_voltage_step`, `step_voltage_up/down`, `set_current_step`, `step_current_up/down` |
| Apply / bounds | `apply`, `query_applied`, `voltage_bounds`, `current_bounds` |
| Output enable | `set_output`, `get_output`, `set_output_all`, `output_regulation` |
| Protection — OVP | `set_ovp_level/enabled`, `get_ovp_level/enabled`, `ovp_tripped/questionable`, `clear_ovp` |
| Protection — OCP | `set_ocp_level/enabled/delay_ms`, `get_ocp_level/enabled/delay_ms`, `ocp_tripped/questionable`, `clear_ocp` |
| Measurement | `measure_voltage`, `measure_current`, `measure_power`, `measure_all`, `measure_all_channels` |
| Channel topology | `set_channel_pair`, `get_channel_pair`, `set_tracking`, `set_track_mode`, `set_output_sync`, `set_remote_sense`, `set_sampling_mode` |
| Status registers | `event_status_register`, `status_byte`, `*ESE`/`*SRE` set/get, `operation_event`, `questionable_event`, `channel_status_event`, `health_check` |
| OPC + state save | `wait_op_complete`, `mark_op_complete`, `wait`, `save_state`, `recall_state` |
| Timer (Arb sequencer) | `program_timer`, `set_timer_enabled/channel/cycles/end_state/run_mode/trigger`, `set_timer_group_index/params`, `get_timer_group_params` (block-format parse), `delete_timer_groups`, template subsystem (`set_timer_template`/`construct_timer_from_template`/etc.) |
| Analyzer | `set_analyzer_enabled/type/common_objects/save/save_path` |
| Trigger I/O (D1–D4) | `set_trigger_in_enabled/type/source/response`, `trigger_in_immediate`, `set_trigger_out_enabled/source/polarity` |
| Memory / filesystem | `list_files`, `change_directory`, `current_directory`, `store_file`, `load_file`, `delete_file`, `external_disks`, `file_exists`, `set_file_locked` |
| System | beeper, brightness, locks (keyboard/touchscreen/RW), language, power-on mode, screen-saver, `set_remote`/`set_local` |
| License + screenshot | `install_license`, `screenshot_bytes`, `save_screenshot` |

### Timer example — IoT-pattern Arb sequence

```python
from benchctrl.drivers.rigol_dp2031 import RigolDP2031

with RigolDP2031.open() as psu:
    # Define a 3-step sleep / wake / TX pattern on CH3
    steps = [
        (3.0, 0.001, 0.500),   # sleep — 3.0 V, 1 mA limit, 500 ms
        (3.0, 0.500, 0.100),   # active — same V, 500 mA budget, 100 ms
        (3.0, 0.030, 0.050),   # TX — 30 mA peak, 50 ms
    ]
    psu.program_timer(3, steps, cycles=5, end_state="OFF",
                      run_mode="CONTinue", trigger="MANual")
    psu.set_output(3, True)
    psu.set_timer_enabled(True)  # runs entirely in device firmware
    # ... wait for the pattern to complete ...
    psu.set_timer_enabled(False)
    psu.set_output(3, False)
```

### Bench-discovered firmware quirks

Tested against firmware 01.00.01.00.16. See
`KNOWN_LIMITATIONS.md § F-3.5` for the full list and `bugs/` for
reproductions ready to file with Rigol. Highlights:

- `:OUTPut:PAIR` state survives `*RST` — the driver's fixture-level
  reset path handles this; user code should call
  `set_channel_pair("OFF")` explicitly after `reset()`.
- `:OUTPut:PAIR PARallel` write-then-query returns stale `OFF` for
  ~1 s before the mode transition completes — wait ≥ 2 s if you
  verify via `get_channel_pair()`.
- OVP latch settles ~150–250 ms after the over-voltage condition.
- `:OUTPut:OVP:CLEar` clears the latch but does NOT re-enable the
  output (deliberate driver choice — the `:SOURce:VOLT:PROT:CLEar`
  form does re-enable).
- `:ANALyzer:COMMon:MEASure:TYPE` write triggers
  `VI_ERROR_SYSTEM_ERROR` over USB-TMC — firmware defect. The SDK
  method is per-spec but unusable on this firmware.

## Siglent SDM4065A — 6½-digit bench DMM

[Siglent SDM4065A](https://www.siglent.eu) — 6½-digit dual-display
bench multimeter. DC/AC volts and amps, 2- and 4-wire resistance,
capacitance, frequency, period, continuity, diode and temperature.

This is the first *measurement-only* driver in the tree: it sources
nothing, so unlike the load and the supply there is no output an agent
can accidentally energise. The failure mode to guard against is a
**plausible wrong number**, and the API is shaped around that.

USB-TMC via pyvisa. LXI also works — pass a `TCPIP::` resource string.

### Quick start

```python
from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A

with SiglentSDM4065A.open() as dmm:     # auto-discover by Siglent VID + PID
    print(dmm.info())

    # 4-wire, pinned to the 200 Ω range — see "Getting the number right"
    print(dmm.measure_resistance_4wire(200))

    print(dmm.measure_dc_voltage(2))    # 2 V range
```

### Getting the number right

Three manual-documented behaviours make the obvious call sequence the
wrong one. All three are handled by the driver, but they shape how you
should use it.

**`MEASure:<fn>?` is `CONFigure` + `READ?` in one command.** That
means it *reconfigures* before it triggers: this function's NPLC, null
state, null value and range all go back to defaults. So

```python
dmm.null_now()                  # install a null offset
dmm.measure_resistance(200)     # ← silently discards it
```

does not do what it looks like. Use `read()` — which triggers without
reconfiguring — or `read_nulled()`, which raises rather than quietly
returning an un-nulled number:

```python
dmm.configure_resistance(200)   # configure once
dmm.set_nplc(100)               # then set integration time
dmm.null_now(samples=3)         # then null, with leads shorted
dmm.read_nulled()               # trigger, keeping all of the above
```

**The default resistance range is 2 kΩ, not autorange** (§7.4.5 of the
remote manual). A 100 Ω DUT lands on the 2 kΩ range silently, which
costs a factor of ten on the percent-of-range accuracy term. Pass the
range explicitly whenever accuracy matters.

**Enabling null arms automatic null-value selection.** Writing
`NULL:STATe ON` sets `NULL:VALue:AUTO`, so the instrument overwrites
your offset with its own next reading. The order must be state first,
then value — writing a value disarms AUTO. `null_now()` sequences this
correctly and returns the offset the instrument actually stored, read
back rather than assumed.

### 2-wire vs 4-wire

2-wire resistance carries lead and contact resistance in series —
about 0.2 Ω per the datasheet. At 100 Ω that is a 0.2 % error, which
swamps anything the meter's own accuracy spec would contribute. Either
use `measure_resistance_4wire()` with sense leads, or short the leads
and `null_now()` first. For a 4½-digit reading of a 100 Ω part, 2-wire
without a null is not a measurement.

### Validating against a second instrument

`tests/test_cross_validate_sdm4065a_qr10x.py` measures one physical
resistance with both the SDM4065A and the QR10x. This catches the class
of bug no single-instrument test can: a units error, a range
mis-scaling, a swapped 2-/4-wire function, a null applied with the
wrong sign — all of which produce self-consistent readings.

```bash
export BENCHCTRL_SDM4065A=auto
export BENCHCTRL_QR10X_PORT=/dev/ttyUSB0
export BENCHCTRL_SDM4065A_WIRING=4      # 4-wire: sense leads connected
pytest -m hardware tests/test_cross_validate_sdm4065a_qr10x.py -q -s
```

`WIRING=4` is the primary path; `=2` makes the tests null first and
skip the comparisons that need 4-wire. `-s` surfaces the measured
lead-and-contact resistance the lead test prints.

Be clear about what the pair proves. The QR10x's ±0.05% dominates the
meter's ±(0.010% + 0.005%), so **agreement** between the two is
budgeted at roughly 0.07 Ω at 100 Ω — wider than the ~38 mΩ offset the
QR101A-1M-R1 actually shows. The meter *alone* (0.02 Ω on the 200 Ω
range) resolves that offset. So: use the pair to catch gross errors,
and the meter to measure. The test file derives both budgets from the
datasheets and pins them with hardware-free tests, so a tolerance
cannot be quietly loosened.

All tolerances are **linear sums**, not root-sum-square. RSS is for
independent random errors; these are specification bounds, and two
instruments each permitted ±X can legitimately sit 2X apart — RSS would
fail on conforming hardware.

### Model-family traps

One manual covers the SDM4045A / SDM4055A / SDM4065A, and the columns
differ in ways that produce a working driver reporting wrong numbers:

| | SDM4065A (this driver) | SDM4055A |
|---|---|---|
| Top resistance range | **1 MΩ** | 2 MΩ |
| NPLC values | 100 / 10 / 1 / 0.1 / 0.01 / 0.001 | 10 / 1 / 0.01 |
| Resistance autozero | yes, **defaults off** (§7.4.7) | not available |

Both range and NPLC are validated against the 4065A column, and the
rejection message names the sibling model so a wrong-model constant
reads as a wrong model rather than a broken driver.

### Overload

An out-of-range input returns `9.9E37`. The driver raises
`SDM4065AOverloadError` instead of returning it — it is a valid float
and would otherwise propagate into arithmetic as a believable reading.
The exception carries the function and range, so a caller can widen
and retry:

```python
from benchctrl.drivers.siglent_sdm4065a import SDM4065AOverloadError

try:
    r = dmm.measure_resistance_4wire(200)
except SDM4065AOverloadError:
    r = dmm.measure_resistance_4wire(2000)
```

This is a fifth exception type where every other driver has four
(Connection / Command / Timeout / Value). It exists because "the input
exceeded the range" is a distinct *recoverable* condition, not a
protocol failure.

### API

| Subsystem | Methods |
|---|---|
| Identity / housekeeping | `info`, `reset`, `clear_status`, `self_test`, `last_error`, `raise_if_error` |
| Function select | `set_function`, `get_function` |
| One-shot measurement | `measure_dc_voltage`, `measure_ac_voltage`, `measure_dc_current`, `measure_ac_current`, `measure_resistance`, `measure_resistance_4wire`, `measure_capacitance`, `measure_frequency`, `measure_period`, `measure_continuity`, `measure_diode`, `measure_temperature` |
| Configure + trigger | `configure_resistance`, `configure_dc_voltage`, `get_configuration`, `read`, `read_nulled`, `initiate`, `fetch`, `abort`, `set_sample_count`, `get_sample_count` |
| Accuracy controls | `set_nplc`, `get_nplc`, `set_range`, `get_range`, `set_autorange`, `set_autozero`, `get_autozero`, `reading_timeout_ms` |
| Null ("Ref") | `null_now`, `set_null`, `get_null`, `set_null_value`, `get_null_value`, `set_null_auto`, `get_null_auto` |
| Temperature | `set_temperature_unit`, `get_temperature_unit` |
| Escape hatch | `write`, `query`, `query_float`, `query_floats` |

`read()` and `fetch()` always return `list[float]`, one entry per
`sample_count`. A scalar return would silently keep only the first
sample when a caller raised the count.

### Long integrations and the VISA timeout

`open()` defaults to a 10 s timeout, which covers a single reading at
any integration time. A burst does not: 100 PLC × 10 samples at 50 Hz
mains is 20 s of integration alone. `reading_timeout_ms()` does the
arithmetic:

```python
ms = SiglentSDM4065A.reading_timeout_ms(nplc=100, samples=10)
dmm = SiglentSDM4065A.open(timeout_ms=ms)
```

It assumes **50 Hz** mains unless told otherwise, because assuming
60 Hz underestimates the time a 50 Hz instrument takes and produces a
timeout that looks like a dead instrument.

### MCP tools

49 tools, prefixed `sdm4065a_`. There is no confirmation-argument tool
here — nothing to arm. The tools that affect accuracy (range, NPLC,
null) say so in their docstrings, which is what the model reads before
calling them.

### Note on SCPI headers

Siglent's manual writes headers **without** the leading root colon
(`SYSTem:ERRor?` where Rigol writes `:SYSTem:ERRor?`). Both forms work
on the instrument. The simulator accepts both deliberately: matching
only the colon-prefixed form would make error queries fall through to
the generic register model and answer `0`, i.e. a permanently clean
error queue — the one failure a driver cannot detect for itself.

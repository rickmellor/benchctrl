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
| CyberPower PDU41002 8-outlet switched PDU | `benchctrl.drivers.cyberpower_pdu41002.CyberPowerPDU41002` | vendor CLI over USB-Serial (FTDI) **or** SSH | **shipped (unreleased)** — switches mains |
| Silicon Labs CP2112 GPIO control lines | `benchctrl.drivers.silabs_cp2112.CP2112` | USB HID feature reports over `hidraw` | **shipped (unreleased)** — open-drain reset lines |

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

### Which port — let it choose

`QR10x.open()` takes a port, but on a host whose kernel lacks `ch341`
(Arduino's Uno Q) the CH340 bridge binds no driver and there *is* no
`/dev/ttyUSB*` to name. `benchctrl.transports.autoserial` decides for you:

```python
from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.transports.autoserial import open_serial_driver

qr = open_serial_driver(QR10x.open, port=None)   # or port="auto"
```

Precedence, fixed:

| Condition | Transport |
|---|---|
| a port is named explicitly | that port, nothing probed |
| a kernel tty exists for `1a86:7523` | that tty |
| a CH340 enumerates with no tty | userspace CH341 driver → pty |

The kernel driver wins where it exists: it is battle-tested, survives
suspend/resume, and costs no Python thread. The userspace driver is a
workaround, and a workaround should not win by default. The same config
therefore works on desktop Linux and on the Uno Q, and a host that later
gains a `ch341` module starts using it with no config change.

A failed open on a kernel tty **raises rather than falling back** — see
`KNOWN_LIMITATIONS § N-6`. The agent and the `qr10x_open` MCP tool both
route through this, so `port` defaults to `"auto"` there too.

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

**The resistance range after a reset is not pinned.** §7.4.5 of the
remote manual says the default is 2 kΩ; on firmware 0.0.0.20 `*RST`
leaves autoranging *on* with `RANGe?` reporting 200 Ω, and
`CONFigure:RESistance DEF` selects 2 kΩ but still leaves autoranging
on where `RESistance:RANGe DEF` turns it off. Either way an unpinned
range can cost a factor of ten on the percent-of-range accuracy term,
and `RANGe?` alone cannot tell you whether the range is stable — read
`RANGe:AUTO?` alongside it, which `get_autorange()` does. Pass the
range explicitly whenever accuracy matters. Written up as
[vendor issue 4](vendor-issues/SDM4065A-firmware-bug-report-4-range-defaults.md).

**Enabling null arms automatic null-value selection.** Writing
`NULL:STATe ON` sets `NULL:VALue:AUTO`, so the instrument overwrites
your offset with its own next reading. The order must be state first,
then value. §7.4.3 says writing a value disarms AUTO; on firmware
0.0.0.20 it does not, so `null_now()` clears AUTO explicitly afterwards
rather than assuming — [vendor issue 1](vendor-issues/SDM4065A-firmware-bug-report-1-null-value-auto.md).
It returns the offset the instrument actually stored, read back rather
than assumed.

### 2-wire vs 4-wire

2-wire resistance carries lead and contact resistance in series — the
datasheet bounds it at 0.2 Ω, and on this bench it measures **78.9 mΩ**
(2-wire 100.12209 Ω vs 4-wire 100.04321 Ω on the same 100 Ω part). At
100 Ω that is a 0.08 % error, ~5x the meter's own accuracy contribution
of ±(0.010 % + 0.005 %). Either use
`measure_resistance_4wire()` with sense leads, or short the leads and
`null_now()` first. For a 4½-digit reading of a 100 Ω part, 2-wire
without a null is not a measurement. Both paths are hardware-verified;
see `KNOWN_LIMITATIONS § H-5`.

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

`WIRING=4` is the primary path and the **default** if the variable is
unset; `=2` makes the tests null first and skip the comparisons that
need 4-wire. Declaring `=2` when that is the real wiring is the point —
left at the default against 2-wire leads the comparisons would run and
report lead resistance as instrument disagreement. `-s` surfaces the
measured lead-and-contact resistance the lead test prints.

**Hardware validation status.** Both suites have been run against the
real meter (firmware 0.0.0.20, serial SDM46A0CA00021) with the sense
leads attached and `WIRING=4`. The driver suite is 13 passed / 1 skipped
/ 0 failed; the cross-validation is 7 passed / 0 skipped, so 4-wire
(`FRESistance`) is validated on silicon and not just against the
simulator. The lead-resistance test measured **78.9 mΩ** of
lead-and-contact error — inside the datasheet's 0.2 Ω bound, but still
about 2x the 38 mΩ offset below, which is why an un-nulled 2-wire read
cannot resolve it. `KNOWN_LIMITATIONS § H-5` carries the full numbers.
The driver suite's remaining skip is its self-balancing pair: with a DUT
across the inputs the deliberate-overload test cannot overload, so it
skips and its complement (the null test) runs.

Be clear about what the pair proves. The QR10x's ±0.05% dominates the
meter's ±(0.010% + 0.005%), so **agreement** between the two is
budgeted at roughly 0.07 Ω at 100 Ω — wider than the ~38 mΩ offset the
QR101A-1M-R1 actually shows. The meter *alone* (0.02 Ω on the 200 Ω
range) resolves that offset. So: use the pair to catch gross errors,
and the meter to measure.

That ~0.07 Ω is also comparable to the 79 mΩ of lead error itself, which
is why the lead-resistance test is written as a *difference of two
readings from the same meter* rather than a comparison against the
QR10x. Differencing 2-wire against 4-wire cancels the QR10x's ±0.05 %
term entirely, so a 79 mΩ result is meaningful where a 79 mΩ
*disagreement* between instruments would not be.

The test file derives both budgets from the
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

### Bench-discovered firmware quirks

Tested against firmware 0.0.0.20 on serial SDM46A0CA00021. Four
findings are written up as reports ready to send to Siglent — see
[`vendor-issues/`](vendor-issues/SDM4065A-firmware-bug-reports-README.md)
for the transcripts, and `KNOWN_LIMITATIONS.md § F-5` for the
driver-facing summary — plus § F-7 and § F-8, which carry the two that
constrain how any code may talk to this meter. Three of the four
present as good data rather than a visible fault, which is what makes
them worth knowing:

- `RESistance:NULL:VALue` does not disarm `NULL:VALue:AUTO` as §7.4.3
  says it does, so a null silently becomes a no-op unless AUTO is
  cleared explicitly. `null_now()` does.
- **§7.4.7's autozero mnemonic `AZ` does not exist on the
  instrument** — the working node is `ZERO:AUTO`. Worse, *querying* an
  undefined header wedges the USB-TMC interface until the meter is
  power-cycled from the front panel, so `RESistance:AZ?` is not a
  harmless experiment. The driver uses `ZERO:AUTO` throughout.
- `*CLS` does not clear the error queue, and an overflowed queue can
  latch into reporting "No Error" permanently — hence `command_error()`
  and `drain_errors()` above.
- The documented default resistance range (2 kΩ) is not the reset
  state, and `CONFigure:<fn> DEF` leaves autoranging on where
  `RESistance:RANGe DEF` turns it off.

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
| Identity / housekeeping | `info`, `is_connected`, `close`, `reset`, `clear_status`, `self_test` |
| Error reporting | `last_error`, `raise_if_error`, `drain_errors`, `command_error`, `standard_event_status` |
| Function select | `set_function`, `get_function` |
| One-shot measurement | `measure_dc_voltage`, `measure_ac_voltage`, `measure_dc_current`, `measure_ac_current`, `measure_resistance`, `measure_resistance_4wire`, `measure_capacitance`, `measure_frequency`, `measure_period`, `measure_continuity`, `measure_diode`, `measure_temperature` |
| Configure + trigger | `configure_resistance`, `configure_dc_voltage`, `get_configuration`, `read`, `read_nulled`, `initiate`, `fetch`, `abort`, `set_sample_count`, `get_sample_count` |
| Accuracy controls | `set_nplc`, `get_nplc`, `set_range`, `get_range`, `set_autorange`, `get_autorange`, `set_autozero`, `get_autozero`, `get_autozero_cached`, `reading_timeout_ms` |
| Null ("Ref") | `null_now`, `set_null`, `get_null`, `set_null_value`, `get_null_value`, `set_null_auto`, `get_null_auto` |
| Temperature | `set_temperature_unit`, `get_temperature_unit` |
| Recovery | `clear_device_buffers` |
| Escape hatch | `write`, `query`, `query_float`, `query_floats` |

`read()` and `fetch()` always return `list[float]`, one entry per
`sample_count`. A scalar return would silently keep only the first
sample when a caller raised the count.

Every function-scoped method (`set_nplc`, `set_range`, the whole null
group, autozero) takes a keyword-only `function=` that defaults to
`"RESistance"`, because the instrument keeps this state **per
function** — a null armed on `RESistance` says nothing about
`FRESistance`.

### Error reporting, and why there are three ways to do it

Firmware 0.0.0.20 makes the obvious check — read `SYSTem:ERRor?` — not
trustworthy on its own: `*CLS` does not empty the queue, and an
overflowed queue can latch into answering "No Error" permanently
([vendor issue 3](vendor-issues/SDM4065A-firmware-bug-report-3-error-queue.md)).
So the driver offers three, with different guarantees:

| Method | Wire form | Use when |
|---|---|---|
| `command_error()` | `*ESR?` bit 5 | the question is "was that rejected?" — the reliable check; read-clear, so it cannot accumulate stale state |
| `last_error()` / `raise_if_error()` | `SYSTem:ERRor?` | you need the numeric code, and the queue is known good |
| `drain_errors(limit=32)` | `SYSTem:ERRor?` until clean | after deliberately sending something rejectable, and as the `*CLS` workaround. Bounded, so a queue that will not empty cannot hang a `finally` block |

`clear_status()` writes `*CLS` **and** drains, because the write alone
leaves entries behind on this firmware.

`clear_device_buffers()` is the layer below: a USB-TMC `INITIATE_CLEAR`
control transfer over endpoint 0, for when both bulk endpoints are
timing out. Best-effort — it returns False rather than raising, and on
this firmware a clear can report `STATUS_SUCCESS` while the SCPI parser
stays stuck ([vendor issue 2](vendor-issues/SDM4065A-firmware-bug-report-2-autozero-query.md)).
It is a no-op on a LAN session, which has no such request.

### SDK-only methods

Per the SDK ↔ MCP parity principle every public method has a matching
tool, with six deliberate exemptions — grouped into four reasons below.
They are named and justified in
`test_sdm4065a_mcp_tools_cover_the_driver_surface` in
`tests/test_mcp.py`, which is what keeps this list from drifting:

| Method(s) | Why no tool |
|---|---|
| `open` | `sdm4065a_open` is the tool; the classmethod itself is internal |
| `query_float`, `query_floats` | typed sugar over `query()`. `sdm4065a_query` is the single raw escape hatch, as for every other driver |
| `get_null_value`, `get_null_auto` | folded into `sdm4065a_get_null`, which returns state, offset and auto together in one dict |
| `get_autozero_cached` | SDK-only by design. It returns the last *commanded* value with no round trip, which is useful inside a timed loop but would be a trap as a tool: an agent cannot tell a cache from a measurement. `sdm4065a_get_autozero` asks the instrument instead |

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

54 tools, prefixed `sdm4065a_`. There is no confirmation-argument tool
here — nothing to arm. The tools that affect accuracy (range, NPLC,
null) say so in their docstrings, which is what the model reads before
calling them. See the SDK-only table above for the six methods that
deliberately have no tool, and [`mcp.md`](mcp.md) for the inventory.

### Note on SCPI headers

Siglent's manual writes headers **without** the leading root colon
(`SYSTem:ERRor?` where Rigol writes `:SYSTem:ERRor?`). Both forms work
on the instrument. The simulator accepts both deliberately: matching
only the colon-prefixed form would make error queries fall through to
the generic register model and answer `0`, i.e. a permanently clean
error queue — the one failure a driver cannot detect for itself.

## CyberPower PDU41002 — 8-outlet switched PDU

[CyberPower PDU41002](https://www.cyberpowersystems.com) — 1U rack
switched PDU, 120 V / 20 A, eight individually switchable NEMA 5-20R
outlets, whole-device metering, RS-232 console and 10/100 Ethernet.

This is the first driver in the tree that **switches mains power**, and
the first with a network transport. Both facts change how it is shaped.

For every other instrument, "safe" means *output off*. For a PDU,
cutting mains is itself the disruptive act: it can de-power a DUT
mid-measurement and drop other instruments' sessions. So the default is
**do not move the contactors**, every switchable outlet is opt-in, and
`close()` deliberately leaves outlet state alone.

> **This driver switches real mains power.** `set_outlet_state`,
> `reset_outlet` and `clear_outlet_command` move contactors on whatever
> is plugged into the PDU. Nothing switches unless its index is in
> `allowed_outlets`, and nothing switches implicitly — `close()`,
> `__exit__` and the governor's default safe state all deliberately
> leave outlet state alone. See [Switching](#switching).

### Quick start

```python
from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

# serial — the bootstrap and recovery transport
with CyberPowerPDU41002.open(port="/dev/benchctrl/pdu41002",
                             allowed_outlets=(1, 2)) as pdu:
    print(pdu.read_identity())          # sys show
    print(pdu.read_device_status())     # devsta show — load, V, Hz, kWh
    print(pdu.outlet_states())          # {1: True, 2: True, ...}

# network — same driver, same CLI, same parsers
with CyberPowerPDU41002.open(host="pdu-benchctrl",
                             allowed_outlets=(1, 2)) as pdu:
    print(pdu.measure_load_A())
```

`allowed_outlets` is a **required** keyword argument. There is no "all"
default and no `Optional`: a config typo has to fail closed on the one
device that can drop mains.

### One CLI, two byte pipes, no SNMP

The vendor CLI is byte-identical over both transports. That was measured,
not assumed — `sys show`, `oltsta show`, `oltsta index 1 show`,
`console show` and `snmpv1 show` compare exactly across serial and SSH,
and a `PDU41002Info` read over SSH compares equal to the same read over
serial. `devsta show` differs only in live mains voltage.

So there is one command engine, one grammar, one set of parsers and one
simulator, with a deliberately thin link seam beneath
(`write` / `read` / `is_open` / `close`). Adding the SSH path changed no
public signature.

SNMP is **not used and is disabled on the device**. The CLI covers
switching, state, metering and identity, so SNMP would have bought a
second differently-shaped protocol, a second codec and a second
simulator for zero capability — plus a cleartext-credential exposure.

### Only one session exists — on the whole device

The PDU permits **exactly one CLI session at a time across all
transports**, and this is the single most surprising thing about it:

- **The incumbent wins.** An SSH login attempted while serial is logged
  in *completes* — the banner prints in full — and the device then
  immediately hangs up. The serial session carries on unaffected.
- **The failure looks exactly like a bad password**, because it arrives
  *after* the password is accepted. The driver raises
  `PDU41002SessionError` rather than an auth error specifically so this
  is diagnosable; that type survives the RPC wire for the same reason.
- **Closing the port does not end the device session.** CLI session
  state outlives the serial port, so `close()` **must** send `exit`.
  Skipping it leaves the PDU unreachable from the other transport. This
  is the one driver in the repo whose `close()` has a required side
  effect on the device.

"Network alongside serial" therefore means **alternating, not
concurrent**. `open()` takes exactly one transport; passing both `port`
and `host` raises rather than silently preferring one, so a run log can
always answer which wire a switch travelled on.

Serial stays the bootstrap and recovery path: no key, no negotiation,
no password-prompt handling.

### The password

The PDU is benchctrl's first device that needs a secret, and the obvious
places to put one are both wrong:

- `DeviceConfig.open` is written back verbatim by `to_dict()`, so a
  password there lands in any saved config;
- `open_kwargs` crosses benchctrl's RPC wire, which is
  HMAC-authenticated but **plaintext**.

So leave `password=None` and set **`BENCHCTRL_PDU_PASSWORD`** in the
environment of the process that talks to the device — the agent's own
environment, not the client's. A missing password fails at `open()`
naming the variable, rather than later as a login timeout.

```bash
read -rs BENCHCTRL_PDU_PASSWORD && export BENCHCTRL_PDU_PASSWORD
```

Belt and braces around that: `DeviceConfig.to_dict()` now masks
`password` / `passphrase` / `secret` keys in `open` as `***` (so
config round-tripping is lossy for those keys **by design**), the
driver's and links' `__repr__` carry no credential material, and the
password never reaches a command line — no `sshpass`, no env prefix on
the ssh argv, both of which would expose it in the process list.

The simulator accepts a configured password, so hardware-free tests
never need the real one.

#### Under systemd, which is how a bench actually runs

The `read -rs` line above is the interactive case — an operator at a
shell, driving the SDK by hand. The agent on a bench board is a systemd
unit with no shell to inherit from, and its existing environment file is
the wrong place: `/etc/benchctrl/agent.env` is **0644 on purpose**,
because it holds `PYTHONPATH` and an operator should be able to read
that without sudo. A secret must not inherit that mode.

So the unit reads a second file, and `install-agent.sh` creates it at
0600 with the variable present but commented out:

```
/etc/benchctrl/agent.env          0644  PYTHONPATH and friends
/etc/benchctrl/agent.secrets.env  0600  BENCHCTRL_PDU_PASSWORD
```

systemd reads both as root *before* dropping to the service user, so the
secrets file does not need to be readable by `arduino` — which is the
whole reason this belongs in the unit rather than in a shell profile.
Write it with no quotes, no `export` and no trailing space: systemd
parses the file itself, and a quoted value arrives with the quotes
attached, so the password silently becomes wrong rather than missing.

Two things about the file are load-bearing:

- **It is optional**, via `EnvironmentFile=-/etc/…`. A bench with no PDU
  has no such file, and a missing secret must not stop the agent serving
  the instruments that need none. Note where the `-` goes: it is part of
  the *value*. `-EnvironmentFile=` is not a directive — systemd logs
  `Unknown key … ignoring` and carries on, so the secret never loads and
  the driver fails at `open()` naming a variable that looks like it was
  set. Confirm with `systemctl show -p EnvironmentFiles benchctrl-agent`,
  which should list the path with `ignore_errors=yes`.
- **A re-install never rewrites it.** `install-agent.sh` guards on
  existence, for the same reason it never regenerates `agent.json`:
  clobbering a working credential during an upgrade is worse than
  leaving a stale one.

After editing it, `sudo systemctl restart benchctrl-agent` — an
`EnvironmentFile` is read at process start, so a running agent keeps the
old value (or keeps having none).

### SSH quirks worth knowing (firmware 1.3.4)

Each of these is a fixed flag in `links.py` with a comment; they look
like sloppy security defaults and are not.

| Symptom | Cause | What the driver does |
|---|---|---|
| `key exchange failed!` | group-*exchange* KEX is broken on this firmware | forces `KexAlgorithms=diffie-hellman-group14-sha256` |
| `Permission denied (keyboard-interactive)` even with the matching key uploaded | device offers keyboard-interactive **only**; pubkey auth is refused | `PubkeyAuthentication=no`, and a pty via `pty.fork()` + `ssh -tt` to type the password |
| host key reads as all zeros | the exported ed25519 host key is null | `StrictHostKeyChecking=no` + `UserKnownHostsFile=/dev/null` — unverifiable either way, so don't poison the operator's `known_hosts` |
| a command fails with `no prompt … (got 130 bytes)` after a few minutes of quiet | the device hung up on an idle session and the *ssh client* printed its notice; nothing on the far end will ever answer again | raises `PDU41002ConnectionError` saying to reopen |
| an idle session reports `PDU41002SessionError` — "another session is logged in" — when nothing else is | ssh's *other* disconnect wording, `Connection to … closed by remote host.`, is the same text the single-session hangup produces | the hangup markers are only read as contention **during login**; post-login they mean the link died, so `_round_trip` raises the connection error instead |

Login over SSH takes around 7.5 s. It is slow, not stuck.

### Reads

| Method | CLI verb | Notes |
|---|---|---|
| `read_identity()` | `sys show` | name, location, contact, model, HW/FW version, MAC — the `*IDN?` equivalent |
| `read_device_status()` | `devsta show` | load A/W/VA, power factor, voltage, frequency, peak load, energy kWh |
| `measure_load_A()` / `measure_voltage_V()` / `measure_frequency_Hz()` | `devsta show` | scalar convenience wrappers |
| `outlet_states()` | `oltsta show` | every outlet in one round trip |
| `outlet_state(n)` / `outlet_name(n)` | `oltsta` | `True` means energised |
| `read_outlet_config(refresh=False)` | `oltcfg index all show` | per-outlet on/off delay and reboot duration; cached |
| `transport` / `is_open` / `outlet_count` / `allowed_outlets` / `panic_outlets` | — | properties |

Metering is **whole-device**. The PDU41002 does not meter outlets
individually, so there is no per-outlet current to ask for.
`power_factor` is `None` at zero load — the device prints `----`, which
is normal rather than a fault.

### Switching

Three methods move contactors, and there is deliberately **no
`outlet_on()` / `outlet_off()` pair** — one switching verb means one code
path to audit.

| Method | CLI verb | Returns |
|---|---|---|
| `set_outlet_state(n, on, *, delayed=False, verify=True)` | `oltctrl index n act on\|off\|delayon\|delayoff` | the **verified read-back** state |
| `reset_outlet(n, *, delayed=False)` | `oltctrl index n act reboot\|delayreboot` | `None` |
| `clear_outlet_command(n)` | `oltctrl index n act cancel` | `None` |

```python
with CyberPowerPDU41002.open(port="/dev/benchctrl/pdu41002",
                             allowed_outlets=(3,)) as pdu:
    assert pdu.set_outlet_state(3, False) is False   # verified off
    ...
    assert pdu.set_outlet_state(3, True) is True     # verified on
```

**`set_outlet_state` returns the read-back state, not `None`, because
`oltctrl` reports nothing at all.** Its reply is a blank line and a
re-prompt — byte-identical whether or not the contactor moved (captured
in `tests/fixtures/pdu41002/outlet_switch.txt`). There is no other way to
learn what happened, so read-back is mandatory rather than prudent.
`verify=False` skips it and logs a warning; the value it returns is the
state you *asked for*, not the state the outlet is in.

**The verify budget is derived from the device, not hardcoded.** Each
outlet's `td_on` / `td_off` is operator-configurable (3 s as shipped),
and `oltcfg` is read to size the wait. A budget picked from the measured
~0.62 s round trip or ~1.5 s settle looks generous and then flakes on a
unit whose delay someone raised. If the outlet never agrees,
`PDU41002ProtocolError` says so and names both possibilities — it did not
move, or its delay exceeds the budget.

**`reset_outlet` returns `None` on purpose.** A reboot ends where it
started, so a read-back cannot tell "cycled" from "never moved". The
transient cut is the point and it is not observable after the fact, so
the method makes no claim about it. If you need proof of the cut, drive
`set_outlet_state(n, False)` then `True` and verify each.

**`clear_outlet_command` is gated like a switch**, not like a read.
Cancelling a scheduled cut leaves mains *on* when an operator expected it
off — a state change in every sense that matters.

#### Two independent guards, and why the method names matter

Aggregate targeting is impossible **structurally**, not by validation:
`oltctrl index all act off` is one line that de-powers the whole bench,
so no signature accepts `"all"`, `"b1"`, `"b2"` or a collection. Two
separate checks enforce it:

1. `_coerce_outlet` rejects anything that is not a plain in-range `int`
   — `bool` before `int`, so `True` cannot become outlet 1. That catches
   a bad *argument*.
2. Every rendered command is matched against
   `^oltctrl index \d+ act (on|off|reboot|delayon|delayoff|delayreboot|cancel)$`
   before a byte is written. That catches a bad *rendering* — a formatting
   bug or a future edit to the action table — which the first check
   cannot see.

The switching methods are named `set_`, `reset` and `clear_` because
`agent/dispatch.py` derives which calls need the writer claim **purely
from the method name**, with no driver-declared override. A method named
`outlet_on()` would be remotely callable by any read-only observer:
mains switching that bypasses the claim gate entirely. Renaming any of
the three silently removes that protection, which is why the mutator set
is pinned by an exact-equality test in both directions.

### Deployment assumptions

**Self-protection here is a cabling invariant, not a software
guarantee.** The driver cannot know which outlet feeds what.

The current bench policy is that the PDU powers **bench instruments and
DUTs only**: the Arduino Uno Q agent host and the network gear are *not*
plugged into it. That is what makes it impossible for the bench to cut
power to its own agent, or to the control path that would recover it.

If that ever changes — if the board or the switch is moved onto the PDU
— then `allowed_outlets` and `panic_outlets` are the controls to
revisit *before* the move, not after. Defence in depth available on the
device itself: its `oltuser` model can restrict outlets in firmware.
Configure that by hand if you want it; benchctrl will not do it for you.

Open the adapter by its stable symlink, not `/dev/ttyUSB0`:
`deploy/udev/62-benchctrl-ftdi.rules` binds
`/dev/benchctrl/pdu41002` to the FTDI serial number, because ttyUSB
numbering is enumeration-order and a second USB-serial adapter can take
`ttyUSB0` while the driver still opens it and starts issuing commands.

### Traps the driver avoids by construction

- **`menumode` is a one-way trap.** Sending it switches the session to a
  menu interface, and returning to the CLI requires a full logout and
  login. Every parser would then fail while the link still looks
  healthy. No method emits it.
- **`console telnet enable` would disable SSH.** The two are mutually
  exclusive in firmware, so that verb can kill the network transport
  from underneath a running test. Not in the method surface.
- **There is no interrupt character.** `\x03` is not an interrupt on
  this CLI — it is echoed and taken as part of the command (a stray one
  produces `Command not found` at a constant column). A bare `CR` *is*
  the resync, and that is what `_resync()` sends after a timeout.
- **Read until the prompt, never until a blank line.** An unknown verb
  produces a ~30-line verb dump, and the number of blank lines before
  the prompt varies by error shape (2, 3, 0). Blank-line termination
  truncates mid-error and desyncs the session.
- **Line endings are not uniform** — caret lines are introduced by a
  bare `\n\r`. The engine parses bytes rather than trusting
  `splitlines()`.
- **Serial echoes the command; SSH does not.** The largest textual
  difference between the transports, and why echo stripping is
  conditional on the link.
- **Idle logout is a safety hazard, not an annoyance.** After the idle
  timeout a command is consumed *as a username* and silently swallowed.
  `_cmd()` detects a login prompt in any response, and read-back
  verification is the second line of defence.

  Two measured corrections to the obvious reading of that. First, the
  timeout is **not** the manual's 3 minutes: this device reports
  `Idle Time : 5 Minutes`, and over SSH the connection is dropped earlier
  still, at ~180 s. Read `devcfg show` rather than trusting the default.

  Second, and more important, **the two transports need opposite
  handling**:

  | | What the device does | Recovery |
  |---|---|---|
  | serial | keeps the port open and drops to `Login Name :` | the driver re-authenticates in place; the caller sees nothing |
  | ssh | closes the connection; the ssh client prints its disconnect notice and exits | **not recoverable** — the caller must `open()` again |

  ssh prints one of *two* notices for this, and the second
  (`Connection to … closed by remote host.`) is byte-shared with the
  single-session hangup, so wording alone cannot tell them apart.
  Position can: those markers only mean contention *during login*. Past a
  successful login they mean the link is gone.

  So a timed-out SSH session is a `PDU41002ConnectionError` naming the
  reopen, not a timeout. The distinction is load-bearing: before it
  existed the symptom was `no prompt after 'sys show' within 12.0s (got
  130 bytes)`, which reads as a slow device and invites a retry that can
  never succeed on a link that no longer exists.
- **`Login Failed` is ambiguous by construction — retry, don't
  classify.** There are **four** login outcomes, not two: the prompt
  (success), a `Login Name :` re-prompt, silence, and ~15 s of dots
  followed by `Login Failed` and then silence with no re-prompt. That
  fourth shape is emitted **byte-identically** for a wrong password *and*
  for a correct password submitted within ~15 s of a previous session
  closing, which the single-session limit makes routine. Nothing on the
  wire separates them, so the driver retries within a 75 s budget rather
  than deciding, and only calls it an auth failure when the budget is
  spent — at which point the error names both possibilities. A driver
  that classified the first refusal would report "wrong password" for a
  password that was right, which is the exact misdiagnosis this driver's
  error types exist to prevent.
- **A bare `CR` is not inert at the login prompt.** It submits an *empty
  line* to whichever field is current, so repeated CRs walk the
  authentication state machine and end in a ~15 s `Please wait for
  authentication....` / `Login Failed` cycle during which the console
  answers nothing. This is why the PDU is not discovery-probeable — see
  below.

### Discovery: identified passively, never probed

The FT232R's `0403:6001` is a generic bridge, so VID/PID cannot name this
device. The other bridge-hidden instrument (the QR10x) is identified by
*probe* — writing a harmless query and matching the reply. **That
approach is not usable here**, and the reason is worth recording because
the obvious design is wrong:

| Attempt | Measured result on firmware 1.3.4 |
|---|---|
| bare `\r` at 9600 | walks the login state machine; the vendor string appears in only one of three states, so five consecutive probes returned `None, cyberpower_pdu41002, None, None, None` |
| any probe at all | the device logs `Login authorization failure via Console` — a bench sweep writing auth failures into a mains switch's audit trail |
| during the retry | ~15 s of silence, so a probe can lock out the driver behind it |
| `?`, `DEL`, `NUL` with no terminator | **no reply at all** — nothing to match on |
| opening the port and reading | no greeting |

So `SERIAL_PROBES` deliberately contains **no PDU entry**, and
`discovery.identify_by_symlink()` names it from the udev symlink instead:

```
$ benchctrl-discover
/dev/ttyUSB0  cyberpower_pdu41002  exact
    identified by udev symlink, not VID/PID — open it by /dev/benchctrl/
    path so the binding survives re-enumeration
```

This is strictly better than a probe rather than a consolation prize: it
is exact rather than heuristic, it survives re-enumeration because the
rule keys on the adapter's serial number, and identifying the bench
writes **zero bytes** to a mains contactor's control port. It does
require `deploy/udev/62-benchctrl-ftdi.rules` to be installed; without
it the PDU comes back unidentified, which is the correct failure — an
unidentified port is obvious, whereas a probe that names the wrong
device is not.

### Simulator

`benchctrl.sim.pdu41002.SimulatedPDU41002` replays captured device
output byte-for-byte: the canned responses under
`tests/fixtures/pdu41002/` are verbatim transcript excerpts with the
firmware version and capture date recorded, not text written from the
manual. That distinction matters — a simulator built from the same
misreading as the driver agrees with it. This one earned its keep
immediately by reproducing the hardware's handling of `\x03`, which is
how that driver bug was found.

Test hooks: `hold_session()` / `release_session()` (single-session
contention), `force_logout()` (serial idle logout — deterministic
re-auth, no wall clock), `drop_ssh_session()` (the SSH idle logout: emits
the client's disconnect notice and then **stops answering**, because a
simulator that kept replying would let a driver appear to recover from a
dead link), `refuse_next_logins` / `refusal_count` (the `Login Failed`
shape on a *correct* credential), `stall_next_auth` (a device that never
answers, which must not be reported as a bad password), `ignore_switches`
(a device that acknowledges `oltctrl` and moves nothing), `command_log`,
`login_log` (records `(user, accepted)` — never passwords),
`hangup_count` and `inject_error(shape)`.

The last four exist because the hardware suite found bugs the simulator
had been agreeing with. Each models a device behaviour the driver had
been *guessing* at, and each pins a fix that only the real PDU could have
prompted.

One caveat recorded in the fixture README: the prompt is
`"CyberPower > "` **with a trailing space** on the wire. The checked-in
fixture files lost it to trailing-whitespace stripping, so do not
"correct" `PROMPT` to match the files — every read would break.

### MCP tools

15 tools, prefixed `pdu41002_`. `pdu41002_open` takes **no password
parameter at all** — absent, not defaulted — so a model cannot place a
credential in a tool-call argument where it would be logged in the
conversation transcript.

Three of them switch mains: `pdu41002_set_outlet_state`,
`pdu41002_reset_outlet` and `pdu41002_clear_outlet_command`. Each
docstring opens by saying so and points at `allowed_outlets` as an
operator decision to *ask about* rather than widen — a model reading only
the tool description should not be able to mistake these for
configuration.

## Silicon Labs CP2112 — open-drain control lines

[Silicon Labs CP2112](https://www.silabs.com/interface/usb-bridges) — a
USB HID-to-SMBus bridge with eight general-purpose I/O pins. benchctrl
uses **only the GPIO**, and only in one mode: **open-drain outputs, for
asserting and releasing a target's reset line.**

The purpose is narrow and worth stating plainly. During the i.MX8 Zephyr
bring-up, an Otii Arc Pro was tied up doing nothing but toggling a reset
pin — a precision SMU acting as a switch. The CP2112 is a ~$15 board that
does exactly that job, which frees the SMU for measurement.

> **This driver drives a line that holds a DUT in reset.**
> `set_line_asserted(i, True)` **latches** — the target stays held until
> something releases it. Prefer `trigger_reset_pulse`, which cannot leave
> a line asserted. Nothing is driven unless its index is in
> `allowed_lines`, and `open()` drives nothing at all.

### Quick start

```python
from benchctrl.drivers.silabs_cp2112 import CP2112

# allowed_lines is required. There is no "all" default: the driver cannot
# know what GPIO.3 is wired to, and the answer lives on the bench.
with CP2112.open(allowed_lines=(3,)) as gpio:
    print(gpio.read_identity())            # part 0x0C, revision, USB serial

    gpio.set_line_mode(3, output=True)     # open-drain; push-pull is not offered
    gpio.trigger_reset_pulse(3, duration_s=0.1, settle_s=0.5)
```

On exit the as-found GPIO configuration is restored, which **releases**
anything the driver was holding.

### Vocabulary: asserted and released, never high and low

Reset lines are active-low, so "set the line high" is ambiguous exactly
where a mistake holds a DUT in reset indefinitely. The whole API says
`asserted` instead:

| term | electrically | effect on an active-low reset |
|---|---|---|
| **asserted** | the pin pulls the net to 0 V | the target is **held in reset** |
| **released** | the pin is high-Z; the net floats to VIO | the target **runs** |

`CP2112LineState.asserted` is therefore `not level`, and it is `None` for
an input, because an input asserts nothing.

### Open-drain is the only drive mode, and that is a safety property

The CP2112 can drive push-pull. This driver **cannot**, and the
restriction is enforced rather than documented:

- `set_line_mode` clears the push-pull bit on every call, even if another
  program had set it.
- `set_line_asserted` refuses a pin that is configured push-pull.
- No public method takes a `push_pull` parameter — a test asserts this, so
  the property cannot be lost to a well-meaning "make it configurable"
  change.

Two reasons, both physical:

1. **It cannot fight the target.** An open-drain pin pulls a net low and
   releases it, but never *sources* into it. A push-pull output at 3.3 V
   wired to a 1.8 V reset net back-feeds the target's rail through its
   ESD diodes. Open-drain makes that failure mode structurally impossible
   rather than merely unlikely.
2. **It fails safe on unplug.** Pull the USB cable and the chip reverts
   every pin to an input (datasheet §7), which for an open-drain reset
   line *is* released. A push-pull pin holding 0 V would leave the DUT in
   reset with no software left to notice.

Logic high comes from the CP2112's own internal pull-up to VIO, so a
released line needs no external resistor — though the datasheet allows an
external pull-up to 5 V if the target needs a stronger one.

### A level identifies nothing; a level you can *change* does

This cost real bench time and is the single most useful thing to know
about the device. Commissioning it produced two readings that looked
contradictory:

- the CP2112 reported every pin as an input latching **1**
- a DMM on the same net read a flat **0.0002 V**

Both were correct, and nothing was broken. An undriven CP2112 pin is
**high-impedance**: a ~10 MΩ voltmeter drags the floating net to nearly
0 V while the chip's input buffer still latches a 1. Neither instrument
was lying; the pin simply was not being driven by anything.

The consequence for anyone trying to work out which pin a probe is on:
**reading a level tells you nothing.** `read_levels()` on an unconfigured
device returns `0xFF` regardless of what is attached. The only
identification that means anything is a level you can make *move* — drive
one pin at a time and watch which one the meter follows.

The same logic is why the hardware tests use a DMM rather than the chip's
own read-back. Reading a pin back through the CP2112 that just drove it
proves the *latch* changed, not that any voltage did.

### GPIO.0, GPIO.1 and GPIO.7 carry chip functions

Three pins have alternate functions the CP2112 can drive itself
(datasheet Table 10):

| GPIO | package pin | alternate function |
|---|---|---|
| 0 | 23 | TX Toggle |
| 1 | 22 | RX Toggle |
| 7 | 12 | Clock Output |

`set_line_mode` refuses these unless the caller passes
`allow_alternate_function=True`, which is the operator saying they have
checked the alternate function is off. **GPIO.7 is stricter:** if the
clock output is actually running, the refusal stands regardless of the
override, because that is a fact the driver can read rather than a claim
it has to take on trust.

The MCP surface **does not expose the override at all**. A model cannot
walk to the bench and confirm a pin is idle, so offering the parameter
would leave the gate one plausible-sounding argument away from bypassed.

### GPIO is not for real-time signalling

Every transition is a separate USB control transfer, so timing is bounded
by bus scheduling rather than by the chip — the datasheet says as much.
`trigger_reset_pulse` refuses durations below 5 ms rather than silently
stretching them; anything needing sub-millisecond edges needs a different
instrument. For reset lines, where hold times are specified in
milliseconds, this is not a constraint that matters.

### Transport: hidraw, not usbfs

Unlike the ADU218 — which the kernel's `usbhid` ignores, leaving usbfs as
the only route — the CP2112 is claimed by `usbhid` and appears as
`/dev/hidraw*`. Both halves of that were measured on the board rather than
assumed:

1. `usbhid` **does** bind it, so a hidraw node exists.
2. `hid-cp2112` is **not built** for the Uno Q kernel, so no in-kernel
   I²C adapter competes for the device.

GPIO commands are HID **feature reports**, carried by `HIDIOCSFEATURE` /
`HIDIOCGFEATURE` ioctls rather than endpoint writes. Two consequences:

- The node must be opened `O_RDWR` **even to read**, because a feature
  *get* is a `GET_REPORT` over the control pipe and needs write access.
- The ioctl request numbers embed the payload size, so they are computed
  with the `_IOC` macro rather than hardcoded. A constant lifted from a
  64-bit header would be wrong on a 32-bit userland, and `hidraw.py` has
  a test that pins the arithmetic against independently-derived values.

Still zero new dependencies: `os`, `fcntl` and `ctypes`.

### The udev rule is VID/PID-scoped for a security reason

`deploy/udev/64-benchctrl-cp2112.rules` matches `10c4:ea90` specifically:

```
SUBSYSTEM=="hidraw", ATTRS{idVendor}=="10c4", ATTRS{idProduct}=="ea90", \
    MODE="0660", GROUP="dialout"
```

The tempting shortcut — a blanket `SUBSYSTEM=="hidraw", MODE="0660"` —
would also hand the bench user the **attached USB keyboard**, which on
this board is `hidraw0`/`hidraw1`. That is a keylogging surface, not a
bench instrument. Serial nodes have no equivalent hazard, which is why
this rule is narrower than the FTDI one next to it.

Note that `10c4:ea60` is the CP210x *UART* bridge, a different chip
entirely, and must not match.

This driver is also the reason a `SYMLINK+=` is offered here when other
drivers do without: `hidrawN` numbering shifts whenever a keyboard is
replugged, and its neighbours are input devices, so a stable
`/dev/benchctrl/cp2112-<serial>` path is worth having.

### Method surface

Every pin-moving method carries a name prefix that `agent/dispatch.py`
recognises as a mutator (`set_`, `trigger`, `reset`). That is not a style
choice: the mutator set is derived **purely from name prefixes**, with no
driver-declared override, so a method named `assert_line` would be
remotely callable **without the writer claim**. A test asserts every
pin-mover is in `surface.mutators` and every read is not.

```python
# reads
info; is_open; path; serial; line_count; allowed_lines
read_identity()  -> CP2112Info
read_gpio_config() -> CP2112GpioConfig
read_levels()    -> int                      # 8-bit image; see the caveat above
read_line_state(i) / read_line_states()
line_is_asserted(i) -> bool

# writes
set_line_mode(i, *, output, allow_alternate_function=False) -> CP2112LineState
set_line_asserted(i, asserted, *, verify=True)             -> CP2112LineState
trigger_reset_pulse(i, *, duration_s=0.1, settle_s=0.0)    -> CP2112LineState
reset_lines()                                              -> CP2112GpioConfig

close(*, restore=True); __enter__; __exit__
```

- **`open()` is observational.** It configures nothing and drives nothing,
  and it records the as-found configuration so `close()` can put it back.
  Re-opening the device therefore cannot disturb a reset line another
  process is holding.
- **`set_line_asserted` verifies by default** and returns the read-back
  state. An open-drain pin cannot pull a net that something stronger is
  holding high, and a caller needs to know that happened rather than get
  a silent success.
- **`trigger_reset_pulse` releases in a `finally`**, so an interrupt during
  the hold cannot strand a DUT in reset.
- **`reset_lines()`** returns every allowed line to an input. High-Z is the
  chip's own power-on state, so this is "as the hardware would come up"
  rather than an invented safe state — and for an open-drain reset line,
  high-Z *is* released.
- **`close()` restores, best-effort, and never raises**, because a failure
  there must not mask the exception that prompted the close.

Writes are read-modify-write against a **single shared config register**,
so the other seven pins are preserved on every call. That register is also
why there is exactly one driver object per device
(`KNOWN_LIMITATIONS.md` §N-4): two objects would clobber each other's
read-modify-write.

### Simulator

`benchctrl.sim.cp2112.SimulatedCP2112` substitutes at the **link seam**,
not behind a pty like every other simulator in the tree — a pty cannot
implement HID ioctls. It models the behaviours that actually bite: writes
to a pin configured as an input are silently ignored; an open-drain pin
cannot force a net high against an external pull-down; a floating input
latches 1; and a device reset reverts every pin to an input.

What a green suite therefore does **not** prove: the ioctl encoding (which
`tests/test_cp2112_hidraw.py` covers separately, against independently
derived request numbers), and that any voltage moved on any wire. That is
what `tests/test_hardware_cp2112.py` and a DMM are for.

### MCP tools

10 tools, prefixed `cp2112_`. Two of them can hold a DUT in reset, and
their docstrings open by saying so: `cp2112_set_line_asserted` names the
release call explicitly rather than assuming the caller infers it, and
`cp2112_trigger_reset_pulse` is described as the tool that *cannot* leave
a line held.

No tool exposes `allow_alternate_function`, for the reason given above.

### Out of scope

**SMBus / I²C is not implemented**, despite being the chip's headline
feature. benchctrl wants the GPIO. Adding a bus master would mean device
address handling, clock configuration and transfer-status polling — a
second protocol with its own failure modes, for a capability nothing on
the bench currently needs. The report IDs are documented in `driver.py`
if that changes.

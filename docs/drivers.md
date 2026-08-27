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
| Ontrak ADU218 relay / digital I/O interface | `benchctrl.drivers.ontrak_adu218.OntrakADU218` | USB HID over raw USBDEVFS ioctls, **no dependencies** | **shipped (unreleased)** — switches signal circuits |

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

`CP2112LineState.asserted` is therefore `not level` — but only for an
**output**. For an input it is `None`, and so is `line_is_asserted()`.

That `None` is not tidiness, and getting it wrong was the one defect real
hardware found in this driver. `asserted` answers "are *we* pulling this
line down?", not "is this net low?". An input sitting on a net that
something else holds low would otherwise report `asserted=True` — which
reads as "we are holding this DUT in reset" while the driver holds nothing
and is merely watching a third party. A caller believing that would
"release" a line it never held and conclude the target is running.

`None` rather than `False` for the same reason: "nobody is asserting this"
and "this pin is an input, so the question does not apply" are different
facts, and only the second means you are asking the wrong object about the
state of a reset line.

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

## Ontrak ADU218 — 8 relays, 8 digital inputs, no dependencies

[Ontrak ADU218](https://www.ontrak.net/adu218.htm) — USB relay and
digital I/O interface: eight 1 A solid-state (PhotoMOS) relays rated to
120 V AC/DC on a screw terminal block, eight opto-isolated digital inputs
with hardware event counters, and a hardware watchdog that de-energises
every relay by itself if the host stops talking.

Two things make this driver unlike the others in this repo.

**It has no dependencies at all.** Not pyserial, not pyvisa, not `hid` or
`pyusb`. The device is USB HID, and the driver talks to it with
`fcntl.ioctl`, `ctypes` and `os` — raw USBDEVFS on `/dev/bus/usb/BBB/DDD`.

**Silence is the only error signal.** The device has no error reply. An
unknown command, a valid command with an out-of-range argument, and a
write-only command are byte-identical on the wire: nothing comes back.
Every design decision below follows from that.

### Quick start

```python
from benchctrl.drivers.ontrak_adu218 import OntrakADU218

with OntrakADU218.open() as adu:              # finds itself by VID/PID
    print(adu.read_identity())                 # costs no round trip
    adu.set_relay_state(0, True)               # returns the verified read-back
    print(adu.relay_states())                  # one command, one instant
    print(adu.input_states())                  # {'A': (...4 bools), 'B': (...)}
    adu.reset_relays()                         # de-energise all eight
```

`open()` takes no port and no path. The driver walks `/sys/bus/usb` for
VID `0x0A07` / PID `0x00DA`, so identity comes from the descriptor rather
than from a device node that renumbers on re-plug. Pass `serial=` when
more than one ADU218 is attached.

### No pyserial — it is not a serial device

The starting assumption for this device was pyserial, and it was wrong:
the ADU218 does not present a tty. It is a USB HID device with one
interface (class `0x03`) and two interrupt endpoints, `0x81` IN and
`0x01` OUT, both `wMaxPacketSize` 8 with `bInterval` 10.

Ontrak's own Linux path is `libusb` plus their `AduHid` shared library.
Neither is available on the Uno Q, and both are dependencies. The route
taken instead — raw USBDEVFS ioctls from the standard library — needs
nothing installed, which is what the operator asked for.

Three facts make that route work rather than merely compile:

- **`USBDEVFS_BULK` on an interrupt endpoint is contractual, not a
  loophole.** The kernel's `devio.c` branches on `USB_ENDPOINT_XFER_INT`,
  rewrites the pipe to `PIPE_INTERRUPT` and calls `usb_fill_int_urb()`
  with the endpoint's own `bInterval`. It is documented in `message.c`'s
  kerneldoc and has behaved this way since v2.6.15. So an 8-byte
  "bulk" transfer to `0x01` is an interrupt transfer.
- **`usbhid` ignores Ontrak devices deliberately**, via `hid_ignore_list`
  in `hid-quirks.c`. `hid-ids.h` defines `USB_VENDOR_ID_ONTRAK 0x0a07`
  and the ADU100's `0x0064`; this device's `0x00da` is in the same claimed
  block. So `USBDEVFS_CLAIMINTERFACE` succeeds with no kernel driver to
  detach first — no `hidraw` fight, no udev unbind rule.
- **Autosuspend cannot strand a live session.** `usbdev_open()` takes a
  runtime-PM reference that is held until the fd is released. The driver
  must therefore never pass `USBDEVFS_ALLOW_SUSPEND`, and does not.

The ioctl request numbers are **computed**, not copied: `USBDEVFS_BULK`
encodes `sizeof(struct usbdevfs_bulktransfer)`, which is 24 on 64-bit and
16 on 32-bit, so the constant differs per ABI (`0xC0185502` vs
`0xC0105502`). A hardcoded value works on the laptop and fails on a
32-bit board.

### Wire format

Every packet is 8 bytes in both directions. Byte 0 is the report ID and
must be **`0x01`** — measured, not assumed: bare ASCII with no prefix,
`0x00` and `0x02` were all silently ignored. Bytes 1..7 carry an ASCII
command, NUL-padded. Seven payload bytes, so an eighth character is
dropped by the device — indistinguishable from an unknown command.

Commands are case-insensitive. Replies are prefixed the same way and
NUL-padded.

| Command | Answers | Width | What |
|---|---|---|---|
| `PK` | yes | 3 | all eight relays as a decimal mask |
| `RPKn` | yes | 1 | one relay, `n` = 0-7 |
| `SKn` | **no** | — | energise relay `n` |
| `RKn` | **no** | — | de-energise relay `n` |
| `MKddd` | **no** | — | write all eight relays, `ddd` = 000-255 |
| `PA` / `PB` | yes | 2 | one input port's nibble |
| `RPA` / `RPB` | yes | 4 | one port's four lines, **MSB first** |
| `RPAn` / `RPBn` | yes | 1 | one input line, `n` = **0-3** |
| `PI` | yes | 3 | all eight inputs as a mask |
| `REn` | yes | 5 | read event counter `n`, no clear |
| `RCn` | yes | 5 | read **and clear** event counter `n` |
| `DB` / `DBn` | yes / **no** | 1 / — | read / set de-bounce, `n` = 0-2 |
| `WD` / `WDn` | yes / **no** | 1 / — | read / set the watchdog, `n` = 0-3 |

Every width in that table came from a hardware capture
(`tests/fixtures/adu218/reads.txt`), not from the manual — the manual gives
an *example* rather than a width for `RPKn`, `RPyn`, `DB` and `WD`, and an
example is not a specification. A width wrong by one turns a desynced reply
into a plausible value instead of an exception.

**`RKn` is write-only despite starting with `R`.** Every other
`R`-prefixed command answers. A driver that inferred "responsive" from the
mnemonic would wait the full timeout on every de-energise — the most-called
command on the device — and would look broken only under load. So the
driver carries an explicit per-command `responsive: bool` table and never
infers.

`RI` appears in the manual's summary table as the name for the
read-all-inputs command. It is silent on hardware; only `PI` answers.

### Silence, and what the driver does about it

There is no `*OPC?`, no `SYST:ERR?`, no `[^]` caret. So:

- Every command is checked against a **whitelist** before it is written.
  A rendered command that matches no entry is refused host-side, because
  a bad *rendering* (a format-string slip) is invisible to an argument
  range check and produces the same silence as a device fault.
- A command declared responsive that answers nothing raises
  `ADU218TimeoutError`, which is documented as **ambiguous by
  construction**: it cannot say whether the command was unknown, the
  argument was out of range, or the device is gone. Pretending otherwise
  would be exactly the misdiagnosis this device invites.
- A reply of the wrong width raises `ADU218ProtocolError`. Without the
  width check a desynced reply is a plausible number.
- **The session is not poisoned.** Unlike the SDM4065A, where a bad
  command leaves an error queued that surfaces on the *next* read, an
  ignored command here costs nothing — the next valid command answers
  normally. That is the one mitigating property.

`open()` **drains** the IN endpoint before doing anything else. Replies
queue on the endpoint rather than overwriting a slot, so a reply left by a
crashed previous process would be returned as the answer to this process's
first query — a silently wrong value, not an exception.

The read timeout is 200 ms. Measured worst cases: 16.65 ms for an idle
`PK`/`RPK0`, 16.68 ms for a post-actuation read-back, 7.35-8.19 ms for a
write-only ioctl. That is a 12x margin, re-confirmed at the shipping value
with zero replies left queued.

### Two index ranges, and one that reads correctly when wrong

Relays are `0..7`. **Input lines are `0..3`** — PORT A and PORT B are four
bits each, eight inputs in total. Counters are `0..7`. A single shared
validator would accept `RPA5`, the device would answer with silence, and
the operator would see a timeout three layers away from the bad argument.
So each range has its own coercion.

`RPy` replies **MSB first**: the leftmost character of `RPA` is line 3, not
line 0. Indexing the reply string directly is an off-by-three that reads
correctly for the all-zero case every unwired bench produces, which is why
the test for it asserts an asymmetric pattern.

**Three commands read the inputs, and their bit orders disagree.** That is
the reason `input_port_mask()` exists alongside `input_states()` and
`input_mask()`, rather than being a redundant fourth spelling of the same
read:

| Method | Command | What the bits do |
|---|---|---|
| `input_states()` | `RPA` / `RPB` | MSB-first *text*; the driver reverses it |
| `input_port_mask(port)` | `PA` / `PB` | LSB-weighted decimal — bit 0 **is** line 0 |
| `input_mask()` | `PI` | both ports in one byte, PORT A in the low nibble |

So `Py` is the only input read needing no transformation, and a caller that
wants one port's bits and wants to trust their positions should use it. `PI`
remains the right call for all eight lines at once — `input_port_mask` is
not a cheaper route to the same answer but a *different* answer, one port,
with the other port's state absent rather than masked off.

Index coercion rejects `bool` **before** `int`, since `bool` is an `int`
subclass: `relay_state(True)` would silently mean relay 1, and
`set_watchdog(True)` would arm a one-second hardware deadman on a bench
nobody expected to hold one.

### Safety

The relays are 1 A signal-level SSRs on instrument leads, not mains
contactors. That is a real difference from the PDU41002 and the policy
differs accordingly — but the direction of a relay's state is still worth
being careful about.

**`allowed_relays` defaults to all eight.** The PDU makes its allowlist
mandatory with no "all" default because a typo there de-powers mains. Here
the operator's stated policy is that the relays toggle freely with the
hardware watchdog available as the per-test interlock. Pass
`allowed_relays=` to narrow it.

**The allowlist guards *closing* a contact, not opening one.**
`set_relay_state` refuses to energise an unlisted relay and always
de-energises one; `reset_relays()` bypasses the list entirely. Both keep
the safe state reachable on exactly the benches most carefully configured
— a rule that also blocked de-energising would make a narrower policy the
more dangerous one. `set_relay_port` is the exception and enforces on the
whole mask, because `MKddd` moves all eight lines in one indivisible
command and there is no de-energise-only form of it. It is also checked
against the whole mask rather than the diff: "no change requested" depends
on a read that could be stale, and a policy that holds only when the
device agrees is not a policy.

**"Indivisible" is a claim about the command, not about the contacts.**
Eight `SKn`/`RKn` writes are eight USB transfers, so the port really does
visit `0b10101000` en route to `0b10101010`; one `MKddd` is one transfer,
so it does not. That is what "simultaneous" means here and all it means.
Skew *between* the eight contacts inside a single `MKddd` is unmeasured:
verification is a `PK` read-back, which reports the landed state and says
nothing about timing, and the manual gives no per-relay switching time to
compare against. If a circuit depends on make-before-break ordering,
measure it on your own bench — nothing here establishes it.

**The relays are rated for 1 switch per second at full load**
(`RELAY_MAX_SWITCH_HZ`), and the manual explicitly does not recommend the
ADU218 for PWM: PhotoMOS dissipation rises with switching speed. Nothing
in the driver enforces this, because the figure is qualified *at full
load* and no part of USB, HID or the ADU command set reports what a
contact is switching — a hard limit would throttle the dry-contact sweeps
that are most of this bench's use on the strength of a condition it cannot
observe. Note the inversion against the ADU208's mechanical relays at
10 CPS: the solid-state part is the *slower* one to cycle under load.

**`open()` reports what it found rather than fixing it.** Power-on relay
state is undocumented and USB suspend holds outputs in their last state,
so `open()` reads the port and logs a warning naming any energised relay.
It does not drive `MK000`, because the driver cannot know whether an
energised relay is holding something that must not be interrupted.
`reset_relays()` is one call away and is explicit.

**`close()` does not de-energise, and does not disarm the watchdog.** A
teardown that dropped contacts would make every `with` block a bench
event. The watchdog case is sharper: if it is armed, releasing the device
*is* the silence it exists to detect, so disarming at close would defeat
exactly the situation it was armed for.

**`is_open` means the link, not a contact.** That is the framework-wide
meaning (`agent/registry.py` publishes it as `"open"`), and it is the
opposite sign to a relay's "open". Both senses live in this device, so no
relay-facing name in the driver uses open/close/opened/closed —
`relay_state()` documents that `True` means *energised* — and a test
enforces the rule, because a method named `close_relay()` would also be
remotely callable with no writer claim.

### The watchdog

`WD0` off, `WD1` 1 s, `WD2` 10 s, `WD3` 1 minute. `WDn` sets **and** arms;
there is no separate arm step. While armed, the device de-energises all
eight relays by itself if no command arrives within the interval. No
software is in that decision path — a wedged process, a killed agent, an
unplugged cable and a panicking kernel all look identical to the device
and all drop the load. That is the point, and it is why this is the
recommended interlock for a test that must not leave a relay closed.

`WD1`'s one second was established by bisecting the silence window on
hardware; the trip falls in `(0.90, 1.10] s`. An earlier capture suggested
3.7 s, which was observation latency rather than a trip time.

Two properties you own once you arm it:

**Any command refeeds the timer** — including an invalid one, and
including a plain state read. So a status-polling loop *silently neuters
the watchdog*: ten rounds of "advance 9 s, read the relay states" keeps a
`WD2` watchdog fed across 90 s with zero trips. The feed has to come from
whatever is actually controlling the test.

**There is no keep-alive helper, deliberately.** A background feeder would
keep the watchdog fed precisely while the failure it guards against was
happening — an interlock that is inert and indistinguishable from a
working one. A test asserts no such method exists.

`WD` reads back `0` both for "timed out" and for "never enabled", so a
trip is invisible in isolation. The driver holds its own armed state and
`read_watchdog_tripped()` compares the two; it also writes `WD0` at
`open()` unless told not to, because a fresh process has no expectation to
compare against and would inherit an ambiguity. Pass
`disarm_watchdog=False` to inspect what a previous session left, at the
cost of that ambiguity.

### Counters

Each digital input has a hardware event counter, so transitions are caught
between polls. Counters wrap at 65535.

`REn` reads without clearing. `RCn` reads **and** clears — the only
command on the device that both answers and changes state, which makes it
the only one that must never be retried: if the reply is lost after the
device has already cleared, the count is gone permanently and a retry
reports 0, indistinguishable from "no events happened". So
`clear_counter()` propagates the timeout rather than retrying, and the
returned count is the only copy. Prefer `read_counter()` and difference
successive readings.

### De-bounce

Three settings: 0, 1, 2. Ontrak's web page lists a fourth (`NONE`), but
the manual bounds `n` to 0-2, the hardware captures show 0/1/2, and the
same four-option string appears on the ADU208 and ADU228 pages — shared
boilerplate. The bench unit reported `DB` = 1 out of the box, which is
also the simulator's default so that a driver returning a hardcoded 0
would be caught.

**A higher setting is a *shorter* filter.** Manual §6c:

| setting | filter width |
|---|---|
| 0 | 10 ms |
| 1 | 1 ms (device default) |
| 2 | 100 µs |

Both intuitive readings are wrong: 0 is not "off" — it is the *longest*
filter — and 2 does not filter hardest, it filters least. Somebody wanting
maximum contact de-bounce would reach for 2 and get 100 µs. Hence
`read_debounce_ms()` alongside `read_debounce()`, and `DEBOUNCE_MS` in the
driver; the raw setting number is not a duration and should not be printed
as though it were.

The practical consequence is that **the setting is unobservable at low
frequencies.** Measured on this bench against a 10 Hz square wave on PA3,
20 s per setting: DB0 10.042, DB1 9.992, DB2 9.992 counts/s — a 0.5 %
spread, i.e. indistinguishable. That is the expected result, not a fault:
every filter width is far shorter than the 50 ms half-period, so there is
nothing for any of them to reject. Telling the three apart needs a
stimulus whose period approaches 10 ms — a few hundred Hz — and the
counters are rated to only 1 kHz (`COUNTER_MAX_FREQUENCY_HZ`), so the
usable discrimination window is narrow. Above that rating the count
under-reports **silently**; the driver cannot detect it.

### Counters count cycles, not edges

The counters count **low-to-high transitions only** — one count per cycle.
Verified on hardware rather than assumed: at 10 Hz the device counter read
10.030 counts/s while host level-sampling independently saw 9.997 rising
*and* 9.997 falling edges per second. Counting both edges would have given
a ratio of 2.0; the measured ratio was 1.003.

Counters are 16-bit and **roll over from 65535 to 0**. Differencing
successive reads is the only correct way to use them, and the difference
must be taken modulo 65536 — a naive `after - before` goes sharply negative
exactly once per 65536 events. `read_counter()` and differencing is
preferred over `clear_counter()`, which destroys the only copy of what it
returns.

### API

```python
# reads (no mutator prefix — deliberate; see below)
is_open -> bool;  relay_count -> int;  input_count -> int
allowed_relays -> frozenset[int];  watchdog_setting -> int   # cached, no I/O
read_identity() -> ADU218Info               # from the descriptor, no round trip
relay_state(index) -> bool                  # True == energised
relay_states() -> dict[int, bool]           # one command, one instant
relay_mask() -> int
input_state(port, index) -> bool            # port "A"/"B", index 0-3
input_states() -> dict[str, tuple[bool, ...]]
input_port_mask(port) -> int                 # Py — one port's nibble, LSB = line 0
input_mask() -> int                          # PI — both ports, A in the low nibble
read_counter(index) -> int;  read_counters() -> dict[int, int]
read_debounce() -> int                      # the setting, 0-2
read_debounce_ms() -> float                 # the filter width; see the table
read_watchdog() -> int;  read_watchdog_tripped() -> bool

# writes (all prefix-matched, so the writer-claim gate catches them)
set_relay_state(index, on, *, verify=True) -> bool   # the verified read-back
set_relay_port(mask, *, verify=True) -> int
reset_relays(*, verify=True) -> int                  # bypasses the allowlist
clear_counter(index) -> int                          # destroys what it returns
set_debounce(setting) -> int
set_watchdog(setting) -> int

close(); __enter__; __exit__
```

`set_relay_state` returns the **verified** read-back rather than `None`,
because a write is unacknowledged on this device. A switch the driver
cannot confirm is a switch it should not claim, so the return value is the
confirmation and discarding it is the caller's explicit choice.
`verify=False` returns the *commanded* value instead — a real downgrade,
and the docstring says so.

Method names are constrained by `agent/dispatch.py`, which derives which
calls need a writer claim purely from name prefixes with no
driver-declared override. Every mutator therefore takes an existing prefix
(`set_`, `reset`, `clear_`). `clear_counter` is the interesting one: it is
a read that mutates, and its `clear_` prefix is what makes the gate catch
it — a name like `read_and_clear_counter` would not be gated.

Adding a prefix such as `relay_` to `_MUTATOR_PREFIXES` was rejected: it
would also capture `relay_state()`, making observation a privileged
operation.

### Exceptions

`ADU218Error(RuntimeError)` is the base. `ADU218ConnectionError`,
`ADU218ProtocolError` (the device answered and the answer is not
believable), `ADU218TimeoutError` (also a builtin `TimeoutError`),
`ADU218ValueError` (also a `ValueError`) and `ADU218PolicyError`.

`ADU218PolicyError` is deliberately **not** a `ValueError`: the index was
perfectly valid for the hardware, and the refusal is configured policy
rather than a malformed request. A caller catching `ValueError` to mean "I
passed something wrong" must not swallow it.

There is deliberately **no** `ADU218CommandError`. Every other driver here
has one, carrying the device's error *reply*; this device has no error
reply to carry, so a `CommandError` would imply a diagnostic that does not
exist. A test asserts its absence so nobody adds one by analogy.

### Not a `SwitchedPDU`, and not on the mains panel

The device key is absent from `registry.SWITCHED_PDU_KEYS` and from the
FUI's `PDU_KEYS`, on purpose in both cases: those gate run-engine outlet
setpoints and a fixed mains panel respectively, and mean "switches mains".
These are 1 A SSRs on instrument leads. The ADU218 gets an instrument-rail
slot with `kind: "switch"` instead. Tests pin both exclusions.

`agent/safety.py`'s `default_safe_state()` is inert for this device, and
that is documented rather than accidental — the case for inertness is
*stronger* here than for the PDU, because the hardware watchdog already
de-energises the relays when the agent stops talking, by a mechanism that
works when `default_safe_state()` cannot run at all. Relay switching is
also absent from `_ARMING_CALLS`: closing a signal relay is not arming an
output, and treating it as one would start a governor countdown on every
switch — a second, weaker deadman layered over the device's own. A test
asserts the intersection is empty so a rename cannot quietly create it.

No `interfaces.Switch` Protocol yet, per `CONTRIBUTING.md` convention 3.
The PDU is 1-indexed mains outlets and this is 0-indexed signal relays; a
Protocol generalised from the two would fit a third device badly.

### Discovery: identified passively, never probed

`SIGNATURES` carries VID `0x0A07` / PID `0x00DA` with `EXACT` confidence,
and `scan_usbfs()` finds it by reading `/sys/bus/usb`. Nothing is written
to the device to identify it, which is the ideal case — the QR10x and the
PDU both need a write-probe because they hide behind generic USB bridges.

`scan_usbfs()` returns `[]` rather than raising when the bus cannot be
enumerated at all. That is not defensive coding: `enumerate_devices()`
raising is correct for a *driver* about to open a device and wrong for a
*scan*, and because `discover()` builds one merged list, letting it
propagate took out **every other transport's results too** — a machine
with no USB sysfs reported no VISA instruments either.

### Simulator

`benchctrl.sim.adu218` is not a `SimDevice` behind a pty, because USB HID
has no byte stream to loop back. Instead `SimulatedAdu218Link` **subclasses
the production USBDEVFS link and overrides only `_transfer()`** (plus
lifecycle).

That is the whole reason the sim is trustworthy: framing, the mandatory
`0x01` report id, NUL padding and stripping, the report-id desync check,
the timeout mapping and `drain()` are all still the shipping code paths, so
a regression in any of them fails a sim-mode test. A hand-written
four-method stand-in would cover none of them. A test asserts the override
set stays that small.

The device model itself is pinned against the hardware captures rather
than against the manual: a test parses `tests/fixtures/adu218/reads.txt` at
run time and asserts every simulated reply width matches the captured one.
That is the fix for the failure mode `sim/qr10x.py` records — a simulator
built from the same misreading as the driver agrees with it, and the pair
passes every test while both are wrong.

The clock is manual by default (`advance(seconds)`), so the watchdog ladder
is deterministic rather than a race.

### MCP tools

19 tools, prefixed `adu218_`. The surface is **not** read-only: three of
them move physical contacts (`adu218_set_relay_state`,
`adu218_set_relay_port`, `adu218_reset_relays`) and `adu218_set_watchdog`
arms a hardware interlock whose effect outlives the connection. Each of
those docstrings says plainly what it does and points at `allowed_relays`
as an operator decision to ask about rather than widen.

`adu218_watchdog` folds the device read, the driver's held expectation and
the trip verdict into one result, deliberately: `read_watchdog_tripped()`
*clears* the expectation when it detects a trip, so a standalone tool would
let a model consume the only trace of a trip without seeing the setting it
must be compared against. Its docstring also warns that reading it refeeds
the timer.

### Tests, and the one that needs an instrument

Three files, and they answer different questions:

| File | Against | What only it can prove |
|---|---|---|
| `tests/test_usbfs_adu218.py` | fake ioctls | the request numbers, struct layout and errno mapping |
| `tests/test_bench_adu218.py` | the simulator | every parser, range check, policy and the watchdog ladder |
| `tests/test_remote_ontrak_adu218.py` | agent + simulator | the seven registration sites, four of which fail silently |
| `tests/test_hardware_ontrak_adu218.py` | the real device | that the relays actually move |

The last one exists because **the driver's own verification is the device
talking about itself.** `SKn` and `RKn` are unacknowledged, so
`set_relay_state()` confirms a switch by reading the same device back — which
catches a device that ignored the command, but not a *driver* whose read-back
is secretly its own commanded value. The only witness that is not the ADU218 is
another instrument.

So on this bench relay K0 is wired across the SDM4065A's leads, and the
hardware suite asserts the driver and the DMM agree on five alternating
transitions. Measured:

```
at open            driver=False  DMM=OVERLOAD (open contact)
K0 energised       driver=True   DMM=9.4237 ohm
K0 de-energised    driver=False  DMM=OVERLOAD (open contact)
K0 energised #2    driver=True   DMM=9.3958 ohm
reset_relays       driver=False  DMM=OVERLOAD (open contact)
```

Two details make that a real check rather than a decoration:

- **No resistance threshold is asserted, ever.** The same closed relay has
  measured 6.14 Ω, 10.69 Ω, 10.65 Ω and 9.40 Ω across four sessions,
  milliohm-stable *within* each, with the step traced to re-seated
  screw-clamped probes rather than to the relay. A threshold set from any one
  of those numbers misreads the others. What is asserted is the shape no probe
  seating can change: closed reads *a number*, open reads the DMM's **overload
  sentinel**. An open contact is not a large resistance, it is an unmeasurable
  one — so the two states cannot be confused by any amount of contact drift.
- **The suite was verified to fail on the defect it is for.** Patching
  `set_relay_state` to return its own argument without touching the device made
  three of the six tests fail, with the message written for exactly that case
  ("the switch was claimed and did not happen"). A hardware test that cannot
  fail is worse than no hardware test, because it reads as evidence.

The 2-wire function is used deliberately: 4-wire needs separate source and
sense leads, and with one pair across the relay `measure_resistance_4wire()`
returns drifting negatives — measured, not assumed.

The watchdog trip is deliberately **not** in the hardware suite. Arming it
de-energises every relay after a measured silence, which is correct behaviour
and a bad thing to do unattended on a shared bench; its ladder is tested
against a synthetic clock and its trip time was bisected by hand into
`tests/fixtures/adu218/watchdog.txt`.

Everything else in that file is about the USB stack rather than the relays:
that `USBDEVFS_BULK` really is serviced on an interrupt endpoint by this
kernel, that `CLAIMINTERFACE` succeeds with no driver to detach (so if a
future kernel drops Ontrak from `hid_ignore_list`, the failure arrives with an
explanation instead of as a mysterious `EBUSY` in the field), that a second
session is refused while the first holds the interface, and that discovery
names the device from sysfs with no probe write.

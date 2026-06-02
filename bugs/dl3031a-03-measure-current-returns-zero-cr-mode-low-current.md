# DL3031A: `:MEASure:CURRent:DC?` returns 0 in CR mode at currents below ~50 mA

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DL3031A programmable DC electronic load |
| Firmware version | 00.01.05.00.01 |
| Serial observed | DL3D232300106 |
| Transport | USB-TMC (USB0::0x1AB1::0x0E11::DL3D232300106::INSTR) |
| Discovered | 2026-05-30 (closed-loop validation against an external PSU) |
| Severity | **Medium** — measurement subsystem reports zero when current is flowing |

## Summary

When the DL3031A is operating in CR (constant-resistance) mode and
sinking less than ~50 mA, the `:MEASure:CURRent:DC?` query returns
`0.000000` even though current is demonstrably flowing through the
input terminals (verified by an external precision power-supply
measurement on the source side).

The defect appears specific to CR mode at low currents. CC-mode
measurements at the same current levels return correct values.

## Reproduction

Test setup: Rigol DP2031 programmable PSU CH1 wired to DL3031A input.
DP2031 sources a fixed voltage; DL3031A in CR mode at varying
resistance presents the load.

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
psu = rm.open_resource("USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR")
load = rm.open_resource("USB0::0x1AB1::0x0E11::DL3D232300106::INSTR")
for inst in (psu, load):
    inst.timeout = 3000
    inst.read_termination = "\n"
    inst.write_termination = "\n"

# PSU: source 12 V CV
psu.write("*RST"); psu.write("*CLS")
time.sleep(0.5)
psu.write(":SOURce1:VOLTage 12.0")
psu.write(":SOURce1:CURRent 1.5")
psu.write(":OUTPut:STATe CH1,ON")

# Load: CR mode, sweep resistance
load.write(":SOURce:FUNCtion RESistance")
load.write(":SOURce:RESistance:RANGe MAX")
load.write(":SOURce:INPut:STATe 1")
time.sleep(0.5)

print(f"{'R_set':>7} {'PSU I_mA':>10} {'Load I_mA':>11} {'Load V':>8}")
for r in (50.0, 100.0, 200.0, 500.0, 1000.0):
    load.write(f":SOURce:RESistance {r:.1f}")
    time.sleep(0.5)
    i_psu = float(psu.query(":MEASure:CURRent:DC? CH1").strip())
    i_load = float(load.query(":MEASure:CURRent:DC?").strip())
    v_load = float(load.query(":MEASure:VOLTage:DC?").strip())
    print(f"{r:7.0f} {i_psu*1000:10.2f} {i_load*1000:11.2f} {v_load:8.3f}")

load.write(":SOURce:INPut:STATe 0")
psu.write(":OUTPut:STATe CH1,OFF")

# Observed output:
#   R_set    PSU I_mA    Load I_mA    Load V
#       50     199.20         0.00     6.901   ← load reports 0 at ~200 mA flow
#      100     112.90         0.00     6.901   ← same
#      200      51.20        51.20    11.985   ← starts reporting correctly
#      500      20.40        18.75    11.986   ← correct above ~50 mA threshold
#     1000       3.74         9.02    11.986   ← measurement degrades again at <20 mA
```

## Expected behaviour

`:MEASure:CURRent:DC?` should report the actual input current (within
the device's documented accuracy spec of ±0.05 % + 0.025 % full-scale)
regardless of operating mode.

## Actual behaviour

In CR mode with currents in the 50–200 mA range, the query returns
`0.000000` while the external source-side measurement confirms 100–
200 mA is flowing. At very low currents (< 20 mA), the reading is
non-zero but inaccurate by a factor of 2-3x.

## Impact

- Closed-loop test rigs that cross-check load current against source
  current have no consistent reference point for CR-mode measurements
  below ~50 mA.
- Power profiling of low-current devices (IoT modules in sleep state,
  microcontroller quiescent current, etc.) using the DL3031A as the
  load instrument is unusable.
- The DL3031A's documented current resolution at low currents (e.g. low
  range claims sub-mA resolution) cannot be relied on in CR mode.

## Workaround

- Use CC mode (where measurement is accurate at the same current
  levels) when low-current load measurement is needed.
- Cross-check with an external ammeter or source-side current
  measurement.

## Notes for the vendor

- Tested with the device freshly power-cycled and with `*RST` + `*CLS`
  between sweeps. Both produce the same result.
- The `:SOURce:RESistance:RANGe MAX` setting was used to put the
  resistance range at maximum; the same behaviour appears at other
  range settings.
- Co-discovered with bugs
  [`dl3031a-01`](dl3031a-01-func-mode-stuck-requires-power-cycle.md)
  and [`dl3031a-02`](dl3031a-02-func-mode-query-returns-incorrect-state.md);
  the relationship (if any) between the WAV-mode reporting and the
  CR-mode measurement defect is unclear — they may share a root
  cause in the function-mode dispatcher.

## Related driver reference

- `benchctrl/drivers/rigol_dl3031a/driver.py` — `measure_current()`,
  `set_resistance()`

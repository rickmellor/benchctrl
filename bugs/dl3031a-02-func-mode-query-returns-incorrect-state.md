# DL3031A: `:SOURce:FUNCtion:MODE?` reports incorrect mode after `*RST` and after power-cycle

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DL3031A programmable DC electronic load |
| Firmware version | 00.01.05.00.01 |
| Serial observed | DL3D232300106 |
| Transport | USB-TMC (USB0::0x1AB1::0x0E11::DL3D232300106::INSTR) |
| Discovered | 2026-05-30 (bench-verified across several test runs) |
| Severity | **High** — query returns wrong value; host code cannot trust the device's reported mode |

## Summary

After `*RST`, and after a physical power-cycle, the `:SOURce:FUNCtion:MODE?`
query returns `WAV` even when the device is operating correctly in the
FIX (static-setpoint) subsystem. CC / CV / CR / CP setpoints work as
expected, the input regulator engages correctly, but the function-mode
query lies about which subsystem is active.

This is independent of the bug reported in
[`dl3031a-01-func-mode-stuck-requires-power-cycle.md`](dl3031a-01-func-mode-stuck-requires-power-cycle.md);
that report covers the "set silently fails" defect, this one covers the
"query returns wrong value" defect.

## Reproduction

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
load = rm.open_resource("USB0::0x1AB1::0x0E11::DL3D232300106::INSTR")
load.timeout = 3000
load.read_termination = "\n"
load.write_termination = "\n"

# 1. Right after a physical power-cycle, with no prior writes:
print("FUNC?       :", repr(load.query(":SOURce:FUNCtion?").strip()))
print("FUNC:MODE?  :", repr(load.query(":SOURce:FUNCtion:MODE?").strip()))
# Observed:
#   FUNC?       : 'CC'
#   FUNC:MODE?  : 'WAV'    ← incorrect

# 2. Try setting FIX explicitly:
load.write(":SOURce:FUNCtion:MODE FIXed")
time.sleep(0.3)
print("FUNC:MODE? after FIX :", repr(load.query(":SOURce:FUNCtion:MODE?").strip()))
print("Error                :", load.query(":SYSTem:ERRor?").strip())
# Observed:
#   FUNC:MODE? after FIX : 'WAV'    ← still wrong, no error queued

# 3. Demonstrate the device is actually in FIX mode by driving a load:
#    Connect a known voltage source to the input first.
load.write(":SOURce:FUNCtion CURRent")
load.write(":SOURce:CURRent:RANGe 6.0")
load.write(":SOURce:CURRent 0.100")
load.write(":SOURce:INPut:STATe 1")
time.sleep(0.5)
print("Actual measured I    :", load.query(":MEASure:CURRent:DC?").strip(), "A")
# Observed against a 12 V / 1.5 A source on the input:
#   Actual measured I    : 0.095 A   (CC at 100 mA setpoint is honoured;
#                                     device IS in FIX, query is wrong)
load.write(":SOURce:INPut:STATe 0")
```

## Expected behaviour

`:SOURce:FUNCtion:MODE?` should return the actual currently-active
regulation subsystem identifier. After `*RST` (and after a power-cycle
that restores factory defaults), the value should be `FIX` since the
static CC / CV / CR / CP setpoint subsystem is what's actually active
and honouring writes.

## Actual behaviour

The query returns `WAV` (waveform-display subsystem) even when:
- The device has just been power-cycled
- No prior writes have placed the device into WAV mode
- The actual regulation behaviour shows FIX-mode CC/CV/CR/CP setpoints
  are being honoured

`:SOURce:FUNCtion:MODE FIXed` writes that should correct the reported
value do not. No error is queued.

## Impact

Host code that reads `:SOURce:FUNCtion:MODE?` to verify the device is
in the expected state has no way to trust the reply. Defensive checks
of the form

```python
assert load.get_function_mode() == "FIX", "expected FIX mode"
```

fail spuriously even when the device is operating correctly. This
forced our driver to treat the mode query as informational only, and
verify mode indirectly by sending a setpoint and measuring the result.

## Workaround

- Don't trust `:SOURce:FUNCtion:MODE?` returns.
- Verify mode behaviourally: send a known setpoint, drive a known load,
  measure the result, infer mode from the response.

## Related driver reference

- `benchctrl/drivers/rigol_dl3031a/driver.py` — `get_function_mode()`
- `tests/test_bench_rigol_dp2031.py` — the comment block explaining
  why DP2031 closed-loop tests had to defer this DL3031A measurement
  cross-check during initial bring-up.

## Related reports

- [`dl3031a-01-func-mode-stuck-requires-power-cycle.md`](dl3031a-01-func-mode-stuck-requires-power-cycle.md) —
  the related bug where `:SOURce:FUNCtion:MODE FIXed` writes silently
  no-op once stuck in a non-FIX state.

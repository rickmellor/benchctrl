# DL3031A: `:SOURce:FUNCtion:MODE FIXed` silently no-ops once device enters LIST / WAV / BATTery / OCP / OPP mode

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DL3031A programmable DC electronic load |
| Firmware version | 00.01.05.00.01 |
| Serial observed | DL3D232300106 |
| Transport | USB-TMC (USB0::0x1AB1::0x0E11::DL3D232300106::INSTR) |
| Discovered | 2026-05-29 (bench-verified across several test runs through 2026-05-30) |
| Severity | **High** — blocks reliable scripted automation; recovery requires physical power-cycle |

## Summary

Once the DL3031A's function-mode register (`:SOURce:FUNCtion:MODE`) has been
moved into any of `LIST`, `WAVeform`, `BATTery`, `OCP`, or `OPP`, subsequent
attempts to set it back to `FIXed` (or its alias `FIX`) are silently
ignored. No SCPI error is queued, the query continues to report the
non-FIX value, and the device continues to behave in its prior mode.
None of the conventional SCPI recovery mechanisms restore FIX mode —
only physically power-cycling the unit returns it to a state where
static CC / CV / CR / CP setpoints work.

## Reproduction

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
load = rm.open_resource("USB0::0x1AB1::0x0E11::DL3D232300106::INSTR")
load.timeout = 3000
load.read_termination = "\n"
load.write_termination = "\n"

# 1. Confirm starting state. After a power-cycle this should be FIX.
print("Initial FUNC:MODE?:", load.query(":SOURce:FUNCtion:MODE?").strip())

# 2. Move to LIST mode via a normal programmed sequence.
#    (Any of WAV / BATT / OCP / OPP demonstrate the same defect — LIST
#    is just the most common entry path.)
load.write(":SOURce:FUNCtion CURRent")
load.write(":SOURce:CURRent:RANGe 6.0")
# Program a minimal 3-step LIST:
load.write(":SOURce:LIST:STEP 3")
load.write(":SOURce:LIST:LEVel 1,0.001")
load.write(":SOURce:LIST:WIDth 1,1.0")
load.write(":SOURce:LIST:SLEW 1,0.5")
load.write(":SOURce:LIST:LEVel 2,0.030")
load.write(":SOURce:LIST:WIDth 2,0.05")
load.write(":SOURce:LIST:SLEW 2,0.5")
load.write(":SOURce:LIST:LEVel 3,0.001")
load.write(":SOURce:LIST:WIDth 3,1.0")
load.write(":SOURce:LIST:SLEW 3,0.5")
load.write(":SOURce:FUNCtion:MODE LIST")
time.sleep(0.3)
print("After enter LIST:", load.query(":SOURce:FUNCtion:MODE?").strip())
# → expected and observed: "LIST"

# 3. Try every documented recovery path.
for cmd in [
    ":SOURce:FUNCtion:MODE FIXed",   # the canonical fix
    ":SOURce:FUNCtion:MODE FIX",     # short form
    "*RST",                          # reset device
    "*CLS",                          # clear status
    ":SOURce:INPut:STATe 0",         # disable input
    ":SOURce:FUNCtion:MODE BATTery", # cycle through another mode
    ":SOURce:FUNCtion:MODE FIXed",   # then try FIX again
    "*WAI",                          # wait for pending
    ":SOURce:FUNCtion:MODE FIXed",   # one more try
]:
    load.write(cmd)
    time.sleep(0.3)
    mode = load.query(":SOURce:FUNCtion:MODE?").strip()
    err = load.query(":SYSTem:ERRor?").strip()
    print(f"after {cmd!r}: FUNC:MODE={mode}  err={err}")

# 4. The only recovery is a physical power-cycle of the DL3031A.
```

## Expected behaviour

`:SOURce:FUNCtion:MODE FIXed` should reliably return the device to the
FIX (static-setpoint) regulation subsystem, regardless of which mode the
device was previously in. SCPI commands documented as state-setters
must always be able to restore the state they target. Alternatively, the
device should queue a `-221,"Settings conflict"` (or similar) error so
the host can detect the failure.

## Actual behaviour

The write is accepted with no error queued. `:SOURce:FUNCtion:MODE?`
continues to return the prior non-FIX value. Subsequent CC / CV / CR /
CP setpoint writes are accepted but the input regulator does not honour
them — sourcing a known voltage into the input shows only the small
input-bias current (~10 mA from the front-end MOSFET) regardless of the
configured setpoint.

`*RST` does not recover.

`*CLS` does not recover.

Toggling `:SOURce:INPut:STATe` does not recover.

Cycling through other modes (BATT → FIX, OCP → FIX) does not recover.

The only recovery path observed is **physically power-cycling the
DL3031A**.

## Impact

This effectively means any automated test sequence that uses LIST,
transient WAVeform, BATTery, OCP, or OPP modes leaves the device
unrecoverable to FIX mode within the same session. Subsequent tests in
the same suite cannot reliably use static CC / CV / CR / CP setpoints
until a human power-cycles the device.

For automated bench-test rigs this is a significant blocker — there is
no SCPI-only way to restore the device to a clean state.

## Workaround

1. Treat any entry into LIST / WAV / BATT / OCP / OPP as a one-way
   transition for the rest of the session and reorganise test sequences
   to do all static-mode work first.
2. Driver-level: detect the stuck condition via `get_function_mode()`
   after teardown, log a clear operator warning ("DL3031A stuck in
   {MODE} mode — power-cycle before reuse"), and skip subsequent tests
   that need FIX mode.
3. Operator-side: power-cycle the device between test runs that require
   FIX-mode start.

## Notes for the vendor

- Tested across multiple `*RST` + `*CLS` sequences with `*OPC?` waits in
  between; the FIX recovery never takes effect.
- The fact that `*RST` does not restore the function mode is itself
  arguably a separate bug — `*RST` is defined by IEEE 488.2 to restore
  factory defaults, and the function mode is the most fundamental state.

## Related driver reference

- `benchctrl/drivers/rigol_dl3031a/driver.py` — `set_function_mode()` /
  `get_function_mode()`
- Already documented in `KNOWN_LIMITATIONS.md § F-3`.

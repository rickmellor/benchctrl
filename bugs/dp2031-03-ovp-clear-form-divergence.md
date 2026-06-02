# DP2031: `:OUTPut:OVP:CLEar` vs `:SOURce<n>:VOLTage:PROTection:CLEar` documentation-vs-behaviour divergence

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DP2031 triple-output programmable DC power supply |
| Firmware version | 01.00.01.00.16 |
| Serial observed | DP2A243500269 |
| Transport | USB-TMC (USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR) |
| Discovered | 2026-05-30 |
| Severity | **Low** — behavioural difference between two forms documented as aliases |

## Summary

The DP2000 programming guide documents two paths to clear a latched
OVP alarm:

- `:OUTPut:OVP:CLEar [<src>]`
- `[:SOURce<n>]:VOLTage:PROTection:CLEar`

The guide treats these as equivalent. Bench-verified behaviour shows
they differ in their effect on the channel's output-enable state after
the latch is cleared:

- `:OUTPut:OVP:CLEar CH<n>` — clears the latch; output stays OFF.
- `:SOURce<n>:VOLTage:PROTection:CLEar` — clears the latch AND
  re-enables the output (returns CHn to its pre-trip enabled state).

This isn't necessarily a bug — both behaviours are useful — but the
docs should make the distinction explicit so callers can pick the right
form deliberately rather than discover it by accident.

## Reproduction

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
psu = rm.open_resource("USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR")
psu.timeout = 3000
psu.read_termination = "\n"
psu.write_termination = "\n"

def trip_ch1():
    """Configure CH1 to immediately OVP-trip on enable."""
    psu.write("*RST"); psu.write("*CLS")
    time.sleep(0.5)
    psu.write(":SOURce1:VOLTage 3.0")
    psu.write(":SOURce1:CURRent 0.5")
    psu.write(":OUTPut:OVP:VALue CH1,2.0")  # OVP below setpoint
    psu.write(":OUTPut:OVP:STATe CH1,ON")
    psu.write(":OUTPut:STATe CH1,ON")
    time.sleep(0.4)  # latch settles in ~150-250 ms
    return (
        psu.query(":OUTPut:OVP:ALAR? CH1").strip(),
        psu.query(":OUTPut:STATe? CH1").strip(),
    )

# Form 1: :OUTPut:OVP:CLEar
trip = trip_ch1()
print(f"After trip:    ALAR={trip[0]}  STATe={trip[1]}")  # ALAR=1, STATe=0
psu.write(":OUTPut:OVP:CLEar CH1")
time.sleep(0.3)
alar = psu.query(":OUTPut:OVP:ALAR? CH1").strip()
state = psu.query(":OUTPut:STATe? CH1").strip()
print(f"After :OUTP:OVP:CLE:    ALAR={alar}  STATe={state}")
# Observed: ALAR=0, STATe=0   ← latch cleared, output stays off

psu.write(":OUTPut:STATe CH1,OFF")
psu.write(":OUTPut:OVP:STATe CH1,OFF")

# Form 2: :SOURce:VOLTage:PROTection:CLEar
trip = trip_ch1()
print(f"\nAfter trip:    ALAR={trip[0]}  STATe={trip[1]}")  # ALAR=1, STATe=0
psu.write(":SOURce1:VOLTage:PROTection:CLEar")
time.sleep(0.3)
alar = psu.query(":OUTPut:OVP:ALAR? CH1").strip()
state = psu.query(":OUTPut:STATe? CH1").strip()
print(f"After :SOUR:VOLT:PROT:CLE:  ALAR={alar}  STATe={state}")
# Observed: ALAR=0, STATe=1   ← latch cleared AND output re-enabled

psu.write(":OUTPut:STATe CH1,OFF")
psu.write(":OUTPut:OVP:STATe CH1,OFF")
```

## Expected behaviour

The programming guide should be explicit about whether the two
`*:CLEar` forms have the same side-effect on output state, OR the two
forms should behave identically.

If they're intended to differ, document the distinction:

> `:OUTPut:OVP:CLEar CH<n>` clears the OVP latch and leaves the output
> in its tripped state (OFF). The caller must explicitly re-enable.
>
> `:SOURce<n>:VOLTage:PROTection:CLEar` clears the OVP latch and
> re-enables the channel's output to its pre-trip state.

If they're intended to be identical, fix the behaviour so they are.

## Actual behaviour

- `:OUTPut:OVP:CLEar CH<n>` → latch cleared, output stays OFF.
- `:SOURce<n>:VOLTage:PROTection:CLEar` → latch cleared, output
  **re-enabled**.

## Impact

Low. The behaviour is consistent within each form, so callers can rely
on whichever they pick — they just need to know the difference. The
benchctrl driver picked `:OUTPut:OVP:CLEar` deliberately because
"clear without re-enabling" is the safer default for most workflows
(after an OVP trip, you typically want the operator to confirm the
fault is resolved before re-energising the channel).

## Workaround

Pick the form that matches your intended behaviour:
- "Acknowledge the trip but don't re-enable" → `:OUTPut:OVP:CLEar`
- "Clear and re-enable in one step" → `:SOURce<n>:VOLTage:PROTection:CLEar`

## Related driver reference

- `benchctrl/drivers/rigol_dp2031/driver.py` — `clear_ovp()`,
  `clear_ocp()` (driver picks the `:OUTPut:OVP:CLEar` form)
- `KNOWN_LIMITATIONS.md § F-3.5` (DP2031 bench-discovered quirks)

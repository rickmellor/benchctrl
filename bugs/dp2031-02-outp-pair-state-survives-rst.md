# DP2031: `:OUTPut:PAIR` state survives `*RST`

## Metadata

| Field | Value |
|---|---|
| Device | Rigol DP2031 triple-output programmable DC power supply |
| Firmware version | 01.00.01.00.16 |
| Serial observed | DP2A243500269 |
| Transport | USB-TMC (USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR) |
| Discovered | 2026-05-30 |
| Severity | **High** — `*RST` does not restore factory default; safety implications |

## Summary

`*RST` does not clear the `:OUTPut:PAIR` setting. If the device is left
in `:OUTPut:PAIR PARallel` (CH1 and CH2 internally paralleled) or
`:OUTPut:PAIR SERies` (CH1 and CH2 internally tied for up to 64 V
composite), `*RST` leaves the channels paired. Subsequent attempts
to configure CH1 and CH2 as independent channels silently affect both
in a coupled way — for example, setting CH1 voltage will also affect
CH2's reported voltage.

IEEE 488.2 §10.32.1 specifies `*RST` as restoring "device-specific
known state". The channel-pair / topology configuration is the most
fundamental device-level state and should be returned to default
(`OFF`) by `*RST`.

This is particularly serious because of the **safety implications**:
a fresh session that assumes independent channels will, after a prior
session that left PAIR=PARallel, drive CH1 and CH2 in parallel without
any indication to the host.

## Reproduction

```python
import pyvisa, time
rm = pyvisa.ResourceManager()
psu = rm.open_resource("USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR")
psu.timeout = 5000
psu.read_termination = "\n"
psu.write_termination = "\n"

# Set up a known non-default state
psu.write(":OUTPut:STATe CH1,OFF")
psu.write(":OUTPut:STATe CH2,OFF")
psu.write(":OUTPut:PAIR PARallel")
time.sleep(2.5)  # wait for transition (see bug dp2031-01)
print("PAIR before *RST:", psu.query(":OUTPut:PAIR?").strip())
# → "PARALLEL"

# Issue *RST — IEEE 488.2 contract is to restore factory default
psu.write("*RST")
time.sleep(1.0)
print("PAIR after  *RST:", psu.query(":OUTPut:PAIR?").strip())
# → still "PARALLEL"   ← bug

# Demonstrate the safety problem:
psu.write(":SOURce1:VOLTage 12.0")
psu.write(":SOURce1:CURRent 1.0")
time.sleep(0.3)
v_ch1 = psu.query(":SOURce1:VOLTage?").strip()
v_ch2 = psu.query(":SOURce2:VOLTage?").strip()
i_ch2 = psu.query(":SOURce2:CURRent?").strip()
print(f"After 'CH1-only' write: CH1 V={v_ch1}  CH2 V={v_ch2}  CH2 I={i_ch2}")
# → CH1 V=12.000, CH2 V=12.000, CH2 I=1.000
#   The "CH1-only" write affected CH2 because PAIR survived *RST.

# Clean up
psu.write(":OUTPut:PAIR OFF")
time.sleep(2.0)
```

## Expected behaviour

After `*RST`, `:OUTPut:PAIR?` should return `OFF` (the factory default).
All three channels should be configured as independent outputs with no
internal cross-coupling. Voltage / current writes targeting CH1 should
affect only CH1.

## Actual behaviour

`*RST` leaves `:OUTPut:PAIR` at its prior value. If the device was in
`PARallel` (or `SERies`), it remains there. Subsequent per-channel
writes affect the paired channels silently.

## Impact

- **Safety**: a script that opens the device, runs `*RST`, and proceeds
  to drive CH1 with a high voltage / current without first checking
  `:OUTPut:PAIR?` will inadvertently drive CH2 to the same setpoint.
  In `SERies` mode the composite CH1+CH2 voltage can reach 64 V, which
  could damage a DUT that was only designed for CH1's nominal 32 V
  envelope.
- **Reproducibility**: test sessions cannot rely on `*RST` for clean
  state. Every script must explicitly write `:OUTPut:PAIR OFF` after
  `*RST` (and wait the >1 s settle described in
  [`dp2031-01-outp-pair-query-returns-stale-off-during-transition.md`](dp2031-01-outp-pair-query-returns-stale-off-during-transition.md)
  before proceeding).
- **Hidden coupling**: a fresh session has no way to know the prior
  session's last state without an explicit query.

## Workaround

Every session must do explicitly, after `*RST`:

```python
psu.write(":OUTPut:PAIR OFF")
time.sleep(2.0)  # see bug dp2031-01 for the transition delay
assert psu.query(":OUTPut:PAIR?").strip() == "OFF"
```

This is what the benchctrl driver does in its `reset()` helper.

## Related driver reference

- `benchctrl/drivers/rigol_dp2031/driver.py` — `set_channel_pair()`,
  `get_channel_pair()`, `reset()`
- `KNOWN_LIMITATIONS.md § F-3.5` (DP2031 bench-discovered quirks)

## Notes for the vendor

- Other channel-pair state can also survive `*RST`; the report focuses
  on `:OUTPut:PAIR` because it has the most severe safety implications,
  but tracking (`:OUTPut:TRACk`) and sync (`:SYSTem:SYNC`) should also
  be audited for `*RST` compliance.
- Restoring `:OUTPut:PAIR OFF` on `*RST` is consistent with how other
  Rigol products (e.g. DP800-series) behave.

# SDM4065A: §7.4.5's "default 2 kΩ" does not describe the reset state, and `CONFigure:RESistance DEF` leaves autoranging enabled

**Instrument:** Siglent SDM4065A 6½-digit bench DMM
**Serial:** SDM46A0CA00021
**Firmware:** 0.0.0.20
**Interface:** USB-TMC (host is a Linux SBC using pyvisa-py/libusb; no
kernel `usbtmc` module involved)
**Manual:** SDM4000A series Remote Control Manual, §7.4.5, §7.4.6
**Severity:** low individually, but it silently costs a factor of ten on the
percent-of-range accuracy term, which is invisible in the reading.

This is the fourth of four independent reports from the same unit; see
`SDM4065A-firmware-bug-reports-README.md` for the set. The others cover
`RESistance:NULL:VALue` not clearing `NULL:VALue:AUTO` (1); §7.4.7 documenting
an autozero mnemonic (`AZ`) the firmware does not implement, plus the USB-TMC
wedge that follows from querying it (2); and `*CLS` not clearing the error
queue (3).

There are two findings: one is a documentation ambiguity, the other a firmware
inconsistency between two commands that should behave identically.

## Finding 1 — "default 2 kΩ" is not the reset state

§7.4.5 states, for both the generic and the explicit SDM4065A parameter lists:

> `<range>`: {200Ω|2kΩ|20kΩ|200kΩ|1MΩ|10MΩ|100MΩ}, **default 2 kΩ**
> Typical Return: `+2.00000000E+03`

A controller author reads that as the range the instrument is on after a reset.
It is not. Measured after `*RST`, on both resistance functions:

```
*RST
RESistance:RANGe:AUTO?      -> 1                    <-- autoranging is ON
RESistance:RANGe?           -> +2.00000000E+02      <-- 200 Ω, not 2 kΩ
FRESistance:RANGe:AUTO?     -> 1
FRESistance:RANGe?          -> +2.00000000E+02
```

Identical after a bare `CONFigure:RESistance` or `CONFigure:FRESistance`.

The 2 kΩ figure *is* correct as the value of the `DEF` parameter —
`RESistance:RANGe? DEF` returns `+2.00000000E+03` — but that is a different
thing from the power-on range, and §7.4.5 does not distinguish them. §7.4.6
separately and correctly documents autoranging as "default ON", which means the
two sections read as contradictory: §7.4.5's own note states that "Selecting a
fixed range ([SENSe:]<function>:RANGe) disables auto ranging", so a 2 kΩ
default range and an ON default autorange cannot both describe the reset state.

### Why it matters

The reading looks right either way, so nothing flags the mistake. A controller
that trusts §7.4.5 and skips setting the range believes it is on a pinned 2 kΩ
range when it is actually autoranging. Two consequences:

- **Accuracy.** The datasheet's resistance accuracy is
  ±(% of reading + % of range). A 100 Ω DUT measured believing the range is
  2 kΩ has its error budget computed against the wrong range term.
- **Stability.** With autoranging on, `RANGe?` reports whatever range the
  instrument has currently selected, and that moves with the input. So the
  readback is indistinguishable between "pinned to 200 Ω" and "autoranging,
  presently on 200 Ω" — a controller cannot tell from `RANGe?` alone whether
  its range is stable. Only `RANGe:AUTO?` disambiguates it.

  We hit this directly while writing the test for finding 2. On a 100 Ω DUT,
  after `CONFigure:RESistance DEF`:

  ```
  CONFigure:RESistance DEF
  RESistance:RANGe?           -> +2.00000000E+03    (queried immediately)
  ...after a reading has been taken...
  RESistance:RANGe?           -> +2.00000000E+02    (autorange has moved)
  ```

  Same command, two different answers, with nothing in between but a
  measurement. That is correct behaviour for an autoranging instrument — but it
  means a controller that follows §7.4.5, believes it has a 2 kΩ default, and
  validates by reading `RANGe?` back can get either answer depending on
  timing. `RANGe:AUTO?` is the only stable signal.

## Finding 2 — `CONFigure:RESistance DEF` does not disable autoranging

§7.4.5's note is unambiguous:

> Selecting a fixed range (`[SENSe:]<function>:RANGe`) disables auto ranging.

Two commands that both select the 2 kΩ range behave differently. Measured, each
from a fresh `*RST`:

```
*RST
RESistance:RANGe DEF
RESistance:RANGe:AUTO?      -> 0                    <-- correct
RESistance:RANGe?           -> +2.00000000E+03

*RST
CONFigure:RESistance DEF
RESistance:RANGe:AUTO?      -> 1                    <-- autoranging still ON
RESistance:RANGe?           -> +2.00000000E+03      <-- but range says 2 kΩ
```

An explicit numeric range through `CONFigure` behaves correctly:

```
*RST
CONFigure:RESistance 2000
RESistance:RANGe:AUTO?      -> 0                    <-- correct
RESistance:RANGe?           -> +2.00000000E+03
```

So `CONFigure:RESistance DEF` leaves the instrument in the state finding 1
describes — autoranging on, with a range readback that suggests it is pinned. Of
the two `DEF` paths, only the `RESistance:RANGe` one disables autoranging, and
of the three `CONFigure` forms only the numeric one does.

No error is queued for any of these, and `*ESR?` reads `0` throughout, so the
inconsistency is silent. (We checked with `*ESR?` rather than the error queue
because the queue is unreliable on this unit — see report 3.)

## Expected vs actual

| Command | Expected (§7.4.5, §7.4.6) | Actual (fw 0.0.0.20) |
|---|---|---|
| `*RST` then `RESistance:RANGe?` | `+2.00000000E+03` per §7.4.5 | `+2.00000000E+02`, autorange ON |
| `*RST` then `RESistance:RANGe:AUTO?` | `1` per §7.4.6 | `1` — correct |
| `RESistance:RANGe DEF` | 2 kΩ, autorange off | 2 kΩ, autorange off — correct |
| `CONFigure:RESistance 2000` | 2 kΩ, autorange off | 2 kΩ, autorange off — correct |
| `CONFigure:RESistance DEF` | 2 kΩ, autorange off | 2 kΩ, **autorange still ON** |

Reproduced across two separate sessions with a power cycle in between, on both
`RESistance` and `FRESistance`.

## Not a bug, for completeness

Two behaviours we checked in the same run and found correct, in case they are
useful as controls:

- Per-function range memory works as §7.1 describes: `CONFigure:RESistance
  2000` left `FRESistance` untouched at autorange/200 Ω.
- `RESistance:RANGe? MIN` returns `+2.00000000E+02` and `MAX` returns
  `+1.00000000E+08`, both matching the SDM4065A column of §7.4.5.

## Workaround

Pin the range explicitly with a numeric argument and verify both nodes, never
just `RANGe?`:

```
CONFigure:RESistance 200
RESistance:RANGe:AUTO?      -> 0     (must be checked; RANGe? alone is ambiguous)
```

Our driver treats the post-reset resistance state as "autorange on, `RANGe?`
reports 200 Ω" rather than the documented 2 kΩ, and exposes the autorange flag
alongside the range so a caller can tell a pinned range from a selected one.

## Requests

1. Please clarify §7.4.5 to distinguish the value of the `DEF` parameter (2 kΩ)
   from the range in effect after a Factory Reset or `CONFigure` (200 Ω with
   autoranging enabled), and reconcile it with §7.4.6's "default ON".
2. Please make `CONFigure:<function> DEF` disable autoranging, consistent with
   §7.4.5's note and with the numeric form — or document the difference if it is
   intentional.
3. Please confirm whether the same applies to the other subsystems that share
   this wording (`VOLTage`, `CURRent`, `CAPacitance` — §7.6.5, §7.2.5, §7.7.5).
   We tested resistance only.
4. Please confirm whether other SDM4000A models and firmware revisions are
   affected.

## Host details

- Linux SBC (aarch64), Python `pyvisa` 1.16.2 with the pure-Python `pyvisa-py`
  backend over `libusb`. No kernel `usbtmc` driver bound.
- VISA resource string as enumerated:
  `USB0::62700::4640::SDM46A0CA00021::0::INSTR` (pyvisa-py renders the USB
  VID/PID in decimal; `62700` = `0xF4EC`, `4640` = `0x1220`).
- `*IDN?` returns
  `Siglent Technologies,SDM4065A,SDM46A0CA00021,0.0.0.20`.

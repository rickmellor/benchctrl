# SDM4065A: `RESistance:NULL:VALue` does not disable `NULL:VALue:AUTO`

**Instrument:** Siglent SDM4065A 6½-digit bench DMM
**Serial:** SDM46A0CA00021
**Firmware:** 0.0.0.20
**Interface:** USB-TMC (host is a Linux SBC using pyvisa-py/libusb; no
kernel `usbtmc` module involved)
**Manual:** SDM4000A series Remote Control Manual, §7.4.2 – §7.4.4

This is the first of four independent reports from the same unit; see
`SDM4065A-firmware-bug-reports-README.md` for the set. The others cover §7.4.7
documenting an autozero mnemonic the firmware does not implement plus the
USB-TMC wedge that follows from querying it (2), `*CLS` not clearing the error
queue and the queue latching into reporting "No Error" (3), and the documented
default resistance range not matching the reset state (4). All four are
unrelated.

## Summary

Writing an explicit null offset with `RESistance:NULL:VALue <value>` leaves
automatic null-value selection **armed**. The manual (§7.4.3) states that
specifying a value disables automatic selection. It does not on this firmware:
`RESistance:NULL:VALue:AUTO?` still answers `1` afterwards.

The consequence is a silently wrong measurement rather than an error. Because
`NULL:VALue:AUTO` remains on, the instrument replaces the offset the
controller just wrote with its own next reading. The null therefore appears to
be applied — `NULL:STATe?` answers `1` and readings are visibly offset — but
the number being subtracted is not the one the controller specified.

This is difficult to detect from a single instrument, since a nulled meter is
self-consistent either way. We found it only by cross-checking against a
separate programmable resistance standard.

## Steps to reproduce

```
*RST
CONFigure:RESistance 200
RESistance:NULL:VALue:AUTO ON
RESistance:NULL:VALue:AUTO?      -> 1        (as expected)
RESistance:NULL:VALue 0.5
RESistance:NULL:VALue:AUTO?      -> 1        (expected 0 per §7.4.3)
```

The same result occurs via the sequence a controller would naturally use, where
`NULL:STATe ON` arms AUTO as documented in §7.4.2:

```
*RST
CONFigure:RESistance 200
RESistance:NPLC 100
RESistance:NULL:STATe ON
RESistance:NULL:VALue:AUTO?      -> 1        (documented side effect, §7.4.2)
RESistance:NULL:VALue 12.129
RESistance:NULL:VALue:AUTO?      -> 1        (expected 0 per §7.4.3)
```

`SYSTem:ERRor?` returns `0,"No error"` throughout — the value write is
accepted, and `RESistance:NULL:VALue?` reads back the value that was written.
Only the AUTO flag is wrong.

## Expected vs actual

| Step | Expected (§7.4.3) | Actual (fw 0.0.0.20) |
|---|---|---|
| `NULL:VALue <v>` while AUTO is on | AUTO becomes `0` | AUTO stays `1` |
| Reading after that write | offset `<v>` is subtracted | offset is replaced by the next reading |

## Workaround

Send `RESistance:NULL:VALue:AUTO OFF` explicitly after writing the value:

```
RESistance:NULL:STATe ON
RESistance:NULL:VALue 12.129
RESistance:NULL:VALue:AUTO OFF     <- required, not documented as required
RESistance:NULL:VALue:AUTO?      -> 0
```

With this added, the stored offset is stable across subsequent `READ?` calls
and the nulled measurement agrees with our reference standard.

## Why this matters

§7.4.3 is what a controller author reads to decide whether an explicit
`AUTO OFF` is needed. Following the manual produces code that looks correct,
passes its own readback checks (`NULL:VALue?` returns the right number), and
still measures against the wrong zero. For 2-wire low-resistance work, where
the datasheet's accuracy specification assumes a null has been taken, this
directly undermines the specified accuracy.

## Requests

1. Confirm whether the intended behaviour is the manual's (`NULL:VALue`
   clears AUTO) or the firmware's (it does not).
2. If the manual is correct, please fix the firmware.
3. If the firmware is correct, please correct §7.4.3, and state that an
   explicit `NULL:VALue:AUTO OFF` is required after writing a null value.
4. Please confirm whether the same applies to the other functions that expose
   a null (`VOLTage`, `CURRent`, `FRESistance`), and whether other SDM4000A
   models and firmware revisions are affected. We tested `RESistance` on
   0.0.0.20 only.

## Secondary observation (not a defect — a documentation suggestion)

The interaction in §7.4.2, where enabling `NULL:STATe` also arms
`NULL:VALue:AUTO`, means the natural command order (value first, then state)
silently nulls by a different number than requested. It is documented, but the
ordering requirement it imposes on a controller is not stated explicitly.
A note in §7.4.2 to the effect of "set `NULL:STATe` before `NULL:VALue`, since
enabling the state arms automatic selection" would save implementers the
discovery.

## Measurement context

For completeness, timings measured on this unit at `CONFigure:RESistance 200`,
which may be useful in reproducing:

| `RESistance:NPLC` | `READ?` wall time |
|---|---|
| 0.001 – 1 | 0.09 – 0.11 s |
| 10 | 0.41 s |
| 100 | 2.09 s |
| 100, `SAMPle:COUNt 5` | 10.14 s (2.03 s per reading) |

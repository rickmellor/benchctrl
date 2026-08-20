# SDM4065A firmware and documentation reports — index

Four independent reports against one unit, written to be sent to Siglent
support either individually or as a set.

**Instrument:** Siglent SDM4065A 6½-digit bench DMM
**Serial:** SDM46A0CA00021
**Firmware:** 0.0.0.20 (`*IDN?` →
`Siglent Technologies,SDM4065A,SDM46A0CA00021,0.0.0.20`)
**Interface:** USB-TMC only (no LAN connected), Linux SBC host using
pyvisa-py 1.16.2 over libusb, no kernel `usbtmc` module bound

All findings are bench-measured on this unit, not inferred from the
documentation. Where a report says "measured", the transcript came from a
script driving the instrument directly.

| # | Report | Type | Severity |
|---|---|---|---|
| 1 | [`RESistance:NULL:VALue` does not disable `NULL:VALue:AUTO`](SDM4065A-firmware-bug-report-1-null-value-auto.md) | firmware, contradicts §7.4.3 | high — nulling silently becomes a no-op |
| 2 | [§7.4.7 documents an autozero mnemonic (`AZ`) that does not exist; querying it wedges USB-TMC](SDM4065A-firmware-bug-report-2-autozero-query.md) | documentation + firmware | high — requires a front-panel power cycle |
| 3 | [`*CLS` does not clear the error queue; the queue can latch into "No Error"](SDM4065A-firmware-bug-report-3-error-queue.md) | firmware, contradicts IEEE 488.2 §10.3 | high — error detection stops working |
| 4 | [§7.4.5's "default 2 kΩ" is not the reset state; `CONFigure:<fn> DEF` leaves autoranging on](SDM4065A-firmware-bug-report-4-range-defaults.md) | documentation + firmware | low — silent 10x on the range accuracy term |

## Suggested priority

If only one thing gets fixed, it should be the **USB-TMC `INITIATE_CLEAR`**
defect described in report 2. It is the only finding here that makes the
instrument unusable without physical access, and it is not specific to the
command that led us to it: `INITIATE_CLEAR` reports `STATUS_SUCCESS` while
leaving the bulk endpoints dead, so *any* aborted read — including a legitimate
slow measurement that outruns a controller timeout — is unrecoverable. Fixing it
turns every future interruption from fatal into routine.

After that, reports 1 and 3 in either order: both cause a controller to produce
confidently wrong results rather than to fail visibly.

## What the four have in common

Three of the four share a failure mode: **the instrument keeps returning
plausible numbers while a setting is not what the controller believes it is.**
Nulling that silently does nothing (1), an error check that silently stops
detecting errors (3), and a range readback that cannot distinguish pinned from
autoranging (4) all present as good data. That is why they are worth fixing
even though none of them produces a visible fault.

## Reproduction

Each report contains a self-contained SCPI transcript. Note two hazards if
reproducing them on the same unit:

- **Do not query an undefined header** (e.g. `RESistance:AZ?`). It wedges the
  USB-TMC interface and needs a front-panel power cycle — see report 2.
- **Drain the error queue between checks.** Because `*CLS` does not empty it
  (report 3), a stale entry will be attributed to whatever command you send
  next. Read `SYSTem:ERRor?` until it returns `0,"No error"`, or use `*ESR?`
  bit 5 instead, which stays correct.

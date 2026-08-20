# SDM4065A: §7.4.7 documents an autozero mnemonic (`AZ`) the instrument does not implement, and querying it wedges the USB-TMC interface until power-cycled

**Instrument:** Siglent SDM4065A 6½-digit bench DMM
**Serial:** SDM46A0CA00021
**Firmware:** 0.0.0.20
**Interface:** USB-TMC (host is a Linux SBC using pyvisa-py/libusb; no
kernel `usbtmc` module involved)
**Manual:** SDM4000A series Remote Control Manual, §7.4.7
**Severity:** the documentation error is minor on its own, but following it
makes the instrument stop responding to all SCPI traffic until a front-panel
power cycle. Reproduced twice.

This is the second of four independent reports from the same unit; see
`SDM4065A-firmware-bug-reports-README.md` for the set. The others concern
`RESistance:NULL:VALue` not clearing `NULL:VALue:AUTO` (1), `*CLS` not clearing
the error queue and the queue latching into reporting "No Error" (3), and the
documented default resistance range not matching the reset state (4).

There are two findings here, one in the manual and one in the firmware. They
are reported together because the first is what leads a controller into the
second.

## Finding 1 — the documented mnemonic does not exist

§7.4.7 gives the autozero syntax as:

```
[SENSe:]{RESistance|FRESistance}:AZ[:STATe]
[SENSe:]{RESistance|FRESistance}:AZ[:STATe]?
```

No spelling of `AZ` is accepted on this firmware. Every one below is rejected
with `-113,"Undefined header"` — **for writes as well as queries**, and on all
four affected functions (`RESistance`, `FRESistance`, `VOLTage:DC`,
`CURRent:DC`):

```
RESistance:AZ ON
RESistance:AZ:STATe ON
SENSe:RESistance:AZ ON
SENSe:RESistance:AZ:STATe ON
RESistance:AZERo ON
RESistance:AZERo:STATe ON
```

The node that *does* work is `ZERO:AUTO` — the Keysight-style spelling:

```
RESistance:ZERO:AUTO ON
RESistance:ZERO:AUTO?      -> 1
```

This round-trips correctly (write, then query returns what was written) on
`RESistance`, `FRESistance`, `VOLTage:DC` and `CURRent:DC`. So autozero is
fully functional and remotely controllable; only the manual is wrong.

We confirmed the rejection through `*ESR?` bit 5 (Command Error) as well as
through the error queue, because the queue is unreliable on this unit — see
report 3. `*ESR?` returns `32` after any `AZ` form and `0` after the
corresponding `ZERO:AUTO` form.

## Finding 2 — a query of an undefined header wedges the bulk endpoints

Because §7.4.7 is wrong, a controller written from the manual will send
`RESistance:AZ?`. Being an undefined header, this produces **no reply** — the
error is queued, but nothing is sent back, so the controller's read times out.
That timeout is the serious part: aborting the unanswered USB-TMC transfer
leaves the instrument's bulk endpoints in a state it does not recover from.

### Steps to reproduce

```
*RST
CONFigure:RESistance 200
RESistance:AZ?                   -> (no response; controller times out)
<any further command>            -> fails at the USB layer
```

On our host every subsequent transfer returns `Errno 110` (connection timed
out) at the libusb layer.

### The wedge, in detail

What we observed after the timeout, which may help localise the fault:

- **Endpoint 0 still works.** USB control transfers succeed. The device is
  still enumerated, still answers descriptor requests, and its USB-TMC
  class requests are serviced.
- **Both bulk endpoints are dead.** No write to bulk-OUT is drained and no
  read from bulk-IN completes. This is consistent with the instrument
  declining to accept further bulk-OUT traffic until an outstanding bulk-IN
  transaction is resolved.
- **USB-TMC `INITIATE_CLEAR` does not recover it.** We sent
  `INITIATE_CLEAR` (class request 5) and then polled `CHECK_CLEAR_STATUS`
  (class request 6). The instrument reported `STATUS_SUCCESS` (`0x01`) — i.e.
  it claimed the clear had completed — but the bulk endpoints remained dead
  immediately afterwards. This is the specified recovery path for exactly this
  situation, and it reports success while not working.
- **A USB port reset does not recover it.** A libusb `reset()` on the device
  (USB port reset, re-enumeration) also left the endpoints dead.
- **Only a front-panel power cycle recovers it.** After power-off/power-on the
  instrument behaves normally again.

We reproduced the whole sequence twice, on separate days, with a power cycle in
between.

**This is not specific to `AZ?`.** We can trigger the same wedge by aborting a
slow legitimate read — e.g. a 100 NPLC `READ?` with `SAMPle:COUNt 5`, which
takes 10.1 s, under a 10 s controller timeout. So the wedge is a general
consequence of any aborted read; `AZ?` is merely a documented command that
guarantees one on the first attempt.

## Expected vs actual

| Step | Expected (§7.4.7) | Actual (fw 0.0.0.20) |
|---|---|---|
| `RESistance:AZ ON` | accepted | `-113,"Undefined header"` |
| `RESistance:AZ?` | returns `0` or `1` | no reply, ever |
| `RESistance:ZERO:AUTO ON` / `?` | (undocumented) | works, round-trips |
| USB-TMC `INITIATE_CLEAR` after the timeout | endpoints usable again | reports `STATUS_SUCCESS`, endpoints still dead |
| USB port reset | interface recovered | still dead |
| Power cycle | — | recovers |

## Workaround

For finding 1: use `ZERO:AUTO` instead of `AZ`. Our driver now does, and reads
the state back successfully on all four functions.

For finding 2: never query a header whose existence is unverified, and set
controller timeouts from measured worst-case integration time rather than a
fixed value. Neither is a real fix — a controller cannot know in advance which
headers exist, and any unforeseen read abort is still unrecoverable without an
operator at the front panel.

## Why this matters

1. **The manual leads controller authors straight into the wedge.** Anyone
   implementing autozero from §7.4.7 will send `RESistance:AZ?`, and the first
   time they do the instrument becomes unusable until somebody physically
   power-cycles it. On an unattended or remote bench that ends the run. It also
   presents as an intermittent, unexplained instrument fault rather than as a
   firmware bug, because the USB device stays enumerated and control transfers
   keep working. It cost us two power cycles and a day to identify.

2. **`INITIATE_CLEAR` reporting success while not clearing** is a defect in its
   own right, independent of the documentation error. It defeats the standard
   USB-TMC recovery mechanism for *any* aborted transfer, so a controller
   cannot recover from a timeout of any cause — it has no reliable way to
   resynchronise short of asking an operator to power-cycle the meter. This is
   the more valuable of the two to fix, since it would turn every future
   interruption from unrecoverable into recoverable.

## Requests

1. Please correct §7.4.7 to document `[SENSe:]{RESistance|FRESistance|
   VOLTage[:DC]|CURRent[:DC]}:ZERO:AUTO[?]`, or add `AZ` as an accepted alias
   if that was the intent. Either resolves finding 1; the alias would also keep
   existing controllers written from the current manual working.
2. Please investigate why USB-TMC `INITIATE_CLEAR` reports `STATUS_SUCCESS`
   without actually clearing the bulk endpoints, and why a USB port reset does
   not clear them either. We would consider this the primary fix.
3. Please confirm whether other SDM4000A models and firmware revisions are
   affected by either finding.

## Host details

For reproduction, in case the host stack is relevant:

- Linux SBC (aarch64), Python `pyvisa` 1.16.2 with the pure-Python `pyvisa-py`
  backend over `libusb`. No kernel `usbtmc` driver bound.
- VISA resource string as enumerated:
  `USB0::62700::4640::SDM46A0CA00021::0::INSTR` (pyvisa-py renders the USB
  VID/PID in decimal; `62700` = `0xF4EC`, `4640` = `0x1220`).
- The instrument is on USB only — no LAN connection — so we could not check
  whether the same behaviour occurs over the socket/VXI-11 interface, or
  whether the LAN interface would have provided an independent recovery path.

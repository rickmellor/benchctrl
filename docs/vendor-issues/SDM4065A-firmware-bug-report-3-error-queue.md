# SDM4065A: `*CLS` does not clear the error queue, and the queue can latch into reporting "No Error" permanently

**Instrument:** Siglent SDM4065A 6½-digit bench DMM
**Serial:** SDM46A0CA00021
**Firmware:** 0.0.0.20
**Interface:** USB-TMC (host is a Linux SBC using pyvisa-py/libusb; no
kernel `usbtmc` module involved)
**Standard:** IEEE 488.2 §10.3 (`*CLS`), §11.4.3.4 (error/event queue)
**Severity:** a controller cannot reliably determine whether a command was
accepted. In the latched state, error reporting stops entirely and only a
power cycle restores it.

This is the third of four independent reports from the same unit; see
`SDM4065A-firmware-bug-reports-README.md` for the set. The others concern
`RESistance:NULL:VALue` not clearing `NULL:VALue:AUTO` (1), §7.4.7 documenting
an autozero mnemonic (`AZ`) that does not exist plus the USB-TMC wedge that
follows from querying it (2), and the documented default resistance range not
matching the reset state (4).

## Finding 1 — `*CLS` does not empty the error queue

IEEE 488.2 §10.3 requires `*CLS` to clear the error/event queue. On this
firmware it does not. Measured:

```
RESistance:AZ ON            -> queues -113 (undefined header, see report 2)
RESistance:AZ:STATe ON      -> queues -113
*CLS
SYSTem:ERRor?               -> -113,"Undefined header"     <-- still there
SYSTem:ERRor?               -> -113,"Undefined header"     <-- and the second
SYSTem:ERRor?               -> 0,"No error"
```

The entries also survive:

- **`*RST`** — both still present afterwards.
- **closing and reopening the VISA session** — at least one entry survived a
  full session teardown and reconnect.

The only operation that removes an entry is reading it with `SYSTem:ERRor?`.

`*CLS` does correctly clear the *status registers*: `*ESR?` reads `0` after a
`*CLS` that left two errors queued. So the command is implemented, but its
queue-clearing side effect is missing.

### Consequence

A controller that follows the standard — `*CLS` to establish a known-clean
state, send a command, then `SYSTem:ERRor?` to check it — reads an error left
by some *earlier* command and attributes it to the command it just sent. Worse,
once the queue reaches its depth it answers `-350,"Queue overflow"` to
everything, so every subsequent check reports a failure that has nothing to do
with the command under test. We saw exactly this: a test asserting that a valid
NPLC value was accepted failed with `-350`, then with a stale `-113`, before we
found the cause.

The workaround is to read the queue until it returns `0,"No error"` after every
operation that might have queued something. That is what our driver now does.

## Finding 2 — the queue can latch into reporting "No Error" permanently

More serious. After the queue had filled and returned `-350,"Queue overflow"`,
it entered a state where `SYSTem:ERRor?` answers `0,"No Error"` **to
everything** — including immediately after a deliberately undefined header.
Measured in that state:

```
*CLS
BOGUS:HEADER ON
SYSTem:ERRor?               -> 0,"No Error"      <-- wrong
*ESR?                       -> 32                <-- bit 5, Command Error: correct
```

We polled `SYSTem:ERRor?` repeatedly with delays up to 1.5 s in case the entry
was merely slow to appear; it never did. Three consecutive reads all returned
`0,"No Error"`.

Two details that may help localise it:

- **`*ESR?` remains completely correct throughout.** Bit 5 (Command Error) sets
  on every rejected header, clears on read, and clears on `*CLS`. So the
  instrument *is* detecting and classifying the error — it is only failing to
  place it in the queue.
- **The reply text changes case.** Before the latch, the clean-queue reply is
  `0,"No error"` (lowercase e). In the latched state it is `0,"No Error"`
  (capital E). That suggests a different code path is answering, which may
  point directly at the fault.

Measurement is entirely unaffected in this state — the instrument continued to
read 100.153744 Ω on a 100 Ω DUT — so nothing about the failure is visible
except through error checking.

`*CLS` and `*RST` do not restore the queue. We have not yet confirmed whether
power-cycling restores it, but a power cycle restored normal queue behaviour
after the earlier session in which we first saw stale entries.

## Expected vs actual

| Step | Expected (IEEE 488.2) | Actual (fw 0.0.0.20) |
|---|---|---|
| `*CLS` with errors queued | queue empty | entries still queued |
| `*RST` with errors queued | queue empty | entries still queued |
| session close/reopen | queue empty | at least one entry survives |
| `*CLS`, bad header, `SYSTem:ERRor?` | reports the error | `0,"No Error"` once latched |
| `*CLS`, bad header, `*ESR?` | bit 5 set | bit 5 set — correct |
| `*ESR?` read twice | second read `0` | second read `0` — correct |

## Workaround

Our driver now:

1. reads the error queue empty after `*CLS`, rather than trusting `*CLS`; and
2. uses `*ESR?` bit 5 — not `SYSTem:ERRor?` — as the authoritative answer to
   "was that command rejected?", since `*ESR?` is a single read-clear register
   that cannot accumulate stale state and stays correct in the latched state.

This works, but it costs the error *code*: `*ESR?` says a command error
occurred, not which one. Diagnostics that need the code (`-113` vs `-224` vs
`-222`) still depend on the queue.

## Requests

1. Please make `*CLS` clear the error queue, per IEEE 488.2 §10.3.
2. Please investigate the latched state in finding 2, in which
   `SYSTem:ERRor?` reports `0,"No Error"` while `*ESR?` correctly reports a
   command error. The change in reply capitalisation (`No error` →
   `No Error`) may identify the responsible code path. We believe an error
   queue overflow precedes it, but have not isolated the trigger.
3. Please confirm whether the queue depth is documented anywhere, and what the
   intended overflow behaviour is — in particular whether `-350` is meant to
   replace the last entry or be appended.
4. Please confirm whether other SDM4000A models and firmware revisions are
   affected.

## Host details

- Linux SBC (aarch64), Python `pyvisa` 1.16.2 with the pure-Python `pyvisa-py`
  backend over `libusb`. No kernel `usbtmc` driver bound.
- VISA resource string as enumerated:
  `USB0::62700::4640::SDM46A0CA00021::0::INSTR` (pyvisa-py renders the USB
  VID/PID in decimal; `62700` = `0xF4EC`, `4640` = `0x1220`).
- The instrument is on USB only — no LAN connection — so we could not check
  whether the same behaviour occurs over the socket/VXI-11 interface.

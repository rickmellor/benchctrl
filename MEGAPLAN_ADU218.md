# benchctrl game plan — Ontrak ADU218 relay I/O driver

**Branch:** `feat/ontrak-adu218` (off `master` @ `2eb0003`)
**State as of:** 2026-08-25
**Stage 0 status:** **complete and committed** (`1044910`). Relay control is
confirmed on hardware with an independent DMM witness. No production code yet.

**Resume by reading:** this file, then `tests/fixtures/adu218/README.md` (the
measured device behaviour — believe it over the PDF), then `AGENTS.md`.

---

## 1. The headline: the user's premise was wrong, and the answer is better

The task was framed as *"I'm assuming we'll need to reverse it and do pyserial
directly, but I don't know if it comes up as a serial device."*

It does not come up as a serial device, and pyserial cannot reach it. The
ADU218 is a USB **HID-class** device (`bInterfaceClass 03`) with two 8-byte
interrupt endpoints. There is no tty and no `/dev/hidraw*` node for it either.

The route that works needs **zero dependencies** — not even the `pyusb`/libusb
that `transports/ch341.py` requires. Raw `USBDEVFS` ioctls through stdlib
`fcntl` + `ctypes` reach the endpoints directly:

```python
USBDEVFS_CLAIMINTERFACE   = 0x8004550F
USBDEVFS_RELEASEINTERFACE = 0x80045510
USBDEVFS_BULK             = 0xC0185502   # interrupt EPs: supported, not tolerated
```

This fully satisfies *"If possible I'd like this to work with no
dependencies"* — it is a better answer than either option the user offered.

### `USBDEVFS_BULK` on interrupt endpoints is contractual, not emergent

Worth settling before writing code, because if this were an implementation
accident a kernel update would silently break the only control path to a relay
board. It is not an accident: **the kernel detects the endpoint type and builds a
real interrupt URB.** `do_proc_bulk()` in `drivers/usb/core/devio.c` validates
only that the endpoint exists, sits in a claimed interface, and has nonzero
`wMaxPacketSize` — there is no comparison against `USB_ENDPOINT_XFER_BULK` — and
then branches on `USB_ENDPOINT_XFER_INT` to rewrite the pipe to `PIPE_INTERRUPT`
and call `usb_fill_int_urb()` with the descriptor's own `bInterval` (10 here). So
writes to `0x01` and reads from `0x81` are genuine interrupt transfers, correctly
paced. The ioctl name is a misnomer, nothing more.

The citable commitment is `usb_bulk_msg()`'s kerneldoc in
`drivers/usb/core/message.c`: *"We will take the liberty of creating an interrupt
URB (with the default interval) if the target is an interrupt endpoint."* Quote
only that sentence — the same comment opens by claiming `usb_interrupt_msg()`
does not exist, which is stale (it exists and tail-calls `usb_bulk_msg`, so the
two are literally the same function). The clause we depend on — that there is no
`USBDEVFS_INTERRUPT` ioctl — is still true.

Continuously present since **v2.6.15 (2006)**: twenty years, one relocation
(`ae8709b296d8` moved it from `message.c` into `devio.c` for v5.16, for an
unrelated syzbot `hung_task` fix, **carrying the branch across verbatim**), no
removal and no deprecation. Low-speed devices have no bulk endpoints at all, so
for this whole device class it is the only synchronous path that can ever work.

**Residual risk, stated precisely: the guarantee lives in kerneldoc and in code,
not in the uapi header or the user-facing ioctl reference.**
`Documentation/driver-api/usb/usb.rst`'s `USBDEVFS_BULK` entry says "a bulk
endpoint number" and does not mention interrupt endpoints (that same paragraph
carries a literal `FIXME` about return semantics), and
`include/uapi/linux/usbdevice_fs.h` says nothing on transfer type — verified
locally. The narrative section is supportive though: *"interrupt transfers can
also be used in a synchronous 'one shot' style"* (`usb.rst:349`), and
`USBDEVFS_BULK` is the only synchronous non-control transfer ioctl usbfs has.

**`SUBMITURB`/`REAPURB` was considered and rejected.** It is not
better-specified — the async path carries the *same* accommodation, with its own
`/* allow single-shot interrupt transfers */` comment, permitting interrupt while
rejecting control and isoc. Declaring `USBDEVFS_URB_TYPE_INTERRUPT` would express
intent more cleanly, but: **`struct usbdevfs_urb` has no timeout field** and
`reap_as()` has no timeout, so the bounded `recv(timeout=…)` that makes the
invalid-command and watchdog-trip captures work would have to be rebuilt from
`REAPURBNDELAY` in a hand-rolled poll loop, plus `DISCARDURB` cancellation, plus
completion-matching by userurb pointer — more state to get wrong in exactly the
queued-response failure mode already documented in finding 5.

**Architecture caveat for the constant:** `0xC0185502` embeds
`sizeof(struct usbdevfs_bulktransfer) = 24`, correct for 64-bit userspace
(aarch64 on the board). 32-bit would be `0xC0105502` — and the header does define
a separate `USBDEVFS_BULK32` for exactly that reason, so this is a real
distinction, not a theoretical one. Verified locally: `ctypes.sizeof()` gives 24
and the ioctl encodes 24. The driver should derive it rather than hardcode, or at
minimum comment why the literal is safe here.

### Why the interface is unclaimed, and why that is not luck

The tempting reading — "another missing kernel module, like `ch341`" — is
wrong, and getting it wrong would make the whole approach look fragile.
`usbhid` **is** built into this kernel and it bound the USB keyboard on the same
bus. The ADU218's interface is unclaimed because upstream Linux **deliberately
ignores it**: `drivers/hid/hid-quirks.c` lists this vendor in
`hid_ignore_list`, and `hid-ids.h` defines `USB_VENDOR_ID_ONTRAK 0x0a07` /
`USB_DEVICE_ID_ONTRAK_ADU100 0x0064`. Our PID `0x00da` is `0x0064 + 118`, the
ADU218 entry. HID core is told to keep away because these devices are not
really HID — the report descriptor wraps a private ASCII protocol.

Two consequences worth the space:

1. **`CLAIMINTERFACE` will always succeed** on any mainline kernel, with no
   driver to detach. The zero-dependency path is a property of upstream Linux,
   not a quirk of this board, and a kernel update will not take it away.
2. **A kernel driver for this device does exist**: `drivers/usb/misc/adutux.c`
   (`CONFIG_USB_ADUTUX`) binds the same six PIDs including `0x0064+118`
   `/* ADU218 */`, exposing `/dev/usb/adutuxN`. It is **not** enabled on the
   Uno Q, nor on the WSL host (`# CONFIG_USB_ADUTUX is not set`), so we do not
   use it. But it is why the transport seam matters: its `write()` copies the
   buffer to the endpoint verbatim, so **the framing is identical on both
   routes**, and it enforces exclusive open (`-EBUSY`) — a stronger writer
   guarantee than usbfs gives us. If we ever meet a host that has it enabled,
   one command encoder serves both paths behind a second link class.

## 2. What the hardware said that the manual did not

Full detail in `tests/fixtures/adu218/README.md`. Five findings, each of which
would have been a driver bug:

| # | Finding | Consequence for the driver |
|---|---|---|
| 1 | **`RI` does not exist.** §5 lists it; §6b calls the same function `PI` in four places. The device answers `PI` and times out on `RI`. Both spellings really are in the PDF, so this is a genuine internal contradiction the hardware settles — cite §6b, never §5 | A driver written from the summary table hangs on *every* input read |
| 2 | **PORT A and PORT B are 4 bits each**, not 8. Eight inputs across two isolated ports | Why `PK` returns 3 digits and `PA` returns 2. Indexing inputs 0–7 against one port is wrong |
| 3 | **Silence is the only error signal.** An unknown command returns nothing — no string, no sentinel | The driver needs a per-command *"expects a response"* table; it cannot discover this at runtime, because correct write-only silence is byte-identical to an error |
| 4 | **The `0x01` prefix is mandatory and is specifically `0x01`** — bare ASCII, `0x00` and `0x02` are all ignored | A test asserting "byte 0 is non-printable" would pass for two encodings the device rejects |
| 5 | **A queued response outlives its command.** Interrupt-IN replies persist until read | A skipped or failed read makes the *next* query return the *previous* value — a silent wrong answer. Drain on open; pair reads and writes strictly |

Finding 5 is not hypothetical: it invalidated the first framing measurement,
which credited a bare-ASCII write with the previous prefixed command's answer.
That is exactly the "sim agrees with the driver's misreading" failure
`AGENTS.md` warns about, caught before any code existed.

Also measured, and **still unexplained — but no longer an invalid comparison**:
closed-contact resistance is **6.14 Ω** against Ontrak's quoted 700 mΩ typ /
1.1 Ω max. Eight reads spread 0.98 mΩ, so it is systematic; § H-5 bounds this
bench's lead error at ~79 mΩ, two orders too small.

The Panasonic `ASCTB467E` family catalogue (AQZ207 has no standalone datasheet)
supplies the test conditions Ontrak's manual strips: on-resistance is specified
at **`IF = 10 mA`, `IL = Max.`, `Within 1 s`**, and `IL = Max.` is **1.0 A** for
this part. Ontrak copied the numbers from the correct column of the correct part
— and dropped the conditions. This bench measured at ~1 mA, three orders of
magnitude below the specified envelope, so **6.14 Ω cannot violate the 1.1 Ω
maximum and cannot satisfy it either.** This unit is not out of spec.

Nor is it explained. The tempting story — R_on climbing as load current falls —
is absent from the datasheet: there is no R_on-vs-current curve at all (R_on is
plotted only against temperature), and its one lower-current point (0.4 A,
≈0.65 Ω at 25 °C) sits slightly *below* the 1.0 A typical, trending the wrong
way. The AC/DC two-MOSFET anti-series topology is real but already inside the
published 0.7 Ω (~2x against each DC-only sibling), so it adds no factor. 5.44 Ω
remains unaccounted for. The discriminating experiment is a reading at the
datasheet's own condition (1.0 A, or 0.4 A for comparison with graph 3-4) — that
energises a 120 V relay into a real load, so it is an operator decision.

**Driver consequence, unchanged and now better justified: never treat
on-resistance as a validation threshold, and do not report a contact-resistance
figure at all** — no in-spec figure exists at any current the driver could know
about. Key open/closed on the DMM's overload sentinel, where the margin is
effectively infinite.

## 3. Relay control is confirmed (the stated goal)

Two full cycles, each cross-checked three ways, plus two more via `MKddd`:

| Command | `RPK0` | `PK` | DMM (independent witness) |
|---|---|---|---|
| `SK0` | 1 | 001 | **6.1398 Ω** |
| `RK0` | 0 | 000 | overload sentinel (open) |
| `SK0` | 1 | 001 | **6.1395 Ω** |
| `RK0` | 0 | 000 | overload sentinel (open) |
| `MK001` | 1 | 001 | **6.1391 Ω** |
| `MK000` | 0 | 000 | overload sentinel (open) |

`RPK0` and `PK` are the device reporting on itself; a firmware that lied would
lie consistently. The DMM is on a different bus, so it is the only independent
evidence the contact physically moved. The bench was left with K0 open, `WD 0`,
`DB 1`.

**Safety basis for switching at all:** K0's load side has only DMM leads, so
there is no load current and the manual's *"1 CPS at full load"* PhotoMOS limit
(with its 20%-of-rated-current escape clause) does not bind. Switching was
still kept slow (0.5 s settle) and the cycle count small.

**Ratings, for sizing the driver's posture:** Panasonic AQZ207 PhotoMOS, form A
N.O., **1 A @ 120 VAC / 120 VDC**, and — worth stating because an operator would
not infer it — **primary insulation only** (the sibling ADU208 is
double-insulated; this model is not). Confirmed against the part datasheet: the
AQZ207 is rated 1.0 A continuous at 200 V peak AC/DC, 2500 Vrms isolation, 1
Form A — every figure Ontrak quotes traces to the right column, so there is no
variant confusion.

The switching-rate ceiling has a **conflict worth honouring conservatively**:
Ontrak says 1 CPS at full load, Panasonic says **0.5 cps** for the part
(`IF = 10 mA, duty = 50 %, IL = Max., VL = Max.`). Both claim "full load"; the
gap is unresolved and may be Ontrak derating at 120 V against a 200 V part. Use
**0.5 CPS** anywhere the docs state a limit. Either way it is load-dependent, so
it stays a `KNOWN_LIMITATIONS.md` entry rather than a code check — the driver
cannot know what is attached.

## 4. The find that changes more than this driver: the watchdog works

`ROADMAP.md`'s *"Hardware interlock for unattended runs"* is open work waiting
on a GPIO e-stop button ordered 2026-08-19, and `KNOWN_LIMITATIONS.md § N-1`
states the honest position that **no software deadman can guarantee an output
goes off**. The ADU218 answers that in hardware:

- `WD1` armed, then host silence → K0 **opened**, witnessed by the DMM, and `WD`
  self-cleared to `0`. Trip measured by bisection at **(0.90, 1.10] s**, i.e.
  the documented 1 s (`watchdog_trip.txt`).
- **Control case:** fed with `PK` every 0.3 s → K0 stayed **closed** for 3.1 s
  with `WD` still `1`.

**A correction to my own earlier claim.** The first capture reported "opened
after 3.7 s" and I reported that as a trip time. It was not: it is `sleep(3.0)`
plus the latency of the DMM read that followed, so it timed my *observation*,
not the timeout. Read as a trip time it implied WD1 fires at ~3.7x its label and
therefore that no WD interval could be trusted without characterisation — a
reviewer drew exactly that conclusion from it. Re-measured properly by bisecting
the silence window (stay quiet for exactly T, then one `RPK0` read that
terminates the window, since polling would refeed the timer): closed at 0.90 s,
open by 1.10 s. **There is no timing anomaly.** None of the four design
consequences below depended on the bad number — they follow from arming coupling
relay state to call frequency.

The ladder is bounded and documented: `WD0` off, `WD1` 1 s, `WD2` 10 s, `WD3`
1 min, `n ∈ 0..3`. No `WD4`, no custom interval, and `WDn` sets the interval
*and* arms in one command — there is no separate arm step to hold.

The control is what makes this safe to rely on: the relay drops because the
host went *quiet*, not because arming `WD1` opens relays. A wedged agent, a
killed process, an unplugged cable and a panicking kernel all produce the same
silence, so all de-energise the load — with no benchctrl process, no GPIO and
no kernel driver in the decision path.

**But it is a loaded gun, and the danger is the shape memory already records as
the "inert governor" trap.** Four consequences, all in `watchdog.txt`:

1. Arming it makes **every relay's state depend on call frequency**. One
   blocking pyvisa call to another instrument could exceed the interval and
   silently open a load the driver was told to hold closed. So `WD` must
   **never** be armed implicitly, and the driver must **not** offer a
   keep-alive thread that hides the coupling.
2. `WD1` (1 s, confirmed) is unusable for a general bench; `WD3` (1 min) is the
   longest available and the only setting a run loop can plausibly meet.
3. `WD` self-clearing to `0` is the **only** distinguishable trace that a
   timeout fired. Polling it is how a host learns. That read belongs in the run
   event stream — a fired interlock absent from the artifact bundle is a gap in
   the audit trail. **But the trace is weaker than it looks:** `WD`=0 means both
   "timed out" *and* "never enabled", so it is only interpretable against a
   driver-held expected value, and a driver restart loses that — the first `WD`
   read after a restart is ambiguous by construction. So the driver holds the
   armed state itself, and writes `WD0` at `open()` unless it is using the
   watchdog, to replace an inherited state with a known one.
4. A test must assert the **ladder** (fed stays closed, unfed opens) against a
   synthetic clock — not merely that `WD1` is accepted. A watchdog nobody feeds
   is indistinguishable from one that works until the day it should have fired.
5. **A status poller silently neuters it.** Any command refeeds the timer —
   invalid ones included, per §6d — so a health-check loop reading `PK` keeps the
   deadman fed however wedged the control path is. The control case above did
   this deliberately, with nothing but `PK`. Two consequences: the feed must live
   on the **control** path only, and a benchctrl dashboard polling device state
   would be enough to disable the interlock. This is the inert-governor shape
   again, and it is the strongest argument against any background feeder.

## 5. Method surface — names are a safety decision

`agent/dispatch.py:86-107` derives `DeviceSurface.mutators` **purely from name
prefixes**, with no driver-declared override, walking the class not the
instance. The full list is `set_ enable_ disable_ write_ start_ stop_ reset
clear_ program_ trigger apply commit abort incr decr take_`.

So a method named `close_relay()` would be **remotely callable with no writer
claim**. Every mutator must therefore take an existing prefix. Proposed:

```python
# reads (deliberately no mutator prefix)
is_open -> bool;  relay_count -> int (8);  input_count -> int (8)
allowed_relays -> frozenset[int]
read_identity() -> ADU218Info          # from the USB descriptor, not a command
relay_state(index) -> bool             # RPKn
relay_states() -> dict[int, bool]      # PK, one round trip
input_state(port, index) -> bool       # RPyn
input_states() -> dict[str, tuple]     # RPA + RPB
read_counter(index) -> int             # REn
read_debounce() -> int                 # DB
read_watchdog() -> int                 # WD  <- also how a fired timeout is seen

# writes (all prefix-matched)
set_relay_state(index, on, *, verify=True) -> bool
set_relay_port(mask, *, verify=True) -> int          # MKddd
set_debounce(setting) -> None                        # DBn
set_watchdog(setting) -> None                        # WDn
clear_counter(index) -> int                          # RCn — reads AND clears
reset_relays() -> None                               # MK000, the safe state
close(); __enter__; __exit__
```

Constraints on the implementation behind that surface, each traceable to a
measured or documented fact rather than taste:

- **An explicit per-command `responsive: bool`,** never inferred from the
  mnemonic. The naming pattern nearly holds — responsive commands start with `R`
  or `P`, bare `DB`/`WD` answer while their `n`-suffixed setters do not — but
  **`RKn` starts with `R` and is write-only**, and it is the most-called command
  on the device. Write-only: `SKn`, `RKn`, `MKddd`, `DBn`, `WDn`. Responsive:
  `RPKn`, `PK`, `RPyn`, `RPy`, `Py`, `PI`, `REn`, `RCn`, `DB`, `WD`.
- **Drain in `open()`, and expect up to three stale frames.** The USB core holds
  ~3 buffers per device, and a stale reply survives a process restart — so
  session N can read session N−1's answers. Drain-until-empty, not drain-one.
- **Two different index ranges.** Relays are `n ∈ 0..7`, input lines `n ∈ 0..3`
  (4 bits per port). A single shared validator is a live off-by-four bug.
- **Validate host-side and never let the device arbitrate.** Out-of-range
  behaviour is undocumented, and `MK300` exceeds a byte — if it aliases rather
  than being rejected, a whole-port write could close relays nobody asked for.
  Bounds: relays 0..7, inputs 0..3, `MK` 000–255 zero-padded to 3, `DB` 0..2,
  `WD` 0..3, ASCII payload ≤ 7 bytes.
- **`read_counter` is the safe one; `clear_counter` must never be auto-retried.**
  `RCn` is the only responsive command that mutates state, so a lost reply after
  the device cleared loses the count permanently. Prefer `REn` + host-side
  differencing wherever the value matters.
- **Do not assume relays are open at `open()`.** Power-on relay state is
  undocumented, and USB *suspend* explicitly holds outputs in their last state —
  including when the host suspends the device because **no handle is open**. So a
  closed handle can coexist with energised outputs indefinitely. Read `PK` and
  report it; drive `MK000` only when explicitly asked.
- **But suspend cannot happen *under* us mid-session, and the reason is worth
  knowing.** `usbdev_open()` takes a runtime-PM reference
  (`usb_autoresume_device()`, `devio.c:1058`) and holds it until release unless
  userspace issues `USBDEVFS_ALLOW_SUSPEND` — which this driver must never do.
  `Documentation/driver-api/usb/power-management.rst:94` states it directly: *"a
  device isn't considered idle so long as a program keeps its usbfs file open,
  whether or not any I/O is going on."* That final clause is exactly the watchdog
  scenario — **deliberate silence on the wire does not make the device idle to the
  PM core**, so arming `WD` and going quiet cannot trigger a suspend. Secondary
  protection: autosuspend is forbidden by default for all non-hub devices
  (`hub.c` calls `usb_disable_autosuspend()`; `power-management.rst:253-256`).

  Two consequences. First, **the guarantee comes from holding the fd open**, which
  is an argument for the long-lived handle this design already has — a
  close/reopen-per-command driver would give up the reference and fall back to
  depending on a settable default (`power/control`, `power-management.rst:159-175`;
  named for understanding, deliberately not configured). Second,
  **`USBDEVFS_ALLOW_SUSPEND` must never be added as a politeness gesture.** It is
  a one-line change that looks tidy and would let a board holding a relay closed
  suspend under us. Worth a comment in `usbfs.py` saying so, since the next reader
  will not know it was considered.
- **Three de-bounce settings, not four.** The web page lists a fourth (`NONE`)
  but the manual bounds `n` to 0..2 and the captures show 0/1/2. The same
  four-option string appears on the ADU208 and ADU228 pages, so it reads as
  shared boilerplate.
- **200 ms read timeout**, matching all three of Ontrak's examples; with
  `bInterval` 10 at low speed the round-trip floor is ~10-20 ms, so that is ~10x
  margin.

Notes on the non-obvious choices:

- **No `open_relay`/`close_relay`.** Beyond the prefix gate, "open" and "close"
  are ambiguous under a driver whose `open()` means *connect*. One switching
  verb taking a bool means one code path to audit.
- **`clear_counter` returns the value it cleared**, because `RCn` is
  destructive-read: discarding the return would lose data irrecoverably.
- **`set_relay_state` returns the verified read-back**, not `None`. Given
  finding 5 (stale queued responses), a switch this driver cannot confirm is a
  switch it should not claim.
- **`read_watchdog()` is a read**, so an observer without the writer claim can
  see that the interlock fired. That asymmetry is deliberate.

## 6. Architecture and open design decisions

```
src/benchctrl/drivers/ontrak_adu218/
    __init__.py     re-exports; states which Protocol it implements (see below)
    driver.py       OntrakADU218, the command table, dataclasses, exceptions
    usbfs.py        the stdlib USBDEVFS link — write/read/is_open/close
    mcp_tools.py    one tool per SDK method (CONTRIBUTING rule 1)
```

**`usbfs.py` inside the driver package, not `transports/`.** `transports/`
holds things that produce a *serial-shaped* object (`ch341.py` exposes a pty so
pyserial works unmodified; `autoserial.py` picks between ttys). This produces an
8-byte packet channel, which is a different shape, and there is exactly one
consumer. Promote it to `transports/` if a second Ontrak device lands — that is
the same "second instance" rule as Protocols.

Three decisions I recommend but which are worth your look:

1. **No `interfaces.Switch` Protocol yet — recommend deferring.**
   `CONTRIBUTING.md` rule 3 says define one when the *second* concrete instance
   lands, and `interfaces.py`'s own docstring names `Switch` as deferred with
   the PDU41002 already present. So the letter of the rule says now. I'd still
   defer, because **no consumer wants to be polymorphic over both yet** and the
   semantics diverge sharply: the PDU switches 120 V/20 A mains and is
   1-indexed; the ADU218 switches 1 A signal-level SSRs and is 0-indexed. A
   Protocol whose two implementers disagree about index base is a footgun, not
   an abstraction. Revisit when a *third* switch or a real consumer appears.
2. **Not in `registry.SWITCHED_PDU_KEYS`.** That tuple's docstring says it
   marks devices that **switch mains**, and it gates the run engine's outlet
   setpoints. The ADU218 is rated 1 A at 120 VAC and is wired to signal on this
   bench. Adding it would let a run's `outlets` setpoints silently target a
   different physical device class. If run-engine relay control is wanted, it
   should get its own key, not borrow the mains one.
3. **`0a07:00da` *does* go in `discovery.SIGNATURES`** — unlike the PDU, which
   had to use `PROBE_LABELS` because it hid behind a generic FTDI bridge. This
   is a unique vendor/product pair, so identification is exact and needs **no
   probe at all**. That matters given `discover()`'s docstring hardened
   `probe=False` specifically because probe candidates now include a mains
   switch: this device adds a switch to the inventory while adding **zero** new
   probe writes. It needs a new `transport` value (`"usbfs"`) since the
   existing ones are `serial`/`usbtmc`/`visa`, plus a `scan_usbfs()` scanner —
   `scan_serial()` cannot see it for the same structural reason
   `scan_driverless_bridges()` exists.

   **The signature and the scanner are one atomic change, not two steps.**
   Measured: a `SIGNATURES` entry with no working scanner is *worse than no
   entry* — the panel reports `NOT FOUND` for a device that is served and open,
   where omitting the signature yields `OPEN`. So a signature **obliges** a
   scanner in the same commit, or the FUI confidently reports working hardware as
   unplugged. And `transport="usbfs"` buys less than it looks:
   `_match_signature()` (`discovery.py:932-940`) **accepts and ignores** its
   `transport` argument, so the field is documentation, not a discriminator. The
   one place it does bite is `_is_probe_candidate` (`discovery.py:831-859`), which
   requires `transport == "serial"` — so a non-serial transport is unprobeable
   *by construction*. That is the behaviour I want, but I should record that it
   comes free rather than from a decision I made.

   There is also a **count coupling** to satisfy at the same time:
   `test_the_bus_denominator_excludes_what_no_scan_can_find` asserts
   `scannable == len(INSTRUMENTS) - 1`, hardcoding exactly one unscannable
   instrument (the QR10x). Landing `INSTRUMENTS` + `SIGNATURES` + `scan_usbfs()`
   together keeps that arithmetic true; landing the row alone breaks it.

   Closest structural precedent is `945accf`, which exists because a driverless
   CH340 "was invisible to `scan_serial()`, which enumerates `comports()` — that
   lists ttys, and the whole problem is that no tty exists", so the QR10x read as
   "not plugged in" while plugged in and working. The ADU218 has the same
   no-tty shape but, unlike the QR10x, a clean VID/PID — so it can legitimately
   sit in the scan numerator once `scan_usbfs()` exists.

## 7. Registries a new driver must land in

Failure modes below are **measured against the installed package**, not inferred
from the PDU41002 commit stack. Two of my earlier entries were backwards; both
are corrected here, and the distinction matters because loud and silent misses
need different mitigations — a loud one needs no test, a silent one needs an
explicit test or it ships.

**Raises immediately (you cannot miss these):**

| File | Change | Failure if missed |
|---|---|---|
| `agent/registry.py:264` | `_adu` opener closure, lazy import | `BenchValueError: no opener for device key` at registry build |
| `sim/factories.py:162` | `make_adu218()` + `FACTORIES` entry | `BenchValueError: no simulator for device key` |
| `net/codec.py:76` | `ADU218Info` | **`BenchProtocolError: dataclass not in the wire-type allowlist`** — this one is loud, not silent |
| `config.py:38` (flag path) | `DEVICE_KEYS` | `cfg.build(local_devices=[...])` raises `BenchValueError: unknown device key` |

**Silent — each needs a deliberate test:**

| File | Change | Silent symptom |
|---|---|---|
| `config.py:38` (**file** path) | `DEVICE_KEYS` | `Config.from_dict` logs one WARNING and returns `devices: []`; `mode_for()` then says `"local"`. **A device configured `mode: remote` is served locally.** The flag path raises, the file path does not — that split is the trap |
| `net/errors.py:141` | every exception class | **this is the silent one.** Missing → round-trips to bare `RuntimeError` with `remote_class='ADU218CommandError'`; `except ADU218CommandError` never matches and nothing reports it. Needs a round-trip test *per class* |
| `dashboards/fui/view.py:130` | `INSTRUMENTS` row | the rail still draws a row — `kind=generic role=INSTRUMENT`. `view.py:186-215` appends unrecognised keys as INSTRUMENT, so the default is wrong rather than absent |
| `dashboards/state.py` | `RUN_EVENT_KINDS` | `state.py:1401` is `elif kind in RUN_EVENT_KINDS:` with **no else**, falling through to `log.append()`. The event shows in LOG.MGR while the panel folds no state |
| `deploy/udev/` | `63-benchctrl-adu218.rules` | **already committed** |

**A typo in `codec.py` or `errors.py` is indistinguishable from an omission.**
Both resolve names via `getattr(module, name, None)` plus a type check
(`codec.py:52-56`, `errors.py:147-155`), so a misspelled entry is silently
skipped. Spelling is not self-checking here.

**Correction to my earlier claim: `tests/test_session_config.py:44,119` are NOT
a mechanical gate.** Both only iterate `DEVICE_KEYS` asserting `mode_for` /
`is_remote` — assertions that hold for any string. Measured: appending an
unwired `ontrak_adu218` to `DEVICE_KEYS` leaves that file at 37 passed. **No
test in the suite fails when a `DEVICE_KEYS` entry is missing.** Stage 4 must
not rely on one.

`tests/test_mcp.py:604` remains the parity test to copy — but note parity is
enforced *per driver* by a hand-written test, so a new driver has no parity
coverage until its copy exists. Convention, not mechanism.

**The real gate in the FUI is a count coupling.** Adding to `INSTRUMENTS`
without a `discovery.SIGNATURES` entry fails
`test_the_bus_denominator_excludes_what_no_scan_can_find`
(`tests/test_dashboard_fui.py:544-551`), which asserts
`scannable == len(INSTRUMENTS) - 1` — i.e. it hardcodes that **exactly one**
instrument is unscannable (the QR10x). A second unscannable instrument breaks
it. See §6.3, which this changes.

**The ordering risk is not file-versus-file.** No commit in any of the four
driver stacks broke the suite mid-stack; the suite stayed green throughout. The
instructive case is `b148bd5`, which landed nine registration sites with **zero
test files** and a green suite — and three of them were wrong. `cd3eaf1` found
all three 1h44m later, and found them *by writing the tests*, not by running the
existing ones. So the ordering constraint that actually bites is
**tests-versus-registration**, and the three defects are worth naming because
each is available to this driver:

- `frozenset` missing from a codec `isinstance` check. Not a one-getter failure:
  the property snapshot rides on **every** `device.call` response, so one
  missing type took out every remote call to the device. The ADU218's own
  `allowed_relays` would be a frozenset.
- A discovery probe marker (`"DEV.TYPE"`) that was a substring of its own
  request, so any echoing device matched — and on hardware **discovery
  identified a mains switch as a programmable resistor.**
- A device that could not be probed at all. Probing the PDU wrote
  authentication failures into its own audit log and silenced the console ~15 s.

## 7a. Test-shape warnings from the same history

Three failures in this repo were invisible for structural reasons rather than
logic reasons, and all three shapes are available to this driver:

- **Per-driver test files let per-driver bugs recur.** `dc4c3f0`: "The identical
  bug was already fixed once, in `siglent_sdm4065a`, and the fix was never
  propagated — the per-driver test shape is what let it survive."
- **A shared resource torn down by one driver, logged below service level.**
  `3874f0a`: closing one VISA driver closed the process-wide `ResourceManager`,
  blinding the agent to all VISA hardware while the dashboard drew NOT FOUND for
  three connected instruments — and "a total loss of the VISA bus looked exactly
  like an idle bench." **The ADU218 holds a claimed usbdevfs interface**, so the
  same shape is reachable: `close()`/`RELEASEINTERFACE` misbehaving under a
  shared handle would be a silent bench-wide blindness.
- **Wire-shape bugs pass unit tests on both sides.** `c44ea14`: the panel's two
  worst defects were "a kind nobody emits (`run_started`) and a payload one level
  down (the `data` nesting)", and tests on either side passed because each built
  its own input. **So the watchdog-trace event must be emitted by a real engine
  in its test**, via the `_real_events` harness — never a hand-built dict. Note
  `tests/test_dashboard_runs.py:857-861` only sweeps kinds spelled `run_*`, so a
  kind named `adu218_watchdog_tripped` would sail past it.

## 8. Stages

Each is independently landable. **Stage 0 is done.**

- **Stage 0 — hardware characterisation. ✅ COMPLETE** (`1044910`). Six
  transcripts, relay control confirmed, watchdog confirmed, framing settled,
  udev rule installed. No production code.
- **Stage 1 — the link and the read surface.** `usbfs.py`, `ADU218Info` from
  the USB descriptor, exceptions, the per-command response-width table, every
  read method. `__init__.py`, `mcp_tools.py`, `sim/adu218.py`, factories,
  `DEVICE_KEYS`, `mcp.py`, FUI, docs. **Zero switching risk — no method here
  moves a contact.** Useful alone: relay and input state reporting.
- **Stage 2 — relay switching.** `set_relay_state`, `set_relay_port`,
  `reset_relays`, the allowlist, read-back verification, and the
  `dispatch.introspect()` test that pins every mutator into `surface.mutators`
  and every read out of it. That test is the guard on §5's naming decision.
- **Stage 3 — inputs, counters, de-bounce.** `RPyn`/`RPy`/`Py`/`PI`, `REn`,
  `RCn`, `DBn`. Needs no new hardware, but the inputs are unwired, so the
  simulator carries the load and the hardware tier can only assert "reads a
  well-formed zero".
- **Stage 4 — the watchdog, on its own.** Deliberately last and deliberately
  separate, because §4 makes it the one feature that can turn a working bench
  into a silently-dropping one. Ships with the synthetic-clock ladder test, a
  `KNOWN_LIMITATIONS.md` entry, and an explicit refusal to auto-feed.
- **Stage 5 — agent/remote integration.** Registry opener, codec, errors,
  discovery scanner, `safety.py` exemptions made deliberate (the ADU218
  implements none of the methods `default_safe_state()` walks, so it is
  currently inert *by accident* — same trap the PDU had),
  `tests/test_remote_ontrak_adu218.py`.
- **Not planned:** PWM or fast switching (the manual forbids it above 1 CPS at
  full load); `interfaces.Switch` (§6.1); run-engine relay setpoints (§6.2 —
  needs its own decision, not a borrowed one).

## 9. Simulator

`src/benchctrl/sim/adu218.py`. **It cannot subclass `SimDevice`** — that class
does `self.link = loopback or SerialLoopback()` and its `port` returns
`self.link.port`, `sim/factories.py:72-74`'s `_asrl()` builds
`f"ASRL{port}::INSTR"`, and all six existing sims subclass
`SimDevice`/`ScpiDevice` with a pty behind them. This device's channel is an
8-byte packet endpoint. The seam is `usbfs.py`'s four-method duck type
(`write`/`read`/`is_open`/`close`), the same informal shape `links.py` uses in
the PDU driver, so the sim provides a fake link and the real driver drives it
unmodified above that line.

**This will be the repo's first simulator that is not a byte stream, and there is
no precedent to copy.** The related-but-weaker precedent is that
simulator transport already legitimately differs from hardware transport: the
SCPI sims run over pyvisa-py's ASRL backend while the real SDM4065A is USB-TMC,
recorded in `docs/simulation.md:16,58-60` and `CHANGELOG.md:875`. So
"sim transport ≠ hardware transport" is accepted practice — just never yet with a
sim that isn't a stream at all. It is **not** in `KNOWN_LIMITATIONS.md`, so this
driver should add the entry rather than assume the divergence is documented.

It must replay the transcripts, not my reading of the PDF. Specifically it must
model, because each is a driver bug if unhandled:

- **silence on unknown commands**, and separately on write-only commands
- **`RI` timing out while `PI` answers** — the single most valuable behaviour to
  model, because it is the one the manual actively lies about
- **fixed response widths** per command (3/1/2/4/1/3/5/1/1)
- **the mandatory `0x01` prefix**, rejecting `0x00`/`0x02`/bare ASCII
- **responses queueing until read**, so finding 5 is reproducible offline
- **the watchdog resetting all relays and self-clearing `WD`**, on an
  injectable clock rather than wall time
- **4-bit input ports**, so an 0–7 index against one port fails in the sim too
- a `command_log: list[bytes]` so tests can assert exact emitted bytes
- **a queue depth of ~3**, not one, so a test can prove drain-until-empty is
  required and drain-one is insufficient
- **`RCn` as read-and-clear**, so a test can demonstrate that retrying it loses
  the count — the sim is the only safe place to exercise that
- **any command refeeding the watchdog**, invalid ones included, so the
  "status poller neuters the interlock" failure is reproducible offline. This is
  the single most valuable thing the sim can model, because it is the one that
  looks like success right up until it matters
- **relays holding state across a simulated suspend / handle close**, so the
  "close() does not de-energise" property is a test rather than a comment

## 10. Verification

- `~/bench-test/.venv/bin/python -m pytest -m "not hardware"` — the only env
  with pytest (python3.12). Baseline at `2eb0003`: **2111 passed, 6 skipped**.
- `pytest -m hardware -k adu218` against the real unit, DMM on K0.
- **Cross-check that is the real proof:** command a relay, then verify with the
  DMM — the device's own `RPK0` is not independent evidence.
- Mutation tests per memory's rules: **assert the test count** (rc=0 with
  nothing collected reads as a pass), hold mutated files outside the module,
  check for equivalent mutants first. Priority mutants: inverting the
  `expects_response` flag, dropping the `0x01` prefix, off-by-one on the 4-bit
  port width, and inverting the watchdog fed/unfed ladder.
- Compare ruff **rule categories** against a master worktree, never counts —
  the lint baseline is already dirty (~860 pre-existing errors with newer ruff).
- `deploy/sync-board.sh --check`.

## 11. Note on process

`AGENTS.md` step 3 asks for three advisory sub-agents (spec / accuracy /
integration) before driver code. They were spawned, the session compacted while
they ran, and their reports did not arrive with it — so I did step 4's work by
hand, which is what produced §2's five findings and §4's watchdog result.

**The spec agent later turned out to still be alive and its report has now
landed**, after the hardware work. That ordering was lucky rather than clever,
and it is worth recording which way the corroboration ran: measurement first
meant the manual could be checked against the device instead of the reverse.
Eight of the nine measured findings are documented *rules* rather than
coincidences, which is a much stronger footing than "it worked when I tried it" —
and one of them (`RI` vs `PI`) resolves a genuine self-contradiction *in* the
vendor document, which measurement alone could not have adjudicated.

The report also caught something I would not have caught myself: it took my
"opened after 3.7 s" at face value, as any reader would, and reasoned that WD1
therefore fires at ~3.7x its documented interval and that no WD setting could be
trusted without characterisation. That inference was sound; **my number was
wrong** — it timed my own observation, not the trip. Re-measuring properly gave
(0.90, 1.10] s. The lesson is the one memory already records about governors:
I had measured the wrong thing and labelled it the right thing. A figure that is
an artifact of the instrumentation reads exactly like a device property.

What the manual supplied that no amount of probing could, because probing is the
risk on a switching device: the bounded `WDn` ladder, the absence of any
non-volatile write, the absence of any mode-latch, and the four things not to
send. It also independently confirmed the on-resistance discrepancy is
unexplained *by the manual* — the on-state figures carry no test conditions at
all — and named the relay part (Panasonic AQZ207) as the next document to read.

Still outstanding: the **accuracy** agent's adversarial review (re-tasked, not
yet returned) and the **integration** agent's anti-pattern list, so §6 and §7
remain derived from reading the PDU41002 commit stack directly.

## 12. Deferred, inherited from master

Not this driver's work, but tracked so it is not lost — from WIP commit
`b44380e`, now on master:

- `agent/runs/analysis.py` (190 lines) has **zero tests**
- no `docs/runs.md` section and no CHANGELOG entry for run-analysis
- `run_analysis` is still absent from `RUN_EVENT_KINDS`

Also still outstanding: `sudo chown rick:rick /home/rick/pdu_creds.txt` — mode
is 600 but owner is `root:root`, so `rick` cannot read their own credential
file. `chown` is blocked in the sandbox, so this one needs the operator.

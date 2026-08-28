# benchctrl game plan — Ontrak ADU218 relay I/O driver

**Branch:** `feat/ontrak-adu218` (off `master` @ `2eb0003`)
**State as of:** 2026-08-27 — **all stages landed, every bench claim witnessed,
the PR open, everything pushed.** Get the commit count from
`git rev-list --count master..HEAD` rather than from here; it was carried forward
by hand for several addenda and was wrong by two. **Not linear:** `97b1e1b` is a
two-parent merge that brought master's CP2112 driver in, so `--no-merges` gives
one fewer. Sections 1-12 below were written
*before* the code and are kept as the design record; where the built driver
diverges, §5 and §13 say so. **Read §20, then §19, then §18, then §17, then §16,
first on return** — those are the bench results; §13 carries the vendor manual's
findings and the PA3 signal measurements.

**The PR is open:** https://github.com/rickmellor/benchctrl/pull/1, the repo's
first, `OPEN` and `MERGEABLE`. It was never blocked on the work — `gh` here is
authed as `generac-rick`, which had pull-only, while `git push` uses the personal
key and always worked; the operator added `generac-rick` as a collaborator on
2026-08-27 and the API flipped to `{"push":true}`. Note `gh pr edit` now fails on
this repo with a deprecated-GraphQL error (`projectCards`); patch the body over
REST instead (`gh api -X PATCH repos/…/pulls/1`).

Remaining: **nothing.** **Every bench item is done, including B1.** §16 closed
the counter map and the eight relays by walking the bench; §17 closed the
watchdog trip; §18 is the leftover-sweep that followed and found a real coverage
regression; §19 answers what B1 actually needs and recovers two vendor specs;
**§20 is B1 itself**, which the operator authorised and which found the one thing
§19 had not: the whole-port mask writes were checked only against the device's
own read-back, so `MKddd` had **no independent witness** until `90e18f5`. The
`SWEEP_ALL` gate is still in the code — satisfied for this bench, kept for the
next one.

**Resume by reading:** §13 of this file, then
`tests/fixtures/adu218/README.md` (the measured device behaviour — believe it
over the PDF), then `AGENTS.md`.

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
| 1 | **`RI` times out; `PI` answers.** §5 lists `RI`; §6b calls the same function `PI` in four places. Both spellings really are in the PDF, so this is a genuine internal contradiction the hardware settles — cite §6b, never §5. Stated as measured: silence is also how this device rejects a *valid* command with a bad argument, so a timeout cannot prove a command is unimplemented, only that this one is unusable | A driver written from the summary table hangs on *every* input read |
| 2 | **PORT A and PORT B are 4 bits each**, not 8. Eight inputs across two isolated ports | Why `PK` returns 3 digits and `PA` returns 2. Indexing inputs 0–7 against one port is wrong |
| 3 | **Silence is the only error signal.** An unknown command returns nothing — no string, no sentinel | The driver needs a per-command *"expects a response"* table; it cannot discover this at runtime, because correct write-only silence is byte-identical to an error |
| 4 | **The `0x01` prefix is mandatory and is specifically `0x01`** — bare ASCII, `0x00` and `0x02` are all ignored | A test asserting "byte 0 is non-printable" would pass for two encodings the device rejects |
| 5 | **A queued response outlives its command.** Interrupt-IN replies persist until read | A skipped or failed read makes the *next* query return the *previous* value — a silent wrong answer. Drain on open; pair reads and writes strictly |

Finding 5 is not hypothetical: it invalidated the first framing measurement,
which credited a bare-ASCII write with the previous prefixed command's answer.
That is exactly the "sim agrees with the driver's misreading" failure
`AGENTS.md` warns about, caught before any code existed.

Also measured, and **now attributed — it was never the relay**: closed-contact
resistance read **6.14 Ω** against Ontrak's quoted 700 mΩ typ / 1.1 Ω max.
Two things closed this, in order.

First, the comparison was never valid. The Panasonic `ASCTB467E` family
catalogue (AQZ207 has no standalone datasheet) supplies the test conditions
Ontrak's manual strips: on-resistance is specified at **`IF = 10 mA`,
`IL = Max.`, `Within 1 s`**, and `IL = Max.` is **1.0 A** for this part. Ontrak
copied the numbers from the correct column of the correct part — and dropped the
conditions. This bench measured at ~1 mA, three orders of magnitude below the
specified envelope, so **6.14 Ω cannot violate the 1.1 Ω maximum and cannot
satisfy it either.** This unit is not out of spec.

Second, the excess is now located, by an accident worth keeping: a later session
read the same closed contact at **10.694 Ω** — a **+4.55 Ω step** — with nothing
in software touched. `on_resistance_drift.txt` characterised it three ways and
the value is milliohm-stable on both sides of the step: 3.93 mΩ across ten
re-actuations (vs 0.12–2.35 mΩ within a single close) and 1.67 mΩ of drift over
a 28 s hold. That shape eliminates the two remaining candidates and confirms the
third:

- **Not the relay's R_on.** A semiconductor's on-resistance does not step 74%
  between sessions and then hold to 4 mΩ across ten actuations. The datasheet's
  only quantified R_on modifier is temperature, bounded at ~1.5x over 25→85 °C.
  (The tempting story, R_on climbing as load current falls, was already
  unsupported: there is no R_on-vs-current curve at all, and the one
  lower-current point — 0.4 A, ≈0.65 Ω at 25 °C — sits slightly *below* the
  1.0 A typical, trending the wrong way. The AC/DC two-MOSFET anti-series
  topology is real but already inside the published 0.7 Ω.)
- **Not a range-dependent DMM offset**, eliminated independently: pinned 200 Ω
  vs autoranged differ by **1.353 mΩ** against a 4550 mΩ step.
- **The series connection outside the relay** — DMM leads, K0 screw terminals,
  clip joints. A connection that is disturbed or lightly oxidised steps to a new
  value and holds there, which is exactly the observed shape. Both 6.14 and
  10.69 are stable readings of a *stable* connection; they are readings of two
  **different** connections.

Which connection moved is not identifiable from the host — those elements are in
series and all outside the relay. Separating them needs 4-wire with sense leads
genuinely attached (KNOWN_LIMITATIONS H-5) and is an operator task.

**CONFIRMED by the operator, and the confirmation is worth more than the
inference was.** Asked whether anything on the K0 path had been re-seated, the
answer was yes: **the probes were re-seated between the two sessions,
deliberately, to improve connection stability**, and untouched for the two hours
covering the drift and re-actuation captures. That matches the elimination on
every axis it could have failed on — the right *element* (probes, not the relay),
the right *interval* (between sessions, not during one), and the right *quiet
window* (nothing touched while the milliohm stability was being measured). The
attribution above was reached before the answer was known and did not depend on
it; this makes it a confirmed cause rather than a surviving candidate.

Two things follow that the inference alone could not establish. First, **the ~5 Ω
was probe-path resistance all along, in both sessions** — the 6.14 Ω figure was
never a relay measurement either. Nothing in the record was ever measuring the
part. Second, and more useful for the driver: the probes are **clamped into a
screw terminal, so the contact point quality is not guaranteed by construction.**
The operator's stated change is *mechanical* — the probe moves less than it did —
which is a different quantity from electrical contact resistance and does not
bound it.

**CORRECTION to my own claim, made one message earlier: I wrote that the
re-seated joint was "more stable (3.93 mΩ across ten actuations)" as well as more
resistive.** That is unsupported, and it is this branch's recurring error shape
for the **fifth** time — I compared the new session's *across-close* spread
(3.93 mΩ) against the old session's *within-close* spread (0.983 mΩ). Different
quantities. The only like-for-like pair available is within-close: **0.983 mΩ
before vs 0.12–2.35 mΩ after**, which is comparable at best and arguably slightly
worse. There is no across-close data from the earlier session at all, so the
comparison cannot be made in either direction. Noting where this one happened:
inside the paragraph congratulating the branch on having caught the shape four
times.

What survives is narrower and still enough. The new joint is stable **in absolute
terms over the measured window** — 3.93 mΩ across ten actuations, 1.67 mΩ over a
28 s hold — which is what licensed the elimination of R_on and of a range
artefact. It is *not* established to be more stable than before, and the reading
went **up** by 4.55 Ω. So repeat-measurement precision is no evidence that a
figure describes the relay: this one is milliohm-repeatable and measures a screw
clamp.

**Driver consequence, unchanged and now permanent rather than provisional:
never treat on-resistance as a validation threshold, and do not report a
contact-resistance figure at all.** The old reason was "the number is
unexplained"; the real reason is stronger and does not expire — the measurement
is dominated by series connections the driver cannot see, so any figure it
reported would be a property of the bench wiring wearing a relay's name. Key
open/closed on the DMM's overload sentinel, which stepped cleanly through all
ten actuations and is entirely insensitive to a 4.55 Ω shift in the closed
value. This is also a live demonstration of why: a driver that had thresholded
"closed" at `< 10 Ω` from the original 6.14 Ω observation would today report
every closed relay as open.

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

The **closed** figures in that column are ~6.14 Ω because these cycles predate
the series-connection step in §2; a later session reads ~10.69 Ω for the same
closed contact. That is why the table is read as *"a finite resistance vs the
overload sentinel"* and not as a resistance measurement — the qualitative
transition is what was confirmed, and it is what the driver keys on. Ten further
actuations in `on_resistance_drift.txt` reproduced the same clean transition at
the new value.

**Safety basis for switching at all:** K0's load side has only DMM leads, so
there is no load current and the manual's *"1 CPS at full load"* PhotoMOS limit
(with its 20%-of-rated-current escape clause) does not bind. Switching was
still kept slow (0.5 s settle) and the cycle count small.

**Ratings, for sizing the driver's posture:** Panasonic AQZ207 PhotoMOS, form A
N.O., **1 A @ 120 VAC / 120 VDC**, and — worth stating because an operator would
not infer it — **primary insulation only** (the sibling ADU208 is
double-insulated; this model is not). Confirmed against the part datasheet: the
AQZ207 is rated 1.0 A continuous at 200 V peak AC/DC, 1 Form A — the current and
voltage figures Ontrak quotes trace to the right column, so there is no variant
confusion.

**Isolation is deliberately not quoted here, because the figures name different
barriers.** The part datasheet's 2500 Vrms is the PhotoMOS's own
**input-to-output** rating — the LED-to-MOSFET gap inside one relay. That is not
the barrier an operator cares about, which is contact-to-anything-touchable, and
it is not the web page's 500 V **channel-to-channel** either. Ontrak's manual
gives 2500 Vrms without saying which barrier it bounds, and its web page gives
3500 V *and* 500 V, so at least two distinct barriers exist and no single number
covers them. Treating the part's input-to-output figure as a system isolation
rating would overstate the barrier that matters by an unknown margin. The rating
that *is* unambiguous and does constrain use is **primary insulation only**, and
that is the one the docs should lead with.

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
open by 1.10 s. **There is no timing anomaly.** None of the five design
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
the "inert governor" trap.** Five consequences, all in `watchdog.txt`:

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
claim**. Every mutator must therefore take an existing prefix. **As built** —
this replaces the original proposal, which under-counted the reads and had three
mutators returning `None` where they now return a verified read-back:

```python
# reads (deliberately no mutator prefix)
is_open -> bool  (the LINK, not a contact — see below);  relay_count -> int (8)
input_count -> int (8)   # 8 TOTAL, across two FOUR-bit ports
allowed_relays -> frozenset[int];  watchdog_setting -> int  # driver-held expectation
read_identity() -> ADU218Info          # from the USB descriptor, not a command
relay_state(index) -> bool             # RPKn — True == ENERGISED (conducting)
relay_states() -> dict[int, bool]      # PK, one round trip
relay_mask() -> int                    # PK as the raw mask
input_state(port, index) -> bool       # RPyn
input_states() -> dict[str, tuple]     # RPA + RPB
input_mask() -> int                    # PI — PORT A is the LOW nibble
read_counter(index) -> int             # REn
read_counters() -> dict[int, int]      # all eight
read_debounce() -> int                 # DB — the SETTING NUMBER, 0..2
read_debounce_ms() -> float            # what the setting MEANS: 10.0 / 1.0 / 0.1
read_watchdog() -> int                 # WD  <- also how a fired timeout is seen
read_watchdog_tripped() -> bool        # WD read against watchdog_setting

# writes (all prefix-matched)
set_relay_state(index, on, *, verify=True) -> bool
set_relay_port(mask, *, verify=True) -> int          # MKddd
set_debounce(setting) -> int                         # DBn, returns the read-back
set_watchdog(setting) -> int                         # WDn, returns the read-back
clear_counter(index) -> int                          # RCn — reads AND clears
reset_relays(*, verify=True) -> int                  # MK000, the safe state
close(); __enter__; __exit__
```

Two notes on that surface that only became clear later:

- **`read_debounce_ms()` exists because the setting number is actively
  misleading.** The manual's §6c gives `0 = 10 ms, 1 = 1 ms, 2 = 100 µs`, so a
  *higher* setting is a *shorter* filter. It is a read, and it starts with
  `read_`, so the dispatch gate classifies it correctly with no special case.
- **`clear_counter()` is the only responsive command that mutates state**, so it
  must never be retried — a retry silently discards counts.

### RESOLVED: `is_open` keeps its framework meaning, and relay state never uses it

This needed settling **before** any code, because `is_open` would otherwise have
to be renamed after Stage 1, and it appears in five registries. It is not a free
choice: `agent/registry.py:45` defines `is_open` as *"the driver object exists"*
and `to_dict()` publishes it as the `"open"` key for **every** device the agent
serves, so it reaches the dashboards and the RPC wire with that meaning already
fixed. Seven other implementations agree (`transports/ch341.py`,
`transports/ptybridge.py`, `sim/loopback.py`, `drivers/otii_arc/transport.py`,
`drivers/eastwood_qr10x/driver.py`, and both `cyberpower_pdu41002` links) — in
all of them `is_open` means **the link is connected**.

For a relay that word is overloaded in the worst possible way: an *open* relay
is **not** conducting, while an *open* driver **is** connected. The two senses
are not merely different, they point in opposite directions on the thing an
operator cares about. So:

1. **`is_open` on the ADU218 driver means the usbfs link is connected**, exactly
   as everywhere else. It says nothing about any contact. Changing its meaning
   for one device would make the agent's own `"open"` field mean two things
   depending on `device_key`.
2. **No relay-facing name may use `open`, `close`, `opened` or `closed`.** That
   already rules out `close_relay()`/`open_relay()`, which the mutator-prefix
   gate rules out independently — two unrelated reasons, which is why the
   surface above has neither.
3. **`relay_state(index) -> bool` must document its polarity explicitly, and
   `True` means energised/conducting**, matching `set_relay_state(index, on=…)`
   and the device's own `RPKn`=1. A bool whose polarity is inferred from the
   method name is exactly the wire-shape defect class memory records: a value
   nobody stated, read the wrong way round by the second caller.
4. `close()` remains `close()` — it is the framework's teardown verb and takes
   no relay argument, so it cannot be confused for a contact operation. Its
   docstring must still say it **does not de-energise the relays**, because that
   is a genuine hazard and unrelated to naming.

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
- **200 ms read timeout — now measured on this device, not inferred.** It was
  originally justified two ways, *neither* a measurement of this hardware:
  Ontrak's examples all use 200 ms, and `bInterval` 10 at low speed implies a
  ~10-20 ms floor by arithmetic. Meanwhile every capture the timeout has to
  survive was taken at **2000 ms** (`errors.txt:5`) or at a value the transcript
  never recorded (`framing.txt`, trials B–E — the entire evidence base for the
  `0x01` prefix). Justifying the threshold with one setup and validating it with
  another is the same mistake as the "3.7 s" watchdog figure and the 1 mA-vs-1 A
  resistance comparison, and it matters most here because **silence is this
  device's only error signal**: a premature timeout does not lose a reply, it
  leaves it queued on EP `0x81` (finding 5), so `verify=True` would report a
  relay position one command out of date with no exception raised.

  Measured in `latency.txt` and `latency_after_command.txt`:

  | Sample | n | median | p99 | max |
  |---|---|---|---|---|
  | `PK`, idle | 200 | 15.99 ms | 16.60 ms | **16.65 ms** |
  | `RPK0`, idle | 200 | 15.99 ms | 16.55 ms | **16.65 ms** |
  | `RPK0` read-back, immediately after `SK0`/`RK0` | 40 | 16.18 ms | 16.68 ms | **16.68 ms** |

  So 200 ms carries **12x margin over the worst observed round trip**, the
  `bInterval` arithmetic was right, and zero short reads occurred in 440
  transfers. The post-command row is the one that matters and the one the idle
  sweep would have missed: the driver's critical path is command-then-read-back,
  so if firmware were busier just after actuating an opto-coupler the deciding
  latency would be the unsampled one. It is not — 16.68 vs 16.65 ms, and all 40
  read-backs agreed with the commanded state.

  Re-validated at the shipping value, closing the setup mismatch: every
  documented silence (`RI`, `XYZ`, `RPK8`, `RPA4`, `DB9`, `MK9999`) still returns
  `ETIMEDOUT` at a 200 ms timeout, and a following `PK` answered normally with
  **zero replies left queued** — so the timeout does not desynchronise the
  pairing. `errors.txt` established those same silences at 2000 ms; they now hold
  at the value the driver will actually use.

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

## 5a. `safety.py`: the PDU's reasoning inverts here — DECIDED, no code needed

Reading `agent/safety.py` for the registration sweep turned up a gap the plan
did not cover. Both hooks are duck-typed on method *names*, so what the ADU218
gets is decided entirely by what I choose to call things — silently, with no
registration step and no test that fires.

**`default_safe_state()`** walks `stop_recording`, `set_output(False)`,
`set_input(False)`, `set_current_limit_enabled(True)`. The planned surface
(`set_relay_state`, `set_relay_port`, `reset_relays`, …) implements none of them,
so on a governor trip **the ADU218 does nothing** — currently inert by accident,
exactly the state the PDU was in.

**But the PDU's justification does not transfer, and this is the point.** That
docstring argues inertness is *correct* because cutting mains is itself the
disruptive act — it de-powers a DUT mid-measurement and drops other instruments'
sessions. None of that is true here: these are 1 A signal-level SSRs, the DUT rail
is not on them, and `MK000` costs one 8-byte write. So for this device
"de-energise on trip" is closer to the ordinary meaning of safe state than it is
for any mains switch. **`reset_relays()` is a genuine candidate for the trip
path** — which is the opposite conclusion to §6.2's, and the two must not be
conflated: staying out of `SWITCHED_PDU_KEYS` is about *run-engine setpoints*,
not about *trip behaviour*.

Three things to settle before Stage 5. **All three are now settled** — two by
reading the code, the third by the user's decision (item 3). The outcome is that
`safety.py` needs no change for this driver, but the *omission must be
documented*, which is not the same as leaving it alone.

1. **CORRECTION — reusing `panic_outlets` is not free, as I claimed.** I wrote
   that the `panic_outlets_of()` duck type "already generalises, so this needs no
   change to `safety.py`". That is true of the *authorisation* check and false of
   the *cut*. `panic_outlets_of()` (`safety.py:447`) is duck-typed on the
   property, but the function that does the work,
   `panic_outlet_safe_state()` (`safety.py:483`), hardcodes the PDU's **method
   names**: `obj.set_outlet_state(outlet, False, verify=False)` at :507 and
   `obj.outlet_state(outlet)` at :520. An ADU218 exposing `panic_outlets` would
   therefore be *selected* as a panic target at :244 and then raise
   `AttributeError` on every relay inside the cut — reported as `FAILED`, i.e. a
   trip path that looks wired and cannot work. So there are two real options and
   the choice must be explicit:
   - **(a)** name the ADU218's own methods `set_outlet_state`/`outlet_state`,
     which is wrong — these are relays, not outlets, and it would collide with
     §5a's whole point that the PDU's reasoning does not transfer; or
   - **(b)** give the driver a distinct property (e.g. `panic_relays`) and a
     small `safety.py` branch, or generalise the cut to look up the accessor pair
     alongside the authorisation property. **(b)**, and the cost is a real
     `safety.py` change, not zero.

   Either way the timeout maths needs revisiting too: `_panic_cut_for()` sizes
   its budget from `PANIC_CUT_CONFIRM_S + PANIC_CUT_PER_OUTLET_S * n`, both
   derived from a contactor's 3 s `td_off`. An ADU218 relay settles in
   milliseconds and its whole round trip is 16.7 ms measured, so the PDU's budget
   is ~3 orders too generous — harmless for correctness, but it would make a
   wedged ADU218 sit in the trip path for seconds before escalating.

2. **CONFIRMED by measurement, not by reading: the surface intersects
   `_ARMING_CALLS` nowhere, and every method classifies correctly.** Checked the
   planned surface against the live `_ARMING_CALLS` and `dispatch.is_mutator`:
   intersection is empty; all six writes (`set_relay_state`, `set_relay_port`,
   `set_debounce`, `set_watchdog`, `clear_counter`, `reset_relays`) classify as
   mutators; all twelve reads do not. So energising a signal relay will not start
   a deadman countdown, and no read requires a writer claim.

   The concern stands unchanged though, because the *mechanism* is still a naming
   coincidence — `set_relay_state` misses only because it is not spelled
   `set_output`. Hence the test asserting the intersection is empty **and** that
   the mutator/read split is exactly as above, so a later rename that reclassifies
   a method fails loudly instead of silently ungating a relay.

3. **DECIDED by the user: the watchdog is the interlock. The driver does not
   open relays on a governor trip.** Default behaviour is plain relay toggling;
   an operator who wants deadman coverage for a given test enables `WD`
   explicitly. So the ADU218 stays out of `default_safe_state()` and gets no
   `panic_relays` property — option (b) in item 1 above is **not built**, and
   `safety.py` needs **no** change for this driver.

   This is a real simplification, not a deferral, and it is worth being explicit
   about what it costs and buys:

   - **What it buys.** The trip path stays honest. A `reset_relays()` hook is
     software in the decision, so it fails exactly when the failures that matter
     occur — a wedged agent, a killed process, an unplugged cable. `WD` fails
     *closed* against all three, in (0.90, 1.10] s, measured. Adding a software
     hook alongside the hardware one would have made the weaker mechanism the
     more visible one, and §4 already records that the inert-governor trap is
     about mechanisms that *look* wired.
   - **What it costs, stated plainly.** With `WD` unarmed — the default — a
     governor trip leaves the relays exactly as they were. That is the correct
     reading of this device (1 A signal SSRs, DUT rail not on them), but it must
     not be *silent*. The inertness the PDU has by accident, the ADU218 will have
     **on purpose**, and the only thing distinguishing those two states in the
     codebase is a docstring that says so.
   - **Therefore the deliverable in Stage 5 is documentation, not code**:
     `default_safe_state()` gains a comment naming the ADU218, stating that its
     omission is deliberate, that `WD` is the interlock, and that the PDU's
     mains-disruption reasoning immediately above it is *not* the reason. Without
     that, the next reader inherits the PDU's argument by proximity — which is
     precisely how this gap was created in the first place.
   - **And `read_watchdog()` carries more weight than before.** It is now the
     *only* way an observer learns the interlock fired, so it stays a read (no
     writer claim), and the `WD`-self-cleared-to-0 trace belongs in the run event
     stream. §4's ambiguity caveat applies: `WD=0` means both "timed out" and
     "never enabled", so the driver must hold its own armed-state to interpret it.

   Item 1's correction stands as a **record of a real trap rather than work to
   do**: had the trip hook been built by reusing `panic_outlets`, it would have
   been selected as a panic target and then raised `AttributeError` on every
   relay. Anyone who later revisits this decision needs to read item 1 first.

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

  **1a. The link seam — DONE.** `usbfs.py` (`Adu218UsbfsLink`,
  `Adu218Device`, `enumerate_devices`, `find_device`) plus `__init__.py` and
  `tests/test_usbfs_adu218.py` (61 tests, hardware-free). Verified on the real
  unit and captured in `tests/fixtures/adu218/link_hardware.txt` and
  `link_dmm_witness.txt`. Five things came out of building it that the
  reconnaissance had not settled:

  - **The ioctl constants are computed, not hardcoded.** `USBDEVFS_BULK`
    embeds `sizeof(struct usbdevfs_bulktransfer)` — 24 on 64-bit, 16 on 32-bit
    — so the literal `0xC0185502` that the probe scripts used is *correct only
    on aarch64*. An armhf agent would have got `ENOTTY` from a constant that
    looks like a fact. Derived via `_ioc()`, and the derivation is pinned to
    all three measured literals by test, so a struct slip fails on a laptop
    rather than on a bench.
  - **Enumeration goes through sysfs, not descriptor parsing.** Cheaper (no
    device I/O at all) and it is the only place the serial number is
    available — the raw node exposes binary descriptors, and string
    descriptors need a control transfer sysfs has already done. So
    `ADU218Info` will need no control transfers either. Note `bcdDevice` is
    `0000`: there is **no firmware version to report**, and inventing one from
    the product string would be a guess dressed as a fact.
  - **`find_device()` refuses to choose.** Two ADU218s present raises and
    names both serials rather than returning `devices[0]`, because "the first
    device" depends on cabling order, and picking wrong energises a different
    bench. `path=` is honoured even when sysfs did not list it, so a caller who
    names a node gets a clear `open()` failure instead of "no ADU218 found".
  - **The drain uses the FULL 200 ms timeout, and the first draft did not.**
    I wrote it with a short timeout as an optimisation. That is worse than
    having no drain: it leaves the reply queued *and* reports the queue clean,
    so the next query still returns the previous command's answer — now with a
    log line claiming the queue was checked. One 200 ms wait per `open()`.
    Bounded by `limit=64` so a flooding device cannot hang `open()`.
  - **The 200 ms timeout survived its own test.** All three documented
    silences (`RI` absent, `ZZZ` garbage, `RPK9` valid-command-bad-argument)
    still time out at 200 ms, and `drain()` afterwards finds **0 replies
    queued** — so the 10x reduction from the 2000 ms evidence base does not
    convert an error into a stale success. That was the open risk in shrinking
    it and it is now closed by measurement.

  Also worth recording: `link_dmm_witness.txt` deliberately includes repeated
  commands (`SK0,SK0` and `RK0,RK0`), which proves the relay commands are
  **absolute, not toggling** — a driver built on a toggle assumption would
  have failed exactly those rows. And the script's own "9 transitions" summary
  line overcounts: only 6 are state changes. The fixture says so rather than
  quoting the flattering number.
- **Stage 2 — relay switching.** `set_relay_state`, `set_relay_port`,
  `reset_relays`, the allowlist, read-back verification, and the
  `dispatch.introspect()` test that pins every mutator into `surface.mutators`
  and every read out of it. That test is the guard on §5's naming decision.
- **Stage 3 — inputs, counters, de-bounce.** `RPyn`/`RPy`/`Py`/`PI`, `REn`,
  `RCn`, `DBn`. Needs no new hardware, but the inputs are unwired, so the
  simulator carries the load and the hardware tier can only assert "reads a
  well-formed zero".
  **Superseded on 2026-08-26:** the operator put a manual square wave on PA3
  from the SDG1032X, so the inputs are no longer unwired and the hardware tier
  now asserts real counting. See §13.
- **Stage 4 — the watchdog, on its own.** Deliberately last and deliberately
  separate, because §4 makes it the one feature that can turn a working bench
  into a silently-dropping one. Ships with the synthetic-clock ladder test, a
  `KNOWN_LIMITATIONS.md` entry, and an explicit refusal to auto-feed.
- **Stage 5 — agent/remote integration.** Registry opener, codec, errors,
  discovery scanner + `SIGNATURES` (one commit, per §6.3), `safety.py`, and
  `tests/test_remote_ontrak_adu218.py`. Per §5a the `safety.py` work is a
  **documented omission** — a comment in `default_safe_state()` recording that
  the ADU218 is deliberately absent because `WD` is the interlock, plus the test
  asserting the surface intersects `_ARMING_CALLS` nowhere. No trip hook, no
  `panic_relays`, no change to the cut path.
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
- `pytest -m hardware -k adu218` against the real unit, DMM across whichever
  relay `BENCHCTRL_ADU218_RELAY` names — K0 originally, **K7 since 2026-08-27**.
- **Cross-check that is the real proof:** command a relay, then verify with the
  DMM — the device's own `RPKn` is not independent evidence.
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

## 13. Addendum, 2026-08-26 — the manual, and the signal on PA3

Two things arrived after §1-§12 were written, and both changed conclusions rather
than merely adding detail.

### The vendor manual was in the repo the whole time

`references/adu208218v2.pdf`, gitignored (copyrighted, binary), read with
`pdftotext -layout`. An earlier claim in this session that `references/` held no
ADU file was simply wrong. What it settled:

- **§6c gives the de-bounce settings as DURATIONS, and the ordering is
  INVERTED**: `0 = 10 ms`, `1 = 1 ms` (default), `2 = 100 µs`. So the highest
  setting is the *weakest* filter. `DEBOUNCE_SETTINGS = (0, 1, 2)` on its own
  invited two wrong inferences — that 0 means "off", and that a bigger number
  means more filtering — and **no millisecond value existed anywhere in the
  repo**. This was a real footgun in my own driver, fixed by adding
  `DEBOUNCE_MS`, `read_debounce_ms()`, `debounce_ms` alongside `debounce` in the
  MCP returns, a `docs/drivers.md` table, limitation F-25, and a test asserting
  the **strict ordering** `ms[0] > ms[1] > ms[2]` so the values cannot be
  silently re-sorted into "sensible" order by a later reader.
- **"Count low to high transitions" (§6c)** — once per cycle, not once per edge.
- **Max Frequency 1 kHz**, above which the count under-reports **silently**. The
  driver cannot detect the overrun, which is what makes the ceiling worth a
  named constant (`COUNTER_MAX_FREQUENCY_HZ`) rather than a docstring aside.

And one thing it conspicuously did *not* settle: **Table 1, the counter↔input
map, is an IMAGE.** `pdftotext` drops it entirely, leaving blank space between
the caption and the next paragraph. So that mapping is measurable or it is
unknown — there is no third option, and no amount of re-reading the text helps.

### The signal on PA3 settled what the document could not

The operator drove PA3 manually from the SDG1032X. Measured, at two frequencies
an order of magnitude apart:

| source | 0.5 Hz | 10 Hz |
|---|---|---|
| device counter | 0.500 /s | **10.030 /s** |
| host level-sampling, rising | 0.500 Hz | 9.997 Hz |
| host level-sampling, falling | — | 9.997 Hz |
| ratio | 1.000 | **1.003** |

**Cycles, not edges** — both-edges counting would have given 2.0, not 1.003. The
two methods fail in different ways, so their agreement is evidence rather than
one measurement repeated. Also: `input_mask()` → `0b00001000` (the first `1` ever
read on an input line on this bench), **counter 3 alone moved** across all eight,
which measures the Table 1 mapping, and `RC3` is genuinely read-and-clear
(`RE3` 98 → `RC3` returned 98 → `RE3` 0).

One harness artifact worth naming so it is not re-diagnosed as a device problem:
the 10 Hz run flagged 63 single-sample runs, which reads like contact bounce. It
is **host-sampler aliasing** — roughly 3 samples per 50 ms half-period. The
0.5 Hz run, sampling the same way, had zero.

### B4 was a negative result, and it stays recorded as one

Varying only `DB` against the fixed 10 Hz wave, 20 s each: 10.042 / 9.992 /
9.992 counts/s. A 0.5 % spread — indistinguishable. That is **expected**, not a
failure: every filter width is far shorter than a 50 ms half-period, so none has
anything to reject. The scope, stated rather than glossed: **a passing de-bounce
round-trip proves acceptance, not effect.** Discriminating the three settings
needs roughly 100-500 Hz, against counters rated to only 1 kHz with silent
under-reporting above that, so the useful window is narrow and I did not push
toward the ceiling unattended.

### What is still unwitnessed

- ~~**PORT B's counter map.**~~ **Closed 2026-08-27** — see section 15.
- ~~**Six of the eight input lines.**~~ **Closed 2026-08-27** — all eight are
  driven and all eight counters witnessed; see section 16.
- ~~**Seven of the eight relays, independently.**~~ **Closed 2026-08-27** — all
  eight independently witnessed with the meter, one at a time; see section 16.
- **De-bounce's effect**, per the above.
- **The two simultaneous whole-port mask writes.** The walk drove each relay
  alone through `set_relay_state`; `0b10101010` / `0b01010101` via
  `set_relay_port` (the `MKddd` path) is covered in the simulator and has never
  run on hardware. It lives inside the opt-in all-eight test, so it stays behind
  the operator's gate — but it is now the *only* thing that test uniquely adds,
  which is a much smaller claim than it was.

### Two tests were written, committed, and had never run — one still hasn't

`test_all_eight_relays_switch_on_the_real_device` and
`test_the_watchdog_trips_and_the_dmm_sees_the_contact_open` are gated on
`BENCHCTRL_ADU218_SWEEP_ALL=1` and `BENCHCTRL_ADU218_ARM_WATCHDOG=1`. Both flags
are defined, by the wording of their own skip messages, as *the operator* stating
they know what is attached. An attempt to set the first one from this side was
correctly blocked — `AGENTS.md`: a route that bypasses a gate is not the same as
satisfying it. They are handed over, not worked around.

**The watchdog one has since run** — the operator authorised its flag in as many
words (*"The bench is safe and only used for this development. Set the
watchdog."*), which is the difference between naming a task and naming a gate.
See §17, which also records that the test as committed here could not actually
support its own claim. `SWEEP_ALL` is still unset and still the operator's.

Design note on the watchdog test that is easy to lose: **the DMM is the witness
precisely because it sends nothing to the ADU218** and therefore cannot feed the
timer. Any ADU218 read would refeed it, so the obvious instrument is the wrong
one.

## 14. Addendum, 2026-08-27 — the meter moved to K7

The operator moved the DMM off the CP2112 and onto **K7**. That un-blocked the
three witnessed tests — the suite went from 8 passed / 4 skipped to **10 passed /
2 skipped**, the only remaining skips being the two operator gates above — and it
immediately falsified two assertions that had been calibrated on K0's wiring.
Fixed in `d981d07`.

### Closed-contact resistance is a property of the wiring, not the relay

`TEST_RELAY` was 0 and is now **7**, and that single change surfaced the finding
that matters beyond this driver:

| observation | figure |
|---|---|
| K0 closed | 9.483 Ω |
| K7 closed, same meter and session, identical PhotoMOS parts | **36.02–36.22 Ω** |
| the same relay across four sessions (probes re-seated) | 6.14 / 10.69 / 10.65 / 9.40 Ω |
| K7 within one session, 15 back-to-back runs of one test | **62 → 127 → 61 Ω** |

Nearly 4× between two relays on the same device, because the excess is lead and
clip resistance *outside* the relay. **Any `< 10 Ω` "closed" rule derived from K0
calls a perfectly good K7 open.** §3's decision to read that table as *"a finite
resistance vs the overload sentinel"* rather than as a measurement is what
survived the re-wiring — a categorical distinction rather than a quantitative
one. This is the single most load-bearing decision in the hardware file, and it
is now recorded as **F-27** in `KNOWN_LIMITATIONS.md`.

### Within-session stability was also a wiring property, and it was a latent flake

`hi < lo * 2` on the two closed readings carried the comment *"the within-session
spread is milliohms"* — true of K0's screw clamps, false of K7's spring clips.
Measured over 15 back-to-back runs of the exact test sequence, the worst
within-trial ratio was **1.80 against a limit of 2.00**, so it failed roughly one
suite run in seven. Diagnosed by *measuring the contact drift*, not by re-running
until green.

Now `hi < lo * 10`, with what that gives up stated at the assertion rather than
implied: a K7→K0 lead move is a ratio of 3.81 and K0→K7 is 6.64, and neither
trips it any more. That coverage was already illusory — a bound the clips can
breach unaided reports lead movement that did not happen, and no threshold
separates 3.81 from a 1.80 the hardware produces on its own. What genuinely
catches a lead on the wrong relay is the pair of relay assertions below.

### The volts gate has a blind spot that reproduces the bug it was written for

The DC-volts wiring gate added in B9 catches leads left on a *powered* net
(3.392 V, the CP2112 case). It cannot catch leads across a **different dry
contact**: another open contact reads ~0 V exactly like the right one, so a stale
`BENCHCTRL_ADU218_RELAY` *fails* rather than skips, and the failure still accused
the relay. Both witnessed assertions now name both causes — "either that relay is
not switching, or the leads are not across it" — and print the device's own
read-back so the operator knows which to go and look at. The `reset_relays`
control assertion had no message at all and now has one, because that assertion
failing is precisely what makes the open-contact claim beneath it unfalsifiable.

Verified by pointing the suite at **K5 while the leads sat on K7**: both
witnessed tests fail, and their messages name the wiring rather than the relay.
That is what makes the K7 passes mean something.

Verification of the whole change: **6 consecutive full hardware runs at 10 passed
/ 2 skipped**, ruff rule/count profile identical to `HEAD` for the changed file,
the file still collecting hardware-free (12 skipped), and the board `--check`
IN SYNC at 105 files identical.

## 15. Addendum, 2026-08-27 — the generator moved to PORT B

The operator moved the SDG1032X off PA3 to "a PB input" without saying which,
which turned the last open item on this plan into a measurement rather than a
confirmation. That framing matters: a probe that asks "is it on PB0?" answers a
question about my guess. The probe written instead sampled **all eight lines and
read all eight counters**, so the result is *which one moved*.

### The finding

| observation | figure |
|---|---|
| driven line | **PB2** — 57 high / 63 low over 120 samples |
| the other seven lines | stuck low, all 120 samples |
| counter that moved | **6**, and only 6 of the eight |
| rate | 201 events in 20.13 s = **9.987/s** against a 10 Hz wave |
| de-bounce at the time | `DB = 1` → 1.0 ms |

So **PB2 → counter 6**, and `4 + 2 = 6` confirms the Table 1 image's offset by
measurement.

### Why PORT B was the reading that mattered

The PA3 result — counter 3 for line 3 — is consistent with an offset of 4, an
offset of 3, and an offset of 0. On PORT A the counter index simply *equals* the
line number, so no PORT A measurement can distinguish them. PB2 → 6 is the first
reading that can, and a wrong offset would have shown counter 4, 5 or 7.

Pinned as a mutation rather than left as an argument: a copy of the hardware
suite with the fixture's offset forced from `else 4` to `else 3`, held in `/tmp`
**outside** the module so the real file was never mutated, ran against B2 and
gave **3 failed, 9 deselected** — the test count asserted, not just the return
code, and the failure was `assert (64 & (1 << 5))`, i.e. the mutated bit index
against the real one, not an unrelated error.

### The control is part of the result

PA3 read low across all 120 samples and counter 3 sat frozen at 25039 across the
20 s window. Without that, "counter 6 moved" is equally consistent with every
counter counting regardless of wiring, or with the old stimulus still being
measured. A delta of exactly 0 on the line the generator *left* is what makes
this the moved lead.

### PORT B's bit ordering, witnessed for the first time

The four input commands order their bits differently, and until now every check
had been against PORT A:

| command | ordering | PB2 expectation |
|---|---|---|
| `RPy` → `input_states()` | MSB-first **text**, reversed by the driver | index 2 |
| `Py` → `input_port_mask('B')` | LSB-weighted decimal, no reordering | `0b0100` |
| `PI` → `input_mask()` | both ports, **PORT A in the LOW nibble** | `0b01000000` |
| `RPyn` → `input_state('B', n)` | per line | line 2 |

All four agreed. The `RPy` reversal is the one worth dwelling on: getting it
wrong makes PB2 read as line 1, an off-by-three that produces the *correct*
answer for exactly the all-zero reply every unwired bench returns. The earlier
sweep's "PORT B ever-high = []" was therefore an absence of evidence, not
evidence of correctness.

**Sampling method is load-bearing.** Each command was sampled 90 times and the
**union** of high bits asserted **equal** to the single expected bit. A single
read of a 10 Hz line catches the low half about half the time — precisely the
flake fixed earlier in this branch — and my first probe hit it, catching PB2 on
`input_port_mask('B')` and missing it on the other two in the same instant. That
is a disagreement between commands only if you believe one read. And "ever high"
alone would pass on a reply with every bit set, so equality is what makes it a
check.

### Three ways the suite's PORT B path was shown to be real

1. **It passes**: `BENCHCTRL_ADU218_INPUT=B2` → **10 passed / 2 skipped**, the
   only skips being the two operator gates.
2. **It falsifies**: pointed at an undriven B1, the three counter tests *skip*
   with `PB1 is not toggling (read only {False})` rather than false-passing.
3. **It kills a mutant**: the offset mutation above.

### Changes landed with it

- `BENCHCTRL_ADU218_INPUT` now defaults to **`B2`**, tracking the bench the way
  `BENCHCTRL_ADU218_RELAY` tracks K7. A stale default skips with the line named,
  so it cannot silently pass. **← this last sentence was wrong, and §18 is what
  it cost.** It closes the *correctness* half only: it cannot silently pass, but
  it can silently stop running. The default was stale within the hour.
- The `counted` fixture's spec parsing was **defensive-guard bugged**: it did
  `int(spec[1:])` *before* the `pytest.skip` that exists for a malformed value,
  so `BENCHCTRL_ADU218_INPUT=XY` raised `ValueError` from the fixture instead of
  skipping — the guard could not fire for most of the inputs it was written to
  catch. Now one `re.fullmatch(r"[AaBb][0-3]", spec)` before any parsing.
- F-26, the module docstring, the fixture docstring and the CHANGELOG now say
  the map is measured on both ports, and say which lines are *not* driven, so
  "witnessed" is not read as "exhaustive".

### Gotcha worth keeping

Running a probe through the agent (`RemoteClient.attach`) makes the agent open
the device lazily and **hold interface 0**, so the next direct-open hardware run
skips all 12 tests with "interface 0 is busy". That is N-4's single-writer guard
working, not a regression — but it presents as a whole suite going dark. `-rs`
gives the reason; `sudo -n systemctl restart benchctrl-agent` releases it.

## 16. Addendum, 2026-08-27 — the bench walk, and every claim made exhaustive

Commit `49cc718`, 32 commits, pushed. The operator offered to reposition the DMM
and the signal generator one terminal pair at a time: *"Say the word and I'll
connect to one relay and one input, you measure, then tell me to switch and I'll
reposition."* Eight steps, each one a full hardware suite run plus a probe that
captured the numbers.

**This is strictly stronger than the gated all-eight sweep**, and it is worth
being precise about why, because the two look interchangeable and are not. The
sweep energises all eight relays at once and can independently witness exactly
**one** of them — the bench has a single meter, sitting across whichever relay
`BENCHCTRL_ADU218_RELAY` names; the other seven rest on the device agreeing with
its own read-back. Walking the meter inverts that: **eight independently
witnessed relays instead of one**.

It also did not bypass the `SWEEP_ALL` gate so much as make it largely
unnecessary. Each step opened the device with `allowed_relays` pinned to the
**single** relay the operator had just wired and vouched for, so no step ever
energised an output nobody had nominated — which is precisely the condition that
flag exists to assert. The flag was never set.

### Relays — all eight, independently witnessed

Every position: overload when open, a finite resistance when closed, and the
alternating five-transition check passing.

| relay | closed (Ω) | within-position spread (Ω) |
|---|---|---|
| K6 | 16.87 | ≤ 0.06 |
| K7 | 17.50 | ≤ 0.06 |
| K1 | 20.77 | ≤ 0.06 |
| K2 | 28.37 | ≤ 0.06 |
| K3 | 31.36 | ≤ 0.06 |
| K4 | 32.09 | ≤ 0.06 |
| K5 | 41.19 | **0.544** |
| K0 | 45.65 | ≤ 0.06 |

A **2.7× spread with no relation to index**, on identical PhotoMOS parts, one
meter, one hour. F-27's "the excess is outside the relay" conclusion now rests on
eight points rather than two. The within-position figures localise something the
two-relay version could not: the drift that once broke `hi < lo * 2` is **clip
seating specifically**, not the bench and not the device — seven positions held
to hundredths of an ohm and one did not.

### Counters — the map is complete, and the image is now redundant

| line | counter that moved | rate (vs 10 Hz) | `PI` bit |
|---|---|---|---|
| PA0 | 0 | 9.972/s | 0 |
| PA1 | 1 | 9.972/s | 1 |
| PA2 | 2 | 10.071/s | 2 |
| PA3 | 3 | 9.972/s | 3 |
| PB0 | 4 | 10.071/s | 4 |
| PB1 | 5 | 10.063/s | 5 |
| PB2 | 6 | 9.973/s | 6 |
| PB3 | 7 | 10.071/s | 7 |

At each position the named counter was the **only** one of eight to move, and the
`PI` union across 60 reads set that bit and no other. Every rate lands within
±0.5 % of the stimulus — ±1 event of quantisation in a 10 s window, so nothing is
dropping or doubling.

Table 1's image is now **redundant rather than corroborated**: the map does not
depend on it at any position. And the PORT B offset, which §15 established on one
line, now rests on **three independent readings** (PB0 → 4, PB2 → 6, PB3 → 7).
PORT A still cannot pin it and never will — there the counter index equals the
line number, so the term under test is absent.

### The K3 incident, and a ceiling that was proposed and rejected

Mid-walk, K3 read **336 kΩ** closed — four orders of magnitude above the
neighbours — and **the suite passed**. I stopped the walk rather than continue,
and wrote a diagnostic designed to separate a marginal mechanical contact
(intermittent, wandering) from a degraded PhotoMOS (repeatable): six open/close
cycles printing the meter reading and the device's own `relay_state(3)` read-back
side by side, with a standing-DC-volts check first.

Two things to keep straight about what happened next. The retest came back clean
— six cycles at 31.5–40.1 Ω, overload on every open, read-back agreeing, standing
volts 0.0011 V — and I initially described the clip as having "re-seated," which
implies self-healing. The operator corrected that: *"i snugged the screw terminals
before your retest."* So the clean run is the **post-repair** state, not evidence
of recovery. The fault was real and persistent until a human fixed it.

Then I proposed adding a **~1 kΩ ceiling** on the closed reading, and argued F-27
did not forbid it because this case was categorically different. The operator
pushed back: *"Do we need a ceiling? In practice we won't be measuring the
relays... they'll be controlling something. So long as their resistance is low
enough to not drop voltage or generate heat, we don't care."*

That is right and my argument was weak, in a way worth recording because it is a
recurring failure shape:

- **Nothing false-passed.** The test's claim is that the relay's *state follows
  the command*. At 336 kΩ that claim was **true** — overload → finite is a real
  state change. A test is not holed by failing to catch something it never
  claimed.
- **"Categorically different" was special pleading.** A loose screw terminal *is*
  a wiring property. That puts it **inside** F-27's rule rather than justifying an
  exception to it — I was reaching for an exemption for the first case that
  actually stung.
- **The driver cannot see the thing a ceiling would police.** Bench wiring quality
  is invisible to it. A threshold there fails on the bench's condition while
  reading as a statement about the device.
- **In service these relays switch a load**, where the voltage drop and the
  dissipation are what matter — not whether the ohms land in a band.

The rejection is recorded in three places so nobody "fixes" it later: a **"Do not
add a ceiling on the closed reading"** comment block at the assertion itself, a
closing paragraph in F-27, and the CHANGELOG.

### What landed

**Docs and comments only — no production code, no assertions changed.** That is
the direct consequence of the ceiling decision, and it is the right shape for the
commit: eight positions of measurement that *confirmed* the existing assertions
rather than moving them.

- **F-26** — the eight-position table, the ±0.5 % rate note, and the
  measure-where-hypotheses-diverge lesson stated generally.
- **F-27** — the eight-relay table with per-position spreads, plus "A ceiling on
  the closed reading was considered and rejected."
- The module docstring's resistance paragraph, rewritten from two relays to
  eight; claim 5 rewritten to say all eight positions are measured.
- The `counted` fixture docstring — the `+ 4` now rests on three PORT B readings.
- CHANGELOG, and the "do not add a ceiling" block at the `hi < lo * 10` assertion.

Verified before committing: **10 passed / 2 skipped** at K7/PB3 after the edits,
the file still collecting hardware-free (12 deselected), ruff clean.

### Probe hygiene worth repeating

The step probes opened the device **directly** rather than through the agent,
specifically to avoid §15's interface-0-busy trap: an agent that has attached
holds interface 0 and the next direct-open run skips all 12 tests. Every probe
also ended in `finally: adu.reset_relays(); adu.close(); dmm.close()`, so an
exception mid-step could not leave a contact energised. Both scripts were removed
from host and board afterwards.

## 17. Addendum, 2026-08-27 — the watchdog trip, and a green test that could not support its own claim

Commit `091bb3f`. The operator authorised the gate in as many words — *"The bench is safe and only used for this development. Set the
watchdog."* — which is worth contrasting with what came before it. *"Let's do
B5"* named the **task**, and an attempt to set
`BENCHCTRL_ADU218_ARM_WATCHDOG=1` on the strength of that was correctly blocked:
naming a task is not authorising its gate. The second message named the flag and
the bench condition the flag asserts, so it cleared. This is the last bench item;
`SWEEP_ALL` remains unset.

### The interlock is real, and now measured

The device de-energises its own relays with **no benchctrl process, no GPIO and
no kernel driver in the decision path** — which is the whole reason the watchdog
matters, since it is the one safety mechanism on this bench that survives the
host dying. Armed at `WD2` (10 s nominal), witnessed by the SDM4065A across K7:

| observation | figure |
|---|---|
| readings after arming, all closed | 24, spanning **0.00 → 9.86 s** |
| spread across those 24 | 17.471 – 17.474 Ω (**3 milliohms**) |
| first open reading | 10.83 s, the overload sentinel |
| **the trip** | **(9.86, 10.83] s**, against a 10.0 s nominal |
| `read_watchdog()` after | `0` — self-cleared by the trip |
| `read_watchdog_tripped()` | `True`, and consumed |

The gap in the bracket is one meter read. Recorded in
`tests/fixtures/adu218/watchdog_trip.txt`, which now carries both brackets —
`WD1` at (0.90, 1.10] s from the earlier session, `WD2` here.

### The test passed, and that was not evidence

The committed test armed `WD1`, slept 1.5 s and asserted the contact was open. It
went green on the first run. It also could not distinguish the claim from its
useless opposite:

* **H1** — the relay opens when the timer **expires**. The claim.
* **H2** — the relay opens as a side effect of **arming at all**. This would make
  the interlock worse than absent: it would drop the load the moment it was
  enabled, and every test in the suite would still pass.

`WD1` structurally cannot separate them, and the reason is a number: **one DMM
read costs ~0.41 s and the whole window is 1 s.** There is no room to sample
*inside* the window, so "armed, waited, open" is the most the test can ever say,
and both hypotheses produce exactly that. This is the general trap worth naming:
**a timeout test whose window is shorter than its own measurement latency cannot
tell "fires on expiry" from "fires on arm".**

`WD2` moves the window to 10 s, where an early sample fits. The test now reads
the contact at ~2 s and asserts it is **still closed** — the coordinate where H1
and H2 disagree — before waiting out the rest of the window.

### A guard on the guard

The early sample is the entire discriminator, so it must land comfortably inside
the window. `assert elapsed < 8.0` runs first, because a slow VISA round trip
would push the sample past the timeout and **silently** degrade the test back
into the `WD1` form that proves nothing. The failure message says so, rather than
leaving the next reader to work out why an 8 appears.

### Proven in both directions

One mutant, two forms of the test, held **outside** the module per the mutation
evidence rules:

| form | H2 mutant (open the relay right after arming) |
|---|---|
| the new `WD2` test | **FAILS**, at the early assertion, message naming the cause, elapsed 3.26 s |
| the old `WD1` test (extracted with `git show HEAD:…`) | **PASSES** |

That is measured, not argued, and it is the whole justification for the change:
the coverage is new, not restated.

### What F-22 does and does not cover

Worth separating, because they read as the same thing. F-22 is about **reading**
a trip — `WD=0` means both "timed out" and "never enabled", so it is
interpretable only against a driver-held expectation. That ambiguity is
unchanged. What is new is the other half: **whether the trip happens at all**,
which is now witnessed on hardware independently of anything the device reports
about itself. F-22 gained the measurement and the bracket.

### Verification

Hardware **11 passed / 1 skipped** in 50.59 s (`BENCHCTRL_ADU218_INPUT=B3`), the
only skip being `SWEEP_ALL`. The watchdog test alone: 1 passed in **17.26 s**,
against 6.65 s for the `WD1` form — the runtime is itself evidence it really
waits the window out. Hardware-free still 12 deselected; `test_bench_adu218.py`
115 passed; ruff clean. Board copy md5-identical to the repo before the run, and
a 7-passed baseline established the witness worked *before* anything was armed.
The agent was confirmed not holding interface 0, so nothing could refeed the
timer. Both probe scripts removed from host and board; every run ended `WD0` with
all relays de-energised.

---

## 18. Addendum, 2026-08-27 — an accurate skip that silently un-ran three tests

Commits `4d3b2d1` (the fix) and `3ee021b` (the record). Found by sweeping for
leftovers *after* the PR was open, which is worth noting on its own: there was no
failing test, no error, and nothing in the diff pointing at it.

### What happened

Run the hardware file with the bench fully wired and **no environment variables
set at all**, which is how anyone else will run it:

```
7 passed, 5 skipped        # expected 10 passed, 2 skipped
```

Three input/counter tests had stopped running. `BENCHCTRL_ADU218_INPUT` still
defaulted to `B2` — where the generator sat *mid*-walk in §16 — and the walk had
ended on **PB3**. So the fixture skipped, correctly, with `PB2 is not toggling`.

### Why the skip being accurate is the defect, not the defence

§15 recorded, in as many words: *"A stale default skips with the line named, so it
cannot silently pass."* That is true and it is not enough. It closes the
**correctness** half — a stale default cannot assert something false — and leaves
the **coverage** half wide open. Nothing was red. Nothing was misconfigured.

The message is the reason it hid. **"PB2 is not toggling" is equally true when
the generator is unplugged and when it is one terminal over**, and only the
second was the case. A skip whose text cannot distinguish "moved" from "absent"
reads as a *bench fact* — "no generator today, fine" — rather than as a default
nobody moved. So the more accurately it described PB2, the more convincingly it
argued for being ignored.

### The general shape

A default that tracks a **physical bench position** (`INPUT` → a terminal,
`RELAY` → K7) goes stale *every time the bench moves*, which on this project was
eight times in one afternoon. Such a default needs a diagnostic that separates
those two cases, or it degrades into a green run with less coverage than it
claims. That is a different requirement from "fail rather than false-pass", and
having satisfied the second is not evidence about the first.

### Fixed two ways

1. **The default is now `B3`, found by discovery rather than assumption.** A probe
   sampled all eight lines for 6 s and reported the observed level set per line;
   exactly one showed `{False, True}`. Reading the walk's notes would have given
   the same answer, but sampling is what makes it a measurement — and the whole
   failure here came from trusting a written-down position.
2. **The skip now names the line that IS toggling.** The fixture sweeps the whole
   port (the device is already open, so it costs one extra pass) and says
   `…but PB3 is toggling, so set BENCHCTRL_ADU218_INPUT to that (or update the
   default)`, or, when nothing moves, `no input line is toggling, so the generator
   is off or unwired`. The next drift diagnoses itself.

Proven by pointing `INPUT` at an undriven `A1`: the skip fires and names PB3.

### Two stale docstrings found in the same pass

- The module docstring still said the watchdog trip had been *"bisected by hand
  into a fixture"* — true before §17, and §17 is precisely what made it false. It
  now quotes the measured bracket, because the trip is asserted by a committed
  test rather than by a note about a past session.
- The counter docstring said the `+ 4` PORT B offset *"rests on PB2"*. After the
  walk it rests on **three** independent readings (PB0 → 4, PB2 → 6, PB3 → 7).
  Understating the evidence invites someone to re-run the weakest version of it.

### Verification

**10 passed / 2 skipped with no environment variables** (was 7/5), the two skips
being the operator gates; **11 passed / 1 skipped** with the watchdog armed;
hardware-free 12 deselected; `test_bench_adu218.py` 115 passed; ruff clean; board
`IN SYNC`, 105 files identical. Recorded in the CHANGELOG — which had to
**correct its own earlier claim** — and in F-26. The discovery probe was removed
from host and board.

## 19. Addendum, 2026-08-27 — "simultaneous" meant two things, and B1 needs neither instrument

Rick asked the question this plan should have answered in writing already:

> *"I'm not clear on what B1 needs. Do we want to try toggling all the relays at
> the same time instead of individually? If we want to measure that I'll have to
> get the logic analyzer out."*

An offer of real hardware effort deserves a precise answer, and producing one
exposed that the repo used the word **simultaneous** in six docstrings without
ever saying which of two claims it was making.

### What B1 is, and why no instrument appears in it

`test_all_eight_relays_switch_on_the_real_device` already does **both** things.
It walks the eight relays individually (`SKn`/`RKn`, each cross-checked against
`relay_mask()`), *then* writes the two whole-port masks `0b10101010` and
`0b01010101` via `set_relay_port`, cross-checking each the same way, then
`reset_relays()`. So "individually **instead of** together" is a false choice —
it is not either.

Its check is the device's own `PK` read-back and deliberately **not** a meter,
for the reason its docstring gives: the bench has one DMM and it is across
`TEST_RELAY`, so seven of the eight cannot be independently witnessed. That
makes B1 *weaker* than the bench walk on every relay it covers, and its only
unique contribution is that the device **accepts a whole-port mask and lands all
eight bits where the mask says** — a state claim.

### The distinction that was missing

`driver.py`'s *"one `MKddd` is a single simultaneous transition"* is a claim
about the **command**, not about the contacts:

- eight `SKn`/`RKn` writes are eight USB transfers, so the port demonstrably
  visits `0b10101000` on the way to `0b10101010`
- one `MKddd` is one transfer, so it does not

That is load-bearing — it is exactly why `set_relay_port` enforces the allowlist
on the **whole mask** rather than on the diff, while `set_relay_state` can get
away with checking only the energising direction.

What it is **not** is a claim about contact-to-contact skew *inside* that one
command. Verification is a `PK` read of the landed state, which cannot see
timing at all. The wording was close enough to a timing guarantee that someone
would eventually cite it for make-before-break ordering, so the driver, the MCP
tool description (the caller most likely to over-read it) and `docs/drivers.md`
now all say the skew is unmeasured and unclaimed.

### Why the logic analyzer was declined

The stronger claim — that the eight contacts move within some skew of each other
— is one **nothing in the repo currently makes**, and two things argue against
buying it now:

1. **There is no number to check against.** The manual's Relay Outputs table
   gives ratings, on-state resistance and the part (Panasonic AQZ207 PhotoMOS)
   but **no switching time**, per-relay or port-wide. Any skew measured would be
   a recorded observation with no pass/fail.
2. These are **solid-state** relays, so the spread is opto-coupler turn-on, very
   likely dominated by the USB HID report boundary rather than by anything the
   choice of `MKddd` vs `SKn` controls.

Recommended instead, if the claim is ever wanted: two DMM/scope channels across
two relays on one `MKddd`. Same question, no new instrument.

**B1's actual blocker is unchanged and is not a measurement.**
`BENCHCTRL_ADU218_SWEEP_ALL=1` energises all eight outputs, and by the flag's
own skip message that is the operator stating they know what is attached to each
one. Still Rick's to set.

### Two vendor specs recovered while looking for a switching time

Neither was anywhere in the repo.

**`RELAY_MAX_SWITCH_HZ = 1.0`** — the spec table's *"1 CPS at full load"* plus
the CAUTION beside it: *"Power dissipation of PhotoMOS relays increases with
switching speed... The ADU218 is not recommended for PWM applications."*
Documented and **deliberately not enforced**: the figure is qualified *at full
load*, and nothing in USB, HID or the ADU command set reports what a contact is
switching, so a limiter would throttle the dry-contact sweeps that are most of
this bench's use on the strength of a condition it cannot observe. Worth
knowing that it **inverts the usual expectation** — the ADU208's *mechanical*
relays manage 10 CPS, so the solid-state part is the slower one to cycle.

**On-state resistance, 700 mΩ typical / 1.1 Ω maximum.** This corroborates F-27
*independently of our own bench*, which matters because F-27's wiring conclusion
previously rested on "the spread has no relation to index" — an inference from
one bench. Every reading in the eight-relay walk is **15× to 41× the vendor
maximum**, so by the manufacturer's own number the relay accounts for under 4 %
of even the **lowest** measurement. The leads dominate by construction, so a
threshold anywhere in the measured range would have been a threshold on lead
resistance with the relay spec two orders of magnitude beneath it.

### The test for the unenforced limit needed a clock

First version: eight `set_relay_port` writes, each read back. It passed — and it
**also passed against a `sleep(1.0 / RELAY_MAX_SWITCH_HZ)` mutant** in the write
path, which is precisely the change it exists to forbid. A throttle has two
shapes and only one of them raises: a limiter that *rejects* the fast call is
caught by a success assertion, while one that *sleeps* to pace it leaves every
state assertion passing, just slowly.

Fixed by asserting `elapsed < 2.0`, chosen from the two hypotheses rather than as
a round number — eight transitions at 1 CPS is ~7 s of pacing against
microseconds on the simulated link, so the ceiling sits an order of magnitude
clear of **both**. The mutant, held outside the module, now fails it at
**8.00 s**, with **1 test collected and run** (an rc=0 with nothing selected
would have read as a pass).

### Verification

`test_bench_adu218.py` **116 passed**; the new test collected under `TestRelays`
(it was first written into `TestDebounce`, which is the wrong class for a relay
claim); mutant kills it at 8.00 s vs the 2.0 s ceiling and the module was
restored and re-checked for the mutant by grep; ruff on the changed files shows
only the pre-existing `UP045`/`UP037` `Optional`-annotation baseline, and these
edits add no annotations. Commits `bdaa835` (code, tests, docs, F-27) and
`9ca2285` (CHANGELOG), both pushed. **No behaviour change** — the driver sends
exactly what it sent before.

Then the whole tree, since two *deployed* files changed: **2446 hardware-free
passed / 6 skipped**, 213 deselected, in 21 m 40 s. The baseline was 2444/6, and
the +2 is this session's own additions (`test_bench_adu218.py` went 115 → 116) —
worth checking rather than assuming, because the sibling constants
`COUNTER_MAX_FREQUENCY_HZ` and `DEBOUNCE_MS` are referenced from three files, so
a new exported constant is not automatically inert. Skips unchanged at 6.

The board was then re-synced (`--check` had correctly gone out of sync on exactly
`driver.py` and `mcp_tools.py`, and nothing else) and is **IN SYNC — 105 files
identical**. On the board: **10 passed / 2 skipped** with no env vars, and
**11 passed / 1 skipped** with the watchdog armed, the last skip being
`SWEEP_ALL`. Device left safe and read back to prove it, not assumed: relay mask
`0b00000000`, `WD` 0, de-bounce 1.0 ms. No agent restart was needed — driver
modules re-import lazily, unlike a core module.

---

## 20. Addendum, 2026-08-27 — B1 ran, and it had been checking the device against itself

The operator removed the last gate in as many words: *"again, the bench is yours
for this work. there is no risk to anything so you can remove these gates. you
are free to sweep all relays as much and in any way you want."* That **satisfies**
`BENCHCTRL_ADU218_SWEEP_ALL=1`, so the all-eight sweep ran for the first time,
and it **passed**.

**The gate was kept, not removed** — and that is a deliberate deviation from what
was offered, stated rather than done quietly. This bench is safe and its operator
says so, which is exactly what the flag is *for*: the skip message defines it as
the operator stating they know what is attached to all eight outputs. But the
skip is the repo's contract with the **next** bench, where the answer may be
different and where nobody will be reading this file. So it was satisfied for
this session and left standing for the next. Verified in both directions: **13
passed / 0 skipped** on the ADU218 file with both gates set, and the gate still
skipping with the line named when they are unset.

### What running it found

The sweep's two whole-port mask writes were checked **only against
`relay_mask()`** — the device reporting on itself. A firmware that accepted
`MKddd`, updated its own state word and never moved a contact satisfied that
check completely. §19 had just finished establishing that the whole-port command
being indivisible is load-bearing (it is the entire basis for `set_relay_port`
enforcing the allowlist on the whole mask rather than on the diff), and the
per-relay `SKn`/`RKn` path has had an independent instrument witness since the
first bench session. The whole-port path had **none**.

**The bench closes it for free, and this is the whole trick.** The two masks the
sweep already writes, `0b10101010` and `0b01010101`, are **complements** — so
whichever relay the single meter is clamped across, one mask closes it and the
other opens it. With `TEST_RELAY = 7` the leading bit, `0b10101010` closes K7 and
`0b01010101` opens it. One witnessed relay out of eight is still the stated
limit, but it is the difference between *"the command path is asserted"* and
*"the command path is measured"*, and it is the same code path for all eight.

`test_a_whole_port_mask_moves_a_contact_an_instrument_can_see` asserts the
complement property (`0b10101010 ^ 0b01010101 == 0xFF`) rather than trusting it,
because a later edit to the sweep's two masks — to a pair that are *not*
complements — would silently reduce the test to witnessing one state twice while
still passing. The **ordering is deliberate**: the mask that *opens* the
witnessed relay is sent while seven others close, which is the reading a
state-word-only bug cannot fake. It has to open a contact the driver never named
individually, on the strength of a bit position inside a three-digit decimal
argument.

### Three mutation attempts, and the first two are the instructive part

1. **A bit-reversal mutant *was* killed — at the wrong assertion.** It failed on
   the **pre-existing read-back** (`assert 85 == 170`), not at the meter. Two
   guards catching one fixture: that kill credited the new test with nothing, and
   stopping there would have meant claiming a witness that had not been
   demonstrated.
2. **Two further mutants leaked through `reset_relays()`**, whose `MK000` takes
   its own `_send` path. A cached mask never cleared, so the **sweep** failed for
   the wrong reason (`ADU218ProtocolError: reset_relays() commanded MK000 but the
   port reads back 0b01010101`) and a one-shot variant was instead consumed by
   `set_relay_port`'s own internal verify read. A mutant that breaks the safe
   state is not a clean discriminator.
3. **The discriminating mutant** swallows only a *nonzero* `MKddd` at the `_send`
   seam while `relay_mask()` returns the commanded value. `MK000` really sends
   and clears the cache, so the safe state stays genuinely reachable and **no
   read-back anywhere can see the lie**. Against it the **existing sweep passes**
   and the **witnessed test fails**, on its own DMM assertion, naming the cause.
   That is the evidence the two tests are distinguishable rather than redundant.

### Verification

Driver restored from a copy held **outside** the module, re-grepped for `MUTANT`
(0 hits), md5 matched against the repo copy
(`b0271d967d72252df708c1a238fe2599`), board **IN SYNC — 105 files identical**.
Hardware: **13 passed / 0 skipped** on the ADU218 file with both gates set,
**16 passed / 3 skipped** across both hardware suites (the 3 being CP2112's
deliberately-undefaulted `BENCHCTRL_CP2112_LINE`, which this authorisation does
not cover — it needs the meter physically moved), and **10 passed / 3 skipped**
with the gates unset. Hardware-free is unaffected: the file collects **13
skipped**, `test_bench_adu218.py` **116 passed**, ruff clean. Device left safe
and **read back** to prove it: relay mask `0b00000000`, `WD` 0, de-bounce
1.0 ms. Commit `90e18f5`, pushed; PR #1 at **46 commits, OPEN and MERGEABLE**.

Docs carry it in three places, each for a different reader: the sweep's own
docstring now says its mask writes **are** witnessed in the test that follows and
warns against deleting them as redundant; `docs/drivers.md` separates what is
**established** (that a `MKddd` reaches the contacts) from what is **not**
(contact-to-contact timing, still unmeasured, and only one relay of eight is
metered); the CHANGELOG carries the mutation history including the two failed
attempts.

### The numbers, because a categorical pass hides its own margin

Every other bench claim in this driver has numbers on file — F-26's
eight-position counter table, F-27's eight-relay resistance table,
`watchdog_trip.txt`'s brackets. This one had only prose, and that is a real gap
rather than a tidiness complaint: the test asserts **categorically** (finite
versus the overload sentinel, which F-27 requires, since the closed value is a
property of the wiring), and a categorical pass does not show how far from the
boundary it sat. Captured in `tests/fixtures/adu218/whole_port_witness.txt`:

| step | mask | bit 7 | DMM |
|---|---|---|---|
| `reset_relays` | `0b00000000` | — | OPEN (overload) |
| `set_relay_port` `MK170` | `0b10101010` | 1 | CLOSED 17.5134 Ω |
| `set_relay_port` `MK085` | `0b01010101` | 0 | OPEN (overload) |

repeated three times (17.5134 / 17.5147 / 17.5152 Ω, spread **0.0018 Ω**), with
standing DC at 0.0006 V confirming the leads were on a dry contact, and the
device left at mask `0b00000000` / `WD` 0.

**And the numbers turned out to corroborate something the test cannot.** The
bench walk (§16, F-27) measured K7 at **17.50 Ω** through the *per-relay* `SKn`
path. `MKddd` puts the same contact at 17.5134–17.5152 Ω. Two independent command
paths, one contact, the same resistance to four decimal places — so `MKddd` is
closing the relay the way `SKn` does, not by some other route that merely ends up
*reported* as closed. That is a second, quantitative answer to the same question
the categorical assertion answers, arrived at differently. It also cuts the other
way as a check on §16: had the walk's eight figures been an artefact of the
command route rather than of the wiring, the two paths would not agree like that.

None of which becomes an assertion. 17.5 Ω is still a property of K7's leads and
would break the moment they move — which is exactly why the numbers live in a
fixture and F-27 gets them as corroboration rather than as a threshold.

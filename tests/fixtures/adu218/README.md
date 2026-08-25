# ADU218 Stage 0 device transcripts

Verbatim captures from the real device, taken before any driver code was
written. Per `AGENTS.md`: a simulator built from the same misreading of a
manual as the driver agrees with the driver and proves nothing. These
transcripts are what the simulator must replay, so it replays the *device*
rather than our reading of the PDF.

## Provenance

| Item | Value |
|---|---|
| Device | Ontrak Control Systems ADU218 USB Relay I/O Interface |
| USB ID | `0a07:00da` |
| USB serial | `E02246` |
| Enumeration | USB 1.10, **Low Speed**, `bInterfaceClass 03` (HID), **no kernel driver bound** |
| Endpoints | 8-byte interrupt: EP `0x01` OUT, EP `0x81` IN, `bInterval` 10 |
| Host | Arduino Uno Q, kernel 6.16.7-g0dd6551ae96b, Python 3.13.5 |
| Access path | raw `USBDEVFS` ioctls via stdlib `fcntl` + `ctypes` — **no pyusb, no hidapi, no pyserial** |

### Why no driver binds, and why that is not luck

Worth stating precisely, because the obvious reading is wrong. `usbhid` is
built into this kernel and *did* bind the USB keyboard on the same bus, so this
is not the `ch341` situation of a missing module (`KNOWN_LIMITATIONS.md` § N-6).

The interface is unclaimed because upstream Linux **deliberately ignores** it:
`drivers/hid/hid-quirks.c` lists `0a07:0x0064`, `+20`, `+30`, `+100`, `+108`,
`+118`, `+200`…`+500` in `hid_ignore_list`, and `hid-ids.h` defines
`USB_VENDOR_ID_ONTRAK 0x0a07` / `USB_DEVICE_ID_ONTRAK_ADU100 0x0064`. Our PID
`0x00da` is `0x0064 + 118` — the ADU218 entry. HID core is told to keep away
because these devices are not really HID; the report descriptor is a wrapper
around a private ASCII protocol.

Two consequences, both load-bearing:

- **`CLAIMINTERFACE` will always succeed** on any mainline kernel, with no
  driver to detach first. The zero-dependency userspace path is not a local
  accident of this board, and it will not break when a kernel is updated.
- **A kernel driver for this device does exist**: `drivers/usb/misc/adutux.c`
  (`CONFIG_USB_ADUTUX`) binds the same six PIDs including `0x0064+118`
  `/* ADU218 */` and exposes `/dev/usb/adutuxN`. It is **not** enabled on this
  board (nor on the WSL host: `# CONFIG_USB_ADUTUX is not set`), so it is not
  the path we take — but it is why the driver's transport seam matters. Its
  `write()` copies the user buffer to the interrupt endpoint verbatim and its
  `read()` returns the payload unmodified, so the framing below is identical on
  both routes. See `framing.txt`. Note `adutux` enforces exclusive open
  (`-EBUSY`), which is a stronger writer guarantee than usbfs gives us.
| Capture date | 2026-08-25 |
| Witness DMM | Siglent SDM4065A, s/n `SDM46A0CA00021`, firmware `0.0.0.20` |
| Wiring | ADU218 relay **K0** load side → SDM4065A input leads. Nothing else attached. |

## Files

- `framing.txt` — proof that the `0x01` report prefix is mandatory
- `reads.txt` — every read-only command, with the exact 8-byte response
- `errors.txt` — invalid and out-of-range commands
- `switch_k0.txt` — two full K0 close/open cycles, each corroborated by the DMM
- `watchdog.txt` — the hardware watchdog opening a relay on host silence, with
  a fed-timer control; also `MKddd`, the counters, and `DBn`. **Carries a
  correction: its "3.7 s" is an observation latency, not a trip time.**
- `watchdog_trip.txt` — the WD1 trip time measured properly, by bisecting the
  silence window: **(0.90, 1.10] s**, i.e. the documented 1 s
- `on_resistance.txt` — characterisation of the closed-contact reading

## Wire format, as measured

Commands are ASCII, case-insensitive, in an 8-byte packet:

```
byte 0    = 0x01          report prefix — MANDATORY, and specifically 0x01
bytes 1.. = ASCII command, NUL-padded to 8
```

Responses arrive on EP `0x81`, also 8 bytes, same shape: byte 0 is `0x01`,
then the ASCII payload, NUL-terminated and NUL-padded.

The prefix was measured, not assumed — `framing.txt` shows bare ASCII, `0x00`
and `0x02` are all silently ignored while `0x01` answers. A test asserting only
that byte 0 is non-printable would pass for two encodings the device rejects.

The measurement turns out to be a documented rule rather than a local quirk, and
knowing why raises confidence that it will hold: the byte is a **report ID**
selecting a pipe — `0x01` Device (ASCII commands), `0x02` RS232, `0x03`
Streaming. §4 of the manual states the ADU208/ADU218 "does not use the RS232 or
Stream pipes", so there is no hardware for a `0x02` packet to reach, which is
exactly why it is *silently* ignored rather than refused. The vendor gives a
byte-exact ADU218 example matching this framing in both directions:

```
01h 52h 45h 32h 00h 00h 00h 00h    "RE2" to the ADU218
01h 31h 30h 34h 34h 39h 00h 00h    count "10449" back to the host
```

Two consequences: never emit `0x02`/`0x03`, and the ASCII payload budget is
**7 bytes** (prefix + payload + NUL padding = 8). The longest documented command
is `MK255` at 5, so a `>7` rejection is a cheap validator with real headroom.
Commands are case-insensitive.

Response payload widths are **fixed per command** and match the manual. The last
column records how well the manual backs each one: six are *stated*, four appear
only in an example — for those four the measurement is the stronger evidence.

| Command | Width | Meaning | In manual |
|---|---|---|---|
| `PK` | 3 | PORT K as decimal `000`–`255` | stated §6a |
| `RPKn` | 1 | one relay, `0` or `1` | example only §6a |
| `Py` (`PA`/`PB`) | 2 | 4-bit input port as decimal `00`–`15` | stated §6b |
| `RPy` | 4 | 4-bit input port in binary, MSB-first | stated §6b |
| `RPyn` | 1 | one input line | example only §6b |
| `PI` | 3 | both input ports, decimal `000`–`255`; PORT A low nibble | stated §6b |
| `REn` / `RCn` | 5 | 16-bit event counter, `00000`–`65535` | stated §6c |
| `DB` | 1 | de-bounce setting | example only §6c |
| `WD` | 1 | watchdog setting | example only §6d |

The `PK`=3 vs `Py`=2 asymmetry is fully explained by the port widths: PORT K is
8 bits (0–255, three digits), PORT A/B are 4 bits (0–15, two digits), and `PI`
needs three because it packs both nibbles into one byte. Values are zero-padded.
**Parse by strip-at-first-NUL rather than fixed slicing** — free, and it survives
either padding style.

Bit weighting, for the record: in `PK`, bit *n* is relay K*n* with LSB = K0
(§6a's example gives `128` = "K7 is closed… K0-K6 are open"). `RPKn` returns
`0` = open, `1` = closed. `RPy` is MSB-first, so `char[0]` = PA3 … `char[3]` =
PA0. In `PI`, PORT A is the low nibble.

Counters: 16-bit, count low-to-high transitions, and **roll over** 65535 → 00000
with no documented overflow flag — so zero counts and exactly 65536 counts are
indistinguishable. The counter-to-input map is in a Table 1 *image* that text
extraction drops entirely; read from the image, it is counters 0–3 → PA0–PA3 and
counters 4–7 → PB0–PB3, corroborated by the manual's `RE1`→"PA1" and
`RC3`→"PA3" examples. There is **no clear-without-read command**: zeroing a
counter requires accepting its value via `RCn`.

## Ratings, as written in the manual's specifications table

Relevant because the driver's safety posture should be sized to the real device,
and because two of these differ from the vendor's web page.

| Spec | ADU218 (this device) |
|---|---|
| Relay type | Panasonic **AQZ207 PhotoMOS**, form A (N.O.), solid-state |
| AC rating | **1 A @ 120 VAC** |
| DC rating | **1 A @ 120 VDC** (the manual's DC row reads "120VAC" — a typo; the web page gives 120 VDC) |
| On-state R | 700 mΩ typ / **1.1 Ω max** — conditions omitted here; the part datasheet specifies them at `IL = 1.0 A` (see finding 4) |
| Max switching speed | **1 CPS at full load** — but Panasonic's own limit for the part is **0.5 cps** (see below) |
| Isolation | 2500 Vrms (manual) — the web page says 3500 V / 500 V channel-to-channel |
| Safety certification | **primary insulation ONLY** |

Two things to carry into the driver and its docs:

- **`§6a` caution, verbatim:** *"At full-load rating, the maximum recommended
  switching speed is 1 CPS. The ADU218 is not recommended for PWM applications.
  Recommended switching speed can be safely exceeded only for applications
  operating at 20% or less of rated current."* The limit is therefore
  load-dependent — 1 Hz at 1 A, effectively unbounded at ≤200 mA — and a driver
  cannot enforce it without knowing the load. That makes it a
  `KNOWN_LIMITATIONS.md` entry, not a code check.

  **Ontrak's 1 CPS is twice the part maker's limit.** Panasonic
  `ASCTB467E` gives max operating frequency as **0.5 cps** for the AQZ207, at
  `IF = 10 mA, duty = 50 %, IL = Max., VL = Max.` Both figures claim to be "at
  full load", so they conflict. Ontrak may be derating differently at 120 V
  against the part's 200 V rating; that is a guess and is not resolved here.
  **If the driver docs ever state a switching-rate ceiling, use 0.5 CPS** — it is
  the more conservative and it comes from the component manufacturer rather than
  the integrator.
- **`§2` caution, verbatim:** *"The ADU218 provides CSA/UL EN60950-1 2nd edition
  safety certification for **primary insulation only**. For applications using
  an ADU218 requiring double insulation, additional protection should be provided
  by user in end application."* The sibling ADU208 is double-insulated; this one
  is not. Worth stating in the driver docs because it is a property of the model
  on the bench, not something an operator would infer.

Manual-vs-web conflicts are recorded rather than silently resolved: operating
temperature is −25/85 °C in the manual and 0–50 °C on the web page; isolation
differs as above; de-bounce options differ (see below).

## Five findings that would each have been a driver bug

1. **`RI` does not exist.** The manual's command summary (§5) lists `RI` to
   read both input ports. The command *description* (§6b) calls the same thing
   `PI`, in four places (prose, command line, and both examples). The device
   answers `PI` and **times out on `RI`** — the summary table is wrong. A driver
   written from the summary would hang on every input read.

   Both spellings really are in the PDF: `RI` appears exactly once (§5, p10),
   `PI` four times (§6b, p13), confirmed against raw non-layout text extraction
   so it is not a `pdftotext` artifact. So this is a genuine internal
   contradiction in the vendor document that the hardware resolves — **cite §6b
   in code, never §5.** `PI` also fits the `P`-prefix decimal-read family
   (`PK`, `PA`, `PB`, `PI`), which is weak corroboration on its own.

2. **PORT A and PORT B are 4 bits each, not 8.** Eight digital inputs total,
   split across two isolated 4-bit ports (`n = 0..3` per port). This is why
   `PK` returns three digits but `PA` returns two. Indexing inputs 0–7
   against a single port is wrong.

3. **An invalid command returns nothing at all** — no error string, no
   sentinel. The read simply times out (`ETIMEDOUT`/110). So the driver must
   know, per command, whether a response is expected; it cannot discover this
   at runtime. The mitigating half of the finding: the session stays healthy,
   and the very next valid command answers normally. This is *unlike* the
   SDM4065A, where a query on an undefined header can strand the bulk
   endpoints until a power cycle (`KNOWN_LIMITATIONS.md` § F-8).

   The manual has no error-response section at all — no sentinel, no NAK, no
   status register — so silence is not documented *as* the error behaviour, but
   it is what the architecture implies, and Ontrak states the general rule:
   "The ADU will not send data to the host computer unless requested." The
   survival half **is** a documented rule rather than luck: §6d says invalid
   commands are received and parsed (they reset the watchdog) and then discarded
   without side effect. That is why the next valid command answers — and, less
   comfortably, why an invalid command still feeds the deadman.

4. **Measured on-resistance is ~6.14 Ω, not the datasheet's 700 mΩ typical /
   1.1 Ω maximum** (specifications table of the vendor manual). Eight repeat
   readings spread 0.98 mΩ, so this is a stable systematic offset and not
   noise. It is a 2-wire measurement, so it includes lead and contact
   resistance — but § H-5 bounds this bench's lead error at ~79 mΩ, two
   orders of magnitude too small to explain a 5 Ω discrepancy. Cause not yet
   established; see `on_resistance.txt`. **The driver must not treat the
   datasheet on-resistance as a validation threshold** — an open/closed check
   should key on the DMM's overload sentinel, where the margin is effectively
   infinite, not on a resistance limit.

   Four candidate mechanisms were checked against the ADU218 manual — series
   protection resistance, a current-sense element, a different specified
   measurement condition, and PhotoMOS R_on being specified at a load current
   far above a DMM's ohmmeter drive — and the manual is **silent on all four**.
   The entire manual contains four resistance-related lines: two input-impedance
   figures (2700 Ω) and the two on-state numbers, and the on-state numbers are
   given **with no test conditions at all**. So the manual offers nothing to
   compare against.

   **The part datasheet supplies the missing conditions, and they invalidate the
   comparison.** Panasonic `ASCTB467E` (PhotoMOS Power SIL 1 Form A family
   catalogue; AQZ207 has no standalone datasheet) specifies on-resistance in a
   table with an explicit Condition column: **`IF = 10 mA`, `IL = Max.`,
   `Within 1 s`**. For the AQZ207, `IL = Max.` resolves to **1.0 A** from the
   absolute-maximum-ratings row. Ontrak's 700 mΩ / 1.1 Ω are copied verbatim out
   of that table — correct column, correct part — **but reprinted with the
   conditions stripped.**

   So the 1.1 Ω maximum is a short-pulse figure at **1.0 A**, and this bench
   measured at roughly **1 mA**: three orders of magnitude below the specified
   envelope. "6.14 Ω vs 1.1 Ω max" was never like-for-like, and nothing in the
   ADU218 manual lets a reader discover that. Consequences for the driver:

   - **This unit is not "out of spec."** There is no datasheet limit a 1 mA
     reading can violate — nor one it can satisfy. Recording it as a fault would
     be wrong.
   - **Nor is it explained.** The tempting story — R_on rises steeply as load
     current falls — is *not* in the datasheet. There is no R_on-vs-load-current
     curve anywhere in it (R_on is plotted only against ambient temperature),
     and the one lower-current point it does give trends the wrong way for that
     story: graph 3-4 is taken at **0.4 A** and reads ≈0.65 Ω at 25 °C, slightly
     *below* the 0.7 Ω typical quoted at 1.0 A. The I-V curve (graph 9-2) is a
     straight line through the origin with no knee, but at ±4 A full scale it
     cannot resolve 1 mA either way. Temperature is the only R_on modifier the
     datasheet quantifies, and it bounds that at ~1.5x over 25→85 °C — nowhere
     near the 5.44 Ω unaccounted for (3.07 Ω per die across the two series
     MOSFETs, against 0.35 Ω per die implied by the typical).
   - **The two-MOSFET topology does not add a factor.** The AC/DC variants
     (AQZ20x) really are two MOSFETs in anti-series — visible in the schematic
     and quantitatively in the ~2x R_on ratio against each DC-only sibling
     (AQZ107 0.34 Ω → AQZ207 0.7 Ω). That doubling is **already inside** the
     published 0.7 Ω. Do not double it again.
   - **The driver must not report or imply a contact-resistance figure**, because
     no in-spec figure exists at any current the driver could know about. An
     open/closed check keys on the DMM's overload sentinel.

   **Status: still unexplained — but for a stated reason rather than a missing
   document.** The discriminating experiment is a measurement at the datasheet's
   own condition (1.0 A, expecting ≤1.1 Ω; or 0.4 A for direct comparison with
   graph 3-4). That energises a 120 V-rated relay into a real load, so it is an
   operator decision, not something to slip into a probe script. See
   `on_resistance.txt`.

5. **A queued response outlives the command that caused it.** Interrupt-IN
   replies sit on EP `0x81` until read, so a driver that skips a read (or
   fails one) leaves an answer behind, and the *next* query returns the
   *previous* command's value — a silent wrong answer, not an error. This is
   not hypothetical: it invalidated the first framing measurement here, which
   credited a bare-ASCII write with a reply that belonged to the prefixed
   command before it. The driver must own the endpoint's read/write pairing
   strictly, and should drain on open. See `framing.txt`.

   Ontrak documents both the cause and the mitigation, and adds a depth figure
   that measurement here did not establish: there are "**three buffers per
   device** in the USB core", so a skipped read can leave *more than one*
   reading stale — the driver cannot assume a single stale frame and drain one.
   Their guidance is "read the response immediately after writing a query
   command", and their own Python example drains at connect: "you may want to
   read until there is nothing left to read from the device… to clear any
   pending reads that was not initiated by us." Two consequences: drain in
   `open()` (a stale reply **survives a process restart**, so session N can read
   session N−1's answers), and serialise strictly write→read under a lock rather
   than pipelining.

## And one finding that is an opportunity, not a bug

**The hardware watchdog is real, and it is a deadman that needs no software
running.** Armed with `WD1`, K0 opened on host silence — witnessed by the DMM,
not just by the device's own `RPK0` — and the fed-timer control held it closed
for 3.1 s, so the drop is caused by silence rather than by arming. `WD` also
self-clears to `0`, which is the only trace a host can use to learn a timeout
happened (with a caveat — see below).

The trip time is **(0.90, 1.10] s** for `WD1`, matching the documented 1 s.
That figure comes from `watchdog_trip.txt`, which bisects the silence window;
`watchdog.txt`'s "3.7 s" was `sleep(3.0)` plus a DMM read and timed the
*observation*, not the trip. The ladder is bounded: `WD0` off, `WD1` 1 s,
`WD2` 10 s, `WD3` 1 min, `n` constrained to 0..3, and `WDn` sets the interval
*and* arms in one command.

Two qualifications that make the feature harder to use safely than it first
appears, both of which belong in the driver's design rather than a footnote:

- **`WD`=0 is ambiguous.** It means "timed out" *and* "never enabled". So the
  self-clear is only a usable trace against a driver-held expected value, and a
  driver restart loses that — the first `WD` read after a restart cannot be
  interpreted. Write `WD0` at `open()` unless using the watchdog.
- **A status poller silently neuters it.** Any command refeeds the timer, so a
  health-check loop reading `PK` keeps the deadman fed however wedged the
  control path is. The control case above did exactly this, deliberately. The
  feed must therefore live on the *control* path only, and the driver must
  expose no background feeder.

This matters beyond the driver: `KNOWN_LIMITATIONS.md` § N-1 says a software
deadman cannot guarantee an output goes off, and `ROADMAP.md`'s "Hardware
interlock for unattended runs" is open work. The relay opens because the host
went *quiet*, so a wedged agent, a killed process, an unplugged cable and a
panicking kernel all de-energise the load. See `watchdog.txt` for the four
design consequences, chiefly that arming the watchdog makes every relay's state
depend on call frequency and must therefore never be implicit.

## What the manual adds that measurement could not

Some things cannot be established by probing a load-switching device, because
the probe is the risk. From the vendor manual and Ontrak's Linux pages, verified
against the captures above:

**Nothing here writes non-volatile memory.** The manual never mentions EEPROM,
flash or NVM in any form; all 15 commands touch volatile I/O state or timer
settings, and every setter has a matching getter. So there is **no documented
command that can permanently alter this device's configuration** — a reassuring
property for a switch, and the reason no capture below needed a restore beyond
re-issuing defaults. Whether `WDn`/`DBn` survive a power cycle is *not*
specified; `DBn`'s documented 1 ms default hints at volatile, but that is
inference.

**No mode-latch exists.** There is no analogue of the CyberPower PDU's
`menumode` — no command changes protocol mode, is one-way, or is documented to
need a power cycle. The nearest thing to a mode is the report-ID byte, and
`0x02`/`0x03` are inert on this model.

**Four things not to send, none of which the captures cover:**

1. **`WD4` and above** — behaviour unspecified; `n` is bounded 0..3.
2. **Out-of-range parameters, `MK300` especially** — the manual is silent on
   whether out-of-range values are ignored, clamped, or wrapped. `MK300`
   exceeds one byte, and an *aliased* whole-port write could close relays that
   were never requested. Validate host-side; never let the device arbitrate.
3. **`RCn` inside any retry wrapper** — it is the only responsive command that
   mutates state. If the reply is lost *after* the device cleared, a retry
   returns a fresh count and the original is gone permanently. Prefer `REn`
   plus host-side differencing wherever the count matters.
4. **`'s'` as byte 0** — it appears in Ontrak's `adutux` docs as a serial-number
   request, but it is a *driver-layer* pseudo-command with "no physical I/O".
   Over USBDEVFS it is an undefined report ID to the firmware. Take the serial
   from the USB descriptor instead (`E02246`).

**Two upstream errors worth not copying:**

- **Ontrak's own Python sample has the relay comments swapped** (`'RK0' # set
  relay 0`, `'SK0' # reset relay 0`). The manual is authoritative and says the
  opposite: `SK4` *closes* K4, `RK3` *opens* K3. S=Set=close, R=Reset=open,
  which is what `switch_k0.txt` measures.
- **The web page lists four de-bounce options** ("10ms, 1ms, 100us, NONE") while
  the manual documents three and bounds `n` to 0..2 — and the same four-option
  string appears verbatim on the ADU208 and ADU228 pages, so it reads as shared
  boilerplate. The captures show 0/1/2 only. **Implement three.**

**Do not infer responsiveness from the mnemonic.** The pattern nearly holds —
responsive commands start with `R` or `P`, and bare `DB`/`WD` answer while their
`n`-suffixed setters do not — but **`RKn` starts with `R` and is write-only**,
and it is the most-called command on the device. The per-command boolean table
is not redundant with a naming rule.

**Relay index ranges differ by command family:** relays are `n = 0..7`, input
lines are `n = 0..3` (4 bits per port). One shared validator across both is a
live off-by-four bug.

**Ontrak explains the HID situation directly**, which corroborates the
`hid_ignore_list` finding above from the vendor's side: "The Ontrak ADU devices
appear in the [kernel HID] black list because they do not behave like standard
HID devices… The ADU devices conform to the USB specification in a unique way
preventing us from using the canned HID drivers." They also note Linux does not
read the ADU's report descriptors and that "a Linux application must inspect the
first data byte to determine the report id" — which is why the `0x01` appears
raw in both directions with nothing stripping it, on our path and on libusb's
alike. Ontrak's own examples use a **200 ms** read timeout; with `bInterval` 10
at low speed the round-trip floor is ~10-20 ms, so that is ~10x margin and worth
adopting.

**Power-on relay state is undocumented, and `close()` does not de-energise.**
The manual never states what the relays do when USB power is first applied, nor
across re-enumeration. What it *does* document is USB **suspend**: "In suspend
mode the ADU208/ADU218 relay outputs remain in their last state" — and
critically, "the host may suspend the connection **if no handle is opened**". So
after a driver closes its handle the device may be suspended with outputs still
energised, indefinitely. Three consequences: never assume relays are open at
`open()` (read `PK`, or drive `MK000` explicitly); an explicit `MK000` before
close covers the clean path; and **nothing in software covers a crash** — which
is precisely why the hardware watchdog matters for the interlock work, and why
its feed must sit on the control path.

Two additional measurement notes, both about the witness rather than the
ADU218:

- `measure_resistance_4wire()` returns a plausible-looking large negative
  float with no sense leads attached, rather than raising. It is also not
  repeatable: `-116276 Ω` on one run, `-146172 Ω` on the capture checked in
  here. Do not use the 4-wire function as a witness on this setup
  (`KNOWN_LIMITATIONS.md` § H-5).
- `measure_continuity()` and `measure_resistance()` disagree by ~134 mΩ on the
  same physical circuit (6.006 Ω vs 6.140 Ω). Consistent with continuity using
  a different range, but it means the two are not interchangeable as a witness.
  The switching fixture uses `measure_resistance()` throughout.

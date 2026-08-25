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
  a fed-timer control; also `MKddd`, the counters, and `DBn`
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

Response payload widths are **fixed per command** and match the manual:

| Command | Width | Meaning |
|---|---|---|
| `PK` | 3 | PORT K as decimal `000`–`255` |
| `RPKn` | 1 | one relay, `0` or `1` |
| `Py` (`PA`/`PB`) | 2 | 4-bit input port as decimal `00`–`15` |
| `RPy` | 4 | 4-bit input port in binary, MSB-first |
| `RPyn` | 1 | one input line |
| `PI` | 3 | both input ports, decimal `000`–`255`; PORT A low nibble |
| `REn` / `RCn` | 5 | 16-bit event counter, `00000`–`65535` |
| `DB` | 1 | de-bounce setting |
| `WD` | 1 | watchdog setting |

## Five findings that would each have been a driver bug

1. **`RI` does not exist.** The manual's command summary (§5) lists `RI` to
   read both input ports. The command *description* (§6b) calls the same thing
   `PI`. The device answers `PI` and **times out on `RI`** — the summary table
   is wrong. A driver written from the summary would hang on every input read.

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

5. **A queued response outlives the command that caused it.** Interrupt-IN
   replies sit on EP `0x81` until read, so a driver that skips a read (or
   fails one) leaves an answer behind, and the *next* query returns the
   *previous* command's value — a silent wrong answer, not an error. This is
   not hypothetical: it invalidated the first framing measurement here, which
   credited a bare-ASCII write with a reply that belonged to the prefixed
   command before it. The driver must own the endpoint's read/write pairing
   strictly, and should drain on open. See `framing.txt`.

## And one finding that is an opportunity, not a bug

**The hardware watchdog is real, and it is a deadman that needs no software
running.** Armed with `WD1`, K0 opened after 3.7 s of host silence — witnessed
by the DMM, not just by the device's own `RPK0` — and the fed-timer control held
it closed for 3.1 s, so the drop is caused by silence rather than by arming.
`WD` also self-clears to `0`, which is the only trace a host can use to learn a
timeout happened.

This matters beyond the driver: `KNOWN_LIMITATIONS.md` § N-1 says a software
deadman cannot guarantee an output goes off, and `ROADMAP.md`'s "Hardware
interlock for unattended runs" is open work. The relay opens because the host
went *quiet*, so a wedged agent, a killed process, an unplugged cable and a
panicking kernel all de-energise the load. See `watchdog.txt` for the four
design consequences, chiefly that arming the watchdog makes every relay's state
depend on call frequency and must therefore never be implicit.

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

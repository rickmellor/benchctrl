# PDU41002 CLI transcripts

Verbatim captures from the real device. **Do not hand-edit to match the
driver** — that defeats the point. If the driver disagrees with a fixture, the
driver is wrong until a fresh capture says otherwise.

| | |
|---|---|
| Model | PDU41002 |
| Hardware version | 1.2 |
| **Firmware version** | **1.3.4** |
| MAC | `00-0C-15-42-29-41` |
| Captured | 2026-08-24 |
| Serial transport | FTDI FT232R `/dev/ttyUSB0`, 9600 8N1 |
| SSH transport | OpenSSH 10.0p2 → `192.168.1.246:22` |

`AGENTS.md` warns that a simulator written from the same misreading as the
driver will agree with the driver and still be wrong. `sim/qr10x.py`'s
docstring records what that cost last time. These files exist so
`sim/pdu41002.py` replays the device instead.

## Files

- `serial_reads.txt` — the read-only command set over serial
- `ssh_reads.txt` — the same commands over SSH
- `errors.txt` — the two documented error shapes
- `outlet_switch.txt` — an outlet actually being switched off and back on

Line endings are preserved as captured (`\r\n`, with stray bare `\n\r` inside
some tables — see below). Read these as bytes, not lines.

## What the captures establish

Facts the driver and simulator must honour, each observed rather than inferred:

1. **Prompt is `CyberPower > `** (trailing space). It terminates every
   response, and it is the only reliable read-until sentinel.

   **Caveat for anyone diffing these files byte-for-byte:** the trailing space
   is *not* present in the checked-in text, because the editor stripped
   trailing whitespace when these transcripts were saved. It **is** present on
   the wire — re-measured directly against the device on 2026-08-24, which
   reported `b'CyberPower > '` as the tail bytes and zero space-less
   occurrences. Do not "fix" `PROMPT` to match these files; a sentinel of
   `"CyberPower >"` without the space would silently match a prefix, and one of
   `"CyberPower > "` compared against a stripped fixture makes every read
   appear to time out.
2. **Serial echoes the command back; SSH does not.** The first line of a
   serial response is the command itself. Any parser must strip a leading echo
   *conditionally*, not assume one either way — this is the single largest
   textual difference between the transports.
3. **Command output is otherwise byte-identical across transports.** Verified
   by normalising the echo and prompt and comparing: `sys show`,
   `oltsta show`, `oltsta index 1 show`, `console show` and `snmpv1 show` all
   match exactly. This is what licenses one CLI engine over two links.
   (`devsta show` differs only in live mains voltage — 121.7 V vs 122.0 V.)
4. **`oltctrl` returns no confirmation whatsoever** — just a bare re-prompt.
   Read-back verification is therefore mandatory, not merely prudent: there is
   no other way to learn whether a mains switch took effect.
5. **The device reports 8 outlets**, and rejects out-of-range indices with
   `Index number must be 1 to 8` — a *different shape* from the caret error,
   with no `^` line. Two error paths to parse.
6. **Tables contain stray `\n\r` sequences** mid-row (notably `snmpv3 show`),
   not clean `\r\n`. Splitting on `\r\n` alone mis-parses them.
7. **SNMP is off**: `SNMPv1 : Disable`, `SNMPv3 : Disable`, and UDP 161 does
   not answer an SNMPv1 `sysDescr.0` get-request (4 s timeout). The design
   uses the CLI only.

## The CLI is single-session — the most important finding

**The device permits exactly one CLI session at a time, across all
transports.** Measured three ways:

- SSH alone, no serial session: logs in, reaches the prompt, survives 30 s idle.
- SSH *while serial is logged in*: authentication **completes** (the banner
  prints in full) and the device then immediately hangs up —
  `Connection ... closed by remote host`. The serial session is unaffected and
  keeps working. **The incumbent wins; the newcomer is dropped.**
- After sending `exit` on serial, SSH connects and runs commands normally.

Two consequences that are easy to get wrong:

- **Closing the serial port does not end the device session.** The CLI keeps
  session state across port open/close, so a driver that just closes the port
  leaves the device occupied and every later SSH attempt dies *after* a
  successful login. `close()` must send `exit`.
- **The failure looks like an auth problem and is not.** It arrives after the
  password is accepted, so "permission denied"-style diagnosis leads nowhere.
  The driver should name this case explicitly.

The manual's "Only one user can log in at a time" (91-page series manual, under
**WEB INTERFACE**) is about the web UI and does *not* document this; it was
established by experiment.

## SSH transport constraints (firmware 1.3.4)

Non-negotiable, all measured:

- `diffie-hellman-group-exchange-sha256` **fails** (`key exchange failed!`).
  Forcing `KexAlgorithms=diffie-hellman-group14-sha256` works. The defect is
  in group *exchange*, not DH.
- **Only `keyboard-interactive` auth is offered.** Pubkey is refused even with
  the matching private key, so `BatchMode=yes` can never work and the link
  needs a pty to answer the `password:` prompt.
- The ed25519 host key reads as **all zeros**, so host-key verification is
  meaningless here.
- **Authentication takes ~7.5 s** after the password is sent (banner at
  ~13.5 s from process start). Connect timeouts must allow for it.
- `console ssh reset_hostkey` was run once during characterisation. It rebooted
  the device and changed none of the above.

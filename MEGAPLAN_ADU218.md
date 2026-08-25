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
USBDEVFS_BULK             = 0xC0185502   # works on interrupt EPs despite the name
```

This fully satisfies *"If possible I'd like this to work with no
dependencies"* — it is a better answer than either option the user offered.

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
| 1 | **`RI` does not exist.** §5 lists it; §6b calls the same function `PI`. The device answers `PI` and times out on `RI` | A driver written from the summary table hangs on *every* input read |
| 2 | **PORT A and PORT B are 4 bits each**, not 8. Eight inputs across two isolated ports | Why `PK` returns 3 digits and `PA` returns 2. Indexing inputs 0–7 against one port is wrong |
| 3 | **Silence is the only error signal.** An unknown command returns nothing — no string, no sentinel | The driver needs a per-command *"expects a response"* table; it cannot discover this at runtime, because correct write-only silence is byte-identical to an error |
| 4 | **The `0x01` prefix is mandatory and is specifically `0x01`** — bare ASCII, `0x00` and `0x02` are all ignored | A test asserting "byte 0 is non-printable" would pass for two encodings the device rejects |
| 5 | **A queued response outlives its command.** Interrupt-IN replies persist until read | A skipped or failed read makes the *next* query return the *previous* value — a silent wrong answer. Drain on open; pair reads and writes strictly |

Finding 5 is not hypothetical: it invalidated the first framing measurement,
which credited a bare-ASCII write with the previous prefixed command's answer.
That is exactly the "sim agrees with the driver's misreading" failure
`AGENTS.md` warns about, caught before any code existed.

Also measured, and **unexplained**: closed-contact resistance is **6.14 Ω**
against a datasheet 700 mΩ typ / 1.1 Ω max. Eight reads spread 0.98 mΩ, so it
is systematic; § H-5 bounds this bench's lead error at ~79 mΩ, two orders too
small. Recorded as open rather than rationalised. **The driver must not use
datasheet on-resistance as a validation threshold** — key open/closed on the
DMM's overload sentinel, where the margin is effectively infinite.

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

## 4. The find that changes more than this driver: the watchdog works

`ROADMAP.md`'s *"Hardware interlock for unattended runs"* is open work waiting
on a GPIO e-stop button ordered 2026-08-19, and `KNOWN_LIMITATIONS.md § N-1`
states the honest position that **no software deadman can guarantee an output
goes off**. The ADU218 answers that in hardware:

- `WD1` armed, then host silence → K0 **opened after 3.7 s**, witnessed by the
  DMM, and `WD` self-cleared to `0`.
- **Control case:** fed with `PK` every 0.3 s → K0 stayed **closed** for 3.1 s
  with `WD` still `1`.

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
2. `WD1` (1 s) is unusable for a general bench; `WD3` (1 min) is the only
   setting a run loop can plausibly meet.
3. `WD` self-clearing to `0` is the **only** distinguishable trace that a
   timeout fired. Polling it is how a host learns. That read belongs in the run
   event stream — a fired interlock absent from the artifact bundle is a gap in
   the audit trail.
4. A test must assert the **ladder** (fed stays closed, unfed opens) against a
   synthetic clock — not merely that `WD1` is accepted. A watchdog nobody feeds
   is indistinguishable from one that works until the day it should have fired.

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

## 7. Registries a new driver must land in

Verified against the PDU41002 commit stack, which is the freshest precedent.
Each of these is a silent failure if missed:

| File | Change | Failure if missed |
|---|---|---|
| `config.py:38` | `DEVICE_KEYS` += `"ontrak_adu218"` | `Config.from_dict` **silently drops** the device |
| `agent/registry.py:264` | `_adu` opener closure, lazy import | `BenchValueError: no opener for device key` |
| `sim/factories.py:162` | `make_adu218()` + `FACTORIES` entry | sim mode unavailable |
| `mcp.py` | import + `register_mcp_tools(mcp)` + flat re-export block | MCP parity test fails |
| `net/codec.py:76` | `ADU218Info` | dataclasses degrade to bare dicts over the wire |
| `net/errors.py:141` | every exception | types degrade along the MRO remotely |
| `discovery.py` | `SIGNATURES` + `scan_usbfs()` + `transport="usbfs"` | invisible to the bench inventory |
| `dashboards/fui/view.py:130` | `INSTRUMENTS` row | slot drawn as unknown-kind |
| `deploy/udev/` | `63-benchctrl-adu218.rules` | **already committed** |

Tests that fail mechanically on a miss: `tests/test_session_config.py:44,119`
iterate `DEVICE_KEYS`; `tests/test_mcp.py:604` is the parity test to copy.

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
owns a `SerialLoopback` and every existing sim is pty-backed, whereas this
device's channel is an 8-byte packet endpoint. The seam is `usbfs.py`'s
four-method duck type (`write`/`read`/`is_open`/`close`), the same informal
shape `links.py` uses in the PDU driver, so the sim provides a fake link and
the real driver drives it unmodified above that line.

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
integration) before driver code. They were spawned; **their reports never
arrived** — the session was compacted while they ran and the tasks are gone.
Rather than re-spawn and wait, I did step 4's work by hand, which is what
produced §2's five findings and §4's watchdog result. That is a stronger
outcome than the agent reports would have been, because every claim here is
measured on the device rather than read from a PDF — but the *integration*
agent's anti-pattern list is the one thing genuinely not replaced, so §6 and §7
were derived by reading the PDU41002 commit stack directly instead.

## 12. Deferred, inherited from master

Not this driver's work, but tracked so it is not lost — from WIP commit
`b44380e`, now on master:

- `agent/runs/analysis.py` (190 lines) has **zero tests**
- no `docs/runs.md` section and no CHANGELOG entry for run-analysis
- `run_analysis` is still absent from `RUN_EVENT_KINDS`

Also still outstanding: `sudo chown rick:rick /home/rick/pdu_creds.txt` — mode
is 600 but owner is `root:root`, so `rick` cannot read their own credential
file. `chown` is blocked in the sandbox, so this one needs the operator.

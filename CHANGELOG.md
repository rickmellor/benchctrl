# Changelog

All notable changes to benchctrl. Follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/).

Known limitations across all versions are tracked in
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) (hardware caps,
firmware quirks, harness workarounds). Read it before debugging a
new failure — it's likely a documented limit.

## [Unreleased]

### A user guide, separate from the reference docs — `docs/guide/`

Fifteen task-oriented pages for people *using* the bench rather than changing
it: an overview, the theory of operation, the equipment matrix, installation,
bench-host setup, local vs remote, driving it from an agent harness, how to add
a driver, and six worked examples — board bringup, sleep current, power
characterization, battery emulation, power cycling, unattended runs.

**Why a new tree rather than more files in `docs/`.** The existing docs are
reference: `api_reference.md` is exhaustive by design, `drivers.md` is per-device,
`design.md` is internals. None of them answer "I have a new board and want to
know what it draws." The guide is organised by task, one file per page, so it
maps 1:1 onto a wiki space — directory becomes page tree — when it moves under
document management. `docs/*.md` is unchanged and is the layer the guide links
down into.

Written to be **externally distributable**: no product names, part numbers or
internal project identifiers. The campaigns that motivated the examples appear
by technique and measured result, so a reader outside the org gets the method
and the numbers without the provenance.

Two conventions the pages hold to, both learned the hard way here.
**Limitations go in the body, not an appendix** — the emulator-and-recording
deadlock belongs next to the emulator example, because an appendix is read after
the mistake. And **numbers are measured, not nominal**: every figure traces to
`scenarios/README.md`, `docs/battery.md`, `docs/drivers.md` or
`KNOWN_LIMITATIONS.md`, including the ones that make the bench look worse — the
27× instrument-regime error, the 4.2 V output ceiling that silently clamps a
fresh LiPo, and the 100 Hz loop that cannot see a switching converter.

**Verified rather than proofread.** A link/anchor checker over all fifteen pages
reports zero broken links; every backticked tool name is cross-checked against
the live 324-entry `TOOL_TIERS`; every subcommand resolves in `--help`; and both
example run specs were parsed verbatim through `RunSpec.from_dict`, so the JSON
in the docs is executable rather than decorative. That pass found fourteen
defects in the first drafts — `Channel` for `OtiiArcChannel` in eleven places,
`--yes` trailing the device group (a parse error, not an authorisation) on five
pages, three tool names that do not exist, and four undefined imports in the
profiler snippet.

It also found one **product** bug, disclosed in the affected page rather than
papered over: `cp2112_open` calls `CP2112.open()` directly instead of
`session.resolve()`, so `--sim silabs_cp2112` is accepted, logged and then
ignored. Seven of the eight drivers resolve correctly; the sim factory and the
registry opener both exist, so only the MCP-tool layer is wrong.

### The whole bench from a shell — 324 CLI subcommands, generated

`benchctrl` reached seven drivers with ten hand-written Arc-only commands. Every
other device was Python-or-MCP only, so "tell me the relay state" meant writing
a script. Now every MCP tool is a shell command:

```bash
benchctrl adu218 relay-states
benchctrl --yes adu218 set-relay-state 0 on
benchctrl --remote unoq.local sdm4065a measure-dc-voltage
```

**Nothing is hand-written per device.** The subcommands are built by reflection
over each driver's `_TOOLS` tuple — the same tuple the MCP server registers — so
a tool added to a driver becomes a shell command with no edit to the CLI. 324
across nine groups: arc 23, qr10x 11, dl3031a 45, dp2031 134, sdm4065a 54,
pdu41002 15, cp2112 10, adu218 19, framework 13. The parity tests in
`test_mcp.py` were decorative before and are load-bearing now.

`_TOOLS` rather than asking a live server, for two reasons and the second one
decided it. Importing `benchctrl.mcp` costs ~0.64 s (FastMCP alone is 0.44 s of
it) against 0.06 s for all eight drivers, and a full remote round trip is 63 ms
— so introspecting a server would be slower than every call the CLI makes. More
decisively, it would hard-depend the `benchctrl` console script on the optional
`[mcp]` extra, which needs Python ≥3.10 while the package declares
`requires-python = ">=3.9"`.

**Risk is the part that is *not* generated**, and that is the whole design.
`cli_tiers.py` classifies all 324 tools explicitly — 163 read, 125 tier 2, 33
needing `--yes`, 3 needing `--yes` *and* a named environment variable — and a
tool with no entry raises rather than defaulting to safe. Two tests assert
completeness in both directions, so adding a driver tool fails the suite until
somebody decides what it can do to the hardware.

Reusing the agent's `dispatch.is_mutator()` was the obvious move and is measurably
wrong. It is a bare name-prefix match: `is_mutator("write")` is **False** — the
raw SCPI passthrough on all three SCPI drivers — while `take_snapshot` matches
and only samples, and `step_voltage_up` moves a live output and matches nothing.
A CLI keying its read/write split on that predicate would file arbitrary SCPI
under "safe". A test asserts `is_mutator` still disagrees, so if the drivers are
ever renamed the table can be simplified deliberately rather than by accident.

Four tiers, not two, because a confirmation nobody reads protects nothing.
`set_voltage` on a de-energised output needs no `--yes`: requiring one for every
setpoint would make the flag reflexive, which is how a real prompt gets waved
through. And the *de-energising* direction is deliberately frictionless —
`adu218 reset-relays` and `cp2112 reset-lines` are tier 2, with a test asserting
de-energising is never gated harder than energising. An operator fighting a live
output should not meet a gate on the way to safety.

The three environment gates are for effects that outlive the command:
`BENCHCTRL_PDU_ALLOW_SWITCHING` for `pdu41002 set-outlet-state` / `reset-outlet`
(it switches mains) and `BENCHCTRL_ADU218_ARM_WATCHDOG` for `adu218
set-watchdog` (`WDn` sets *and* arms, and the relay opens later, after the CLI
has exited). Separate variables because one authorisation must not grant the
other: `--yes` says you meant to run a command, the variable says you know what
is physically attached, and those are different claims. `--help` marks the two
tiers `[!]` and `[!!]` so an operator can see which commands move hardware
before typing one.

**`bool("off")` is `True`, and a CLI passes strings.** A boolean parameter routed
through `argparse`'s `type=bool` would energise on *every* spelling, including
the ones that mean no. Every boolean takes an explicit word instead — `on`/`off`,
`true`/`false`, `1`/`0`, `yes`/`no`, `enable`/`disable` — and anything else is
refused by argparse. The PDU driver's `isinstance(on, bool)` guard would have
caught the resulting non-bool, but a CLI that is only safe because a driver three
layers down is defensive is not safe.

**A one-shot needs a lifecycle a server does not**, and four things follow that
reflection cannot infer (`cli_lifecycle.py`):

- **Teardown depends on the mode.** Locally, the driver's `*_close` tool.
  Remotely, `close()` is *refused* by the proxy — closing is governor-mediated so
  an armed output is never orphaned — and the correct exit is a clean client
  disconnect, which the agent reads as consent to release the writer claim.
  `agent.close` is **not** the remote equivalent: it trips the governor
  bench-wide, so one command finishing would drive every armed device on the
  bench to safe, including instruments the command never touched.
- **A one-shot must not disarm what a previous one armed.** `adu218_open`
  defaults `disarm_watchdog=True` and its connect path sends `WD0`. Right for a
  long-lived server, since the watchdog setting reads 0 for both "timed out" and
  "never enabled" (§ F-22) so a fresh session cannot interpret an inherited
  value — and wrong here, where *every* invocation is a fresh session. Left
  alone, `benchctrl adu218 relay-states` would silently disarm a watchdog the
  previous command armed. The CLI overrides the default; the driver is unchanged.
- **A failed de-energise is a dict key, not an exception.** `dl3031a_close`
  disables the load input and `dp2031_close` disables three outputs, and both
  report failure as `input_off_failed` / `outputs_off_failed` in their *result*
  while the close itself completes. A CLI checking only for exceptions would
  print your measurement and exit 0 with the load still sinking current. Hence
  **exit code 4**: the reading still prints, the warning goes to stderr so
  `--json` stays parseable, and the shell learns something went wrong. Losing the
  measurement to a teardown problem would make the CLI unusable exactly when the
  bench is misbehaving.
- **The open is implicit**, and its arguments come from the `open` block of
  `~/.config/benchctrl/config.json` — the same dict the MCP server and the agent
  already forward — so `allowed_outlets` and the port live in one reviewable
  file rather than a second CLI-only one that would eventually disagree.

**`--remote` / `--local` / `--sim` now exist.** `docs/remote.md:52` had
documented them as CLI flags since remote mode landed, and until now only
`benchctrl-agent` had them. Any non-local binding is announced on stderr
(`benchctrl: ontrak_adu218 -> sim`), so *"why did that read a simulator"* has an
answer; silence means everything resolved local, because a line per device would
train you to ignore the one that matters. With no flags the CLI installs no
config object at all — an empty one would count as an override and beat the
environment and the config file, which have documented precedence.

Exit codes are a contract: 0 ran, 1 ran but found nothing, 2 the device or
library refused, 3 the *CLI* refused, 4 teardown could not de-energise, 130
interrupted. 2 and 3 are distinct so a script can tell "the device is not there"
from "you did not authorise this", and a refusal happens before anything is
configured or opened and says `Nothing has been sent to the device.`

Catching only `BenchError` would have let ordinary failures escape as tracebacks:
**no driver exception subclasses it** — `ADU218Error`, `PDU41002Error`,
`QR10xError` and the rest all descend straight from `RuntimeError` — so "device
not plugged in" would have exited 1 with a traceback, and 1 is already
`discover`'s code for "found nothing". The class list comes from
`net/errors._registry()`, which is maintained anyway so exceptions survive the
RPC wire, filtered to `benchctrl.`-owned classes so a bare `ValueError` from a
CLI bug is not disguised as an instrument refusing something.

A claim conflict gets its own explanation, because the server's wording is aimed
at a protocol client: it names an opaque session id and says this session "is a
read-only observer", which reads as though the operator asked for that. From a
shell it is nearly always a second `benchctrl` process or a dashboard that got
there first, and `RemoteClient.attach` calls `agent.claim` once with no retry and
no backoff anywhere in `net/client.py` — so the advice is to wait, and the device
is untouched. Keying on message text is fragile and is contained by a test that
produces the error from a **real** agent with two connected clients, so a
reworded server message fails the test rather than silently turning the
explanation off.

Also fixed on the way through: `_render_scalar` JSON-dumped nested collections,
so `benchctrl adu218 relay-states` printed `relays: {"0": false, ...}` from the
*non-JSON* output mode — the on/off rendering never reached the values that
needed it most, on the one device where on/off is the whole answer. Found by
running every example in the new documentation instead of trusting it.

The ten legacy Arc commands are untouched and still open the Arc directly, so
`--sim` and `--remote` do not apply to them; `benchctrl set-voltage 3.3` and
`benchctrl arc set-voltage 3.3` are different commands reaching the same
instrument until the legacy ten are removed.

Two limits are documented rather than fixed. **Local mode has no governor** —
`SafetyGovernor` is built only by `AgentServer`, so a local one-shot has no arm
tracking and no deadman while `--remote` has both (§ A-6); the ADU218 is covered
anyway by its hardware watchdog and the PDU is exempt by design, but the Arc and
the two Rigols are real exposure. And **mains switching is now one shell line**,
which does not change the cabling invariant that makes self-kill impossible but
does raise the cost of anyone ever changing it (§ F-12).

**Two claims needed a real process against a real device**, so
`tests/test_hardware_cli.py` spawns `python3 -m benchctrl` as a subprocess: that a
reflected subcommand drives the instrument through a cold open/call/teardown, and
that the exit codes are what `$?` sees rather than what `main()` returned — the
second also depends on `__main__` and on no traceback escaping, and scripts branch
on `$?`.

The gate is tested as a *pair*, because a refusal proves nothing alone: a
permanently-closed gate passes every refusal assertion, so the **satisfied** case
is the discriminator and is checked against real hardware, where it has to get
past the gate and reach the device. With nothing attached that run also fails —
with 2 — and 2-vs-3 would stop being a distinction. Measured on the board:
`adu218 set-relay-state 0 on` exits **3** with `Nothing has been sent to the
device.` and an empty stdout; `--yes` plus `BENCHCTRL_ADU218_ARM_WATCHDOG=1`
reaches the instrument and exits **0**.

Eight of the ten pass on the bench today. The two that switch a relay skip unless
`BENCHCTRL_CLI_HW_WRITE=1`: the CLI deliberately has no `allowed_relays` flag, so
a CLI write reaches whatever relay is named, and the nomination has to come from
the operator. It shares `BENCHCTRL_ADU218_RELAY` with the driver's own hardware
tests so the two files cannot drift about which contact is safe to move.

New: `src/benchctrl/cli_generated.py`, `cli_tiers.py`, `cli_lifecycle.py`,
[`docs/cli.md`](docs/cli.md), 140 hardware-free tests across
`test_cli_generated.py`, `test_cli_tiers.py` and `test_cli_main.py`, and 10
hardware-marked in `test_hardware_cli.py`.

### Ontrak ADU218 relay / digital I/O interface — zero dependencies

Eight 1 A solid-state relays, eight opto-isolated digital inputs with
hardware event counters, and a hardware watchdog that de-energises every
relay by itself if the host stops talking. First driver in benchctrl with
**no dependencies at all** — not pyserial, not pyvisa, not `hid` or
`pyusb`.

The device is USB HID, not serial: the starting assumption was pyserial
and it was wrong. Ontrak's own Linux path is `libusb` plus their `AduHid`
shared library, neither of which is available on the Uno Q and both of
which are dependencies. The route taken instead is raw USBDEVFS ioctls
from the standard library — `fcntl.ioctl`, `ctypes`, `os` — which needs
nothing installed.

Three kernel facts make that route contractual rather than a trick:
`USBDEVFS_BULK` on an interrupt endpoint is explicitly handled by
`devio.c`, which rewrites the pipe and calls `usb_fill_int_urb()` with the
endpoint's own `bInterval`; `usbhid` *deliberately* ignores Ontrak devices
via `hid_ignore_list`, so `CLAIMINTERFACE` succeeds with no driver to
detach and no udev unbind rule; and `usbdev_open()` holds a runtime-PM
reference for the life of the fd, so autosuspend cannot strand a live
session. The ioctl request numbers are **computed**, not copied —
`USBDEVFS_BULK` encodes a struct size that differs between 32- and 64-bit,
so a hardcoded constant works on the laptop and fails on the board.

**Silence is the device's only error signal**, and that shaped everything
else. There is no error reply: an unknown command, a valid command with an
out-of-range argument, and a write-only command working perfectly are
byte-identical on the wire — nothing comes back. So the driver carries an
explicit whitelist of every command it can render, an explicit
per-command `responsive: bool` (never inferred — `RKn` is write-only
despite starting with `R`, while every other `R` command answers, and it
is the most-called command on the device), and an explicit per-command
reply width taken from hardware captures rather than the manual, because a
width wrong by one turns a desynced reply into a plausible value.
`ADU218TimeoutError` is documented as ambiguous *by construction*: the
information needed to disambiguate is not on the wire. The mitigating
half, unlike the SDM4065A: an ignored command does not poison the session.

Four more device behaviours drove decisions that look odd without them:

- **Writes are unacknowledged**, so `set_relay_state` returns the
  **verified read-back** rather than `None`, and `open()` drains the IN
  endpoint before anything else — replies queue rather than overwrite, so
  a reply left by a crashed previous process would be returned as the
  answer to this process's first query, a silently wrong value rather than
  an exception.
- **The watchdog is fed by *any* command**, including a plain state read
  and including one the device rejects. So a status-polling loop silently
  neuters it — measured on a synthetic clock, ten rounds of "advance 9 s,
  read the relays" held a relay across 90 s with a 10 s watchdog armed and
  zero trips. There is deliberately **no keep-alive helper**: a background
  feeder would keep the timer fed precisely while the failure it guards
  against was happening. `close()` also does not disarm it, because
  releasing the device *is* the silence it exists to detect.
- **`WD` reads 0 both for "timed out" and for "never enabled"**, so a trip
  leaves no trace the device can be asked about. The driver holds its own
  armed state and compares, and writes `WD0` at `open()` unless told
  otherwise, because a fresh process would otherwise inherit the ambiguity.
- **`RCn` is the only command that both answers and mutates.** A lost
  reply after the device has cleared loses the count permanently, and a
  retry would report 0 — indistinguishable from "no events". So
  `clear_counter()` never retries and the returned value is the only copy.

Safety differs from the PDU41002 on purpose: these are 1 A signal SSRs on
instrument leads, not mains contactors, so `allowed_relays` defaults to
all eight per the operator's stated policy rather than being mandatory.
The allowlist guards *closing* a contact, not opening one —
`set_relay_state` always de-energises and `reset_relays()` bypasses the
list entirely, so the safe state stays reachable on exactly the benches
most carefully configured. `set_relay_port` is the exception and enforces
on the whole mask, because `MKddd` moves all eight lines in one
indivisible command. `open()` warns about relays it found already
energised rather than driving them off, since it cannot know what they are
holding.

Two index ranges, not one: relays are 0-7 and **input lines are 0-3**
(ports A and B are four bits each). A shared validator would accept
`RPA5`, which the device answers with silence — a timeout three layers
from the bad argument. `RPy` also replies MSB-first, so the leftmost
character is line 3; indexing it directly is an off-by-three that reads
correctly for the all-zero case every unwired bench produces.

Naming is constrained by `agent/dispatch.py`, which derives which calls
need a writer claim purely from name prefixes: every mutator takes an
existing prefix, and `is_open` keeps its framework meaning (*link
connected*) while nothing relay-facing borrows open/close — a test
enforces it, since a `close_relay()` would be remotely callable with no
claim. The device key is deliberately absent from `SWITCHED_PDU_KEYS` and
the FUI's `PDU_KEYS`, which mean "switches mains".

The simulator subclasses the **production** USBDEVFS link and replaces
only the ioctl and the device node, so framing, the mandatory `0x01` report id, NUL
padding, the desync check, the timeout mapping and `drain()` all remain
shipping code paths under test. Its reply widths are asserted against
`tests/fixtures/adu218/reads.txt` at run time rather than transcribed, so
it replays the device instead of agreeing with a reading of the manual.
Its clock is manual, so the watchdog ladder is deterministic.

Also included: 19 MCP tools (`adu218_*`), a passive discovery signature
(VID `0x0A07` / PID `0x00DA`, identified from sysfs — nothing is written
to the device), full agent/remote registration, and
`KNOWN_LIMITATIONS.md` entries H-6, H-7, F-21 through F-24, and A-5 —
which is not about this device alone: no simulator in the repo reaches its
instrument's real transport, and writing the ADU218's version of that gap
was what made the general case worth stating.

There is also a hardware suite (`tests/test_hardware_ontrak_adu218.py`),
and it earns its keep for one reason: the driver's own verification is the
device talking about itself. Since writes are unacknowledged,
`set_relay_state()` confirms a switch by re-reading the same device — which
catches a device that ignored a command but not a driver whose read-back is
secretly its own commanded value. So relay K0 is wired across the
SDM4065A's leads and the suite asserts both instruments agree across five
alternating transitions. No resistance threshold is asserted: the same
closed relay has measured between 6.14 Ω and 10.69 Ω across sessions, all
of it probe seating. What is asserted is that a closed contact reads *a
number* and an open one reads the DMM's overload sentinel — an open contact
is unmeasurable rather than merely large, so no amount of contact drift can
confuse the two. The suite was then verified to fail on the defect it
exists for: a `set_relay_state` patched to return its own argument without
touching the device fails three of the six tests.

The input, counter and de-bounce half of the device was implemented and
simulator-covered long before any input line was ever driven to a `1`,
which meant no counter had ever incremented on hardware. A square wave into
PA3 closed that gap and produced two findings:

- **Counters count cycles, not edges.** Cross-checked against host level
  sampling, whose failure mode is different: at 10 Hz the device counted
  10.030/s while the host saw 9.997 rising *and* 9.997 falling edges/s —
  ratio 1.003, where counting both edges would give 2.0. Confirmed again at
  0.5 Hz. This matters because the two hypotheses differ by exactly 2× and a
  doubled frequency looks plausible indefinitely.
- **The counter-to-input map is only an image.** Counter assignments live in
  a "Table 1" *image* that the manual's text layer omits entirely, so the
  mapping the driver uses was unverifiable from the document. Driving PA3
  moved counter 3 and nothing else, with the other seven flat.

Moving the generator to **PB2** later closed the other half, and it is the
half that carries the information: with the stimulus on PORT A every counter
index equals its line number, so offsets of 4, 3 and 0 are all consistent
with the PA3 result. On PB2, counter **6** was the only one of eight to move
— 201 events in 20.13 s, 9.987/s against a 10 Hz wave — which pins the
offset at 4. PA3 falling silent and counter 3 freezing is part of that
result: it is what rules out "everything counts regardless" and proves the
lead moved rather than the measurement repeating. Mutating the offset to 3
fails all three counter tests, so the map is now pinned rather than merely
consistent.

**Then all eight positions were measured**, by walking both the meter and the
generator across the device one terminal pair at a time. Every input line moved
its own counter and only its own — PA0-PA3 → counters 0-3, PB0-PB3 → counters
4-7 — each setting its own `PI` bit and no other, every rate within ±0.5 % of
the 10 Hz stimulus (which is ±1 event of quantisation in a 10 s window). The
Table 1 image is now redundant rather than corroborated, and the PORT B offset
rests on three independent readings (PB0 → 4, PB2 → 6, PB3 → 7).

The same walk **independently witnessed all eight relays**, which the opt-in
all-eight sweep cannot: the bench has one meter, so that test checks seven
relays against the device's own read-back. Here each relay in turn read
*overload* open and a finite value closed, with the alternating
five-transition check passing at every position. Closed readings: **16.87 /
17.50 / 20.77 / 28.37 / 31.36 / 32.09 / 41.19 / 45.65 Ω** — a **2.7× spread
with no relation to index**, which settles F-27's wiring-not-relay conclusion on
eight points instead of two. Within-position spread was ≤ 0.06 Ω everywhere but
one relay, so the drift that once broke a `hi < lo * 2` bound is clip seating.

A loose screw terminal during that walk made one relay read **336 kΩ closed**,
and the suite passed. Deliberately left that way: the claim is that the relay's
state follows the command, and overload → finite is a real state change, so the
claim held. What was broken was bench wiring quality — not the driver's claim,
not visible to it, and a ceiling would be exactly the
threshold-on-a-wiring-property F-27 exists to forbid.

**The hardware watchdog was armed on the bench and allowed to trip**, which is
the claim the whole watchdog design rests on and the last thing here that had
only ever been tested against a synthetic clock. Armed at `WD2`, the DMM held
17.471–17.474 Ω across 24 consecutive readings spanning 9.86 s after arming,
then read the overload sentinel — the trip brackets to **(9.86, 10.83] s**
against a 10.0 s nominal, one meter read wide. The *device* de-energised its own
relays, with no benchctrl process, no GPIO and no kernel driver in the decision
path.

Running it exposed a hole in the test making the claim, and closed it. The
original form armed `WD1` (1 s), waited 1.5 s and asserted the contact was open
— which is satisfied by two different hypotheses: that the relay opens when the
timer **expires**, or that it opens as a side effect of **arming at all**. The
second would make the interlock useless, since it would drop the load the moment
it was enabled. `WD1` cannot separate them, because one meter read costs ~0.41 s
and the entire window is 1 s, leaving no room to sample inside it. The test now
arms `WD2` and asserts the contact is **still closed** early in the window,
guarded by a check that the early sample really landed early — a slow VISA round
trip would otherwise silently degrade it back into the form that cannot tell the
difference. Proven by mutation in both directions: de-energising the relay right
after arming fails the `WD2` form on the early assertion and **passes** the
`WD1` form, so the added coverage is measured rather than asserted.

The same run witnessed **PORT B's bit ordering for the first time**, across
all four input commands, which had only ever been checked against PORT A —
`RPy`'s MSB-first text reversal, `Py`'s LSB weighting, `PI` placing PORT B in
the *high* nibble, and the per-line read all agreed on line 2. Sampled as a
union with an equality assertion, because a single read of a 10 Hz line is
exactly the coin flip that made an earlier test flaky, and "ever high" would
pass on a reply with every bit set. The earlier sweep's "no PORT B line ever
high" was an absence of evidence — nothing was attached there — not evidence
the ordering was right.

**A skip that names the right line can still hide a stale default.**
`BENCHCTRL_ADU218_INPUT` defaulted to `B2`, and skipping with the line named
rather than false-passing was treated as sufficient. It is not: the bench walk
left the generator on **PB3**, so with the bench fully wired and nothing
misconfigured, this file ran 7 passed / 5 skipped instead of 10 / 2 and three
input/counter tests silently stopped running. "PB2 is not toggling" is equally
true whether the generator is unplugged or one terminal over, and the second is
what had happened. The default is now `B3`, found by sampling all eight lines
rather than assumed, and the skip now says where the generator **is** — one
extra sweep of the port, since the device is already open. Verified with
`INPUT=A1`: it names PB3 and says to set the variable or move the default. A
default that tracks the bench needs a message that can distinguish "moved" from
"absent", or it degrades into a green run with less coverage than it claims.

The vendor manual also supplied what no capture had: **de-bounce settings
are durations, and they run backwards** — `0` = 10 ms, `1` = 1 ms
(default), `2` = 100 µs. So the *highest* setting is the *weakest* filter,
and an operator chasing maximum de-bounce would pick `2` and get the least.
`read_debounce_ms()` and `DEBOUNCE_MS` exist for that reason, and the MCP
tools now return `debounce_ms` beside the raw setting.

Measuring the ladder against a real signal produced a **negative result
worth recording with its scope**: at 10 Hz all three settings counted
identically (10.042 / 9.992 / 9.992 counts/s, 0.5 % spread). That is
expected rather than broken — every filter width is far shorter than a
50 ms half-period, so none has anything to reject — but it means a passing
de-bounce round-trip proves acceptance, *not* effect. Telling the settings
apart needs a few hundred Hz against counters rated to only 1 kHz, above
which the count under-reports silently. New entries F-25 and F-26.

The hardware suite grew from 6 tests to 12: the inputs, the counters, the
cycles-not-edges cross-check and the de-bounce round trip now run against
the real device (**10 passed, 1 skipped** with a live 10 Hz stimulus). Two
are opt-in behind environment variables, because their default should not
be to act: `BENCHCTRL_ADU218_SWEEP_ALL` for the all-eight-relay sweep,
which previously existed only as an uncodified ad-hoc run so nothing would
have caught a regression on relays 1-7, and `BENCHCTRL_ADU218_ARM_WATCHDOG`
for the witnessed trip. Both energise outputs the operator has not
otherwise nominated, and per `AGENTS.md` that is the operator's call to
make, not a default.

Hardware-free coverage closed the two gaps that inspection had found and
nothing asserted: the 65535 → 0 counter wrap (including the negative
`after - before` it produces, since differencing is the only correct way to
use these counters) and the reply-above-maximum protocol error. Relays 6
and 7 also previously appeared *only* inside port-mask tests, so all eight
now switch individually with the emitted `SKn`/`RKn` commands asserted —
each verified against a deliberately shifted command builder, which the
per-index test kills at exactly index 7.

A late audit of the command whitelist against the public method surface
found the driver **documenting a capability it could not use**. `PA`/`PB` —
one input port's nibble — was whitelisted, given a hardware-measured reply
width, modelled by the simulator and written into a `docs/drivers.md` table
row, while no method could send it. The whitelist serves double duty here,
as the safety gate and as the response-width table, and an unreachable
entry satisfies both, so nothing in the suite objected. The SDK ↔ MCP
parity test guards the opposite direction and was silent too.

Closed with `input_port_mask(port)` and `adu218_input_port_mask`, which are
worth having rather than a fourth spelling of the same read: `Py` is the
only input read whose reply is **LSB-weighted decimal**, so bit 0 is line 0
with no reordering, where `RPy` is MSB-first text and `PI` packs both ports
into one byte. It is also a *different* answer from a masked `PI` — one
port, with the other port's state absent rather than masked off.

The general guard matters more than the method:
`test_every_whitelisted_command_is_reachable_from_the_sdk` drives the
public surface and asserts no whitelist entry goes unsent, so the next
command documented as present and absent at once fails a test instead of
shipping. It drives the surface rather than grepping the source, because a
grep passes on a method that renders a command and is never called — and it
asserts a floor on the number of distinct commands sent first, since a
reachability test that exercises nothing passes trivially.

Fixed along the way: `scan_usbfs()` now returns `[]` rather than raising
when the USB bus cannot be enumerated. `enumerate_devices()` raising is
correct for a driver about to open a device and wrong for a scan, and
because `discover()` builds one merged list, letting it propagate took out
**every other transport's results too** — a machine with no `/sys/bus/usb`
reported no VISA instruments either.

The hardware suite's DMM witness gained two skips, both for conditions that
previously arrived looking like something else. A resistance read cannot
tell an open contact from **leads that are not on the contact at all** —
both are unmeasurable — so the witness now reads DC volts first and skips
when the leads sit on a powered net, naming the wiring instead of failing as
`assert None is not None`. It cannot mask a stuck relay: a dry contact reads
~0 V open or closed. Measured after the CP2112 work moved the meter: 3.392 V
standing, unmoved by K0. And because a running agent opens the SDM4065A
lazily and keeps the VISA handle *after* the claim is released, a direct open
returns errno 16 while the meter sits idle — indistinguishable from an
unplugged instrument. The witness now borrows the meter *through* the agent
in that case, the way `tests/test_hardware_cp2112.py` does by design, rather
than requiring the whole bench be safe-stopped to run one file.

The leads then moved again — to **K7** — and that exercised the volts gate's
blind spot: it catches a *powered* net, but another **dry** contact reads ~0 V
exactly like the right one, so a stale `BENCHCTRL_ADU218_RELAY` fails rather
than skips. Both witnessed relay assertions now name **both** causes ("either
that relay is not switching, or the leads are not across it — this test cannot
tell them apart") and print the device's own read-back to say which to go and
look at. `test_reset_relays_reaches_a_genuinely_open_contact`'s control read was
a bare `assert witness() is not None` with no message at all; it has one now,
because that assertion failing is what makes the open-contact claim below it
unfalsifiable. Verified by pointing the suite at K5 with the leads on K7: both
tests fail with the wiring named. `TEST_RELAY`'s default moved 0 → 7 to match
the bench.

**K7 is also the strongest evidence yet for refusing a resistance threshold.**
Same meter, same session, identical PhotoMOS parts: K0 closed reads
**9.483 Ω**, K7 closed reads **36.02–36.22 Ω** — nearly 4× apart, because the
excess is lead and contact resistance *outside* the relay. Any `< 10 Ω` rule
derived from K0 would call a perfectly good K7 open. Keying on the overload
sentinel instead is what makes the witness survive being re-wired at all.

Re-wiring also exposed a **second flaky assertion** in the same test, and it
failed for the reason the first one did: a bound calibrated on the old wiring.
`hi < lo * 2` on the two closed readings carried the comment "the within-session
spread is milliohms" — true of K0's screw clamps, false of K7's spring clips.
Measured over 15 back-to-back runs of that exact sequence, K7 closed drifted
**62 → 127 → 61 Ω**, monotonically and then back, worst within-trial ratio
**1.80** against a limit of 2.00 — so it failed about one suite run in seven.
Now an order of magnitude, with what that gives up written down rather than
implied: at ×10 a K7→K0 lead move (ratio 3.81) no longer trips it. That
coverage was illusory anyway, since no threshold separates 3.81 from a 1.80 the
clips produce unaided; the assertions that actually catch a lead on the wrong
relay are the two above it. 6 consecutive full-suite runs green after. New
entry **F-27** collects every closed-contact resistance figure measured on this
bench and states why no test may assert a threshold or assume stability.

`test_a_driven_input_line_reads_high_and_only_its_own_counter_moves` was
**flaky on real hardware, 2 passes in 6 runs**, and the cause was one
`input_mask()` read asserted high on a line toggling at 10 Hz — a coin flip
sitting directly beneath a block that samples 60 times *because* the line
toggles. It now samples the union the same way (measured: 32 of 60 reads
catch the low half, so a single read fails about half the time) and asserts
the union equals exactly the driven bit, so the bit-position claim a wrong
shift would break is still tested. 6 of 6 after.

**"Simultaneous" said six times without saying which claim it was.** Asked
what the gated all-eight test needs and whether it wanted a logic analyzer,
the honest answer turned out to require a distinction the docs never drew.
`MKddd` being *indivisible* is a claim about the **command**: eight
`SKn`/`RKn` writes are eight USB transfers, so the port really does pass
through `0b10101000` en route to `0b10101010`, and one `MKddd` does not —
which is the whole basis for `set_relay_port` enforcing the allowlist on the
entire mask. Contact-to-contact **skew inside that one command is unmeasured
and now says so**, in the driver, the MCP tool description and
`docs/drivers.md`: verification is a `PK` read-back of the landed state,
which cannot see timing, and the manual gives no per-relay switching time to
compare against. The previous wording would have been cited as evidence for
make-before-break ordering it does not support.

Two vendor specs surfaced while looking for that switching time, neither
recorded anywhere. `RELAY_MAX_SWITCH_HZ = 1.0` carries the spec table's
*1 CPS at full load* and the CAUTION that PhotoMOS dissipation rises with
switching speed — the ADU218 is explicitly not for PWM. It is **documented
and deliberately not enforced**, because the figure is qualified *at full
load* and nothing in USB, HID or the ADU command set reports what a contact
is switching, so a limiter would throttle the dry-contact sweeps that are
most of this bench's use on a condition it cannot observe. The inversion is
worth knowing: the ADU208's *mechanical* relays manage 10 CPS, so the
solid-state part is the slower one to cycle. The test pins the absence of a
limiter on **elapsed time rather than on "it returned"**, since a throttle
has two shapes and only one raises — a version that sleeps to pace the
writes leaves every state assertion passing, just slowly. A
`sleep(1/RELAY_MAX_SWITCH_HZ)` mutant held outside the module fails it at
8.00 s against a 2.0 s ceiling.

And on-state resistance, **700 mΩ typical / 1.1 Ω maximum** (Panasonic
AQZ207), added to F-27 — which corroborates that entry independently of this
bench. Every reading in the eight-relay walk is **15× to 41× the vendor
maximum**, so the relay accounts for under 4 % of even the lowest one. The
wiring conclusion had rested on "the spread has no relation to index", an
inference from a single bench.

**The whole-port command path now has an independent witness.** With the
operator opening the bench up for the all-eight sweep, that gated test ran for
the first time — and running it exposed that its two port-mask writes were
checked only against `relay_mask()`, i.e. the device reporting on itself. A
firmware that accepted `MKddd`, updated its own state word and never moved a
contact satisfied it completely, which is the one defect class the per-relay
path had been protected from since the first bench session.

The bench closed it for free: the two masks the sweep already writes,
`0b10101010` and `0b01010101`, are **complements**, so whichever relay the one
meter is clamped across, one mask closes it and the other opens it. A new
witnessed test asserts that, and asserts the two masks are still complements so
a later edit cannot silently reduce it to measuring one state twice.

Proven by mutation, and the first two attempts are the instructive part. A
bit-reversal mutant *was* killed — but at the pre-existing read-back assertion
(`assert 85 == 170`), not at the meter, so the witness contributed nothing:
two guards catching one fixture. Two further mutants leaked through
`reset_relays`, whose `MK000` takes its own `_send` path. The discriminating
mutant swallows only a **nonzero** `MKddd` at the `_send` seam while
`relay_mask()` returns the commanded value, leaving the safe state genuinely
reachable and no read-back anywhere able to see the lie: against it the
**existing sweep passes and the witnessed test fails**, on its own DMM
assertion, naming the cause. Ordering in the test is deliberate — the mask that
*opens* the metered relay is sent while seven others close, which is the
reading a state-word-only bug cannot fake.

The `SWEEP_ALL` gate was **kept**, not removed. This bench is safe and its
operator said so, which satisfies the gate; the skip is the repo's contract
with the next bench, where it may not be. Verified both ways: 13 passed / 0
skipped with the gates set, and the gate still skips with the line named when
unset.

The readings themselves are on file in
`tests/fixtures/adu218/whole_port_witness.txt`, because the test asserts
**categorically** — finite versus the overload sentinel, per F-27, since the
closed value is a property of the wiring — and a categorical pass does not show
how far from the boundary it sat. That capture carries a corroboration the test
cannot make: the bench walk read K7 at **17.50 Ω** through the *per-relay* `SKn`
path, and `MK170` puts the same contact at **17.5134–17.5152 Ω** over three
passes. Two independent command paths, one contact, the same resistance to four
decimal places — so `MKddd` closes the relay the way `SKn` does, rather than by
some other route that merely ends up reported as closed. F-27 records it as a
cross-check on its own eight-relay table.

### `benchctrl-mcp` never installed its configuration, so four of the five precedence levels were inert

`session.resolve()` is the local / remote / sim seam, and it reads a module
global that something has to populate. `session.configure_from_environment()`
is what populates it from the documented layers — and it had **no callers
anywhere in `src/` or `tests/`**. So `benchctrl-mcp` resolved every device
`local`, unconditionally. `BENCHCTRL_REMOTE`, `BENCHCTRL_TOKEN`,
`BENCHCTRL_SIM_DEVICES`, `BENCHCTRL_LOCAL_DEVICES` and
`~/.config/benchctrl/config.json` all parsed correctly, produced a correct
`Config`, and were then dropped. Only precedence level 1 —
`session.configure()` called from Python — ever worked, which is why every
test and every remote-mode demo passed: they all take that route.

**The failure mode points the wrong way, which is the reason this is a
safety fix and not a papercut.** Ask for `mode="sim"` and you got the real
instrument, silently, with no diagnostic — because the seam's whole purpose
is that the 324 tools cannot tell a simulator, a remote proxy and real
silicon apart. Someone stepping relays or switching mains while reading
"simulated" in their own shell history is the concrete hazard;
`KNOWN_LIMITATIONS.md § N-8` records it.

`mcp.install_config()` now runs before `mcp.run()`, and any non-local
binding is announced on **stderr** — an MCP server inherits no logging
configuration from its client and stdout is the JSON-RPC channel, so a
`log.info` would have gone nowhere. A malformed config is fatal rather
than a warning, since carrying on means driving hardware nobody asked for.

`main()` also now calls `session.shutdown()` in its `finally`, which
`session.shutdown()`'s own docstring had been instructing readers to do
("call this from any process-exit path that can arm hardware — see
`benchctrl.mcp.main`") while `main()` did not. The agent reads a clean
disconnect as consent to drop the writer claim and drive an armed device to
its safe state; without it, exiting the MCP server left that to the deadman.

The tests assert on what `session.resolve()` hands back rather than that a
function was called, and they are split by *route*: `load_env()` returns
`None` when no variable is set, so a fix that only handled the environment
would leave the config file just as inert. Both fail against the pre-fix
behaviour; the "with nothing configured, nothing changes" test passes either
way, correctly, since it does not depend on the wiring.

### `net/errors.py` — a constructor that accepts the message but does not store it

Found by that witness, over the real RPC wire: a `SDM4065AOverloadError`
crossing the agent link arrived with its **sentence doubled**, and its
`function` field set to an entire error message rather than a DMM function.

`_instantiate()` tries `cls(message)` first and treated *not raising* as
succeeding. `SDM4065AOverloadError(function, range_)` takes a string first,
so that call is perfectly legal — it files the whole message under
`function` and then composes a new message quoting it. Every strategy in the
cascade now has to store the message unchanged (`args == (message,)`) to be
accepted, so a re-composing constructor falls through to `__new__` instead.
The check is on `args` rather than `str()` because `KeyError.__str__` is
always a `repr` of its argument, and comparing text there would reject a
faithful `KeyError` and degrade it to `RemoteBenchError` — losing the type
this module exists to preserve.

The existing round-trip test asserted `f"{name} message" in str(exc)`, and
**containment is exactly what a re-composed message satisfies**, which is
how this shipped. It now asserts `args` exactly and that the decoded class is
still the class the agent raised, since a message check alone also passes on
a silent degradation. All 59 registered exceptions round-trip under the
stricter assertion; two did not before.

### Silicon Labs CP2112 — open-drain control lines for hardware reset

The bench can now assert and release a DUT's reset line with a ~$15 USB
board. During the i.MX8 Zephyr bring-up an Otii Arc Pro spent the whole
effort doing nothing but toggling a reset pin; this frees the SMU for
measurement, which is the only reason the driver exists.

Scope is narrow on purpose: **GPIO only, open-drain only.** SMBus is not
implemented despite being the chip's headline feature, and push-pull
output is unreachable through the public API rather than merely
discouraged. `set_line_mode` clears the push-pull bit on every call,
`set_line_asserted` refuses a push-pull pin, and a test asserts no public
method takes a `push_pull` parameter. Open-drain cannot source into a
target's reset net, and on unplug the chip reverts to inputs — which for
an open-drain reset line *is* released. Both are physical properties worth
enforcing rather than documenting.

The API says `asserted`, never high or low. Reset lines are active-low, so
"set the line high" is ambiguous exactly where a mistake holds a DUT in
reset indefinitely. `trigger_reset_pulse` releases in a `finally` so an
interrupt cannot strand a target, and `close()` restores the configuration
found at `open()` — which releases anything the driver was holding.

`allowed_lines` is a required keyword argument with no "all" default: the
driver cannot know what GPIO.3 is wired to. GPIO.0, GPIO.1 and GPIO.7
carry alternate chip functions and need an explicit override, and GPIO.7's
refusal stands regardless of the override if its clock output is actually
running — that being a fact the driver can read rather than a claim it has
to accept.

**One bench fact is worth reading before using this driver.** An undriven
CP2112 pin is high-impedance, so the chip's input buffer latches 1 while a
10 MΩ meter reads ~0 V on the same net. Both readings are correct, and
`read_levels()` therefore returns `0xFF` regardless of what is attached. A
pin is identified only by a level you can make *change*. This is why the
hardware tests are witnessed by a DMM rather than by the chip's own
read-back, which is downstream of the thing under test. See
`KNOWN_LIMITATIONS.md` §F-17.

Transport is `hidraw`, not usbfs — measured both ways on the board rather
than assumed: `usbhid` does claim the chip, and `hid-cp2112` is not built
for the Uno Q kernel so no in-kernel I²C adapter competes. GPIO commands
are HID *feature reports*, so the node needs `O_RDWR` even to read, and the
ioctl request numbers are computed with `_IOC` rather than hardcoded (the
payload size is embedded in the request number, so a constant lifted from
a 64-bit header is wrong on a 32-bit userland).

The udev rule is scoped to `10c4:ea90` specifically. A blanket
`SUBSYSTEM=="hidraw"` rule would also hand the bench user the attached USB
keyboard, which is a keylogging surface rather than a bench instrument;
`discovery.scan_hidraw()` filters to known signatures for the same reason.

10 MCP tools, prefixed `cp2112_`. None exposes
`allow_alternate_function` — a model cannot walk to the bench and confirm
a pin is idle, so offering it would leave the gate one plausible-sounding
argument away from bypassed.

Zero new dependencies: `os`, `fcntl`, `ctypes`.

### CyberPower PDU41002 switched PDU — over serial *or* SSH

The bench can now cut and restore mains: eight-outlet 120 V / 20 A
switched PDU, whole-device metering, per-outlet state and delays, and
per-outlet switching. It is the first driver whose device switches mains
power and the first with a network transport.

Reads landed first on purpose, so the parsers, the session handling and
the allowlist machinery were all proven on hardware before any method
could cut power. `allowed_outlets` gates every switch; nothing moves
implicitly — `close()`, `__exit__` and the governor's default safe state
all deliberately leave outlet state alone, because for a PDU (unlike
every other instrument) *cutting* power is itself the disruptive act.

`open(port=...)` selects the serial console, `open(host=...)` selects
SSH, and supplying both raises rather than silently preferring one:
which wire carried a mains switch has to be answerable from a run log.
`allowed_outlets` is a required keyword argument with no "all" default,
because a config typo has to fail closed on this device.

One CLI engine serves both transports, and that was measured rather
than assumed — `sys show`, `oltsta show`, `console show` and friends are
byte-identical across the two, and a `PDU41002Info` read over SSH
compares equal to the same read over serial. So there is one grammar,
one set of parsers and one simulator. **No SNMP** (disabled on the
device, and the CLI covers every capability) and **no new
dependencies**: serial is `pyserial`, SSH is a `pty.fork()` to
`/usr/bin/ssh`, so nothing needs a compiled wheel on a board with no
pip.

Four device behaviours drove design decisions that look odd without
them:

- **`oltctrl` acknowledges nothing.** A switch command answers with a
  blank line and a re-prompt, byte-identical whether or not the
  contactor moved. So `set_outlet_state` returns the **verified
  read-back** state rather than `None`, and the wait for it is sized from
  the outlet's own configured `td_on` / `td_off` (operator-settable, 3 s
  as shipped) rather than from a hardcoded number that would flake the
  moment someone raised it. `reset_outlet` returns `None` and says why:
  a reboot ends where it started, so no read-back can distinguish
  "cycled" from "never moved". Testing this needed a simulator that can
  *lie* — hence `ignore_switches`, which acknowledges a switch and moves
  nothing.
- **The CLI is single-session across the whole device**, and the
  incumbent wins. A second login *completes* — banner and all — and is
  then hung up, so the failure is indistinguishable from a bad password
  unless the driver names it. Hence a distinct `PDU41002SessionError`
  that survives the RPC wire. Consequence: `close()` **must** send
  `exit`, because CLI session state outlives the serial port and a
  driver that merely closes its port leaves the PDU unreachable from
  the other transport. This is the only `close()` in the repo with a
  required side effect on the device.
- **There is no interrupt character.** `\x03` is echoed and taken as
  part of the command; a bare `CR` is the resync. This one was caught by
  the simulator faithfully reproducing the hardware rather than
  agreeing with the driver.
- **SSH needs three non-default flags**, each fixed by measurement:
  group-*exchange* KEX fails on firmware 1.3.4 (force
  `diffie-hellman-group14-sha256`), the device offers only
  `keyboard-interactive` so pubkey auth is refused and a pty is
  mandatory, and the exported host key is all zeros so host-key
  verification is worthless — pinned to `/dev/null` rather than
  poisoning the operator's `known_hosts`.

**Three more behaviours were found by the hardware tier itself**, and are
worth separating because of how they were found: every one had a
simulator agreeing with the driver's misreading, so the whole
hardware-free suite passed while the driver was wrong. The tier is
`tests/test_hardware_cyberpower_pdu41002.py`, run with
`pytest -m hardware -k pdu41002` against a real PDU over both transports.

- **`Login Failed` is ambiguous by construction.** The login has *four*
  outcomes, not two, and the fourth — ~15 s of dots, `Login Failed`, then
  silence with no re-prompt — is emitted **byte-identically** for a wrong
  password and for a *correct* one submitted within ~15 s of a previous
  session closing. The single-session limit above makes that routine, so
  the driver now retries within a 75 s budget rather than classifying,
  and only calls it `PDU41002AuthError` once the budget is spent, naming
  both possibilities. Fixing this also meant fixing the simulator, whose
  tidy `Login Name :` re-prompt on a wrong password is something no
  firmware does — and the tidiness is precisely what hid the bug.
- **A device that never answers was reported as a bad password.** The
  post-password read window was derived from the remaining budget with a
  2 s floor, and *any* missing prompt was read as a rejection, so a slow
  serial login surfaced as "check `BENCHCTRL_PDU_PASSWORD`" — the exact
  misdiagnosis this driver's error types exist to prevent. Now a separate
  `_AUTH_WAIT_S` floor and a three-way verdict: prompt, explicit
  rejection, or a `PDU41002TimeoutError` saying the password was *not*
  rejected and the device did not answer.
- **An idle SSH session is dead, not logged out.** The idle timeout is
  5 minutes on this unit rather than the manual's 3, and the two
  transports need opposite handling: serial keeps the port and drops to a
  login prompt, so the driver re-authenticates in place, while SSH is
  disconnected at ~180 s and the ssh client exits, leaving nothing to
  re-authenticate on. That arrived as `no prompt after 'sys show' within
  12.0s (got 130 bytes)` — a slow-device symptom inviting a retry that
  could never work. Now the client's disconnect notice is recognised and
  raises `PDU41002ConnectionError` naming the reopen as the recovery.

  ssh has **two** wordings for that event, and the second is byte-shared
  with the single-session hangup — so recognising only the first traded
  the timeout for `PDU41002SessionError`: *"another session is logged in —
  send 'exit' on it"*, when nothing else was logged in and that advice
  cannot be followed on a dead link. Wording cannot separate them and a
  longer list would rot; **position can.** Those markers only mean
  contention *during login*, so they are matched post-login as
  `_LINK_GONE_MARKERS` in `_round_trip` and `_raise_if_hungup` is no
  longer called from `_cmd`. The hardware-free test is parametrised over
  both wordings, because the one-shape version passed while the device
  failed.

Each of the three is pinned hardware-free by a test that kills a mutant
reproducing the hardware symptom verbatim, and each measurement lives in
a code comment rather than only in a session log. See F-15 and F-16 in
`KNOWN_LIMITATIONS.md`.

**First device in benchctrl that needs a secret.** It is read from
`BENCHCTRL_PDU_PASSWORD` in the environment of the process that talks to
the device, never from config and never from `open_kwargs`:
`DeviceConfig.to_dict()` writes `open` back verbatim, and the RPC wire
is HMAC-authenticated but plaintext, so either path would have leaked
it. `DeviceConfig.to_dict()` now also masks `password` / `passphrase` /
`secret` keys inside `open` as `***`, which makes config round-tripping
lossy for those keys by design. The MCP `pdu41002_open` tool has no
password parameter at all — absent, not defaulted, so a model cannot put
a credential where it would be logged in a transcript.

Method names are constrained by the agent's dispatch gate, which derives
mutators purely from name prefixes: an `outlet_on()` would have been
remotely callable *without a writer claim*, i.e. mains switching
bypassing the claim gate. So the three switching methods are
`set_outlet_state`, `reset_outlet` and `clear_outlet_command`, the
mutator set is pinned by exact-equality tests in both directions
(locally and against the live agent surface), and adding `"outlet_"` to
the prefix list was rejected because it would also capture the reads.

Aggregate targeting is impossible structurally rather than by validation
— `oltctrl index all act off` is one line that de-powers the whole bench.
Two independent guards: coercion rejects anything that is not a plain
in-range `int` (catching a bad *argument*, `bool` before `int` so `True`
cannot become outlet 1), and every rendered command is matched against a
single-index whitelist regex before a byte is written (catching a bad
*rendering*, which coercion cannot see). `menumode` and
`console telnet enable` are unreachable by the same regex — the first is
a one-way trap that breaks every parser, the second would disable SSH
from underneath a running test.

Self-protection here is a **cabling invariant, not a software
guarantee**: the PDU powers bench instruments and DUTs only, with the
agent host and network gear deliberately not plugged into it. That is
written down in `docs/drivers.md` as a deployment assumption, because it
is what makes a governor-triggered cut safe to support at all.

Fixtures under `tests/fixtures/pdu41002/` are verbatim transcript
excerpts with firmware version and capture date, not text written from
the manual — a simulator built from the same misreading as the driver
agrees with it. `deploy/udev/62-benchctrl-ftdi.rules` binds
`/dev/benchctrl/pdu41002` to the adapter's serial number, since
`ttyUSB0` is enumeration-order and the driver must not open whichever
cable happened to come up first.

**The governor's exemptions are now deliberate rather than accidental.**
`default_safe_state()` was already inert on the PDU — it implements none
of the four methods that function tries — so the omission is now written
down with its reasoning, because "fixing" it would turn one lost
heartbeat into a bench-wide power cut. `set_outlet_state` is likewise
absent from `_ARMING_CALLS` on purpose: energising an outlet is not
arming an output, and treating it as one would start a deadman countdown
on every switch *and* mark the PDU permanently armed with nothing able to
disarm it.

Those two exemptions mean the PDU never appears in `armed_devices`, so
the opt-in cut needed its own path or `panic_outlets` would have been a
setting that silently did nothing — an inert governor, indistinguishable
from a working one. `trip()` now also walks devices that authorised a
cut, *after* the armed devices (pulling mains from under a live output is
how an inductive kick reaches a DUT), confirms each outlet by read-back,
and reports FAILED rather than SAFE when it cannot. It gets a budget
derived from the outlet count instead of the half second instruments get,
because a contactor honouring a 3 s `td_off` physically cannot report
itself off in time and every trip would otherwise escalate to a transport
reset. Nothing armed still means nothing cut.

**A phase can now power-cycle a DUT.** `safety.allowed_outlets` and a
phase `setpoints.outlets` mapping bring mains into the declarative layer,
so cold-boot, brownout-recovery and hung-DUT tests are specs rather than
someone standing at the bench.

Adding fields to the spec had one trap worth naming: `RunSpec.sha256`
hashes canonical JSON and is the only thing tying an archived result
bundle to the spec that produced it, so a key emitted unconditionally
would silently re-hash **every spec ever archived** — including runs with
nothing to do with mains. Both new fields are emitted only when used, and
a test pins the hash of an outlet-free spec against a hard-coded value
captured before any of this existed. Outlet keys normalise to sorted ints
on construction, because JSON object keys are strings and `{3: true}`
would otherwise hash differently after a round trip.

Empty `allowed_outlets` means **none**, not unrestricted, and it is
checked in both directions — de-powering a DUT is as much a state change
as energising one. Aggregates stay inexpressible (`all`, `b1`, `b2`
cannot be spelled) and `bool` is refused before `int`, matching the
driver.

Four engine behaviours, each the answer to a way this could look fine and
be wrong:

- **A failed switch fails the phase.** The engine goes through
  `set_outlet_state(..., verify=True)` and lets the exception end the
  run. Since `oltctrl` acknowledges nothing, the alternative — log it and
  carry on, in a process nobody is watching — produces a bundle full of
  data describing a power cycle that never happened.
- **A spec that switches outlets with no PDU attached is refused at
  construction**, before the run directory exists. Skipping the switch
  and completing is the same failure with a worse signature.
- **Mains first, then the instrument setpoint.** A setpoint applied to a
  de-powered DUT lands nowhere; energising mains under a live output is
  how an inductive kick reaches a DUT. The governor's trip path is the
  same ordering in reverse.
- **`settle_s` defaults to 3 s, not 0**, so the opening samples of a
  power-cycle phase are not a supply settling and a DUT booting. It is
  `Optional` rather than a float defaulting to zero because absent and
  `0.0` mean opposite things here, and conflating them would make the
  safe default unreachable. It counts against `max_duration_s`, is
  refused on a phase that switches nothing, and an abort arriving during
  one does not wait it out.

A run never cuts mains on its own way out — phase ends, aborts and errors
all leave outlets where the last phase put them. Every transition is a
`run_outlet` event carrying the verified read-back state, not the
requested one.

Remotely this needs the writer claim on **both** device keys. Without
that check `run.submit` would have been the one path to a mains contactor
that skipped the gate every other route enforces; the refusal names the
key to claim. A spec that mentions no outlets needs no PDU claim, even on
a bench that has one.

**MAINS.MGR on the FUI, and the PDU is deliberately not on the instrument
rail.** It is core harness, like the Arduino hosting the agent — not
something a run measures with, but the thing deciding whether the rest of
the bench has power. A rail row would have described mains switching in
the rail's vocabulary (`ARMED`, `IDLE`, the arm counter), and "outlet 3 is
energised" is not an armed output. The exclusion is explicit because the
rail's membership rule appends any key it does not recognise as an unknown
instrument, so serving the PDU put it on the rail immediately: the default
was the wrong treatment. A test pins it, and the panel's words are
asserted disjoint from the rail's.

The panel sits bottom-left with voltage, frequency, load and per-outlet
state. Everything about it follows from one constraint: the dashboard is an
observer session and `device.call` is not an observer method, so a display
**cannot** read the PDU — reading one means write-grade access to the
device that switches mains. State arrives only because the bench pushes
it: a periodic `mains` sweep in the agent, plus the engine's `run_outlet`
events folded in between sweeps. The sweep publishes every time and logs
only a *changed* one, following the presence sweep, and it never opens the
PDU itself — a display connecting must not be why the bench logs into a
mains switch.

Which means there is **no correcting poll**, so the panel keeps its own
clock: a sweep older than 30 s (three intervals) is flagged aged-out. The
global `STALE` banner cannot cover this, because status frames keep
arriving while the sweep is dead — a fault only this panel can detect.

Three ways to have nothing to show, kept apart because two are identical
in the data and the operator's next move differs: `NO PDU` (no mains
control on this bench — the panel hides itself, and it is the only
self-hiding panel), `NOT REPORTED` (served, unheard: ordinary on an idle
bench *and* exactly what a broken sweep looks like), and a live reading,
where `DUT SETTLING` outranks `MAINS LIVE` as the more specific claim about
the same instant.

Staleness degrades the opposite way to the rail's, and that asymmetry is
the point. A stale `ARMED` over-warns, which is safe; a stale outlet `OFF`
reads as "the DUT is de-powered" to somebody deciding whether to reach into
an enclosure. So a stale port keeps its live colour and is dimmed, never
struck through and never recoloured; the outlet map is dropped outright on
disconnect and a reconnect does not restore it; only outlets actually
reported get rows; and the energised count is summed from the rows drawn,
so the tally cannot disagree with the screen. `.port.on` is amber rather
than red — all eight outlets are normally on, and spending red on the
steady state would devalue the one red reserved for an armed output.

`tests/test_mains_end_to_end.py` closes the gap the unit tests structurally
cannot. Mains has two publishers in modules that share no code
(`server._mains_sweep` and `RunEngine._switch_outlets`) and a consumer that
imports nothing from `benchctrl.agent` by design, so a renamed event key is
a compile-clean silent blanking with no correcting poll to recover from it.
These nine tests run the whole stack — production driver over a pty, worker,
agent, HMAC wire, real observer session, shipped `build_view`, fetched over
HTTP from the URL the browser uses — and assert only against the display.
Verified by mutation: renaming one published metering key fails here and
nowhere else in the suite, as does putting the PDU back on the rail or
letting the sweep open the PDU on a display's behalf.

### Verifiable source sync to a board (`deploy/sync-board.sh`)

Development tooling, not part of installation. It exists because the FUI
reached the board twice as pre-review software: files went over one at a
time with `scp`, the panel came up, and every surface check passed. The
deployed `state.py` had none of the observer-role safety check. The root
cause was not carelessness — it was that **"deployed" was a claim nobody
could check.**

So the copy is the easy half. The half that was missing is that afterwards
a `sha256` manifest is taken on each end and compared, and the output
either says `IN SYNC — N files identical` or names every file that
differs. `--check` verifies without changing anything, because most days
the useful question is "is the board current?"

On its first real run (2026-08-20, repo at `3cfb8dc`) it found **16 files
of genuine drift** on a board believed to be current — 14 differing, 2
never delivered. Each differing file matched an older commit in
`git log --all`, so the board was running stale deploys rather than local
edits.

Three files: `sync-board.sh` (workstation side),
`board_sync_manifest.py` (the comparison; stdlib only, so it runs on a
board with no pip and on a tree whose `benchctrl` package is broken —
diagnosing a bad deploy is exactly when you need it), and
`board_apply_sync.sh` (the board side). The last is a separate file rather
than a string inside an `ssh` argument because it is the only destructive
code here, and inline it was neither lintable nor testable: `sh -n` on the
outer script saw one string literal, so an unbalanced quote inside it
passed while failing on the board.

What bounds the damage, and why each guard is enforced rather than
documented:

- The board's `src/` holds **vendored dependencies** beside our package —
  `serial`, `usb`, `pyvisa`, `pyvisa_py` — because it has no pip. They are
  not in this repo, so they are exactly what the stale-file sweep would
  delete, taking the agent and the bench down. `PACKAGE` is validated as a
  single plain directory name in all three files.
- `REMOTE_SRC` must end in `/src`. `/home/arduino/benchctrl`, one
  component from the default, is the agent's live blob and runs directory.
- `check_sync` checks the exit status of every command. It is called from
  an `if`, which suspends `set -e`, so an unchecked failure leaves an
  empty manifest — and two empty manifests compare as *in sync*. A
  zero-line manifest is refused outright as a second, independent
  backstop.

Nothing is restarted. Bouncing the agent disconnects instruments and
restarting lightdm blanks the panel; both need root, which this never
asks for. It does say two things it will not act on: a running agent ends
up on a **mix** of old and new modules (drivers import lazily, so it picks
up new driver files while keeping the old core), and killing the FUI would
not work anyway — `benchctrl-kiosk` `exec`s the browser, discarding its
cleanup trap, so nothing respawns the dashboard.

Known blind spots, stated because a verification tool that overstates its
coverage is worse than none:

- **The push is not atomic.** `tar` extracts member by member into the
  live tree, so a dropped connection or a full disk leaves the mixed tree
  this exists to eliminate. `set -eu` aborts before the delete sweep
  rather than sweeping against a partial extraction, and a trap prints
  "the board WAS modified and was NOT verified" on any failure after the
  push begins — but the window is real.
- **`--check` does not delete, so it cannot fix stale `__pycache__`.** A
  board carrying it reports `IN SYNC`, correctly: that bytecode is
  excluded from the comparison by design, because the board's 3.13 and the
  workstation's 3.12 never agree. Legacy-location `.pyc`
  (`benchctrl/panel.pyc`) *is* compared and swept — that is the form PEP
  3147 imports with no source present, verified both ways on 3.12.
- CI lints and type-checks `src`, not `deploy/`. The only thing holding
  `board_sync_manifest.py` to the 3.9 floor is `tests/` loading it by path
  on every leg of the version matrix.

### Read-only bench status display on the board's HDMI panel

The board boots straight into a full-screen status console instead of an
X login prompt it has no keyboard to answer. It shows what the bench is
doing — armed instruments, run stage, recent events, staleness — and
cannot command anything.

Everything about it falls out of one requirement: **it must be impossible
for the display to block or influence bench operation.** That ruled out
the obvious designs, and it closed a hazard that was already live.

`Governor.trip()` emitted its `safety_trip` event *before* driving
instruments safe, and that event walked every session calling
`sock.sendall()` synchronously, on the deadman thread, with no send
timeout. One client that stopped reading — a wedged browser, an unplugged
panel — could stall inside the governor and **delay disarming an armed
instrument**. The `except Exception` guard did not help: it catches a sink
that raises, and this is a sink that blocks. An always-on HDMI panel is
the client most likely to wedge, so building on that fan-out would have
turned a rare hazard into a routine one. `benchctrl.agent.eventbus` makes
the fix structural — a producer calls `offer()` and never touches a
socket — with per-subscriber bounded queues, priority *eviction* rather
than queue-jumping (so order is never permuted), and drops announced
in-band as `events_dropped` rather than silently swallowed.

Polling was also out, and for a sharper reason: the agent calls
`governor.touch()` on **every** inbound frame, so a panel polling once a
second would pin `seconds_since_contact` near zero and the deadman could
never fire. The display that exists to report an unsafe state would be
causing it. So the feed holds an **observer** session, whose traffic does
not count as operator contact, and `OBSERVER_METHODS` allowlists what it
may call at all. `run.abort` is deliberately excluded.

Asking for that role is not the same as having it, and only the agent can
enforce it, so `BenchStatus` now checks the role the agent grants back in
its `WELCOME`. A missing or false flag latches `NOT OBSERVER` (severity
`critical`, above `STALE`), marks the view untrustworthy, and says
`DEADMAN MAY NOT FIRE` in plain words — a downgrade detector against an
older or regressed agent, where the readings look perfectly current while
the deadman is held open.

The display never shows a value it does not have. A slot shows a real
reading or the literal `NO LINK`; the scope says `NO SIGNAL`; stale
numbers are struck through rather than held on screen; inferred state is
dashed; an unreachable server drops a curtain over everything. Decorative
texture is synthetic and unmistakably so, and is kept out of the event
log, which is bench truth.

`benchctrl.dashboards.fui` renders it from `http.server` plus three static
files on the system python — no venv, no third-party packages, nothing
installed on the board's 84%-full root filesystem. The Streamlit panel it
replaces is removed, along with `install-dashboard.sh`,
`benchctrl-dashboard` and `board_render_check.py`; Streamlit remains an
optional dependency for `applications/sensor_profiler`.

Keeping the display cheap turned out to need measurement rather than
instinct. On the board it cost 138% of a CPU core, and almost all of it
was one CSS element: a full-viewport `position: fixed` scanline, 48% of a
core on its own — more than the hologram, traces, glow, grid and polling
combined. Shrinking it, removing its blur, `will-change` and
`contain: strict` each changed nothing; only not spanning the viewport
did. A frame governor backs that up by degrading frame interval, canvas
resolution and glow under load, and it is measured as achieved-vs-
requested frame interval because the first version timed the draw calls —
a number that is structurally always zero, since canvas rasterisation is
off-thread. It looked correct and did nothing. The governor only ever
degrades decoration: the data clock is a separate timer, so the frame rate
may collapse and the data rate may not.

`deploy/install-fui.sh` installs the display; `deploy/install-kiosk.sh`
makes the board boot into it via lightdm autologin. The kiosk installer
removes the only local login on a keyboard-less board, so it refuses
unless ssh is active and the display is already installed, configures
lightdm through a drop-in so recovery is deleting one file, and has
`--undo`. See `docs/dashboard.md`.

The e-stop that will share this panel is **not** in this change. The
button is ordered; the physical path deliberately bypasses the display,
the network and the event bus, because a hardware interlock has to work
when everything else is broken. Four design questions are recorded
unanswered in `docs/dashboard.md`.


### Serial transport selection — kernel driver first

`benchctrl.transports.autoserial` picks how to reach a CH340-based
instrument instead of making the operator know which host they are on.
The userspace CH341 driver shipped earlier as a workaround for kernels
built without `CONFIG_USB_SERIAL_CH341`, but nothing chose between it and
the kernel's own driver: `open_ch341_pty()` had exactly one caller in the
repo, a manual verification script. Whoever wrote the config had to know
whether to name `/dev/ttyUSB0` or start a bridge — so a config that worked
on desktop Linux was wrong on the Uno Q and vice versa.

Precedence is fixed: an explicitly named port wins and probes nothing, a
kernel-bound tty for `1a86:7523` beats userspace, and the userspace driver
runs only when the kernel bound nothing. The kernel driver wins where it
exists because it is battle-tested, survives suspend/resume and costs no
Python thread; ours is a workaround and should not win by default. A host
that later gains a `ch341` module starts using it with no config change.

A **failed** open does not trigger fallback. If a kernel tty exists and
opening it fails, that raises: sliding to userspace would turn "another
process holds the port" or "the cable is unplugged" into a different,
working transport measuring something else — rule 4's silent fallback, at
the transport layer. Transport is chosen by what the host *has*, never by
what failed.

Bridge lifetime is bound to the driver, so `registry.close()` and every
existing teardown path release the USB claim without knowing a bridge
exists. It is closed even if the driver's own `close()` raises — a driver
that fails to close still has to give the chip back, or the next open
finds it claimed by a dead handle.

The agent's opener and the `qr10x_open` MCP tool both route through this,
so remote and local behave identically and `port` now defaults to
`"auto"` rather than `"COM7"`.

#### Discovered

**A driverless CH340 was invisible to discovery.** `discovery.scan_serial()`
enumerates `list_ports.comports()`, which lists *ttys* — and the entire
problem on a kernel without `ch341` is that no tty exists. So the QR10x was
absent from the Uno Q's inventory, which reads as "not plugged in" when it
is plugged in and working. `scan_driverless_bridges()` closes that blind
spot, reporting the adapter with `path="auto"` (the honest answer: there is
no node until the bridge creates a pty, and `"auto"` is also what to pass as
`port`). It suppresses itself when the kernel *has* bound the adapter, so
nothing is double-reported.

**Verified on real hardware** — QR101A-1M-R1, serial 00000248, on the Uno Q
bench board. The userspace branch is selected correctly, the driver reads
its identity through it, and an open → close → reopen cycle succeeds, which
is what proves the USB claim is released rather than leaked. Discovery
reports the adapter where it previously reported nothing.

#### Known issues

- **The kernel-first branch is test-verified, not silicon-verified.** No
  host on this bench has a kernel `ch341` to exercise it against: the Uno Q
  is built without it, and WSL has no CH340 passed through. The precedence
  logic is covered by `tests/test_autoserial.py`, including a mutation check
  that each branch fails when inverted, but "kernel tty is preferred" has
  not been observed on real hardware that has one. Scoped for revalidation
  in `ROADMAP.md` § *Revalidate serial transport selection on a desktop
  Linux host*, with the negative case (a busy kernel tty must raise rather
  than fall back) called out as the one that matters most.
- **`serial_number=` selection is unusable on our adapter, and untested
  generally.** This CH340G reports `iSerialNumber=0` — no serial-number
  descriptor — so there is nothing for `CH341Device.open(serial_number=...)`
  to match, and `index=` is the only way to pick among several. Other CH340
  variants do carry one. Multi-adapter selection has never been exercised on
  hardware either way: only one CH340 has ever been attached to this bench.
- **Only the CH340G (`1a86:7523`) is handled.** The CH9102 (`1a86:55d4`)
  uses a different register layout, and other bridges (FTDI, CP210x,
  PL2303) are untouched — they are only ever reachable through their kernel
  drivers. `autoserial` is CH341-specific by name and scope.

### Siglent SDM4065A — 6½-digit bench DMM

A fifth instrument, and the first *measurement-only* one: it sources
nothing, so unlike the load and the supply there is no output an agent
can accidentally energise. The failure mode to guard against is a
plausible wrong number, and the driver is shaped around that.

Speaks USB-TMC via pyvisa (LXI works too — pass a `TCPIP::` resource).
54 MCP tools, full remote support, a simulator, and 154 tests across
three files: 118 driver (14 hardware-marked), 15 QR10x cross-validation
(7 hardware-marked), and 21 remote-path. DC/AC volts and amps, 2- and
4-wire resistance, capacitance, frequency, period, continuity, diode,
temperature.

**Validated on real hardware** — serial SDM46A0CA00021, firmware
0.0.0.20, over USB-TMC from the Uno Q bench board: 13 passed / 1 skipped
in the driver suite, **7 passed / 0 skipped** in the QR10x
cross-validation, 0 failed. Both 2-wire and 4-wire paths are silicon-
verified.

The 4-wire path produced the measurement the docs had been quoting the
datasheet for. One 100 Ω QR10x setpoint, read both ways on the same
leads: 2-wire 100.12209 Ω, 4-wire 100.04321 Ω, against the QR10x's own
PV of 100.03800 Ω. **Lead and contact resistance is 78.9 mΩ** on this
bench — inside the datasheet's 0.2 Ω bound, but still 2x the 38 mΩ
offset the cross-validation is trying to resolve, so the standing
conclusion holds: an unnulled 2-wire reading cannot see that offset. The
4-wire reading lands 5.2 mΩ from the QR10x's own measurement where the
2-wire reading is 84 mΩ away.

`TWO_WIRE_LEAD_OHM` deliberately stays at the datasheet's 0.2 Ω rather
than being tightened to 78.9 mΩ. Lead resistance is a property of the
cables and contacts, not of the meter, so a tolerance pinned to our
bench would fail for anyone with longer leads without indicating a
driver defect. The measurement is evidence; the bound stays a bound.

The driver suite's remaining skip is structural, not a gap: the overload
test needs an **open** input and the null test needs a **connected**
one, so exactly one of the pair always skips. With the 100 Ω DUT
attached it is the overload test; both skip messages name the input
state so the log says which case ran.

Three quirks drove the API shape, all from the SDM4000A remote manual
and all verified by hand against the extracted text rather than taken
on trust:

- **`MEASure:<fn>?` is `CONFigure` + `READ?` in one command**, and
  `CONFigure` resets that function's NPLC, null state, null value
  *and* range to defaults. So `null_now()` followed by
  `measure_resistance()` silently discards the null — the most natural
  call sequence is the broken one. `null_now()` therefore samples with
  `READ?`, and `read_nulled()` exists to raise rather than quietly
  return an un-nulled number when no null is active.
- **Enabling `NULL:STATe` arms `NULL:VALue:AUTO`** (§7.4.2), which
  makes the instrument overwrite the offset with its own next reading.
  Correct order is state-then-value; the natural value-then-state order
  nulls by the wrong number, and a test pins that the naive ordering
  really does fail. §7.4.3 promises that writing a value disarms AUTO —
  on this unit it does not, so `null_now()` disarms it explicitly (see
  *Discovered* below).
- **The range readback is ambiguous.** §7.4.5's "default 2 kΩ" turned
  out not to describe the reset state at all (see *Discovered* below);
  what matters for the API is that `RANGe?` reports the same number
  whether the range was pinned or merely selected by autorange, so
  `get_autorange()` is required to interpret it. Accuracy work must pin
  the range explicitly rather than trust a post-`CONFigure` readback.

Model-family traps, since one manual covers the SDM4045A/4055A/4065A:
the 4065A tops out at **1 MΩ** where the 4055A has 2 MΩ, and accepts
six NPLC values (100/10/1/0.1/0.01/0.001) where the 4055A accepts
three. Both are validated against the 4065A column and the error
message names the sibling model, because a constant taken from the
wrong column produces a driver that works and reports plausible
numbers. Resistance autozero is 4065A-only and defaults **off** — but
under a different SCPI node than §7.4.7 documents, see *Discovered*.

The `9.9E37` overload sentinel raises `SDM4065AOverloadError` rather
than being returned — it is a valid float and would otherwise
propagate into arithmetic as a believable reading. That is a fifth
exception type where every other driver has four
(Connection/Command/Timeout/Value); "the input exceeded the range" is
a distinct recoverable condition, and a caller widens the range and
retries on it.

Siglent documents SCPI headers **without** the leading root colon
(`SYSTem:ERRor?` where Rigol writes `:SYSTem:ERRor?`). The simulator
aliases both forms: without that, `ScpiDevice`'s `:SYSTem:ERRor`
registration would not match, the query would fall through to the
generic register lookup, and `last_error()` would answer `0` — a
silently clean error queue, the one failure a driver cannot detect.

`discovery.SIGNATURES` gains the Siglent VID/PID (0xF4EC/0x1220) so
the meter appears in the bench inventory the remote agent reports, not
just to the driver's own VISA scan. The signature identifies the
*family*; its `note` says to read `*IDN?` for the model. The VID/PID is
confirmed against the physical meter (`lsusb` on the bench board), not
taken from the datasheet.

Two hardware-marked test sets ship with it, both now run against the
physical meter:

- `test_bench_siglent_sdm4065a.py` proves the manual quirks on silicon
  rather than against the simulator I wrote from the same manual — a
  circularity worth closing, and closing it is what found the four
  firmware/manual defects below — plus NPLC/range coercion, autozero's
  OFF default, and the overload sentinel.
- `test_cross_validate_sdm4065a_qr10x.py` measures one physical
  resistance with both instruments. Its tolerances are derived from the
  two datasheets in-file and pinned by hardware-free tests, and it is
  explicit that the QR10x's ±0.05% dominates: the pair resolves *gross*
  errors (units, scaling, swapped 2-/4-wire, a null with the wrong
  sign), while only the meter alone resolves the 38 mΩ offset.

Running them needed a deployment fix first, not a driver one — see
`KNOWN_LIMITATIONS.md` § F-6 and the new
`deploy/udev/61-benchctrl-usbtmc.rules`. On a kernel with no `usbtmc`
module, pyvisa-py drives the instrument over libusb and needs write
access to `/dev/bus/usb/BBB/DDD`; without it the meter is *invisible*
rather than unopenable, because pyvisa-py cannot read the string
descriptors it needs to build a resource name. `discover()` returns
`[]` and the driver says "no SDM4065A found" — indistinguishable from
an unplugged instrument. The rule covers both Rigols too, which had the
same latent problem.

#### Discovered

Running the suite against silicon found **four defects in the SDM4065A
firmware or its manual** that reading the manual could not have found.
All four are written up as sendable vendor reports in
[`docs/vendor-issues/`](docs/vendor-issues/SDM4065A-firmware-bug-reports-README.md),
measured on serial SDM46A0CA00021, firmware 0.0.0.20. The two that
change how any code must talk to this meter also have limitations
entries: `KNOWN_LIMITATIONS.md` § F-7 (error queue) and § F-8 (the
undefined-header wedge).

Three of the four share one failure mode, which is why they were worth
the effort to isolate: **the instrument keeps returning plausible
numbers while a setting is not what the caller believes it is.**

1. **`NULL:VALue` does not disarm `NULL:VALue:AUTO`**, though §7.4.3
   says it does. So the instrument overwrites the offset with its next
   reading and the null becomes a no-op that leaves every result
   *looking* nulled. `null_now()` now disarms AUTO explicitly instead of
   relying on the documented side effect.

2. **§7.4.7's `AZ` autozero mnemonic does not exist.** Every spelling
   (`RESistance:AZ`, `:AZ:STATe`, `SENSe:`-prefixed, `AZERo`) is
   rejected with `-113` for *writes as well as queries*, on all four
   functions. The node that works is **`ZERO:AUTO`**, which round-trips
   correctly — so autozero is fully controllable and only the manual is
   wrong. Finding this cost two power cycles, because *querying* an
   undefined header returns nothing, the read times out, and the aborted
   USB-TMC transfer strands the bulk endpoints. Neither USBTMC
   `INITIATE_CLEAR` (which reports `STATUS_SUCCESS`) nor a libusb port
   reset recovers it — only a front-panel power cycle. That wedge is
   reproducible from *any* aborted read, including a legitimate slow
   measurement that outruns a timeout, and is the most serious thing in
   the four reports.

3. **`*CLS` does not clear the error queue**, which IEEE 488.2 §10.3
   requires. Entries survive `*CLS`, survive `*RST`, and one survived
   closing and reopening the VISA session; only reading removes them.
   Undrained, the queue fills and answers `-350 "Queue overflow"` to
   everything, so an error check reports a stale failure from an
   unrelated command. Worse, the queue can then latch into answering
   `0,"No Error"` permanently — measured, it denied a deliberately bogus
   header that `*ESR?` correctly flagged. `*ESR?` stayed accurate
   throughout, so `command_error()` reads **`*ESR?` bit 5** as the
   authoritative "was that rejected?" and `clear_status()` drains the
   queue rather than trusting `*CLS`.

4. **The documented default resistance range is not the reset state.**
   §7.4.5 says 2 kΩ; measured, `*RST` and a bare `CONFigure:RESistance`
   both leave autorange **on** with `RANGe?` reporting 200 Ω. The 2 kΩ
   figure describes the `DEF` *parameter*, a different thing, and §7.4.6
   separately documents autorange as defaulting ON — the two sections
   contradict each other. Probing it also found a firmware
   inconsistency: `RESistance:RANGe DEF` disables autoranging as
   §7.4.5's own note promises, while `CONFigure:RESistance DEF` leaves it
   on. `DEFAULT_RESISTANCE_RANGE` is therefore 200.0, paired with
   `DEFAULT_RESISTANCE_AUTORANGE`, and the driver never sends `DEF`.

The simulator models all four **as measured, not as documented** —
including rejecting the `AZ` mnemonic and leaving the error queue dirty
after `*CLS`. A simulator more standards-compliant than the instrument
is the one kind of simulator bug a passing test cannot catch: the driver
looks correct until it meets the hardware.

#### Known issues

- **The bench unit's error queue is currently latched silent** (defect 3
  above). Measurements are unaffected and the suite handles it —
  `test_hw_error_reporting_is_reachable` warns rather than fails, since a
  silent queue is the instrument misbehaving and not the driver — but a
  power cycle is needed to restore error-code reporting. `command_error()`
  works either way.
- **`*ESR?` gives no error code.** It reports *that* a command was
  rejected, not which error it was. Diagnostics needing the code
  (`-113` vs `-224` vs `-222`) still depend on the error queue.
- **Autozero timing is not characterised.** The driver can set and read
  the state, but the settling cost of an autozero cycle at each NPLC has
  not been measured.
- **Two-instrument agreement resolves gross errors only.** The QR10x's
  ±0.05 % dominates the meter's accuracy, so the cross-validation budget
  at 100 Ω is ~0.07 Ω — wider than the 38 mΩ offset and comparable to the
  79 mΩ of lead error itself. It catches units, scaling, a swapped
  2-/4-wire function and a null of the wrong sign; it cannot certify
  either instrument. The lead-resistance measurement escapes this because
  it differences two readings from the *same* meter, cancelling the QR10x
  term entirely.

### `deploy/` — the agent as a systemd service

`docs/remote.md` prescribed an `ExecStopPost=... --safe-stop` unit but
shipped no unit file. `deploy/` now holds the real thing plus
`install-agent.sh`, which generates a token, writes
`/etc/benchctrl/agent.json` at 0640, installs the unit and verifies
the import path *before* touching systemd — a wrong `PYTHONPATH`
otherwise surfaces only as a unit flapping every 5 s.

`ExecStart` invokes `python3 -m benchctrl.agent.main`, not the console
script the docs suggested: the target board has no pip and reaches the
package through `PYTHONPATH`. `PYTHON=` covers the venv case.

The unit now also reads a **second, optional** environment file,
`/etc/benchctrl/agent.secrets.env` at 0600, which is where
`BENCHCTRL_PDU_PASSWORD` lives on a bench board. It could not go in
`agent.env`: that one is 0644 on purpose, because it carries `PYTHONPATH`
and an operator should be able to read it without sudo. systemd reads
both as root before dropping to the service user, so the secrets file
never has to be readable by `arduino` — the reason this belongs in the
unit rather than a shell profile. `install-agent.sh` creates it with the
variable commented out, so the place to put a credential is discoverable
on the board and not only in the docs, and never rewrites an existing one,
for the same reason it never regenerates a token.

Optional matters as much as 0600: a bench with no PDU has no such file,
and a missing secret must not stop the agent serving the instruments that
need none. The `-` goes on the **value** — `EnvironmentFile=-/path`.
Writing `-EnvironmentFile=` instead gets you `Unknown key … ignoring` in
the journal and an agent that starts fine and then fails at `open()`
naming a variable that looks like it was set; the unit carries a comment
saying so, because that failure is otherwise indistinguishable from a
wrong password.

Also `deploy/install-display-hotplug.sh` — a udev-triggered oneshot
that enables a DP/HDMI output negotiated *after* Xorg's startup probe,
which is how HDMI-through-a-USB-C-hub behaves on an Uno Q. Verified
against the real race, not just a synthetic `udevadm trigger`.

### Userspace CH341 driver — the QR10x on a kernel without `ch341`

Arduino's Uno Q kernel is built `# CONFIG_USB_SERIAL_CH341 is not set`
with no generic fallback, so the CH340 bridge the QR10x speaks through
enumerates but binds no driver and no `/dev/ttyUSB*` appears. Loading a
prebuilt module is out (`CONFIG_MODULE_FORCE_LOAD` off, vermagic
mismatch) and so is building one (no compiler, no headers package for
`6.16.7-g0dd6551ae96b`).

`transports/ch341.py` therefore speaks the chip's register protocol over
libusb, and `transports/ptybridge.py` exposes it as a **real pty**. The
payoff is that no driver changes: `QR10x.open()` gets a `/dev/pts/N`
path and opens it with unmodified `serial.Serial`.

Baud and framing registers are pinned against the in-tree
`drivers/usb/serial/ch341.c` (115200 → prescaler `0x03`, divisor
`0xcc`; 8N1 LCR → `0xc3`). An unrepresentable rate raises rather than
programming the nearest one — silently mis-clocking opens the port and
corrupts every byte, which reads as a protocol bug.

`deploy/udev/60-benchctrl-ch341.rules` grants the bench user write
access to `/dev/bus/usb/*` for this one VID/PID: libusb control
transfers need write, and the kernel creates those nodes `root:root
0664`. Scoped deliberately — not a blanket `usb_device` rule.

Verified end to end on the real instrument (QR101A-1M-R1, serial
00000248, fw 5.967KS) as the unprivileged service user: setpoint
100.0 Ω reading back 100.038 Ω, repeatable across open/close cycles.
See `KNOWN_LIMITATIONS.md` N-6 for the one-time root step.

Reading real values also exposed a simulator fidelity bug: `DEV.PROD`
answered `"QR104"`, a model code, where the field is a YYYYMMDD
production date (hardware returns `20221119`). The sim could therefore
never have caught a driver that mis-parsed that field. Its identity is
now shaped after the real unit, with the serial left obviously
synthetic so captured logs stay attributable.

### Fixed

- **`runs_dir` in `agent.json` was silently ignored.** `agent/main.py`
  built `BenchAgent` without forwarding it, so every run bundle landed
  in `$CWD/benchctrl-runs` regardless of config. Invisible when
  launching by hand from a checkout; under systemd there is no
  meaningful cwd. `llm_base_url` was unforwarded the same way.
- **The token-permission warning fired on the mode it recommended.**
  The check masked `st_mode & 0o077`, so the documented 0640
  `root:<service user>` deployment tripped it and was told to `chmod
  640`. Now masks `0o007` — group read is required for the service
  user to read its own config; only world access is a finding.

## [1.2.0] — remote mode, unattended runs, and hardware-free simulation

Three things land together, each usable on its own: instrument
**simulators** that let the whole stack run with no hardware
attached, a **remote mode** that puts the instruments on one machine
and the agent on another, and a **run engine** that takes a
declarative experiment and executes it unattended.

The through-line is that none of it changes the tool surface. All
**226 MCP tools are byte-for-byte unchanged** and cannot tell whether
they are driving a local device, a device across the network, or a
simulator. With nothing configured, behaviour is identical to v1.1.0.

### `benchctrl.sim` — instrument simulators

Device simulators that speak their instrument's **real wire
protocol** over a pty, so production drivers connect to them
unmodified via `serial.Serial`. These are not mocks of benchctrl
classes — an end-to-end test exercises `Transport`, the binary
framing in `protocol.py`, the timed session-init handshake, and the
recording reader thread with nothing monkeypatched.

- `sim.loopback.SerialLoopback` — pty pair held in **raw** mode (a
  cooked pty rewrites CR/LF and breaks every checksum), non-blocking
  master with a bounded tx queue that reports overruns rather than
  silently dropping samples
- `sim.otii_arc.SimulatedOtiiArc` — session handshake, `SET` with
  per-parameter range validation emitting genuine negative-status
  error frames, `GET` readbacks including the 268 B channel
  inventory, baseline streaming, and packed sub-1 / sub-4 high-rate
  framing. Fault injection for the async `BenchCommandError` path
- `sim.qr10x.SimulatedQR10x` — AT protocol, relay-ladder
  quantisation, safety limit, settling delay
- `sim.scpi` — SCPI simulators for both Rigols, reached through
  **pyvisa-py's ASRL backend**, so the real driver and the real
  pyvisa stack run against them over a pty. A generic register model
  covers the bulk of the ~254 distinct SCPI strings; measurement,
  identity and error-queue behaviour are explicit. Models the
  DL3031A quirk where `:SOUR:FUNC` is set with `CURRent` but reads
  back as `CC`
- `sim.waveforms` — analytically-known signals, so tests assert
  exact statistics rather than "a number arrived". `OhmicLoad`
  closes the V→I loop, making the battery `Emulator` exercisable
  without hardware
- `sim.factories` — `mode="sim"` returns the **production driver**
  bound to a simulator, never a mock, so sim mode exercises the real
  code path. Simulator lifetime is tied to the driver

### `benchctrl.session` / `benchctrl.config` / `benchctrl.discovery`

- **`session`** — the local/remote/sim seam. Each driver's
  `_get_<device>()` singleton populates via `session.resolve()`,
  which returns a local driver, a remote proxy, or a simulated
  instrument per configuration. Mode resolves **per device key, not
  per process**: an Arc on a remote bench and a Rigol on the laptop
  can be driven from one MCP server. Total diff across the four
  `mcp_tools.py` files is 56 lines
- **`config`** — layered configuration (explicit > CLI > env > file >
  all-local). JSON rather than TOML because `tomllib` is 3.11+ and
  the package declares `>=3.9`. `EndpointConfig` rejects
  `deadman_s <= heartbeat_s`, which would make a healthy link trip
  the safety governor. A remote device naming no reachable endpoint
  is a **loud error**, never a silent fall back to local — the quiet
  failure drives the wrong hardware
- **`discovery`** — one signature table replacing three ad-hoc
  mechanisms, answering "what is on this bench" rather than "where
  is my device". Carries a **confidence level**: devices behind
  generic USB-serial bridges (CH340, FTDI, CP210x) are never
  claimed, because those VID/PIDs are shared by thousands of
  products. The QR10x has no recorded VID/PID and is identified by
  AT probe instead. A test asserts no driver signature collides with
  a known bridge

### `benchctrl.net` — the wire protocol

- Length-prefixed **typed frames on one socket**, so binary rides
  natively and 64 KB blob chunks interleave with heartbeats instead
  of blocking them
- **HMAC-SHA256 challenge-response** over a pre-shared token — the
  secret never crosses the wire, so a passive listener on shared
  wifi cannot lift it
- Value codec with a **closed allowlist**; resolving a dotted class
  path from the wire would be RCE on the client
- Exceptions carry their class, MRO and attributes across all four
  driver hierarchies, degrading to the nearest known ancestor on
  version skew
- `net.beacon` — stdlib UDP beacon (plus a static Avahi service
  file) carrying a token **fingerprint** and a device count, never
  models or serials, since it is broadcast to the whole subnet

### `benchctrl.agent` — the bench-side server

- One **owning thread per device**, restoring the invariant
  `Transport` documents ("one thread owns one Transport") that a
  networked server would otherwise break
- Dispatch is an allowlist computed by **introspecting the driver
  class** — never `getattr` on whatever arrives
- Blobs spill to `/home/arduino`, not the 2 GB root partition
- **Stdlib + pyserial only**, because the target board cannot
  install anything else
- `agent.safety` — the agent tracks armed state **from the wire**,
  not from client bookkeeping, and drives outputs off when contact
  is lost. This is the failure mode that does not exist locally,
  where a dead process still runs `__exit__`
- New `benchctrl-agent` console entry point

### `benchctrl.agent.runs` — unattended experiments

- A run is **data, not code**: a JSON spec validated before anything
  is energised, content-hashed so a result always traces to the spec
  that produced it
- The **safety envelope is declared up front** and no later phase —
  or model — can widen it
- Ordering inside a tick is itself a safety property: sample, check
  the envelope, evaluate rules, check phase exit, roll a chunk
- Conditions carry a **dwell time**, because measurement noise
  crosses any threshold occasionally and a run that aborted on one
  stray sample would be worse than no rule at all
- **Durability**: SQLite in WAL mode plus an `fsync`'d
  `events.ndjson` mirror. The mirror is deliberately redundant — WAL
  survives a crash but not an SD card losing power mid-write, which
  is real on a board someone unplugs. Sequence numbers are assigned
  inside the transaction that persists the event, so a reconnecting
  host using `since_seq` can neither miss an event nor see one twice
- A run still marked `running` under a previous boot id is marked
  **interrupted and never silently resumed** — after a power cut the
  DUT's state is unknown, so a human decides

### `benchctrl.agent.llm` — advisory supervisor

- **Eight tools, allowlisted in code**: four read, two annotate, two
  move the run toward its end. No driver method is reachable — the
  model cannot energise anything, widen the envelope, or repeat a
  phase. `advance_phase` is forward-only within the declared list;
  `abort_run` only stops things
- Three policy violations in a phase disable the model for the run
- Runs on its own thread and sees only pre-computed aggregates. A
  test asserts a run finishes on time with a **30-second model stall
  against a 3-second run**
- The boundary is stated plainly in the code: deterministic rules
  are the safety system, the model is commentary

### `Recording` — stream codecs for `.opensmu`

`save_to_stream()` / `load_from_stream()` / `to_bytes()` /
`from_bytes()` expose the existing `.opensmu` encoding without
requiring a filesystem path. Remote mode transfers recordings as
`.opensmu` blobs, so these are the **wire codec as well as the file
writer** — one format and one implementation shared by the file
path, the wire, the run engine's chunks, and `sensor_profiler`'s
analyzer.

`save()` / `load()` become thin wrappers and are byte-for-byte
unchanged; `load()` still names the offending path in its error,
which the stream version cannot know.

### Fixed

- `transport.py` — the DTR/RTS assignment is now guarded. Modem-control
  ioctls are not universally supported (ptys and some virtual COM
  drivers raise) and the Arc does not need the lines asserted.
  Mirrors the guard already on `reset_input_buffer`
- Latent race: `_get_dl3031a` / `_get_dp2031` / `_get_qr10x` read
  their module global without taking the lock their `open`/`close`
  counterparts hold
- Run spec content hashing — `chunk_s=60` and `chunk_s=60.0` compare
  equal but serialise differently, so a spec's hash changed across a
  JSON round trip. Numeric fields are now coerced at construction

### Discovered

Two platform findings that shaped the design, both recorded in the
source:

- Python 3.12+ resolves `runtime_checkable` `Protocol` members with
  `inspect.getattr_static`, which does **not** invoke `__getattr__`.
  A purely dynamic proxy can therefore never satisfy
  `isinstance(x, SourceMeasurementUnit)` no matter how complete it
  is. `RemoteSMU` declares the twelve contract methods explicitly;
  everything else stays dynamic
- `QR10xTimeoutError` inherits both `RuntimeError` and
  `TimeoutError` (an `OSError`), whose C layouts are incompatible,
  so `cls.__new__(cls)` is rejected outright. Exception rebuilding
  is a three-strategy cascade rather than one clever call

New `KNOWN_LIMITATIONS.md` entries:

- **A-3** — `start_recording()` does not flush the inbound buffer,
  so a recording's first samples can predate it. Found by the new
  simulator tests; documented rather than fixed, since a blind flush
  would also discard legitimate in-flight samples
- **A-4** — `Transport.read_chunk` blocks in `serial.read(8192)`
  until the buffer fills or the 0.5 s timeout expires, so a running
  recording reports no samples for up to half a second. Bounds live
  progress reporting
- **N-1 … N-5** — the network section. N-1 states plainly that a
  software deadman cannot guarantee an output goes off through a
  wedged driver, and that a **hardware interlock is the only real
  guarantee** for unattended runs

### Docs

- [`docs/remote.md`](docs/remote.md) — setup, per-device
  local/remote/sim splitting, discovery, the security posture
  (authentication without confidentiality, and the SSH tunnel that
  fixes it), measured latencies from an actual Uno Q, behavioural
  differences, and Uno Q deployment
- [`docs/runs.md`](docs/runs.md) — spec format, the safety envelope
  and why dwell times matter, the artifact bundle, restart
  semantics, and the LLM layer's tool allowlist
- `ARCHITECTURE.md` corrected on three counts: 92 tools (actual
  226), 24 Arc tools (actual 23), and no mention of DP2031 at all.
  Layer-4 section added for remote mode

### Dependencies

`mcp` pinned `<2` — 2.0 removed `mcp.server.fastmcp`, which
`benchctrl.mcp` imports.

### Tests

**956 hardware-free passing** (+130 vs v1.1.0), 23 skipped, 152
hardware-marked deselected. New suites: `test_sim_loopback.py` (21),
`test_session_config.py` + `test_discovery.py` + `test_beacon.py`
(54), `test_recording_io.py`, `test_remote_protocol.py` (49),
`test_run_engine.py` + `test_llm_supervisor.py` (63).

Remote mode verified end to end over the wire: submitted a run,
disconnected the host mid-flight, reconnected, and replayed exactly
the missed event range.

## [1.1.0] — Rigol DP2031 driver (4-phase rollout)

New driver: **`benchctrl.drivers.rigol_dp2031.RigolDP2031`** — full
USB-TMC coverage of the Rigol DP2031 triple-output programmable PSU.
LAN / RS232 / GPIB transports are out of scope (deferred — USB-only
for this release).

### Phase A — skeleton + source / measure / output / protection levels

- `RigolDP2031` class, `DP2031Channel` IntEnum (1/2/3),
  `RigolDP2031Info`, `RigolDP2031Error` hierarchy
- Per-channel `set_voltage` / `get_voltage` / `set_current` /
  `get_current` with envelope validation (CH1/CH2 0–32 V/3 A;
  CH3 0–6 V/5 A)
- `set_output(ch, on)` / `set_output_all(on)` / `get_output(ch)` /
  `output_regulation(ch)` (CV/CC/UR)
- OVP and OCP level + enable per channel
- `measure_voltage` / `measure_current` / `measure_power` /
  `measure_all` (single-channel CSV parse) / `measure_all_channels`
  (3-channel snapshot)
- Auto-discover via Rigol VID + DP2000 PID (0x1AB1 / 0xA4A8)
- Context manager disables all 3 channels on exit (safety)

### Phase B — protection trip/clear + IEEE 488.2 status + system basics

- Protection trip/clear: `clear_ovp` / `clear_ocp` / `ovp_tripped` /
  `ocp_tripped` / `ovp_questionable` / `ocp_questionable`
- OCP delay (0–1000 ms) with `"200ms"` unit-suffix parser
- Full IEEE 488.2 status surface (`*ESR`/`*ESE`/`*SRE`/`*STB`/`*OPC`/
  `*OPC?`/`*WAI`/`*TST?`/`*OPT?`/`*PSC`/`*SAV`/`*RCL`)
- `:STATus:*` subsystem incl. per-channel ISUMmary registers
- `health_check()` convenience — decodes questionable bits + per-channel
  vunreg/iunreg/ovp/ocp/otp flags
- System basics: beeper / brightness / locks / language / power-on /
  remote / local

### Phase C — pair / tracking / sense / sampling / step / apply / bounds

- Channel pair (OFF/SERies/PARallel), tracking (`:OUTPut:TRACk` and
  `:SYSTem:TMODe` aliases), output sync, 4-wire remote sense
  (per-channel + `"ALL"`), sampling mode (AUTO/HIGH/LOW)
- V/I step + `step_voltage_up`/`down`, `step_current_up`/`down`
- `apply()` one-shot V/I shorthand + `query_applied()` (full triplet
  or VOLT/CURR option)
- `voltage_bounds()` / `current_bounds()` — device-reported MIN/MAX/DEF
  (MAX is ~5% over nominal envelope)

### Phase D — Timer + Analyzer + Trigger I/O + Memory + license + screenshot

- **Timer (Arb sequencer)** — 17 methods covering state, channel,
  cycles (int or `None` for infinite), end state, run mode, trigger
  source, group editor (index/params/delete), template subsystem
  (SINE/PULSE/RAMP/UP/DN/UPDN/RISE/FALL with min/max/period/points/
  object), and a `program_timer()` convenience that pre-validates and
  batch-writes a full sequence in one call.
- **Analyzer** — 7 methods for the IoT power / pulse-current analyzer
  including common-object selection and the log-to-file feature.
- **Trigger I/O** — 11 methods for the D1–D4 rear digital lines
  (per-line in/out enable, type, source, response, polarity, and an
  immediate-trigger fire).
- **Memory** — 8 methods for the device's internal C-disk + external
  USB filesystem (list/cd/store/load/delete/exists/lock + disk list).
  Pairs with the IEEE 488.2 `*SAV`/`*RCL` from Phase B for fast slot
  save/recall.
- **License install** (`:LIC:SET`) and **screenshot capture**
  (`:SYSTem:PRINt?` with IEEE 488.2 block-header stripping for the
  binary BMP reply).
- IEEE 488.2 block-format parsers: `_query_block_payload` (strings)
  and `_strip_block_header_bytes` (binary).

### MCP tools

134 new `dp2031_*` MCP tools across all four phases. Orchestrator
grows 92 → **226 total** tools (Otii Arc 23 + QR10x 11 + DL3031A 45 +
DP2031 134 + cross-driver 13).

### Tests

**526 tests** (462 hardware-free + 64 hardware-marked), all passing
on a real DP2031 (FW 01.00.01.00.16, S/N DP2A243500269) +
DL3031A + Otii Arc + QR10x bench. Hardware tests include OVP trip +
clear on CH3, multi-channel setpoint round-trip, tracking / pair
state, Timer programming with `program_timer` + readback via
IEEE 488.2 block parser, screenshot BMP capture.

### Bench-discovered firmware quirks

`KNOWN_LIMITATIONS.md § F-3.5` documents the DP2031-specific quirks
found during bring-up:

- `:OUTPut:PAIR` SERies/PARallel state survives `*RST`
- `:OUTPut:PAIR PARallel` write-then-query returns stale `OFF` for
  ~1+ seconds before the mode transition completes
- `:OUTPut:PAIR PARallel` mode transition takes ≥ 1 s
- OVP latch settles ~150–250 ms after the over-voltage condition
- `:OUTPut:OVP:CLEar` clears the latch but does NOT re-enable the
  output (the `:SOURce<n>:VOLT:PROT:CLEar` form does — distinct
  behaviour despite docs treating them as aliases)
- `:OUTPut:TRACk ON` sets the state register but the analog
  CH1→CH2 setpoint mirroring requires further conditions
- `:ANALyzer:COMMon:MEASure:TYPE` write triggers
  `VI_ERROR_SYSTEM_ERROR` over USB-TMC (firmware defect — see
  `bugs/` directory)
- `:OCP:DELay?` returns string with `"ms"` suffix
- `:LANG:TYPE?` returns long form (`"ENGLISH"`); input takes short
- `:SOURce<n>:VOLT? MAX` includes ~5% headroom over nominal
- `*OPT?` returns `"NONE"` literal when no options installed
- Many boolean queries return with trailing space

Bug reports for the firmware defects worth filing with Rigol are
prepared in the `bugs/` directory.

## [1.0.0] — driver-symmetric architecture + package rename

The first public release. The v0.x tree was called `opensmu` and
treated the Otii Arc Pro as a first-class top-level class while
companion instruments lived under `bench/`. v1.0 renames the package
to `benchctrl` (broader scope than just SMUs) and makes every
instrument a peer driver under `benchctrl.drivers.<vendor_model>/`.

This is a **clean break** — the package never shipped publicly, so
no backwards-compatibility shims, no v0.x aliases. Code that ran
against `opensmu.SMU` will fail to import and needs to be updated to
`from benchctrl.drivers.otii_arc import OtiiArc`.

### Architecture

- `opensmu` → `benchctrl` (new top-level package name)
- `opensmu.SMU` → `benchctrl.drivers.otii_arc.OtiiArc`
- `opensmu.Channel` → split into `benchctrl.channels.StandardChannel`
  (common subset used by framework subsystems) and
  `benchctrl.drivers.otii_arc.OtiiArcChannel` (Arc's full inventory)
- `opensmu.bench.QR10x` → `benchctrl.drivers.eastwood_qr10x.QR10x`
- `opensmu.bench.RigolDL3031A` → `benchctrl.drivers.rigol_dl3031a.RigolDL3031A`
- `opensmu.SMUError`, `SMUConnectionError`, ... →
  `benchctrl.BenchError`, `BenchConnectionError`, ...
- New `benchctrl.interfaces.SourceMeasurementUnit` Protocol — battery
  emulator + profiler + scenarios depend on the Protocol, not the
  concrete Arc class. Adding a new SMU means implementing the
  Protocol; battery emulation against it works for free.
- New `benchctrl.analysis/` and `benchctrl.dashboards/` packages —
  intentionally empty placeholders reserved for v1.x analytics and
  graphical UIs.

### MCP server reorganisation

Each driver now owns its MCP surface. `benchctrl.drivers.<X>.mcp_tools`
exposes a `register_mcp_tools(mcp)` function that the top-level
`benchctrl/mcp.py` orchestrator calls at startup. Per-driver
connection singletons (`_smu`, `_qr10x`, `_dl3031a`) live in the
driver mcp_tools modules — tests inject fakes by mutating the driver
module directly.

Tool count: **92** (vs ~93 at v0.9.7 — no functionality lost; the
delta is from the removal of one duplicate). Tool names within each
driver are unchanged for stability.

### `validation/` → `scenarios/`

The harness directory was always wider in scope than "validation":

- `validation/` → `scenarios/`
- `validation/run_validation.py` → `scenarios/run.py`
- `validation/scenarios/` → `scenarios/saved/`

All 100+ saved captures move via `git mv` so history is preserved.
The CLI now reads `python scenarios/run.py --scenario static …`.

### What stays unchanged

- `.opensmu` saved-recording file format — it's a filename suffix,
  not an import path, and v0.x captures load against v1.0 with no
  changes. Possible future format rename is v1.x cosmetics.
- Battery profile JSON round-trip (bit-identical with Otii's bundled
  format).
- Hardware behavior on every driver — public method names within
  each driver are stable (`set_voltage`, `set_resistance`,
  `program_list`, etc.).
- The 27+ captured scenarios — moved location, content unchanged.
- KNOWN_LIMITATIONS findings (firmware bugs, hardware caps, etc.).

### Tests

- **313 hardware-free** (+21 vs v0.9.7, mostly new Protocol +
  StandardChannel coverage)
- **86 hardware-marked** (+0; full coverage retained against Arc
  Pro + DL3031A); 6 QR10x tests skip when not connected

### Migration cheat sheet

| Old (v0.x) | New (v1.0) |
|---|---|
| `from opensmu import SMU` | `from benchctrl.drivers.otii_arc import OtiiArc` |
| `from opensmu import Channel` | `from benchctrl.drivers.otii_arc import OtiiArcChannel` (or `from benchctrl.channels import StandardChannel`) |
| `from opensmu import Recording` | `from benchctrl import Recording` |
| `from opensmu.bench import QR10x` | `from benchctrl.drivers.eastwood_qr10x import QR10x` |
| `from opensmu.bench import RigolDL3031A` | `from benchctrl.drivers.rigol_dl3031a import RigolDL3031A` |
| `from opensmu.exceptions import SMUError` | `from benchctrl.exceptions import BenchError` |
| `opensmu-mcp` CLI | `benchctrl-mcp` CLI |
| `python validation/run_validation.py …` | `python scenarios/run.py …` |

## [0.9.7] — adversarial-review fix-batch + LIST/battery firmware bugs unearthed

### Fixed — compound silent-failure chain in `Emulator`

Three independent bugs that compounded to produce "emulator runs
silently, state() looks fine, but no DUT integration is happening":

1. `Emulator.start()` wrapped every SMU config call (`set_range`,
   `set_current_limit`, `set_current_limit_enabled`,
   `set_power_regulation`) in `try/except Exception: log + pass`.
   A typo, a wrong arg, or a missing mock method would silently
   skip configuration and proceed to `set_output(True)` with the
   device in an unknown state. **Now**: exceptions propagate;
   start() aborts before enabling the output.
2. `Emulator._read_current` had a bare `except Exception: return 0.0`
   that v0.9.2 claimed to fix but didn't. Any read failure (queued
   SCPI error, transport hiccup, etc.) produced "I = 0 forever,
   v_out = OCV". The outer `_loop` already catches and logs+retries,
   so the inner swallow was both redundant and harmful. **Now**:
   exceptions propagate to the outer handler, which logs at warning
   and continues.
3. The test mock SMU (`_MockSMU` in `tests/test_battery_emulator.py`)
   was missing the v0.9.1 methods. Combined with (1), the calls
   silently no-op'd in tests. **Now**: the mock has all four
   methods + matching `*_calls` lists, and new tests assert
   `start()` actually calls them in order.

New tests: `test_emulator_start_configures_smu_cv_mode`,
`test_emulator_start_propagates_config_failures`,
`test_emulator_loop_logs_and_retries_on_read_failure`.

### Discovered — `:SOUR:LIST:STEP` semantics + STEP=4 firmware bug

Bench investigation of the "5-step LIST onset slip" found two
distinct issues:

1. **`:SOUR:LIST:STEP N` means N total steps**, not N+1 as the
   manual's application instance suggested. v0.9.6's driver was
   sending `STEP N-1`, executing one fewer step than intended.
   Captures for CR2032/CR123A still showed TX bursts because
   `end_behavior="LAST"` held the (mis-played) TX step.
2. **`STEP=4` is a firmware bug** (verified on fw 00.01.05.00.01):
   regardless of how many steps are programmed, ``STEP=4`` fires
   no steps at all. ``STEP=2``, ``STEP=3``, ``STEP=5``, etc. all
   work. The driver now rejects 4-step programs at
   `list_set_step_count(4)` and `program_list(steps=[...4...])`
   with a clear error pointing to the workaround (use 3 or 5
   steps with appropriate `count`).

### Discovered — DL3031A `:SOUR:FUNC:MODE FIXed` is one-way

Once the device enters LIST / WAV / BATTery / OCP / OPP mode,
`:SOUR:FUNC:MODE FIXed` is silently rejected. `*RST`, `*WAI`,
`*OPC?`, `*CLS`, input toggling, and cycling through other modes
all fail to bring it back. **Only power-cycling** restores FIX
mode. Documented in `KNOWN_LIMITATIONS.md` § F-3. The runner's
`dynamic-list` teardown reads back `get_function_mode()` and logs
*"DL3031A stuck in {MODE} mode after teardown — power-cycle before
reuse"* so the operator sees the failure.

### Discovered — `:FETCh:DISChargingTime?` returns H:MM:SS, not float

The manual documents the return type as "a real number" but the
device returns colon-delimited time (e.g. `"0:0:15"`).
`battery_stats()` now parses both formats — H:M:S preferred, plain
float fallback if a future firmware change matches the manual.
4 new parser tests including malformed-input handling.

### Discovered (workaround pending) — LiPo dynamic-list unreliable in high range

With the Arc Pro in high range (LiPo profiles, 4.2 V), LIST
playback fires a partial / misordered sequence — one TX burst at a
non-deterministic time, then stops, instead of the expected
`cycles × n_steps` repeats. CR2032 / CR123A in low range work
correctly. Cause appears to interact with the FUNC:MODE-FIXed
one-way bug (above); not isolated. Documented as
`KNOWN_LIMITATIONS.md` § F-2. Use `--pattern hires` for LiPo
transient validation in the meantime.

### Discovered — `transient_set_frequency` takes Hz, not kHz

Manual says kHz; bench-verified the device takes Hz. Sending
`:SOUR:CURR:TRAN:FREQ 10` reads back as `0.1 s` period. The driver
parameter name `hz` was already correct; the docstring was
misleading and has been corrected. New hardware-marked test
asserts period = 1/freq.

### Fixed — `RigolDL3031A._autodiscover` ambiguity

Now deterministic (sorted resource list). If multiple Rigol
DL3000 devices are connected, raises with the list of candidate
VISA resource strings so the caller can pass an explicit one.

### Fixed — MCP exception types match driver hierarchies

`_get_dl3031a()` now raises `RigolDLConnectionError` (was bare
`RuntimeError`); `_get_qr10x()` raises `QR10xConnectionError`.
Per-driver hierarchies stay consistent across SDK and MCP surfaces.

### Fixed — `dl3031a_close` surfaces input-off failures

Previously returned `{"closed": true}` even if `set_input(False)`
raised, leaving the load potentially sinking current with the
operator unaware. Now returns
`{"closed": true, "input_off_failed": "...", "warning": "..."}`
with a clear safety note when the input couldn't be disabled.

### Added — SDK ↔ MCP parity catch-up for DL3031A

The v0.9.6 review surfaced that the DL3031A had ~25 SDK methods
without MCP equivalents — primarily `get_*` queryable state and
individual `measure_*` / `fetch_*` methods. **Now**: 45 DL3031A
MCP tools (was 25). Granular LIST / transient / battery setters
remain SDK-only (the convenience wrappers `program_list` /
`configure_transient_pulse` / `configure_battery_test` are the
MCP-facing surface) — documented as SDK-only in the bench docs.

### Added — `--profile-dir` + `BENCHCTRL_BATTERY_PROFILE_DIR`

`scenarios/run.py` no longer hardcodes a
user-specific Otii install path. Resolution order: CLI
`--profile-dir`, then `$BENCHCTRL_BATTERY_PROFILE_DIR`, then
auto-detect under `%LOCALAPPDATA%/otii3/app-*/resources/batteryprofiles`,
then repo-local `scenarios/profiles/`.

### Added — `KNOWN_LIMITATIONS.md`

Aggregated list of hardware caps, firmware quirks, and harness
workarounds, in one file rather than buried under per-version
CHANGELOG entries. Linked from the top of CHANGELOG.

### Smaller fixes / tightenings

- `dl3031a_program_list` MCP docstring now matches the driver's
  validator (was `2-512`, driver enforces `2-512 \\ {4}`).
- LIST test assertions are now exact wire-format matches (was
  `startswith` which would let `:RANGE` typos pass).
- Validation `time.sleep(min(0.005, end - now))` clamps to
  non-negative (was a `ValueError` when the loop fell behind).
- `--cycles 0` is rejected with a clear error (was infinite LIST
  + zero sleep → load running indefinitely).
- Ctrl-C during `--all` matrix runs now breaks cleanly (was
  swallowed by the `except Exception` and continued to the next
  profile).
- Validation hires/dynamic-list runners reject `pinned_V < 0.1V`
  before pinning (was a silent zero-output capture).
- Loop pile-up / fetch_all aliasing now documented in docstrings.
- Concurrency note added to MCP module docstring (single-client
  serialization assumed).

### Tests

292 hardware-free tests passing (was 282 in v0.9.6, +10):
- 4 emulator hardening tests (config-error propagation, mock
  parity, read-failure retry)
- 4 battery_stats parser tests (H:M:S, hour-minute-seconds,
  float fallback, malformed input)
- Tighter LIST wire-format assertions
- MCP-level coercion and connection-error tests
- Driver-exception type assertions

Hardware-marked tests: 90+ (was 89; +2 new — battery discharge
smoke + transient_set_frequency unit check).

## [0.9.6] — DL3031A built-in modes: LIST, transient, battery discharge

### Added — `RigolDL3031A.program_list` and friends (LIST mode)

Wraps `:SOURce:LIST:*` so a programmable CC/CV/CR/CP sequence can be
pushed to the DL3031A and executed entirely in firmware with
deterministic timing (step widths from 50 µs to 3600 s). The right
tool for sub-100 ms TX bursts and other transients where USB-TMC
round-trips can't keep up.

API:

- `list_set_mode` / `list_set_range` / `list_set_count` /
  `list_set_step_count` / `list_set_step` / `list_set_slew` /
  `list_set_end` — granular SCPI wrappers
- `program_list(steps=..., mode='CC', count=1, range_value=...,
  slew_A_per_us=..., end_behavior='OFF'|'LAST',
  trigger_source='BUS'|'MANUal'|'EXTernal')` — convenience that
  pushes a whole sequence in one call and switches the device to
  LIST regulation mode

Two manual-misread bugs ironed out:

- `:SOUR:LIST:STEP N` means steps 0..N inclusive (so N+1 total).
  The driver accepts the **total** step count and subtracts 1
  internally to match the firmware's convention.
- `:SOUR:LIST:SLEW <step>,<value>` is **per-step**, not global.
  `program_list` applies the same `slew_A_per_us` to every step
  when provided.

Documented LIST-end behavior: `LAST` (hold final step's value) or
`OFF` (disable input). The manual incorrectly suggests `NORMal|LAST`
in places — the firmware accepts only `LAST|OFF`.

### Added — trigger system (`:TRIGger`)

- `set_trigger_source` ({BUS|EXTernal|MANUal}) / `get_trigger_source`
- `trigger_now` — issues `:TRIGger` (software / BUS trigger)

LIST and transient sequences default to MANUal trigger after `*RST`;
the driver sets BUS as the default in `program_list` so a software
trigger fires the sequence.

### Added — CC transient mode (`:SOURce:CURRent:TRANsient:*`)

- `transient_set_mode` (CONTinuous / PULSe / TOGGle)
- `transient_set_a_level` / `transient_set_b_level` /
  `transient_set_a_width` / `transient_set_b_width` /
  `transient_set_frequency` / `transient_set_duty`
- `transient_enable` — arm / disarm via `:SOURce:TRANsient[:STATe]`
- `configure_transient_pulse(a_level_A, b_level_A, a_width_s,
  b_width_s, mode)` — convenience

### Added — battery discharge mode (`:SOURce:BATTary:*`)

- `battery_set_range` / `battery_set_level` /
  `battery_set_voltage_stop` (V_stop + VEN enabstop) /
  `battery_set_capacity_stop` / `battery_set_time_stop` /
  `battery_set_von`
- `configure_battery_test(current_A, v_stop_V?, capacity_stop_mAh?,
  time_stop_s?, von_V?, range_A?)` — convenience that pushes the
  full setup and switches to `BATTery` function mode
- `battery_stats()` — returns capacity (mAh), energy (Wh), discharge
  time (s), and instantaneous V/I

### Added — `set_function_mode` / `get_function_mode`

Switch the top-level regulation source between FIXed / LIST / WAVe /
BATTery / OCP / OPP. Implicitly set by the various
`program_list` / `configure_*` helpers.

### Added — `:FETCh:` query methods

Already in v0.9.5 but consolidated here for clarity. `fetch_voltage`
/ `fetch_current` / `fetch_power` / `fetch_resistance` / `fetch_all`
read the device's continuously-updated measurement register
(non-blocking ~1 ms each, vs `measure_*` which trigger a fresh
~200 ms integration).

### Added — `:SYSTem:KEY` 0 (CC) through 25 (numeric) etc

Not exposed in this release; the driver leaves front-panel-key
simulation as the user's job via `dl.write(":SYSTem:KEY <code>")`
if needed.

### Added — MCP tools for the new methods

New tools (`dl3031a_*`):

- `dl3031a_set_function_mode` / `dl3031a_get_function_mode`
- `dl3031a_program_list` — push a LIST from a JSON `[level, width]` list
- `dl3031a_set_trigger_source` / `dl3031a_trigger`
- `dl3031a_configure_transient_pulse` / `dl3031a_transient_enable`
- `dl3031a_configure_battery_test` / `dl3031a_battery_stats`
- `dl3031a_fetch`

DL3031A MCP tool count: 17 → **25**. Total MCP tools: 65 → **73**.

### Added — `--scenario dynamic-list` validation runner

New scenario type: programs a CC LIST on the DL3031A, fires it via
software trigger inside an `SMU.record(...)` window, captures the
response at the Arc Pro's native streaming rate (~4 kHz on I,
~1 kHz on V). The DL3031A executes the steps entirely in firmware
with deterministic timing — no per-step USB-TMC round-trip.

`DEFAULT_LIST_TX_PATTERN_A` is currently 3 steps (sleep / 50 ms TX
/ sleep). **Capability ceiling** (see `KNOWN_LIMITATIONS.md` § F-1):
LIST programs with ≥ 5 steps under BUS trigger have a ~3 s onset
slip — the LIST fires but the waveform doesn't start until well
after `:TRIGger`. The SCPI strings the driver sends are individually
correct; the cause is in the firmware/trigger interaction and is
not yet isolated. Use 3 steps + `count=N` repeats as the reliable
shape.

Saved scenarios for CR2032 and CR123A. **LiPo dynamic-list is not
supported in v0.9.6** (`KNOWN_LIMITATIONS.md` § F-2): with the Arc
Pro in high range, the LIST playback shows zero current during
capture even though the SCPI surface accepts the program. Use
`--pattern hires` for LiPo transient validation in the meantime.

### Tests

72 hardware-free tests in `tests/test_bench_rigol_dl3031a.py`
(was 36 in v0.9.3) — covering wire-format for every new method
plus end-to-end mocks. Two hardware-marked tests including a
real LIST program push.

### Discovered

- `:SOUR:LIST:STEP N` is highest-index, not total-count
  (3-step list → STEP 2). Driver compensates.
- `:SOUR:LIST:SLEW` is per-step, not global.
- `:SOUR:LIST:END` accepts `LAST|OFF`, not `NORMal|LAST` as I
  initially misread.
- `:SOUR:FUNC:MODE` doesn't always honor `FIXed` after switching
  away. Workaround: re-program the desired subsystem instead.
- LIST integration timing inside `SMU.record()` is misaligned vs
  the harness's phase events — the LIST plays but with delayed
  onset (~3 s after `:TRIGger` in some flows). Tagged as a known
  limit in `scenarios/README.md`; the captured waveforms still
  show real LIST behavior.

## [0.9.5] — high-resolution dynamic capture via Arc Pro native streaming

### Added — `--pattern hires` dynamic scenario

`scenarios/run.py --pattern hires` switches the dynamic
runner to a dedicated implementation that uses `SMU.record(...)` for
the actual V/I capture instead of host-side state polling. The Arc
Pro streams ~4 kHz on `MAIN_CURRENT` (subtype-4 packed) and ~1 kHz on
`MAIN_VOLTAGE`, which is enough to resolve the DL3031A's actual TX
response shape — the standard pattern's 20 Hz polling could not.

Three hires scenarios captured (CR2032, CR123A, LiPo @ +20 °C):

- 4 000 Hz I time series, ~36 k samples in 10 s
- TX I peaks: 71 mA (CR2032), 70 mA (CR123A), 86 mA (LiPo)
- TX steady: ~30 mA across all chemistries
- DL3031A inrush is consistently ~2× steady — closed-loop catch-up
  artifact, documented for future LIST/transient-mode comparison

### Discovered — emulator loop + recording reader deadlock

The `Emulator._loop` thread writes `set_voltage` at 100 Hz while
`SMU.record` consumes the transport at ~4 kHz. Running both
concurrently against the same Arc Pro consistently deadlocks within
~100 ms (transport-level contention).

Workaround in the hires runner: settle the emulator at fresh OCV,
read the resulting voltage, stop the emulator, pin the SMU at that
voltage manually, then open the recording. SoC tracking is off for
the duration. For 10 s × 30 mA peak, the SoC delta is ~0.08 mAh
(< 0.04 % of CR2032's capacity) — meaningless for the hires use case.

Saved scenarios document this with `"recording.pinned_voltage_V"` so
it's recoverable from the JSON.

### Discovered — DL3031A input-toggle latency ~700 ms

The first hires runs used 100 ms TX phases (cycle: sleep → R=100 →
sleep). At 4 kHz sampling the recording showed current spikes
landing 700 ms after the `:SOUR:INP:STAT 1` command, well outside
the labeled TX window. Toggling input state cycles the regulation
loop's internal settling — much slower than just changing a
setpoint.

`DEFAULT_HIRES_IOT_PATTERN` widened to 300 ms TX. Sub-300 ms work
needs "input always ON, toggle CC current setpoint" — left for a
future scenario.

### Added — `:FETCh:` query methods on `RigolDL3031A`

`fetch_voltage` / `fetch_current` / `fetch_power` / `fetch_resistance`
/ `fetch_all`. Non-blocking reads of the device's continuously-updated
measurement registers, vs the existing `measure_*` which trigger a
fresh 10 PLC (~200 ms) integration per call. Distinct use cases:
`measure_*` for high-accuracy single-shot, `fetch_*` for fast loops.

The DL3031A adapter (and the standard dynamic scenario's
`load.measure()` hook) now use `fetch_*` so they don't get throttled
by the 200 ms integration time.

## [0.9.4] — validation harness supports both loads; full matrix on DL3031A

### Added — `--load {qr10x,dl3031a}` in the validation harness

`scenarios/run.py` now accepts either programmable load
via a single CLI switch. The harness wires through a thin
`_LoadAdapter` abstraction that owns each instrument's lifecycle
(open / setup / set_resistance / teardown / close) so the scenario
logic stays load-agnostic.

DL3031A specifics handled in the adapter:

- Out-of-range R (> DL3031A CR max, ~16 kΩ) translates to
  `set_input(False)` — open circuit. Better physical model for a
  sleeping IoT device than a finite 100 kΩ anyway.
- CR min and max are queried at runtime from the device, not hardcoded.
- `*RST` settles for 200 ms before subsequent commands.
- `__exit__` (and `teardown`) always disables the load input.

Bench dict in saved scenarios now carries an additional `load_kind`
field so post-hoc analysis can group / filter by which load produced
each capture. Scenario filenames are now `<profile>_<scenario>_<load>_<utc>.{json,csv,png}`,
schema_version bumped to 2.

### Captured — 11 new DL3031A scenarios

Full static matrix (8 profiles) + dynamic IoT pattern (CR2032, CR123A,
LiPo @ +20 °C) re-captured with the DL3031A as the load. Headline
findings vs the QR10x v0.9.2 baseline:

- **LiPo at 12 Ω**: QR10x was capped at 20 Ω (1 W safety). DL3031A
  pulled true high-current behavior — at −10 °C, V sagged to **3.24 V
  at 324 mA**, vs 3.70 V at 185 mA on QR10x. Real cold-soaked LiPo
  behavior, previously beyond our reach.
- **Light loads (< 1 mA)**: DL3031A's CR-mode regulation breaks
  down. The 10 kΩ / 1 kΩ steps appear as input-off in the captures
  because the load can't sink that little. QR10x remains correct at
  any current.
- **Dynamic phase alignment**: DL3031A switches in microseconds; the
  emulator's ~100 Hz polling can't keep up. The dl3031a dynamic phase
  summary reports near-zero TX current. Documented; sets up the next
  pass (read directly from DL3031A `:MEASure:` queries).

See `scenarios/README.md` for the full per-load takeaway table.

### Tests / parity

No new test files (harness changes covered by existing scenario
captures); existing test suite still passes (248 hardware-free).

## [0.9.3] — Rigol DL3031A driver (`benchctrl.drivers.rigol_dl3031a.RigolDL3031A`)

### Added — SCPI-over-USB-TMC driver for the Rigol DL3000 series

`benchctrl.drivers.rigol_dl3031a.RigolDL3031A` — pyvisa-based driver for the Rigol
DL3021A / DL3031A programmable DC electronic load. Auto-discovers by
USB VID/PID (`0x1AB1`/`0x0E11`) or accepts an explicit VISA resource
string (USB-TMC or LXI).

Public API (mirrors the QR10x pattern):

- `open(resource=None)` / `close()` (context-manager safe; `__exit__`
  disables the load input)
- `info()` returns `RigolDLInfo` from `*IDN?`
- `reset()` / `clear_status()` / `last_error()` / `raise_if_error()`
- `set_mode(...)` / `get_mode()` — CC / CV / CR / CP
- `set_input(bool)` / `get_input()`
- Per-mode setpoints: `set_current` / `set_voltage` / `set_resistance` / `set_power`
- Ranges: `set_current_range` / `set_voltage_range`
- `set_slew(A/µs)` — symmetric CC/transient slew rate
- `measure_voltage` / `measure_current` / `measure_power` /
  `measure_resistance` / `measure_all()`

Exception hierarchy:

- `RigolDLError` (base)
  - `RigolDLConnectionError` — VISA open / transport failure
  - `RigolDLCommandError` — device returned non-zero from `:SYSTem:ERRor?`
  - `RigolDLValueError` — client-side range / type check failed
  - `RigolDLTimeoutError` — VISA `VI_ERROR_TMO`

Lives behind the `bench-visa` extra (already in `pyproject.toml`); the
top-level `benchctrl.bench` module lazy-imports `RigolDL3031A` via PEP 562
so the QR10x path stays usable without pyvisa.

### Added — 17 MCP tools for DL3031A

Per the SDK ↔ MCP parity principle, every public method has a matching
tool. The MCP server holds one DL3031A connection across calls until
`dl3031a_close()`. Tool count: 48 → **65**.

Tools: `dl3031a_open` / `dl3031a_close` / `dl3031a_info` /
`dl3031a_reset` / `dl3031a_last_error` / `dl3031a_set_mode` /
`dl3031a_get_mode` / `dl3031a_set_input` / `dl3031a_get_input` /
`dl3031a_set_current` / `dl3031a_set_voltage` /
`dl3031a_set_resistance` / `dl3031a_set_power` /
`dl3031a_set_current_range` / `dl3031a_set_voltage_range` /
`dl3031a_set_slew` / `dl3031a_measure`.

### Verified — talks to real hardware

Bench setup: DL3031A on USB-TMC via USB hub (the VISA resource string
is `USB0::0x1AB1::0x0E11::DL3D232300106::INSTR`), Rigol Ultra Sigma's
VISA backend loaded. `*IDN?` parses cleanly; mode / setpoint /
measurement round-trip; hardware-marked test passes.

### Tests

- 36 hardware-free unit tests using a `FakeInstrument` that records
  every `write()` and replies to `query()` from a scripted dict.
- 1 hardware-marked test that hits the real device (auto-discover or
  override via `BENCHCTRL_DL3031A_RESOURCE`).
- Total tests: 248 hardware-free passing (was 212).

## [0.9.2] — bench validation harness + multi-profile matrix + LiPo support

### Added — `scenarios/` harness with reusable scenarios

New top-level `scenarios/` directory holds an end-to-end test harness
(`run.py`) plus 11 saved scenarios captured against real
hardware (Arc Pro + Eastwood QR10x on COM7). Each scenario is a
self-describing JSON + CSV + PNG bundle that embeds a copy of the
input battery profile, so a saved scenario is fully reproducible
regardless of changes to the bundled profiles.

Two scenario kinds:

- **`static`** — step through a fixed list of QR resistances (100 kΩ
  down to 12 Ω, plus a recovery step), record one snapshot per step.
- **`dynamic`** — drive the QR through a time-varying IoT pattern
  (sleep / wake / TX burst), poll the emulator's state at 20 Hz.

CLI::

    python scenarios/run.py --scenario static --all
    python scenarios/run.py --scenario dynamic \
        --profile "CR2032-Energizer-(25)" --cycles 3

See `scenarios/README.md` for the full results table.

### Added — multi-profile validation matrix

Static sweep captured for all 8 bundled Otii battery profiles
(CR2032, CR123A, CR2, AA-Varta, AAA-Duracell, LiPo @ +20/+5/−10 °C).
Confirms the emulator faithfully reproduces chemistry-specific
behavior:

- CR2032 collapses at 12 Ω (1.85 V at 140 mA) — real coin cells do
  exactly this.
- CR123A sustains 258 mA at 12 Ω with 95 mV sag — designed for high
  pulse currents, reproduced.
- LiPo temperature sweep: same chemistry, ESR rises ~10× from +20 °C
  to −10 °C (12 Ω: V drops to 4.20 → 4.11 → 3.70 V respectively).

### Added — dynamic IoT-pattern scenarios

Captured for CR2032, CR123A, LiPo @ 20 °C. The CR2032/CR123A pair is
the most striking comparison — a 100 Ω, 400 ms "TX burst" sags a
CR2032 by 186 mV with multi-second recovery, vs. 10 mV with instant
recovery on a CR123A. This is the whole point of battery emulation
in the first place, and the emulator nails it.

### Fixed — emulator startup clamp to safety_max_voltage_V

`Emulator.start()` now clamps the initial OCV setpoint to
`safety_max_voltage_V` before sending `set_voltage(initial_v)`.
Previously, a profile whose fresh OCV exceeded what the SMU can
physically deliver (e.g. LiPo at 4.31 V on Arc Pro high range, capped
at ~4.2 V under load) would be silently rejected by the device with
error -101 and "reverted to last_good_value". The error queued and
poisoned every subsequent `read_value(MAIN_CURRENT)` call (each
returning 0.0 via the read-error swallow path), so the emulator's
loop would tick along reporting v_out = OCV and I = 0 forever.

Now the clamp happens before the rejection can occur, and the
emulator logs a warning. Tested with all three LiPo profiles.

### Added — `EmulatorConfig.voltage_range` with auto-detection

New `Optional[str]` field. Default `None` auto-selects `"low"` for
cells whose fresh OCV (× series multiplier) is ≤ 3.4 V, `"high"`
otherwise. This is what makes LiPo and other multi-cell stacks work
out-of-the-box — previously the emulator was hardcoded to
`set_range("low")` which caps voltage at ~3.5 V.

Override with `voltage_range="high"` or `"low"` for explicit control.

### Discovered — Arc Pro high-range output caps at ≈ 4.2 V under load

Bench-measured. `set_voltage(4.31)` is silently rejected with error
-101 / `last_good_value=4200000`. Documented in `scenarios/README.md`
under "Known limits" and `PROFILE_OVERRIDES` in the harness sets
LiPo's `safety_max_V` to 4.2 V to work with this constraint.

## [0.9.1] — emulator CV-mode fix + end-to-end validation

### Fixed — emulator CV-mode regulation

`Emulator.start()` now explicitly configures the SMU for
**constant-voltage** regulation before enabling output: `set_range("low")`
+ `set_current_limit(config.current_limit_A)` +
`set_current_limit_enabled(True)` + `set_power_regulation("voltage")`.

Without this, the device would inherit whatever mode the last test left
it in — typically `current` mode at 0 A target after a profiler run.
The Arc then prioritized current regulation (holding I=0 A) over the
voltage setpoint and refused to source current into the load, so the
emulator looked like an open circuit at the terminals.

### Added — `EmulatorConfig.current_limit_A`

New field, default 0.5 A. Sets the OC trip threshold during emulation —
the SMU acts as an ideal voltage source up to this current, then
protects itself. Tune up for higher-current DUTs (LiPo etc.).

### Fixed — `set_voltage` method name

The emulator was calling `self.smu.set_main_voltage(...)` which doesn't
exist on the real SMU — it's `set_voltage(...)`. Mock tests had a
matching `set_main_voltage` method so didn't catch this. Renamed
throughout (emulator + mock) so they match the real SMU surface.

### Verified — end-to-end emulator validation against the QR10x

Bench setup: Arc Pro emulating an Energizer CR2032 (fresh OCV 3.224 V,
ESR ≈ 9 Ω), QR10x as the programmable load. Stepped through
100 kΩ → 10 kΩ → 3 kΩ → 1 kΩ → 300 Ω → 100 Ω → 50 Ω → 25 Ω → 12 Ω,
then recovery back to 100 kΩ.

Highlights:

- Voltage sag tracks the ESR curve exactly: at 1 kΩ load the predicted
  9 Ω × 3.2 mA = 28.8 mV drop matched the measured 28 mV; at 100 Ω,
  predicted 279 mV vs measured 288 mV.
- The CR2032 emulator **collapses at 12 Ω load to 1.66 V** — below the
  profile's 1.8 V cutoff. That's exactly how a real CR2032 would
  behave at 160 mA draw.
- SoC integration verified: after the sweep SoC dropped to 99.925%,
  OCV came back at 3.146 V on the high-impedance recovery step — the
  profile's discharge curve correctly produced the new OCV.
- The ~14% R_err at low impedances tracks ~1.5 Ω of cable + plug
  series resistance in the measurement loop — physics, not an
  emulator bug.

### Tests

Existing test count unchanged (300 / 300 pass). The CV-mode fix is
covered by both mock-SMU tests and the real-hardware validation
above; no new unit tests beyond what already exists.

## [0.9.0] — bench subpackage + measurement stabilization

Two themes:

1. **Stabilize the profiler's measurement path** so step-response
   readings are accurate on real hardware (the previous implementation
   could return stale baseline samples).
2. **Open the door to bench instruments** via a new
   ``benchctrl.bench`` subpackage, starting with the Eastwood Tech QR10x
   programmable resistance.

### Added — `SMU.read_window(channels, duration_s)`

New public primitive on the SMU: drains a window of inbound samples
and returns them grouped by channel. Like ``read_raw`` but parses
samples; like a brief recording but without the orchestration cost.

```python
samples = smu.read_window([Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT], 0.5)
# {Channel.MAIN_VOLTAGE: [3.2071, 3.2080, ...], Channel.MAIN_CURRENT: [-0.020, -0.020, ...]}
```

Surfaces queued device errors as ``BenchCommandError`` on the next SET,
matching ``read_value``'s semantics. Refuses when a recording is
active (the reader thread owns the byte stream then).

### Fixed — battery profiler

- **Sink-sign convention**: ``set_main_current(positive)`` is *source*
  (push into load), ``set_main_current(negative)`` is *sink* (draw
  from load). The profiler now negates user-supplied positive load
  magnitudes internally — the API stays clean.
- **Stable step-response measurement**: ``_measure_v_i`` now uses
  ``SMU.read_window`` and averages the most-recent half of the window.
  Previously it called ``read_value``, which could return the *first*
  sample seen in its drain window — often a stale pre-step baseline
  value. This made V_loaded read as ~0 V (the output-disabled noise
  floor) on real hardware.
- ``run()`` now explicitly sets the SMU into CC sink mode at start:
  ``set_range("low")`` + ``set_current_limit_enabled(True)`` +
  ``set_power_regulation("current")``.

### Verified — real AA pair

End-to-end profiler run against two AA alkalines in series:

- OCV: 3.187-3.191 V (per-cell 1.594-1.596 V — fresh)
- V_loaded at 20 mA sink: 3.11-3.16 V
- ESR pack: 2.7-3.8 Ω (mean 3.18 Ω), per-cell 1.4-1.9 Ω
  (includes banana plug + cable + Arc internal sense path)
- 10-cycle short profile completed in 15.2 s, used 10.6 µAh total

### Added — `benchctrl.bench` subpackage + `QR10x` driver

New subpackage for non-Arc lab instruments. First driver:
**Eastwood Tech QR10x programmable resistance** — 1 Ω-8.4 MΩ depending
on model, ±0.02-0.05% accuracy, AT commands over USB-Serial (CH340
chip, 115200 8N1).

```python
from benchctrl.drivers.eastwood_qr10x import QR10x

with QR10x.open("COM7") as qr:
    print(qr.info())
    qr.set_safety_limit(12.0)
    qr.set_resistance(10_000)
    qr.actual_resistance()    # 10000.xxx (PV)
```

Public surface: ``info()``, ``set_resistance(ohms)`` /
``get_setpoint()`` / ``actual_resistance()``, ``set_safety_limit(ohms)``
/ ``get_safety_limit()``, ``get_temperature()``, ``incr(delta_ohm)`` /
``decr(delta_ohm)``. Exception hierarchy: ``QR10xError`` ->
``QR10xConnectionError`` / ``QR10xProtocolError`` /
``QR10xTimeoutError`` / ``QR10xValueError``.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- `qr10x_open(port="COM7", baudrate=115200)` / `qr10x_close()`
- `qr10x_info()`
- `qr10x_set_resistance(ohms)` / `qr10x_get_setpoint()` /
  `qr10x_actual_resistance()`
- `qr10x_set_safety_limit(ohms)` / `qr10x_get_safety_limit()`
- `qr10x_get_temperature()`
- `qr10x_incr(delta_ohm)` / `qr10x_decr(delta_ohm)`

Only one QR10x at a time; server holds the connection across tool
calls until `qr10x_close()`.

### Optional dependencies

- `pip install benchctrl[bench]` — declares intent (QR10x driver uses
  pyserial which is already a base dep)
- `pip install benchctrl[bench-visa]` — for future SCPI/VISA-based
  instruments (Rigol DL3031A etc.), pulls in pyvisa

### Tests

- 3 new hardware tests for `SMU.read_window` in `test_smu_stream.py`.
- 19 tests in `test_bench_qr10x.py` (13 hardware-free + 6 hardware on
  COM7). Override port via `BENCHCTRL_QR10X_PORT`.

Total tests: 278 → **300** (assuming all hardware tests run).

### Verified — QR10x driver on live device

First-try success against the connected QR101B-AM-1R on COM7:

- `info()`: serial 00000248, HW 5.1N, FW 5.967KS, TCR 25 ppm/°C
- `set_resistance(100)` → PV 100.038 Ω (0.04% deviation)
- `set_safety_limit(12.0)` accepted device-side
- `incr(900)` → 1000.027 Ω; `decr(900)` → 100.038 Ω
- Internal temp: 24.7 °C

## [0.8.0] — battery emulator (phase 4 of the battery feature suite) — FOUR PHASES DONE

Host-side control loop that drives the SMU as a battery with OCV + ESR
sag. Completes the four-phase battery feature suite: profile I/O, life
calculator, hardware profiler, and emulator.

### Added — `benchctrl.battery.emulator`

- **`EmulatorConfig`** dataclass — profile + initial SoC + series + parallel
  + temperature + soc_tracking + update_interval_s + safety_max_voltage_V
  + safety_max_used_mAh + soc_floor + current_read_timeout_s.
- **`EmulatorState`** dataclass — SoC, used capacity (mAh), OCV (V),
  ESR (Ω), output voltage (V), measured current (A), runtime, iteration,
  running flag, stop reason.
- **`Emulator(smu, config)`** with `start()`, `stop()`, `state()`,
  `run_for(seconds)` lifecycle. Background daemon thread runs the
  control loop at the configured update interval (default 100 Hz).
- Control loop algorithm: read mc, integrate I·dt → SoC, look up
  OCV(SoC) + ESR(SoC) from the profile, apply series/parallel
  multipliers, write `V = OCV − I·ESR` clamped to safety_max_voltage_V.
- Safety stops on `safety_max_used_mAh` and `soc_floor`. Output is
  always disabled in a `finally` block before `stop()` returns.

### Modes

- **`soc_tracking=True`** (default) — cell drains as DUT draws current.
- **`soc_tracking=False`** — SoC pinned at initial value. ESR sag still
  applies; cell doesn't appear to run down. Steady-state characterisation.

### Series / parallel

- `series=N` multiplies OCV and ESR.
- `parallel=N` divides ESR and multiplies effective capacity.

### Bandwidth

USB-driven host loop tops out around ~ms-scale latency per read+write
cycle. 100 Hz default update rate handles steady-state and slow
transients well (anything > ~10 ms response). Sub-ms ESR tracking
needs firmware-level regulation — out of reach for the host loop and
out of scope for benchctrl today.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_emulator_start(profile_path, initial_soc=1.0, series=1, parallel=1, temperature=None, soc_tracking=True, safety_max_voltage_V=5.0, update_interval_s=0.01, safety_max_used_mAh=None, soc_floor=0.0)`** —
  start the emulator. Only one at a time. Refuses with structured
  guidance if one's already running.
- **`battery_emulator_state()`** — snapshot.
- **`battery_emulator_stop()`** — stop + disable output. Idempotent.

### Tests

14 new tests in `tests/test_battery_emulator.py` against a `_MockSMU`
that models a DUT drawing constant current:

- Input validation (4): rejects bad SoC, zero capacity profile, zero
  series/parallel, zero update interval.
- Run behaviour (10): seeds output at total OCV on start, disables
  output on stop, rejects double-start, idempotent stop, applies ESR
  sag (V = OCV − I·ESR), tracks SoC over time, `soc_tracking=False`
  pins SoC, safety_max_voltage clamps output, safety_max_used_mAh
  triggers stop, `run_for(seconds)` returns final state, `parallel`
  scales bank capacity, `state()` is thread-safe under load.

Total tests: 262 → **276 passing**.

### Hardware validation

Phase 4 ships green against a mock SMU. Real-DUT validation (connect a
load drawing variable current, verify voltage sag tracks ESR curve) is
a separate hardware task — tracked for when bench hardware is wired
up.

### What this completes

The four-phase battery suite is now feature-complete:

| Capability | benchctrl module |
|---|---|
| Battery profile JSON I/O | `benchctrl.battery.profile` (v0.5.0) |
| Battery life calculator | `benchctrl.battery.calculator` (v0.6.0) |
| Battery profiler (hardware discharge → profile) | `benchctrl.battery.profiler` (v0.7.0) |
| Battery emulator (host-side OCV + ESR loop) | `benchctrl.battery.emulator` (v0.8.0) |

Profile JSONs interchange bit-for-bit with Otii-format files in both
directions, so cells profiled in either tool can be loaded by the
other.

## [0.7.0] — battery profiler (phase 3 of the battery feature suite)

Hardware orchestration: drive a real battery through a configured
discharge profile, measure V/I per cycle, build a profile JSON.

### Added — `benchctrl.battery.profiler`

- **`ProfilerConfig`** dataclass — discharge profile + battery
  metadata + temperature + measurement-window / relaxation /
  initial-settle timing + progress throttle + sample cap.
- **`ProfilerSample`** dataclass — one captured cycle:
  iteration, timestamp, OCV, loaded voltage, loaded current, ESR,
  cumulative capacity consumed (mAh).
- **`ProfilerResult`** dataclass — final BatteryProfile + samples +
  runtime + stop reason + aborted flag.
- **`Profiler(smu, config).run(progress=None)`** — synchronous
  orchestrated discharge. Alternates between high and low current
  steps, measures OCV after relaxation, computes ESR from step
  response. Stops on exit conditions (iteration limit, OCV cutoff,
  loaded-voltage cutoff), `Profiler.abort()` from another thread or
  the progress callback, or `sample_cap` reached.
- Profile auto-tags `software_version="benchctrl/<ver>"` and queries the
  connected SMU for `firmware_version` + `device_id` to populate
  the `device` metadata block (round-trip-compatible with Otii's
  format).

### Constraints

- **Step duration**: minimum 50 ms. Profiles with extremely fast high
  pulses (e.g. Otii's CR2032 default of 2 ms) are rejected with a clear
  error.
- **Mode**: `"current"` only in v0.7.0. `"power"` and `"resistance"`
  modes raise on construction; tracked for v0.7.x.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_profiler_estimate_duration(capacity_mAh, high_current_A, high_time_s, low_current_A, low_time_s)`** —
  pre-run estimate. Returns cycle count, total seconds, human-readable
  duration. Use before kicking off a long run.
- **`battery_profiler_run(output_path, ..., capacity_mAh, nominal_voltage_V, ...)`** —
  full synchronous run; writes profile to disk. Documented warning that
  most MCP clients will time out before a real profile completes —
  Python `Profiler` API recommended for anything beyond a short demo.

### Tests

14 new tests in `tests/test_battery_profiler.py` using a `_MockSMU`
that models a linear-decay cell with configurable ESR:

- Input validation (4): rejects too-short steps, negative currents,
  unsupported modes, zero capacity.
- Run behaviour (10): produces correct sample count, stops on OCV
  cutoff, disables output at end, alternates high/low steps,
  computes non-negative ESR, progress callback fires + survives
  exceptions, abort halts after current cycle, built profile
  round-trips through JSON, runtime is recorded.

Total tests: 248 → **262 passing**.

### Hardware validation

Phase 3 ships green tests against a mock SMU. Real-battery validation
is a separate hardware task (needs an actual cell on the output
terminals) — tracked for a follow-up when bench hardware is wired up.

## [0.6.0] — battery life calculator (phase 2 of the battery feature suite)

Pure-Python duty-cycle life estimator on top of benchctrl's profile
format from v0.5.0.

### Added — `benchctrl.battery.calculator`

- **`DutyCycle`** dataclass — active/sleep load pattern with computed
  `cycle_time_s`, `cycle_charge_C`, `average_current_A` properties.
- **`LifeEstimate`** dataclass — runtime + iterations + capacity
  consumed + self-discharge loss + safety margin loss + final voltage
  (profile method only) + method + stop reason.
- **`estimate_life_constant_current(capacity_mAh, duty_cycle, ...)`** —
  analytic estimator. Treats the cell as a flat-voltage reservoir.
  Optional self-discharge (% per month) and safety margin (%). Returns
  infinite runtime for zero drain.
- **`estimate_life_from_profile(profile, duty_cycle, temperature=None, ...)`** —
  iterative estimator against a `BatteryProfile`. Looks up OCV at each
  cycle's used-capacity point. Stops at cutoff voltage (defaults to
  profile's own) or usable-capacity exhaustion. Matches Otii's Battery
  Life Calculator semantics.
- **`duty_cycle_from_recording(rec, active_window, sleep_window, channel="mc")`** —
  extract a DutyCycle from a captured Recording by averaging main
  current over user-selected time windows. Otii's "Get from selection"
  workflow.

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_life_estimate(capacity_mAh, active_current_A, active_time_s, sleep_current_A, sleep_time_s, ...)`** —
  constant-current estimator
- **`battery_life_estimate_from_profile(profile_path, ...)`** —
  profile-based estimator
- **`battery_life_from_recording(recording_path, active_window_start_s, ..., profile_path=None, capacity_mAh=None, ...)`** —
  end-to-end: load saved capture, extract windows, estimate

### Verified

CR2032-Energizer with a typical IoT load (20 mA / 100 ms / 60 s cycle ≈
38 µA average): ~250 days from the constant-current estimator; ~208
days with 1%/month self-discharge + 10% safety margin. Real numbers,
match physical intuition.

### Tests

18 new tests in `tests/test_battery_calculator.py`:
- DutyCycle properties (cycle time, cycle charge, average current)
  + input validation
- Constant-current estimator: simple math, safety margin scaling,
  self-discharge, zero-drain infinity, input validation
- Profile-based estimator: flat-profile equivalence to CC, voltage
  cutoff, safety margin scaling, real CR2032 sanity check
- DutyCycle extraction from synthetic Recording + edge cases
- Humanize formatter (seconds → "5 days 3 hours …")

Total tests: 230 → **248 passing**.

## [0.5.0] — battery profile format (phase 1 of the battery feature suite)

First of four phases of the battery feature suite. This release
nails the **profile JSON format** — the data structure every
subsequent battery feature reads / writes. Compatible with the JSON
shape used by the Otii desktop app so bundled cell profiles round-trip
cleanly.

### Added — `benchctrl.battery.profile`

- **`BatteryProfile`** dataclass with nested `Battery`,
  `DischargeTable`, `DischargeProfile`, `DischargeStep`,
  `DischargeSample`, `ExitConditions`, `DeviceInfo` types — covers every
  field the Otii format uses.
- **`BatteryProfile.load(path)` / `.save(path)`** — JSON I/O. Output
  format is bit-identical to Otii's bundled profiles (`%LOCALAPPDATA%\\otii3\\app-*\\resources\\batteryprofiles`).
- **`profile.ocv_at(used_capacity_mAh, temperature=None)`** — linearly
  interpolate the open-circuit voltage at a given used capacity.
- **`profile.esr_at(used_capacity_mAh, temperature=None)`** — same for
  the equivalent series resistance.
- **`profile.select_table(temperature=None)`** — picks the nearest
  discharge table for multi-temperature profiles.
- **`profile.summary()`** — JSON-friendly summary (nominal V/C, cutoff,
  temperatures, per-table extents).

### Verified

- All 8 profiles bundled with Otii 3.7.2 (AA, AAA, CR123A, CR2, CR2032,
  LiPo at three temperatures) load, round-trip, and re-save as
  bit-identical JSON — tested in-suite via a hardware-free test that
  walks the Otii install directory.
- CR2032 interpolation matches the bundled profile's first sample to
  3 decimal places (V) and 3 decimal places (Ω).

### Added — MCP tools (per SDK ↔ MCP parity principle)

- **`battery_profile_summary(path)`** — load + return summary
- **`battery_profile_lookup(path, used_capacity_mAh, temperature=None)`** —
  return interpolated OCV / ESR at a point

Both work on saved files; no SMU connection required.

### Documentation

- **`docs/battery.md`** — feature plan, phased status, JSON schema,
  code recipes (loading bundled profiles, building synthetic ones,
  merging multi-temperature data).
- Skill ([`skills/benchctrl/SKILL.md`](skills/benchctrl/SKILL.md)) gains a
  "Battery features" section pointing at the new subpackage.

### Tests

- 18 new tests in `tests/test_battery_profile.py`:
  - 13 synthetic-data tests (interpolation, clamping, JSON round-trip,
    multi-table selection, empty-profile edge cases, unit normalisation)
  - 5 real-profile tests (gated by Otii install presence): load every
    bundled profile, bit-identical round-trip, known-value
    interpolation checks, LiPo power-mode detection, multi-temperature
    file enumeration

Total tests: 212 → **230 passing**.

## [0.4.1] — MCP server synced with v0.4.0 output formats

Pure additive change to keep the MCP server in lock-step with the SDK
(see the new ["SDK ↔ MCP parity principle"](docs/mcp.md#sdk--mcp-parity-principle)
section). Three new tools, plus extensions to `record`.

### Added — MCP tools

- **`record(..., save_path="run.parquet")`** — the existing `record`
  tool now handles `.parquet` save paths via the v0.4.0 Parquet output.
  Requires `benchctrl[parquet]`.
- **`record(..., plot_png="run.png")`** — new optional param; when
  given, also renders a matplotlib quick-look PNG in the same call.
  Requires `benchctrl[plot]`.
- **`plot_recording(input_path, output_png, channels=None, title=None)`** —
  load a saved `.opensmu` file and render a PNG. No SMU connection
  required.
- **`recording_summary(input_path)`** — load a saved `.opensmu` and
  return its name, start/end times, offset, device metadata, and
  per-channel statistics. No SMU connection required.
- **`export_recording(input_path, output_path)`** — convert a saved
  `.opensmu` to another format (CSV / JSON / Parquet / benchctrl)
  based on the output extension. No SMU connection required.

MCP tool count: 23 → **26**.

### Documentation

- New section in [`docs/mcp.md`](docs/mcp.md#sdk--mcp-parity-principle):
  the SDK ↔ MCP parity principle with a table mapping SDK features to
  their MCP equivalents (and explicit exceptions — `to_numpy()` /
  `to_pandas()` don't cross the MCP serialisation boundary; use
  `save_parquet` / `plot_recording` instead).

### Tests

- 6 new hardware-free tests covering `recording_summary`,
  `plot_recording`, and `export_recording` against synthetic recordings.
- 3 new hardware-required tests covering `record(save_path=".parquet")`,
  `record(plot_png=…)`, and combined save+plot in one call.

Total: 203 → **212 tests passing**.

## [0.4.0] — output formats: numpy, pandas, parquet, matplotlib

Recordings now offer first-class export to the scientific-Python stack,
all gated by **strictly optional** dependencies. The base install pulls
only pyserial; you only need the extras for the features you actually
use.

### Added

- **`Recording.to_numpy(channel)`** → 1D float32 `numpy.ndarray` of values.
  Install with `pip install 'benchctrl[numpy]'`.
- **`Recording.timestamps_numpy(channel)`** → 1D float64 `numpy.ndarray`
  of synthesised timestamps (offset-adjusted).
- **`Recording.to_pandas(channel=None)`** — returns a `pandas.Series`
  if a channel is given, or a wide `pandas.DataFrame` (one column per
  channel, NaN-padded where rates differ) if not.
  Install with `pip install 'benchctrl[pandas]'`.
- **`Recording.save_parquet(path, compression="snappy")`** → Apache
  Parquet file. Wide form, columnar, ~10-20× smaller than the equivalent
  CSV. Opens cleanly in pandas, polars, duckdb, Excel via Power Query,
  and Apache Arrow tooling. Embeds channel units/labels/wire-ids and
  the recording name as column-level metadata.
  Install with `pip install 'benchctrl[parquet]'`.
- **`Recording.plot(channels=None, show=True, title=None)`** →
  matplotlib `Figure` with one subplot per channel and shared x-axis.
  Install with `pip install 'benchctrl[plot]'`.
- **`benchctrl[science]`** umbrella extras key installs parquet + plot
  (which pull in pandas, numpy, pyarrow, matplotlib).
- **`docs/output_formats.md`** — full chooser table covering every
  format (native / parquet / CSV long / CSV wide / JSON / numpy /
  pandas / matplotlib / raw), sizing comparisons, a decision tree, and
  the optional-dependency rule.

### Notes on the optional model

benchctrl imports cleanly without any of `numpy`, `pandas`, `pyarrow`, or
`matplotlib` installed — verified by an in-suite test that blocks these
modules in a child interpreter and confirms `import benchctrl` succeeds
with none of them loaded.

Each method's import is lazy: the dependency is only imported the
moment the method is called. If the dep is missing, the method raises
a clear `ImportError`:

```
ImportError: save_parquet() requires pyarrow.
Install with: pip install 'benchctrl[parquet]'
```

### Sizing example

Same 5 s recording of `mc` (4 kHz) + `mv` (1 kHz) across formats:

| Format | Size |
|---|---|
| `.opensmu` (native) | ~40 KB |
| `.parquet` (snappy) | ~50 KB |
| `.csv` (wide) | ~200 KB |
| `.json` | ~600 KB |
| `.csv` (long) | ~750 KB |

For a 30-minute battery-profiling capture (7.2 M samples on `mc`), the
gap widens — parquet stays around ~10 MB while CSV crosses 250 MB.

### Tests

16 new tests in `tests/test_recording_export_extras.py`:
- numpy: 5 tests (shape/dtype/values/timestamps/offset/empty-buffer)
- pandas: 3 tests (Series, wide DataFrame, empty)
- parquet: 3 tests (round-trip, embedded metadata, compaction)
- plot: 3 tests (subplots-per-channel, channel subset, empty-rejection)
- lazy import: 2 tests (clean benchctrl import without deps, friendly
  ImportError when calling a method without its dep)

Total tests: 187 → **203 passing**.

## [0.3.1] — Claude Code skill

### Added

- **`skills/benchctrl/SKILL.md`** — a Claude Code skill that complements
  the MCP server. The MCP server lets Claude *drive* the device; the
  skill guides Claude when *writing benchctrl Python code* (custom
  analysis, batch processing, plotting, transient detection, anything
  beyond the 23 tool surface).
- The skill covers: the two integration paths (MCP vs Python),
  context-manager + safety patterns, the channel-code quick reference,
  recording analysis recipes, exception hierarchy, anti-patterns
  (don't use the Otii server, don't call deferred methods, don't write
  your own framing), and copy-paste recipes for voltage sweep,
  transient detection, live monitoring, and batch processing.
- Install instructions added to [`docs/mcp.md`](docs/mcp.md) — symlink
  (recommended, stays in sync with repo) or static copy. Lands at
  `~/.claude/skills/benchctrl/SKILL.md`.

### When does the skill activate?

Whenever the user is doing anything with benchctrl beyond what MCP tools
cover. Frontmatter description: "Use when controlling a Qoitech Otii
Arc Pro source-measurement unit, writing code with the benchctrl Python
library, analysing captured .opensmu recordings, or building
measurement automation."

## [0.3.0] — MCP server

Open the Arc Pro to any MCP-aware client (Claude Code, Claude Desktop,
Cursor, custom agents) as a set of structured tools.

### Added

- **`benchctrl.mcp`** — a FastMCP server exposing 23 tools covering every
  user-facing capability of the library: device info, every setpoint,
  output enable (with safety guards), live reads, snapshot, synchronous
  recording with statistics, GPIO/UART, connection management.
- **`benchctrl-mcp` console script** — `pip install benchctrl[mcp]` and run
  `benchctrl-mcp` to start the stdio server.
- **`docs/mcp.md`** — install, configuration (Claude Code + Claude Desktop
  JSON snippets), full tool reference, safety model, troubleshooting.
- **Safety model** for `enable_output`: refuses unless all three guards
  pass (current_limit set, voltage set, `confirm_dut_attached=True`).
  Returns structured `{"error": ..., "guidance": ...}` responses so the
  LLM gets clear feedback on how to proceed.
- **23 new tests** (6 hardware-free + 17 hardware-required) covering tool
  surface, schemas, state snapshots, and round-trips against the device.

### Verified end-to-end

- `benchctrl-mcp` initializes MCP protocol v2024-11-05 over stdio.
- `tools/list` enumerates all 23 tools with descriptions sourced from
  Python docstrings.
- `tools/call info` returns live device metadata: name=Arc, fw=3.1.3,
  serial=442032203546324D3230353235313033.

### Optional dependency

- `mcp >= 1.0` — installed automatically with `pip install benchctrl[mcp]`.

## [0.2.0] — 100% decoding sweep

A systematic decode pass across every captured trace exposed the rest of the
wire vocabulary. Result: benchctrl now understands every distinct frame
type the device emits or accepts in the captured corpus.

### Added — newly decoded wire commands

- **GET-parameter interface** (`type=0x64`) — every parameter we can SET, we
  can now read back. New `SMU.get_param(cmd_code)` returns a unified
  :class:`Response` with status + data. Convenience methods:
  `get_device_name()`, `get_hw_version()`, `get_fw_version()`,
  `get_device_id()`, `get_main_voltage_setpoint()`,
  `get_max_current_setpoint()`, `get_exp_voltage_setpoint()`,
  `get_uart_baudrate_setpoint()`, `get_channel_inventory()`.
- **SET_POWER_REGULATION** (`type=0x66 cmd=0x0A`) — `set_power_regulation()`
  now sends a real wire command. Modes map to `voltage=0`, `current=1`,
  `inline=10`, `off=100`.
- **write_tx** (`type=0x82 cmd=0x19` + UTF-8 text) — `SMU.write_tx()` no
  longer raises; sends a variable-length text payload distinct from the
  SET/GET vocabulary.
- **set_tx** (decoded as `set_gpo(3, state)`) — no longer raises. The TX
  pin is the third GPO slot in the GPO bit pattern (bits 6/7 of
  `SET_GPO`).
- **set_gpo(3, state)** — previously rejected, now valid.
- **Prepare-stop (`type=0x7E`)** — `stop_recording()` now sends this 8-byte
  flush frame before the per-channel disable burst, matching vendor
  behaviour. Eliminates a small streaming-mode-switch lag.
- **POLL (`type=0x0A`)** — `encode_poll(seq, timestamp_us)` available for
  the optional ~1 Hz host heartbeat (not sent by default; the device
  works without it).

### Added — decoded inbound formats

- **Unified `Response` parser** — `parse_response(payload)` decodes any
  `0e 03 99 ff`-prefixed frame into `Response(response_seq, status, data)`
  with status conventions `0=OK / -3=N/A / negative=rejected`.
  `Response.as_u32() / as_int() / as_float() / as_text() / as_u32_array()`
  pull typed values from the data field.
- **180-byte `ce f2 2f ff` baseline envelope** — confirmed as a simple
  container holding one `02 00 08 00`-prefixed sample record per channel.
  Already correctly parsed by `iter_samples`' baseline byte-scan; now
  documented in `docs/protocol.md`.
- **Packed sample frame** — already implemented in v0.1.1, now formally
  documented alongside the GET response format.

### Changed — protocol model

- `parse_error_frame` / `parse_set_ack_frame` now key off the response
  status word instead of the legacy `04 10 00 00` / `0x10XX` "discriminator"
  bytes — which we discovered in cap #16 were actually just sequence
  numbers, not type discriminators. The legacy convenience APIs are
  preserved for backwards compatibility, but new code should use
  `parse_response()` directly.

### Defer notes (no change in scope, just clarified)

- **`set_channel_samplerate`** — fails at the Otii server's JavaScript
  layer before reaching the device (cap #42). Most plausible
  interpretation: there is no wire command for this — sample rates are
  hardware-fixed and "sample rate" in the GUI is a post-processing
  downsample. Marked architecturally not-a-wire-command.
- **`calibrate()`** — fires zero wire commands via the API (cap #41).
  The vendor's calibration flow lives somewhere else (likely the
  Desktop GUI's service-mode path). Stub kept.
- **Device-side battery emulation** — not observed in the workflows
  we could capture (cap #40). benchctrl ships a host-side emulator
  instead — see v0.8.0. Stubs kept on the SMU class for surface parity.

### Internal

- `protocol.py` constants: `CMD_SET_POWER_REGULATION`, `CMD_WRITE_TX`,
  `CMD_GET_DEVICE_NAME`, `CMD_GET_HW_VERSION`, `CMD_GET_FW_VERSION`,
  `CMD_GET_DEVICE_ID`, `CMD_GET_CHANNEL_INVENTORY`, `POWER_REGULATION_MAP`,
  `TYPE_PREPARE_STOP`, `TYPE_WRITE_TEXT`.
- `device.py` GET round-trip: `SMU.get_param()` sends a type=0x64 request,
  drains inbound bytes, matches on response_seq, returns the parsed
  `Response`. Requires no active recording (reader thread owns the
  byte stream during recording).
- Hardware-free test coverage expanded from 89 to 105 tests; hardware
  tier from 43 to 59 (132 -> 164 total).

## [0.1.1] — full-rate streaming

### Fixed

- **Full-rate sample streaming unlocked.** Decoded from a fresh wire capture
  of the Otii vendor server: recording is set up by a *per-channel* enable
  command (`[seq][0x78][wire_id][1]`) followed by an 8-byte cleanup
  (`[seq][0x7C]`) — not the 76-byte `69 83 2a ff …` payload v0.1 sent (which
  turned out to be a misread of the device's *inbound* packed-sample
  frame).
- Verified rates on the Arc Pro: mc 4042 sps, mp 4042 sps, mv 1015 sps —
  ~670× improvement on mc/mp and ~170× on mv.
- Sub-millisecond transients on the DUT are now resolvable.

### Added

- `protocol.encode_channel_enable_for_recording(seq, wire_id, enable)` —
  builds the 16-byte per-channel enable / disable payload.
- `protocol.iter_samples()` now auto-detects and unpacks the **packed
  sample frame** format (`69 83 2a ff` + per-channel sub-1 / sub-4
  records + sentinel). Sub-4 records yield 4 samples per frame at
  4× the frame rate.
- Constants `PACKED_FRAME_MAGIC`, `PACKED_FRAME_SENTINEL` for the
  inbound packed frame envelope.
- `TYPE_CHANNEL_ENABLE` constant (alias of the now-misnomered
  `TYPE_STOP_RECORDING`).
- Tightened hardware test asserts `>=1500` mv samples and `>=6000` mc
  samples in a 2 s recording (was `>=5`).

### Internal

- `SMU.start_recording()` now sends a per-channel `type=0x78` enable burst
  + cleanup instead of the legacy 76-byte payload.
- `SMU.stop_recording()` sends symmetric per-channel disables.
- Removed unused `encode_start_recording` and `RecordingChannel` imports
  from `device.py`.

## [0.1.0] — initial release

First public release. Drives the Qoitech Otii Arc / Arc Pro directly
over USB CDC-ACM, in pure Python.

### Added

- `SMU` class with full device lifecycle (`open` / `close` / context
  manager) and the three-step session-init handshake
- Setters for every wire command in the v0.1 scope: main voltage,
  current limit, main current (CC), output enable, range, 4-wire,
  source-current-limit enable, expansion-port voltage, EXP-5V,
  legacy sink, ADC shunt resistor, UART enable + baud rate, GPO pin
  state
- Cached state properties for every setter
- `Channel` enum carrying code, wire id, subtype, sample rate, unit,
  label, and co-enables metadata for all 14 channels
- Per-channel enable / disable, with auto-co-enable for the
  `mc → mp` and `ac → ap` pairs
- `Recording` class — context-managed via `SMU.record()` with a
  background reader thread, or manual `start_recording` / `stop_recording`
- `Recording.statistics` returning `Statistics` (min, max, average,
  rms, sample_count, duration, charge for current channels, energy
  for power channels)
- `Recording.info`, `.data`, `.timestamps`, `.index_at`, `.count`,
  `.crop`, `.downsample`, `.rename`, `.log`
- `Recording.save_csv` (long + wide), `.save_json`, `.save_raw`, and
  `.save` (native `.opensmu` binary) with `Recording.load` for round-trip
- Real-time streaming iterator (`SMU.stream`) yielding typed `Sample`s
- `SMU.read_value` and `SMU.read_raw` escape hatches
- Asynchronous device-error frame surfacing via `BenchCommandError` on
  next API call
- Full pyserial-based discovery (`SMU.discover()`)
- Comprehensive exception hierarchy: `BenchError`, `BenchConnectionError`,
  `BenchProtocolError`, `BenchCommandError`, `BenchValueError`,
  `BenchTimeoutError`, `BenchNotImplementedError`
- CLI: `benchctrl discover / info / set-voltage / set-output /
  set-range / set-current-limit / set-exp-voltage / set-gpo /
  capture / stream`
- 132 tests (89 hardware-free + 43 hardware-required)
- Documentation: getting started, API reference, wire-protocol
  reference, AGENTS.md, design doc, official API inventory,
  ROADMAP, TEST_PLAN, VALIDATION_REPORT
- 4 example scripts: `basic`, `streaming`, `voltage_sweep`,
  `save_and_load`

### Deferred (raises `BenchNotImplementedError`)

- Battery emulation: `set_supply_battery_emulator`,
  `set_battery_profile`, `enable_battery_profiling`,
  `wait_for_battery_data`, and the entire `BatteryEmulator` class
- Calibration: `calibrate()`
- Firmware upgrade: `firmware_upgrade()` (deferred indefinitely —
  bricking risk)
- Channel-level sample rate control: `set_channel_samplerate()`
- UART log channel: `iter_uart_log()`
- TX / RX as GPO / GPI: `set_tx()`, `get_rx()`, `write_tx()`

See `ROADMAP.md` for rationale and pick-up notes for each.

### Known limitations

- Device's baseline streaming rate is ~6 Hz across all channels until
  the (not-yet-decoded) full-rate command is sent. The channel-capability
  rates (1 kHz / 4 kHz) are theoretical maxima.
- Single-device support tested; multi-device API present but exercised
  only with one Arc Pro.
- Windows / Linux / macOS via pyserial — only Windows has been
  hardware-validated in this release.

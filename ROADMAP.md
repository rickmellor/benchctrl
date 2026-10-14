# benchctrl roadmap

What's planned, what's deferred, and why. Entries follow a consistent
format: **status** (current state) + **scope when picked up** (what
landing this would mean) + **why deferred** (the blocker / rationale).

For shipped features, see [`CHANGELOG.md`](CHANGELOG.md). For known
hardware/firmware caps and workarounds, see
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

## Near-term

### Resolve DL3031A LIST timing in Arc Pro high range

**Status**: documented in [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) § F-2.
With the Arc Pro in high range (LiPo profiles, 4.2 V), the DL3031A's
LIST playback fires a partial / misordered sequence instead of the
expected `cycles × n_steps` repeats. CR2032 / CR123A in low range
work cleanly.

**Why deferred**: cause appears to interact with the FUNC:MODE-FIXed
one-way firmware bug (§ F-3). Isolating it cleanly needs an
oscilloscope on the trigger line.

**Scope when picked up**:
1. Scope-capture the DL3031A `:TRIGger` line and the input current
   simultaneously to see what the device actually does at trigger time
   vs LIST playback time
2. If the cause is recoverable in software, build a workaround into
   `run_dynamic_list` (e.g. dummy LIST cycle to "prime" the state
   machine in high range)
3. If not, document it as a hardware limit and move on

### Validation harness: extract sub-second TX bursts from native streaming

**Status**: hires runner captures at ~4 kHz but phase tagging is
host-clock-aligned. Sub-100 ms TX bursts in `--scenario dynamic-list`
work in firmware but phase-tagging accuracy depends on the host
correlating record timestamps with the trigger instant.

**Scope when picked up**:
1. Use the DL3031A's `:TRIGger` as a digital event that the Arc Pro
   can latch on its GPIO line (rear-panel I/O)
2. Embed the trigger instant in the recording's sample stream instead
   of relying on host-clock correlation
3. Achievable phase-tagging accuracy: ±1 sample (~250 µs at 4 kHz),
   vs. ±200 ms today

### Validation harness: DP2031 as a source

**Status**: the DP2031 driver shipped in v1.1.0 with full MCP parity,
but the scenario harness in `scenarios/` still only models the *load*
side. Cell-charging scenarios need the supply as a first-class actor.

**Scope when picked up**:
1. Extend `_LoadAdapter` to also be a `_SourceAdapter`
2. A charge-profile scenario kind alongside static / dynamic /
   dynamic-list
3. Reference captures committed like the existing 27

### Hardware interlock for unattended runs

**Status**: `KNOWN_LIMITATIONS.md § N-1` states the honest position —
the agent's software deadman drives outputs off when contact is lost,
but nothing in software can guarantee an output goes off through a
wedged driver. For genuinely unattended overnight runs that is not
good enough.

The software half has landed: `benchctrl.agent.eventbus` fans events out
without a producer ever touching a socket, and the board's HDMI panel
(`benchctrl.dashboards.fui`, `docs/dashboard.md`) reports bench state
read-only. The **physical** interlock is what remains. An e-stop button
was ordered 2026-08-19 to hang on the board's GPIO; `docs/dashboard.md`
records the four design questions not yet guessed at — chiefly where the
watcher lives so it fires while the agent is blocked in a pyvisa call,
and whether a trip latches until an explicit reset.

**Scope when picked up**:
1. Drive a relay or contactor from the agent host's GPIO on the same
   deadman timer
2. Expose its state in the run's event stream so the artifact bundle
   records whether the interlock ever fired
3. Document the wiring; make it opt-in but loudly recommended in
   `docs/runs.md`

### Revalidate serial transport selection on a desktop Linux host

**Status**: **resolved 2026-09-11** on the Raspberry Pi 5 bench agent
(`benchpi`, Pi OS trixie, kernel 6.18 with `ch341`), QR101A-1M-R1 serial
00000248 — the same unit the userspace path was verified with on the Uno Q,
so the two transports were compared on one instrument:

1. `resolve_ch341_port(port=None)` → `SerialTarget(port='/dev/ttyUSB1',
   bridge=None, how='kernel')`; no pty created, no `_benchctrl_bridge` on
   the driver.
2. `info()` over the kernel tty: `QR101A-1M-R1`, serial `00000248`,
   hw `5.1N`, fw `5.967KS` — identical to the userspace-path answers.
   Setpoint 100.0 Ω reads back 100.038 Ω, the same figure the Uno Q reports.
3. Open → close → reopen: clean.
4. `discover()` reports `/dev/ttyUSB1 — CH340 USB-serial bridge` via
   `scan_serial()`; `scan_driverless_bridges()` returns `[]`.
5. The negative case did **not** fall back — `how="kernel"` both times, and
   there is no code path from a failed kernel open to the bridge. But it did
   not *raise* either, for a reason the original wording did not anticipate:
   a Linux tty is not exclusive by default. pyserial's `exclusive=True` is an
   advisory `flock`, and `QR10x.open` does not set even that, so a second
   process opens the same `/dev/ttyUSB1` and both talk to the instrument.
   On the Uno Q the libusb claim made the userspace path exclusive by
   construction; the kernel path has no such property. Tracked as
   **"Exclusive open on kernel ttys"** below.
6. `test_bench_qr10x.py` with `BENCHCTRL_QR10X_PORT=auto`: 6 passed on the
   Pi through the kernel tty (the fixture now goes through `autoserial`, so
   `auto` means the same thing in the suite as in the agent; the `COM7`
   default still passes straight through). `test_cross_validate_sdm4065a_qr10x.py`
   skipped — no DMM on the Pi's hub yet.

Also settled: the same `agent.json` (no `open.eastwood_qr10x.port`) works
unmodified on both boards; the agent journal shows
`autoserial: kernel ch341 driver present, using /dev/ttyUSB1` on the Pi.

**Still unresolved**: `serial_number=` selection — this CH340G reports
`iSerialNumber=0`, so there is still nothing to match, and only one adapter
has ever been attached. Needs a second adapter, not a second host.

### Exclusive open on kernel ttys

**Status**: found while closing the item above. `QR10x.open` (and the other
pyserial drivers) open a kernel tty without `exclusive=True`, so nothing stops
two benchctrl processes — or benchctrl and a stray `screen` — from sharing an
instrument's port. The agent's single-writer claim (`KNOWN_LIMITATIONS.md`
§ N-4) only governs clients of *one* agent.

**Why deferred**: `exclusive=True` is an advisory `flock`, which protects
against another pyserial/flock-aware opener but not against an arbitrary
`open(2)`; `TIOCEXCL` is the enforced form but is per-fd and cleared on close.
Either is a driver-wide behaviour change across seven drivers and the pty-backed
simulators, so it belongs in its own change with its own tests, not in the Pi
deployment work.

### Transport-layer encryption for remote mode

**Status**: `benchctrl.net` authenticates with HMAC-SHA256
challenge-response, so the token never crosses the wire — but the
traffic itself is plaintext. `docs/remote.md` recommends an SSH
tunnel, which works and is what we use.

**Why deferred**: TLS means certificate management on a board that
someone re-flashes regularly, and the SSH tunnel is a complete answer
for the deployments we have. Worth revisiting if benchctrl ends up on
a network where a tunnel isn't practical.

## Vision

### LED / indicator classifier on the Metis — **shipped 2026-09-12**
The capture-and-label loop (`benchctrl.vision.labelloop`) and the classifier
path (`classify()` / `vision_classify`, `classify=` on a capture, a
`Classification` with a logit margin, region-bound models served by name from
the sidecar) are in. First model: the Pi's own `ACT` LED, 200 frames, 100 %
on a held-out round, trained and compiled on scrub with the Axelera devkit
(`metis` repo `experiments/vision/bench_led/`). Lessons from the metis
board-reader experiment held: real frames beat synthetic, exposure first,
gate on margin, keep the classical-CV sanity read. Left for a later pass:
colour / blink-code classes (a multi-class dataset and a temporal read),
`require N consistent reads` as a driver helper rather than a caller rule,
and a FUI overlay of the current read.

### Hardware trigger for the camera
**Status**: capture is software-triggered from the sidecar, tagged with `seq`.
The Basler's opto-isolated trigger input and the cable are on the bench; wiring
it to a CP2112 line (or a Pi GPIO) would let a *bench event* — a reset pulse,
a PDU switch — fire the exposure with microsecond alignment. Lands on the same
`TriggerSource` seam in `benchctrl.vision.camera` (`Line1` instead of
`Software`); the driver and the wire need nothing. Open question: how `seq`
is assigned to a frame the sidecar did not fire — probably by the agent
stamping the event that pulsed the line.

### A tool that lets a model *look* at the bench
**Status**: `vision_frame`/`vision_trigger_capture` return metadata and write
the JPEG to `save_to`; no tool returns an image. FastMCP can return an `Image`
content block, which would let an agent see the frame directly. Deferred
because there is no image-return precedent in this server and the transcript
cost of a 300 KB frame per call wants a deliberate design (downscale, crop to
ROI, rate-limit), not a one-line addition.

## Signal sources and the scope loop

### SDG1032X arbitrary-waveform upload over LAN — **shipped 2026-09-12**
The SDG is on the bench LAN (`sdg1032x.home.arpa`, 192.168.1.230, static, set
over SCPI). `write_arb`/`read_arb` go over its port-5025 socket with newline
escaping; 16384-point waveforms round-trip byte-exact. Details and the
USB-TMC finding in `KNOWN_LIMITATIONS.md` § F-25. Left: a hardware trigger
and the scope loop below make an uploaded waveform *observable*.

### Rigol DS1000Z oscilloscope driver, then generator→scope sweeps
**Status**: the scope (`1ab1:04ce`) is on the bench and shows as unclaimed.

**Scope when picked up**: a `rigol_ds1000z` driver (waveform capture, trigger,
measurements) built the way the SDG was: read-back everywhere, sim first,
hardware-marked tests; then the cross-instrument loop the bench exists for —
the SDG sweeps (`set_sweep`) or bursts into the DUT, the scope captures, the
counter/DMM cross-check. The FUI's scope quadrant gets the capture.

### Reading the SDG's screen text with the label loop
**Status**: the FUI shows the generator's own screen (`SCDP`) and the camera
sees the same panel. A frequency/amplitude read from the *camera* would be a
third, optical check of every setting.

**Scope when picked up**: the label loop already labels for free — command a
frequency through the driver, capture, the commanded value is the label — so
per-character models over a fixed screen layout are the same recipe as the
Output-key classifiers, and the `SCDP` bitmap is a pixel-perfect reference
for the layout. Needs a multi-region classify call (`docs/vision.md` § V-9
binds a model to one region today).

## Foundation hardening

### Apply the disconnect grace, or stop promising it

**Status**: `KNOWN_LIMITATIONS.md § N-9`, with the measurement.
`SafetyGovernor.grace_for_disconnect()` returns 3 s when armed, has one call
site repo-wide, and that site is a `log.warning` argument. Nothing consumes the
value, nothing on the disconnect path touches `_last_contact`, and
`TripReason.CLIENT_DISCONNECT` is raised nowhere — so the real delay is the
full remaining deadman window. Measured at **6.45 s with `deadman_s=6` and
12.46 s with `deadman_s=12`**, against a logged promise of "≤ 3s" in both
cases. At the shipped default of 15 s the message understates it fivefold.

**Why it matters more than the number**: an operator reads that line at the
moment a client holding a live output died, and decides from it whether to walk
to the bench.

**Scope when picked up**, one of two, and the choice is the work:
1. **Apply it** — back-date `_last_contact` on disconnect so the grace really
   is the window, and trip with `TripReason.CLIENT_DISCONNECT`, which is what
   that enum member exists for. Shortens a safety delay, so it needs a timing
   assertion: every "did it reach safe state" assertion passes on both the old
   and new behaviour, which is exactly how this shipped.
2. **Delete it** — drop `grace_for_disconnect()` and log
   `max(0, deadman_s - seconds_since_contact)`, the honest figure.

Option 1 is better behaviour and option 2 is a smaller change; either is
strictly better than a promise wired to nothing.

**Why deferred**: found while documenting the CLI, and it is agent-side safety
behaviour. Changing when the bench goes safe does not belong in a CLI commit.

### Close the writer-claim escapes in `agent/dispatch.py`

**Status**: found while building the CLI's authorisation table, and it is an
*agent*-side gap rather than a CLI one. `dispatch.py` derives
`DeviceSurface.mutators` from a bare list of name prefixes with no
driver-declared override, so a method that changes the instrument but happens
not to start with `set_`/`reset`/`clear_`/`trigger` is **remotely callable
without holding the writer claim**. Measured against the CLI's explicit
classification, which is the independent second opinion this needs:

| device | escapes | of which tier 1 |
|---|---|---|
| `rigol_dp2031` | 17 | `delete_file`, `install_license`, `load_file`, `recall_state` |
| `siglent_sdm4065a` | 8 | `write`, `query` |
| `rigol_dl3031a` | 3 | `transient_enable` |

28 in total; the Arc, QR10x, PDU, CP2112 and ADU218 have none, because their
naming was chosen with this constraint already in view (see the note in
`cyberpower_pdu41002/driver.py` on why there is no `outlet_on`).

**The worst two are `write` and `query`** on all three SCPI drivers
(`siglent_sdm4065a/driver.py:479,488`, `rigol_dl3031a/driver.py:265,277`,
`rigol_dp2031/driver.py:296,310`). They are arbitrary-command escape hatches —
anything the instrument accepts, including `*RST` and an output enable — and
`is_mutator("write")` is `False`, so a read-only observer can send them. Only
the SDM4065A exposes them as MCP tools today, which is why the other two do not
appear in the table above; the driver methods are equally reachable over the
wire.

**Why the CLI did not just reuse the predicate**: it is wrong in both
directions. `take_snapshot` matches the prefix list and only samples, while
`step_voltage_up` moves a live output and matches nothing. `cli_tiers.py`
classifies all 324 tools explicitly instead, and a test asserts `is_mutator`
still disagrees — so this entry has a tripwire, not just a note.

**Scope when picked up**:
1. Give drivers a declarative way to mark a method as a mutator — a decorator
   or a class-level tuple — so the answer comes from the driver rather than from
   its spelling. Adding prefixes to `_MUTATOR_PREFIXES` is not the fix: adding
   `outlet_` would capture `outlet_state()` and make *reads* require a claim.
2. Cross-check the result against `cli_tiers.py` in a test, so the two
   classifications cannot drift. This is the cheap part and the valuable part.
3. Mutation-test the change: reverting any single method to non-mutator must
   fail a test.
4. Exact-equality mutator tests on the three oldest drivers, which is where
   the naming predates the constraint.

**Why deferred**: it needs its own commit and its own mutation evidence, and it
touches the wire-level authorisation of three instruments — not something to
fold into a CLI change. Nothing is newly exposed by the CLI, which gates all 28
locally regardless of what the agent thinks.

### Make the CI matrix meaningful again

**Status**: every GitHub Actions run on `master` has failed for at least the
last eight commits, from four unrelated pre-existing causes. Recorded in
[`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) § N-7 with the module table. They
are independent, which is the reason none has been fixed: clearing any one
leaves the matrix red, so no single piece of work is visibly worth doing.

**Why it matters more than it looks.** The cost is not the red mark — it is that
a red matrix carries no information, so a *genuine* regression would be
invisible among failures everyone has learned to skip. Right now comparing a
branch against `master` requires diffing collection errors module by module.

**Scope when picked up**, four separable pieces — and they must be done together
to produce any signal at all:

- **Windows collection.** Five modules import POSIX-only stdlib at module
  scope — `sim/loopback.py`, `transports/ptybridge.py`,
  `drivers/cyberpower_pdu41002/links.py`, `drivers/silabs_cp2112/hidraw.py`,
  `drivers/ontrak_adu218/usbfs.py`. The imports are load-bearing (real ptys,
  real USBDEVFS/hidraw ioctls), so the fix is **not** to make them optional —
  it is to decide what Windows CI is *for*. Cheapest honest answer: drop
  `windows-latest` from the matrix, since every device on this bench hangs off a
  Linux host and no Windows target exists. If Windows must stay, the modules
  need platform guards *and* their tests need `skipif`, which is more surface to
  maintain than the coverage is worth.
- **The ruff baseline.** 1198 findings on `master` at CI's scope under ruff
  0.16.3, far newer than the config was written for. It is concentrated enough to
  be tractable: 538 are `UP045` (`Optional[X]` → `X | None`) and 315 are `F401`,
  so ~71% is two mechanical rules, and `--fix` handles a large share. `UP045` is
  safe to apply *despite* `target-version = "py39"` — it fires because 83 of the
  100 modules in `src/` carry `from __future__ import annotations`, which is what
  makes `X | None` legal in an annotation on 3.9, and ruff will not offer the fix
  where it is not. So this piece is genuinely independent of the 3.9 decision
  below. The alternative is to pin ruff to the version the config assumes and
  defer the sweep. Until it is
  cleared, lint claims must compare rule *codes* against a `master` worktree at
  the same scope, never counts: the same tree gives 1105 / 1197 / 1198 / 1262
  depending on which directories are passed.
- **The 3.9 `Install` step**, which fails before ruff or pytest is reached, so that
  row tests nothing at all. The cause is a **packaging** contradiction rather than
  a code defect: `pyproject.toml` declares `requires-python = ">=3.9"`, but the
  `[mcp]` extra pins `mcp>=1.0,<2` and `mcp` itself requires `>=3.10`, so pip
  reports `Could not find a version that satisfies the requirement mcp<2,>=1.0
  (extra == "mcp")` and stops. The 3.9 promise has therefore never been
  installable with extras. Two honest resolutions, and it is a maintainer call
  which: raise `requires-python` to `>=3.10` and drop 3.9 from the matrix, or make
  `mcp` conditional (`mcp>=1.0,<2; python_version >= "3.10"`) and accept that on
  3.9 the MCP server is simply absent. Whichever is chosen, the *claim* in
  `pyproject.toml` and the matrix row should agree — today they do not. Note the
  tooling has already half-decided: `[tool.ruff] target-version = "py39"` but
  `[tool.mypy] python_version = "3.10"`, so the two linters are checking against
  different language levels. Settle all three in one edit.
- **The Ubuntu rows need a VISA backend, and a comment in the workflow says they
  don't.** `1 failed, 2114 passed, 6 skipped, 111 errors` on `master`, every
  error one cause: `Could not locate a VISA implementation. Install either the
  IVI binary or pyvisa-py.` The install step already fetches `[bench-visa]` and
  then explains why it stops there — *"Hardware-free tests use mock objects; we
  install `[bench-visa]` for the pyvisa Python module but don't install a VISA
  backend, which the mocks don't need."* That assumption is **false**, and the
  errors say where: they land on `tests/test_sim_loopback.py`, i.e. the
  simulator tests, which construct a real driver and so reach `ResourceManager()`
  before any mock intervenes. So this is a stale comment rather than a forgotten
  line, and `pyvisa-py` is declared in **no** extra in `pyproject.toml` — § N-5
  documents it as something the operator installs by hand. Two ways out: add
  `pyvisa-py` to the CI install, or give the sim tests a backend-free path so the
  comment becomes true. Cheapest of the four by a wide margin and the only one
  that turns a currently-red row *green*, so it is the sensible place to start.

Deliberately **not** bundled into a driver PR: it touches four drivers, the lint
config, the packaging metadata and the workflow, and would dwarf the diff of
whichever driver happened to notice it.

### Strict mypy

**Status**: `mypy src` runs with `check_untyped_defs = true` but not
strict. CI has it as `continue-on-error: true`.

**Scope when picked up**: Audit the existing `# type: ignore` set,
add return-type annotations on the few functions still missing them,
flip `--strict`. Mostly mechanical.

### Multi-device coordination

**Status**: multiple independent `OtiiArc` instances can already open
on different ports concurrently. Each gets its own thread-safe
protocol session, and as of v1.2 they can live on different machines.
**What's missing**: fan-out helpers (`set_all_main(...)`),
cross-device sync (shared timebase, simultaneous trigger), aggregated
recording.

**Why deferred**: only one Arc Pro available for hardware validation;
designing the API responsibly needs a multi-device rig. Note that
`benchctrl.sim` now makes the *API* design testable without one — a
second simulated Arc costs nothing — so the blocker is narrower than
it was: it is validation, not design.

**Scope when picked up**:
1. Prototype `MultiSMU` against two simulated Arcs
2. Fan `set_*` / `enable_channels` / recording out across devices
3. Decide on timebase strategy (host clock vs designated leader).
   Remote mode makes this harder and more interesting — the honest
   answer may be that cross-machine sync needs a hardware trigger line
4. Acquire a second Arc / Arc Pro and validate
5. End-to-end demo: synchronized capture on 2+ devices

### Retire the ten legacy Arc CLI commands

**Status**: `benchctrl discover|info|set-voltage|set-output|set-range|
set-current-limit|set-exp-voltage|set-gpo|capture|stream` predate the generated
subcommands and are hand-written and Arc-only. They open the Arc directly, so
`--sim` and `--remote` do **not** apply to them and neither does the `--yes`
authorisation model — `benchctrl set-voltage 3.3` needs no flag while
`benchctrl arc set-voltage 3.3` runs through the tier table. Two commands, same
instrument, different rules.

**Why deferred**: they are documented in `docs/getting_started.md` as the CLI
since v0.1, and removing them is a breaking change for anyone's shell scripts
with no deprecation cycle behind it. Not urgent, because the overlap is
confusing rather than dangerous — the legacy path is Arc-only, and the Arc is
not a switching device.

**Scope when picked up**:
1. Emit a deprecation notice on stderr naming the `benchctrl arc …` equivalent
2. Keep them working for one release after that
3. Remove, and fold `--port` handling into the generated open path
4. `capture` maps cleanly onto `arc record`, which is synchronous and takes a
   `--save-path` with the same extension detection. `stream` does **not** map
   onto anything: it prints samples continuously for the life of the process,
   and the generated surface offers only `live` (next sample) and
   `take-snapshot` (a brief drained window). Either `stream` stays, or a
   streaming tool is added — but a tool that never returns is a poor fit for
   the MCP surface the subcommands are generated from, which is the real
   reason it has no equivalent

### Project save/load

**Status**: a `Recording` instance can be serialised to a
self-contained `.opensmu` (msgpack-style binary) or `.csv` / `.json`
via `Recording.save_*()`. The official Otii Desktop's `Project` /
`Otii.open_project()` / `Project.save_as()` produces an opaque
server-managed format we don't replicate.

**Why deferred**: their project format is server-internal; opening
project files cross-vendor isn't a priority. The scenario harness in
`scenarios/` covers the "save a captured experiment" use case
cleanly.

### UART log channel parsing

**Status**: the `rx` channel produces text fragments with
per-fragment timestamps. Raw bytes can be retrieved via
`SMU.read_raw()` and parsed manually.

**Why deferred**: needs a focused capture pass to characterize the
wire-level type-0x0003 records.

**Scope when picked up**:
1. Capture USB traffic of the Otii Desktop displaying a UART feed
2. Add a `RxFrame` parser to `protocol.py`
3. Expose via `Recording.uart_messages()` or similar

## Indefinite — hardware-side constraints

### Internal calibration

**Status**: `SMU.calibrate()` raises `BenchNotImplementedError`. The
Otii TCP API's `Arc.calibrate()` sends **zero wire commands** to the
device — the actual calibration flow lives somewhere other than the
documented API (likely the Desktop GUI's service-mode path or a USB
control transfer outside the bulk endpoint).

**Why deferred indefinitely**: calibration writes persistent state to
the device; an incorrect implementation can degrade measurement
accuracy. The wire format has not been observed, and speculative
bytes are too risky.

**Scope when picked up**: Capture the Desktop GUI's calibration flow,
decode the trigger and progress responses, expose with a clear "this
writes to NVM" warning.

### Firmware upgrade

**Status**: `SMU.firmware_upgrade()` raises
`BenchNotImplementedError` and points users at the vendor app.

**Why deferred indefinitely**: bricking risk. The official
`Arc.firmware_upgrade()` ships an image which puts the device in a
bootloader speaking a proprietary protocol. Interrupted or malformed
upload can leave the device unrecoverable without vendor tooling.
Not worth the risk to replicate.

### Channel sample-rate control as a wire command

**Status**: `SMU.set_channel_samplerate()` raises
`BenchNotImplementedError`. The Otii server's
`set_channel_samplerate` errors at the JavaScript layer before any
bytes reach the device — the most plausible interpretation is that
there is no wire command for this, and "sample rate" in the vendor
GUI is a post-processing downsample applied after capture.

**What works today**: client-side downsampling on `Recording` data
via `Recording.downsample(channel, factor)`. Hardware always streams
at native rates (1 kHz subtype-1 / 4 kHz subtype-4) when recording is
enabled.

## Resolved — historical context

(Kept here briefly so future readers see what was deferred and how it
got picked up. Full release notes in [`CHANGELOG.md`](CHANGELOG.md).)

- **v0.1.1**: Full-rate sample streaming — the "start recording" unlock is a per-channel `[seq:u32][0x78][wire_id][1]` command. Native ~4 kHz on MAIN_CURRENT verified.
- **v0.6.0** through **v0.8.0**: Battery profile I/O, life calculator, profiler, and emulator landed in `benchctrl.battery`. Implemented as a host-side stack on top of benchctrl's existing wire vocabulary.
- **v0.9.0** through **v0.9.6**: Bench instrument drivers (QR10x, RigolDL3031A) including firmware-side LIST / transient / battery-discharge modes; MCP server expanded to 93 tools; validation harness with three scenario kinds.
- **v0.9.7**: Adversarial-review fix-batch — propagating-error model in the emulator, parity catch-up, KNOWN_LIMITATIONS.md, several firmware bugs discovered and worked around.
- **v1.0.0**: Driver-symmetric architecture. Package renamed `opensmu` → `benchctrl`; the Arc stopped being a privileged top-level class and became a peer under `drivers/`, with the `SourceMeasurementUnit` Protocol as the contract.
- **v1.1.0**: Rigol DP2031 driver over four phases — the "hardware test for the DP2031 power supply" item from this roadmap. Landed larger than the ~25 tools estimated here: 134 MCP tools covering the Arb timer sequencer, the IoT power analyzer, trigger I/O and the device filesystem.
- **v1.2.0**: Three items that were never on this roadmap but became obvious once the driver surface stabilised — `benchctrl.sim` (wire-protocol simulators, so the whole stack runs hardware-free), `benchctrl.net` + `benchctrl.agent` (remote mode, instruments on one machine and the agent on another), and `agent/runs` (declarative unattended experiments with a durable event log). All three sit behind `session.resolve()`, so the 226 MCP tools were unchanged.

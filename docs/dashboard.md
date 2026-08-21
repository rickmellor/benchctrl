# The bench status dashboard

A read-only status display on the bench machine's own HDMI panel, plus
the e-stop that will share it. Runs on the board next to the agent; the
panel shows what the bench is doing without an operator opening a laptop.

Status: the read-only display **works and runs on the board**. The event
bus underneath it (`benchctrl.agent.eventbus`), the observer session, and
the FUI console are all in. What is outstanding is the **e-stop**: the
button is ordered, and the design questions below are deliberately still
open. See `ROADMAP.md` § *Hardware interlock for unattended runs*.

## The rule everything here follows

> The dashboard must never be able to change what it displays.

A status display sits beside safety-critical machinery, so every design
choice below falls out of one requirement: **it must be impossible for the
dashboard to block or influence bench operation.** Concretely:

- Producers never block on it. The dashboard cannot apply back-pressure
  to the governor, the run engine, or a driver.
- It cannot keep the bench alive. Its polling must not count as operator
  contact, or it would defeat the deadman.
- It cannot command anything. The one exception, when it arrives, is the
  e-stop — and an e-stop can only ever make the bench *safer*.
- When it falls behind, it says so rather than rendering a stale view.

## The hazard this replaced

Worth recording, because it was live and it is the reason for the
machinery.

`Governor.trip()` emits its `safety_trip` event **before** it drives
instruments to a safe state (`agent/safety.py:214`, disarm loop at 223).
That event went to `BenchAgent._broadcast_event` (`agent/server.py:192`),
which walked every session calling `Session.send_event` →
`FrameWriter.send` → `sock.sendall()` **synchronously**. No send timeout
is set anywhere — `net/frames.py:120` sets one for reads only. And
`trip()` runs on the deadman thread (`agent/server.py:138-144`).

So a single client that stopped reading — a wedged browser, an unplugged
panel, a laptop that suspended mid-run — could stall `sendall()` inside
the governor and **delay disarming an armed instrument**. The
`except Exception` guard around the event sink does not help: it catches a
sink that *raises*, and this failure is a sink that *blocks*.

An always-on HDMI panel is exactly the client most likely to wedge, so
building it on the old fan-out would have turned a rare hazard into a
routine one.

`benchctrl.agent.eventbus` makes the fix structural rather than a
convention: **a producer never touches a socket.**

## Architecture

```
  ┌─ bench board (Uno Q) ─────────────────────────────────────────┐
  │                                                               │
  │  benchctrl-agent (systemd)                                    │
  │    Governor / run engine / drivers                            │
  │        │ offer()  — never blocks, never raises                │
  │        ▼                                                      │
  │    EventBus ── bounded queue + sender thread per subscriber   │
  │        │                                                      │
  │        │ EVT frames (droppable, shallow queue)                │
  │        ▼                                                      │
  │  benchctrl-fui (stdlib http.server, system python)            │
  │    AgentFeed ─► BenchStatus ─► build_view() ─► /api/view      │
  │        │ serves :8600                                        │
  │        ▼                                                      │
  │  chromium --kiosk ──► HDMI panel (card0-DP-1, DP altmode)     │
  │                                                               │
  │  e-stop GPIO watcher ──────────────► Governor.trip()          │
  │      (physical button, bypasses all of the above)             │
  └───────────────────────────────────────────────────────────────┘
```

The display is a **separate process**, not in-process with the agent. It
therefore talks to the agent over `benchctrl.net` like any other client,
which is what makes the read-path rules below protocol-level guarantees
rather than in-process politeness.

It needs no virtualenv and no third-party packages: `http.server` plus three
static files, run from `/usr/bin/python3`. That is not minimalism for its own
sake — the board's root filesystem is ~10 G at 84% full, and the display it
replaced cost 438 M of venv and ~130 M of RSS to render the same facts.

### Why event-driven and not polling

A status panel that polls looks simpler, and on this agent it is actively
unsafe. `agent/server.py:347` calls `governor.touch()` on **every**
inbound frame, and `Governor.touch()`'s docstring says so plainly: *"Record
contact from a client. Any inbound frame counts."* Since

```python
should_trip() == any_armed and seconds_since_contact > deadman_s
```

a dashboard polling `agent.status()` once a second would pin
`seconds_since_contact` near zero forever. If the operator's real client
died with an output armed, the deadman would never fire — and the thing
preventing it would be the status display. The display would be *causing*
the unsafe state it is meant to report.

So the dashboard subscribes to events and reads through a path that does
not mark operator contact.

#### Asking for the observer role is not having it

`AgentFeed` connects with `observer=True`, but only the **agent** can enforce
what that means — it is `agent/server.py` that skips `governor.touch()` for an
observer session. A client that asks and is silently given an ordinary session
gets exactly the hazard above, and every reading on the panel still looks
perfectly current while it happens.

The agent echoes the granted role back in its `WELCOME` specifically so a client
can check, so `BenchStatus.apply_connected` checks it. A missing or false flag
sets `observer_denied`, which:

- takes the headline (`NOT OBSERVER`, severity `critical`) above `STALE` — a
  stale view is a display problem, a held-open deadman is a bench problem;
- makes the view **not** `trustworthy`, so readings render degraded;
- puts `AGENT DENIED OBSERVER ROLE — DEADMAN MAY NOT FIRE` in the footer,
  because "observer" is jargon and the consequence is not;
- **latches**, like `unsafe_latch`. A later reconnect that happens to land on a
  good agent says nothing about how long the deadman was held open.

This is a downgrade detector, not a guarantee: against an older agent, or one
whose observer branch regressed, it is the only warning available. A missing key
is the realistic case — an agent predating the role would simply not mention it
— which is why the check is `is not True` rather than a falsy test. Defaulting
an absent safety flag to "fine" is how a check like this comes to pass while
meaning nothing.

### Priority and shedding

Events carry the run engine's existing severity taxonomy
(`agent/runs/spec.py:31`): `debug` < `info` < `warn` < `alarm` <
`critical`. Under back-pressure the **least important queued event is
evicted**, so a `safety_trip` still reaches a panel drowning in `info`
chatter.

Priority is implemented by eviction, never by queue-jumping. Two
consequences, both relied on by consumers:

1. **Order is never permuted.** Shedding only removes entries, so what
   survives arrives in production order and `seq` stays monotonic.
2. **Gaps are always announced.** Drops are counted and reported in-band
   as a coalesced `events_dropped` event, so the panel can display "N
   events dropped — this view is incomplete" instead of quietly lying.
   A silently-stale bench display is worse than a blank one.

An unrecognised severity ranks **above** `info`, not below: a severity
added after this table was written is more likely to be a new alarm class
than new chatter, and shedding something important is the worse error.

The panel's queue is deliberately shallow (`DISPLAY_MAX_QUEUE = 32` vs
`DEFAULT_MAX_QUEUE = 256`). A display wants *current* state; a deep queue
only means it renders history nobody is reading.

### The instrument rail: three sources, one column

The right-hand rail took the first version of this panel from "wrong" to "useful",
and the bug is worth recording because it was not a plumbing failure. All five
slots read `NO LINK` on the development board while **four instruments sat on the
bus, powered and ready**.

The cause: `SafetyGovernor.state_for()` creates a device's `ArmState` *lazily*, on
the first call that could arm something. So `agent.status`'s `safety.devices` is
`{}` on a perfectly healthy idle agent, and an idle bench and an empty bench are
indistinguishable in that field. Nothing in the panel had asked whether the
hardware existed — only what it was doing.

Three independent facts, three sources, and blurring them is what produced the
five-`NO_LINK` rail:

| Question | Source | Cost |
|---|---|---|
| What is it *doing*? | `safety.devices` in `agent.status` | ~5 ms |
| Does the agent *serve* it? | `registry.describe()`, rides along in `WELCOME` | free |
| Is it *attached*? | `agent.discover` | ~1.65 s |

That cost column is the whole reason discovery is a separate poll with its own
cadence (`DEFAULT_INVENTORY_S = 30 s`) rather than riding the status loop.
Measured on the board: `agent.discover` 1649/1650/1691/1754/1656 ms against
`agent.status` at 105 ms cold then 5 ms warm — a factor of ~300, because
identifying a USB-TMC instrument means reading its string descriptors over
libusb. Both RPCs were already in `OBSERVER_METHODS`, so none of this widens what
a display is permitted to do.

A dark slot now says *which* dark it is, because the operator's next action
differs for each. Precedence runs most-actionable-first, so a live arm state can
never be displaced by a configuration detail:

| State | Means | Next action |
|---|---|---|
| `OPEN FAILED` | the agent tried to open it and failed | read the error; usually a udev rule |
| `NOT SERVED` | not in the agent's `--devices` list | fix the config |
| `NO ID` | scanned, not found, but *unscannable* | nothing — see below |
| `NOT FOUND` | scanned and genuinely not on the bus | plug it in |
| `SCANNING` | nobody has looked yet | wait |
| `STANDBY` | served, on the bus, not opened yet | nothing; this is a healthy bench |
| `NO LINK` | no session, so none of the above is known | fix the link |

Three distinctions in that table are doing real work:

- **`NOT FOUND` vs `SCANNING`.** A measurement of absence and the absence of a
  measurement look identical on a screen and are not the same fact. Collapsing
  them makes the panel assert an empty bench for the first seconds after every
  boot — the window an operator is most likely to be looking at it.
- **`NOT FOUND` vs `NO ID`.** `eastwood_qr10x` is the one declared device key with
  **no VID/PID signature** in `discovery.SIGNATURES`; it sits behind a generic
  CH340 bridge whose USB ID says nothing about what is on the other end, and
  `inventory()` never runs the serial probe that could tell. A scan therefore
  *structurally cannot find it*, so `NOT FOUND` there would be a false negative
  dressed as a measurement. It is also why the bus counter reads `N/4`, not
  `N/5`: counting an unfindable instrument in the denominator would make a fully
  populated bench read as incomplete forever. The undecidable set is read from
  `discovery.SIGNATURES` per frame rather than hardcoded, so adding a signature
  upgrades the slot automatically instead of leaving it saying "cannot tell"
  after the thing that could tell arrived.

  `discover(probe=True)` exists and will identify such a device by asking it
  `AT+DEV.TYPE?`, but it is off by default and cannot rescue this slot on the
  development board anyway. Probing writes bytes at hardware, so it is opt-in, it
  never touches an already-identified instrument, and it needs an openable port —
  and the board's kernel ships **no `ch341` module** (only `usbserial.ko`), so the
  CH340 has no `/dev/ttyUSB*` and discovery reports `path="auto"`. The QR10x is
  fully reachable there through the driver's userspace bridge — it answers with
  `QR101A-1M-R1`, serial `00000248` — but only by *opening* it, which is not
  something a status display may do. `NO ID` is therefore the honest state for
  that slot rather than a gap waiting to be plumbed.
- **`NOT SERVED` vs no device table at all.** An agent that never sent a table is
  not an agent that sent one omitting the key. Without `registry_known`, every
  slot reads `NOT SERVED` against an older agent and on the first frame.

Staleness gets **opposite** treatments on purpose. On disconnect, arm state is
kept and marked `inferred`; presence is dropped outright. A stale `ARMED`
over-warns, which is the safe direction. A stale `ATTACHED` under-warns by
asserting something about the physical bench that nobody has checked since the
link died.

Hardware the scan finds that no driver claims gets its own list below the rail,
because the rail only has rows for devices benchctrl has drivers for, and
"something is plugged in that benchctrl cannot drive" is a real bench fact. On the
development board that list is a CH340 bridge plus an SDG1032X and a DS1000Z
scope — both attached, both identified, neither having a driver yet.

### The rail adapts to the bench, but never to cable state

Slot membership comes from the agent's device table and the last scan; the
presentation table (`INSTRUMENTS`) is a *lookup*, not the rail's contents. The
first version used it directly, which hardcoded the development bench: a user
with one DMM got four permanently dark slots for hardware they do not own, and an
agent serving a device with no entry in that table got **no row at all** — the
rail was silently incomplete with nothing on screen hinting at it. A device with
no spec now still gets a row, labelled from its key (`rigol_dp2031` → `DP2031`),
because an ugly label beats a missing instrument.

A device earns a row by being **served, present, or carrying an open error**, and
each disjunct covers a case the others miss:

- **served but absent** → its row says `NOT FOUND`: there is a cable to plug in.
- **present but unserved** → `NOT SERVED`: the hardware is here and the config
  has a gap. This is why membership is the *union* and not just the served list.
- **neither, but it failed to open** → `OPEN FAILED`. Reachable when an agent is
  restarted with a shorter device list while the recorded failure survives on the
  slot; filtering on served-or-present alone would drop the single row an operator
  could act on, exactly when it appeared.

Anything else — neither served, nor attached, nor ever tried — gets no row,
because nothing on this bench has ever referred to it.

What membership deliberately does *not* follow is what is currently plugged in.
Unplugging an instrument the agent still serves changes its `present` to false and
leaves the row where it was, so the absence is visible rather than a row quietly
vanishing; only a configuration change alters the rail's shape. The counter's
denominator tracks the rail for the same reason — a two-instrument bench must not
read `2/5`, and a bench of nothing but the QR10x drops the bus count entirely
rather than printing `0/0`, which is arithmetic and not information.

### Identity: what the slot *is*, not just what it is doing

Each slot carries the model string and serial number discovery read off the bus
(`hw_label`, `serial_number`, `usb_id`). The serial is the field that makes the
rail auditable: two DP2031s on one bench are indistinguishable by model alone, and
"which supply did that run drive" is answered here or not at all. VISA reports it
as field 3 of the resource string (`USB0::6833::42152::DP2A243500269::0::INSTR`)
even when `serial_number` comes back null, so it is parsed from there — by
splitting on `::`, never by substring search, and handling the NI-VISA form that
omits the trailing interface number. A resource with no serial field yields
nothing rather than a wrong string.

Identity is dropped on disconnect along with presence, and for the same reason: it
is a claim about the physical bench. A slot still displaying `SN DP2A243500269`
after the link died is what convinces an operator that the run in front of them
drove the supply in front of them.
Only entries carrying a `usb_id` are listed, so the board's four `/dev/ttyS*`
ports and VISA aliases do not inflate the bench.

What the rail surfaced immediately on the real board is a genuine config gap the
old five-`NO_LINK` version hid: three instruments identified on the bus at
`exact` confidence that the agent does not serve.

## E-stop — two independent mechanisms

Both will exist, and they are not redundant copies of each other.

| | Physical button | On-screen button |
|---|---|---|
| Path | GPIO on the board → agent thread → `Governor.trip()` | touchscreen → dashboard → agent over the network |
| Available when | always, including a dead X server, dead display server, wedged panel | only when the whole display stack is healthy |
| Authority | authoritative | convenience |
| Arrives | ordered 2026-08-19 | with the touchscreen |

The physical button is the real interlock and must work when *everything
else* is broken — that is the entire point of a hardware e-stop, and it is
why it does not route through the dashboard, the network, or the event
bus. The on-screen one is a convenience for an operator already looking at
the panel.

Board GPIO is ready for it with no privileged setup: `/dev/gpiochip{0,1,2}`
are `root:gpiod` mode 0660 and user `arduino` is **already in the `gpiod`
group**. `gpiomon` / `gpioget` are installed; there is no Python `gpiod`
module yet, and no legacy `/sys/class/gpio` (character device only).

This is the software half of `ROADMAP.md` § *Hardware interlock for
unattended runs*, whose honest position in `KNOWN_LIMITATIONS § N-1` is
that no software deadman can guarantee an output goes off through a wedged
driver.

Design questions still open, and deliberately not guessed at:

- Where the GPIO watcher lives so it still fires when the agent's main
  thread is blocked in a pyvisa call.
- Whether a trip **latches** until an explicit reset, and where the latch
  lives. Without a latch, releasing the button could let a run resume.
- How the two mechanisms avoid a confusing combined state — the screen
  showing `ARMED` because its event queue is behind, when the button has
  already tripped.
- Whether `TripReason` grows one `estop` member or one per mechanism. The
  artifact log has to be able to answer "which one did the operator press"
  afterwards, which argues for two.

## Display takeover

The board currently boots to the lightdm greeter. The panel is
`card0-DP-1`, fed by **DP altmode through a USB-C hub**, already
`connected` and `enabled` at 1440x900.

Takeover is by **lightdm autologin into a kiosk session**, not by
replacing the display manager. That is deliberate: `deploy/`'s existing
`benchctrl-display-hotplug` fix finds its X authority by parsing a running
Xorg's `-auth` argv, so it depends on lightdm starting Xorg. Masking
lightdm would break a validated asset and remove the desktop-login
fallback. Keeping it also means xfce stays installed and selectable the
moment autologin is turned off, so recovering the board is editing one
file rather than reflashing it.

## Board constraints worth knowing

Measured on the Uno Q, not assumed:

| | |
|---|---|
| `/` | 9.8 G, 84% full — **1.6 G free**, do not install here |
| `/home/arduino` | separate ext4, **16 G free** — put anything large here |
| RAM | 3.6 G total, ~2.5 G available |
| CPU | 4 cores; a status display gets a small fraction of one, see below |
| Python | 3.13.5, `aarch64`, **no pip**, no `ensurepip`, `EXTERNALLY-MANAGED` |
| Present | Chromium 142, Xorg, lightdm, xfce, `cairo`, `gi`, `pyserial`, `pyusb`, `pyvisa` |
| GPU | `/dev/dri/renderD128`; the kiosk runs `--enable-gpu-rasterization` |

The FUI needs none of that beyond the stdlib, so `deploy/`'s pip-free story
holds for the display as well as the agent: no venv, no wheels, nothing on
`/`.

### What the display is allowed to cost

The rule from the outset was that the display must never be able to contend
with benchctrl. That is a CPU budget, so it was measured rather than assumed —
on the board, on the real panel, in the shipping `--enable-gpu-rasterization`
configuration, by sampling `/proc/<pid>/stat` across the whole Chromium
process tree.

What the first version cost, and where it went:

| | % of one core | % of the 4-core board |
|---|---|---|
| Chromium floor (`about:blank`) | 0.3–5.8 | 0.1–1.4 |
| First FUI, as written | 138 | 34.6 |
| …after the fixes below | 61–99 | 15–25 |

The instructive part is what turned out **not** to matter. Stubbing out the
hologram, the traces, the canvas glow and the background grid *together* moved
it 176% → 147% of a core. Almost the entire cost was one element: a
full-viewport `position: fixed` scanline sweeping the panel, at **48% of a
core on its own** — more than every other animation combined. Shrinking it
140px → 2px changed nothing; removing its glow changed nothing;
`will-change` and `contain:strict` changed nothing. Only *not spanning the
viewport* changed anything. A large fixed layer moving over live content is
something this GPU will not composite cheaply, and no amount of tuning the
element's appearance addresses that.

Two mechanisms keep it in budget, both in `fui.js`/`fui.css`:

- **The sweep is scoped to the hologram panel**, where it overlays static
  chrome instead of the whole page.
- **A frame governor** steps quality down (frame interval, canvas resolution,
  glow) when frames run over budget, and back up only after a long quiet
  stretch. Tiers are `FULL → HIGH → MED → LOW → MINIMAL`; the active tier is
  displayed next to the frame rate in `SYSTEM.MGR`, so a coarsely-drawing
  panel is a fact you can read off the panel rather than something to discover
  with a profiler.

The governor only ever degrades **decoration**. The data clock is a separate
`setInterval` at `POLL_MS`, deliberately independent of the frame loop: a
board too busy to draw a hologram must still update the numbers on schedule.
The frame rate may collapse; the data rate may not.

#### Two things the governor taught us

**The first one was inert, and it looked fine.** It measured cost as time spent
inside the draw calls, which is structurally always ~0 — canvas rasterisation
happens off the main thread, so `performance.now()` around the drawing reported
nothing while the process burned 130% of a core. The tiers, the thresholds and
the comparison were all present and correct-looking, and the ladder never once
engaged. It now measures **achieved-vs-requested frame interval** instead, which
sees load on any thread. Both directions are exercised in
`tests/test_dashboard_fui.py`, which runs the real `fui.js` under node against a
synthetic clock, because this is a bug that reading the file cannot find.

**Saturating the board does not make it step down** — and that is the correct
behaviour, not a failure. Four busy loops on a 4-core board moved the frame
interval from 100 ms to 102 ms and the tier never changed. The loop re-arms on a
`setTimeout` at its tier's interval, so under contention it *yields* rather than
competing for the CPU it was already declining to use. The data clock stayed
alive throughout, which is the half that matters. Step-down exists for a display
that is genuinely too expensive for its host, and it was confirmed on the board
by making frames expensive directly: `MED → LOW → MINIMAL` in 10 s, and back up
to `LOW` about 60 s after the cost was removed.

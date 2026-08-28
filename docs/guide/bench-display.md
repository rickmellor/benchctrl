# The bench display

**The job:** know what the bench is doing by looking up, without opening a
laptop, unlocking anything or typing a command.

A bench host with a monitor on it runs a fullscreen status panel showing what is
armed, what is attached, which mains outlets are energised and what has happened
recently. It is the one part of benchctrl with a graphical interface, and it is
**read-only by construction** — there is no route through it that changes
anything.

This page is how to run it and how to read it. The design reasoning, and the
hazard it was built to avoid, are in [`dashboard.md`](../dashboard.md).

## What it is, and what it is not

It runs **on the bench host itself**, drawing to that machine's own graphics
output. On the reference board that is a monitor on the HDMI end of a USB-C hub.
The board boots straight into it: no greeter, no login, no keyboard needed.

Under the hood it is a local web page — a small HTTP server on the board and a
browser in kiosk mode against `127.0.0.1`. That is an implementation detail with
one consequence worth knowing: it means you can also view the same panel from
your workstation over an SSH tunnel, which is how you should test it before
turning the kiosk on.

It is **not** a remote desktop. There is no VNC, no RDP and no X forwarding
anywhere in benchctrl, and the panel is not a control surface — you cannot arm an
instrument, switch an outlet or start a run from it. The eventual on-screen
e-stop will be the sole exception, and an e-stop can only ever make the bench
safer.

## Why it cannot be a control surface

Worth understanding before you rely on it, because two of the rules below look
like excessive caution and are not.

**The panel holds an *observer* session.** The agent enforces what that means:
`device.call` is not in `OBSERVER_METHODS`, so the display physically cannot
reach a driver. A bug in the display therefore cannot arm an instrument — the
agent would refuse it.

**It subscribes to events rather than polling.** This is the one that matters. The
agent counts *any* inbound frame as operator contact and resets the deadman
timer. A panel polling once a second would pin the deadman open forever, so if
your real client died with an output armed, the thing preventing the safety
timeout from firing would be the status display. It would be *causing* the unsafe
state it exists to report.

**The display asks whether it was really granted the observer role**, because
asking for it is not the same as having it. Against an older agent, or one whose
observer branch regressed, a client that asks and is silently given an ordinary
session gets exactly the hazard above — and every reading on the panel still
looks perfectly current while it happens. If the agent does not confirm the role,
the panel says `NOT OBSERVER` in red at the top and
`AGENT DENIED OBSERVER ROLE — DEADMAN MAY NOT FIRE` along the bottom, and it
**latches**: a later reconnect onto a good agent says nothing about how long the
deadman was held open.

## Setting it up

Two steps, in this order, and the order is not optional.

### 1. Install the display service

```bash
sudo ./deploy/install-fui.sh
```

Then start it by hand and check it from your workstation **before** going any
further:

```bash
# on the board
sudo -u arduino /usr/local/bin/benchctrl-fui

# on your workstation
ssh -L 8600:127.0.0.1:8600 arduino@<board>
# then open http://127.0.0.1:8600
```

It binds **loopback only** by default. That is deliberate: the view names your
instruments, their serial numbers and their arm state, so publishing it on the
LAN is a decision rather than a default. The tunnel above is the intended way to
look at it from elsewhere.

It needs no virtualenv and no third-party packages — `http.server` plus three
static files, run from the system `python3`. On a board whose root filesystem is
84% full that is the whole point: the display it replaced cost 438 MB of venv and
~130 MB of RSS to render the same facts.

### 2. Turn on boot-to-kiosk

Only once the panel renders your bench correctly:

```bash
sudo ./deploy/install-kiosk.sh
sudo ./deploy/install-kiosk.sh --undo    # back to the normal greeter
```

**This removes the board's only local login.** On a board with no keyboard
attached, SSH becomes the only way back in — so the script refuses to run unless
it can see SSH is actually active, and refuses if `benchctrl-fui` is missing,
because that combination boots to a black screen with no prompt.

Takeover is lightdm autologin into a kiosk session, not a replacement display
manager. So xfce stays installed and selectable the moment autologin is off, and
recovering the board is editing one file rather than reflashing it:

```bash
sudo rm /etc/lightdm/lightdm.conf.d/90-benchctrl-kiosk.conf
sudo systemctl restart lightdm
```

### If the panel stays dark

On a board whose display arrives as **DisplayPort altmode over USB-C** — the
reference board has no HDMI port of its own — there is a startup race: if the hub
negotiates DP after Xorg's probe, the kernel sees the hotplug but nothing tells X
to use the output, and the panel stays dark with a connector that reads
`connected` but `disabled`. That is an ordering problem, not a missing driver and
not a bad EDID.

```bash
sudo ./deploy/install-display-hotplug.sh
```

Then trust `journalctl -t display-hotplug -b` rather than `systemctl show` — for
a `Type=oneshot` unit a stale `Result=success` from a previous run is easy to
misread as evidence this one fired.

## Reading the panel

Four panels and a rail.

| Panel | Shows |
|---|---|
| `SYSTEM.MGR` | agent link, whether the view is trustworthy, session role, dropped events, paint rate |
| `STATE.MGR` | the headline, what is armed, active runs, current stage |
| `LOG.MGR` | recent events, newest last |
| `MAINS.MGR` | the PDU: voltage, frequency, load, per-outlet state |
| instrument rail | one slot per instrument, right-hand column |

**A dark slot says *which* dark it is**, because your next action differs for
each. This distinction is the single most useful thing on the panel:

| State | Means | What to do |
|---|---|---|
| `OPEN FAILED` | the agent tried to open it and failed | read the error — usually a udev rule |
| `NOT SERVED` | not in the agent's device list | fix the config |
| `NO ID` | scanned, not found, and *unscannable* | nothing; see below |
| `NOT FOUND` | scanned and genuinely not on the bus | plug it in |
| `SCANNING` | nobody has looked yet | wait |
| `STANDBY` | served, present, not opened | nothing — this is a healthy bench |
| `NO LINK` | no session, so none of the above is known | fix the link |

`NOT FOUND` and `SCANNING` are separate on purpose: a measurement of absence and
the absence of a measurement look identical on a screen and are not the same
fact. `NO ID` is the honest state for an instrument that hides behind a generic
USB-serial bridge — a scan structurally cannot identify it, so `NOT FOUND` there
would be a false negative dressed up as a measurement. It is also why the bus
counter may read `N/4` on a five-slot rail rather than `N/5`.

Each slot carries the model string and **serial number** read off the bus. That
is the field that makes the rail auditable: two identical supplies on one bench
are indistinguishable by model alone, and "which supply drove that run" is
answered here or not at all.

### The staleness rules are asymmetric, and it matters

The panel degrades differently in different places, and each direction is chosen
so that being wrong is being *safe*.

- **A stale instrument slot is struck through and recoloured.** A stale `ARMED`
  over-warns, which is the safe direction.
- **A stale mains outlet keeps its live colour**, dimmed and dashed, never
  recoloured. A stale outlet reading `OFF` says *"the DUT is de-powered"* to
  somebody deciding whether to reach into an enclosure — the most dangerous thing
  this display could imply. "Last seen energised" has to stay readable at a
  minute old.
- **On disconnect, arm state is kept and marked inferred; presence, identity and
  the outlet map are dropped outright.** Those are claims about the physical
  bench, and they are worth nothing once the link that established them has died.

**`MAINS.MGR` has no correcting poll.** The display cannot read the PDU — that
would mean write-grade access to the one device that switches mains — so mains
state arrives only because the bench *pushes* it. If that sweep stops, nothing on
the panel changes by itself and the global stale clock does not help, because
status frames keep arriving. So the panel keeps its own age and shows
`NOT REPORTED` or an explicit age rather than blanks.

**Dropped events are announced, never silent.** Under back-pressure the least
important queued event is evicted, so a safety trip still reaches a panel
drowning in chatter — and the gap is reported in-band as a count, because a
silently-stale bench display is worse than a blank one.

## What it costs the bench

Nothing that contends with benchctrl, and this was measured rather than assumed:
on the board, on the real panel, in the shipping configuration. The first version
cost **138% of one core**; after the fixes it is 61–99% of one core, 15–25% of the
4-core board.

The instructive part is what turned out not to matter. Stubbing out the hologram,
the traces, the canvas glow and the background grid *together* moved it 176% →
147%. Almost the entire cost was one element: a full-viewport scanline sweeping
the panel, at **48% of a core on its own**. Shrinking it 140px → 2px changed
nothing; removing its glow changed nothing. Only not spanning the viewport did.

A frame governor now steps decoration quality down when frames run over budget —
the active tier is displayed next to the frame rate in `SYSTEM.MGR`, so a coarsely
drawing panel is a fact you can read off the panel rather than discover with a
profiler. **It only ever degrades decoration.** The data clock is deliberately
independent of the frame loop: a board too busy to draw a hologram must still
update the numbers on schedule. The frame rate may collapse; the data rate may
not.

## Keeping it current

```bash
./deploy/sync-board.sh --check      # is the board current? change nothing
./deploy/sync-board.sh --restart    # sync, then restart the session
```

After a sync the panel is **still running the old code**, because nothing
supervises it: the kiosk session starts it once and then `exec`s the browser,
discarding the shell that would have respawned it. So `pkill` is the wrong tool —
it would leave the browser sitting on a dead port, on a panel with no keyboard.
Restarting the session is what works:

```bash
ssh -t <board> 'sudo systemctl restart lightdm'
```

`--restart` prints that command rather than running it, since it needs sudo. **The
agent is untouched either way**, deliberately — restarting it disconnects
instruments mid-bench. Restart that one yourself, when nothing is armed.

That `--check` half exists because of a real failure with this exact component:
the display was installed twice from a staging directory older than the repo, so
the installer faithfully installed pre-fix software both times. Every surface
check passed, because the panel *was* up — just not running the version that had
been reviewed.

## The e-stop

The panel will eventually carry an on-screen e-stop, and there will also be a
physical button. They are **not** redundant copies of each other:

| | Physical button | On-screen button |
|---|---|---|
| Path | GPIO → agent thread → governor trip | touchscreen → display → agent over the network |
| Works when | always, including a dead X server or a wedged panel | only when the whole display stack is healthy |
| Authority | authoritative | convenience |

The physical button is the real interlock and must work when everything else is
broken — which is exactly why it does not route through the display, the network
or the event bus. Status: the button is ordered; the on-screen half arrives with a
touchscreen. Neither is shipping yet, so **do not treat the panel as an
interlock** — see [Unattended runs](examples/unattended-runs.md) for what to rely
on instead.

## Next

- [Setting up a bench host](bench-host-setup.md) — the agent this panel observes
- [Unattended runs](examples/unattended-runs.md) — what the panel is showing you overnight
- [`dashboard.md`](../dashboard.md) — the design in full: the hazard it replaced, the event bus, the priority model
- [`deploy/README.md`](../../deploy/README.md) — every installer script in detail

# The bench status dashboard

A read-only status display on the bench machine's own HDMI panel, plus
the e-stop that will share it. Runs on the board next to the agent; the
panel shows what the bench is doing without an operator opening a laptop.

Status: **in progress.** The event bus underneath it has landed
(`benchctrl.agent.eventbus`); the display itself is being built. See
`ROADMAP.md`.

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
  │  dashboard (own venv, Streamlit)                              │
  │        │ serves :8501                                         │
  │        ▼                                                      │
  │  chromium --kiosk ──► HDMI panel (card0-DP-1, DP altmode)     │
  │                                                               │
  │  e-stop GPIO watcher ──────────────► Governor.trip()          │
  │      (physical button, bypasses all of the above)             │
  └───────────────────────────────────────────────────────────────┘
```

The dashboard is a **separate process in its own virtualenv**, not
in-process with the agent. It therefore talks to the agent over
`benchctrl.net` like any other client, which is what makes the read-path
rules below protocol-level guarantees rather than in-process politeness.

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

## E-stop — two independent mechanisms

Both will exist, and they are not redundant copies of each other.

| | Physical button | On-screen button |
|---|---|---|
| Path | GPIO on the board → agent thread → `Governor.trip()` | touchscreen → dashboard → agent over the network |
| Available when | always, including a dead X server, dead Streamlit, wedged panel | only when the whole display stack is healthy |
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
| `/` | 9.8 G, 80% full — **1.9 G free**, do not install here |
| `/home/arduino` | separate ext4, **16.3 G free** — the venv goes here |
| RAM | 3.6 G total, ~2.9 G available |
| Python | 3.13.5, `aarch64`, **no pip**, no `ensurepip`, `EXTERNALLY-MANAGED` |
| Available | `apt` has `python3-pip` 25.1.1 and `python3-venv`; PyPI reachable |
| Present | Chromium 142, Xorg, lightdm, xfce, `cairo`, `gi`, `pyserial`, `pyusb`, `pyvisa` |
| Absent | `numpy`, `pandas`, `tkinter`, `PIL`, `streamlit` |

Every Streamlit dependency has a prebuilt `cp313` `aarch64` wheel
(~104 MB download, ~250 MB installed); only `watchdog` lacks one, and it is
optional. Because `/usr/lib/python3.13/EXTERNALLY-MANAGED` is present, this
is a venv under `/home/arduino`, never a `--user` install.

This does mean the dashboard breaks `deploy/`'s pip-free story, which
exists because the agent must install on a board with no pip at all. The
split is intentional and worth stating: **the agent stays pip-free; the
dashboard is opt-in and needs pip.** A board can run the agent with no
display and no venv, exactly as today.

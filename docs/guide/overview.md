# Overview

benchctrl is a Python control stack for a lab bench. It gives you one
programmable surface over a set of instruments — a source-measure unit, an
electronic load, a power supply, a multimeter, a switched PDU, relays, and
control lines — so that a measurement you can do by hand becomes a measurement
you can run, repeat, and hand to someone else.

## The problem it solves

A bench measurement usually starts as a person, a scope, and a notebook. That
works once. It does not survive being asked *"what was the sleep current on the
build from three weeks ago?"*, and it does not scale to running overnight.

Vendor software gets you partway and then stops. Each instrument has its own
application, its own scripting dialect, and no notion of the others existing. A
test that needs a supply, a load and a meter to cooperate becomes glue code
against three unrelated APIs, and the glue is where the bugs live.

benchctrl replaces the glue:

- **One API for every instrument.** `open()`, configure, measure, close. The
  same shape whether it is a $15 USB breakout or a 6½-digit DMM.
- **The measurement is code**, so it is reviewable, version-controlled and
  re-runnable. A result comes with the exact script that produced it.
- **Instruments compose.** Emulate a battery with one device while a second
  applies a load pattern and a third measures independently. The subsystems
  that need "a source-measure unit" depend on a Protocol, not a product, so
  they do not care which one you have.
- **The bench does not have to be where you are**, and it does not have to
  exist. The same code drives real hardware, hardware on a machine in the lab,
  or wire-protocol simulators on a laptop on a plane.

## Three ways to drive it

All three reach the same code. Pick per task, not per project.

### Python

The full surface. Use it when the measurement has logic in it — a sweep, a
control loop, a decision based on a reading.

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(0.5)
    with smu.record(OtiiArcChannel.MAIN_CURRENT) as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    print(rec.statistics(OtiiArcChannel.MAIN_CURRENT).average)
```

### Shell

Every instrument function is also a subcommand — 324 of them. Use it for the
one-shot question, and in shell scripts and CI.

```bash
benchctrl adu218 relay-states           # what is switched right now?
benchctrl sdm4065a measure-dc-voltage   # what does the meter say?
benchctrl --yes dp2031 set-output 1 on  # writes need explicit authorisation
```

See [`cli.md`](../cli.md).

### An AI agent

The same functions are exposed as [Model Context
Protocol](https://modelcontextprotocol.io) tools, so an agent can run real
measurements: *"set 3.3 V, record for ten seconds, tell me the mean current."*
This is not a chat wrapper — the agent calls the same code your script would.

See [Driving it from an AI agent](agent-harness.md).

## What it runs on

| | |
|---|---|
| **Python** | 3.9 – 3.13 |
| **Platforms** | Windows, Linux, macOS |
| **Hard dependencies** | `pyserial`. Everything else is an optional extra |
| **License** | MIT |
| **Status** | Beta. The SDK surface follows semver and has been stable since 1.0 |

Two instruments need no dependencies at all beyond the standard library — the
relay interface and the control-line breakout talk USB HID directly. That
matters on a small board with no compiler and no package manager, which is
exactly where a bench host tends to end up.

## What it is not

Being clear about this saves you evaluating it for the wrong job.

- **It is not a replacement for vendor calibration software.** Instrument
  calibration and firmware updates stay with the vendor's tools.
- **It is not a real-time control system.** Host-side control loops run at
  ~100 Hz. Where microsecond timing matters, benchctrl programs the
  *instrument's own firmware* to do it — a load's LIST mode plays a sequence
  with sub-100 µs steps while benchctrl records the result. Timing you need
  guaranteed does not travel over USB.
- **It is not a safety system.** It has real safety machinery — authorisation
  gates, allowlists, arm tracking, a deadman timer — and that machinery is
  damage limitation, not a guarantee. Software cannot promise an output goes
  off through a wedged driver. For genuinely unattended work at energies that
  matter, a hardware interlock is the only real answer, and the guide says so
  where it applies.
- **It is not a GUI.** There is a read-only status dashboard for a bench
  display. Everything else is API, CLI or agent.

## How the pieces fit

```
        Python API   ·   CLI (324 commands)   ·   MCP tools (324)
        └──────────────────────┬──────────────────────┘
                               │
                    session.resolve()  — per device, one of:
                    local          remote            simulated
                      │              │                  │
                      │              │                  └─ wire-protocol
                      │              │                     simulator
                      │              └─ bench host over TCP
                      │                 (authenticated, safety-governed)
                      ▼
                 instrument drivers  ──USB──▶  your DUT
```

The important property is the middle row. Whether a given instrument is real,
remote or simulated is resolved **per device**, and nothing above that line can
tell the difference. So a test written against a simulator runs unchanged
against hardware, and a bench can be half in the lab and half on your desk.

[Theory of operation](theory-of-operation.md) explains why it is built this way
and what follows from it.

## Where to go next

- **Is my instrument supported?** → [Supported equipment](equipment-matrix.md)
- **How does it actually work?** → [Theory of operation](theory-of-operation.md)
- **I want to install it** → [Installation](installation.md)
- **I want to see it do something useful** → [Bringing up a board](examples/board-bringup.md)

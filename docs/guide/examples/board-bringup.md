# Bringing up a board

**The job:** a new board has arrived. You want to power it from a known,
current-limited supply, hold it in reset, release it, and see what it actually
draws while it boots — before you trust it with anything.

This is the most common thing anyone does with this bench, and it is worth doing
in the order below, because each step is a claim you can check before the next
one can hurt anything.

## What you need

| Instrument | Job here |
|---|---|
| Source-measure unit (Arc / Arc Pro) | the board's power, and the current measurement |
| CP2112 control-line bridge | pulls the board's `RESET#` low |
| *(optional)* switched PDU | if the board is mains-powered, or its programmer is |

The CP2112 is a ~$15 board and it is on this bench for a specific reason: reset
lines used to be driven by parking a source-measure unit's GPO on them, which
tied up a $1000 instrument for a job a dollar chip does. Freeing the SMU to do
the measuring is the whole point.

If you have no hardware yet, the source-measure unit half of this page works
against a simulator — add `--sim otii_arc` to the `arc` commands, or set
`BENCHCTRL_SIM_DEVICES=otii_arc`. Do a dry run that way first; it is free.

**The CP2112 is the exception.** Its simulator exists and the Python API and
tests use it, but the CLI's `cp2112 open` resolves the device directly rather
than through the session, so `--sim silabs_cp2112` is accepted, logged, and then
ignored — the command fails looking for a real `hidraw` node. Until that is
fixed, dry-run the control-line steps in Python against
`benchctrl.sim.factories.make_cp2112`, or run them on the real bridge.

## 1. Before you energise anything

Two numbers you need to have decided, not guessed:

- **The board's supply voltage**, from its schematic.
- **A current limit** you are confident is above its real inrush and below the
  point where a short does damage. For a small MCU board, 200 mA is usually a
  reasonable first guess. Too tight is a boot that fails mysteriously; too loose
  is a wiring mistake that becomes smoke.

Then check what the reset line is. **Active-low is the overwhelming default** —
`RESET#`, `nRST`, `SRST` — and this whole page assumes it. If yours is
active-high, the CP2112 is the wrong tool: it is open-drain only, so it can pull
a net down and release it, and it cannot drive one up. That constraint is
deliberate (it cannot fight the board's own rail, and it fails safe if you
unplug it), but it does mean an active-high reset needs a relay instead.

Wire it:

- SMU `+` to the board's supply input, SMU `−` to its ground
- CP2112 GPIO of your choice to the board's `RESET#` net
- **CP2112 ground to the board's ground.** Without this the "pull low" has no
  return path and the line will not move.

## 2. Confirm the instruments are there

```bash
benchctrl arc info
benchctrl cp2112 open --allowed-lines 3
benchctrl cp2112 line-states
```

To sweep the whole bench rather than one instrument, use
`benchctrl.discovery.discover()` from Python. Note its `probe=False` default and
leave it alone: a scan reads USB descriptors and sysfs, while a probe *writes* to
a port whose occupant is by definition unknown — and on this bench one of those
ports is a mains contactor's control line.

`--allowed-lines` is not boilerplate. It defaults to **empty**, meaning drive
nothing, and listing a line is you saying *"this pin is wired to a reset net and
pulling it low is a thing I want."* The driver cannot know that, and the failure
it prevents is holding some other board in reset for an afternoon.

Opening the CP2112 changes nothing on the hardware — every pin keeps the
direction and level it had, and the as-found configuration is recorded so
`close` can restore it. So this step is safe to run before you are sure of
anything.

Expect GPIO 3 to come back as an **input**. That is the chip's power-on state,
and for an open-drain reset line an input *is* released.

## 3. Assert reset before you apply power

This is the step people skip, and it is the one that makes the rest clean. If
the board comes up running before you are ready, its first current transient is
gone and you cannot get it back without cycling power again.

```bash
benchctrl cp2112 set-line-mode 3 on
benchctrl --yes cp2112 set-line-asserted 3 on
```

Two things about that pair. Configuring the direction needs no `--yes` —
an open-drain output that has not been asserted drives nothing. **Asserting it
does**, because that is the call that reaches into the DUT.

And `--yes` is a **global** flag, so it goes before the device group.
`benchctrl cp2112 set-line-asserted 3 on --yes` is an argument-parsing error,
not an authorisation — worth knowing before you retype a command you thought you
had already authorised.

`set-line-mode` configures GPIO 3 as an **open-drain output**; the driver will
not configure a push-pull output at all, so there is no way to accidentally
fight the board's pull-up. Both commands return the **verified read-back** state
rather than assuming the write landed.

If `set-line-asserted` refuses with *"read back released"*, the line is being
driven high by something else — a programmer, a debug probe, a supervisor chip.
Open-drain cannot win that fight, and the driver tells you rather than reporting
success.

## 4. Apply power

Three separate facts, in this order:

```bash
benchctrl arc set-current-limit 0.2
benchctrl arc set-voltage 3.3
benchctrl --yes arc enable-output --confirm-dut-attached on
```

The two setpoints need no `--yes`: with the output off they cannot energise
anything, and requiring confirmation for `set-voltage` would train you to pass
`--yes` reflexively, which is how a confirmation stops being one. Only
`enable-output` is gated.

`enable-output` refuses unless the limit and the voltage have both been set and
you have passed `--confirm-dut-attached on` — it takes a value and defaults to
`off`, so forgetting it is a refusal rather than a silent energisation. Each is a
different claim and none can be inferred from the others: the limit bounds the damage, the voltage says
something knows what is about to be driven, and the confirmation is you saying
there is a board on the other end of the leads.

The board is now powered and held in reset. Read what that costs:

```bash
benchctrl arc live --channel mc
```

On a small MCU board this is typically a few hundred microamps to a couple of
milliamps — leakage, pull-ups, and any always-on regulator. **Write it down.**
It is the number you will compare everything else against, and it is the one
measurement in this whole page that cannot be repeated once the board is
running.

## 5. Release reset while recording

Start the capture first, then release. In Python, because you want the samples:

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel
from benchctrl.drivers.silabs_cp2112 import CP2112

with OtiiArc.open() as smu, CP2112.open(allowed_lines=(3,)) as lines:
    with smu.record(OtiiArcChannel.MAIN_CURRENT, OtiiArcChannel.MAIN_VOLTAGE) as rec:
        time.sleep(0.5)                             # baseline, held in reset
        lines.trigger_reset_pulse(3, duration_s=0.05, settle_s=0.0)
        time.sleep(5.0)                             # let it boot
    rec.save("bringup.opensmu")

print(rec.statistics(OtiiArcChannel.MAIN_CURRENT))
print(rec.statistics(OtiiArcChannel.MAIN_VOLTAGE))
```

Use `trigger_reset_pulse`, not two `set_line_asserted` calls. It releases in a
`finally`, so a `Ctrl-C` mid-pulse cannot leave your board held in reset — which
is the one state that leaves the bench worse than untouched. It also refuses a
pulse shorter than **5 ms**, because each GPIO transition is its own USB control
transfer: the floor is bus scheduling, not the chip, and a 1 ms request would
silently become about 3 ms. If you need a microsecond-accurate pulse you need a
different instrument, and the driver says so rather than handing you a wrong
one.

That half second of baseline before the pulse is what makes the trace readable.
Without it you have a boot transient with nothing to compare it to.

## 6. Read the trace

Three things to look for, in order of how often they are the problem:

**Did the rail sag?** Compare `MAIN_VOLTAGE` minimum against your setpoint. A
board whose inrush exceeds your current limit does not fail loudly — the SMU
regulates down, the rail droops, and the board browns out and resets in a loop.
The symptom is a *periodic* current pattern, and it looks like firmware
misbehaving. If the voltage minimum is well below the setpoint, raise the limit
before you debug the firmware.

**Did the peak exceed your limit?** If peak is pinned exactly at the limit, you
have not measured inrush — you have measured your limit.

**Does the running current match the datasheet?** The gap between held-in-reset
and running is the firmware's cost. A board drawing its held-in-reset current
after release never came out of reset. A board drawing far more than expected is
usually a peripheral left enabled or a pin fighting a pull-up.

The `settle_s` argument to `trigger_reset_pulse` exists for the next step:
whatever you measure immediately after release is a board booting, not a board
running. Give it your boot time before you start calling numbers "idle".

## Bring it down cleanly

```bash
benchctrl arc disable-output
benchctrl cp2112 reset-lines
benchctrl cp2112 close
```

`disable-output` is never gated — reaching safety takes no `--yes`, on purpose,
because an operator fighting a live output should not find a gate in the way.
`reset-lines` returns every allowed line to an input, releasing anything held,
and only touches lines in the allowlist. `close` restores the pin configuration
that was there when you opened.

In Python the `with` blocks do all of this on the way out, including on an
exception — which is a real advantage of local mode. Over a network the same
guarantee needs the governor, and it is weaker; see
[Local and remote mode](../local-vs-remote.md).

## From an agent

The same workflow through an MCP client, and it is a genuinely good use of one —
the sequencing matters more than the throughput:

> **You:** Power my board at 3.3 V, 200 mA limit, held in reset. Tell me what it
> draws.
>
> **You:** Now release reset and record ten seconds.
>
> **You:** How much of that is the firmware?

Prefer `cp2112_trigger_reset_pulse` over `cp2112_set_line_asserted` in agent
workflows for exactly the reason above: it cannot leave a line held. The
docstring says so, and the docstring is what the model reads.

## When this shape of bringup is not enough

| Situation | What to do instead |
|---|---|
| The board runs on mains, not a bench supply | switch it with the PDU — [Power-cycle testing](power-cycling.md) |
| The reset line is active-high | a relay, not the CP2112 — it is open-drain only |
| You need a sub-millisecond pulse | not this bench; the floor is 5 ms |
| Reset is on I²C or a debug transport | out of scope: the CP2112's I²C engine is deliberately not exposed |
| Several boards, or a hard-cut power rail | the ADU218's eight relays, and its watchdog |
| You want the board to think it is on a battery | [Battery emulation](battery-emulation.md) |
| More than a couple of minutes | [Unattended runs](unattended-runs.md) |

## Next

- [Sleep and duty-cycle current](sleep-current.md) — what the board draws once it is behaving
- [Power consumption characterization](power-characterization.md) — turning that into a battery-life number
- [`drivers.md`](../../drivers.md#silicon-labs-cp2112--open-drain-control-lines) — the control-line driver in full

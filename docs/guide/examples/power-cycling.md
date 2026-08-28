# Power-cycle and cold-boot testing

**The job:** automate the tests that need power to actually go away — cold boot,
brownout recovery, recovering a hung DUT, and the class of bug that only appears
on the hundredth power cycle.

These are the tests everyone agrees are important and nobody runs, because
running them by hand means standing at a bench pulling a plug. They are also
where the most embarrassing field failures live: a device that comes up wrong one
time in fifty, a filesystem that corrupts when power drops mid-write, a radio that
will not re-associate after a cold start.

## Pick the right cut for the job

Four ways to remove power, and they are not interchangeable:

| Method | Cuts | Use when |
|---|---|---|
| `arc disable-output` | the bench supply's rail | the DUT is powered by the SMU. Cleanest, fastest, fully logged |
| CP2112 reset line | nothing — holds the processor | you want a reset, not a power cycle. **Not the same test** |
| ADU218 relay | a DC rail or a signal path (1 A) | multiple rails, a hard break in a lead, or a DUT the SMU does not power |
| PDU outlet | **mains** | the DUT, or its programmer, runs off the wall |

The distinction that matters most is the second row. **A reset is not a power
cycle.** Holding reset leaves RAM contents, retained registers, an RTC and any
capacitor charge intact. Half the bugs in this class are about state that survives
a reset and not a power cut — so if you are testing cold-boot behaviour, cut power.

## The simplest case: the SMU powers the DUT

Nothing extra needed, and it is fully inside one process:

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.set_current_limit(0.2)
    smu.set_voltage(3.3)
    with smu.record(OtiiArcChannel.MAIN_CURRENT, OtiiArcChannel.MAIN_VOLTAGE) as rec:
        for cycle in range(50):
            smu.set_output(True)
            time.sleep(10.0)                 # boot and settle
            i = smu.read_value(OtiiArcChannel.MAIN_CURRENT)
            print(f"cycle {cycle}: {i*1000:.2f} mA")
            smu.set_output(False)
            time.sleep(5.0)                  # let rails actually collapse
    rec.save("power-cycles.opensmu")
```

**That 5 s off-time is the part to get right.** A DUT with a bulk capacitor and a
low sleep current can hold its rail for seconds; cut power for 200 ms and you have
run a brownout test, not a cold-boot test. Measure it: cut power, watch the
voltage channel, and see how long the rail takes to actually reach zero. Then use
more than that.

If the rail will not collapse at all, something else is feeding it — a programmer,
a USB connection, a pull-up to another board.

### Brownout, which is a different test

A brownout is not a power cut; it is a supply that sags and comes back. Devices
fail differently there — a processor that browns out partway can write garbage
where a cleanly-powered-down one writes nothing. Sweep the sag:

```python
for v in (3.3, 3.0, 2.7, 2.4, 2.1, 1.8):
    smu.set_voltage(v)
    time.sleep(2.0)
    print(v, smu.read_value(OtiiArcChannel.MAIN_CURRENT))
smu.set_voltage(3.3)
```

The interesting result is the band between "works" and "off". A device that stops
working at 2.4 V and stops drawing current at 1.8 V has a 600 mV window where it
is powered and wrong. That window is where the corruption happens.

## Cutting mains with a PDU

For a DUT that runs off the wall — or whose programmer, hub or bench instrument
does — the switched PDU is the tool. It is also the most consequential thing on
this bench, and it is gated accordingly.

```bash
export BENCHCTRL_PDU_ALLOW_SWITCHING=1
benchctrl pdu41002 outlet-states
benchctrl --yes pdu41002 set-outlet-state 3 off
benchctrl --yes pdu41002 set-outlet-state 3 on
```

`--yes` is a global flag, so it precedes the device group — a trailing `--yes`
is an argument-parsing error, not an authorisation.

Two gates, not one: `--yes` for the command, and a named environment variable
because the consequence outlives the command. A mains outlet that is off stays off
after your shell exits, so a typo there is not a transient mistake.

The five things to know before using it:

- **`allowed_outlets` comes from the bench's config, not from the command.** It
  describes what this deployment's cabling permits. If the outlet you want is not
  in it, that is a question for whoever wired the rack — not something to widen by
  reopening with a bigger list.
- **There is no way to address more than one outlet at a time.** No `all`, no
  banks. `oltctrl index all act off` is one line that de-powers a whole rack, and
  nothing in the method surface can reach it. A `bool` index is refused before
  `int`, because `True` would silently become outlet 1.
- **The device acknowledges a switch command with nothing at all.** Read-back
  verification is the only evidence a contactor moved, which is why it cannot be
  disabled from the command line. Telling an operator a DUT is de-powered when its
  contactor never moved is worse than failing loudly.
- **The switch is not instant, and the delay is configurable per outlet.** As
  shipped it is 3 s on and off with a 5 s reboot duration, and an operator can
  change it. Read `outlet-config` rather than assuming; a retry budget based on the
  ~0.6 s command round trip will flake.
- **Never plug the bench host or the network gear into the PDU.** That is a cabling
  invariant, not something software enforces: the driver cannot know which outlet
  feeds what, and a bench that can cut power to its own controller cannot recover
  itself.

`reset-outlet` cuts and restores automatically for the outlet's configured reboot
duration. It reports no final state, deliberately — the outlet ends where it
started, so reading it back cannot prove the cut happened. If you need proof,
switch off and on separately and verify each step.

## Cutting a DC rail with a relay

For multiple rails, a hard break in a sense lead, or a DUT the SMU does not power,
the ADU218 gives eight 1 A solid-state relays:

```bash
benchctrl adu218 relay-states
benchctrl --yes adu218 set-relay-state 2 off
benchctrl --yes adu218 set-relay-state 2 on
```

Same shape as the PDU: an allowlist for energising, de-energising always allowed,
read-back verification because the device never acknowledges a write, and one
relay per call — with `set-relay-port` available when you genuinely need several to
transition simultaneously.

Note the asymmetry with the PDU: **de-energising is never gated harder than
energising**, anywhere in this system. Reaching the safe state must not require
more ceremony than leaving it.

## Automating a hundred cycles

Fifty cycles by hand is not going to happen. As a declarative run spec, mains
switching becomes a phase:

```json
{
  "schema": 1,
  "name": "cold-boot-x50",
  "dut": "gateway-board",
  "device": "otii_arc",
  "safety": {
    "max_voltage_V": 3.5, "max_current_A": 0.5,
    "max_duration_s": 7200,
    "allowed_outlets": [3],
    "abort_if": [
      {"ch": "mv", "op": "<", "value": 2.7, "for_s": 5, "reason": "rail collapsed"}
    ]
  },
  "sampling": {"channels": ["mc", "mv"], "chunk_s": 300, "metric_period_s": 10},
  "phases": [
    {"name": "running", "mode": "cv", "duration_s": 600,
     "setpoints": {"voltage_V": 3.0}},
    {"name": "mains-off", "mode": "idle", "duration_s": 30,
     "setpoints": {"outlets": {"3": false}}},
    {"name": "cold-boot", "mode": "cv", "duration_s": 300, "settle_s": 20,
     "setpoints": {"outlets": {"3": true}, "voltage_V": 3.0}}
  ]
}
```

Four properties of that spec worth understanding before you leave one running:

**`safety.allowed_outlets` is empty by default, and empty means none.** It is a
per-experiment allowlist checked *in addition to* the driver's: the driver says
what this deployment may ever touch, the spec says what this run is allowed to. A
phase naming an unlisted outlet is refused at submission — in **both directions**,
because de-powering a DUT is as much a state change as energising one.

**A failed switch fails the phase.** Because the device acknowledges nothing, the
engine verifies and lets the exception end the run. Logging it and carrying on is
the tempting choice, and it produces a full bundle of data describing a power cycle
that never happened.

**Mains is switched before the instrument setpoint.** A setpoint applied while the
DUT is de-powered lands nowhere, and energising mains under an already-live output
is how an inductive kick reaches a DUT. On a governor trip the order reverses.

**`settle_s` defaults to 3 s, not 0** — and for a real DUT you want your boot
time, as the 20 s above. A device coming up on mains draws inrush and then boots,
so the opening samples of a power-cycle phase describe a supply settling rather
than the thing under test. `"settle_s": 0` is a valid, explicit "this one needs
none". The window counts against `max_duration_s`, and an abort arriving during it
is not made to wait it out.

Over a network this needs the writer claim on **both** device keys — the
instrument and the PDU:

```python
client.call("agent.claim", {"device": "otii_arc"})
client.call("agent.claim", {"device": "cyberpower_pdu41002"})
client.call("run.submit", {"spec": spec})
```

Without the second claim, `run.submit` would be the one path to a mains contactor
that skipped the gate every other route enforces. A spec that mentions no outlets
needs no PDU claim, even on a bench that has one.

Every transition lands in the record as a `run_outlet` event carrying the
**verified read-back** state, not the requested one — so the timeline says what the
contactor did. A mains transition missing from the bundle is a hole in the audit
trail of a run that power-cycled a DUT.

## Reading the results

The value of a hundred cycles is in the outliers, so look for the run that differs:

- **Boot current profiles that do not match.** Compare per-cycle inrush peak and
  settling time. One cycle in fifty taking twice as long to settle is a real
  finding, and it is invisible in an average.
- **A cycle that never reached running current.** The device did not come up. This
  is the failure you are hunting.
- **Drift across the sequence.** Current creeping up over fifty cycles suggests
  something accumulating — a leak, thermal, a counter.

Because chunks are `.opensmu`, the same tools read them:

```bash
benchctrl framework recording-summary runs/<run_id>/data/chunk003.opensmu
benchctrl framework plot-recording runs/<run_id>/data/chunk003.opensmu out.png
```

## What a run will not do for you

**A run never cuts mains on its own way out.** Phase ends, aborts and errors leave
outlets exactly where the last phase put them — the next phase almost certainly
wants the DUT up, and an operator may be about to inspect a live one. Cutting on a
lost heartbeat is the governor's separate, opt-in `panic_outlets` decision, and it
must be a subset of the allowlist.

**A run marked `running` under a previous boot id becomes `interrupted`, never
resumed.** After a power cut the DUT's state is unknown, and continuing a phase
mid-flight produces data that looks valid and is not.

**Software cannot guarantee an output goes off.** A driver thread wedged in a
blocking read cannot be reached, and closing a serial port does not command an
output off. For unattended power-cycling at energies that matter, use a hardware
interlock — see [Unattended runs](unattended-runs.md).

## Next

- [Unattended runs](unattended-runs.md) — the hundred cycles, overnight, with an interlock
- [Bringing up a board](board-bringup.md) — reset lines, which are the other tool
- [`runs.md`](../../runs.md#power-cycling-a-dut-mid-run) — the spec surface in full

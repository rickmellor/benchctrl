# Unattended runs

**The job:** hand the bench a complete experiment, disconnect, and come back to a
finished artifact bundle — with an honest account of anything that went wrong
while you were asleep.

Everything else in this guide is something you watch. This is the one you do not,
and that changes the requirements: a laptop that sleeps must not end the
experiment, a network drop must not lose data, and a crash at hour six must leave
the bench in a state that is safe rather than a state that is live.

## A run is data, not code

The thing executing is alone on a bench for hours, possibly driving power into a
DUT, and the host that submitted it may be asleep. So a run is a JSON document,
not a script. That buys three properties that a script cannot have:

- It can be **validated before anything is energised** — a phase that asks for more
  voltage than the envelope allows is refused at submission, not discovered at
  runtime.
- It is **hashed**, so a result always traces to the exact spec that produced it.
- It can be **re-run months later**, by someone else, from the bundle.

## A spec

```json
{
  "schema": 1,
  "name": "sensor-24h-assoc",
  "dut": "room-temp-sensor",
  "device": "otii_arc",

  "safety": {
    "max_voltage_V": 3.5,
    "max_current_A": 0.15,
    "max_duration_s": 90000,
    "max_board_temp_C": 80,
    "abort_if": [
      {"ch": "mv", "op": "<", "value": 2.7, "for_s": 5, "reason": "rail collapsed"},
      {"ch": "mc", "op": ">", "value": 0.12, "for_s": 1, "reason": "overcurrent"}
    ]
  },

  "sampling": {"channels": ["mc", "mv"], "chunk_s": 300, "metric_period_s": 10},

  "phases": [
    {"name": "soak", "mode": "cv",
     "setpoints": {"voltage_V": 3.0, "current_limit_A": 0.1, "range": "low"},
     "duration_s": 86400,
     "exit": [{"ch": "mc", "op": "<", "value": 0.001, "for_s": 60,
               "reason": "DUT slept"}]},
    {"name": "cooldown", "mode": "idle", "duration_s": 300}
  ],

  "rules": [
    {"when": {"ch": "mc", "op": ">", "value": 0.05, "for_s": 2},
     "emit": {"kind": "rule_fired", "severity": "warn", "text": "sustained TX burst"}}
  ]
}
```

Submit it:

```python
run = client.call("run.submit", {"spec": spec})
```

## The safety envelope

Declared once, checked before the first setpoint, and impossible for a later
phase — or the optional LLM — to widen. Inside each tick the ordering is fixed:

1. sample metrics
2. **evaluate the envelope** — an abort here pre-empts everything else
3. evaluate rules, emitting events
4. evaluate phase-exit conditions
5. roll a chunk if one is due

The envelope being checked second, before rules and before phase logic, is the
whole design: nothing a rule or a phase does can get ahead of it.

`max_duration_s` is a backstop on the entire run, independent of what the phases
add up to. Set it. A phase with a condition that never fires is otherwise a run
that never ends.

### Every condition takes a dwell time, and it earns its place

`for_s` is not optional decoration. Measurement noise crosses any threshold
occasionally, and a run that aborted on a single stray sample would be worse than
having no rule at all — you would come back to an aborted run and no data, and you
would learn to raise the threshold until the rule was useless.

Conditions are also **edge-triggered**: a sustained overcurrent emits one event,
not one per tick for six hours.

## Sizing the capture

`chunk_s` bounds two different risks at once. Samples accumulate in memory until a
chunk is written, and a crash loses at most one chunk.

- Five minutes at 4 kHz is a comfortable chunk.
- One hour is fine if the filesystem is local and you have the RAM.
- Over a network, a large chunk is a large transfer at every roll — and remote
  recordings are capped at `max_recording_s` (300 s as shipped, roughly 40 bytes
  per sample held in RAM) precisely because of that.

Long captures belong to the run engine rather than to a `record()` call, because
the engine chunks to disk as it goes.

## The bundle

```
runs/<run_id>/
  spec.json          the exact spec that ran, content-hashed
  run.db             sqlite (WAL): run, phase, event, metric, chunk, artifact
  events.ndjson      fsync'd append-only mirror
  notes.md           narrative, including any LLM commentary
  data/chunkNNN.opensmu
  manifest.json      written at completion
```

`events.ndjson` is deliberately redundant with the event table. WAL survives a
crash; it does not reliably survive an SD card losing power mid-write, which is a
real thing on a board someone unplugs. Events are rare by design, so one fsync
each is cheap insurance for the narrative of what happened.

Chunks are the same `.opensmu` format `Recording.save()` writes, so
`Recording.load()` reads them and the existing analysis tooling already understands
them.

## Following along, and reconnecting

```python
client.call("run.status",      {"run_id": run_id})
client.call("run.events",      {"run_id": run_id, "since_seq": last_seq})
client.call("run.artifacts",   {"run_id": run_id})
client.call("run.fetch_chunk", {"run_id": run_id, "idx": 0})
client.call("run.abort",       {"run_id": run_id, "reason": "..."})
```

Sequence numbers are assigned inside the same transaction that persists the event,
so `since_seq` replays exactly what a disconnected host missed — nothing lost,
nothing duplicated. Close the laptop, open it in the morning, ask for everything
after your last sequence number.

## The safety story, honestly

This is the section to read before leaving something running at energies that
matter. Four layers, in the order they act, and each one's limit.

**1. The driver refuses.** Allowlists rather than denylists, no aggregate
targeting, open-drain only where that applies, read-back verification where the
device acknowledges nothing. *Limit:* it only governs calls that reach it.

**2. One writer per device.** The claim is taken at open, so a second session
loses before it half-configures anything. *Limit:* nothing retries and nothing
queues — the loser fails.

**3. The governor, on a remote bench.** It tracks armed state from the calls it
sees and escalates when frames stop arriving: a priority safe-state command, then
a transport reset and retry, then critical events and a refusal to accept new
work. *Limit:* it cannot reach a driver thread wedged in a blocking read.

**4. The service stop path.** The systemd unit ships with:

```ini
ExecStopPost=... --safe-stop
TimeoutStopSec=60
```

Without those, a `systemctl restart` leaves the DUT rail live across the gap
between the old process dying and the new one binding. The 60 s timeout is there
because disarming talks to real instruments over serial and a SIGKILL halfway
through is exactly the case the flag exists to prevent. If you write your own
unit, carry both lines.

Verify that path specifically, because it only runs on stop:

```bash
sudo systemctl stop benchctrl-agent
journalctl -u benchctrl-agent -n 10 --no-pager   # want: "safe-stop: otii_arc disarmed"
```

### Software cannot guarantee an output goes off

All four layers above are damage limitation. Closing a serial port does not command
an output off — a source-measure unit holds its last commanded state — and a wedged
thread is unreachable by anything in the process that is wedged.

**Only a hardware interlock is real.** For unattended work at energies that matter,
add one:

- a relay on the DUT rail, de-energised by default
- the instrument's own hardware GPO, if it fails to a safe state
- the ADU218's **firmware watchdog**, which is the only true interlock in the
  instrument list

The watchdog's property is that no software is in the decision path. While armed,
the device de-energises all eight relays by itself if it receives no command within
the interval — and a wedged process, a killed agent, an unplugged cable and a
panicking kernel all look identical to the device and all drop the load.

```bash
export BENCHCTRL_ADU218_ARM_WATCHDOG=1
benchctrl --yes adu218 set-watchdog 3      # 3 = one minute; --yes is global
```

Three consequences you own the moment you arm it:

1. **Every relay's state now depends on how often you call.** A single slow call to
   another instrument can exceed the interval and drop a relay you were told to
   hold. The 1-second setting is unusable for general work; one minute is the
   longest available. The trip point was bisected on this bench to
   **(0.90, 1.10] s** for the 1-second setting.
2. **Any command refeeds the timer** — including an invalid one, and including a
   plain state read. So a status-polling loop silently neuters the watchdog. The
   feed must come from whatever is actually controlling the test.
3. **There is deliberately no keep-alive helper.** A background feeder would keep
   the watchdog fed precisely while the failure it guards against was happening.

Do not arm it speculatively. Arm it when a test needs the interlock, and disarm it
when that test ends.

## After a restart

A run still marked `running` under a previous boot id is marked `interrupted`. It
is **never silently resumed**: after a power cut the DUT's state is unknown, and
continuing a phase mid-flight would produce data that looks valid and is not.

Chunk numbering continues from the highest index written, so a resumed run does not
overwrite what it already captured.

## The optional commentary layer

A run can have a small on-board model annotate itself. Optional, advisory, and
strictly off the control path.

The constraint that shaped it: the on-board model runs at roughly **3.3
tokens/second**, so a turn takes about two minutes. That single fact means it
cannot be in a control loop, cannot react to an event promptly, and cannot see raw
samples. What it can do is read aggregates the engine already computed and write a
sentence about them.

Eight tools, allowlisted in code:

| Tool | Effect |
|---|---|
| `run_status`, `phase_summary`, `recent_events`, `metric_window` | read-only |
| `annotate`, `raise_alert` | additive — notes and flags |
| `advance_phase` | forward-only, within the declared phase list |
| `abort_run` | monotone toward the safe state |

There is no `set_voltage`, no `set_output`, no driver method of any kind. It cannot
energise anything, widen the envelope, repeat a phase or extend a run. Three policy
violations in one phase disable it for the remainder.

If it is unreachable, stalled or talking nonsense, the run is unaffected — it
simply has fewer annotations. A test asserts a three-second run finishes on time
against a thirty-second model stall.

**The deterministic rules are the safety system. The model is commentary.** That is
the right way to think about an LLM anywhere near a bench.

## A checklist before you walk away

- [ ] `max_duration_s` set, and shorter than you think you need
- [ ] `abort_if` covers rail collapse **and** overcurrent, both with a `for_s`
- [ ] every condition's dwell time is longer than your measurement noise
- [ ] `chunk_s` sized so a crash costs an acceptable amount of data
- [ ] `settle_s` set to the DUT's real boot time on any power-cycle phase
- [ ] the writer claim held on **every** device the spec touches, including the PDU
- [ ] the service's `--safe-stop` path verified in the journal, not assumed
- [ ] a hardware interlock if the energies matter — nothing in software substitutes
- [ ] a dry run against simulators first: `benchctrl-agent --simulate`
- [ ] the journal checked for a trailing `(SIMULATED)`, so you know which bench you
      just handed a 24-hour experiment to

That last one is not a joke. A simulated bench answers every call successfully,
which is exactly what a working bench does.

## Next

- [Setting up a bench host](../bench-host-setup.md) — the service, the udev rules, the stop path
- [Local and remote mode](../local-vs-remote.md) — what changes over a network
- [Power consumption characterization](power-characterization.md) — the 24-hour campaign this was built for
- [`runs.md`](../../runs.md) — the full spec reference

# Unattended runs

Hand the bench a declarative experiment, disconnect, and come back to a
complete artifact bundle.

A run is data, not code. That is deliberate: the thing executing it is alone
on a bench for hours, possibly driving power into a DUT, and the host that
submitted it may be asleep. A JSON document can be validated before anything
is energised, hashed so a result always traces to the spec that produced it,
and re-run months later.

## A spec

```json
{
  "schema": 1,
  "name": "cr2032-assoc-24h",
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

  "sampling": {
    "channels": ["mc", "mv"],
    "chunk_s": 300,
    "metric_period_s": 10
  },

  "phases": [
    {
      "name": "soak",
      "mode": "cv",
      "setpoints": {"voltage_V": 3.0, "current_limit_A": 0.1, "range": "low"},
      "duration_s": 3600,
      "exit": [{"ch": "mc", "op": "<", "value": 0.001, "for_s": 60,
                "reason": "DUT slept"}]
    },
    {
      "name": "cooldown",
      "mode": "idle",
      "duration_s": 300
    }
  ],

  "rules": [
    {"when": {"ch": "mc", "op": ">", "value": 0.05, "for_s": 2},
     "emit": {"kind": "rule_fired", "severity": "warn",
              "text": "sustained TX burst"}}
  ],

  "llm": {
    "enabled": true,
    "min_interval_s": 60,
    "call_on": ["phase_end", "severity"]
  }
}
```

Submit it:

```python
run = client.call("run.submit", {"spec": spec})
```

## The safety envelope

Declared once, checked before the first setpoint, and impossible for a later
phase — or the LLM — to widen. A spec whose phase asks for more voltage than
the envelope allows is rejected at submission, not discovered at runtime.

Inside each tick the ordering is fixed:

1. sample metrics
2. **evaluate the envelope** — an abort here pre-empts everything else
3. evaluate rules, emitting events
4. evaluate phase-exit conditions
5. roll a chunk if one is due

`max_duration_s` is a backstop on the whole run, independent of what the
phases add up to.

## Dwell times

Every condition takes `for_s`, and it earns its place. Measurement noise
crosses any threshold occasionally; a run that aborted on a single stray
sample would be worse than having no rule. Conditions are also
edge-triggered — a sustained overcurrent emits one event, not one per tick
for an hour.

## Modes

| Mode | Behaviour |
|---|---|
| `idle` | output off; still samples and evaluates rules |
| `cv` | constant voltage from `setpoints.voltage_V` |
| `cc` | constant current from `setpoints.current_A` |
| `emulator` | battery emulation from `emulator.profile` |

`emulator` phases never record. The emulator's 100 Hz control loop and the
recording reader thread contend for the same transport and deadlock within
about 100 ms (`KNOWN_LIMITATIONS` A-1). Moving both to the bench moves the
deadlock with them, so the engine forbids the combination structurally
rather than discovering it at 3 a.m.

## Power-cycling a DUT mid-run

A phase can switch mains outlets on a [PDU41002](drivers.md#switching), which
is what makes cold-boot, brownout-recovery and hung-DUT tests expressible as a
spec rather than as someone standing at the bench.

```json
{
  "safety": { "allowed_outlets": [3] },
  "phases": [
    {"name": "running",  "mode": "cv",   "duration_s": 600,
     "setpoints": {"voltage_V": 3.0}},
    {"name": "mains-off", "mode": "idle", "duration_s": 30,
     "setpoints": {"outlets": {"3": false}}},
    {"name": "cold-boot", "mode": "cv",   "duration_s": 300, "settle_s": 20,
     "setpoints": {"outlets": {"3": true}, "voltage_V": 3.0}}
  ]
}
```

**`safety.allowed_outlets` is empty by default, and empty means none.** It is
an allowlist for this experiment, checked *in addition to* the driver's own
`allowed_outlets`: the driver says what this deployment may ever touch, the
spec says what this run is allowed to. A phase naming an unlisted outlet is
refused at submission — in **both directions**, because de-powering a DUT is as
much a state change as energising one.

Aggregates are inexpressible. There is no spelling of `all`, `b1` or `b2`, and
a `bool` index is refused before `int` (`True` would silently become outlet 1).

Four properties worth knowing before you leave one running overnight:

- **A failed switch fails the phase.** `oltctrl` acknowledges nothing, so the
  engine goes through `set_outlet_state(..., verify=True)` and lets the
  exception end the run. Logging it and carrying on is the tempting choice and
  it produces a bundle full of data describing a power cycle that never
  happened.
- **Mains is switched before the instrument setpoint.** A setpoint applied
  while the DUT is de-powered lands nowhere, and energising mains under an
  already-live output is how an inductive kick reaches a DUT. On a governor
  trip the order is the reverse — see
  [remote.md](remote.md#the-pdu-is-exempt-from-the-safe-state-and-cuts-mains-only-on-request).
- **`settle_s` defaults to 3 s, not 0.** A DUT coming up on mains draws inrush
  and then boots, so the opening samples of a power-cycle phase describe a
  supply settling rather than the thing under test. Set it to your DUT's boot
  time; `"settle_s": 0` is a valid, explicit "this one needs none". The window
  counts against `max_duration_s`, and an abort arriving during it is not made
  to wait it out.
- **A run never cuts mains on its own way out.** Phase ends, aborts and errors
  leave outlets exactly where the last phase put them: the next phase almost
  certainly wants the DUT up, and the operator may be about to inspect a live
  one. Cutting on a lost heartbeat is the governor's separate, opt-in
  `panic_outlets` decision.

Over the wire this needs the writer claim on **both** device keys — the
instrument and the PDU:

```python
client.call("agent.claim", {"device": "otii_arc"})
client.call("agent.claim", {"device": "cyberpower_pdu41002"})
client.call("run.submit", {"spec": spec})
```

Without the second claim `run.submit` would be the one path to a mains
contactor that skipped the gate every other route enforces. The refusal names
the key to claim. An ordinary spec that mentions no outlets needs no PDU claim
even on a bench that has one.

Every transition is in the run record as a `run_outlet` event carrying the
**verified read-back** state, not the requested one — so the timeline says what
the contactor did. A mains transition missing from the bundle is a hole in the
audit trail of a run that power-cycled a DUT.

## Following a run

```python
client.call("run.status", {"run_id": run_id})
client.call("run.events", {"run_id": run_id, "since_seq": last_seq})
client.call("run.artifacts", {"run_id": run_id})
client.call("run.fetch_chunk", {"run_id": run_id, "idx": 0})
client.call("run.abort", {"run_id": run_id, "reason": "..."})
```

Sequence numbers are assigned inside the same transaction that persists the
event, so `since_seq` replays exactly what a disconnected host missed —
nothing lost, nothing duplicated.

## The bundle

```
runs/<run_id>/
  spec.json          the exact spec that ran, content-hashed
  run.db             sqlite (WAL): run, phase, event, metric, chunk, artifact
  events.ndjson      fsync'd append-only mirror
  notes.md           narrative, including LLM commentary
  data/chunkNNN.opensmu
  manifest.json      written at completion
```

`events.ndjson` is deliberately redundant with the `event` table. WAL
survives a crash; it does not reliably survive an SD card losing power
mid-write, which is a real thing on a board someone unplugs. Events are rare
by design, so one fsync each is cheap insurance for the narrative of what
happened.

Chunks are `.opensmu` — the same format `Recording.save()` writes, so
`Recording.load()` reads them and `applications/sensor_profiler/analyze.py`
already understands them.

## After a restart

A run still marked `running` under a previous boot id is marked
`interrupted`. It is never silently resumed: after a power cut the DUT's
state is unknown, and continuing a phase mid-flight would produce data that
looks valid and is not. Chunk numbering continues from the highest index
written, so a resumed run does not overwrite what it already captured.

## The LLM layer

Optional, advisory, and strictly off the control path.

The on-board model runs at roughly 3.3 tokens/second. A turn — 400 prompt
tokens plus 120 output tokens — takes about two minutes. That single fact
determines the entire design: it cannot be in a control loop, cannot react
to an event promptly, and cannot see raw samples. What it can do is read
aggregates the engine already computed and write a sentence about them.

Eight tools, allowlisted in code:

| Tool | Effect |
|---|---|
| `run_status`, `phase_summary`, `recent_events`, `metric_window` | read-only |
| `annotate`, `raise_alert` | additive — notes and flags |
| `advance_phase` | forward-only, within the declared phase list |
| `abort_run` | monotone toward the safe state |

There is no `set_voltage`, no `set_output`, no driver method of any kind.
The model cannot energise anything, widen the envelope, repeat a phase, or
extend a run. Three policy violations in one phase disable it for the
remainder.

If the model is unreachable, stalled, or talking nonsense, the run is
unaffected — it simply has fewer annotations. A test asserts a three-second
run finishes on time against a thirty-second model stall.

**The deterministic rules are the safety system. The model is commentary.**

### Pointing it at a backend

The agent speaks the OpenAI-compatible chat API over plain `urllib`, so
anything that serves that interface on localhost works. On the Uno Q that
means ollama running natively:

```bash
OLLAMA_MODELS=/home/arduino/ollama/models ollama serve
```

Give the unit `Nice=10` and `CPUQuota=200%` so llama.cpp cannot starve the
agent's I/O threads on a four-core board, and log board temperature as a
metric — sustained inference throttles, and throttling shows up as
control-loop jitter.

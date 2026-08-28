# Driving it from an AI agent

benchctrl exposes its whole surface — all 324 functions — as [Model Context
Protocol](https://modelcontextprotocol.io) tools. Any MCP client can then run
real measurements:

> *"Set 3.3 V with a 200 mA limit, record for ten seconds, and tell me the mean
> and peak current."*

This is not a chat wrapper around a script. The agent calls the same functions
your Python would, through the same `session.resolve()` seam, so it works
against local instruments, a bench host, or simulators without knowing which.

## Install and run

```bash
pip install "benchctrl[mcp]"
benchctrl-mcp                      # speaks MCP over stdio
```

Add the bench-instrument extras you have:

```bash
pip install "benchctrl[mcp,bench-visa]"          # + both Rigols and the DMM
pip install "benchctrl[mcp,bench-visa,science]"  # + plotting and Parquet tools
```

A driver whose extras are missing simply contributes no tools, rather than
breaking the server.

## Wire it into Claude Code

```json
{
  "mcpServers": {
    "benchctrl": {
      "command": "benchctrl-mcp"
    }
  }
}
```

Restart, then check with `/mcp` — `benchctrl` should be listed under connected
servers.

For a bench in the lab, put the binding in the server's environment:

```json
{
  "mcpServers": {
    "benchctrl": {
      "command": "benchctrl-mcp",
      "env": {
        "BENCHCTRL_REMOTE": "bench.local:9737",
        "BENCHCTRL_TOKEN": "..."
      }
    }
  }
}
```

## Wire it into Claude Desktop

Same block, in `claude_desktop_config.json` (the path varies by OS — see the
[MCP quickstart](https://modelcontextprotocol.io/quickstart/user)), then restart
the app.

## Try it with no hardware

Worth doing first. The agent cannot tell the difference, so this is a real
rehearsal of the conversation you are about to have with a live bench:

```json
{
  "mcpServers": {
    "benchctrl": {
      "command": "benchctrl-mcp",
      "env": {"BENCHCTRL_SIM_DEVICES": "otii_arc,ontrak_adu218,silabs_cp2112"}
    }
  }
}
```

The server prints each non-local binding to stderr, so you can confirm what it
bound.

## What the agent gets

| Prefix | Instrument | Tools |
|---|---|---|
| *(unprefixed)* | Otii Arc / Arc Pro source-measure unit | 23 |
| `qr10x_*` | Eastwood QR10x resistance standard | 11 |
| `dl3031a_*` | Rigol DL3031A electronic load | 45 |
| `dp2031_*` | Rigol DP2031 triple supply | 134 |
| `sdm4065a_*` | Siglent SDM4065A multimeter | 54 |
| `pdu41002_*` | CyberPower PDU41002 switched PDU | 15 |
| `adu218_*` | Ontrak ADU218 relays and inputs | 19 |
| `cp2112_*` | SiLabs CP2112 control lines | 10 |
| *(cross-driver)* | battery analytics, recording I/O, connection | 13 |

Every public SDK method has a matching tool — including the battery emulator,
the life calculator and the recording loader. That parity is enforced by tests
rather than maintained by hand, because the failure mode is a capability that
works in Python and is invisible to an agent.

The tool docstring is the same text a model sees and the same text
`benchctrl <group> <command> --help` prints, so what you read is what it reads.

## The safety model

A model is confident and fast, which is a different risk profile from a reviewed
script. Three mechanisms narrow it.

### Energising takes three independent facts

On the source-measure unit, `enable_output` refuses unless **all three** hold:

1. `set_current_limit(amps)` has been called — bounds DUT damage in a fault
2. `set_voltage(volts)` has been called — so something knows what is about to be
   driven
3. the caller passes `confirm_dut_attached=True`

Each is a different claim, and none can be inferred from the others. A failed
guard returns `{"error": ..., "guidance": ...}`, where `guidance` tells the model
exactly what is missing — so it recovers by satisfying the guard rather than by
guessing around it. The companion drivers follow the same pattern.

### Some arguments are not exposed at all

Withheld deliberately, and the reason is always that the argument is a **bench
observation a caller cannot establish**:

- **`verify`** — defaults `True` on both switching drivers, and is load-bearing.
  The ADU218 reports no error, ever, so `verify=False` returns the value you
  *commanded* rather than the one the device is at. Verification is not a
  preference.
- **`allowed_relays` / `allowed_outlets` / `panic_outlets`** — safety allowlists.
  An argument would let a caller *widen* scope per call, inverting their purpose.
  They come from config, which describes this bench's cabling.
- **`allow_alternate_function`** (CP2112) — an operator attestation about what a
  pin is wired to. A model has no way to know.
- **`pdu41002_open` takes no password parameter.** It is absent rather than
  defaulted, because a credential passed as a tool argument is written into the
  conversation transcript. It comes from `BENCHCTRL_PDU_PASSWORD` in the
  server's environment.

### Mains switching is read-only through this surface

In this release every `pdu41002_*` tool is **read-only**, and
`pdu41002_allowed_outlets()` reports `switching_available: false` — so a model
cannot infer from the presence of an allowlist that it has a way to use it.

The ADU218 set is deliberately **not** read-only: `adu218_set_relay_state`
really does close a contact, and `allowed_relays` defaults to *all eight*. The
asymmetry is the device, not an inconsistency — a mains typo de-powers the bench,
whereas these are 1 A signal relays on instrument leads that the bench wants to
toggle freely. Pass `allowed_relays` when they are wired to something you care
about.

## Two docstrings to read before pointing a model at a live bench

**`cp2112_set_line_asserted` can hold a DUT in reset indefinitely.** Its
docstring names the release call explicitly rather than assuming the caller
infers it. `cp2112_trigger_reset_pulse` is the one that *cannot* leave a line
held — it releases in a `finally` — so prefer it in agent workflows.

**`sdm4065a_measure_*` reconfigures the meter before it triggers**, which
discards a null. After nulling, `sdm4065a_read` and `sdm4065a_read_nulled` are
the tools to use. This is the class of trap that matters most with a meter: it
returns a plausible wrong number rather than an error.

## When *not* to use the MCP surface

The tools are one round trip each, driven by a model that thinks between calls.
That is right for interactive work and wrong for anything with tight timing or
lots of data.

| Task | Use |
|---|---|
| "Set 3.3 V, record 5 s, what was the peak?" | **MCP** |
| "What is the rail reading right now?" | **MCP** |
| A voltage sweep with a decision at each step | **MCP** for the sweep, Python for the analysis |
| FFT, plotting, anomaly detection | **Python** |
| Every `.opensmu` file in a folder | **Python** |
| CI, or a scheduled measurement | **Python** |
| A control loop | **Python, on the bench** |
| Anything overnight | **the run engine** — see [Unattended runs](examples/unattended-runs.md) |

Two hard edges:

- **The battery profiler takes hours.** Most MCP clients time out long before it
  finishes. `battery_profiler_estimate_duration` exists so you know what you are
  committing to; for anything past a demo, use the Python `Profiler`.
- **The battery emulator's 100 Hz loop does not survive a network.** Locally
  through MCP it is fine. Remote, it is not viable — run it on the bench.

A useful pattern: let the agent drive the bench interactively while it *writes*
the Python for the long version. The 324 tools and the SDK are the same
functions, so what it proves by hand transfers directly into the script.

## The run engine's model is a different, much smaller surface

Do not confuse the two. This MCP server is your agent's full 324-tool surface.
The **run engine's on-bench supervisor** is eight allowlisted tools, and the
constraint that shaped it is that a small on-board model runs at roughly
3.3 tokens/second — about two minutes per turn. That single fact means it cannot
be in a control loop, cannot react promptly, and cannot see raw samples. What it
can do is read aggregates the engine already computed and write a sentence.

| Tool | Effect |
|---|---|
| `run_status`, `phase_summary`, `recent_events`, `metric_window` | read-only |
| `annotate`, `raise_alert` | additive — notes and flags |
| `advance_phase` | forward-only, within the declared phase list |
| `abort_run` | monotone toward the safe state |

There is no `set_voltage`, no `set_output`, no driver method of any kind. It
cannot energise anything, widen the declared envelope, repeat a phase or extend
a run. Three policy violations in one phase disable it for the rest of the run.
If it is unreachable, stalled or talking nonsense, the run is unaffected — it
just has fewer annotations, and a test asserts a three-second run finishes on
time against a thirty-second model stall.

**The deterministic rules are the safety system. The model is commentary.** That
is the right way to think about an LLM anywhere near a bench.

## A worked conversation

What a first session on a real bench actually looks like:

> **You:** What instruments can you see?
>
> **Agent:** *calls `info`, `sdm4065a_info`, `adu218_info`,
> `adu218_relay_states`* — an Arc Pro on COM6, an SDM4065A, and an ADU218 with
> all eight relays open.
>
> **You:** Power my board at 3.3 V with a 200 mA limit and tell me what it draws
> at idle.
>
> **Agent:** *calls `set_current_limit(0.2)`, `set_voltage(3.3)`,
> `enable_output(confirm_dut_attached=True)`, `record(...)`* — `record` returns
> the per-channel statistics itself, so there is no second call to reduce them —
> mean 41.2 mA over 10 s, peak 118 mA in the first 30 ms.
>
> **You:** Hold it in reset and measure again.
>
> **Agent:** *calls `cp2112_set_line_asserted(3, True)`, records, releases* —
> 2.1 mA held in reset, so about 39 mA is the running firmware.

That last comparison is the measurement, and it took two tool calls rather than
an afternoon. It is also exactly the shape of
[Bringing up a board](examples/board-bringup.md).

## Next

- [Bringing up a board](examples/board-bringup.md) — the same workflow, in Python
- [Local and remote mode](local-vs-remote.md) — pointing the server at a lab bench
- [`mcp.md`](../mcp.md) — the full tool tables

# benchctrl MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes your whole bench as tools any MCP-aware client (Claude Code,
Claude Desktop, Cursor, custom agents) can call. Built on the official
`mcp` Python SDK.

**313 tools**, registered per driver:

| Source | Tools |
|---|---|
| Otii Arc / Arc Pro | 23 |
| Eastwood QR10x | 11 |
| Rigol DL3031A | 45 |
| Rigol DP2031 | 134 |
| Siglent SDM4065A | 54 |
| CyberPower PDU41002 | 15 |
| Silicon Labs CP2112 | 10 |
| Ontrak ADU218 | 19 |
| Cross-driver (battery, recording I/O, connection) | 13 |
| **Total** | **324** |

Each driver registers its own surface via `register_mcp_tools(mcp)`;
`benchctrl.mcp` is the orchestrator that wires them together. A driver
whose extras aren't installed simply contributes no tools.

## Install

```bash
pip install "benchctrl[mcp]"                  # server + Arc + QR10x
pip install "benchctrl[mcp,bench-visa]"       # + both Rigols and the SDM4065A
pip install "benchctrl[mcp,bench-visa,science]"  # + plot/parquet tools
```

For development:

```bash
pip install -e ".[mcp,dev,bench-visa,science]"
```

## Run

The server speaks MCP over stdio (the standard transport for editor /
desktop integrations):

```bash
benchctrl-mcp
```

Or equivalently:

```bash
python -m benchctrl.mcp
```

The server holds each device connection for its lifetime — only one
process can hold a given device at a time. Closing the server (or
calling the `disconnect` tool) releases it.

### Remote and simulated benches

The tools are identical whether the instruments are local, on another
machine, or simulated. `benchctrl.session` resolves that **per
device**, so you can mix modes freely:

```bash
# every device on a bench machine
BENCHCTRL_REMOTE=bench.local:9737 BENCHCTRL_TOKEN=... benchctrl-mcp

# Rigols remote, Arc on this laptop
BENCHCTRL_REMOTE=bench.local:9737 BENCHCTRL_LOCAL_DEVICES=otii_arc benchctrl-mcp

# no hardware at all
BENCHCTRL_SIM_DEVICES=otii_arc,eastwood_qr10x,rigol_dl3031a,rigol_dp2031,siglent_sdm4065a,cyberpower_pdu41002,silabs_cp2112,ontrak_adu218 benchctrl-mcp
```

With nothing configured everything is local. See
[`remote.md`](remote.md) and [`simulation.md`](simulation.md).

## Wire into Claude Code

Add to your Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "benchctrl": {
      "command": "benchctrl-mcp"
    }
  }
}
```

Claude will pick it up the next time it starts. Verify with `/mcp` in
the Claude Code interface — `benchctrl` should appear under connected
servers.

## Wire into Claude Desktop

Edit `claude_desktop_config.json` (path varies by OS — see the [Claude
Desktop docs](https://modelcontextprotocol.io/quickstart/user)) and add:

```json
{
  "mcpServers": {
    "benchctrl": {
      "command": "benchctrl-mcp"
    }
  }
}
```

Restart Claude Desktop.

## Safety model

Tools that energise something require explicit confirmation. On the
Arc, `enable_output` refuses unless **all three** of these are true:

1. `set_current_limit(amps)` has been called (bounds DUT damage in faults)
2. `set_voltage(volts)` has been called (so we know what's about to be driven)
3. The caller passes `confirm_dut_attached=True`

If any guard fails, the tool returns a structured `{"error": ..., "guidance": ...}`
response. The `guidance` field tells the LLM exactly what's needed.

The same pattern applies to the companion drivers — `dl3031a_set_input`
and the DP2031 output tools take confirmation arguments.

Every other tool is non-destructive on a setup with nothing connected to
the output terminals.

Two boundaries worth knowing if you are pointing a model at a real
bench:

- **Remote mode does not change the tool surface, but it does change
  the failure modes.** The agent runs a safety governor that drives
  outputs off when contact with the host is lost. See
  [`remote.md`](remote.md).
- **The run engine's LLM supervisor is a separate, much narrower
  surface** — eight allowlisted tools, none of which can energise
  anything or widen a declared safety envelope. It is not this server.
  See [`runs.md`](runs.md).

## Tool surface

The tables below cover the Arc and the cross-driver tools. The
companion drivers follow a prefix convention and map one-to-one onto
their SDK methods, which are documented in [`drivers.md`](drivers.md):

| Prefix | Driver | Count |
|---|---|---|
| `qr10x_*` | Eastwood QR10x | 11 |
| `dl3031a_*` | Rigol DL3031A | 45 |
| `dp2031_*` | Rigol DP2031 | 134 |
| `sdm4065a_*` | Siglent SDM4065A | 54 |
| `pdu41002_*` | CyberPower PDU41002 | 15 |
| `adu218_*` | Ontrak ADU218 | 19 |

The DP2031 set is the large one, covering source/measure, protection,
IEEE 488.2 status, channel pairing and tracking, the Arb timer
sequencer, the IoT power analyzer, trigger I/O, and the device
filesystem.

The SDM4065A set has no confirmation-argument tools: a meter sources
nothing, so there is nothing to arm. Its risk is a plausible wrong
number instead, so the tools that affect accuracy — `sdm4065a_set_range`,
`sdm4065a_set_nplc`, `sdm4065a_null_now` — carry the traps in their
docstrings. In particular `sdm4065a_measure_*` reconfigures before it
triggers and therefore discards a null; `sdm4065a_read` and
`sdm4065a_read_nulled` are the ones to use after nulling.

The `pdu41002_*` set is the one to read the docstrings of before
calling. It is the only driver whose device switches mains, and in this
release every one of its tools is **read-only** — `pdu41002_open`
reports `switching_available: false` through
`pdu41002_allowed_outlets()` so a model cannot infer from an allowlist
that it has a way to use it. `pdu41002_open` also takes **no password
parameter**: it is absent rather than defaulted, because a credential
passed as a tool argument would be logged in the conversation
transcript. The password comes from `BENCHCTRL_PDU_PASSWORD` in the
server's environment.

Two device behaviours worth knowing before a model calls them:
`pdu41002_open` fails with a *session* error, not an auth error, when
another transport holds the device's single CLI session — and it fails
that way *after* the password is accepted; and `pdu41002_close` must be
called, because closing the connection without it leaves the PDU
unreachable from the other transport. See
[`drivers.md`](drivers.md#cyberpower-pdu41002--8-outlet-switched-pdu).

The `adu218_*` set switches relays, and unlike the PDU set it is **not**
read-only — `adu218_set_relay_state` really does close a contact. Three
things about it are unusual enough to be worth reading first:

- **`adu218_open`'s `allowed_relays` is optional, and omitting it allows
  every relay** — the opposite of `pdu41002_open`, where the allowlist is
  mandatory. The difference is the device: a mains typo de-powers the
  bench, whereas these are 1 A signal SSRs on instrument leads that the
  bench wants to toggle freely. Pass it when the relays are wired to
  something that must not switch. Listed or not, *de-energising is always
  permitted*: an unlisted relay is refused by
  `adu218_set_relay_state(..., on=True)` and accepted by the same call
  with `on=False`, because an allowlist must never make the safe state
  unreachable.
- **The device never reports an error.** An unknown command, a valid
  command with a bad argument, and a write-only command are
  byte-identical on the wire — all silence. So every write is confirmed
  by reading the state back, and `adu218_set_relay_state` returns the
  *read-back* value rather than the value it was asked for. If a tool
  answers, the device answered.
- **`adu218_set_watchdog` changes what silence means,** and **any**
  command refeeds the timer — so a model that polls state in a loop
  silently prevents the watchdog from ever tripping. Arm it for a test
  that needs the interlock and disarm it when that test ends.
  `adu218_watchdog` returning 0 is ambiguous: it means either
  "timed out" or "never enabled", and the device cannot tell them apart.

See [`drivers.md`](drivers.md#ontrak-adu218--8-relays-8-digital-inputs-no-dependencies).

Four of its tools exist because of documented firmware defects rather
than because the SCPI surface needed them: `sdm4065a_command_error`
(`*ESR?` bit 5, the reliable rejection check when the error queue is
not), `sdm4065a_drain_errors` (`*CLS` does not empty the queue),
`sdm4065a_standard_event_status`, and `sdm4065a_clear_device_buffers`
(USB-TMC `INITIATE_CLEAR`, for a wedged endpoint pair). The defects are
written up in [`vendor-issues/`](vendor-issues/SDM4065A-firmware-bug-reports-README.md).

### Information

| Tool | Returns |
|---|---|
| `info()` | Port, device name, firmware/hardware version, serial id |
| `state()` | Every cached setpoint + connection state (no wire traffic) |
| `versions()` | Reads name/hw/fw/serial from the device |
| `list_channels()` | All 14 channels with codes, rates, units, labels |

### Setpoints (do not enable output)

| Tool | Args |
|---|---|
| `set_voltage(volts)` | float V, 0.0-5.5 |
| `set_current_limit(amps)` | float A, 0.001-5.0 |
| `set_exp_voltage(volts)` | float V, 1.2-5.0 |
| `set_exp_5v(enabled)` | bool |
| `set_range(range_)` | `"low"` or `"high"` |
| `set_4wire(enabled)` | bool |
| `set_current_limit_enabled(enabled)` | bool (True=CC mode, False=cut-off) |
| `set_uart(enabled, baudrate=None)` | bool, optional int |
| `set_gpo(pin, state_on)` | int 1/2/3, bool. Pin 3 = TX pin |
| `set_power_regulation(mode)` | `"voltage"` / `"current"` / `"inline"` / `"off"` |

### Output control

| Tool | Args |
|---|---|
| `enable_output(confirm_dut_attached)` | bool — must be True, plus enforced guards |
| `disable_output()` | — |

### Measurement / capture

| Tool | Args | Returns |
|---|---|---|
| `live(channel="mv", timeout_s=1.5)` | str, float | one sample value |
| `take_snapshot(duration_s=0.5)` | float | latest value per channel in window |
| `record(seconds, channels=None, save_path=None, name="recording", plot_png=None)` | float, list[str], path, str, path | per-channel stats + optional file + optional PNG |

`record`'s `save_path` extension is auto-detected: `.csv`, `.json`,
`.opensmu`, or `.parquet` (the last requires `benchctrl[parquet]`).
`plot_png` requires `benchctrl[plot]`.

### Recording I/O (no SMU connection required)

| Tool | Args | What it does |
|---|---|---|
| `recording_summary(input_path)` | path | Load a saved `.opensmu` file and return its metadata + per-channel statistics |
| `plot_recording(input_path, output_png, channels=None, title=None)` | paths, optional list/str | Render a matplotlib quick-look PNG from a saved recording (one subplot per channel) |
| `export_recording(input_path, output_path)` | paths | Convert a saved `.opensmu` to `.csv` / `.json` / `.parquet` / `.opensmu` based on output extension |

### Communication / GPIO

| Tool | Args |
|---|---|
| `write_uart_tx(text)` | str |
| `get_gpi(pin)` | int 1/2 |

### Connection

| Tool | Args |
|---|---|
| `reconnect()` | — |
| `disconnect()` | — (releases the device for another client) |

## Example interactions

> "Connect to the SMU and tell me the firmware version."

Claude calls `info()` → `{"name": "Arc", "fw_version": "3.1.3", ...}`.

> "Set 3.3 V with a 1 A limit, record main current for 5 seconds, give
> me the peak current."

Claude calls in order:

1. `set_voltage(3.3)`
2. `set_current_limit(1.0)`
3. (asks user to confirm DUT)
4. `enable_output(confirm_dut_attached=True)`
5. `record(5.0, ["mc"])`
6. `disable_output()`
7. Returns stats including `max` from `record`'s response.

> "What's the voltage on Sense+ right now?"

Claude calls `live("sp")`.

> "Snapshot everything."

Claude calls `take_snapshot(0.5)` → returns latest value per channel.

## Install the Claude Code skill

Complementary to the MCP server: a skill that guides Claude when **writing
benchctrl Python code** for tasks the MCP tools don't cover (custom analysis,
batch processing, plotting, transient detection, etc.).

The skill lives in this repo at [`skills/benchctrl/SKILL.md`](../skills/benchctrl/SKILL.md).
Install it into your Claude config one of two ways.

**Symlink** (recommended — skill stays in sync with repo updates):

```powershell
# PowerShell:
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
New-Item -ItemType SymbolicLink `
    -Path "$env:USERPROFILE\.claude\skills\benchctrl" `
    -Target "$(Resolve-Path .\skills\benchctrl)"
```

```bash
# bash / zsh:
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/benchctrl" ~/.claude/skills/benchctrl
```

**Copy** (static install):

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills\benchctrl" | Out-Null
Copy-Item -Recurse -Force .\skills\benchctrl\* "$env:USERPROFILE\.claude\skills\benchctrl\"
```

```bash
mkdir -p ~/.claude/skills/benchctrl
cp -r skills/benchctrl/* ~/.claude/skills/benchctrl/
```

Verify it loaded: in a fresh Claude Code session, ask "what's the canonical
recording pattern in benchctrl?" — Claude should invoke the `benchctrl` skill
and respond with the `with SMU.open() as smu: ...` context-manager
pattern. If the skill didn't activate, ask Claude to `/list-skills` or
similar (commands vary by Claude Code build).

### MCP vs skill: when does which apply?

| User says... | Claude does... |
|---|---|
| "Set 3.3V and record 5 seconds" | Calls MCP `set_voltage` → `set_current_limit` → `enable_output` → `record` |
| "Write a voltage-sweep script and save the I-V curve" | Loads benchctrl skill, writes Python with `SMU` + `Recording` |
| "What's the peak current right now?" | Calls MCP `live("mc")` or `take_snapshot` |
| "Process every .opensmu file in /captures/" | Loads benchctrl skill, writes Python with `Recording.load()` |
| "What channels does the Arc have?" | Either: MCP `list_channels` (returns JSON) or benchctrl skill (channel table in markdown) |

The MCP server is for **driving the device**; the skill is for **writing code about the device**. They complement each other.

## SDK ↔ MCP parity principle

The MCP server is intentionally kept in sync with the benchctrl Python
SDK. When a new SDK feature lands that's meaningful in an interactive /
tool-calling context, a matching MCP tool ships with it:

| SDK feature (introduced) | MCP equivalent |
|---|---|
| `SMU.set_voltage()` (v0.1.0) | `set_voltage` tool |
| `SMU.start_recording()` (v0.1.0) | `record` tool |
| `SMU.get_*` GET interface (v0.2.0) | `state`, `versions`, `info` (cached + live readbacks) |
| `Recording.save_parquet()` (v0.4.0) | `record(save_path="…parquet")`, `export_recording(in, out)` |
| `Recording.plot()` (v0.4.0) | `record(plot_png=…)`, `plot_recording(in, out)` |
| `Recording.to_numpy()` / `to_pandas()` (v0.4.0) | *N/A* — in-process objects don't cross the MCP boundary; use file-based tools instead |

When benchctrl gains a new wire command, surface, or output format, the
expectation is: **if it fits the chat / tool-calling model, it ships
with an MCP tool in the same release.** The exceptions are in-memory
Python objects (`numpy.ndarray`, `pandas.DataFrame`, matplotlib
`Figure`) which can't meaningfully cross the MCP serialisation
boundary — for those we expose file-based equivalents (`save_parquet`,
plot-to-PNG) instead.

## What's not exposed

- **Calibration and firmware upgrade** — deferred at the library level
  for safety, not omitted from the tool surface (see
  [`../ROADMAP.md`](../ROADMAP.md)). The SDK stubs raise
  `BenchNotImplementedError`.
- **Multi-Arc coordination** — one server holds one Arc. Multiple
  *different* instruments on one bench are fully supported by a single
  server, and as of 1.2 they can be split across machines.
- **The run engine** — submitting and steering unattended runs is the
  agent's surface, not this one. See [`runs.md`](runs.md).

Battery emulation *is* exposed (the `battery_*` tools), as is saved-
recording analysis (`recording_summary`, `plot_recording`,
`export_recording`) — both were listed as unavailable in older
versions of this document.

## Troubleshooting

**"no Arc devices found"** when the device is plugged in: another
process holds the COM port. Kill `otii_server`/`otii_core`/any other
benchctrl instance and retry.

**Tool calls hang**: the device may have dropped streaming after USB
disturbance. Call `reconnect()` to force a re-init handshake.

**"REFUSED: confirm_dut_attached=False"** from `enable_output`: that's
the safety guard. Set a voltage + current limit, verify your DUT can
tolerate them, then retry with the confirmation flag.

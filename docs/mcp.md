# OpenSMU MCP server

A [Model Context Protocol](https://modelcontextprotocol.io) server that
exposes your Arc Pro as a set of tools any MCP-aware client (Claude Code,
Claude Desktop, Cursor, custom agents) can call. Built on the official
`mcp` Python SDK.

## Install

```bash
pip install "opensmu[mcp]"
```

For development:

```bash
pip install -e ".[mcp,dev]"
```

## Run

The server speaks MCP over stdio (the standard transport for editor /
desktop integrations):

```bash
opensmu-mcp
```

Or equivalently:

```bash
python -m opensmu.mcp
```

The server holds the SMU connection for its lifetime — only one process
can hold the device at a time. Closing the server (or calling the
`disconnect` tool) releases it.

## Wire into Claude Code

Add to your Claude Code MCP configuration:

```json
{
  "mcpServers": {
    "opensmu": {
      "command": "opensmu-mcp"
    }
  }
}
```

Claude will pick it up the next time it starts. Verify with `/mcp` in
the Claude Code interface — `opensmu` should appear under connected
servers.

## Wire into Claude Desktop

Edit `claude_desktop_config.json` (path varies by OS — see the [Claude
Desktop docs](https://modelcontextprotocol.io/quickstart/user)) and add:

```json
{
  "mcpServers": {
    "opensmu": {
      "command": "opensmu-mcp"
    }
  }
}
```

Restart Claude Desktop.

## Safety model

Only **one** tool drives voltage onto the output terminals:
`enable_output`. It refuses unless **all three** of these are true:

1. `set_current_limit(amps)` has been called (bounds DUT damage in faults)
2. `set_voltage(volts)` has been called (so we know what's about to be driven)
3. The caller passes `confirm_dut_attached=True`

If any guard fails, the tool returns a structured `{"error": ..., "guidance": ...}`
response. The `guidance` field tells the LLM exactly what's needed.

Every other tool is non-destructive on a setup with nothing connected to
the output terminals.

## Tool surface

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
`.opensmu`, or `.parquet` (the last requires `opensmu[parquet]`).
`plot_png` requires `opensmu[plot]`.

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
opensmu Python code** for tasks the MCP tools don't cover (custom analysis,
batch processing, plotting, transient detection, etc.).

The skill lives in this repo at [`skills/opensmu/SKILL.md`](../skills/opensmu/SKILL.md).
Install it into your Claude config one of two ways.

**Symlink** (recommended — skill stays in sync with repo updates):

```powershell
# PowerShell:
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills" | Out-Null
New-Item -ItemType SymbolicLink `
    -Path "$env:USERPROFILE\.claude\skills\opensmu" `
    -Target "$(Resolve-Path .\skills\opensmu)"
```

```bash
# bash / zsh:
mkdir -p ~/.claude/skills
ln -s "$(pwd)/skills/opensmu" ~/.claude/skills/opensmu
```

**Copy** (static install):

```powershell
New-Item -ItemType Directory -Force "$env:USERPROFILE\.claude\skills\opensmu" | Out-Null
Copy-Item -Recurse -Force .\skills\opensmu\* "$env:USERPROFILE\.claude\skills\opensmu\"
```

```bash
mkdir -p ~/.claude/skills/opensmu
cp -r skills/opensmu/* ~/.claude/skills/opensmu/
```

Verify it loaded: in a fresh Claude Code session, ask "what's the canonical
recording pattern in opensmu?" — Claude should invoke the `opensmu` skill
and respond with the `with SMU.open() as smu: ...` context-manager
pattern. If the skill didn't activate, ask Claude to `/list-skills` or
similar (commands vary by Claude Code build).

### MCP vs skill: when does which apply?

| User says... | Claude does... |
|---|---|
| "Set 3.3V and record 5 seconds" | Calls MCP `set_voltage` → `set_current_limit` → `enable_output` → `record` |
| "Write a voltage-sweep script and save the I-V curve" | Loads opensmu skill, writes Python with `SMU` + `Recording` |
| "What's the peak current right now?" | Calls MCP `live("mc")` or `take_snapshot` |
| "Process every .opensmu file in /captures/" | Loads opensmu skill, writes Python with `Recording.load()` |
| "What channels does the Arc have?" | Either: MCP `list_channels` (returns JSON) or opensmu skill (channel table in markdown) |

The MCP server is for **driving the device**; the skill is for **writing code about the device**. They complement each other.

## SDK ↔ MCP parity principle

The MCP server is intentionally kept in sync with the opensmu Python
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

When opensmu gains a new wire command, surface, or output format, the
expectation is: **if it fits the chat / tool-calling model, it ships
with an MCP tool in the same release.** The exceptions are in-memory
Python objects (`numpy.ndarray`, `pandas.DataFrame`, matplotlib
`Figure`) which can't meaningfully cross the MCP serialisation
boundary — for those we expose file-based equivalents (`save_parquet`,
plot-to-PNG) instead.

## What's not exposed

- Battery emulation, calibration, firmware upgrade — deferred at the
  library level (see [`../ROADMAP.md`](../ROADMAP.md)).
- The native `.opensmu` file format is round-trippable — to analyse a
  saved recording, use the Python `Recording.load()` API directly.
- Multi-device coordination — open one server per device on different
  port names if needed (one server holds one port).

## Troubleshooting

**"no Arc devices found"** when the device is plugged in: another
process holds the COM port. Kill `otii_server`/`otii_core`/any other
opensmu instance and retry.

**Tool calls hang**: the device may have dropped streaming after USB
disturbance. Call `reconnect()` to force a re-init handshake.

**"REFUSED: confirm_dut_attached=False"** from `enable_output`: that's
the safety guard. Set a voltage + current limit, verify your DUT can
tolerate them, then retry with the confirmation flag.

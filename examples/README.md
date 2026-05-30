# Examples

Runnable scripts that exercise each benchctrl subsystem. Pick whichever
matches what you're building.

| Script | What it does | Requires |
|---|---|---|
| [`basic.py`](basic.py) | Open the Arc, set 3.3 V, record for 5 s, save CSV | Arc / Arc Pro |
| [`voltage_sweep.py`](voltage_sweep.py) | Step through voltage levels and measure current at each | Arc / Arc Pro + DUT |
| [`streaming.py`](streaming.py) | Real-time sample iterator (host-driven, low-latency) | Arc / Arc Pro |
| [`save_and_load.py`](save_and_load.py) | Native `.opensmu` round-trip + CSV / JSON exports | Arc / Arc Pro |
| [`battery_emulator.py`](battery_emulator.py) | Emulate a battery profile against a DUT using a 100 Hz host-side OCV + ESR control loop | Arc Pro + battery profile JSON |
| [`bench_qr10x.py`](bench_qr10x.py) | Drive the Eastwood QR10x programmable resistor | QR10x on USB-Serial |
| [`bench_dl3031a.py`](bench_dl3031a.py) | Drive the Rigol DL3031A electronic load — including LIST / transient / battery-discharge modes | DL3031A on USB-TMC, `benchctrl[bench-visa]` |
| [`mcp_server.py`](mcp_server.py) | Start the benchctrl MCP server (92 tools for LLM clients) | `benchctrl[mcp]` |

For reproducible regression-quality scenarios that combine the
battery emulator with a programmable load, see
[`scenarios/`](../scenarios/) — that's not really an "example" but
a packaged scenario harness.

## Install for examples

The examples assume base + extras:

```bash
pip install -e ".[dev,mcp,bench-visa,science]"
```

You don't need all extras to run any single example. The script's
docstring lists its specific requirements.

## Conventions

- Each script is single-file and self-documenting (read the docstring
  for setup + usage)
- All scripts accept `--help` to show their CLI surface
- No script will damage hardware without an explicit confirmation flag
  — the safety convention is that voltage-on / current-flowing
  operations require either context-manager scope (which auto-disables
  on exit) or an explicit `--enable-output` / `--confirm` flag (none
  currently need one because the harness scope handles it)

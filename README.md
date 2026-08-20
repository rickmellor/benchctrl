# benchctrl

Open-source Python control stack for your lab bench. Drives the
[Qoitech Otii Arc / Arc Pro][otii] SMU directly over USB CDC-ACM,
plus a growing set of companion instruments (programmable loads,
resistors, power supplies), with one MCP server that exposes the whole
bench to LLM agents. Cross-platform (Windows / Linux / macOS) via
[pyserial][pyserial] / [pyvisa][pyvisa].

The bench doesn't have to be on the same machine as the agent, and it
doesn't have to exist: `benchctrl.net` puts the instruments on a
remote host and `benchctrl.sim` replaces them with wire-protocol
simulators — the same 280 MCP tools drive all three cases unchanged.

Driver-symmetric architecture: every instrument lives under
`benchctrl.drivers.<vendor_model>/`, the Otii Arc included. Battery
emulator / profiler / analytics depend on a `SourceMeasurementUnit`
Protocol, not on any concrete driver — bring your own SMU and it
slots in.

| | |
|---|---|
| **Version** | 1.2.0 |
| **Tests** | 1333 hardware-free + 173 hardware-marked |
| **MCP tools** | 280 |
| **License** | MIT |
| **Python** | 3.9 – 3.13 |
| **Hardware (today)** | Qoitech Otii Arc / Arc Pro (SMU), Eastwood Tech QR10x (resistor), Rigol DL3031A (load), Rigol DP2031 (PSU), Siglent SDM4065A (DMM) |
| **No hardware?** | Every driver has a wire-protocol simulator — `--simulate` runs the whole stack |

[otii]: https://www.qoitech.com/otii/
[pyserial]: https://pyserial.readthedocs.io/
[pyvisa]: https://pyvisa.readthedocs.io/

## What this is for

You have an Otii Arc / Arc Pro on the bench, maybe a programmable load
or two, and you want to:

- Drive everything from your own Python scripts over plain USB.
- Emulate a battery profile against a real DUT, with state-of-charge
  tracking and OCV-IR-drop modeling — and pick which instrument
  sources or sinks (the Arc plays battery; the DL3031A can play
  battery-side discharge at higher current).
- Wire a programmable load, supply or meter (Eastwood QR10x resistor
  box, Rigol DL3031A electronic load, Rigol DP2031 triple-output PSU,
  Siglent SDM4065A 6½-digit DMM) into the same workflow.
- Hand a real bench to an LLM agent through the [Model Context
  Protocol][mcp] without writing your own tool surface.
- Save reproducible test scenarios you can re-run as regression checks.
- Keep the bench in the lab and drive it from your laptop, or develop
  against simulators with no instruments attached at all.
- Hand the bench a declarative experiment and walk away — it runs
  unattended, survives the host disconnecting, and hands back a
  self-describing artifact bundle.

benchctrl is all of that, in one package.

[mcp]: https://modelcontextprotocol.io

## Architecture

```
            +-----------------------------------------+
            |  MCP server (benchctrl.mcp)             |   280 tools — drives every driver
            |    orchestrates per-driver registration |   from Claude Code / Desktop / etc
            +-----------------------------------------+
                                 |
                                 v
            +-----------------------------------------+
            |  benchctrl.session  — resolve()         |   Per-device seam. Returns a local
            |    local  |  remote  |  sim             |   driver, a remote proxy, or a
            +-----------------------------------------+   simulator. Unconfigured = local.
                  |              |              |
                  v              v              v
        +-----------+ +---------+ +----------+ +---------+ +----------+
        | Otii Arc  | | QR10x   | | DL3031A  | | DP2031  | | SDM4065A |  Drivers (peers)
        | driver    | | driver  | | driver   | | driver  | | driver   |
        +-----------+ +---------+ +----------+ +---------+ +----------+
              ^            ^            ^           ^
              |            |            |           |         (measure-only —
        +-----+------------+------------+-----------+-----+     sources nothing,
        |            SourceMeasurementUnit                |     so not an SMU)
        |            (benchctrl.interfaces)               |  Protocol — drivers conform
        +-------------------------------------------------+
              ^                    ^                  ^
              |                    |                  |
        +-----+----+     +---------+--------+   +-----+------+
        | battery  |     | scenarios/       |   | agent/runs |  Vendor-agnostic
        | emulator |     | run.py harness   |   | unattended |  subsystems
        | profiler |     +------------------+   +------------+
        +----------+
```

Two seams make the rest optional. `session.resolve()` decides
per device whether you get real hardware, a proxy to another machine
(`benchctrl.net`), or a wire-protocol simulator (`benchctrl.sim`) —
and the 280 tools above it cannot tell the difference. The
`SourceMeasurementUnit` Protocol means battery, scenarios, and the run
engine never name a concrete driver.

### `benchctrl.drivers.otii_arc.OtiiArc` — direct hardware control

Connect, configure, source / measure, record at native streaming rates
(~4 kHz on the current channel). Frame-aware error detection. Channel
enable, expansion port, GPIO, UART. Pure Python over USB CDC-ACM.

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    with smu.record(OtiiArcChannel.MAIN_CURRENT, OtiiArcChannel.MAIN_VOLTAGE) as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    print(rec.statistics(OtiiArcChannel.MAIN_CURRENT))
    rec.save_csv("run.csv")
```

→ [`docs/getting_started.md`](docs/getting_started.md),
[`docs/api_reference.md`](docs/api_reference.md)

### `benchctrl.battery` — battery emulation + analytics

A four-piece battery workflow on top of any
`SourceMeasurementUnit`-conforming driver:

- **Battery profile I/O** — read / write the Otii-format JSON profile
  files (so the bundled CR2032, CR123A, LiPo profiles round-trip
  cleanly through benchctrl)
- **Battery life calculator** — predict runtime given a profile and a
  duty cycle
- **Profiler** — drive a discharge sweep against a real cell to
  generate a fresh profile
- **Emulator** — host-side 100 Hz control loop runs
  `V = OCV(SoC) − I·ESR(SoC)` so the DUT sees a battery's behaviour.
  Today the Arc fills this role on its high range; for high-current
  discharge characterisation the DL3031A's firmware battery-test mode
  covers the sinking side.

```python
from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.battery import BatteryProfile, Emulator, EmulatorConfig

profile = BatteryProfile.load("CR2032-Energizer-(25).json")
with OtiiArc.open() as smu:
    emu = Emulator(smu, EmulatorConfig(
        profile=profile, initial_soc=1.0,
        safety_max_voltage_V=3.5, current_limit_A=0.5,
    ))
    emu.start()
    # ... your DUT runs against a simulated CR2032 ...
    emu.stop()
```

`Emulator` and `Profiler` accept any object conforming to
`benchctrl.interfaces.SourceMeasurementUnit`, not just the Arc.

→ [`docs/battery.md`](docs/battery.md)

### Companion drivers — load + resistor

Each driver lives in its own subpackage; import only what you have.

| Driver | Class | Wire stack | Use case |
|---|---|---|---|
| Eastwood Tech QR10x | `benchctrl.drivers.eastwood_qr10x.QR10x` | USB-Serial (CH340), AT commands | Passive load — sleep current / quiescent / low-mA |
| Rigol DL3031A | `benchctrl.drivers.rigol_dl3031a.RigolDL3031A` | USB-TMC + SCPI via pyvisa | Active load — high-current / fast transients / built-in LIST / battery-discharge mode |
| Rigol DP2031 | `benchctrl.drivers.rigol_dp2031.RigolDP2031` | USB-TMC + SCPI via pyvisa | Triple-output PSU — source side; series/parallel pairing, Arb timer sequencer, trigger I/O |
| Siglent SDM4065A | `benchctrl.drivers.siglent_sdm4065a.SiglentSDM4065A` | USB-TMC + SCPI via pyvisa | 6½-digit DMM — measure-only reference; 4-wire resistance, null/Ref, NPLC control |

```python
from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.drivers.rigol_dl3031a import RigolDL3031A

with QR10x.open("COM7") as qr:
    qr.set_safety_limit(12.0)
    qr.set_resistance(1000)

with RigolDL3031A.open() as dl:        # auto-discover by Rigol VID/PID
    dl.program_list(
        steps=[(0.0001, 1.0), (0.030, 0.050), (0.0001, 1.0)],
        mode="CC", count=3, end_behavior="LAST", trigger_source="BUS",
    )
    dl.set_input(True)
    dl.trigger_now()                   # firmware plays 50 ms TX bursts
```

→ [`docs/drivers.md`](docs/drivers.md)

### `benchctrl.mcp` — Model Context Protocol server

280 tools exposing the whole SDK to MCP-aware clients (Claude Code,
Claude Desktop, etc) — Otii Arc 23, QR10x 11, DL3031A 45, DP2031 134,
SDM4065A 54, plus 13 cross-driver. Each driver registers its own tools via
`register_mcp_tools(mcp)` and the orchestrator wires them together.
Lets an LLM agent run real measurements: "discover the Arc, set
3.3 V, enable output, record for 10 seconds, report mean current."

```bash
pip install benchctrl[mcp]
benchctrl-mcp                              # or `python -m benchctrl.mcp`
```

Every public SDK method has a matching MCP tool — including the
battery emulator and the bench drivers. Safety-critical tools
(`enable_output`, `dl3031a_set_input`) require explicit confirmation
arguments.

→ [`docs/mcp.md`](docs/mcp.md)

### `benchctrl.net` + `benchctrl.agent` — remote mode

Instruments on one machine, agent on another — the 280 MCP tools are
unchanged and cannot tell the difference.

```bash
benchctrl-agent --token <token>                 # on the bench machine
BENCHCTRL_REMOTE=bench.local:9737 benchctrl-mcp # on the host
```

With nothing configured, everything stays local and behaviour is unchanged.

The agent can also be handed a declarative experiment and left alone: it
keeps running when the host disconnects, persists events durably, and
replays exactly what a reconnecting host missed.

→ [`docs/remote.md`](docs/remote.md), [`docs/runs.md`](docs/runs.md)

### `benchctrl.sim` — hardware-free simulation

Every driver has a simulator that speaks its instrument's **real wire
protocol** over a pseudo-terminal, so the production driver connects to
it unmodified. These aren't mocks of benchctrl classes — `Transport`,
the binary framing, the session handshake and the recording reader
thread all run for real, with nothing monkeypatched. The Rigols are
reached through pyvisa-py's ASRL backend, so the real pyvisa stack is
in the path too.

```bash
benchctrl-agent --simulate                        # whole bench, no instruments
BENCHCTRL_SIM_DEVICES=otii_arc,siglent_sdm4065a benchctrl-mcp  # or just some
```

```python
from benchctrl.sim import SimulatedOtiiArc
from benchctrl.drivers.otii_arc import OtiiArc

with SimulatedOtiiArc() as sim:
    smu = OtiiArc.open(sim.port)       # the production driver, unmodified
    smu.set_voltage(3.3)
    with smu.record("mc", "mv") as rec:
        ...
```

Simulator waveforms are analytically known, so tests assert exact
statistics rather than "a number arrived", and `OhmicLoad` closes the
V→I loop — the battery emulator is exercisable without a cell.

### `scenarios/` — reproducible scenario harness

End-to-end scenarios that drive the emulator against a programmable
load and save the captured response to disk as self-describing
artifacts (JSON metadata + CSV samples + PNG plot + a copy of the
input battery profile, so a saved scenario is fully reproducible
years later).

Three scenario kinds:

- **`static`** — load sweep, settle each step, record one snapshot
  per step. Verifies ESR-driven voltage sag matches the profile.
- **`dynamic`** — host-driven IoT pattern (sleep / wake / TX burst)
  at 20 Hz polling.
- **`dynamic-list`** — DL3031A's firmware LIST mode plays the
  sequence with sub-100 µs timing while `SMU.record()` captures the
  Arc's native ~4 kHz V/I stream.

```bash
python scenarios/run.py --scenario static --all
python scenarios/run.py --scenario dynamic --profile "CR2032-Energizer-(25)" --cycles 3
python scenarios/run.py --scenario dynamic-list --profile "CR2032-Energizer-(25)" --load dl3031a
```

27 scenarios shipped with the repo as reference data. Headline:
LiPo at 12 Ω, −10 °C sags to 3.24 V at 324 mA (10× ESR rise vs +20 °C)
— exactly what a real cold-soaked LiPo does, captured against an
emulated cell.

→ [`scenarios/README.md`](scenarios/README.md)

## Install

```bash
pip install benchctrl                      # SMU + battery + QR10x bench driver
pip install benchctrl[mcp]                 # + MCP server
pip install benchctrl[bench-visa]          # + Rigol DL3031A (pyvisa)
pip install benchctrl[science]             # + numpy / pandas / parquet / matplotlib
pip install benchctrl[dev]                 # + pytest / ruff / mypy
```

All output formats (Parquet, pandas, numpy, matplotlib) are
**optional lazy imports** — installing the base package is enough for
everyday work. See [extras matrix](docs/output_formats.md#install).

## Quick start (5 minutes)

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

# 1. Discover & connect (auto-detects the first connected Arc / Pro)
with OtiiArc.open() as smu:
    print(smu.info)

    # 2. Configure
    smu.set_current_limit(0.5)           # 500 mA over-current trip
    smu.enable_channels(OtiiArcChannel.MAIN_CURRENT, OtiiArcChannel.MAIN_VOLTAGE)

    # 3. Source 3.3 V and record for 5 seconds
    smu.set_voltage(3.3)
    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5.0)
        smu.set_output(False)

    # 4. Inspect
    stats = rec.statistics(OtiiArcChannel.MAIN_CURRENT)
    print(f"I: mean={stats.average*1000:.2f} mA  max={stats.max*1000:.2f} mA")

    # 5. Save (native format preserves the full sample timeline)
    rec.save("run.opensmu")
    rec.save_csv("run.csv")
```

More: [`docs/getting_started.md`](docs/getting_started.md).

## Documentation

| Doc | What's in it |
|---|---|
| [`docs/getting_started.md`](docs/getting_started.md) | Install + first capture tutorial |
| [`docs/api_reference.md`](docs/api_reference.md) | Every public class and method |
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | Driver-symmetric layout, Protocol contract, registration model |
| [`docs/design.md`](docs/design.md) | Arc driver internals + design decisions |
| [`docs/battery.md`](docs/battery.md) | Battery profile / emulator / profiler / life calculator |
| [`docs/drivers.md`](docs/drivers.md) | QR10x + DL3031A + DP2031 + SDM4065A drivers, firmware modes |
| [`docs/mcp.md`](docs/mcp.md) | MCP server setup + tool inventory |
| [`docs/simulation.md`](docs/simulation.md) | Running the stack with no hardware attached |
| [`docs/remote.md`](docs/remote.md) | Remote mode — instruments on one machine, agent on another |
| [`docs/runs.md`](docs/runs.md) | Unattended runs: declarative specs, safety envelope, artifacts |
| [`docs/dashboard.md`](docs/dashboard.md) | Read-only bench status display + the e-stop that will share it |
| [`docs/output_formats.md`](docs/output_formats.md) | `.opensmu` / Parquet / CSV / JSON / numpy / pandas / matplotlib |
| [`docs/otii_arc_protocol.md`](docs/otii_arc_protocol.md) | Arc USB wire protocol reference (reverse-engineered) |
| [`docs/AGENTS.md`](docs/AGENTS.md) | Briefing for AI agents picking up the codebase |
| [`scenarios/README.md`](scenarios/README.md) | Scenario harness + saved captures |
| [`CHANGELOG.md`](CHANGELOG.md) | Release-by-release |
| [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) | Hardware caps, firmware quirks, harness workarounds — **read this before debugging a new failure** |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, testing, conventions |
| [`ROADMAP.md`](ROADMAP.md) | Deferred features + rationale |

## Status & known limits

benchctrl is in **beta** (`Development Status :: 4 - Beta`). The SDK
surface is stable and follows semver — 1.1 added the DP2031 driver and
1.2 added remote mode, simulation, and the run engine, all additively.
Remote mode and the run engine are the newest subsystems and the most
likely to grow; the local driver surface has been stable since 1.0.

We document hardware caps, firmware quirks, and harness workarounds
explicitly in [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) rather
than burying them in per-version CHANGELOG entries. Notable ones:

- Arc Pro high-range output caps at ≈ 4.2 V under load (matters for
  LiPo emulation)
- DL3031A `:SOUR:LIST:STEP=4` is a firmware bug — driver rejects
  4-step programs at the SDK level
- DL3031A `:SOUR:FUNC:MODE FIXed` is one-way; only power-cycle
  returns the device to FIX mode
- Emulator + `SMU.record()` deadlock if run concurrently (workaround
  in the hires validation runner)
- **Remote mode authenticates but does not encrypt.** The token is
  never sent (HMAC challenge-response), but instrument traffic is
  readable on the wire — tunnel over SSH on an untrusted network
- **A software deadman cannot guarantee an output goes off** through
  a wedged driver. For genuinely unattended runs a hardware interlock
  is the only real guarantee (§ N-1)

If you hit something that isn't documented, please open an issue —
that's how we keep the list honest.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev install, test
conventions, and the architectural principles (SDK ↔ MCP parity, every
public method has a test, firmware bugs get documented in
`KNOWN_LIMITATIONS.md`).

```bash
git clone https://github.com/rickmellor/benchctrl
cd benchctrl
pip install -e ".[dev,mcp,bench-visa,science]"
pytest -m "not hardware"                 # 1333 tests, ~10 min, no device needed
pytest                                    # full suite (bench on USB)
```

## Licensing & affiliation

MIT. See [`LICENSE`](LICENSE).

benchctrl is an independent open-source project. **Not affiliated with,
endorsed by, or supported by Qoitech AB, Rigol Technologies, or
Eastwood Tech.** "Otii", "Arc", and related marks are trademarks of
Qoitech AB. "Rigol" and "DL3031A" are trademarks of Rigol
Technologies. "QR10x" is a trademark of Eastwood Tech.

Each driver opens its device's standard USB endpoint exactly the way
the operating system invites any application to do — pyserial
CDC-ACM for the Arc and QR10x, pyvisa USB-TMC for the DL3031A.

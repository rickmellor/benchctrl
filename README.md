# OpenSMU

Open-source Python control stack for USB source-measurement units. Drives
the [Qoitech Otii Arc / Arc Pro][otii] directly over its USB CDC-ACM
interface — no vendor server, no Automation Toolbox license, no GUI.
Cross-platform (Windows / Linux / macOS) via [pyserial][pyserial].

What started as an SMU driver has grown into a bench-instrument stack:
SMU control, a full battery-emulator pipeline that replaces Qoitech's
licensed Battery Toolbox, drivers for companion instruments
(programmable resistors and electronic loads), an MCP server so LLM
agents can drive a real bench, and a validation harness that captures
reproducible regression-quality scenarios.

| | |
|---|---|
| **Version** | 0.9.7 (beta) |
| **Tests** | 292 hardware-free + ~90 hardware-marked passing |
| **License** | MIT |
| **Python** | 3.9 – 3.13 |
| **Hardware** | Qoitech Otii Arc / Arc Pro (USB CDC-ACM) |

[otii]: https://www.qoitech.com/otii/
[pyserial]: https://pyserial.readthedocs.io/

## What this is for

You have an Otii Arc or Arc Pro on the bench. You want to:

- Drive it from your own Python scripts without running Qoitech's
  server or owning their automation license.
- Emulate a battery profile against a real DUT, with state-of-charge
  tracking and proper OCV-IR-drop modeling — the same job Qoitech's
  Battery Toolbox does, but free and scriptable.
- Wire a programmable load (Eastwood QR10x resistor box, Rigol DL3031A
  electronic load) into the same workflow.
- Hand a real bench to an LLM agent through the [Model Context
  Protocol][mcp] without writing your own tool surface.
- Save reproducible test scenarios you can re-run as regression checks.

OpenSMU is all of that, in one package.

[mcp]: https://modelcontextprotocol.io

## The five subsystems

```
            +-------------------------+
            |  MCP server             |   93 tools — drive any subsystem
            |  (opensmu.mcp)          |   from Claude Code / Desktop / etc
            +-------------------------+
              |          |        |
              v          v        v
    +-------------+ +---------+ +--------+
    | SMU         | | battery | | bench  |   Public SDK
    | (Arc / Pro) | | profile | | drivers|
    +-------------+ | profiler| +--------+
              ^     | emulator|     ^
              |     +---------+     |
              |          |          |
              +----------+----------+
                         |
              +-------------------------+
              | validation/             |   Reproducible scenario
              | (run_validation.py)     |   harness for the bench
              +-------------------------+
```

### 1. `opensmu.SMU` — direct hardware control

Connect, configure, source / measure, record at native streaming rates
(~4 kHz on the current channel). Frame-aware error detection. Channel
enable, expansion port, GPIO, UART. Everything the Qoitech Automation
Toolbox does at the SMU layer, for $0.

```python
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    with smu.record(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE) as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    print(rec.statistics(Channel.MAIN_CURRENT))
    rec.save_csv("run.csv")
```

→ [`docs/getting_started.md`](docs/getting_started.md),
[`docs/api_reference.md`](docs/api_reference.md)

### 2. `opensmu.battery` — Battery Toolbox replacement

Otii's Battery Toolbox is a paid license. The workflow it sells —
profile a real cell, then emulate it against a DUT with SoC tracking —
is in this package, in four phases:

- **Battery profile I/O** — bit-identical round-trip with Otii's JSON
  format (read/write Otii's bundled CR2032, CR123A, LiPo, etc.)
- **Battery life calculator** — predict runtime given a profile and a
  load
- **Profiler** — drive a discharge sweep against a real cell to
  generate a fresh profile
- **Emulator** — Arc acts as a battery: 100 Hz host control loop runs
  `V = OCV(SoC) − I·ESR(SoC)` and the DUT can't tell the difference

```python
from opensmu import SMU
from opensmu.battery import BatteryProfile, Emulator, EmulatorConfig

profile = BatteryProfile.load("CR2032-Energizer-(25).json")
with SMU.open() as smu:
    emu = Emulator(smu, EmulatorConfig(
        profile=profile, initial_soc=1.0,
        safety_max_voltage_V=3.5, current_limit_A=0.5,
    ))
    emu.start()
    # ... your DUT runs against a simulated CR2032 ...
    emu.stop()
```

→ [`docs/battery.md`](docs/battery.md)

### 3. `opensmu.bench` — companion instrument drivers

Drivers for the other instruments that share the bench with the Arc.
Each is independent; import only what you have.

| Driver | Class | Wire stack | Use case |
|---|---|---|---|
| Eastwood Tech QR10x | `opensmu.bench.QR10x` | USB-Serial (CH340), AT commands | Passive load — sleep current / quiescent / low-mA |
| Rigol DL3031A | `opensmu.bench.RigolDL3031A` | USB-TMC + SCPI via pyvisa | Active load — high-current / fast transients / built-in LIST / battery-discharge mode |

```python
from opensmu.bench import QR10x, RigolDL3031A

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

→ [`docs/bench.md`](docs/bench.md)

### 4. `opensmu.mcp` — Model Context Protocol server

93 tools exposing the whole SDK to MCP-aware clients (Claude Code,
Claude Desktop, etc). Lets an LLM agent run real measurements:
"discover the Arc, set 3.3 V, enable output, record for 10 seconds,
report mean current."

```bash
pip install opensmu[mcp]
opensmu-mcp                              # or `python -m opensmu.mcp`
```

Every public SDK method has a matching MCP tool — including the
battery emulator and the bench drivers. Safety-critical tools
(`enable_output`, `dl3031a_set_input`) require explicit confirmation
arguments.

→ [`docs/mcp.md`](docs/mcp.md)

### 5. `validation/` — reproducible scenario harness

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
python validation/run_validation.py --scenario static --all
python validation/run_validation.py --scenario dynamic --profile "CR2032-Energizer-(25)" --cycles 3
python validation/run_validation.py --scenario dynamic-list --profile "CR2032-Energizer-(25)" --load dl3031a
```

27 scenarios shipped with the repo as reference data. Headline:
LiPo at 12 Ω, −10 °C sags to 3.24 V at 324 mA (10× ESR rise vs +20 °C)
— exactly what a real cold-soaked LiPo does, captured against an
emulated cell.

→ [`validation/README.md`](validation/README.md)

## Install

```bash
pip install opensmu                      # SMU + battery + QR10x bench driver
pip install opensmu[mcp]                 # + MCP server
pip install opensmu[bench-visa]          # + Rigol DL3031A (pyvisa)
pip install opensmu[science]             # + numpy / pandas / parquet / matplotlib
pip install opensmu[dev]                 # + pytest / ruff / mypy
```

All output formats (Parquet, pandas, numpy, matplotlib) are
**optional lazy imports** — installing the base package is enough for
everyday work. See [extras matrix](docs/output_formats.md#install).

## Quick start (5 minutes)

```python
import time
from opensmu import SMU, Channel

# 1. Discover & connect (auto-detects the first connected Arc / Pro)
with SMU.open() as smu:
    print(smu.info)

    # 2. Configure
    smu.set_current_limit(0.5)           # 500 mA over-current trip
    smu.enable_channels(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE)

    # 3. Source 3.3 V and record for 5 seconds
    smu.set_voltage(3.3)
    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5.0)
        smu.set_output(False)

    # 4. Inspect
    stats = rec.statistics(Channel.MAIN_CURRENT)
    print(f"I: mean={stats.mean*1000:.2f} mA  max={stats.max*1000:.2f} mA")

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
| [`ARCHITECTURE.md`](ARCHITECTURE.md) | One-page tour of the five subsystems and how they layer |
| [`docs/design.md`](docs/design.md) | SMU-layer architecture + design decisions |
| [`docs/battery.md`](docs/battery.md) | Battery profile / emulator / profiler / life calculator |
| [`docs/bench.md`](docs/bench.md) | QR10x + DL3031A drivers, firmware modes |
| [`docs/mcp.md`](docs/mcp.md) | MCP server setup + tool inventory |
| [`docs/output_formats.md`](docs/output_formats.md) | `.opensmu` / Parquet / CSV / JSON / numpy / pandas / matplotlib |
| [`docs/protocol.md`](docs/protocol.md) | USB wire protocol reference (reverse-engineered) |
| [`docs/AGENTS.md`](docs/AGENTS.md) | Briefing for AI agents picking up the codebase |
| [`validation/README.md`](validation/README.md) | Scenario harness + saved captures |
| [`CHANGELOG.md`](CHANGELOG.md) | Release-by-release |
| [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md) | Hardware caps, firmware quirks, harness workarounds — **read this before debugging a new failure** |
| [`CONTRIBUTING.md`](CONTRIBUTING.md) | Dev setup, testing, conventions |
| [`ROADMAP.md`](ROADMAP.md) | Deferred features + rationale |

## Status & known limits

OpenSMU is in **beta** (`Development Status :: 4 - Beta`). The SDK
surface is stable; we're not planning breaking changes before 1.0.
The validation harness is the most active area — new scenario types
land in `validation/` as they're characterized.

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

If you hit something that isn't documented, please open an issue —
that's how we keep the list honest.

## Contributing

See [`CONTRIBUTING.md`](CONTRIBUTING.md) for dev install, test
conventions, and the architectural principles (SDK ↔ MCP parity, every
public method has a test, firmware bugs get documented in
`KNOWN_LIMITATIONS.md`).

```bash
git clone https://github.com/opensmu/opensmu
cd opensmu
pip install -e ".[dev,mcp,bench-visa,science]"
pytest -m "not hardware"                 # ~3 minutes, no device needed
pytest                                    # full suite (Arc Pro on USB)
```

## Licensing & affiliation

MIT. See [`LICENSE`](LICENSE).

OpenSMU is an independent open-source project. **Not affiliated with,
endorsed by, or supported by Qoitech AB.** "Otii", "Arc", and related
marks are trademarks of Qoitech AB. The DL3031A driver is not
affiliated with Rigol Technologies.

The wire protocol was reverse-engineered from passive observation of
legitimate USB traffic between a user's own hardware and software they
had licensed. The hardware enforces no license check on the wire;
OpenSMU simply opens the device's standard CDC-ACM endpoint, exactly
as the operating system invites any application to do.

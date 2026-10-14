# Contributing to benchctrl

Thanks for considering a contribution. This document covers everything
you need to set up, test, and ship a change.

If you're an AI agent picking up the codebase rather than a human,
the more efficient briefing is [`docs/AGENTS.md`](docs/AGENTS.md) +
[`skills/benchctrl/SKILL.md`](skills/benchctrl/SKILL.md). Come back here
for conventions and PR mechanics.

## Development setup

```bash
git clone https://github.com/rickmellor/benchctrl
cd benchctrl
pip install -e ".[dev,mcp,bench-visa,science]"
```

Python 3.9+ is supported; CI runs 3.9, 3.10, 3.11, 3.12, 3.13.

The extras you actually need depend on what you're touching:

| Working on... | Install |
|---|---|
| SMU / battery / QR10x bench driver | `pip install -e ".[dev]"` |
| MCP server | `pip install -e ".[dev,mcp]"` |
| Rigol DL3031A / DP2031 / Siglent SDM4065A drivers / `bench-visa` | `pip install -e ".[dev,bench-visa]"` |
| CyberPower PDU41002 driver | `pip install -e ".[dev]"` — pyserial for the serial link, `/usr/bin/ssh` for the network one |
| Ontrak ADU218 driver | `pip install -e ".[dev]"` — the driver itself needs **nothing**; stdlib `fcntl`/`ctypes` only |
| Silicon Labs CP2112 driver | `pip install -e ".[dev]"` — likewise nothing of its own; stdlib `os`/`fcntl`/`ctypes` only |
| Parquet / pandas / matplotlib output paths | `pip install -e ".[dev,science]"` |
| `benchctrl.sim` simulators | `pip install -e ".[dev,bench-visa]"` (the SCPI sims need pyvisa-py) |
| `benchctrl.net` / `benchctrl.agent` | `pip install -e ".[dev]"` — stdlib + pyserial only, by design |
| Everything | `pip install -e ".[dev,mcp,bench-visa,science]"` |

`pyvisa` (in `bench-visa`) needs a VISA backend. On Windows the easiest
path is to install the [Rigol Ultra Sigma][ultra-sigma] driver, which
bundles a working backend. Alternatives: NI-VISA, Keysight IO
Libraries, or `pyvisa-py` + a USB backend (per-device WinUSB driver
via Zadig).

[ultra-sigma]: https://www.rigolna.com/products/digital-oscilloscopes/ultrasigma/

## Running tests

The test suite is split by hardware requirement using pytest markers.

```bash
pytest -m "not hardware" -q       # ~10 minutes, no device needed (1333 tests)
pytest -m hardware -q              # requires Arc Pro + companion instruments (173)
pytest -q                          # both
```

Hardware-marked tests need:

- An **Arc Pro** on USB (`COM*` / `/dev/ttyACM*` / `/dev/cu.usbmodem*`)
- For DL3031A tests: a **Rigol DL3031A** on USB-TMC
- For DP2031 tests: a **Rigol DP2031** on USB-TMC
- For QR10x tests: an **Eastwood QR10x** on a USB-Serial port
- For SDM4065A tests: a **Siglent SDM4065A** on USB-TMC
- For CLI tests (`test_hardware_cli.py`): an **Ontrak ADU218**, and the tests
  must run **where the instruments are** — they spawn `python3 -m benchctrl` as
  a real subprocess, so on a remote bench that means running them on the bench
  machine, not against it. Two of them switch a relay and skip unless
  `BENCHCTRL_CLI_HW_WRITE=1`, because the CLI has no `allowed_relays` flag by
  design.

Tests skip cleanly with a useful message if the hardware isn't
present, so partial setups don't fail the suite — you just get fewer
green dots.

If you're adding a new hardware test, follow the existing pattern:

```python
@pytest.mark.hardware
def test_hardware_my_thing():
    pytest.importorskip("pyvisa")
    resource = os.environ.get("BENCHCTRL_DL3031A_RESOURCE")
    try:
        load = RigolDL3031A.open(resource=resource)
    except Exception as e:
        pytest.skip(f"DL3031A unavailable: {e}")
    try:
        # ... real device assertions ...
    finally:
        load.close()
```

## Lint and type-check

```bash
ruff check src tests scenarios
mypy src
```

CI runs both. `ruff` is configured for E/F/W/I/B/UP/N/SIM with
`line-length = 100`. `mypy` is `check_untyped_defs = true` but not
strict — strict mode is a deferred item in `ROADMAP.md`.

## Conventions

benchctrl has a few load-bearing principles you'll find referenced
throughout the codebase and CHANGELOG. New code is expected to follow
them.

### 1. SDK ↔ MCP parity

Every public SDK method has a matching MCP tool. When you add a public
method to `OtiiArc` / `Emulator` / `QR10x` / `RigolDL3031A` /
`RigolDP2031` / `SiglentSDM4065A` / `CyberPowerPDU41002` /
`OntrakADU218` / `CP2112`, add the matching MCP tool to that
driver's `mcp_tools.py` (or, for cross-driver tools,
`src/benchctrl/mcp.py`) in the same PR, and list it in that module's
`_TOOLS` tuple.

`test_sdm4065a_mcp_tools_cover_the_driver_surface` in
`tests/test_mcp.py` asserts this mechanically for the SDM4065A, with
the deliberate exemptions named and justified in the test. Copy that
pattern for a new driver — the failure mode it catches is a capability
that works locally and is invisible to an agent. The ADU218 has the
same parity test, and it earns its keep: the driver's public surface and
its tool surface were written days apart, and the test is what caught
the gap.

The ADU218 also carries the *converse* test,
`test_every_whitelisted_command_is_reachable_from_the_sdk`, which is worth
copying for any driver with a command whitelist. Parity guards the SDK →
MCP direction; this one guards wire → SDK, and it caught a command that was
whitelisted, given a hardware-measured reply width, modelled by the
simulator and written into a docs table while **no public method could
send it**. A capability documented as present and absent at once is not
something either the parity test or a grep will find.

Tools reach their device through the module's `_get_<device>()`
singleton, which populates via `session.resolve()`. Don't open a
driver directly in a tool — that bypasses the seam and the tool then
works only locally, not remote or simulated.

The MCP tool wraps the SDK method, coerces arguments to JSON-friendly
types where needed, and returns a dict (never a custom dataclass).
For SDK methods that return numbers, the MCP tool wraps in
`{"<name>": value}`; for SDK methods that return a dict / dataclass,
the MCP tool returns the dict (calling `__dict__` or `to_dict()` if
appropriate).

Granular wrappers behind a convenience helper (e.g. the individual
`list_set_*` methods behind `program_list`) can be SDK-only —
document them in `docs/drivers.md` as such. The opposite isn't
allowed: no MCP tool without a corresponding SDK method.

### 2. Every public method has a test

A hardware-free test for the SDK surface; a hardware-marked test for
the end-to-end behavior. Both should exercise the same wire format /
SCPI string.

**Prefer `benchctrl.sim` over mocks.** We've been bitten by mocks
silently diverging from real hardware (see the v0.9.2 emulator mock
fix). A simulator speaks the instrument's actual wire protocol over a
pty, so the transport, framing, handshake and reader threads stay in
the path and a divergence shows up as a failure instead of a passing
fiction. Reach for a mock only when you specifically need to assert
that a particular call was made.

Where a mock is still the right tool, mirror the real surface's method
names exactly so a rename breaks the test.

### 3. Layers do not skip

```
Application / examples / CLI / scenarios / applications
    ↓
Vendor-agnostic subsystems (battery, scenarios harness, mcp, agent/runs)
    ↓
session.resolve()  —  local | remote | sim
    ↓
SourceMeasurementUnit Protocol + framework primitives
    ↓
Driver public API (OtiiArc / QR10x / RigolDL3031A / RigolDP2031 /
                  SiglentSDM4065A / CyberPowerPDU41002 / OntrakADU218 /
                  CP2112)
    ↓
Driver-internal modules (channels / protocol / transport)
    ↓
pyserial / pyvisa   —   or a sim pty, or a net proxy
```

`Recording` never reaches into a driver's transport. The battery
emulator never imports a concrete driver class. The MCP orchestrator
never imports `pyserial`. If you find yourself wanting to skip a
layer, that's a signal something's at the wrong height in the stack.

Adding a new instrument means a new `benchctrl.drivers.<vendor_model>/`
subpackage with a `register_mcp_tools(mcp)` function. If it's an SMU,
it conforms to `SourceMeasurementUnit`. If it's a different category
(load, switch, DAC), define a new Protocol when the second concrete
instance lands — premature Protocols are overhead without payoff.

### 4. Errors propagate; silent fallbacks are bugs

`except Exception: pass` is almost always wrong. The v0.9.7 review
found two compound silent-failure bugs in the emulator that combined
to make hardware faults look like a working emulator producing zero
current. **Default to propagating.** If you genuinely need a quiet
fallback (rare), document why on the line and add a test that
verifies the fallback path is reached.

The one acceptable pattern is teardown cleanup:

```python
try:
    self.set_input(False)
except Exception as e:
    log.warning("set_input(False) failed during teardown: %s", e)
```

— and even there, log loud enough that the operator sees it.

### 5. Document firmware bugs in `KNOWN_LIMITATIONS.md`

If you find a hardware or firmware quirk, write it up under the
appropriate section (`Hardware` / `Driver-firmware interactions` /
`Harness` / `Network`). The driver should *reject* the known-bad case
at the API boundary with a clear error pointing to the workaround.

Example (`drivers/rigol_dl3031a/driver.py`):

```python
if steps == 4:
    raise RigolDLValueError(
        "LIST STEP=4 is a firmware bug — no steps fire. "
        "Use 3 or 5 steps with appropriate count instead."
    )
```

Don't silently work around firmware bugs — surface them so users
understand what's happening.

### 6. CHANGELOG is honest

Every release entry includes "Discovered" and "Known issues" subsections
when they apply. "Investigation TBD" is fine as a placeholder but it's
not a substitute for documenting what does and doesn't work. The
adversarial review process specifically checks for CHANGELOG accuracy.

## Pull request workflow

1. Fork + branch off `master`.
2. Make your change with tests.
3. Update `CHANGELOG.md` under an "Unreleased" header at the top, or
   add to the active version's entry if you're collaborating on it.
4. If your change adds a known limit, also update
   `KNOWN_LIMITATIONS.md`.
5. Run the hardware-free suite locally (`pytest -m "not hardware"`).
6. Open the PR. The template will walk you through the checklist.

CI runs:

- `pytest -m "not hardware"` on Python 3.9 → 3.13
- `ruff check`
- `mypy`

Hardware tests run locally only — there's no per-PR way to verify
them on CI without dedicated bench hardware.

## Where things live

```
benchctrl/
├── README.md, ARCHITECTURE.md          repo entry points
├── CHANGELOG.md, KNOWN_LIMITATIONS.md   what changed / what's broken
├── CONTRIBUTING.md (this file)
├── ROADMAP.md                          deferred features
├── PROGRESS.md                         live build snapshot
├── pyproject.toml                      package config
├── src/benchctrl/
│   ├── __init__.py                      framework primitives only
│   ├── channels.py                      StandardChannel enum
│   ├── interfaces.py                    SourceMeasurementUnit Protocol
│   ├── exceptions.py                    BenchError hierarchy
│   ├── recording.py, samples.py         Recording + statistics + exports
│   ├── mcp.py                           MCP orchestrator + cross-driver tools
│   ├── cli.py                           benchctrl CLI entry
│   ├── session.py                       resolve() — local | remote | sim seam
│   ├── config.py                        layered config, DEVICE_KEYS
│   ├── discovery.py                     bench-wide device identification
│   ├── battery/                         battery characterisation + emulation
│   │   ├── profile.py                   profile JSON I/O
│   │   ├── calculator.py                predicted runtime
│   │   ├── profiler.py                  generate fresh profile (any SMU)
│   │   └── emulator.py                  act as a battery (any SMU)
│   ├── analysis/                        v1.x placeholder
│   ├── dashboards/                      read-only bench status display
│   │   ├── feed.py                      AgentFeed — observer session + events
│   │   ├── state.py                     BenchStatus — snapshot, no renderer
│   │   └── fui/                         the cinematic console
│   │       ├── server.py                stdlib http.server, /api/view
│   │       ├── view.py                  snapshot -> view model (pure)
│   │       └── static/                  index.html, fui.css, fui.js
│   ├── drivers/
│   │   ├── otii_arc/                    Qoitech Otii Arc / Arc Pro SMU
│   │   │   ├── device.py                OtiiArc class
│   │   │   ├── protocol.py              wire framing + codec
│   │   │   ├── transport.py             pyserial wrapper
│   │   │   ├── channels.py              OtiiArcChannel enum
│   │   │   └── mcp_tools.py             register_mcp_tools(mcp)
│   │   ├── eastwood_qr10x/              Eastwood QR10x programmable resistor
│   │   │   ├── driver.py                QR10x class
│   │   │   └── mcp_tools.py
│   │   ├── rigol_dl3031a/               Rigol DL3031A electronic load
│   │   │   ├── driver.py                RigolDL3031A class
│   │   │   └── mcp_tools.py
│   │   ├── rigol_dp2031/                Rigol DP2031 triple-output PSU
│   │   │   ├── driver.py                RigolDP2031 class
│   │   │   └── mcp_tools.py
│   │   ├── siglent_sdm4065a/            Siglent SDM4065A 6½-digit DMM
│   │   │   ├── driver.py                SiglentSDM4065A class
│   │   │   └── mcp_tools.py
│   │   ├── cyberpower_pdu41002/         CyberPower 8-outlet switched PDU
│   │   │   ├── driver.py                CLI engine + CyberPowerPDU41002
│   │   │   ├── links.py                 serial and ssh byte pipes
│   │   │   └── mcp_tools.py
│   │   ├── ontrak_adu218/               Ontrak ADU218 relays + digital I/O
│   │   │   ├── usbfs.py                 raw USBDEVFS ioctls, stdlib only
│   │   │   ├── driver.py                OntrakADU218 class
│   │   │   └── mcp_tools.py
│   │   └── silabs_cp2112/               CP2112 open-drain control lines
│   │       ├── hidraw.py                HID feature reports over hidraw
│   │       ├── driver.py                CP2112 class
│   │       └── mcp_tools.py
│   ├── sim/                             wire-protocol simulators (pty-backed)
│   │   ├── base.py, loopback.py         SimDevice + pty pair
│   │   ├── otii_arc.py, qr10x.py        per-instrument simulators
│   │   ├── scpi.py                      both Rigols, via pyvisa-py ASRL
│   │   ├── sdm4065a.py                  Siglent DMM, incl. its quirks
│   │   ├── pdu41002.py                  the PDU's CLI, incl. its error shapes
│   │   ├── adu218.py                    subclasses the real USB link, not a pty
│   │   ├── cp2112.py                    stands in at the hidraw link seam
│   │   ├── waveforms.py                 analytically-known signals
│   │   └── factories.py                 production driver + simulator
│   ├── transports/                      reaching a device the kernel can't
│   │   ├── ch341.py                     userspace CH340 driver over libusb
│   │   ├── ptybridge.py                 userspace device -> real pty
│   │   └── autoserial.py                kernel driver first, userspace fallback
│   ├── net/                             remote wire protocol
│   │   ├── frames.py, codec.py          typed frames, allowlisted values
│   │   ├── auth.py                      HMAC-SHA256 challenge-response
│   │   ├── client.py, proxy.py          host side
│   │   ├── beacon.py                    UDP discovery
│   │   └── errors.py                    exception marshalling
│   └── agent/                           bench-side server
│       ├── main.py, server.py           benchctrl-agent entry + server
│       ├── worker.py, dispatch.py       one owning thread per device
│       ├── registry.py, safety.py       device table + safety governor
│       ├── runs/                        declarative unattended experiments
│       └── llm/                         advisory supervisor
├── tests/                              hw-free + hardware-marked
├── docs/                               topical docs
├── scenarios/
│   ├── run.py                          scenario harness
│   ├── README.md                       harness docs + results
│   └── saved/                          saved captures (JSON / CSV / PNG)
├── applications/sensor_profiler/       DUT power profiling + Streamlit browser
├── bugs/                               vendor firmware bug reports
├── skills/benchctrl/SKILL.md            Claude Code skill briefing
└── .github/                            PR + issue templates, CI workflow
```

When you're hunting for something:

- **"Where does X live?"** — search `src/benchctrl/` first, then
  `scenarios/` if it's harness-related.
- **"How does X work?"** — `docs/` is topical; `ARCHITECTURE.md` is
  the overview.
- **"Why was X done this way?"** — `CHANGELOG.md` and
  `docs/design.md`. Git history is also good — every commit explains
  the why, not just the what.
- **"Why doesn't X work?"** — `KNOWN_LIMITATIONS.md` first.

## Code of conduct

Be excellent to each other. Hardware bring-up has a way of producing
frustration; channel it into good bug reports.

## License

By contributing, you agree your contributions are licensed under MIT.

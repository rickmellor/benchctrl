# benchctrl build progress

Live status log so anyone (human or AI) picking this up mid-flight
knows exactly where it is. Updated after every milestone; latest entry
on top.

This file is the *working* snapshot. For the release-by-release
history see [`CHANGELOG.md`](CHANGELOG.md); for what's deliberately
not built yet see [`ROADMAP.md`](ROADMAP.md); for hardware and
firmware caps see [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).

## Status snapshot

- **Version**: 1.2.0
- **Branch**: `feat/remote-mode` (6 feature commits + a docs pass ahead
  of `master`)
- **Tests**: 956 hardware-free + 152 hardware-marked, 23 skipped.
  Hardware-free suite runs in ~7 minutes with nothing plugged in.
- **MCP tools**: 226 — Otii Arc 23, QR10x 11, DL3031A 45, DP2031 134,
  cross-driver 13
- **Drivers**: Otii Arc / Arc Pro (SMU), Eastwood QR10x (programmable
  resistor), Rigol DL3031A (electronic load), Rigol DP2031
  (triple-output PSU)
- **Scenarios captured**: 27 — 11 QR10x + 11 DL3031A standard + 3 hires
  + 2 dynamic-list
- **Entry points**: `benchctrl`, `benchctrl-mcp`, `benchctrl-agent`
- **Hardware**: Arc Pro, DL3031A, DP2031, QR10x. Outputs off between
  runs; hardware tests skip cleanly when a device is absent.

## Where things stand

### Shipped and stable

**Driver layer** (since 1.0, extended in 1.1). Every instrument is a
peer under `benchctrl.drivers.<vendor_model>/`, conforming to the
`SourceMeasurementUnit` Protocol where it makes sense. The DP2031
landed in 1.1.0 as the largest surface in the codebase — 134 MCP tools
covering the Arb timer sequencer, the IoT power analyzer, trigger I/O
and the device filesystem.

**Battery subsystem**. Profile I/O, life calculator, hardware
profiler, and the 100 Hz host-side emulator. Vendor-agnostic — works
against any conforming driver.

**MCP server**. 226 tools. SDK ↔ MCP parity is a review gate, not an
aspiration.

**Scenario harness**. Three kinds (static, dynamic, dynamic-list) with
27 committed captures as reference data.

### Shipped in 1.2, newest and most likely to move

**`benchctrl.sim`** — wire-protocol simulators over ptys. The stack
now runs end-to-end with no hardware, and most of the hardware-free
suite drives simulators rather than mocks, so transport, framing,
handshake and reader threads stay in the path.

**`benchctrl.session` / `.config` / `.discovery`** — the local /
remote / sim seam. Resolution is per device key, not per process.
Unconfigured behaviour is byte-for-byte what it was before.

**`benchctrl.net` + `benchctrl.agent`** — remote mode. Typed frames on
one socket, HMAC-SHA256 challenge-response auth, allowlisted value
codec, one owning thread per device, and a safety governor that drives
outputs off when host contact is lost.

**`agent/runs` + `agent/llm`** — declarative unattended experiments
with a durable event log (SQLite WAL + fsync'd ndjson mirror), and an
advisory LLM supervisor confined to eight allowlisted tools that
cannot energise anything.

## Verified measurements

Kept here because they are the evidence behind the claims elsewhere in
the docs.

- **Native rates**: mc 4042 sps, mp 4042 sps, mv 1015 sps
- **Profiler**: AA pair, OCV 3.19 V, ESR 3.18 Ω, 10-cycle short
  profile (15 s, 10.6 µAh used)
- **Emulator**: CR2032 profile + QR10x load sweep (100 kΩ → 12 Ω).
  Voltage sag tracks ESR closely — predicted 28.8 mV vs measured 28 mV
  at 1 kΩ; predicted 279 mV vs measured 288 mV at 100 Ω. The cell
  collapses at 12 Ω as a real CR2032 would, and SoC recovery shows the
  new OCV at lower SoC.
- **Multi-profile matrix**: 8 profiles × static sweep + 3 × dynamic IoT
  pattern, saved in `scenarios/saved/`. Captures chemistry-specific
  behaviour (CR2032 collapse, CR123A pulse capability) and LiPo
  temperature dependency — ESR rises ~10× from +20 °C to −10 °C.
- **Remote mode, end to end**: submitted a run, disconnected the host
  mid-flight, reconnected, and replayed exactly the missed event range.
- **LLM supervisor does not gate the run**: a test asserts a 3-second
  run finishes on time against a 30-second model stall.

## Open threads

Ordered roughly by how much they'd change if picked up next.

1. **Hardware interlock for unattended runs.** `KNOWN_LIMITATIONS` N-1
   says plainly that a software deadman cannot guarantee an output
   goes off through a wedged driver. Overnight runs deserve a relay on
   the same timer. Scoped in `ROADMAP.md`.
2. **DP2031 as a source in the scenario harness.** The driver shipped
   in 1.1 but `scenarios/` still only models the load side, so
   cell-charging scenarios aren't expressible yet.
3. **DL3031A LIST timing in Arc Pro high range** (§ F-2). Still
   unresolved; isolating it needs a scope on the trigger line.
4. **Strict mypy.** `check_untyped_defs` is on, `--strict` is not. CI
   has mypy as `continue-on-error`. Mostly mechanical.
5. **Multi-device coordination.** Now partly unblocked — `sim` makes
   the API designable without a second Arc, so what's left is
   validation rather than design. Cross-machine timebase is the
   genuinely hard part and may need a hardware trigger line.

## Design questions currently open

- Should cross-machine recording sync use a designated leader's
  timebase, or admit that host-clock correlation is the honest limit
  and expose the uncertainty in the recording metadata?
- The run engine's chunk rolling is time-based (`chunk_s`). Should it
  also roll on sample count, for runs whose rate varies a lot between
  phases?
- `discovery` currently refuses to claim anything behind a CH340 /
  FTDI / CP210x bridge. Probing (as the QR10x already does by AT
  command) is more useful but slower and more intrusive. Worth making
  the probe opt-in per driver rather than hardcoded?

## Where to find what

| Looking for | Path |
|---|---|
| Tutorial for a new user | `docs/getting_started.md` |
| Every public API element | `docs/api_reference.md` |
| The Arc USB wire protocol | `docs/otii_arc_protocol.md` |
| Architecture / decisions | `ARCHITECTURE.md`, `docs/design.md` |
| Companion driver details | `docs/drivers.md` |
| Running without hardware | `docs/simulation.md` |
| Remote mode + deployment | `docs/remote.md` |
| Unattended runs | `docs/runs.md` |
| MCP server + tools | `docs/mcp.md` |
| What the official Otii client exposes (for parity) | `docs/official_api_inventory.md` |
| AI agent briefing | `docs/AGENTS.md` |
| Deferred features | `ROADMAP.md` |
| Test strategy + inventory | `TEST_PLAN.md` |
| Validation results | `VALIDATION_REPORT.md` |
| Hardware / firmware caps | `KNOWN_LIMITATIONS.md` |
| Release notes | `CHANGELOG.md` |

## Quick resume commands

```bash
cd ~/repos/benchctrl
git log --oneline -20

pytest -m "not hardware" -q     # 956 tests, ~7 min, no hardware
pytest -m hardware -q           # 152 tests, needs the bench on USB
pytest -q                       # both

benchctrl discover              # what's on this bench
benchctrl info                  # smoke test against a live Arc
```

No hardware to hand:

```bash
BENCHCTRL_SIM_DEVICES=otii_arc,eastwood_qr10x,rigol_dl3031a,rigol_dp2031 benchctrl-mcp
benchctrl-agent --simulate
```

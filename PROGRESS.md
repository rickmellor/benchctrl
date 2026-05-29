# OpenSMU build progress

Live status log so anyone (human or AI) picking this up mid-flight knows
exactly where it is. Updated after every milestone; latest entry on top.

## Status snapshot

- **Phase**: v0.8.0 shipped — Battery Toolbox replacement COMPLETE (4 of 4 phases)
- **Tests**: 276 / 276 passing (profiler + emulator tests use mock SMUs; real-hardware validation is a follow-up task)
- **Battery subpackage**: `.profile` (v0.5) + `.calculator` (v0.6) + `.profiler` (v0.7) + `.emulator` (v0.8) — all four phases shipped
- **Outcome**: Qoitech's licensed Battery Toolbox is fully replaced by opensmu on top of the wire vocabulary we already decoded. Profile JSONs interchange bit-for-bit with Otii in both directions.
- **Verified rates**: mc 4042 sps, mp 4042 sps, mv 1015 sps (native)
- **MCP server**: 26 tools (was 23 in v0.3.0); `opensmu-mcp` ready
- **Claude Code skill** at `skills/opensmu/SKILL.md`
- **Output formats**: `.opensmu` / Parquet / CSV / JSON / numpy / pandas
  / matplotlib — all strictly optional, lazy-imported
- **SDK ↔ MCP parity principle** documented in `docs/mcp.md` —
  policy: new SDK features that make sense in a tool-calling context
  ship with an MCP equivalent in the same release
- **Last commit**: v0.4.1 MCP sync
- **Hardware**: Arc Pro on COM6, output off, nothing connected

## v0.2.0 update — what was decoded

Methodical sweep across phase33 + new captures phase34c, phase35-43.

| Item | Status | Wire form |
|---|---|---|
| type=0x64 GET interface | DONE | `[seq][0x64][cmd][0]` |
| Unified Response format | DONE | `[0e 03 99 ff][seq:u32][status:i32][data]` |
| 180-byte ce-frame | DONE | envelope of baseline records (already parsed) |
| type=0x0A POLL | DONE | optional host heartbeat |
| cmd=0x0A SET_POWER_REGULATION | DONE | voltage=0/current=1/inline=10/off=100 |
| set_tx = set_gpo(3, …) | DONE | bits 6/7 in SET_GPO bit pattern |
| type=0x82 write_tx | DONE | `[seq][0x82][0x19][utf-8…]` |
| 0x7E prepare-stop | DONE | flushes packed-stream buffer |
| Channel inventory (cmd=0x8D) | DONE | 256-byte response, structured |
| Calibration via API | DECODED as no-op — Desktop path TBD |
| set_channel_samplerate | DECODED as not-a-wire-command (server concept) |
| Battery emulation | BLOCKED on Battery Toolbox license |

## v0.1.1 update (today)

Decoded the missing "start recording" command via a fresh USB capture of
the Otii server doing a real recording (`33-otii-full-rate.raw` in the
parent project). Three findings:

1. **Per-channel `type=0x78` is the unlock.** `[seq][0x78][wire_id][1]`
   enables that channel for streaming, `…[0]` disables. The 8-byte
   `type=0x7C` cleanup follows the burst.
2. **The 76-byte `69 83 2a ff …` payload v0.1 sent was wrong.** It was a
   misread of the device's *inbound* packed sample frame (which carries
   1 sample per sub-1 channel + 4 packed samples per sub-4 channel,
   arriving every 1 ms). Sending it had no effect.
3. **opensmu's "init step 3"** is the same `type=0x78` command applied
   to channel `0x17` (rx) — that's what triggers baseline streaming on
   open.

Wired through `protocol.encode_channel_enable_for_recording`, updated
`SMU.start_recording` / `stop_recording`, taught `iter_samples` to
auto-detect and unpack the packed-frame format. Tightened test asserts
the device actually delivers ≥6000 mc samples in 2 s now (was ≥5).

## What's done

Everything in the v0.1 scope is built, tested, and documented. The
package is installable (`pip install -e .` works), the CLI is wired up
(`python -m opensmu` and `opensmu` both work), and every documented
public method is exercised end-to-end.

| # | Phase | Status |
|---|---|---|
| 1 | Survey official `otii_tcp_client` API | DONE — see `docs/official_api_inventory.md` |
| 2 | Write design doc | DONE — see `docs/design.md` |
| 3 | Scaffold package | DONE — `pyproject.toml`, `src/opensmu/`, MIT license |
| 4 | Transport + protocol + framing | DONE — `transport.py`, `protocol.py` |
| 5 | Device (SMU) class | DONE — `device.py` |
| 6 | Recording + samples | DONE — `recording.py`, `samples.py`, native `.opensmu` format |
| 7 | TEST_PLAN + pytest suite | DONE — see `TEST_PLAN.md`, `tests/` |
| 8 | Validation against hardware | DONE — see `VALIDATION_REPORT.md` |
| 9 | Documentation | DONE — getting started, API reference, protocol, AGENTS.md, design |
| 10 | Polish + handoff | DONE — this file, CHANGELOG, final commits |

## Resuming in the morning

1. `cd C:\Users\rickm\Desktop\opensmu`
2. `cat PROGRESS.md` (this file)
3. `git log --oneline` to see the trail
4. `python -m pytest tests/ -q` to confirm all 132 tests still green
5. `cat ROADMAP.md` to see what's deferred for v0.2

Use `opensmu info` from the shell as a smoke test against the live
device.

## Where to find what

| Looking for | Path |
|---|---|
| Tutorial for a new user | `docs/getting_started.md` |
| Every public API element | `docs/api_reference.md` |
| The USB wire protocol | `docs/protocol.md` |
| Architecture / decisions | `docs/design.md` |
| What the official client exposes (for parity) | `docs/official_api_inventory.md` |
| AI agent briefing | `docs/AGENTS.md` |
| Deferred features (v0.2+) | `ROADMAP.md` |
| Test inventory | `TEST_PLAN.md` |
| Last validation results | `VALIDATION_REPORT.md` |
| Release notes | `CHANGELOG.md` |

## Notable findings during the build

### 1. Device baseline streams at ~6 Hz, not 1 kHz / 4 kHz

The biggest surprise. Both opensmu and the legacy `arc_direct` library
get only ~6 Hz across all channels after standard session init. The
documented native rates (1 kHz subtype-1, 4 kHz subtype-4) are
capabilities, not the default. A wire-level command unlocks higher
rates — it's deferred to v0.2 (see `ROADMAP.md` § Full-rate streaming).

Tests were calibrated to the actual rate. Recording functionality is
fully working; just slower than the maximum the hardware can deliver.
Useful for any measurement where sub-1-ms detail isn't needed.

### 2. Battery emulation is the biggest scope item for v0.2

The user explicitly flagged battery emulation as the priority for the
next pass — Qoitech's separate Battery Toolbox license can be
obsoleted once it's working. Scoped in `ROADMAP.md`.

### 3. Floating-point boundaries in time-domain slicing

Switched `slice_indices` from `floor(start*rate)` to `ceil(start*rate)`
to fix off-by-one when window bounds land exactly on sample boundaries.
Semantics now: include sample `k` iff `start <= t_k < end`.

## v0.2 priority queue

In order of user-stated interest:

1. **Battery emulation** — decode profile upload + SoC API + battery data
   stream, obsoleting the Battery Toolbox.
2. **Full-rate streaming** — decode the unlock command so high-rate
   channels actually deliver 4 kHz.
3. **Calibration** — capture the calibration flow, expose with
   appropriate safety warnings.
4. **set_channel_samplerate** — re-attempt capture if the Otii server
   bug is fixed in a newer version.

Multi-device coordination + UART log parsing + project save/load are
v0.3 candidates.

## Open questions for tomorrow

- Should `set_supply_battery_emulator()` accept a JSON file path
  directly, or take a parsed `BatteryProfile` dataclass and a
  `BatteryProfile.from_json()` factory? (Tomorrow's design call.)
- Should the full-rate streaming unlock be implicit (always on after
  init) or explicit (`SMU.set_streaming_mode("high")`)?

## Quick resume commands

```powershell
cd C:\Users\rickm\Desktop\opensmu
git log --oneline -20
python -m pytest tests/ -m "not hardware" -q     # hardware-free, <1 s
python -m pytest tests/ -m hardware -q            # requires Arc Pro on COM6, ~35 s
python -m pytest tests/ -q                        # both, 132 tests
opensmu discover
opensmu info
```

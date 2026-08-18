# benchctrl v1.2.0 — validation report

Last refreshed **2026-08-17** against `feat/remote-mode`.

Re-run with `pytest` to refresh. Strategy and coverage targets live in
[`TEST_PLAN.md`](TEST_PLAN.md).

## Summary

| Tier | Result | When |
|---|---|---|
| Hardware-free | **956 passed, 23 skipped, 0 failed** | Executed 2026-08-17 for this report |
| Hardware-marked | 152 passed | Last executed during the 1.1.0 / 1.2.0 build passes — **not re-run for this report** |

That distinction is deliberate. The hardware-free tier below is a live
result from this machine. The hardware tier is carried forward from
the runs recorded in the commits that landed each feature, because no
instruments were attached when this report was refreshed. Treat the
hardware numbers as "last known green", not "green today".

### Hardware-free run detail

```
956 passed, 23 skipped, 152 deselected in 403.42s (0:06:43)
```

- **Platform**: Linux 7.0.0-28-generic, Python 3.12.3
- **Relevant deps**: pyserial 3.5, PyVISA 1.16.2, PyVISA-py 0.8.1, mcp 1.29.0
- **Hardware attached**: none

All 23 skips are environmental, not failures:

| Count | Reason |
|---|---|
| 8 | `numpy` not installed in this venv |
| 6 | `pandas` not installed |
| 3 | `pyarrow` not installed |
| 5 | `matplotlib` not installed |
| 3 | Otii desktop app's bundled profiles not present on this system |

The first four groups are the `benchctrl[science]` optional extras
doing exactly what they should — the lazy-import paths skip cleanly
rather than failing. Install `".[science]"` to clear them.

## What the hardware-free tier actually proves now

This is the part that changed most since the v0.1 report. These tests
no longer run against mocks of benchctrl classes. They run the
production drivers against **device simulators** that speak the real
wire protocol over a pseudo-terminal.

So a green hardware-free run exercises, with nothing monkeypatched:

- `Transport` and the pyserial call path
- the Arc's binary framing, checksums, and resync behaviour
- the timed three-step session-init handshake
- `SET` range validation and genuine negative-status error frames
- baseline streaming and packed sub-1 / sub-4 high-rate framing
- the recording reader thread
- for both Rigols, the **real pyvisa stack** via pyvisa-py's ASRL
  backend

It does not exercise the physics, the analog envelope, or the firmware
defects catalogued in [`KNOWN_LIMITATIONS.md`](KNOWN_LIMITATIONS.md).
Those are what the hardware tier is for.

## Coverage by subsystem

| Subsystem | Coverage |
|---|---|
| `exceptions` | every class, hierarchy and carried fields |
| `channels` | every enum constant, every property, reverse lookup |
| `interfaces` | `SourceMeasurementUnit` Protocol conformance |
| `drivers.otii_arc.protocol` | encode/decode round-trips, every SET command code, GPO bit pattern against capture observations, error/ack discrimination, garbage skipping, truncation |
| `drivers.otii_arc.device` | every setter end-to-end against a simulator, every client-side range check raising before send, channel enable/disable and co-enables, recording context manager and manual start/stop, stream iterator, async error surfacing, deferred stubs |
| `samples` | parsing, `ChannelBuffer` slicing, statistics incl. charge and energy |
| `recording` | full lifecycle, plus file **and stream** codecs proven byte-identical |
| `battery` | profile I/O round-trip, life calculator, profiler, and the 100 Hz emulator loop closed through `OhmicLoad` |
| `drivers.eastwood_qr10x` | AT surface, relay-ladder quantisation, safety limit |
| `drivers.rigol_dl3031a` | SCPI surface, LIST / transient / battery-discharge, 4-step rejection |
| `drivers.rigol_dp2031` | the largest suite — source/measure, protection, IEEE 488.2 status, pairing, Arb timer, analyzer, trigger I/O, memory, block parsers |
| `config` / `session` | precedence rules, per-device modes, and every loud-failure path |
| `discovery` | signature table, confidence levels, no collision with known USB bridges |
| `sim` | pty raw mode, bounded tx queue overrun reporting, end-to-end capture |
| `net` | framing, blob chunking under heartbeat, HMAC auth incl. rejection, codec allowlist, exception marshalling across four hierarchies |
| `agent.runs` | spec validation and hashing, tick ordering, dwell times, durability, replay exactness, interrupted-run handling |
| `agent.llm` | tool allowlist, forward-only phase advance, violation lockout, run-not-gated-by-model |
| `mcp` | registration, coercion, dict returns, safety guards |

## Findings

### Finding 1 — full-rate streaming resolved (was Finding 1 in v0.1)

The v0.1 report concluded the Arc streamed at ~6 Hz and that a
wire-level unlock existed but was undecoded. It was decoded in v0.1.1:
a per-channel `[seq:u32][0x78][wire_id][1]` command. Verified native
rates are **mc 4042 sps, mp 4042 sps, mv 1015 sps**. The ~6 Hz figure
remains correct for the *baseline* stream before `start_recording()`.

### Finding 2 — error frame timing (still current)

Device-side error responses for an out-of-range `set_voltage()` arrive
asynchronously and are queued for the next SET to surface. At baseline
rate this can take 1–2 s. The relevant hardware test polls with a
timeout rather than asserting immediate delivery. The simulator
reproduces this ordering, so the async path is now covered
hardware-free too.

### Finding 3 — `start_recording()` does not flush the inbound buffer

**Found by the new simulator tests** (KNOWN_LIMITATIONS A-3). A
recording's first samples can predate the call that started it.
Documented rather than fixed: a blind flush would also discard
legitimate in-flight samples.

### Finding 4 — `read_chunk` blocks up to 0.5 s

`Transport.read_chunk` blocks in `serial.read(8192)` until the buffer
fills or the timeout expires, so a running recording reports no
samples for up to half a second (KNOWN_LIMITATIONS A-4). This bounds
live progress reporting. Not changed, because altering the read
strategy touches timing on every recording's hot path.

### Finding 5 — run spec hashing was float-sensitive

`chunk_s=60` and `chunk_s=60.0` compare equal but serialise
differently, so a spec's content hash changed across a JSON round
trip — which would have broken the guarantee that a result traces to
the spec that produced it. Numeric fields are now coerced at
construction. Caught during 1.2 testing; fixed and covered.

### Finding 6 — two platform constraints shaped the remote design

- Python 3.12+ resolves `runtime_checkable` `Protocol` members with
  `inspect.getattr_static`, which does not invoke `__getattr__`. A
  purely dynamic proxy can therefore never satisfy
  `isinstance(x, SourceMeasurementUnit)`. `RemoteSMU` declares the
  twelve contract methods explicitly.
- `QR10xTimeoutError` inherits both `RuntimeError` and `TimeoutError`
  (an `OSError`), whose C layouts are incompatible, so
  `cls.__new__(cls)` is rejected outright. Exception rebuilding is a
  three-strategy cascade.

## Remote-mode validation

Performed during the 1.2 build pass, over the wire against a real
agent: a run was submitted, the host was disconnected mid-flight,
reconnected, and the missed event range replayed exactly. Not
re-executed for this report.

## How to reproduce

```bash
cd ~/repos/benchctrl
pytest -m "not hardware" -q      # 956 tests, ~7 min, no hardware
pytest -m hardware -q            # 152 tests, needs the bench on USB
pytest -q                        # both
```

To clear the 23 environmental skips:

```bash
pip install -e ".[dev,mcp,bench-visa,science]"
```

A single test in isolation:

```bash
pytest tests/test_run_engine.py::test_spec_round_trips_and_hashes -v
```

## Open items

- **Re-run the hardware tier and refresh the numbers above.** They are
  carried forward, not measured today.
- Add a scenario tier once the DP2031 works as a source in
  `scenarios/` (see `ROADMAP.md`).
- Add a multi-device run when a second Arc is available. The API can
  now be prototyped against two simulators first.
- Run the MCP suite against a live remote agent as a standing check
  that the session seam stays transparent.

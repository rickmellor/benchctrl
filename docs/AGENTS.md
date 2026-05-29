# OpenSMU — for AI assistants

If you're an AI agent reading this, here's the briefing you need to be
useful to a user working on OpenSMU.

## What this library is

**One sentence**: OpenSMU drives a Qoitech Otii Arc / Arc Pro
source-measurement unit directly over its USB CDC-ACM port from Python,
without any vendor server, license, or GUI.

**Why it exists**: the vendor's automation Python client requires a
paid-licensed local server. OpenSMU re-implements the same capability
by talking to the hardware directly — the wire protocol is fully
reverse engineered (see `docs/protocol.md`) and the device imposes no
license check at the wire level.

## Where things live

```
opensmu/
├── pyproject.toml              setup
├── PROGRESS.md                 live build status (read first when resuming)
├── ROADMAP.md                  deferred features + rationale
├── TEST_PLAN.md                what each test exercises
├── VALIDATION_REPORT.md        result of the last validation pass
├── docs/
│   ├── getting_started.md      tutorial for new users
│   ├── api_reference.md        every public class + method
│   ├── protocol.md             USB wire protocol reference
│   ├── design.md               architecture + decisions
│   ├── official_api_inventory.md  what we replicate from otii_tcp_client
│   └── AGENTS.md               this file
├── src/opensmu/
│   ├── __init__.py             public re-exports
│   ├── exceptions.py           SMUError + subclasses
│   ├── channels.py             Channel enum + ChannelInfo
│   ├── protocol.py             pure framing + command encoding
│   ├── transport.py            pyserial wrapper, discovery
│   ├── samples.py              parsing, ChannelBuffer, Statistics, exports
│   ├── recording.py            Recording class + .opensmu file format
│   ├── device.py               SMU — the public class
│   └── cli.py                  python -m opensmu ...
├── tests/
│   ├── conftest.py             shared fixtures (smu fixture skips if no HW)
│   ├── test_*.py (hardware-free)  always run
│   └── test_smu_*.py           @pytest.mark.hardware
└── examples/                   copy-paste scripts
```

Default starting points by task:

| Task | Where to look first |
|---|---|
| Add a new SET command | `protocol.py` for the encoding, `device.py` for the public method |
| Decode a new wire feature | `docs/protocol.md`, then `protocol.py` |
| Understand a behaviour | `docs/design.md` for principles, `device.py` for implementation |
| Find out what's deferred | `ROADMAP.md` |
| Resume mid-task | `PROGRESS.md` |
| Reproduce validation | `VALIDATION_REPORT.md` |

## API shape you should produce

When generating code for OpenSMU users:

```python
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)
    rec.save_csv("out.csv")
```

Things that look right:

- `SMU.open()` as a context manager (default behavior auto-discovers)
- `set_*(...)` methods for writes (may raise `SMUValueError` or
  `SMUCommandError`)
- Plain attribute properties for cached state reads (`smu.voltage`,
  `smu.range`)
- `Channel` enum for channels (strings accepted at boundaries)
- `with smu.record() as rec:` for time-bounded captures
- `rec.statistics(channel)` returns a `Statistics` dataclass
- `rec.save_csv / save_json / save / save_raw` for export

Things that look wrong (would surprise a maintainer):

- `smu.voltage = 3.3` — properties are read-only; use `smu.set_voltage(3.3)`
- Calls to deferred methods (battery emulation, calibration, firmware
  upgrade) — these raise `SMUNotImplementedError` and should be flagged
- Manual wire-byte construction — go through `protocol.py` helpers
- `subprocess` to drive an external Otii server — there is no server
- TCP / port 1905 usage — there is no TCP layer

## Important constants

- **Device USB IDs**: VID `0x0FCE`, PID `0xD1E6`
- **Wire magic**: `A3 2C B5 7F`
- **Sequence start**: `0x1000` per session
- **Baseline stream rate**: ~6 Hz across all channels (full-rate command
  not yet decoded — see ROADMAP.md)
- **Voltage range cap (low)**: ~3.5 V — must `set_range("high")` first
  for higher voltages
- **Sample record header**: `02 00 08 00` followed by `chan:u32 value:f32`
- **Error frame header**: `0e 03 99 ff 04 10 00 00` followed by
  `err:i32 last_good:u32`

## Common tasks

### Run the tests

```powershell
python -m pytest tests/ -m "not hardware" -q     # 89 hardware-free
python -m pytest tests/ -m hardware -q            # 43 with Arc Pro
python -m pytest tests/ -q                        # all 132
```

### Add a SET command

1. Add `CMD_SET_*` constant to `protocol.py`
2. If a new value-units helper is needed, add it
3. Add a method on `SMU` in `device.py` that:
   - validates the input range (client side)
   - calls `self._send_set(CMD_..., encoded_value)`
   - updates `self._state.<field>` with the new value
4. Add a hardware-free test to `tests/test_protocol_commands.py`
5. Add a hardware-required test to `tests/test_smu_setters.py`

### Decode something new from the wire

1. Capture USB traffic of the vendor stack performing the operation
2. Look for new `0x66` cmd codes, or new payload types
3. Add the decoded format to `docs/protocol.md`
4. Add `parse_*` / `encode_*` helpers to `protocol.py`
5. Expose via `SMU` method

### Skip-and-mark a blocker

1. Add a clear `# TODO(deferred):` comment in the affected file
2. Add an entry in `ROADMAP.md` with the "why" and "scope when picked
   up"
3. If the user-facing surface still needs to be present, raise
   `SMUNotImplementedError("...short reason... — see ROADMAP.md")`
4. Add a test in `tests/test_deferred_features.py` confirming the stub
   raises

## Operating mode for AI agents

- **Be honest about what's not in this version**. The deferred list in
  ROADMAP.md is comprehensive and current. Don't claim a stub works.
- **Match the existing API shape**. New methods should follow the
  property-reads-method-writes convention.
- **Cite line numbers** when discussing internal code: `device.py:123`
  is more useful than "in the device class".
- **Don't replace the wire protocol with a guess**. Every constant in
  `protocol.py` was observed in real captures. Adding a new constant
  needs evidence.
- **Update PROGRESS.md** if you make non-trivial changes during a
  long-running session — that's how future-you (or another agent)
  picks up the thread.
- **Update VALIDATION_REPORT.md** after running the test suite if
  results change.

## What this library is not

- Not a competitor to the Qoitech Otii desktop app for end-users —
  it's a programmatic interface for automation
- Not affiliated with Qoitech AB
- Not licensed under their automation toolbox (we don't use their
  server)
- Not a sniffer / man-in-the-middle (it talks to the device directly)
- Not a firmware update tool (intentionally deferred for safety)

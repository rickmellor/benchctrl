# Contributing to OpenSMU

Thanks for considering a contribution. This document covers everything
you need to set up, test, and ship a change.

If you're an AI agent picking up the codebase rather than a human,
the more efficient briefing is [`docs/AGENTS.md`](docs/AGENTS.md) +
[`skills/opensmu/SKILL.md`](skills/opensmu/SKILL.md). Come back here
for conventions and PR mechanics.

## Development setup

```bash
git clone https://github.com/opensmu/opensmu
cd opensmu
pip install -e ".[dev,mcp,bench-visa,science]"
```

Python 3.9+ is supported; CI runs 3.9, 3.10, 3.11, 3.12, 3.13.

The extras you actually need depend on what you're touching:

| Working on... | Install |
|---|---|
| SMU / battery / QR10x bench driver | `pip install -e ".[dev]"` |
| MCP server | `pip install -e ".[dev,mcp]"` |
| Rigol DL3031A driver / `bench-visa` | `pip install -e ".[dev,bench-visa]"` |
| Parquet / pandas / matplotlib output paths | `pip install -e ".[dev,science]"` |
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
pytest -m "not hardware" -q       # ~3 minutes, no device needed (282 tests)
pytest -m hardware -q              # requires Arc Pro + companion instruments
pytest -q                          # both (~5 minutes with hardware)
```

Hardware-marked tests need:

- An **Arc Pro** on USB (`COM*` / `/dev/ttyACM*` / `/dev/cu.usbmodem*`)
- For DL3031A tests: a **Rigol DL3031A** on USB-TMC
- For QR10x tests: an **Eastwood QR10x** on a USB-Serial port

Tests skip cleanly with a useful message if the hardware isn't
present, so partial setups don't fail the suite — you just get fewer
green dots.

If you're adding a new hardware test, follow the existing pattern:

```python
@pytest.mark.hardware
def test_hardware_my_thing():
    pytest.importorskip("pyvisa")
    resource = os.environ.get("OPENSMU_DL3031A_RESOURCE")
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
ruff check src tests validation
mypy src
```

CI runs both. `ruff` is configured for E/F/W/I/B/UP/N/SIM with
`line-length = 100`. `mypy` is `check_untyped_defs = true` but not
strict — strict mode is a deferred item in `ROADMAP.md`.

## Conventions

OpenSMU has a few load-bearing principles you'll find referenced
throughout the codebase and CHANGELOG. New code is expected to follow
them.

### 1. SDK ↔ MCP parity

Every public SDK method has a matching MCP tool. When you add a
public method to `SMU` / `Emulator` / `QR10x` / `RigolDL3031A`, add
the matching `@mcp.tool()` in `src/opensmu/mcp.py` in the same PR.

The MCP tool wraps the SDK method, coerces arguments to JSON-friendly
types where needed, and returns a dict (never a custom dataclass).
For SDK methods that return numbers, the MCP tool wraps in
`{"<name>": value}`; for SDK methods that return a dict / dataclass,
the MCP tool returns the dict (calling `__dict__` or `to_dict()` if
appropriate).

Granular wrappers behind a convenience helper (e.g. the individual
`list_set_*` methods behind `program_list`) can be SDK-only —
document them in `docs/bench.md` as such. The opposite isn't allowed:
no MCP tool without a corresponding SDK method.

### 2. Every public method has a test

Mock-based test for the SDK surface; hardware-marked test for the
end-to-end behavior. The mock and the hardware test should exercise
the same wire format / SCPI string. We've been bitten by mocks
silently diverging from real hardware (see v0.9.2 emulator mock fix);
new mocks should mirror the real surface's method names exactly so a
rename would break tests.

### 3. Layers do not skip

```
Application / examples / CLI
    ↓
SMU + Recording (public API)
    ↓
samples / protocol (pure)
    ↓
transport (pyserial I/O)
```

`Recording` never reaches into `Transport`. `SMU` never imports
`pyserial` directly. Same layering applies in `battery/`, `bench/`,
and the validation harness. If you find yourself wanting to skip a
layer, that's a signal something's at the wrong height in the stack.

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
`Harness`). The driver should *reject* the known-bad case at the API
boundary with a clear error pointing to the workaround.

Example (`rigol_dl3031a.py`):

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
opensmu/
├── README.md, ARCHITECTURE.md          repo entry points
├── CHANGELOG.md, KNOWN_LIMITATIONS.md   what changed / what's broken
├── CONTRIBUTING.md (this file)
├── ROADMAP.md                          deferred features
├── PROGRESS.md                         live build snapshot
├── pyproject.toml                      package config
├── src/opensmu/
│   ├── device.py, transport.py,        SMU core
│   │   protocol.py, samples.py,
│   │   recording.py, channels.py
│   ├── exceptions.py
│   ├── battery/                        Battery Toolbox replacement
│   │   ├── profile.py                  profile JSON I/O
│   │   ├── life_calculator.py          predicted runtime
│   │   ├── profiler.py                 generate fresh profile
│   │   └── emulator.py                 act as a battery
│   ├── bench/                          companion instruments
│   │   ├── qr10x.py                    Eastwood programmable resistor
│   │   └── rigol_dl3031a.py            Rigol electronic load
│   ├── mcp.py                          MCP server (93 tools)
│   └── cli.py                          opensmu CLI entry
├── tests/                              hw-free + hardware-marked
├── docs/                               topical docs
├── validation/
│   ├── run_validation.py               scenario harness
│   ├── README.md                       harness docs + results
│   └── scenarios/                      saved captures (JSON / CSV / PNG)
├── skills/opensmu/SKILL.md             Claude Code skill briefing
└── .github/                            PR + issue templates, CI workflow
```

When you're hunting for something:

- **"Where does X live?"** — search `src/opensmu/` first, then
  `validation/` if it's harness-related.
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

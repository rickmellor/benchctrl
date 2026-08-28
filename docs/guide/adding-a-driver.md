# Adding a driver

Bringing a new instrument into benchctrl means writing one self-contained
package plus a handful of one-line registrations. Drivers are peers — there is
no base class to inherit and no privileged instrument — so nothing you write
here changes anything that already works.

This page is the practical path. [`CONTRIBUTING.md`](../../CONTRIBUTING.md) has
the full conventions; [`ARCHITECTURE.md`](../../ARCHITECTURE.md) has the layer
model.

## What you are building

```
src/benchctrl/drivers/<vendor_model>/
    __init__.py      re-exports; says which Protocol it implements, or why none
    driver.py        the public class, dataclasses, exceptions
    <transport>.py   the wire layer, if it is not just pyserial or pyvisa
    mcp_tools.py     one tool per public method, plus _TOOLS
```

Plus a simulator at `src/benchctrl/sim/<model>.py`, and about eight
registration lines listed at the end.

## Step 0: measure the device before you write a parser

This is the step that is tempting to skip and expensive to skip.

Talk to the instrument by hand — a serial terminal, a VISA session, a
five-line script — and **save the transcripts as test fixtures** with the
firmware version and the capture date. Then write the parser against those
bytes.

The reason is specific: if you write the driver and the simulator from the same
reading of the manual, and the reading is wrong, **the simulator agrees with the
driver and the test suite passes.** A fixture captured from the device is the
only thing in the loop that can disagree with you.

What that has actually caught on this bench, in one device:

- A table's column layout was not what the manual showed — and the manual did
  not show that output at all.
- The settling time everyone quoted was measuring the round trip, not the
  contactor. The real budget had to be derived from a *configurable* per-outlet
  delay, so a hardcoded retry window would have flaked.
- There were **three** error shapes, not the two documented; keying on either
  sentinel alone missed a case.
- Line endings were not uniform within one device — some rows separated by
  `\n\r` — so `splitlines()` mis-parsed them. Parse bytes.
- Reading "until a blank line" truncated a 30-line error dump and desynced the
  session. Read until the prompt.

None of those are exotic. They are what a device does when the manual was
written by someone who was not looking at this firmware.

## Step 1: the public class

Four conventions matter, and each exists because of a specific failure.

### Vocabulary follows physics, not the register

Name the method after what happens on the wire, not what bit gets set. The
control-line driver says `asserted`/`released` rather than `high`/`low`, because
reset lines are active-low and "set the line high" is ambiguous exactly where a
mistake holds a processor in reset indefinitely.

Take the ambiguity seriously in return types too. `line_is_asserted()` returns
`None` for an input, not `False`, because *"nobody is pulling this line down"*
and *"this pin is an input, so the question does not apply"* are different facts
— and only the second means the caller is asking the wrong object. That `None`
is the one defect real hardware found in that driver.

### Method names decide whether the writer claim applies

`agent/dispatch.py` derives the mutator set **purely from name prefixes**, with
no driver-declared override. So a method named `outlet_on()` or `assert_line()`
would be **remotely callable without the writer claim** — a state change that
skipped the gate every other route enforces.

Every method that moves hardware must start with `set_`, `reset`, `clear_` or
`trigger`. And write the test that proves it:

```python
def test_every_mutator_is_recognised_as_one():
    with MyDriver.open(...) as dev:                 # a simulator will do
        surface = introspect(dev, "myvendor_mymodel")
    assert "set_relay_state" in surface.mutators
    assert "relay_states" in surface.methods          # remotely callable...
    assert "relay_states" not in surface.mutators     # ...but needs no claim
```

`introspect` takes the **instance** and its device key, not the class — it reads
the bound methods. Assert both directions: `in surface.methods` catches a method
that is not reachable at all, which `not in surface.mutators` would pass
silently.

Do not add your device's noun to `_MUTATOR_PREFIXES` to make a nicer name work.
Adding `outlet_` would also capture `outlet_state()`, making a *read* require a
writer claim.

### Refuse what the device should not be asked

Safety rules that are specific to your instrument belong in your driver,
enforced rather than documented:

- **Allowlists, not denylists**, for anything that switches. A required
  keyword-only argument with no "all" default, so a config typo fails closed.
- **No aggregate targeting.** If the command set has a "do this to everything"
  form, no method surface should reach it — and assert the *rendered command*
  against a regex before the write, so the guarantee survives a refactor.
- **Restrict drive modes** where a mode is unsafe. Enforce it: clear the bit on
  every call, refuse a pin found in the wrong mode, and accept no parameter that
  would re-enable it. A test asserting the parameter's absence keeps a
  well-meaning "make it configurable" change from taking the property away.
- **Verify by read-back** where the device acknowledges nothing. Several
  instruments here have *no* error reply at all — an unknown command, an
  out-of-range argument and a write-only command are byte-identical on the wire:
  nothing comes back. Telling an operator a DUT is de-powered when its contactor
  never moved is worse than failing loudly.
- **Reject known-bad cases at the API boundary**, naming the workaround:

  ```python
  if steps == 4:
      raise RigolDLValueError(
          "LIST STEP=4 is a firmware bug — no steps fire. "
          "Use 3 or 5 steps with appropriate count instead."
      )
  ```

### Errors propagate; silent fallbacks are bugs

`except Exception: pass` is almost always wrong. A past review found two
compound silent failures that together made hardware faults look like a working
subsystem producing zero current.

Derive from the `BenchError` hierarchy and *also* from the matching builtin, so
callers can catch either:

```python
class MyDeviceError(BenchError): ...
class MyDeviceValueError(MyDeviceError, ValueError): ...
class MyDeviceTimeoutError(MyDeviceError, TimeoutError): ...
```

That dual inheritance is not decoration — it is what lets an exception keep its
meaning after crossing the network, where marshalling walks the MRO.

The one acceptable quiet path is teardown cleanup, and even there, log loudly:

```python
try:
    self.set_input(False)
except Exception as e:
    log.warning("set_input(False) failed during teardown: %s", e)
```

### Where two problems share a symptom, say which

If two very different causes produce the same failure, the error must
distinguish them. One device here permits a single command session across all
its transports, and a second connection fails **after** a successful login —
indistinguishable from bad credentials unless the driver says so. Every
occurrence would otherwise be misdiagnosed.

## Step 2: Protocol, or deliberately none

If your instrument is a source-measure unit, implement
`benchctrl.interfaces.SourceMeasurementUnit`. The battery emulator, the
profiler, the life calculator and the run engine all depend on that Protocol and
never name a concrete driver, so conforming gets you all of them for free. That
is the extension point that matters.

For anything else — a load, a supply, a meter, a switch, a control line —
**define no Protocol.** A Protocol lands when the *second* concrete instance
does, and one implementation is not evidence for the right abstraction.

Say so in the docstring rather than leaving it implied:

```python
"""Silicon Labs CP2112 — open-drain control lines over HID.

Implements **no Protocol** from benchctrl.interfaces, and that is deliberate
rather than an omission. CONTRIBUTING.md convention 3 defines a Protocol when
the *second* instance of a shape lands; this is the bench's first digital-
control device. A ``ControlLine`` Protocol generalised from one sample would
bake in CP2112 specifics — whole-port configuration registers, a USB round trip
per transition — that a relay board would not share.
"""
```

## Step 3: the simulator

Not optional in practice: it is what makes your driver testable in CI, on a
laptop, and by everyone who does not own your instrument.

**Speak the real wire protocol.** Subclass `SimDevice` and answer bytes over a
pty, so the production driver connects unmodified and the transport, framing,
handshake and reader threads all stay in the path. Use canned output taken from
your Step 0 fixtures, not output you composed.

Two devices here substitute at the **link seam** instead, because a pty cannot
carry HID ioctls. That is fine, and the docstring should say what it costs: the
ioctl encoding is then untested by the simulator and needs its own test against
independently derived request numbers.

**Model the behaviours that bite**, not the happy path:

- writes to a pin configured as an input are silently ignored
- an open-drain pin cannot force a net high against an external pull-down
- a floating input latches 1
- a device reset reverts every pin to an input
- a mode command that is one-way stays one-way — one simulator models a menu
  trap by refusing to answer normal commands afterwards, so a test can prove the
  driver never gets there

Make time injectable. An idle-logout test needs `force_logout()`, not
`sleep(180)`.

And be explicit in the docstring about what a green suite does **not** prove:
that any voltage moved on any wire. That is what hardware-marked tests and a
multimeter are for.

## Step 4: MCP tools, in the same commit

Convention 1: **every public SDK method has a matching MCP tool.** Not in a
follow-up — the failure mode is a capability that works in Python and is
invisible to an agent.

```python
def mydev_set_thing(index: int, on: bool) -> dict:
    """One sentence on what this does to the DUT.

    If it latches, name the release call here. This docstring is what a model
    reads before calling, so it is part of the safety interface.
    """
    return {"state": _get_dev().set_thing(index, on)}

_TOOLS = (mydev_open, mydev_close, mydev_set_thing, ...)

def register_mcp_tools(mcp) -> None:
    for fn in _TOOLS:
        mcp.tool()(fn)
```

Rules:

- Reach the device through the module's `_get_dev()` singleton, which populates
  via `session.resolve()`. **Never open a driver directly in a tool** — that
  bypasses the seam, and the tool then works locally but not remote or
  simulated.
- Return a dict, never a dataclass. Numbers get wrapped: `{"volts": 3.3}`.
- Copy the mechanical parity test from `tests/test_mcp.py`, with any exemptions
  named and justified in the test. It earns its keep — on one driver the public
  surface and the tool surface were written days apart, and the test is what
  found the gap.
- If your device has a command allowlist, copy the **converse** test too:
  `test_every_whitelisted_command_is_reachable_from_the_sdk`. Parity guards
  SDK → MCP; this guards wire → SDK, and it caught a command that was
  whitelisted, given a hardware-measured reply width, modelled by the simulator
  and written into a docs table while **no public method could send it**. A
  capability documented as present and absent at once is not something a grep
  finds.

Granular internals behind a convenience helper may be SDK-only — document them
as such. The reverse is never allowed: no MCP tool without an SDK method.

Because the CLI is generated by reflection over `_TOOLS`, registering your tools
also gives you `benchctrl <group> <command>` with no CLI edit.

### Classify every tool

Add each tool to `TOOL_TIERS` in
[`cli_tiers.py`](../../src/benchctrl/cli_tiers.py). A test fails the build if
anything ships unclassified.

| Tier | Gate | Use for |
|---|---|---|
| `READ` | none | measurements, state, identity |
| `TIER2` | none | setpoints on a de-energised output, ranges — **and de-energising** |
| `TIER1` | `--yes` | energising, sinking, closing a contact, driving a line into a DUT |
| `TIER1_ENV` | `--yes` + a named variable | consequence outlives the command: switching mains, arming a watchdog |

Two rules to get right. **De-energising is never gated harder than energising** —
a test asserts it, because if reaching safety needed more ceremony than leaving
it, an operator fighting a live output would have a gate in the way. And do not
key the tier off `dispatch.is_mutator()`: that predicate files a raw SCPI
passthrough under "safe" and a snapshot read under "dangerous".

If a parameter is an **operator attestation about the bench** rather than
something a caller can establish — an allowlist, a "yes, this pin really is a
reset line" flag, a `verify=False` — do not expose it as a CLI flag or a tool
argument. Say so in `--help` under *"Deliberately not exposed"*, with the reason.
An operator who cannot find a flag deserves to know it was withheld on purpose
rather than forgotten.

## Step 5: registration

Miss one of these and the failure is usually silent rather than loud. Device key
convention is `<vendor>_<model>`.

| File | What to add | If you forget |
|---|---|---|
| `config.py` → `DEVICE_KEYS` | your key | `Config.from_dict` **silently drops** the device |
| `agent/registry.py` | an opener closure, lazily importing your driver | `BenchValueError: no opener for device key` |
| `sim/factories.py` | `make_<model>()` + a `FACTORIES` entry | `--sim` cannot bind it |
| `mcp.py` | import + `register_mcp_tools(mcp)` | no tools, no CLI group |
| `cli_tiers.py` | one line per tool | the build fails (by design) |
| `net/codec.py` | your dataclass names | dataclasses degrade to bare dicts over the wire |
| `net/errors.py` | your exception module | exceptions degrade along the MRO |
| `discovery.py` | a `SIGNATURES` or `PROBE_LABELS` entry | not listed by `discover()` |
| `dashboards/fui/view.py` | an `INSTRUMENTS` row | absent from the status display |
| `deploy/udev/` | a rule, if the node needs one | see below |

Tests that fail mechanically on a miss: `tests/test_session_config.py` iterates
`DEVICE_KEYS`, and `tests/test_mcp.py` holds the parity tests.

**Identity beats probing.** If your device has its own VID/PID, add a
`SIGNATURES` entry and let identity come from the descriptor. Only use a
`PROBE_LABELS` probe when it does not — one instrument here hides behind a
generic USB-serial bridge and can only be found by writing to it. If you do add
a probe, **probe by writing, never by querying**, and remember that another
device may be behind the same bridge at a different baud rate.

### udev, if it needs it

Scope every rule to a VID/PID. Do not write `SUBSYSTEM=="hidraw"` to save a line
— on a typical machine that also matches the attached keyboard, which makes it a
keylogging surface rather than a bench rule.

Say in the rule's comment what breaks without it, because the symptoms differ
wildly: a missing USB-TMC rule makes an instrument **invisible** (`discover()`
returns `[]`, reading exactly like a bad cable), while a missing HID rule makes
`open()` raise `EACCES` and name the file. The second is a much better failure,
and it is worth catching the error and naming the rules file yourself.

## Step 6: tests

Two tiers, both required:

- **Hardware-free** — every public method, against your simulator. This runs in
  CI on Python 3.9 through 3.13.
- **Hardware-marked** — the same wire format, end to end, against the real
  instrument. `pytest -m hardware -k <model>`.

Prefer simulators over mocks. Mocks here have silently diverged from real
hardware before. Where a mock genuinely is the right tool — you need to assert a
particular call was made — mirror the real method names exactly, so a rename
breaks the test.

The tests that pay for themselves fastest:

- the `dispatch.introspect()` mutator test from Step 1
- MCP parity, and its converse if you have a command allowlist
- allowlist refusals on every mutator
- `bool` rejected **before** `int` for an index, or `True` silently becomes 1
- the rendered-command regex, and an assertion that no emitted command ever
  contains an aggregate target
- read-back catching a *lying* device — build the fixture where the device
  accepts the command and moves nothing
- `close()` idempotence, and `__exit__` not changing device state

Two habits worth borrowing from this repo's history:

**Assert the test count, not just the exit code.** A run that collected nothing
exits 0, which reads as a pass.

**Build the input only one guard rejects.** If a mutation to your driver survives
the suite, the usual cause is that two different checks both reject your fixture,
so neither is actually tested.

## Step 7: document

- `docs/drivers.md` — a section for your instrument: quick start, safety,
  method surface, MCP tools, and what is out of scope
- `KNOWN_LIMITATIONS.md` — every firmware quirk you found, with **what you
  measured**. The driver should reject the known-bad case at the boundary and
  point at the entry
- `CHANGELOG.md` under `[Unreleased]`, including "Discovered" and "Known issues"
  where they apply
- `docs/guide/equipment-matrix.md` — add the row, and say what your instrument
  is *good for* relative to the others

That last one matters more than it looks. The single most useful thing in this
guide is the crossover between two loads — one is correct below 1 mA, the other
above — and it exists only because both were run through the same matrix and the
numbers were written down. Neither instrument errors in the other's regime; the
wrong one just returns a plausible number, off by a factor of 27.

## Layers do not skip

```
Application / examples / CLI / scenarios
    ↓
Vendor-agnostic subsystems (battery, scenarios, mcp, agent/runs)
    ↓
session.resolve()  —  local | remote | sim
    ↓
SourceMeasurementUnit Protocol + framework primitives
    ↓
Driver public API
    ↓
Driver-internal modules (channels / protocol / transport)
    ↓
pyserial / pyvisa — or a sim pty, or a net proxy
```

`Recording` never reaches into a driver's transport. The battery emulator never
imports a concrete driver class. The MCP orchestrator never imports `pyserial`.
If you find yourself wanting to skip a layer, something is at the wrong height in
the stack.

## Dependencies

Keep them per-driver, in an extra, and prefer none.

`pyserial` is the only base dependency. Two drivers here need **nothing beyond
the standard library** — they talk USB HID directly — and that is precisely why
they work on a small bench host with no compiler and no package manager. If your
driver can be stdlib-only, make it stdlib-only; it widens where the whole system
can run.

A driver whose extras are missing must contribute nothing rather than break the
import.

## Checklist

- [ ] Hardware transcripts saved as fixtures, with firmware version and date
- [ ] Public class: physics vocabulary, mutator-prefixed writes, enforced safety
- [ ] Exceptions derive from `BenchError` **and** the matching builtin
- [ ] Protocol implemented, or non-conformance declared in the docstring
- [ ] Simulator speaking the real wire protocol, modelling the awkward cases
- [ ] MCP tools with `_TOOLS` + `register_mcp_tools`, same commit
- [ ] Every tool classified in `cli_tiers.py`
- [ ] All ten registration points
- [ ] `dispatch.introspect()` mutator test
- [ ] MCP parity test (and its converse, if there is an allowlist)
- [ ] Hardware-free tests green; hardware tests run at least once
- [ ] udev rule if needed, VID/PID-scoped, with the symptom in the comment
- [ ] `drivers.md`, `KNOWN_LIMITATIONS.md`, `CHANGELOG.md`, equipment matrix

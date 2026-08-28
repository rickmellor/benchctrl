# Theory of operation

This page explains how benchctrl is built and why. It is worth reading before
you trust it with hardware, because most of the design decisions are about
failure — what happens when a cable falls out, a host crashes, or two things
try to drive one instrument at once.

## The shape of the system

Four layers. Each depends only on the one below it.

```
┌──────────────────────────────────────────────────────────────────┐
│  Entry points     Python API  ·  CLI  ·  MCP server              │
│                   Same functions, three front doors.             │
├──────────────────────────────────────────────────────────────────┤
│  Subsystems       battery emulator · profiler · life calculator  │
│                   run engine · scenario harness · analysis       │
│                   Depend on a Protocol, never on a product.      │
├──────────────────────────────────────────────────────────────────┤
│  Resolution       session.resolve(device_key) →                  │
│                     local driver | remote proxy | simulator      │
│                   Decided per device. Callers cannot tell.       │
├──────────────────────────────────────────────────────────────────┤
│  Drivers          one package per instrument, all peers          │
│                   otii_arc · eastwood_qr10x · rigol_dl3031a      │
│                   rigol_dp2031 · siglent_sdm4065a · ontrak_adu218│
│                   cyberpower_pdu41002 · silabs_cp2112            │
└──────────────────────────────────────────────────────────────────┘
                              │
                            USB / serial / network
                              ▼
                          instruments → your DUT
```

Two seams do the heavy lifting, and everything else follows from them.

## Seam 1: drivers are peers

There is no privileged instrument. The source-measure unit that started the
project lives in `benchctrl.drivers.otii_arc`, structurally identical to the
$15 USB breakout in `benchctrl.drivers.silabs_cp2112`. Each driver package
owns its device completely: the wire protocol, the safety rules that are
specific to it, its simulator, and its own tool registration.

Why it matters to you: **you install only what you have.** Drivers are
independent, dependencies are per-driver extras, and a driver whose extras are
missing contributes nothing rather than breaking the import. A bench with one
instrument does not carry the weight of seven others.

### Subsystems depend on a Protocol, not a product

The battery emulator does not know what an Otii Arc is. It requires an object
satisfying `SourceMeasurementUnit` — a Protocol describing the source-side
setters, the measurement calls and the identity calls it needs. Anything
conforming slots in.

That is why "bring your own SMU" is real rather than aspirational, and it is the
extension point that matters most if your bench differs from ours. See
[Adding a driver](adding-a-driver.md).

Note the honest limit: **today only the source-measure unit driver implements
that Protocol.** The load, the supply and the meter have surfaces that do not
fit an SMU shape, and no `Switch` or `ControlLine` Protocol has been defined
yet, because two devices are not enough evidence for the right abstraction. A
Protocol invented for one implementation is a guess.

## Seam 2: local, remote and simulated are the same call

`session.resolve()` takes a device key and returns something you can drive. It
resolves independently per device to one of three things:

| Mode | What you get | When |
|---|---|---|
| **local** | the real driver, on this machine | the instrument is plugged in here |
| **remote** | a proxy to a bench host over TCP | the instrument is in the lab |
| **sim** | a wire-protocol simulator | no instrument at all |

Nothing above this seam can tell which it got. That single property is what
gives you:

- **Tests that mean something.** A test written against a simulator runs
  unchanged against hardware, so the simulator is not a parallel universe that
  drifts out of sync.
- **A split bench.** Resolution is per *device*, so the source-measure unit can
  be in the lab while the supply is plugged into your laptop, driven by one
  script that says nothing about either.
- **Development anywhere.** The whole stack runs with no instruments.

**With nothing configured, everything resolves local.** That is deliberate: the
default is the least surprising behaviour, and configuration is opt-in. It also
means an empty config file cannot silently become an override — see
[Local and remote mode](local-vs-remote.md) for the precedence rules.

### The simulators are not mocks

This distinction decides how much a passing test is worth.

A simulator speaks its instrument's **real wire protocol** over a
pseudo-terminal, and the production driver connects to it unmodified. The
transport, the binary framing, the session handshake, the reader thread — all
real code, nothing monkeypatched. For the instruments that talk USB-TMC, the
real VISA stack is in the path too.

So the simulators exercise the code that actually ships. What they cannot prove
is anything about the *device*: a simulator models the hardware's behaviour
because the hardware was measured, so asserting that behaviour against the
simulator proves only that the model is self-consistent. That is why the test
suite is split, and why hardware-marked tests exist as a separate tier.

Simulator waveforms are analytically known, which means tests assert exact
statistics rather than "a number arrived".

## Where the network boundary sits, and why

In remote mode the split is **above the driver, not below it**. The bench host
runs the real driver against real USB; the host machine holds a proxy.

The alternative — tunnelling the USB transport itself — was rejected for two
concrete reasons:

1. **Latency lands in the wrong place.** The source-measure unit has a timed
   wake handshake and demultiplexes a ~4 kHz sample stream. Putting wifi
   latency inside those is not a slowdown, it is a correctness problem.
2. **It would only remote one instrument.** A transport-level proxy is specific
   to a transport. Proxying above the driver covers every instrument, including
   the two that talk USB HID through raw ioctls.

The consequence you must design around: **closed-loop subsystems run on the
bench, not across the link.** The battery emulator's control loop ticks at
100 Hz, and each tick is two round trips. Over a network that is 0.6–2.0 s of
network traffic per second of wall clock — the loop cannot keep time. Run it
on the bench host. [Local and remote mode](local-vs-remote.md) has the full
list of what changes.

## Safety, and what "safe" actually means

benchctrl drives outputs that can push current into hardware, hold a processor
in reset, and switch mains. The safety design is layered, and each layer is
honest about what it does not cover.

### Layer 1: the driver refuses what it should not do

Safety rules that are specific to an instrument live in its driver, enforced
rather than documented:

- **Allowlists, not denylists.** The devices that switch things take a
  required list of what they may touch. A typo then fails closed. Adding a
  channel cannot silently widen scope.
- **No aggregate targeting.** Where a device's command set offers "switch
  everything" — one line that de-powers a whole rack — no method surface
  reaches it, and the rendered command is pattern-checked before it is sent.
- **Restricted drive modes.** The control-line breakout can drive push-pull;
  this driver cannot, on purpose. Open-drain cannot fight a target's rail, and
  it fails safe on unplug because the chip reverts every pin to an input.
- **Read-back verification.** Where a device acknowledges nothing — and
  several acknowledge nothing at all — the driver re-reads and raises on
  disagreement. Telling you a DUT is de-powered when its contactor never moved
  is worse than failing loudly.

### Layer 2: one writer per device

Two things driving one instrument is a class of bug that produces
irreproducible results. So a device has exactly one writer. Additional
sessions attach as read-only observers, and the claim is taken at open —
before any command runs — so the loser fails immediately rather than
half-configuring the hardware.

Nothing retries and nothing queues. The correct response to losing a claim is
to wait or to run your command where the claim is held.

### Layer 3: the governor, on remote benches

Locally, a dropped connection is harmless: the process that opened the device
is the one that died, so its cleanup runs. Over a network, the host can vanish
while the bench keeps driving current into a DUT with nothing watching.

The bench-side governor tracks armed state **from the calls it sees**, rather
than trusting the client to report it. If no frame arrives while something is
armed, it escalates: a priority safe-state command that jumps the device's work
queue, then a transport reset and retry, then critical events and a refusal to
accept new work.

Two exemptions are deliberate, because "safe" is not universal:

- **A switched PDU is exempt from the safe state.** For every instrument on
  the bench, safe means *stop sourcing or sinking*. For a PDU, cutting mains is
  itself the disruptive act — it de-powers a DUT mid-measurement and drops
  every other instrument's session. A lost heartbeat must not become a
  bench-wide power failure. Cutting an outlet on a trip is available, opt-in,
  and per-outlet.
- **A PDU is exempt from arm tracking.** Energising an outlet is not arming an
  output: nothing is being driven, and whatever is plugged in arms itself on
  its own device key. Treating it as armed would start a deadman countdown on
  every switch — power up the bench, go to lunch, come back to a tripped
  governor.

### The limit worth stating plainly

**Software cannot guarantee an output goes off.** If a driver thread is wedged
inside a blocking read, the governor cannot get a command out. Closing a serial
port does not command an output off — a source-measure unit holds its last
commanded state.

For an unattended overnight run at energies that matter, the only real
guarantee is a hardware interlock: a relay on the DUT rail driven from a
control line, or the instrument's own hardware watchdog. One of the supported
relay interfaces has exactly that — a firmware watchdog that de-energises all
eight relays by itself, with no software in the decision path. A wedged
process, a killed agent, an unplugged cable and a panicking kernel all look
identical to it, and all drop the load.

Treat the governor as damage limitation, not a safety certificate.

### Layer 4: authorisation at the entry point

The API assumes you meant what you called. The CLI and the agent surface do
not, because a typo in a shell and a confident language model are different
risks from a reviewed script.

Every one of the 324 functions is explicitly classified into one of four tiers,
and a test fails the build if a new function ships unclassified:

| Tier | Gate | Examples |
|---|---|---|
| read | none | measurements, state queries, identity |
| write, low consequence | none | setpoints on a de-energised output, ranges, integration time — **and de-energising**, which is never gated harder than energising |
| write | explicit `--yes` | energising, sinking, closing a relay, driving a line into a DUT |
| write, consequence outlives the command | `--yes` **and** a named environment variable | switching mains; arming a hardware watchdog |

Two details in there are the whole point. **Reaching the safe state is
frictionless** — if de-energising needed more ceremony than energising, an
operator fighting a live output would have a gate in the way. And the
classification is explicit rather than inferred from function names: a
name-prefix rule would file a raw command passthrough under "safe" and a
snapshot read under "dangerous".

See [`cli.md`](../cli.md) for the mechanics.

## What a measurement actually does

Putting it together, a recording on a remote bench:

1. **Resolve.** `session.resolve("otii_arc")` returns a proxy, because config
   says that key is remote.
2. **Claim and open.** The proxy authenticates to the bench host with an HMAC
   challenge-response — the token never crosses the wire — and takes the
   writer claim. If someone else holds it, this fails now.
3. **Configure.** Setpoints and ranges go across as individual calls. Property
   snapshots ride along on every response, so reading eighteen properties costs
   nothing extra instead of eighteen round trips.
4. **Arm.** Enabling the output marks the device armed on the bench side. The
   deadman is now live.
5. **Record.** Sampling happens on the bench at the instrument's native rate.
   Data is *not* streamed home — live counts, statistics and previews are
   computed on the bench and cost nothing. This is why a recording is
   unavailable until the block exits.
6. **Transfer.** At stop, the samples come back in one transfer. A 60-second
   capture at 9 ksps is about 2.2 MB.
7. **Disarm and release.** Ending the session cleanly releases the claim. The
   bench host reads a clean disconnect as consent — which is exactly what
   distinguishes it from a host that crashed.

Every one of those steps is the same call locally, with steps 2, 6 and 7
collapsing to nothing.

## Design decisions you will notice

A few choices show through into the API, and knowing the reason makes them
predictable rather than arbitrary.

**Vocabulary follows physics, not convenience.** The control-line driver says
`asserted` and `released`, never `high` and `low`, because reset lines are
active-low and "set the line high" is ambiguous exactly where a mistake holds a
processor in reset indefinitely. For an input, "am I asserting this?" returns
`None` rather than a boolean — "nobody is pulling this line down" and "this pin
is an input, so the question does not apply" are different facts, and only the
second means you are asking the wrong object.

**Firmware does the timing.** Where microsecond timing matters, benchctrl
programs the instrument's own sequencer and then records the result. A
host-driven loop over USB cannot hold sub-millisecond steps, so it does not
pretend to.

**Failures name the cause.** Where two very different problems produce the same
symptom, the error says which one it is. A device whose command interface allows
one session at a time fails *after* a successful login when another session
holds it — indistinguishable from bad credentials unless the driver says so.

**Known limitations are documented, not hidden.**
[`KNOWN_LIMITATIONS.md`](../../KNOWN_LIMITATIONS.md) is a first-class
document listing hardware caps, firmware bugs and harness workarounds, with
what was measured. Read it before debugging a new failure — a good fraction of
surprises are already in there with an explanation.

## Where to go next

- [Supported equipment](equipment-matrix.md) — what each instrument is good for
- [Local and remote mode](local-vs-remote.md) — the trade-offs in detail
- [Adding a driver](adding-a-driver.md) — the extension path

# Local and remote mode

Three ways a device can resolve — **local**, **remote**, **sim** — decided
independently per device, and nothing above that seam can tell which it got.

This page covers when to use which, how to configure it, and what genuinely
changes in remote mode. The last part matters most: remote mode is not
transparent, and one subsystem does not work across a network at all.

## The three modes

| Mode | The driver runs | Use when |
|---|---|---|
| **local** | in your process, against USB on this machine | the instruments are plugged into the machine you are working on |
| **remote** | on a bench host, reached over TCP | the bench lives in a lab, is shared, or must keep running while your laptop sleeps |
| **sim** | against a wire-protocol simulator | you are writing the measurement, in CI, or on a plane |

**With nothing configured, everything resolves local.** No config file, no
environment variables, no flags means behaviour is byte-for-byte what it was
before remote mode existed.

## Local

The default, and the right choice for interactive work. Plug it in, open it,
drive it:

```python
from benchctrl.drivers.otii_arc import OtiiArc
with OtiiArc.open() as smu:
    ...
```

Local mode has one property remote mode cannot match: **the process that opened
the device is the process that dies.** If your script crashes, its `__exit__`
runs and the output goes off. There is no network partition to reason about.

## Remote

The instruments stay at the bench; your code runs wherever you are.

On the bench machine:

```bash
benchctrl-agent --generate-token       # save this
benchctrl-agent --token <token>
```

On your machine:

```bash
export BENCHCTRL_REMOTE=bench.local:9737
export BENCHCTRL_TOKEN=<token>
benchctrl-mcp                          # or your own script, or the CLI
```

Or per command:

```bash
benchctrl --remote bench.local:9737 adu218 relay-states
```

Any non-local binding is announced on stderr, so *"why did that read a
simulator"* always has an answer. Silence means everything resolved local.

For standing the agent up properly — as a service, with device permissions and
the safety stop wired in — see [Setting up a bench host](bench-host-setup.md).

## Sim

```bash
benchctrl-agent --simulate                              # whole bench
BENCHCTRL_SIM_DEVICES=otii_arc,ontrak_adu218 benchctrl  # just some
benchctrl --sim ontrak_adu218 adu218 relay-states       # one command
```

A simulator speaks its instrument's **real wire protocol** over a
pseudo-terminal, with the production driver unmodified — the transport, the
binary framing, the session handshake and the reader thread all run for real.
The two USB-HID devices substitute at the link seam instead, because a pty
cannot carry HID ioctls.

**What a green simulator suite proves:** the code that ships works, end to end,
including the framing and the threading.

**What it does not prove:** anything about the device. A simulator models
hardware behaviour *because the hardware was measured*, so asserting that
behaviour against the simulator proves the model is self-consistent. It also
proves nothing about wiring — no voltage moved on any wire. That is what
hardware-marked tests and a multimeter are for.

## A split bench

Because resolution is per device key, a bench can be half in the lab and half on
your desk, driven by one script that mentions neither. Put this in
`~/.config/benchctrl/config.json`:

```json
{
  "endpoints": {
    "bench": {"host": "bench.local", "port": 9737, "token": "..."}
  },
  "devices": {
    "otii_arc":         {"mode": "remote", "endpoint": "bench"},
    "eastwood_qr10x":   {"mode": "remote", "endpoint": "bench"},
    "siglent_sdm4065a": {"mode": "remote", "endpoint": "bench"},
    "silabs_cp2112":    {"mode": "remote", "endpoint": "bench"},
    "ontrak_adu218":    {"mode": "remote", "endpoint": "bench"},
    "rigol_dl3031a":    {"mode": "local"},
    "rigol_dp2031":     {"mode": "local"}
  }
}
```

That is five instruments at the bench, both Rigols plugged into the laptop, one
script driving all seven.

Also useful: everything remote *except* the one instrument you are bringing up.

```bash
benchctrl --remote bench.local --local silabs_cp2112 cp2112 line-states
```

## Configuration precedence

Highest first:

1. `benchctrl.session.configure(...)` in code
2. CLI flags — `--remote`, `--local`, `--sim`
3. environment — `BENCHCTRL_REMOTE`, `BENCHCTRL_TOKEN`,
   `BENCHCTRL_LOCAL_DEVICES`, `BENCHCTRL_SIM_DEVICES`, `BENCHCTRL_CONFIG`
4. `~/.config/benchctrl/config.json`
5. everything local

Two things about this are easy to get wrong.

**With no flags, the CLI installs no config object at all** — deliberately, so
that an empty object cannot count as an override and beat levels 3 and 4.

**If you embed benchctrl in your own program, call
`session.configure_from_environment()` once before the first device opens.**
Levels 2–4 only reach `session` when an entry point installs them. `benchctrl`
and `benchctrl-mcp` both do it; your script does not, and the failure mode is
that a device you asked to **simulate drives the real instrument**.

A config that cannot be read, or that names an invalid mode, exits non-zero. It
never degrades to "no config", for the same reason.

### Per-bench open arguments

Arguments to `open()` — ports, allowlists — come from the `open` block of the
same config file, so the shell command does not carry this bench's cabling:

```json
{
  "devices": {
    "cyberpower_pdu41002": {
      "mode": "local",
      "open": {"port": "/dev/benchctrl/pdu41002", "allowed_outlets": [4, 5]}
    }
  }
}
```

**Never put a password there.** `to_dict()` emits `open` verbatim, and `open`
crosses the RPC wire. Secrets come from the environment where the *driver* runs
— on the bench host, for a remote bench.

## What actually changes in remote mode

The tool surface does not change. These do.

### Latency

Measured over USB (`adb forward`) to the reference bench host. Wifi is slower
and lumpier — expect 2–8 ms typical, 20–100 ms at the tail.

| Operation | Local | Remote |
|---|---|---|
| `set_voltage` | ~1 ms | ~4 ms |
| `state()` — 18 properties | ~0.05 ms | ~0.01 ms (piggybacked) |
| `read_value`, no recording | ~500 ms (device-bound) | + 1 round trip |
| `record()` 60 s @ 9 ksps | no transfer | + 2.2 MB at stop |
| `stream()` per sample | up to 200 ms (already batched) | 200–400 ms |
| `Emulator` @ 100 Hz | 10 ms/tick | **not viable — run it on the bench** |

Property snapshots ride along on every response, which is why reading eighteen
properties is *cheaper* remote than local rather than eighteen round trips.

### The battery emulator does not work across a network

This is the one hard limitation, and it is worth understanding rather than
working around.

The emulator is a **closed control loop**: read current, compute
`V = OCV(SoC) − I × ESR(SoC)`, write voltage — at 100 Hz. Each tick is two round
trips. Over a network that is 0.6–2.0 s of traffic per second of wall clock, so
the loop cannot keep time, and a battery emulator that cannot keep time is not
emulating a battery.

The fix is not a faster network. **Run the loop where the instrument is** — as
an unattended run submitted to the bench host, with `mode: "emulator"` in the
phase. The engine runs the loop on the bench and you watch events, which is the
same experiment with the network outside the loop instead of inside it.

The same reasoning applies to anything you write yourself that reads and writes
the same instrument in a tight loop. Push the loop to the bench.

### Six behavioural differences

- **`close()` is not proxied.** Closing is governor-mediated so an armed output
  is never orphaned. Use `agent.close` or `session.shutdown()`.
- **Recording data is unavailable until the `record()` block exits.** Live
  counts, statistics and previews are computed on the bench and cost nothing.
  After the block the object *is* a real `Recording`.
- **Recordings are capped at `max_recording_s`** (300 s as shipped) — a chunk is
  held in RAM at roughly 40 bytes per sample. Longer captures go through the run
  engine, which chunks to disk.
- **`read_window()` cannot preserve caller-object identity** in its result keys.
  Object identity does not cross a wire; keys are equal by code and by `==`.
- **One writer per device.** Extra sessions attach as read-only observers, and
  the claim is taken at open, so the loser fails before half-configuring
  anything. Nothing retries and nothing queues.
- **The emulator and a recording cannot run concurrently on one source-measure
  unit** — true locally too, but you meet it here because the run engine refuses
  the combination rather than deadlocking.

### One application ships with a local-only default

`applications/sensor_profiler` rolls a capture chunk every **60 minutes** by
default. Over a network that is a very large transfer at every roll, so run that
application in local mode or lower `--chunk-minutes`. See
[`KNOWN_LIMITATIONS.md`](../../KNOWN_LIMITATIONS.md) § N-3.

### Safety changes shape

Locally, a crash cleans up after itself. Over a network the bench can be left
driving current into a DUT with nothing watching, so the bench host runs a
governor: it tracks armed state **from the calls it sees**, and escalates when
frames stop arriving — priority safe-state command, then transport reset and
retry, then critical events and a refusal to accept new work.

Two exemptions are deliberate. A switched PDU is exempt from the safe state
(cutting mains is itself the disruptive act, and a lost heartbeat must not become
a bench-wide power failure) and from arm tracking (energising an outlet is not
arming an output). Cutting an outlet on a trip is available, opt-in and
per-outlet via `panic_outlets`.

**Software cannot guarantee an output goes off.** A driver thread wedged in a
blocking read cannot be reached by the governor, and closing a serial port does
not command an output off. For unattended work at energies that matter, use a
hardware interlock — see [Unattended runs](examples/unattended-runs.md).

### Security

Authentication is HMAC-SHA256 challenge-response over a pre-shared token, so the
token never crosses the wire and a server nonce bounds replay. Three failures
from one address tarpits it for 30 seconds.

**There is no confidentiality.** Everything after the handshake is plaintext on
the LAN — setpoints, measurements, everything. What the handshake buys you is
that a stray host on the same subnet cannot drive your instruments, which is the
threat that matters when the far end can push current into a DUT. On a network
you do not control, tunnel it:

```bash
ssh -L 9737:localhost:9737 user@bench.local
```

Running the agent with no token is allowed and logs a warning on every
connection. Do not do it on a network you do not control.

## Choosing

| Situation | Mode |
|---|---|
| Interactive bring-up, instruments on your desk | local |
| A shared bench, or one in a different room | remote |
| Runs that must survive your laptop sleeping | remote, via the run engine |
| A control loop reading and writing one instrument | **local, or on the bench** |
| Writing the measurement before the hardware arrives | sim |
| CI | sim |
| Bringing up one new instrument against a working bench | remote + `--local <that one>` |

## Finding a bench

The agent broadcasts a small UDP beacon:

```python
from benchctrl.net import beacon
for bench in beacon.listen(timeout=5.0, token=my_token):
    print(bench)   # "unoq at 10.0.0.7:9737 — 2 device(s), agent 1.2.0"
```

It carries a token *fingerprint* so you can pick out your own bench, and a
device count. It deliberately carries no model names or serial numbers, because
it is broadcast to the whole subnet.

For zeroconf tools, install a static Avahi service file:

```bash
benchctrl-agent --print-avahi-service > /etc/avahi/services/benchctrl.service
```

## Next

- [Setting up a bench host](bench-host-setup.md) — the agent as a service
- [Unattended runs](examples/unattended-runs.md) — hand the bench an experiment
- [`remote.md`](../remote.md) — the protocol-level reference

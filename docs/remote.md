# Remote mode

Run the instruments on one machine and the agent on another. The 226 MCP
tools are unchanged and cannot tell the difference.

The motivating setup: an Arduino Uno Q sits at the bench holding the USB
cables, the dev laptop runs the coding agent and the MCP server, and calls
stream over wifi. But nothing here is Uno Q specific — the agent runs on any
machine that can see the instruments.

## Quick start

On the bench machine:

```bash
benchctrl-agent --generate-token          # save this somewhere
benchctrl-agent --token <token>           # or use a config file, below
```

On the host:

```bash
export BENCHCTRL_REMOTE=bench.local:9737
export BENCHCTRL_TOKEN=<token>
benchctrl-mcp
```

That is the whole change. Every tool now operates on the instruments
attached to the bench machine.

**With nothing configured, nothing changes.** No config file, no environment
variables, no flags means every device resolves to `local` and behaviour is
byte-for-byte what it was.

## No hardware? Simulate the whole bench

```bash
benchctrl-agent --simulate
```

Every device is backed by a simulator behind a pseudo-terminal, with the
production driver still in the path — the real `Transport`, the real binary
framing, the real reader thread. Only the silicon is fake. This is how the
remote stack was developed and it is the fastest way to try any of this
without instruments on the desk.

## Configuration

Precedence, highest first:

1. `benchctrl.session.configure(...)` in code
2. CLI flags — `--remote`, `--local`, `--sim`
3. environment — `BENCHCTRL_REMOTE`, `BENCHCTRL_TOKEN`, `BENCHCTRL_LOCAL_DEVICES`
4. `~/.config/benchctrl/config.json`
5. everything local

Mode resolves **per device key**, which is what lets you split a bench:

```json
{
  "endpoints": {
    "bench": {"host": "unoq.local", "port": 9737, "token": "..."}
  },
  "devices": {
    "otii_arc":       {"mode": "remote", "endpoint": "bench"},
    "eastwood_qr10x": {"mode": "remote", "endpoint": "bench"},
    "rigol_dl3031a":  {"mode": "local"},
    "rigol_dp2031":   {"mode": "local"}
  }
}
```

Arc and QR10x on the bench, both Rigols plugged into the laptop, one MCP
server driving all four.

Agent side, `/etc/benchctrl/agent.json` (mode 0640):

```json
{
  "host": "0.0.0.0", "port": 9737, "token": "...",
  "devices": ["otii_arc"],
  "deadman_s": 15, "max_recording_s": 300,
  "blob_dir": "/home/arduino/benchctrl/blobs",
  "runs_dir": "/home/arduino/benchctrl/runs"
}
```

## Discovery

The agent broadcasts a small UDP beacon; hosts listen for it.

```python
from benchctrl.net import beacon
for bench in beacon.listen(timeout=5.0, token=my_token):
    print(bench)          # "unoq at 10.0.0.7:9737 — 2 device(s), agent 1.1.0"
```

The beacon carries a token *fingerprint* — a hash prefix — so you can pick
out your own bench, and a device count. It deliberately carries no model
names or serial numbers: it is broadcast to the entire subnet.

For DNS-SD interop, install a static Avahi service file:

```bash
benchctrl-agent --print-avahi-service > /etc/avahi/services/benchctrl.service
```

avahi-daemon picks up that directory automatically, so `avahi-browse` and
other zeroconf tools find the bench with no Python dependency at all.

## Security

Authentication is HMAC-SHA256 challenge-response over a pre-shared token.
The token never crosses the wire, so a passive listener on shared wifi
cannot lift it, and a server nonce bounds replay. Three failures from one
address tarpits it for 30 seconds.

**There is no confidentiality.** Everything after the handshake is plaintext
on the LAN — setpoints, measurements, everything. Anyone who can sniff the
link can read it, and anyone who can inject packets can interfere. What the
handshake buys you is that a stray host on the same subnet cannot drive your
instruments, which is the threat that matters when the far end can push
current into a DUT.

For a hostile network, tunnel it:

```bash
ssh -L 9737:localhost:9737 arduino@bench.local
```

Running the agent with no token at all is allowed and logs a warning on
every connection. Do not do it on a network you do not control.

## Safety

Locally, a dropped connection is harmless — the process that opened the
device is the one that died, and `__exit__` runs. Over a network the host
can vanish while the bench keeps driving current into a DUT with nothing
watching. That is what the safety governor exists for.

The agent tracks armed state itself, from the calls it sees, rather than
trusting client bookkeeping. If no frame arrives for `deadman_s` while
something is armed, it escalates: a priority `safe_state()` that jumps the
device's work queue; then a transport reset and retry; then critical events
and a refusal to accept new work.

**Software cannot guarantee the output goes off.** If the driver thread is
wedged inside a blocking read the governor cannot get a command out, and
closing the serial port does not command an Arc's output off — it holds its
last commanded state. For an unattended overnight run the only real
guarantee is a hardware interlock: a relay on the DUT rail driven from a
GPIO, or the Arc's own GPO. Treat the governor as damage limitation, not a
safety certificate.

Deploy with `ExecStopPost=... --safe-stop` so a service restart disarms the
bench rather than leaving an output live across the gap. That is what the unit
in [`deploy/`](../deploy/README.md) does — it invokes `python3 -m
benchctrl.agent.main` rather than the console script, because the board has no
pip and reaches the package through `PYTHONPATH`.

## What is different in remote mode

Measured over USB (`adb forward`) to an Uno Q. Wifi will be slower and
lumpier — expect 2–8 ms typical and 20–100 ms at the tail.

| Operation | Local | Remote |
|---|---|---|
| `set_voltage` (MCP tool) | ~1 ms | ~4 ms |
| `state()` — 18 properties | ~0.05 ms | ~0.01 ms (piggybacked) |
| `read_value`, no recording | ~500 ms (device-bound) | +1 round trip |
| `record()` 60 s @ 9 ksps | no transfer | +2.2 MB at stop |
| `stream()` per sample | up to 200 ms (already batched) | 200–400 ms |
| `Emulator` @ 100 Hz | 10 ms/tick | **not viable — run it on the bench** |

Property snapshots ride on every response, so `_smu_state()` costs nothing
extra. Without that it would be 18 round trips and every setter tool would
feel broken.

Behavioural differences worth knowing:

- **`close()` is not proxied.** Closing is governor-mediated so an armed
  output is never orphaned. Use `agent.close` or `session.shutdown()`.
- **Recording data is unavailable until the `record()` block exits.** Live
  counts, statistics and previews are computed on the bench and cost
  nothing. After the block, the object *is* a real `Recording`.
- **Recordings are capped at `max_recording_s` (default 300 s)**, because a
  chunk is held in RAM at roughly 40 bytes per sample. Longer captures go
  through the run engine, which chunks to disk.
- **`read_window()` cannot preserve caller-object identity** in its result
  keys — object identity does not cross a wire. Keys are equal by code and
  by `==`.
- **One writer per device.** Extra sessions are read-only observers.
- **A-1 still applies.** The emulator and a recording cannot run
  concurrently on one Arc; the run engine refuses the combination rather
  than deadlocking.

## Unattended runs

Submit a declarative spec and disconnect. The bench keeps going.

```python
run = client.call("run.submit", {"spec": spec})
# ... host goes away, comes back hours later ...
missed = client.call("run.events", {"run_id": run["run_id"], "since_seq": last})
```

Events carry monotonic sequence numbers assigned inside the transaction that
persists them, so a reconnecting host replays exactly what it missed —
nothing lost, nothing duplicated.

Each run produces a self-describing bundle:

```
runs/<run_id>/
  spec.json          the exact spec, content-hashed
  run.db             sqlite (WAL): events, metrics, phases, chunks
  events.ndjson      fsync'd mirror — survives a corrupted database
  notes.md           narrative, including any LLM commentary
  data/chunkNNN.opensmu
  manifest.json
```

A run still marked `running` under a previous boot id is marked
`interrupted` rather than resumed. After a power cut the DUT's state is
unknown, so that is a decision for a person.

See `docs/runs.md` for the spec format.

## Deploying to an Arduino Uno Q

App Lab apps run in Docker containers and **cannot reach `/dev/ttyUSB*` or
`/dev/ttyACM*`** — device passthrough is brick-level and limited to
camera/microphone/speaker classes. The agent therefore runs as a native
systemd service, not an App Lab app.

That is fine, and it is why the agent is stdlib + `pyserial` only: the board
has no pip, and the MCP stack's dependencies ship compiled wheels for the
wrong architecture. `benchctrl` itself is pure Python, so the board runs the
real drivers.

Deploying needs no package manager at all:

```bash
adb push src/benchctrl /home/arduino/benchctrl/src/
cd /home/arduino/benchctrl/src && unzip pyserial-3.5-py2.py3-none-any.whl
PYTHONPATH=. python3 -m benchctrl.agent.main --simulate
```

A wheel is a zip; unzipping `pyserial` next to the package is enough.

Once that runs by hand, install the service — unit, config and token in one
step:

```bash
cd deploy && sudo ./install-agent.sh
```

See [`deploy/README.md`](../deploy/README.md) for the installed layout, the
`SRC_DIR`/`PYTHON`/`RUN_USER` knobs, how to prove the `--safe-stop` path
actually fires, and an optional fix for HDMI through a USB-C hub.

Other board notes:

- Blobs and runs go under `/home/arduino` (17 GB free), never `/` (under
  2 GB, shared with Docker images and App Lab's models).
- The `arduino` user is already in `dialout`.
- Add udev rules for stable device names so multi-instrument configs are
  deterministic:
  ```
  SUBSYSTEM=="tty", ATTRS{idVendor}=="0fce", ATTRS{idProduct}=="d1e6", \
    SYMLINK+="benchctrl/otii-$attr{serial}", MODE="0660", GROUP="dialout"
  ```
- Keep the USB cable attached during bring-up. `adb` is the only
  out-of-band console if wifi drops mid-run, and `adb forward tcp:9737
  tcp:9737` gives a working TCP path with no network at all.

# Simulation — running the stack with no hardware

`benchctrl.sim` provides a simulator for every driver in the package.
Each one speaks its instrument's **real wire protocol** over a
pseudo-terminal, so the production driver connects to it unmodified
through `serial.Serial`.

This is the distinction that matters: these are *device* simulators,
not mocks of benchctrl classes. When a test drives `SimulatedOtiiArc`,
the code under test includes `Transport`, the binary framing in
`protocol.py`, the timed session-init handshake, the error-frame
queue, and the recording reader thread — none of it monkeypatched. A
bug in any of those layers shows up in a hardware-free test run.

The Rigol simulators go one step further and are reached through
**pyvisa-py's ASRL backend**, so the real pyvisa stack is in the path
as well.

## When to use it

| Situation | What to reach for |
|---|---|
| Writing tests for anything that touches a driver | `SimulatedOtiiArc` etc. directly |
| Developing MCP tools or agent code without a bench | `BENCHCTRL_SIM_DEVICES=...` |
| Demoing the whole stack on a laptop | `benchctrl-agent --simulate` |
| Exercising the battery emulator without a cell | `OhmicLoad` (see below) |

## Direct use

```python
import time
from benchctrl.sim import SimulatedOtiiArc
from benchctrl.drivers.otii_arc import OtiiArc

with SimulatedOtiiArc() as sim:
    smu = OtiiArc.open(sim.port)          # the production driver
    smu.set_voltage(3.3)
    smu.set_output(True)
    with smu.record("mc", "mv") as rec:
        time.sleep(1.0)
    print(rec.statistics("mc").mean)
```

`sim.port` is the slave side of the pty pair; hand it to any driver's
`open()` exactly as you would a real `/dev/ttyACM0` or `COM6`.

Every simulator is a context manager and a `SimDevice`, which gives
you `start()` / `stop()` / `close()`, an `elapsed_s` clock, and
`pump(ticks=N)` for deterministic stepping when you don't want the
free-running thread.

### The simulators

| Class | Simulates | Protocol modelled |
|---|---|---|
| `SimulatedOtiiArc` | Otii Arc / Arc Pro | Session handshake, `SET` with per-parameter range validation, `GET` readbacks incl. the 268 B channel inventory, baseline streaming, packed sub-1 / sub-4 high-rate framing |
| `SimulatedQR10x` | Eastwood QR10x | AT command set, relay-ladder quantisation, safety limit, settling delay |
| `SimulatedRigolDL3031A` | Rigol DL3031A | SCPI over ASRL, incl. the `:SOUR:FUNC` set-as-`CURRent`/read-as-`CC` quirk |
| `SimulatedRigolDP2031` | Rigol DP2031 | SCPI over ASRL, three channels, register model |

The SCPI simulators use a generic register model that covers the bulk
of the ~254 distinct SCPI strings across the two Rigols; measurement,
identity and error-queue behaviour are modelled explicitly rather than
generically.

## Sim mode — no code changes

`benchctrl.session` resolves each device to `local`, `remote` or `sim`
independently, so you can simulate part of a bench and leave the rest
real. Nothing above the seam changes — all 226 MCP tools are unaware.

```bash
# whole bench simulated
benchctrl-agent --simulate

# just the two you don't have on the desk today
BENCHCTRL_SIM_DEVICES=otii_arc,rigol_dp2031 benchctrl-mcp
```

Device keys are the canonical ones from `benchctrl.config.DEVICE_KEYS`:
`otii_arc`, `eastwood_qr10x`, `rigol_dl3031a`, `rigol_dp2031`.

Or in a config file:

```json
{
  "devices": {
    "otii_arc":     { "mode": "sim" },
    "rigol_dl3031a": { "mode": "local" }
  }
}
```

See [`remote.md`](remote.md) for the full configuration precedence
(explicit > CLI > env > file > all-local).

In sim mode the factories return the **production driver bound to a
simulator**, never a mock, so sim mode exercises the real code path.
The simulator's lifetime is tied to the driver — closing the driver
closes the simulator.

## Waveforms — asserting exact values

Channel data comes from `benchctrl.sim.waveforms`, which provides
analytically-known signals. That lets tests assert exact statistics
instead of "a number arrived":

```python
from benchctrl.sim import SimulatedOtiiArc, Sine, Constant

sim = SimulatedOtiiArc(channels={
    "mc": Sine(amplitude=0.010, offset=0.050, freq_hz=2.0),
    "mv": Constant(3.3),
})
```

Available: `Constant`, `Sine`, `Square`, `Ramp`, `Steps`, and
`OhmicLoad`. Any object matching the `Waveform` protocol works.

`OhmicLoad` is the interesting one — it closes the V→I loop by
computing current from the simulator's present output voltage, which
makes the **battery emulator exercisable without a cell**. It is the
default for the `mc` channel (330 Ω unless you pass `load_ohm=`).

## Fault injection

The async error path is the hardest thing to test against real
hardware, since it depends on the device rejecting something. The Arc
simulator can be told to reject the next `SET` with a genuine
negative-status error frame:

```python
sim.inject_error()          # next SET is rejected out-of-range
```

The driver then surfaces it as a real `BenchCommandError`, queued and
raised on the following `SET`, exactly as the hardware does.

## Known gaps

Simulators model the wire protocol, not the physics or the firmware
defects. In particular they do **not** reproduce the quirks catalogued
in [`../KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) — the DL3031A
4-step LIST bug, the DP2031 pair-mode transition delay, the Arc Pro
4.2 V high-range ceiling under load. A test that passes in sim can
still fail on the bench for those reasons, which is why the
hardware-marked suite exists.

Two limits that *are* shared with hardware, because they live in the
driver rather than the device:

- **A-3** — `start_recording()` does not flush the inbound buffer, so
  a recording's first samples can predate it. Found by these tests.
- **A-4** — `Transport.read_chunk` blocks until the buffer fills or
  the 0.5 s timeout expires, so a running recording reports no samples
  for up to half a second.

## Cost

Nothing in `benchctrl.sim` is imported by the driver or MCP layers. It
is a development and test dependency only, and costs nothing at
runtime.

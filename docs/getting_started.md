# Getting started with benchctrl

## Install

```bash
pip install benchctrl                  # SMU + battery + QR10x
pip install benchctrl[mcp]             # + MCP server
pip install benchctrl[bench-visa]      # + the Rigol drivers (pyvisa)
pip install benchctrl[science]         # + numpy / pandas / parquet / matplotlib
```

For development:

```bash
git clone https://github.com/rickmellor/benchctrl
cd benchctrl
pip install -e ".[dev,mcp,bench-visa,science]"
pytest -m "not hardware" -q            # 1333 tests, no device needed
```

## No hardware yet?

You don't need an instrument to work through this guide. Every driver
has a simulator that speaks the real wire protocol over a
pseudo-terminal, so the production driver runs unmodified:

```python
from benchctrl.sim import SimulatedOtiiArc
from benchctrl.drivers.otii_arc import OtiiArc

with SimulatedOtiiArc() as sim:
    smu = OtiiArc.open(sim.port)
    ...                                 # everything below works as written
```

Substitute that for `OtiiArc.open()` anywhere in this document. See
[`simulation.md`](simulation.md) for the full picture.

## Plug in the device

The library targets the Qoitech Otii Arc / Arc Pro (USB VID `0x0FCE`,
PID `0xD1E6`). Plug the device into a USB port. It will enumerate as a
standard CDC-ACM virtual COM port:

- **Windows**: `COM6` (or similar)
- **Linux**: `/dev/ttyACM0`
- **macOS**: `/dev/cu.usbmodem*`

No driver install needed on Windows 10/11 — the in-box `usbser.sys` is
sufficient. Linux and macOS need no additional setup.

## First connection

```python
from benchctrl.drivers.otii_arc import OtiiArc
print(OtiiArc.discover())
# [OtiiArcInfo(port='COM6', name='Arc', serial='1234...', description='USB Serial Device')]

with OtiiArc.open() as smu:
    print(f"Connected to {smu.info.port}")
    print(f"Range: {smu.range}")
```

The `OtiiArc.open()` call automatically:
- finds the first connected device (`OtiiArc.discover()` returns the list)
- opens the serial port
- sends the three-step session-init handshake the device requires

## Set a voltage and turn the output on

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.set_output(True)
    time.sleep(1.0)
    smu.set_output(False)
```

**Safety**: with no DUT (device under test) connected, this just drives
the output terminals open-circuit. With a DUT attached, the voltage will
appear on the terminals through the current limit. Confirm your DUT can
tolerate the configured voltage before enabling output.

## Record samples

```python
import time
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    smu.enable_channels(OtiiArcChannel.MAIN_VOLTAGE, OtiiArcChannel.MAIN_CURRENT)

    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5.0)
        smu.set_output(False)

    stats = rec.statistics(OtiiArcChannel.MAIN_CURRENT)
    print(f"Avg current: {stats.average*1000:.2f} mA, total charge: {stats.charge:.4f} C")
    rec.save_csv("run.csv")
```

`smu.record()` is a context manager. It:
1. Sends the `START_RECORDING` payload with your enabled channels.
2. Starts a background reader thread that buffers samples into the
   `Recording` object.
3. On exit, sends `STOP_RECORDING` and joins the reader.

You can also drive the recording manually:

```python
rec = smu.start_recording()
# ...
rec = smu.stop_recording()
```

## Channels

Channels are an enum:

```python
from benchctrl.drivers.otii_arc import OtiiArcChannel

OtiiArcChannel.MAIN_CURRENT     # the headline 'mc' channel
OtiiArcChannel.MAIN_VOLTAGE     # 'mv'
OtiiArcChannel.MAIN_POWER       # 'mp'
OtiiArcChannel.ADC_CURRENT      # 'ac'
OtiiArcChannel.ADC_VOLTAGE      # 'av'
OtiiArcChannel.SENSE_PLUS       # 'sp'
OtiiArcChannel.SENSE_MINUS      # 'sn'
OtiiArcChannel.VBUS             # 'vb'
OtiiArcChannel.DC_JACK          # 'vj'
OtiiArcChannel.TEMPERATURE      # 'tp' (always on)
OtiiArcChannel.GPI1             # 'i1'
OtiiArcChannel.GPI2             # 'i2'
OtiiArcChannel.UART_RX          # 'rx'  (deferred parsing)
```

Framework subsystems that don't care about Arc-specific channels can
import `StandardChannel` from `benchctrl.channels` — it covers the
common `MAIN_CURRENT` / `MAIN_VOLTAGE` / `MAIN_POWER` subset and works
with any SMU driver.

Each channel carries metadata:

```python
OtiiArcChannel.MAIN_CURRENT.code         # 'mc'
OtiiArcChannel.MAIN_CURRENT.wire_id      # 0x00
OtiiArcChannel.MAIN_CURRENT.subtype      # 4 (high-rate)
OtiiArcChannel.MAIN_CURRENT.sample_rate  # 4000 (theoretical max)
OtiiArcChannel.MAIN_CURRENT.unit         # 'A'
OtiiArcChannel.MAIN_CURRENT.label        # 'Main Current'
```

Strings work too at API boundaries:

```python
smu.enable_channels("mc", "mv")
```

## Save and reload

```python
from benchctrl import Recording

# Save
rec.save("capture.opensmu")     # native binary, lossless
rec.save_csv("capture.csv")     # long-form by default
rec.save_csv("capture.csv", format="wide")  # one column per channel
rec.save_json("capture.json")   # samples + statistics

# Reload (no SMU needed)
loaded = Recording.load("capture.opensmu")
stats = loaded.statistics("mc")
```

## Streaming (no recording)

For interactive monitoring without buffering:

```python
from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

with OtiiArc.open() as smu:
    for sample in smu.stream(seconds=10.0):
        if sample.channel is OtiiArcChannel.MAIN_VOLTAGE:
            print(f"V = {sample.value:.4f}")
```

`smu.stream()` yields `Sample(timestamp, value, channel)` named tuples.
It cannot run concurrently with a recording.

## Error handling

```python
from benchctrl.drivers.otii_arc import OtiiArc
from benchctrl.exceptions import BenchValueError, BenchCommandError

with OtiiArc.open() as smu:
    try:
        smu.set_voltage(10.0)      # out of client-side range
    except BenchValueError as e:
        print("Client refused:", e)

    smu.set_range("low")
    try:
        smu.set_voltage(4.0)        # device will reject in low range
        import time; time.sleep(2)
        smu.set_voltage(3.25)       # this triggers raise of the queued error
    except BenchCommandError as e:
        print(f"Device rejected: err={e.error_code}, reverted to {e.last_good_value}")
```

## The rest of the bench

The Arc is one driver among peers. Each lives in its own subpackage,
so you import only what you own:

```python
from benchctrl.drivers.eastwood_qr10x import QR10x          # programmable resistor
from benchctrl.drivers.rigol_dl3031a import RigolDL3031A    # electronic load
from benchctrl.drivers.rigol_dp2031 import RigolDP2031      # triple-output PSU
from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A  # 6½-digit DMM
from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002  # switched PDU
from benchctrl.drivers.ontrak_adu218 import OntrakADU218      # relays + digital I/O
from benchctrl.drivers.silabs_cp2112 import CP2112            # open-drain control lines
```

They share the conventions you've already seen — `open()` as a context
manager, `set_*` methods for writes, properties for cached reads.
→ [`drivers.md`](drivers.md)

The last three switch or drive things rather than sourcing or measuring
them, and all three take an allowlist of what they may energise. The ADU218
is the only one where it is *optional*, and that is the device rather than
the design: `allowed_outlets` and `allowed_lines` are mandatory with no
"all", because the PDU switches **mains** and a CP2112 line is wired to
whatever a DUT put on that pin, whereas the ADU218's relays are 1 A
signal-level SSRs that the bench wants to toggle freely.

```python
# Optional here — pass it when the relays are wired to something that
# must not switch. Listed or not, de-energising is always permitted.
with OntrakADU218.open(allowed_relays=(0,)) as adu:
    adu.set_relay_state(0, True)     # returns the *read-back* state
    adu.reset_relays()               # de-energise everything, verified
```

The read-back is not belt-and-braces. The ADU218 does not acknowledge a
write and never reports an error at all, so the only way to know a relay
moved is to ask what state it is in — which is why the setter returns
that rather than the value you passed. → [`drivers.md`](drivers.md)

The CP2112 holds a DUT's reset line low — a ~$15 breakout doing a job that
would otherwise tie up an SMU pin. Its lines are **open-drain**: it pulls a
net low and releases it, and never sources into it, which is what makes it
safe on a 1.8 V reset net.

```python
with CP2112.open(allowed_lines=(3,)) as gpio:
    gpio.set_line_mode(3, output=True)              # open-drain is the only mode
    gpio.trigger_reset_pulse(3, duration_s=0.050)   # ≥5 ms; shorter is refused
```

Three of the eight lines (0, 1 and 7) carry an alternate function the chip
can drive itself, and `set_line_mode` refuses those rather than put two
drivers on one net — deliberately, so it is a decision you make knowingly.

One thing to know before you try to find your pin in software: **a level
read back from the chip identifies nothing.** An undriven pin is
high-impedance, so `read_levels()` reports every pin as a latched 1 no
matter what is attached — while a meter on the same net reads ~0 V. Both are
correct. The only way to identify a pin is a level you can make *move*:
assert one line at a time and watch which one the meter follows.
→ [`drivers.md`](drivers.md)

Battery emulation, profiling, and life calculation work against *any*
driver conforming to `benchctrl.interfaces.SourceMeasurementUnit`, not
just the Arc. → [`battery.md`](battery.md)

## What's not implemented

See [`ROADMAP.md`](../ROADMAP.md) for the full list with rationale.
Key items:

- **Calibration** — deferred indefinitely for safety; writes
  persistent state to the device and the wire format is unobserved.
- **Firmware upgrade** — intentionally never; use the vendor app.
- **Channel sample-rate control** — no wire command appears to exist.
  Use `Recording.downsample(channel, factor)` on the host instead.
- **UART log channel parsing** — the `rx` channel produces text
  fragments; structured parsing is not built yet.
- **Multi-device coordination** — multiple independent instances work
  and can now be split across machines, but there are no fan-out
  helpers or shared-timebase sync.

These raise `BenchNotImplementedError` where a user-facing surface
exists, rather than failing quietly.

## CLI

```bash
benchctrl discover                              # list devices
benchctrl info                                  # show state
benchctrl set-voltage 3.3                       # set V
benchctrl set-output on                         # enable output
benchctrl capture 5.0 run.csv -c mc mv          # record for 5 s
benchctrl stream 10                             # live print
```

Two more entry points ship with the package:

```bash
benchctrl-mcp                    # MCP server — 324 tools for LLM agents
benchctrl-agent --token <token>  # bench-side server for remote mode
```

## Next steps

- [`api_reference.md`](api_reference.md) — every class and method
- [`drivers.md`](drivers.md) — the QR10x, DL3031A, DP2031, SDM4065A,
  PDU41002, ADU218 and CP2112 drivers
- [`battery.md`](battery.md) — emulation, profiling, life calculation
- [`output_formats.md`](output_formats.md) — where your samples can go
- [`simulation.md`](simulation.md) — working without hardware
- [`remote.md`](remote.md) — instruments on another machine
- [`runs.md`](runs.md) — unattended declarative experiments
- [`mcp.md`](mcp.md) — driving the bench from an LLM agent
- [`otii_arc_protocol.md`](otii_arc_protocol.md) — what's on the USB cable
- [`examples/`](../examples/) — copy-paste-friendly scripts

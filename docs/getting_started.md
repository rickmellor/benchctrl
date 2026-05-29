# Getting started with OpenSMU

## Install

```bash
pip install opensmu        # once published
```

For development:

```bash
git clone <repo>
cd opensmu
pip install -e ".[dev]"
```

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
from opensmu import SMU

print(SMU.discover())
# [SMUInfo(port='COM6', name='Arc', serial='1234...', description='USB Serial Device')]

with SMU.open() as smu:
    print(f"Connected to {smu.info.port}")
    print(f"Range: {smu.range}")
```

The `SMU.open()` call automatically:
- finds the first connected device (`SMU.discover()` returns the list)
- opens the serial port
- sends the three-step session-init handshake the device requires

## Set a voltage and turn the output on

```python
import time
from opensmu import SMU, Channel

with SMU.open() as smu:
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
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)

    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5.0)
        smu.set_output(False)

    stats = rec.statistics(Channel.MAIN_CURRENT)
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
from opensmu import Channel

Channel.MAIN_CURRENT     # the headline 'mc' channel
Channel.MAIN_VOLTAGE     # 'mv'
Channel.MAIN_POWER       # 'mp'
Channel.ADC_CURRENT      # 'ac'
Channel.ADC_VOLTAGE      # 'av'
Channel.SENSE_PLUS       # 'sp'
Channel.SENSE_MINUS      # 'sn'
Channel.VBUS             # 'vb'
Channel.DC_JACK          # 'vj'
Channel.TEMPERATURE      # 'tp' (always on)
Channel.GPI1             # 'i1'
Channel.GPI2             # 'i2'
Channel.UART_RX          # 'rx'  (deferred parsing)
```

Each channel carries metadata:

```python
Channel.MAIN_CURRENT.code         # 'mc'
Channel.MAIN_CURRENT.wire_id      # 0x00
Channel.MAIN_CURRENT.subtype      # 4 (high-rate)
Channel.MAIN_CURRENT.sample_rate  # 4000 (theoretical max)
Channel.MAIN_CURRENT.unit         # 'A'
Channel.MAIN_CURRENT.label        # 'Main Current'
```

Strings work too at API boundaries:

```python
smu.enable_channels("mc", "mv")
```

## Save and reload

```python
from opensmu import Recording

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
from opensmu import SMU, Channel

with SMU.open() as smu:
    for sample in smu.stream(seconds=10.0):
        if sample.channel is Channel.MAIN_VOLTAGE:
            print(f"V = {sample.value:.4f}")
```

`smu.stream()` yields `Sample(timestamp, value, channel)` named tuples.
It cannot run concurrently with a recording.

## Error handling

```python
from opensmu import SMU
from opensmu.exceptions import SMUValueError, SMUCommandError

with SMU.open() as smu:
    try:
        smu.set_voltage(10.0)      # out of client-side range
    except SMUValueError as e:
        print("Client refused:", e)

    smu.set_range("low")
    try:
        smu.set_voltage(4.0)        # device will reject in low range
        import time; time.sleep(2)
        smu.set_voltage(3.25)       # this triggers raise of the queued error
    except SMUCommandError as e:
        print(f"Device rejected: err={e.error_code}, reverted to {e.last_good_value}")
```

## What's not in v0.1

See [`ROADMAP.md`](../ROADMAP.md) for the full list. Key items:

- **Battery emulation** (deferred to v0.2) — `set_supply_battery_emulator`,
  battery profiles, SoC tracking. Stubs raise `SMUNotImplementedError`.
- **Calibration** — deferred for safety.
- **Firmware upgrade** — intentionally never; use the vendor app.
- **Full-rate streaming** — the device's baseline stream is ~6 Hz; the
  command that unlocks 1 kHz / 4 kHz native rates hasn't been decoded
  yet. Sub-millisecond transients will be missed.
- **Multi-device coordination** — single device only for now (multiple
  independent instances work).

## CLI

```bash
opensmu discover                              # list devices
opensmu info                                  # show state
opensmu set-voltage 3.3                       # set V
opensmu set-output on                         # enable output
opensmu capture 5.0 run.csv -c mc mv          # record for 5 s
opensmu stream 10                             # live print
```

## Next steps

- [`docs/api_reference.md`](api_reference.md) — every class and method
- [`docs/protocol.md`](protocol.md) — what's on the USB cable
- [`examples/`](../examples/) — copy-paste-friendly scripts

# Installation

```bash
pip install benchctrl
```

That gets you the source-measure unit, the resistance standard, the two USB-HID
devices, the switched PDU, the battery subsystem, the recording format and the
command line. `pyserial` is the only hard dependency.

Everything else is an optional extra, and the extras exist because a bench host
is often a small board with no compiler.

## Extras

| Command | Adds |
|---|---|
| `pip install benchctrl` | Arc / Arc Pro, QR10x, ADU218, CP2112, PDU41002, battery, CLI |
| `pip install "benchctrl[bench-visa]"` | Rigol DL3031A, Rigol DP2031, Siglent SDM4065A (`pyvisa`) |
| `pip install "benchctrl[mcp]"` | the MCP server, for AI agents |
| `pip install "benchctrl[science]"` | numpy, pandas, Parquet, matplotlib |
| `pip install "benchctrl[dev]"` | pytest, ruff, mypy |

Combine them: `pip install "benchctrl[mcp,bench-visa,science]"`.

Output formats are **lazy imports**, so a missing extra costs you a specific
format rather than an import error at startup — `rec.save("x.opensmu")` works on
a base install, `rec.to_parquet()` tells you to install the extra.

Requires Python 3.9–3.13, on Windows, Linux or macOS.

## PyVISA needs a backend

`pip install "benchctrl[bench-visa]"` installs `pyvisa`, which is a front end.
It needs a VISA implementation underneath, and this is the step most likely to
cost you an afternoon:

| Backend | Notes |
|---|---|
| **Rigol Ultra Sigma** | bundles a working backend; easiest path on Windows |
| **NI-VISA** | free from ni.com; the most widely tested |
| **Keysight IO Libraries** | free |
| **pyvisa-py** | pure Python, plus a USB backend (libusb). Needs per-device Zadig setup on Windows |

On Linux, `pyvisa-py` + `pyusb` is usually the least trouble, and it is the only
option that works on a board with no vendor installer. See
[Setting up a bench host](bench-host-setup.md).

## Check it worked

With no instruments attached, use a simulator — this exercises the real driver,
the real transport and the real framing, so a pass means the install is sound:

```bash
benchctrl --sim ontrak_adu218 adu218 relay-states
```

```
benchctrl: ontrak_adu218 -> sim
relays: 0=off, 1=off, 2=off, 3=off, 4=off, 5=off, 6=off, 7=off
energised: -
mask: 0
```

The `-> sim` line on stderr is how you know it is not talking to hardware.
Silence there means every device resolved local.

With an instrument attached:

```bash
benchctrl arc info          # ask one instrument who it is
```

For a sweep of everything attached rather than one instrument, use the Python
entry point — the CLI's top-level `discover` is a legacy Arc-only command that
predates the other drivers and only reports source-measure units:

```python
from benchctrl import discovery
for d in discovery.discover():
    print(d)
```

Exit code 1 with no output means the command ran and found nothing — a distinct
outcome from a failure. See the [exit-code contract](../cli.md#exit-codes).

## Linux: device permissions

Serial instruments are `root:dialout 0660`, so your user needs to be in
`dialout`:

```bash
sudo usermod -aG dialout "$USER"     # log out and back in
```

That is enough for the source-measure unit and the PDU's serial console. Three
cases need more, and all three fail in ways that do not look like permissions:

| Instrument | Node | Needs |
|---|---|---|
| USB-TMC (both Rigols, the DMM) | `/dev/bus/usb/*` | `deploy/udev/61-benchctrl-usbtmc.rules` — **without it the instrument is invisible, not unopenable** |
| ADU218 | `/dev/bus/usb/*` | `deploy/udev/63-benchctrl-adu218.rules` |
| CP2112 | `/dev/hidraw*` | `deploy/udev/64-benchctrl-cp2112.rules` |

Install a rule and re-trigger — reloading alone changes nothing for a device
that is already plugged in:

```bash
sudo install -m 0644 deploy/udev/61-benchctrl-usbtmc.rules /etc/udev/rules.d/
sudo udevadm control --reload-rules
sudo udevadm trigger --action=add
```

Two warnings worth taking seriously:

- **Do not widen the CP2112 rule to all of `SUBSYSTEM=="hidraw"`.** On a
  typical machine that also matches the attached keyboard, which makes it a
  keylogging surface rather than a bench rule. The shipped rule is scoped to
  `10c4:ea90`.
- **A one-off `chmod` looks like it works and then stops.** The node is
  recreated on every replug with a different number.

Full reasoning for each rule is in [`deploy/README.md`](../../deploy/README.md).

### Why the USB-TMC case is the nasty one

Without that rule, `discover()` returns `[]` and the driver says *"no SDM4065A
found"* — indistinguishable from a bad cable. The cause is that `pyvisa-py`
reads the device's USB **string descriptors** to build a resource name, and that
read is a control transfer, so it fails and the device is dropped from
`list_resources()` entirely. If an instrument you can see in `lsusb` does not
appear in `discover()`, install the rule before you replace the cable.

## Windows

`pip install benchctrl` and go — the Arc and the QR10x enumerate as COM ports
with no extra setup.

For USB-TMC instruments, install **Rigol Ultra Sigma** (or NI-VISA) first; it
brings a working backend and the USB drivers with it. The pure-Python path works
too but needs Zadig per device.

## Development install

```bash
git clone https://github.com/rickmellor/benchctrl
cd benchctrl
pip install -e ".[dev,mcp,bench-visa,science]"
pytest -m "not hardware"
```

The hardware-free suite needs no instruments — it runs the production drivers
against wire-protocol simulators. See [Adding a driver](adding-a-driver.md).

## Bench host, and AI agents

- Running the instruments on a different machine than your code:
  [Local and remote mode](local-vs-remote.md), then
  [Setting up a bench host](bench-host-setup.md).
- Driving it from Claude Code or another MCP client:
  [Driving it from an AI agent](agent-harness.md).

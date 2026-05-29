# OpenSMU

Open-source Python control library for USB source-measurement units built
around the Qoitech Otii Arc / Arc Pro hardware.

Drives the device directly over its USB CDC-ACM interface — no vendor
server, no automation-toolbox license, no GUI required. Cross-platform
(Windows / Linux / macOS) via [pyserial](https://pyserial.readthedocs.io/).

## Status

v0.1 — covers everything a single-device user needs:

- Set / read main voltage, current limit, range, output enable
- Set 4-wire mode, source-current limit, ADC shunt resistor
- Expansion port: 5V, digital supply, GPO/GPI, TX/RX
- UART decoder enable + baud rate + TX write
- Per-channel measurement enable
- Start / stop recordings
- Statistics, per-channel data, CSV / JSON / native export
- Real-time streaming iterator
- Frame-aware error detection

Deferred to v0.2+: battery emulation, calibration, firmware upgrade,
sample-rate control. See [`ROADMAP.md`](ROADMAP.md).

## Install

```bash
pip install opensmu
```

For development:

```bash
git clone <repo>
cd opensmu
pip install -e ".[dev]"
```

## Quick start

```python
import time
from opensmu import SMU, Channel

with SMU.open() as smu:
    smu.set_voltage(3.3)
    smu.set_current_limit(1.0)
    smu.set_exp_voltage(3.3)
    smu.enable_channels(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE)

    with smu.record() as rec:
        smu.set_output(True)
        time.sleep(5)
        smu.set_output(False)

    print(rec.statistics(Channel.MAIN_CURRENT))
    rec.save_csv("run.csv")
```

## Documentation

- [Getting started](docs/getting_started.md) — installation, first capture
- [API reference](docs/api_reference.md) — every class and method
- [Wire protocol](docs/protocol.md) — what's on the USB cable
- [For AI assistants](docs/AGENTS.md) — context briefing for agent use
- [Design](docs/design.md) — architecture, decisions, layering
- [Roadmap](ROADMAP.md) — deferred features and rationale

## Licensing

MIT. See [`LICENSE`](LICENSE).

OpenSMU is an independent open-source project. It is not affiliated with,
endorsed by, or supported by Qoitech AB. "Otii", "Arc", and related marks
are trademarks of Qoitech AB.

## Acknowledgements

The wire protocol was reverse-engineered from passive observation of legitimate
USB traffic between a user's own hardware and software they had licensed. The
hardware enforces no license check on the wire; OpenSMU simply opens the
device's standard CDC-ACM endpoint, exactly as the operating system invites
any application to do.

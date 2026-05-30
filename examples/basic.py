"""Minimal end-to-end example.

Opens the first connected Arc, configures 3.3 V / 1 A limit, records the
main voltage + current for 5 seconds, prints summary statistics, and
saves a CSV.

Run:
    python examples/basic.py
"""

from __future__ import annotations

import time

from benchctrl.drivers.otii_arc.device import OtiiArc as SMU
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel


def main() -> None:
    with SMU.open() as smu:
        print(f"Opened {smu.info.port if smu.info else 'unknown port'}")

        smu.set_voltage(3.3)
        smu.set_current_limit(1.0)
        smu.set_exp_voltage(3.3)
        smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)

        with smu.record(name="basic-demo") as rec:
            print("Recording for 5 s ...")
            time.sleep(5.0)

        for ch in (Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT):
            stats = rec.statistics(ch)
            print(
                f"  {ch.code:>2}: "
                f"avg={stats.average:8.5f} {ch.unit}  "
                f"min={stats.min:8.5f}  max={stats.max:8.5f}  "
                f"n={stats.sample_count}"
            )

        out = rec.save_csv("basic-demo.csv", format="wide")
        print(f"Saved {out}")


if __name__ == "__main__":
    main()

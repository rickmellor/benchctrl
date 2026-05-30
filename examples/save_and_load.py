"""Capture, save to native .opensmu file, then load and re-analyse.

Demonstrates the round-trip — useful when you want to record once and
analyse many times.

Run:
    python examples/save_and_load.py
"""

from __future__ import annotations

import time

from benchctrl import SMU, Channel, Recording


def main() -> None:
    # Phase 1 — capture and save
    with SMU.open() as smu:
        smu.set_voltage(3.3)
        smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
        with smu.record(name="save-load-demo") as rec:
            time.sleep(2.0)
        rec.save("capture.opensmu")
        print(f"Captured {rec.count(Channel.MAIN_VOLTAGE)} mv samples, saved.")

    # Phase 2 — load and analyse without the SMU
    reloaded = Recording.load("capture.opensmu")
    stats = reloaded.statistics(Channel.MAIN_VOLTAGE)
    print(
        f"Reloaded: avg={stats.average:.4f} V  min={stats.min:.4f}  max={stats.max:.4f}"
    )

    reloaded.save_csv("capture.csv", format="wide")
    print("Wrote capture.csv")


if __name__ == "__main__":
    main()

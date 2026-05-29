"""Sweep main voltage from 0 V to 3.25 V and record current at each step.

WARNING: enables the output. Make sure your DUT can tolerate the swept
voltages before running.

Run:
    python examples/voltage_sweep.py
"""

from __future__ import annotations

import time

from opensmu import SMU, Channel

VOLTAGES = [0.0, 0.5, 1.0, 1.5, 2.0, 2.5, 3.0, 3.25]
DWELL_SECONDS = 1.0


def main() -> None:
    with SMU.open() as smu:
        smu.set_current_limit(1.0)
        smu.set_range("low")
        smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)

        smu.set_voltage(0.0)
        smu.set_output(True)
        try:
            with smu.record(name="voltage-sweep") as rec:
                for v in VOLTAGES:
                    smu.set_voltage(v)
                    print(f"  V_set = {v:.3f} V, dwelling {DWELL_SECONDS:.1f} s ...")
                    time.sleep(DWELL_SECONDS)
        finally:
            smu.set_output(False)
            smu.set_voltage(0.0)

        for v in VOLTAGES:
            # Slice around each step's dwell window for stats
            t0 = VOLTAGES.index(v) * DWELL_SECONDS
            t1 = t0 + DWELL_SECONDS
            i_stats = rec.statistics(Channel.MAIN_CURRENT, start=t0, end=t1)
            v_stats = rec.statistics(Channel.MAIN_VOLTAGE, start=t0, end=t1)
            print(
                f"  V_set={v:.2f}  V_meas={v_stats.average:.4f}"
                f"  I_avg={i_stats.average*1000:8.3f} mA"
            )

        rec.save_csv("voltage-sweep.csv", format="wide")
        print("Saved voltage-sweep.csv")


if __name__ == "__main__":
    main()

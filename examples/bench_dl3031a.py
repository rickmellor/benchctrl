"""Drive the Rigol DL3031A electronic load — including firmware LIST mode.

The DL3031A is an active programmable electronic load. CC / CV / CR /
CP modes plus firmware-side LIST, transient, and battery-discharge
modes that execute with sub-100 µs timing — perfect for actual TX
burst loads that USB-TMC round-trips can't keep up with.

Setup:
    DL3031A connected via USB-TMC. Requires benchctrl[bench-visa] for
    pyvisa, and a VISA backend (Rigol Ultra Sigma bundles one; NI-VISA
    or Keysight IO Libraries also work; pyvisa-py + libusb is the
    pure-Python option).

    Optional: an Arc Pro (or other voltage source) sourcing ~3.2 V so
    the load has something to sink. Without a source, V/I reads will
    be ~zero but the SCPI surface still exercises cleanly.

Run:
    python examples/bench_dl3031a.py                # auto-discover
    python examples/bench_dl3031a.py --resource 'USB0::0x1AB1::0x0E11::DL3D...::INSTR'

The example demonstrates four things:
  1. Static CC mode (set a current, read V/I)
  2. CR mode (the QR10x equivalent)
  3. Firmware LIST mode (sleep / TX / sleep at sub-100 ms TX widths)
  4. battery_stats() readback after a battery-discharge test
"""

from __future__ import annotations

import argparse
import time

from benchctrl.bench import RigolDL3031A


def demo_static_cc(dl: RigolDL3031A) -> None:
    print("=== CC mode ===")
    dl.set_mode("CC")
    dl.set_current_range(6.0)        # low range covers 6 A
    dl.set_current(0.030)             # 30 mA target
    dl.set_input(True)
    time.sleep(0.5)
    print(f"  V at terminals: {dl.fetch_voltage():.4f} V")
    print(f"  I sunk:         {dl.fetch_current() * 1000:.2f} mA")
    dl.set_input(False)


def demo_cr_mode(dl: RigolDL3031A) -> None:
    print("=== CR mode ===")
    dl.set_mode("CR")
    dl.set_resistance(100.0)          # 100 Ω equivalent
    dl.set_input(True)
    time.sleep(0.5)
    print(f"  V: {dl.fetch_voltage():.4f} V  I: {dl.fetch_current() * 1000:.2f} mA")
    dl.set_input(False)


def demo_list_mode(dl: RigolDL3031A) -> None:
    """Program an IoT-style sleep/TX/sleep pattern and play it in firmware."""
    print("=== LIST mode (firmware-timed sequence) ===")
    dl.reset()
    dl.clear_status()
    time.sleep(0.3)
    dl.set_voltage_range(36.0)
    dl.set_current_range(6.0)
    dl.program_list(
        steps=[
            (0.0001, 1.0),    # sleep: 100 µA for 1 s
            (0.030,  0.050),  # TX: 30 mA for 50 ms
            (0.0001, 1.0),    # sleep: 100 µA for 1 s
        ],
        mode="CC",
        count=3,              # repeat 3 times → 3 TX bursts
        range_value=6.0,
        slew_A_per_us=0.5,
        end_behavior="LAST",
        trigger_source="BUS",  # software trigger via dl.trigger_now()
    )
    print(f"  LIST programmed. FUNC:MODE = {dl.get_function_mode()}")
    dl.set_input(True)
    time.sleep(0.2)            # let arming settle
    print("  Triggering...")
    dl.trigger_now()
    time.sleep(3 * 2.05 + 0.5)  # play out
    dl.set_input(False)
    print("  Done. (LIST plays entirely in firmware — no per-step USB-TMC.)")


def demo_battery_discharge(dl: RigolDL3031A) -> None:
    """Configure a brief discharge test and read back firmware-side stats."""
    print("=== Battery discharge mode (firmware test) ===")
    dl.reset()
    dl.clear_status()
    time.sleep(0.3)
    dl.configure_battery_test(
        current_A=0.001,       # 1 mA discharge
        time_stop_s=3.0,       # stop after 3 s
        range_A=6.0,
    )
    print(f"  Configured. FUNC:MODE = {dl.get_function_mode()}")
    dl.set_input(True)
    for _ in range(4):
        time.sleep(0.8)
        stats = dl.battery_stats()
        # discharge_time_s is parsed from the device's H:MM:SS format
        print(
            f"    t={stats['discharge_time_s']:>4.1f}s  "
            f"V={stats['voltage_V']:.3f}V  "
            f"I={stats['current_A'] * 1000:+.2f}mA  "
            f"capacity={stats['capacity_mAh']:.4f} mAh"
        )
    dl.set_input(False)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--resource", default=None,
                        help="VISA resource string (auto-discover if omitted)")
    args = parser.parse_args()

    with RigolDL3031A.open(args.resource) as dl:
        info = dl.info()
        print(f"Connected: {info.model} S/N {info.serial}")
        print(f"  Firmware: {info.firmware}")
        print(f"  Resource: {info.resource}")
        print()
        demo_static_cc(dl)
        print()
        demo_cr_mode(dl)
        print()
        demo_list_mode(dl)
        print()
        demo_battery_discharge(dl)


if __name__ == "__main__":
    main()

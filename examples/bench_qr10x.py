"""Drive the Eastwood QR10x programmable resistor over USB-Serial.

The QR10x is a passive relay-network programmable resistance box.
Useful as a known load for SMU validation or as a sensor simulator.

Setup:
    Eastwood Tech QR10x (QR100, QR101) connected to a USB port. The
    device enumerates as a USB-Serial COM port. Identify the port
    from your OS (Windows: Device Manager; Linux/macOS: ls /dev/cu* or
    dmesg).

Run:
    python examples/bench_qr10x.py COM7
    python examples/bench_qr10x.py /dev/ttyUSB0

Safety: the QR10x's rated power is 1-2 W depending on output value.
Always set a safety limit appropriate to your source voltage:
    V (V)  | safe min R (Ω, 1 W rating)
    1.5    | 3
    3.2    | 12
    5.0    | 25
    12     | 150
"""

from __future__ import annotations

import argparse
import time

from benchctrl.bench import QR10x


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", help="USB-Serial port (e.g. COM7, /dev/ttyUSB0)")
    parser.add_argument("--safety-limit-ohm", type=float, default=12.0,
                        help="device-enforced minimum R (default 12 Ω = safe at 3.2 V / 1 W)")
    args = parser.parse_args()

    with QR10x.open(args.port) as qr:
        info = qr.info()
        print(f"Connected: {info.device_type} S/N {info.serial}")
        print(f"  Firmware: {info.firmware_version}")
        print(f"  TCR: {info.temperature_coefficient_ppm} ppm/°C")
        print(f"  Internal temperature: {qr.get_temperature():.2f} °C")

        qr.set_safety_limit(args.safety_limit_ohm)
        print(f"  RLIMIT set to {qr.get_safety_limit():.1f} Ω")
        print()

        # Sweep through a few representative values
        for setpoint in [100_000.0, 10_000.0, 1_000.0, 100.0]:
            qr.set_resistance(setpoint)
            time.sleep(0.2)              # let the relay network settle
            achieved = qr.actual_resistance()
            print(f"  set {setpoint:>8.1f} Ω → achieved {achieved:.4f} Ω")

        # Return to a safe high-impedance state
        qr.set_resistance(100_000.0)


if __name__ == "__main__":
    main()

"""Emulate a CR2032 against a DUT.

The Arc Pro acts as a battery: a 100 Hz control loop reads the load's
current, integrates SoC, looks up OCV and ESR from the profile's
discharge table, and writes V = OCV(SoC) - I*ESR(SoC) back to the
Arc. The DUT sees a cell that sags and drains exactly like the real
chemistry.

Setup:
    Arc Pro on USB. A DUT (or programmable load) wired across the
    Arc's main output. For the bench-validation case, the QR10x or
    DL3031A drivers in benchctrl.bench let you script the load too.

Run:
    python examples/battery_emulator.py path/to/CR2032-Energizer-(25).json

The bundled Otii profiles ship under:
    %LOCALAPPDATA%/otii3/app-*/resources/batteryprofiles      (Windows)
    ~/Library/Application Support/otii3/...                   (macOS)
"""

from __future__ import annotations

import argparse
import time
from pathlib import Path

from benchctrl import SMU
from benchctrl.battery import BatteryProfile, Emulator, EmulatorConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("profile_path", type=Path,
                        help="path to an Otii-format battery profile JSON")
    parser.add_argument("--initial-soc", type=float, default=1.0,
                        help="starting state of charge (0..1, default fresh)")
    parser.add_argument("--seconds", type=float, default=10.0,
                        help="how long to run the emulator (default 10 s)")
    args = parser.parse_args()

    profile = BatteryProfile.load(args.profile_path)
    print(f"Loaded profile: {profile.battery.manufacturer} "
          f"{profile.battery.model}")
    print(f"  Nominal voltage: {profile.nominal_voltage:.2f} V")
    print(f"  Nominal capacity: {profile.nominal_capacity_mAh:.0f} mAh")
    print(f"  Fresh OCV: {profile.ocv_at(0.0):.4f} V")

    config = EmulatorConfig(
        profile=profile,
        initial_soc=args.initial_soc,
        # Cap output to what the Arc can deliver. LiPo profiles whose
        # fresh OCV exceeds 4.2 V are clamped — Arc Pro's high-range
        # output tops out under load (KNOWN_LIMITATIONS § H-1).
        safety_max_voltage_V=min(
            profile.ocv_at(0.0) + 0.2,
            4.2 if profile.ocv_at(0.0) > 3.4 else 3.5,
        ),
        current_limit_A=0.5,
    )

    with SMU.open() as smu:
        emu = Emulator(smu, config)
        emu.start()
        print(f"Emulator running. Polling state every 1 s for {args.seconds:.0f} s...")
        try:
            t_end = time.monotonic() + args.seconds
            while time.monotonic() < t_end:
                time.sleep(1.0)
                st = emu.state()
                print(
                    f"  V_out={st.output_voltage_V:.4f}V  "
                    f"I={st.measured_current_A * 1000:+.2f}mA  "
                    f"SoC={st.soc * 100:.3f}%  "
                    f"OCV={st.ocv_V:.4f}V  ESR={st.esr_ohm:.3f}Ω"
                )
        finally:
            emu.stop()
            print("Emulator stopped, output disabled.")


if __name__ == "__main__":
    main()

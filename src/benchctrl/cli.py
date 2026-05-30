"""Command-line tool — ``python -m benchctrl <command>``.

Designed for quick one-shot operations from a shell. For anything
non-trivial, prefer the Python API.

Commands:
    discover         — list connected SMUs
    info             — show state of a connected SMU
    set-voltage V    — set main voltage
    set-output ON|OFF — toggle main output
    set-range low|high — set measurement range
    set-current-limit A — set current limit
    set-exp-voltage V — set expansion-port voltage
    set-gpo PIN ON|OFF — set GPO pin state
    capture SECONDS PATH — capture and save to .opensmu / .csv / .json
    stream SECONDS   — print a live tail of samples
"""

from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

from benchctrl.drivers.otii_arc.device import OtiiArc as SMU
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel
from benchctrl._version import __version__
from benchctrl.exceptions import BenchError


def _open_smu(args) -> SMU:
    if args.port:
        return SMU.open(args.port)
    return SMU.open()


def cmd_discover(args) -> int:
    devices = SMU.discover()
    if not devices:
        print("No Arc Pro found.")
        return 1
    for d in devices:
        sn = d.serial or "-"
        print(f"  {d.port:8} name={d.name!r:10} serial={sn}")
    return 0


def cmd_info(args) -> int:
    with _open_smu(args) as smu:
        info = smu.info
        if info is not None:
            print(f"Port:           {info.port}")
            print(f"Name:           {info.name}")
            if info.serial:
                print(f"Serial:         {info.serial}")
        # Drain a brief stream to populate cached values
        time.sleep(0.5)
        smu.read_raw(0.5)
        v = smu.version()
        if v:
            print(f"Version:        {v}")
        print(f"Connected:      {smu.is_connected}")
        print(f"Range:          {smu.range or '-'}")
        print(f"Voltage:        {smu.voltage if smu.voltage is not None else '-'} V")
        print(f"Current limit:  {smu.current_limit if smu.current_limit is not None else '-'} A")
        print(f"Output:         {smu.output_enabled}")
    return 0


def cmd_set_voltage(args) -> int:
    with _open_smu(args) as smu:
        smu.set_voltage(args.value)
    return 0


def cmd_set_output(args) -> int:
    enable = args.state.lower() in ("on", "true", "1", "enable")
    with _open_smu(args) as smu:
        smu.set_output(enable)
    return 0


def cmd_set_range(args) -> int:
    with _open_smu(args) as smu:
        smu.set_range(args.range)
    return 0


def cmd_set_current_limit(args) -> int:
    with _open_smu(args) as smu:
        smu.set_current_limit(args.value)
    return 0


def cmd_set_exp_voltage(args) -> int:
    with _open_smu(args) as smu:
        smu.set_exp_voltage(args.value)
    return 0


def cmd_set_gpo(args) -> int:
    enable = args.state.lower() in ("on", "true", "1", "enable")
    with _open_smu(args) as smu:
        smu.set_gpo(args.pin, enable)
    return 0


def cmd_capture(args) -> int:
    out_path = Path(args.path)
    suffix = out_path.suffix.lower()
    channels = [Channel.from_code(c) for c in (args.channels or ["mv", "mc"])]

    with _open_smu(args) as smu:
        smu.enable_channels(*channels)
        with smu.record(name=out_path.stem) as rec:
            time.sleep(args.seconds)
        for ch, buf in rec._buffers.items():
            print(f"  {ch.code}: {len(buf.values)} samples ({ch.label}, {ch.unit})")
        if suffix == ".csv":
            rec.save_csv(out_path, format=args.format)
        elif suffix == ".json":
            rec.save_json(out_path)
        elif suffix == ".opensmu":
            rec.save(out_path)
        else:
            print(f"  unknown extension {suffix!r}, saving as native .opensmu")
            out_path = out_path.with_suffix(".opensmu")
            rec.save(out_path)
        print(f"Saved to {out_path}")
    return 0


def cmd_stream(args) -> int:
    deadline = time.monotonic() + args.seconds
    with _open_smu(args) as smu:
        for sample in smu.stream(seconds=args.seconds):
            print(
                f"t={sample.timestamp:7.3f} s  "
                f"{sample.channel.code} = {sample.value:10.6f} {sample.channel.unit}"
            )
            if time.monotonic() > deadline:
                break
    return 0


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="benchctrl",
        description="benchctrl command-line tool",
    )
    p.add_argument("--version", action="version", version=__version__)
    p.add_argument("--port", help="serial port (e.g. COM6, /dev/ttyACM0)")
    sub = p.add_subparsers(dest="command", required=True)

    sub.add_parser("discover", help="list connected SMUs").set_defaults(
        func=cmd_discover
    )
    sub.add_parser("info", help="show device state").set_defaults(func=cmd_info)

    sv = sub.add_parser("set-voltage", help="set main voltage (V)")
    sv.add_argument("value", type=float)
    sv.set_defaults(func=cmd_set_voltage)

    so = sub.add_parser("set-output", help="set main output enable")
    so.add_argument("state", choices=["on", "off"])
    so.set_defaults(func=cmd_set_output)

    sr = sub.add_parser("set-range", help="set measurement range")
    sr.add_argument("range", choices=["low", "high"])
    sr.set_defaults(func=cmd_set_range)

    scl = sub.add_parser("set-current-limit", help="set current limit (A)")
    scl.add_argument("value", type=float)
    scl.set_defaults(func=cmd_set_current_limit)

    sxv = sub.add_parser("set-exp-voltage", help="set expansion-port voltage (V)")
    sxv.add_argument("value", type=float)
    sxv.set_defaults(func=cmd_set_exp_voltage)

    sg = sub.add_parser("set-gpo", help="set GPO pin state")
    sg.add_argument("pin", type=int, choices=[1, 2])
    sg.add_argument("state", choices=["on", "off"])
    sg.set_defaults(func=cmd_set_gpo)

    cap = sub.add_parser("capture", help="record and save samples")
    cap.add_argument("seconds", type=float, help="recording duration")
    cap.add_argument(
        "path", help="output path (extension determines format: .opensmu/.csv/.json)"
    )
    cap.add_argument(
        "--channels", "-c", nargs="*", help="channel codes (default: mv mc)"
    )
    cap.add_argument(
        "--format", default="long", choices=["long", "wide"], help="CSV format"
    )
    cap.set_defaults(func=cmd_capture)

    st = sub.add_parser("stream", help="print live samples")
    st.add_argument("seconds", type=float, help="stream duration")
    st.set_defaults(func=cmd_stream)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.func(args)
    except BenchError as exc:
        print(f"benchctrl error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("interrupted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())

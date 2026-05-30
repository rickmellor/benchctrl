"""Live streaming — print samples as they arrive.

Run:
    python examples/streaming.py
"""

from __future__ import annotations

from benchctrl import SMU


def main() -> None:
    with SMU.open() as smu:
        print(f"Streaming for 10 s from {smu.info.port if smu.info else '?'}")
        for sample in smu.stream(seconds=10.0):
            print(
                f"  t={sample.timestamp:6.3f} s  "
                f"{sample.channel.code} = {sample.value:10.6f} {sample.channel.unit}"
            )


if __name__ == "__main__":
    main()

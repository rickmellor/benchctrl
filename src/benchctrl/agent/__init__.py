"""Board-side agent: serves local instruments over the network.

Runs as a native systemd service, not an App Lab app — App Lab containers
cannot reach ``/dev/ttyUSB*`` or ``/dev/ttyACM*``, and device passthrough is
brick-level and limited to camera/microphone/speaker classes.

Stdlib + pyserial + benchctrl only.
"""

from __future__ import annotations

__all__ = [
    "blobs",
    "dispatch",
    "recordings",
    "registry",
    "safety",
    "server",
    "worker",
]

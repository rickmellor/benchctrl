"""Qoitech Otii Arc / Arc Pro driver.

Exposes:

- :py:class:`OtiiArc` — the main device class. Source / measure / record.
- :py:class:`OtiiArcChannel` — the Arc's full channel inventory.
- :py:class:`OtiiArcInfo` — discovery-time descriptor.
- :py:class:`ChannelInfo` — per-channel static metadata.
- :py:class:`PortInfo` — pyserial port descriptor.

Implementation modules ``protocol``, ``transport``, ``channels`` are
internal to this driver but importable for advanced use.

Implements :py:class:`benchctrl.interfaces.SourceMeasurementUnit`.
"""

from benchctrl.drivers.otii_arc.channels import (
    ChannelInfo,
    OtiiArcChannel,
)
from benchctrl.drivers.otii_arc.device import OtiiArc, OtiiArcInfo
from benchctrl.drivers.otii_arc.transport import PortInfo

__all__ = [
    "OtiiArc",
    "OtiiArcChannel",
    "OtiiArcInfo",
    "ChannelInfo",
    "PortInfo",
]

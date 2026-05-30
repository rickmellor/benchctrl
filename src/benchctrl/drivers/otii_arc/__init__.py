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

The :py:class:`OtiiArc` / :py:class:`OtiiArcInfo` / :py:class:`PortInfo`
classes are exposed via PEP 562 lazy attribute lookup. This means
``from benchctrl.drivers.otii_arc.channels import OtiiArcChannel``
doesn't trigger ``device.py`` to load — and therefore doesn't pull in
:py:class:`benchctrl.recording.Recording` (which would be a circular
import while ``recording.py`` itself is loading).
"""

from typing import TYPE_CHECKING

from benchctrl.drivers.otii_arc.channels import ChannelInfo, OtiiArcChannel

if TYPE_CHECKING:
    from benchctrl.drivers.otii_arc.device import OtiiArc, OtiiArcInfo
    from benchctrl.drivers.otii_arc.transport import PortInfo

__all__ = [
    "OtiiArc",
    "OtiiArcChannel",
    "OtiiArcInfo",
    "ChannelInfo",
    "PortInfo",
]


_LAZY = {
    "OtiiArc": ("benchctrl.drivers.otii_arc.device", "OtiiArc"),
    "OtiiArcInfo": ("benchctrl.drivers.otii_arc.device", "OtiiArcInfo"),
    "PortInfo": ("benchctrl.drivers.otii_arc.transport", "PortInfo"),
}


def __getattr__(name):
    if name in _LAZY:
        import importlib
        module_name, attr_name = _LAZY[name]
        module = importlib.import_module(module_name)
        value = getattr(module, attr_name)
        globals()[name] = value
        return value
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")

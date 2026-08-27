"""Silicon Labs CP2112 — open-drain control lines (hardware reset) over HID.

Implements **no Protocol** from :py:mod:`benchctrl.interfaces`, and that is
deliberate rather than an omission. ``CONTRIBUTING.md`` convention 3 defines a
Protocol when the *second* instance of a shape lands; this is the bench's first
digital-control device, everything else being a source, sink or meter. A
``ControlLine`` Protocol generalised from one sample would bake in CP2112
specifics — whole-port configuration registers, and a USB round trip per
transition — that a real GPIO expander or a relay board would not share.
``rigol_dp2031/__init__.py`` is the precedent for declaring non-conformance in
the docstring instead of implying it.

Only the GPIO half of the chip is implemented; the SMBus/I2C bridge is out of
scope. See ``KNOWN_LIMITATIONS.md``.
"""

from benchctrl.drivers.silabs_cp2112.driver import (
    ALTERNATE_FUNCTIONS,
    LINE_COUNT,
    MIN_PULSE_S,
    PACKAGE_PINS,
    PART_NUMBER_CP2112,
    CP2112,
    CP2112ConnectionError,
    CP2112Error,
    CP2112GpioConfig,
    CP2112Info,
    CP2112LineState,
    CP2112PolicyError,
    CP2112ProtocolError,
    CP2112ValueError,
    CP2112VerifyError,
)
from benchctrl.drivers.silabs_cp2112.hidraw import (
    PRODUCT_ID,
    VENDOR_ID,
    HidrawError,
    HidrawLink,
    find_hidraw_nodes,
    read_serial,
)

__all__ = [
    "ALTERNATE_FUNCTIONS",
    "CP2112",
    "CP2112ConnectionError",
    "CP2112Error",
    "CP2112GpioConfig",
    "CP2112Info",
    "CP2112LineState",
    "CP2112PolicyError",
    "CP2112ProtocolError",
    "CP2112ValueError",
    "CP2112VerifyError",
    "HidrawError",
    "HidrawLink",
    "LINE_COUNT",
    "MIN_PULSE_S",
    "PACKAGE_PINS",
    "PART_NUMBER_CP2112",
    "PRODUCT_ID",
    "VENDOR_ID",
    "find_hidraw_nodes",
    "read_serial",
]

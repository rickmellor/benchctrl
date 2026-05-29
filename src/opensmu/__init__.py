"""OpenSMU — direct USB control for source-measurement units.

Public API surface. Internal modules are not re-exported here.

Typical use:

    >>> import time
    >>> from opensmu import SMU, Channel
    >>> with SMU.open() as smu:
    ...     smu.set_voltage(3.3)
    ...     smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    ...     with smu.record() as rec:
    ...         smu.set_output(True)
    ...         time.sleep(2)
    ...         smu.set_output(False)
    ...     print(rec.statistics(Channel.MAIN_CURRENT))
"""

from opensmu._version import __version__
from opensmu.channels import Channel, ChannelInfo
from opensmu.device import SMU, SMUInfo
from opensmu.exceptions import (
    SMUCommandError,
    SMUConnectionError,
    SMUError,
    SMUNotImplementedError,
    SMUProtocolError,
    SMUTimeoutError,
    SMUValueError,
)
from opensmu.recording import ChannelInfoResult, Recording, Statistics
from opensmu.samples import Sample

__all__ = [
    "__version__",
    "SMU",
    "SMUInfo",
    "Channel",
    "ChannelInfo",
    "Recording",
    "Statistics",
    "ChannelInfoResult",
    "Sample",
    "SMUError",
    "SMUConnectionError",
    "SMUProtocolError",
    "SMUCommandError",
    "SMUValueError",
    "SMUTimeoutError",
    "SMUNotImplementedError",
]

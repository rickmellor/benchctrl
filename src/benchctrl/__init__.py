"""benchctrl — direct USB control for source-measurement units.

Public API surface. Internal modules are not re-exported here.

Typical use:

    >>> import time
    >>> from benchctrl import SMU, Channel
    >>> with SMU.open() as smu:
    ...     smu.set_voltage(3.3)
    ...     smu.enable_channels(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT)
    ...     with smu.record() as rec:
    ...         smu.set_output(True)
    ...         time.sleep(2)
    ...         smu.set_output(False)
    ...     print(rec.statistics(Channel.MAIN_CURRENT))
"""

from benchctrl._version import __version__
from benchctrl.channels import Channel, ChannelInfo
from benchctrl.device import SMU, SMUInfo
from benchctrl.exceptions import (
    BenchCommandError,
    BenchConnectionError,
    BenchError,
    BenchNotImplementedError,
    BenchProtocolError,
    BenchTimeoutError,
    BenchValueError,
)
from benchctrl.recording import ChannelInfoResult, Recording
from benchctrl.samples import Sample, Statistics

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
    "BenchError",
    "BenchConnectionError",
    "BenchProtocolError",
    "BenchCommandError",
    "BenchValueError",
    "BenchTimeoutError",
    "BenchNotImplementedError",
]

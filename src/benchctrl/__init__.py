"""benchctrl — open framework for source-measurement and companion bench instruments.

The top-level ``benchctrl`` namespace contains only **framework primitives**:

- :py:class:`Recording` — captured sample timeline + statistics + exports
- :py:class:`StandardChannel` — minimal common channel inventory
- :py:class:`BenchError` and subclasses — generic exception hierarchy
- :py:class:`Sample`, :py:class:`Statistics` — measurement value types
- :py:class:`SourceMeasurementUnit` — abstract Protocol for any SMU

Concrete instrument drivers live under :py:mod:`benchctrl.drivers`
and are imported by full path:

    >>> from benchctrl.drivers.otii_arc import OtiiArc
    >>> from benchctrl.drivers.eastwood_qr10x import QR10x
    >>> from benchctrl.drivers.rigol_dl3031a import RigolDL3031A

Typical use:

    >>> import time
    >>> from benchctrl import StandardChannel
    >>> from benchctrl.drivers.otii_arc import OtiiArc
    >>> with OtiiArc.open() as smu:
    ...     smu.set_voltage(3.3)
    ...     smu.enable_channels(StandardChannel.MAIN_VOLTAGE,
    ...                         StandardChannel.MAIN_CURRENT)
    ...     with smu.record() as rec:
    ...         smu.set_output(True)
    ...         time.sleep(2)
    ...         smu.set_output(False)
    ...     print(rec.statistics(StandardChannel.MAIN_CURRENT))
"""

from benchctrl._version import __version__
from benchctrl.channels import StandardChannel
from benchctrl.exceptions import (
    BenchCommandError,
    BenchConnectionError,
    BenchError,
    BenchNotImplementedError,
    BenchProtocolError,
    BenchTimeoutError,
    BenchValueError,
)
from benchctrl.interfaces import ChannelLike, SourceMeasurementUnit
from benchctrl.recording import ChannelInfoResult, Recording
from benchctrl.samples import Sample, Statistics

__all__ = [
    "__version__",
    "Recording",
    "Statistics",
    "Sample",
    "ChannelInfoResult",
    "StandardChannel",
    "SourceMeasurementUnit",
    "ChannelLike",
    "BenchError",
    "BenchConnectionError",
    "BenchProtocolError",
    "BenchCommandError",
    "BenchValueError",
    "BenchTimeoutError",
    "BenchNotImplementedError",
]

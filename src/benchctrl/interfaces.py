"""Abstract device interfaces (Protocols).

Protocols defined here describe the minimal surface a higher-level
subsystem (e.g. the battery emulator) needs from a category of bench
instrument. Concrete drivers under ``benchctrl.drivers.*`` implement
the relevant protocol(s); the subsystems depend on the protocol, not
on any specific driver.

Currently defined:

- :py:class:`SourceMeasurementUnit` — a device that both sources V/I
  AND measures V/I (Otii Arc, future Keithley / Keysight SMUs, etc.).
  :py:class:`benchctrl.drivers.otii_arc.OtiiArc` implements this
  protocol.

Deliberately deferred until the first concrete instance lands:

- ``Source`` (pure-source: programmable supplies, DACs in source mode)
- ``Sink`` (pure-sink: electronic loads — QR10x and DL3031A keep
  their concrete surfaces since they're quite different from each
  other)
- ``Switch`` (programmable relays / multiplexers)
- ``Measurement`` (pure-measurement: DMMs, scopes)

Pre-defining unused protocols is overhead without payoff. Add them
when the first driver in each category arrives.
"""

from __future__ import annotations

from contextlib import AbstractContextManager
from typing import TYPE_CHECKING, Iterable, Protocol, Union, runtime_checkable

from benchctrl.channels import StandardChannel

# Recording is imported under TYPE_CHECKING to avoid a circular at
# package import time: benchctrl/__init__.py → interfaces →
# Recording → drivers.otii_arc.channels → drivers.otii_arc/__init__ →
# device → Recording (not yet fully loaded). The Protocol surface
# only mentions Recording in a return-type annotation, which works
# fine with `from __future__ import annotations`.
if TYPE_CHECKING:
    from benchctrl.recording import Recording

#: A channel reference accepted by Protocol methods. ``StandardChannel``
#: is the framework-level enum (``MAIN_CURRENT`` / ``MAIN_VOLTAGE`` /
#: ``MAIN_POWER``). The two-letter codes (``"mc"`` / ``"mv"`` / ``"mp"``)
#: also work. Driver-specific enums (e.g. ``OtiiArcChannel``) carry the
#: same codes for the standard subset, so passing one through is
#: equivalent.
ChannelLike = Union[StandardChannel, str]


@runtime_checkable
class SourceMeasurementUnit(Protocol):
    """The minimal contract a battery emulator (and similar
    higher-level consumers) needs from any SMU.

    Implementations:

    - :py:class:`benchctrl.drivers.otii_arc.OtiiArc` (the Otii Arc /
      Arc Pro).

    The class is decorated ``@runtime_checkable`` so callers can do
    ``isinstance(smu, SourceMeasurementUnit)`` for diagnostics, but
    static typing is the preferred mode of use.

    Naming convention notes:

    - **Methods are setters / getters / actions.** Reads of cached
      state are properties (not part of this Protocol — properties
      vary too much across vendors to standardize).
    - **Units are SI** unless otherwise noted: amperes, volts, watts,
      seconds, ohms.
    """

    # ---- Source-side configuration ---------------------------------

    def set_voltage(self, volts: float) -> None:
        """Set the main output voltage."""

    def set_main_current(self, amps: float) -> None:
        """Set the CC-mode source/sink current setpoint."""

    def set_output(self, enable: bool) -> None:
        """Enable or disable the main output."""

    def set_range(self, range_: str) -> None:
        """Pick the measurement range.

        Common values are ``"low"`` and ``"high"``; vendor-specific
        ranges are allowed by passing the vendor's range identifier.
        """

    def set_power_regulation(self, mode: str) -> None:
        """Pick the regulation mode.

        Common modes: ``"voltage"`` (CV), ``"current"`` (CC),
        ``"inline"`` (Otii-specific), ``"off"``.
        """

    def set_current_limit(self, amps: float) -> None:
        """Set the over-current trip threshold."""

    def set_current_limit_enabled(self, enable: bool) -> None:
        """Arm or disarm the over-current trip."""

    # ---- Measurement -----------------------------------------------

    def read_value(self, channel: ChannelLike, timeout: float = 1.5) -> float:
        """Block up to ``timeout`` seconds for the next sample on
        ``channel`` and return its value."""

    def read_window(
        self,
        channels: Iterable[ChannelLike],
        duration_s: float,
    ) -> dict:
        """Drain ``duration_s`` seconds of incoming samples and return
        a dict keyed by channel with a list of values per channel."""

    def record(
        self,
        *channels: ChannelLike,
        name: str = "recording",
    ) -> AbstractContextManager[Recording]:
        """Context manager for time-bounded recording. The yielded
        :py:class:`Recording` carries the captured samples."""

    # ---- Identity --------------------------------------------------

    def get_fw_version(self) -> str:
        """Query the device firmware version string."""

    def get_device_id(self) -> str:
        """Query the device serial / identifier."""

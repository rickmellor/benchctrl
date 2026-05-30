"""Abstract device interfaces (Protocols).

Protocols defined here describe the minimal surface a higher-level
subsystem (e.g. the battery emulator) needs from a category of bench
instrument. Concrete drivers under ``benchctrl.drivers.*`` implement
the relevant protocol(s); the subsystems depend on the protocol, not
on any specific driver.

Currently defined:

- :py:class:`SourceMeasurementUnit` — a device that both sources V/I
  AND measures V/I (Otii Arc, future Keithley / Keysight SMUs, etc.).
  The Otii Arc class (``benchctrl.drivers.otii_arc.OtiiArc`` — still
  ``benchctrl.SMU`` until Phase 4 of the v1.0 refactor lands)
  implements this protocol.

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
from typing import Iterable, Protocol, Union, runtime_checkable

# Import lazily-typed references — the Channel enum is currently
# benchctrl.channels.Channel (Arc-specific). v1.0 phase 4 splits this
# into a top-level StandardChannel + per-driver enums. For now the
# Protocol accepts the Channel enum or a string code.
from benchctrl.channels import Channel
from benchctrl.recording import Recording

#: A channel reference — the canonical enum, or its two-letter code
#: (``"mc"`` for MAIN_CURRENT, etc.). Future per-driver Channel enums
#: that include the standard members will also satisfy this hint.
ChannelLike = Union[Channel, str]


@runtime_checkable
class SourceMeasurementUnit(Protocol):
    """The minimal contract a battery emulator (and similar
    higher-level consumers) needs from any SMU.

    Implementations:

    - ``benchctrl.SMU`` (the Otii Arc / Arc Pro) — phase-4-rename will
      land this as ``benchctrl.drivers.otii_arc.OtiiArc``.

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

"""Exception hierarchy for OpenSMU.

    SMUError                       — base
    ├── SMUConnectionError         — port can't be opened / lost mid-stream
    ├── SMUProtocolError           — bad magic / bad checksum / unexpected frame
    ├── SMUCommandError            — device rejected a SET (carries error_code, last_good_value)
    ├── SMUValueError              — client-side range check failed before send
    ├── SMUTimeoutError            — no samples within deadline
    └── SMUNotImplementedError     — deferred feature
"""

from __future__ import annotations


class SMUError(Exception):
    """Base class for all OpenSMU exceptions."""


class SMUConnectionError(SMUError):
    """Failed to open the serial port, or lost connection mid-stream."""


class SMUProtocolError(SMUError):
    """Received bytes that don't match the on-wire protocol (bad magic,
    bad checksum, truncated frame, unknown frame type when one was expected)."""


class SMUCommandError(SMUError, RuntimeError):
    """Device explicitly rejected a SET command.

    Attributes:
        error_code: Signed integer reported by the device.
        last_good_value: Value the parameter reverted to after the rejection.
        command_code: Which SET command was rejected (0x05 for 4-wire, etc.).
    """

    def __init__(
        self,
        error_code: int,
        last_good_value: int,
        command_code: int | None = None,
        message: str | None = None,
    ) -> None:
        self.error_code = error_code
        self.last_good_value = last_good_value
        self.command_code = command_code
        if message is None:
            cmd_text = f" cmd=0x{command_code:02X}" if command_code is not None else ""
            message = (
                f"device rejected SET{cmd_text}: error_code={error_code}, "
                f"reverted to last_good_value={last_good_value}"
            )
        super().__init__(message)


class SMUValueError(SMUError, ValueError):
    """A parameter failed a client-side range check before being sent."""


class SMUTimeoutError(SMUError, TimeoutError):
    """No samples or expected response arrived within the deadline."""


class SMUNotImplementedError(SMUError, NotImplementedError):
    """A deferred feature was invoked. See ROADMAP.md."""

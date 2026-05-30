"""Exception hierarchy for benchctrl.

    BenchError                       — base
    ├── BenchConnectionError         — port can't be opened / lost mid-stream
    ├── BenchProtocolError           — bad magic / bad checksum / unexpected frame
    ├── BenchCommandError            — device rejected a SET (carries error_code, last_good_value)
    ├── BenchValueError              — client-side range check failed before send
    ├── BenchTimeoutError            — no samples within deadline
    └── BenchNotImplementedError     — deferred feature

The same hierarchy applies to any bench instrument the framework drives
(Otii Arc, future SMUs, etc.). Driver-specific instruments that own a
distinct exception hierarchy (QR10x, RigolDL3031A) keep their own
top-level classes — those predate the framework rename and are scoped
to the vendor.
"""

from __future__ import annotations


class BenchError(Exception):
    """Base class for all benchctrl exceptions."""


class BenchConnectionError(BenchError):
    """Failed to open the transport, or lost connection mid-stream."""


class BenchProtocolError(BenchError):
    """Received bytes that don't match the on-wire protocol (bad magic,
    bad checksum, truncated frame, unknown frame type when one was expected)."""


class BenchCommandError(BenchError, RuntimeError):
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


class BenchValueError(BenchError, ValueError):
    """A parameter failed a client-side range check before being sent."""


class BenchTimeoutError(BenchError, TimeoutError):
    """No samples or expected response arrived within the deadline."""


class BenchNotImplementedError(BenchError, NotImplementedError):
    """A deferred feature was invoked. See ROADMAP.md."""

"""Exception hierarchy + attribute carrying."""

from __future__ import annotations

import pytest

from opensmu.exceptions import (
    SMUCommandError,
    SMUConnectionError,
    SMUError,
    SMUNotImplementedError,
    SMUProtocolError,
    SMUTimeoutError,
    SMUValueError,
)


def test_hierarchy_under_smu_error():
    for cls in (
        SMUConnectionError,
        SMUProtocolError,
        SMUCommandError,
        SMUValueError,
        SMUTimeoutError,
        SMUNotImplementedError,
    ):
        assert issubclass(cls, SMUError)


def test_value_error_is_value_error():
    assert issubclass(SMUValueError, ValueError)
    with pytest.raises(ValueError):
        raise SMUValueError("nope")


def test_timeout_error_is_timeout_error():
    assert issubclass(SMUTimeoutError, TimeoutError)
    with pytest.raises(TimeoutError):
        raise SMUTimeoutError("nope")


def test_command_error_carries_fields():
    err = SMUCommandError(error_code=-101, last_good_value=3_000_000, command_code=0x0B)
    assert err.error_code == -101
    assert err.last_good_value == 3_000_000
    assert err.command_code == 0x0B
    # Default message format
    s = str(err)
    assert "-101" in s
    assert "3000000" in s
    assert "0x0B" in s


def test_command_error_custom_message():
    err = SMUCommandError(error_code=-1, last_good_value=0, message="explicit")
    assert str(err) == "explicit"


def test_not_implemented_subclass():
    with pytest.raises(NotImplementedError):
        raise SMUNotImplementedError("deferred")

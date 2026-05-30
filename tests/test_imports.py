"""Verify every public symbol is importable from the package root."""

from __future__ import annotations

import benchctrl


def test_version_string():
    assert isinstance(benchctrl.__version__, str)
    assert benchctrl.__version__.count(".") == 2  # semver MAJOR.MINOR.PATCH


def test_all_public_names_resolve():
    for name in benchctrl.__all__:
        assert hasattr(benchctrl, name), f"{name} declared in __all__ but not exported"


def test_top_level_classes():
    # Spot-check the headliners
    from benchctrl import (
        SMU,
        Channel,
        ChannelInfoResult,
        Recording,
        Sample,
        SMUCommandError,
        SMUConnectionError,
        SMUError,
        SMUNotImplementedError,
        SMUProtocolError,
        SMUTimeoutError,
        SMUValueError,
        Statistics,
    )
    assert SMU
    assert Channel
    assert ChannelInfoResult
    assert Recording
    assert Sample
    assert SMUCommandError and SMUConnectionError and SMUError
    assert SMUNotImplementedError and SMUProtocolError
    assert SMUTimeoutError and SMUValueError
    assert Statistics

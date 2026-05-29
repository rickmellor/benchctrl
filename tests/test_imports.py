"""Verify every public symbol is importable from the package root."""

from __future__ import annotations

import opensmu


def test_version_string():
    assert isinstance(opensmu.__version__, str)
    assert opensmu.__version__.count(".") == 2  # semver MAJOR.MINOR.PATCH


def test_all_public_names_resolve():
    for name in opensmu.__all__:
        assert hasattr(opensmu, name), f"{name} declared in __all__ but not exported"


def test_top_level_classes():
    # Spot-check the headliners
    from opensmu import (
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

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
        BenchCommandError,
        BenchConnectionError,
        BenchError,
        BenchNotImplementedError,
        BenchProtocolError,
        BenchTimeoutError,
        BenchValueError,
        Statistics,
    )
    assert SMU
    assert Channel
    assert ChannelInfoResult
    assert Recording
    assert Sample
    assert BenchCommandError and BenchConnectionError and BenchError
    assert BenchNotImplementedError and BenchProtocolError
    assert BenchTimeoutError and BenchValueError
    assert Statistics

"""Verify every public symbol is importable from the package root."""

from __future__ import annotations

import benchctrl


def test_version_string():
    assert isinstance(benchctrl.__version__, str)
    assert benchctrl.__version__.count(".") == 2  # semver MAJOR.MINOR.PATCH


def test_all_public_names_resolve():
    for name in benchctrl.__all__:
        assert hasattr(benchctrl, name), f"{name} declared in __all__ but not exported"


def test_top_level_framework_primitives():
    """Top-level benchctrl re-exports only framework primitives.
    Driver classes (OtiiArc, QR10x, RigolDL3031A) are imported by
    full path under benchctrl.drivers.*."""
    from benchctrl import (
        ChannelInfoResult,
        Recording,
        Sample,
        SourceMeasurementUnit,
        StandardChannel,
        Statistics,
        BenchCommandError,
        BenchConnectionError,
        BenchError,
        BenchNotImplementedError,
        BenchProtocolError,
        BenchTimeoutError,
        BenchValueError,
    )
    assert ChannelInfoResult
    assert Recording
    assert Sample
    assert SourceMeasurementUnit
    assert StandardChannel
    assert Statistics
    assert BenchCommandError and BenchConnectionError and BenchError
    assert BenchNotImplementedError and BenchProtocolError
    assert BenchTimeoutError and BenchValueError


def test_top_level_does_not_leak_driver_classes():
    """Verify driver classes are NOT at the top level — they must be
    imported by full path."""
    import benchctrl
    for name in ("SMU", "OtiiArc", "Channel", "OtiiArcChannel", "QR10x",
                 "RigolDL3031A", "SMUInfo", "OtiiArcInfo", "PortInfo"):
        assert not hasattr(benchctrl, name), (
            f"{name} should not be at benchctrl top level — import from "
            f"benchctrl.drivers.* instead"
        )


def test_otii_arc_driver_importable():
    """The Arc driver classes are importable by full path."""
    from benchctrl.drivers.otii_arc import (
        OtiiArc,
        OtiiArcChannel,
        OtiiArcInfo,
        ChannelInfo,
        PortInfo,
    )
    assert OtiiArc and OtiiArcChannel and OtiiArcInfo
    assert ChannelInfo and PortInfo

"""Smoke tests for the abstract interface Protocols.

Verifies that the existing concrete drivers satisfy the protocols
they're meant to implement. The Protocols are intentionally narrow
(just the surface a battery emulator or validation harness needs);
this test ensures we don't accidentally regress that contract during
driver refactors.
"""
from __future__ import annotations

import inspect

import pytest

from benchctrl.interfaces import SourceMeasurementUnit


def _protocol_members(proto: type) -> set[str]:
    """Return the names of the methods/attrs the Protocol declares,
    skipping dunder and the standard Protocol-machinery members."""
    members = {
        name for name, _ in inspect.getmembers(proto, predicate=inspect.isfunction)
        if not name.startswith("_")
    }
    # Inherited from typing.Protocol — not part of our contract
    members.discard("__init_subclass__")
    return members


def test_otii_arc_class_implements_source_measurement_unit():
    """The Arc class (currently benchctrl.SMU; will become
    benchctrl.drivers.otii_arc.OtiiArc in phase 4) must expose every
    method the SourceMeasurementUnit Protocol declares."""
    from benchctrl import SMU
    required = _protocol_members(SourceMeasurementUnit)
    missing = [name for name in required if not hasattr(SMU, name)]
    assert not missing, (
        f"SMU is missing Protocol methods: {missing}. "
        f"Either add them, or remove them from the Protocol."
    )


def test_source_measurement_unit_is_runtime_checkable():
    """isinstance(x, SourceMeasurementUnit) should work for diagnostics
    even though static typing is the preferred mode."""
    # We can't instantiate SMU without hardware, so check the protocol
    # itself is runtime_checkable by attempting isinstance against an
    # obviously-wrong object — it should return False, not raise.
    assert isinstance(SourceMeasurementUnit, type)
    assert isinstance(None, SourceMeasurementUnit) is False


@pytest.mark.parametrize(
    "method",
    sorted(_protocol_members(SourceMeasurementUnit)),
)
def test_each_protocol_method_documented(method):
    """Every Protocol method must have a docstring — they're the
    contract drivers commit to honoring."""
    func = getattr(SourceMeasurementUnit, method)
    assert func.__doc__ and func.__doc__.strip(), (
        f"SourceMeasurementUnit.{method} has no docstring"
    )

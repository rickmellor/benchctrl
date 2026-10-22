"""Shared pytest fixtures.

The hardware-required tier needs an Arc Pro on COM6. If none is present,
those tests skip with a clear message instead of failing.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

import pytest

# Make `src/benchctrl` importable when running pytest from the project root
# even before `pip install -e .` is run.
ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


@pytest.fixture
def smu():
    """Open a real Arc Pro for the duration of the test.

    Skips the test if no device is found rather than failing.
    """
    from benchctrl.drivers.otii_arc.device import OtiiArc as SMU
    from benchctrl.exceptions import BenchConnectionError

    try:
        devices = SMU.discover()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"discovery failed: {exc}")
    if not devices:
        pytest.skip("no Arc Pro found")
    try:
        smu = SMU.open(devices[0])
    except BenchConnectionError as exc:
        pytest.skip(f"could not open Arc Pro: {exc}")
    try:
        yield smu
    finally:
        smu.close()


_PLAN_ID = re.compile(r"^[A-Z][A-Z0-9]*-P[0-9][0-9a-z]*\.[0-9]+$")


def pytest_collection_modifyitems(config, items):
    """Copy ``@pytest.mark.plan("ID")`` into the junit XML as a property.

    tools/plancheck.py derives each plan item's status from those results; a
    property carries a reference the static scan can't see (a marker applied
    by a fixture or a hook). A malformed ID is a usage error, so a typo fails
    the run instead of quietly referencing nothing.
    """
    no_mcp = sys.version_info < (3, 10)
    for item in items:
        # Skipped by Python version, never by "is it importable": from 3.10 a
        # missing mcp is a broken install and must fail, not quietly skip.
        if no_mcp and item.get_closest_marker("needs_mcp") is not None:
            item.add_marker(pytest.mark.skip(reason="mcp<2 needs Python >= 3.10"))
        for marker in item.iter_markers("plan"):
            if not marker.args:
                raise pytest.UsageError(f"{item.nodeid}: @pytest.mark.plan needs an item ID")
            for plan_id in marker.args:
                if not isinstance(plan_id, str) or not _PLAN_ID.match(plan_id):
                    raise pytest.UsageError(f"{item.nodeid}: bad plan item ID {plan_id!r}")
                item.user_properties.append(("plan", plan_id))

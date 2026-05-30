"""Shared pytest fixtures.

The hardware-required tier needs an Arc Pro on COM6. If none is present,
those tests skip with a clear message instead of failing.
"""

from __future__ import annotations

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
    from benchctrl import SMU
    from benchctrl.exceptions import SMUConnectionError

    try:
        devices = SMU.discover()
    except Exception as exc:  # noqa: BLE001
        pytest.skip(f"discovery failed: {exc}")
    if not devices:
        pytest.skip("no Arc Pro found")
    try:
        smu = SMU.open(devices[0])
    except SMUConnectionError as exc:
        pytest.skip(f"could not open Arc Pro: {exc}")
    try:
        yield smu
    finally:
        smu.close()

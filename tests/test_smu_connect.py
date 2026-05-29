"""Hardware-required: connection lifecycle."""

from __future__ import annotations

import pytest

from opensmu import SMU


pytestmark = pytest.mark.hardware


def test_discover_finds_arc():
    devices = SMU.discover()
    assert devices, "no Arc Pro discovered — check that it's plugged in"


def test_open_close_cycle(smu):
    assert smu.is_connected
    # info is populated if discovery returned a descriptor
    if smu.info is not None:
        assert smu.info.port
        assert smu.info.name


def test_close_then_reopen(smu):
    smu.close()
    assert not smu.is_connected
    # SMU.open() defaults to first discovered device, which is the one we
    # just closed. Reopen should succeed.
    again = SMU.open()
    try:
        assert again.is_connected
    finally:
        again.close()

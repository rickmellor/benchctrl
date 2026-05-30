"""Transport discovery + PortInfo."""

from __future__ import annotations

from benchctrl.drivers.otii_arc.transport import PortInfo, discover_arc_ports


def test_discover_returns_list():
    out = discover_arc_ports()
    assert isinstance(out, list)
    # Empty is acceptable (no device plugged in for hardware-free runs)
    for p in out:
        assert isinstance(p, PortInfo)


def test_port_info_str():
    p = PortInfo(device="COM6", name="Arc", serial_number="ABC", description="USB Arc")
    s = str(p)
    assert "COM6" in s
    assert "USB Arc" in s
    assert "ABC" in s

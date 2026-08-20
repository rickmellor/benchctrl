"""Transport selection: kernel ch341 driver first, userspace bridge as fallback.

These tests pin the *precedence*, which is the part that has to hold identically
on desktop Linux and on the Uno Q. The CH341 register layer and the pty bridge
are covered in ``test_ch341.py``; here both branches are stubbed so the decision
itself is what is under test.
"""

from __future__ import annotations

import pytest

from benchctrl.exceptions import BenchConnectionError
from benchctrl.transports import autoserial
from benchctrl.transports.ch341 import CH340_PID, CH340_VID


class FakePort:
    """One entry of ``serial.tools.list_ports.comports()``."""

    def __init__(self, device: str, vid: int | None, pid: int | None) -> None:
        self.device = device
        self.vid = vid
        self.pid = pid


class FakeBridge:
    """Stands in for a started PtySerialBridge."""

    def __init__(self, port: str = "/dev/pts/9") -> None:
        self.port = port
        self.closed = False

    def close(self) -> None:
        self.closed = True


@pytest.fixture
def kernel_ttys(monkeypatch):
    """Control what the kernel appears to have bound."""

    ports: list[FakePort] = []

    def fake_comports():
        return list(ports)

    import serial.tools.list_ports as list_ports

    monkeypatch.setattr(list_ports, "comports", fake_comports)
    return ports


@pytest.fixture
def userspace(monkeypatch):
    """Record whether the userspace bridge was opened, and with what."""

    calls: list[dict] = []

    def fake_open(**kwargs):
        calls.append(kwargs)
        return FakeBridge()

    monkeypatch.setattr(autoserial, "open_ch341_pty", fake_open)
    return calls


# --------------------------------------------------------------------------
# Precedence
# --------------------------------------------------------------------------


def test_an_explicit_port_wins_and_probes_nothing(kernel_ttys, userspace):
    """An operator naming a port is not second-guessed."""
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    target = autoserial.resolve_ch341_port(port="/dev/ttyS7")

    assert target.port == "/dev/ttyS7"
    assert target.how == "explicit"
    assert target.bridge is None
    assert userspace == [], "explicit port must not open the userspace driver"


def test_the_kernel_tty_wins_over_the_userspace_driver(kernel_ttys, userspace):
    """Where the kernel has a driver, ours must not take over."""
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    target = autoserial.resolve_ch341_port(port=None)

    assert target.port == "/dev/ttyUSB0"
    assert target.how == "kernel"
    assert target.bridge is None
    assert userspace == []


def test_userspace_is_used_when_the_kernel_bound_nothing(kernel_ttys, userspace):
    """The Uno Q case: the adapter enumerates but has no tty."""
    target = autoserial.resolve_ch341_port(port=None)

    assert target.port == "/dev/pts/9"
    assert target.how == "userspace"
    assert target.bridge is not None
    assert len(userspace) == 1


def test_an_unrelated_tty_does_not_count_as_the_kernel_having_a_driver(
    kernel_ttys, userspace
):
    """A different adapter's tty must not suppress the fallback.

    Matching on presence-of-any-tty rather than on VID/PID would make an
    unrelated FTDI cable hide the QR10x.
    """
    kernel_ttys.append(FakePort("/dev/ttyUSB0", 0x0403, 0x6001))  # FTDI

    target = autoserial.resolve_ch341_port(port=None)

    assert target.how == "userspace"
    assert len(userspace) == 1


@pytest.mark.parametrize("sentinel", [None, "", "auto", "AUTO", "Auto"])
def test_every_auto_sentinel_triggers_selection(sentinel, kernel_ttys, userspace):
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    target = autoserial.resolve_ch341_port(port=sentinel)

    assert target.how == "kernel"


def test_the_configured_baudrate_reaches_the_userspace_driver(kernel_ttys, userspace):
    """A bridge opened at the wrong rate would produce silent garbage."""
    autoserial.resolve_ch341_port(port=None, baudrate=9600)

    assert userspace[0]["baudrate"] == 9600


def test_a_serial_number_selects_among_several_adapters(kernel_ttys, userspace):
    autoserial.resolve_ch341_port(port=None, serial_number="ABC123")

    assert userspace[0]["serial_number"] == "ABC123"


def test_no_adapter_at_all_raises_naming_both_causes(kernel_ttys, monkeypatch):
    """"Not found" is ambiguous between an unplugged cable and missing udev."""

    def fake_open(**kwargs):
        raise BenchConnectionError("no CH340 bridge found (1a86:7523)")

    monkeypatch.setattr(autoserial, "open_ch341_pty", fake_open)

    with pytest.raises(BenchConnectionError) as excinfo:
        autoserial.resolve_ch341_port(port=None)

    msg = str(excinfo.value)
    assert "no kernel tty" in msg
    assert "udev" in msg, "the message must mention the other likely cause"


def test_multiple_kernel_ttys_pick_the_first_and_warn(kernel_ttys, userspace, caplog):
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))
    kernel_ttys.append(FakePort("/dev/ttyUSB1", CH340_VID, CH340_PID))

    with caplog.at_level("WARNING"):
        target = autoserial.resolve_ch341_port(port=None)

    assert target.port == "/dev/ttyUSB0"
    assert "2 CH340 ttys" in caplog.text


# --------------------------------------------------------------------------
# Bridge lifetime — the part that leaks a USB claim if it is wrong
# --------------------------------------------------------------------------


class FakeDriver:
    def __init__(self, port: str, **kwargs) -> None:
        self.port = port
        self.kwargs = kwargs
        self.closed = False

    def close(self) -> None:
        self.closed = True


def test_closing_the_driver_also_closes_the_bridge(kernel_ttys, userspace):
    """Otherwise the chip stays claimed and the next open fails."""
    obj = autoserial.open_serial_driver(FakeDriver, port=None)
    bridge = obj._benchctrl_bridge

    assert not bridge.closed
    obj.close()

    assert obj.closed
    assert bridge.closed


def test_the_bridge_is_closed_even_if_the_driver_close_raises(kernel_ttys, userspace):
    class Exploding(FakeDriver):
        def close(self) -> None:
            raise RuntimeError("instrument teardown failed")

    obj = autoserial.open_serial_driver(Exploding, port=None)
    bridge = obj._benchctrl_bridge

    with pytest.raises(RuntimeError):
        obj.close()

    assert bridge.closed, "a failing driver close must still release the USB claim"


def test_a_failed_driver_open_does_not_leak_the_bridge(kernel_ttys, monkeypatch):
    created: list[FakeBridge] = []

    def fake_open(**kwargs):
        b = FakeBridge()
        created.append(b)
        return b

    monkeypatch.setattr(autoserial, "open_ch341_pty", fake_open)

    def exploding_opener(port, **kwargs):
        raise BenchConnectionError("no response to AT")

    with pytest.raises(BenchConnectionError):
        autoserial.open_serial_driver(exploding_opener, port=None)

    assert created[0].closed, "the bridge must not outlive a failed driver open"


def test_no_bridge_attribute_when_the_kernel_supplied_the_port(kernel_ttys, userspace):
    """The kernel path must not carry bridge machinery it does not need."""
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    obj = autoserial.open_serial_driver(FakeDriver, port=None)

    assert obj.port == "/dev/ttyUSB0"
    assert not hasattr(obj, "_benchctrl_bridge")
    # close() is the driver's own, unwrapped.
    obj.close()
    assert obj.closed


def test_open_kwargs_reach_the_driver(kernel_ttys, userspace):
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    obj = autoserial.open_serial_driver(FakeDriver, port=None, baudrate=115200)

    assert obj.kwargs == {"baudrate": 115200}


def test_a_failed_kernel_open_does_not_fall_back_to_userspace(kernel_ttys, userspace):
    """The transport is chosen by what the host has, never by what failed.

    Falling back here would turn "someone else holds the port" into a
    different, working transport measuring something else — CONTRIBUTING
    rule 4's silent fallback, at the transport layer.
    """
    kernel_ttys.append(FakePort("/dev/ttyUSB0", CH340_VID, CH340_PID))

    def busy_opener(port, **kwargs):
        raise BenchConnectionError(f"{port}: device or resource busy")

    with pytest.raises(BenchConnectionError, match="busy"):
        autoserial.open_serial_driver(busy_opener, port=None)

    assert userspace == [], "a failed kernel open must not try the userspace driver"

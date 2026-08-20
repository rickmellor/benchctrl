"""Unified discovery: identification, confidence, and de-duplication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from benchctrl import discovery
from benchctrl.discovery import EXACT, UNKNOWN, DiscoveredDevice


@dataclass
class FakePort:
    device: str
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    description: str = ""
    manufacturer: Optional[str] = None
    product: Optional[str] = None


def _patch_ports(monkeypatch, ports):
    import serial.tools.list_ports as list_ports

    monkeypatch.setattr(list_ports, "comports", lambda: ports)


def test_arc_is_identified_exactly(monkeypatch):
    _patch_ports(
        monkeypatch,
        [FakePort("/dev/ttyACM0", vid=0x0FCE, pid=0xD1E6, serial_number="ARC1",
                  description="Arc", product="Arc")],
    )
    found = discovery.scan_serial()
    assert len(found) == 1
    assert found[0].device_key == "otii_arc"
    assert found[0].confidence == EXACT
    assert found[0].usb_id == "0fce:d1e6"
    assert found[0].identified


def test_generic_bridge_is_never_claimed(monkeypatch):
    """A CH340 could be anything. Guessing produces false positives."""
    _patch_ports(
        monkeypatch,
        [FakePort("/dev/ttyUSB0", vid=0x1A86, pid=0x7523, description="USB Serial")],
    )
    found = discovery.scan_serial()
    assert found[0].device_key is None
    assert found[0].confidence == UNKNOWN
    assert "CH340" in found[0].label
    assert "probe" in found[0].note


def test_unknown_device_is_reported_not_dropped(monkeypatch):
    """An unrecognised instrument still belongs in the inventory."""
    _patch_ports(monkeypatch, [FakePort("/dev/ttyACM3", vid=0x2341, pid=0x0043)])
    found = discovery.scan_serial()
    assert len(found) == 1
    assert not found[0].identified


def test_no_signature_collides_with_a_generic_bridge():
    """A driver signature sharing a bridge's VID/PID would be a false positive."""
    for sig in discovery.SIGNATURES:
        assert (sig.vid, sig.pid) not in discovery.GENERIC_BRIDGES


def test_signatures_are_unique():
    seen = set()
    for sig in discovery.SIGNATURES:
        assert (sig.vid, sig.pid) not in seen, f"duplicate signature: {sig.label}"
        seen.add((sig.vid, sig.pid))


def test_signature_keys_are_real_device_keys():
    from benchctrl.config import DEVICE_KEYS

    for sig in discovery.SIGNATURES:
        assert sig.device_key in DEVICE_KEYS


def test_visa_resource_id_parsing():
    assert discovery._visa_usb_ids("USB0::0x1AB1::0x0E11::DL3D2323::INSTR") == (
        0x1AB1,
        0x0E11,
    )
    assert discovery._visa_usb_ids("ASRL/dev/pts/3::INSTR") == (None, None)
    assert discovery._visa_usb_ids("TCPIP::10.0.0.5::INSTR") == (None, None)


def test_visa_scan_identifies_rigols(monkeypatch):
    class FakeRM:
        def list_resources(self):
            return (
                "USB0::0x1AB1::0x0E11::DL3D2323::INSTR",
                "USB0::0x1AB1::0xA4A8::DP2A1111::INSTR",
                "ASRL/dev/ttyS0::INSTR",
            )

        def close(self):
            pass

    found = discovery.scan_visa(FakeRM())
    keys = [d.device_key for d in found]
    assert "rigol_dl3031a" in keys
    assert "rigol_dp2031" in keys
    assert None in keys  # the plain serial resource stays unclaimed


def test_visa_scan_identifies_the_siglent_dmm(monkeypatch):
    """The DMM is USB-TMC like the Rigols, but on Siglent's own VID.

    Without a SIGNATURES entry the driver could still open the meter (it
    scans VISA itself) while the bench inventory reported it as unidentified
    — so a remote operator listing the bench would not see the instrument
    the agent is perfectly able to serve.
    """

    class FakeRM:
        def list_resources(self):
            return ("USB0::0xF4EC::0x1220::SDM40FAKE0001::INSTR",)

        def close(self):
            pass

    found = discovery.scan_visa(FakeRM())
    assert [d.device_key for d in found] == ["siglent_sdm4065a"]
    assert found[0].confidence == EXACT


def test_the_siglent_signature_records_the_family_ambiguity():
    """The VID/PID is shared by the SDM4045A/4055A/4065A, whose resistance
    ranges and NPLC sets differ. A signature that claimed model certainty
    would licence a caller to skip ``*IDN?`` and pick wrong constants."""
    sig = next(s for s in discovery.SIGNATURES if s.device_key == "siglent_sdm4065a")
    assert "IDN" in sig.note


def test_visa_scan_is_silent_without_a_backend(monkeypatch):
    """A bench with only serial instruments is a valid bench."""
    import builtins

    real_import = builtins.__import__

    def no_pyvisa(name, *args, **kwargs):
        if name == "pyvisa":
            raise ImportError("no pyvisa")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", no_pyvisa)
    assert discovery.scan_visa() == []


def test_discover_dedupes_usbtmc_against_visa(monkeypatch):
    """One instrument must not appear twice under two transports."""
    shared = dict(vid=0x1AB1, pid=0x0E11, serial_number="DL3D2323")
    monkeypatch.setattr(discovery, "scan_serial", lambda: [])
    monkeypatch.setattr(
        discovery,
        "scan_usbtmc",
        lambda: [DiscoveredDevice(path="/dev/usbtmc0", transport="usbtmc", **shared)],
    )
    monkeypatch.setattr(
        discovery,
        "scan_visa",
        lambda: [
            DiscoveredDevice(
                path="USB0::0x1AB1::0x0E11::DL3D2323::INSTR",
                transport="visa",
                device_key="rigol_dl3031a",
                **shared,
            )
        ],
    )
    found = discovery.discover()
    assert len(found) == 1
    assert found[0].transport == "visa"  # the form a driver can open


def test_inventory_shape(monkeypatch):
    _patch_ports(
        monkeypatch,
        [
            FakePort("/dev/ttyACM0", vid=0x0FCE, pid=0xD1E6),
            FakePort("/dev/ttyUSB0", vid=0x1A86, pid=0x7523),
        ],
    )
    monkeypatch.setattr(discovery, "scan_visa", lambda: [])
    monkeypatch.setattr(discovery, "scan_usbtmc", lambda: [])
    inv = discovery.inventory()
    assert inv["count"] == 2
    assert inv["identified"] == 1
    assert "otii_arc" in inv["by_device_key"]
    assert "_unidentified" in inv["by_device_key"]


def test_find_for_filters(monkeypatch):
    _patch_ports(
        monkeypatch,
        [
            FakePort("/dev/ttyACM0", vid=0x0FCE, pid=0xD1E6),
            FakePort("/dev/ttyUSB0", vid=0x1A86, pid=0x7523),
        ],
    )
    monkeypatch.setattr(discovery, "scan_visa", lambda: [])
    monkeypatch.setattr(discovery, "scan_usbtmc", lambda: [])
    assert [d.path for d in discovery.find_for("otii_arc")] == ["/dev/ttyACM0"]
    assert [d.path for d in discovery.unidentified()] == ["/dev/ttyUSB0"]


def test_probe_identifies_a_simulated_qr10x():
    """The QR10x has no known VID/PID, so identification is by probe."""
    from benchctrl.sim.qr10x import SimulatedQR10x

    with SimulatedQR10x() as sim:
        assert discovery.probe_serial_identity(sim.port) == "eastwood_qr10x"


def test_probe_returns_none_for_a_foreign_device():
    from benchctrl.sim.otii_arc import SimulatedOtiiArc

    with SimulatedOtiiArc() as sim:
        assert discovery.probe_serial_identity(sim.port, timeout=0.3) is None


def test_probe_never_raises_on_a_bad_path():
    assert discovery.probe_serial_identity("/dev/does-not-exist") is None


def test_format_inventory_is_readable(monkeypatch):
    text = discovery.format_inventory(
        [
            DiscoveredDevice(
                path="/dev/ttyACM0",
                transport="serial",
                device_key="otii_arc",
                label="Otii Arc",
                vid=0x0FCE,
                pid=0xD1E6,
                confidence=EXACT,
            )
        ]
    )
    assert "otii_arc" in text
    assert "1 device(s), 1 identified." in text


def test_format_inventory_handles_empty():
    assert discovery.format_inventory([]) == "No instruments found."


# --------------------------------------------------------------------------
# Bridges the kernel never bound to a tty
# --------------------------------------------------------------------------
#
# ``comports()`` enumerates ttys, so on a kernel without CONFIG_USB_SERIAL_CH341
# a CH340 is invisible to scan_serial by construction — the QR10x reads as "not
# plugged in" when it is plugged in and usable. scan_driverless_bridges covers
# exactly that blind spot.


class FakeUsbDevice:
    """A pyusb device object, as far as the descriptor reads are concerned.

    The mixedCase names are pyusb's own (they mirror the USB descriptor field
    names), so they cannot be renamed to satisfy N815 without the fake ceasing
    to stand in for the real thing.
    """

    iManufacturer = 1  # noqa: N815
    iProduct = 2  # noqa: N815
    iSerialNumber = 3  # noqa: N815


def _patch_find_all(monkeypatch, devices):
    from benchctrl.transports import ch341

    monkeypatch.setattr(ch341.CH341Device, "find_all", classmethod(lambda cls: devices))


def test_a_driverless_ch340_is_reported(monkeypatch):
    _patch_ports(monkeypatch, [])
    _patch_find_all(monkeypatch, [FakeUsbDevice()])

    found = discovery.scan_driverless_bridges()

    assert len(found) == 1
    assert found[0].usb_id == "1a86:7523"
    # "auto" is both the honest answer (no node exists yet) and the value to
    # pass as port= to open it.
    assert found[0].path == "auto"
    assert found[0].confidence == UNKNOWN
    assert "userspace" in found[0].note


def test_a_kernel_bound_ch340_is_not_reported_twice(monkeypatch):
    """scan_serial already has it; reporting it as driverless would be wrong."""
    _patch_ports(monkeypatch, [FakePort("/dev/ttyUSB0", vid=0x1A86, pid=0x7523)])
    _patch_find_all(monkeypatch, [FakeUsbDevice()])

    assert discovery.scan_driverless_bridges() == []


def test_an_unrelated_tty_does_not_suppress_a_driverless_ch340(monkeypatch):
    """An FTDI cable elsewhere on the bus must not hide the CH340."""
    _patch_ports(monkeypatch, [FakePort("/dev/ttyUSB0", vid=0x0403, pid=0x6001)])
    _patch_find_all(monkeypatch, [FakeUsbDevice()])

    assert len(discovery.scan_driverless_bridges()) == 1


def test_no_usb_access_is_not_a_discovery_failure(monkeypatch):
    """Missing pyusb or udev rule means no answer, not a crash."""
    from benchctrl.exceptions import BenchConnectionError
    from benchctrl.transports import ch341

    def boom(cls):
        raise BenchConnectionError("pyusb unavailable")

    _patch_ports(monkeypatch, [])
    monkeypatch.setattr(ch341.CH341Device, "find_all", classmethod(boom))

    assert discovery.scan_driverless_bridges() == []


def test_discover_includes_driverless_bridges(monkeypatch):
    """The wiring, not just the scanner: discover() must call it."""
    _patch_ports(monkeypatch, [])
    _patch_find_all(monkeypatch, [FakeUsbDevice()])

    found = discovery.discover(usbtmc=False, visa=False)

    assert [d.path for d in found] == ["auto"]

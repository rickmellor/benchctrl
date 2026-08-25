"""Unified discovery: identification, confidence, and de-duplication."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Optional

import pytest

from benchctrl import discovery
from benchctrl.discovery import EXACT, HEURISTIC, UNKNOWN, DiscoveredDevice


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
        lambda *a, **kw: [
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
    monkeypatch.setattr(discovery, "scan_visa", lambda *a, **kw: [])
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
    monkeypatch.setattr(discovery, "scan_visa", lambda *a, **kw: [])
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


def test_the_pdu_is_deliberately_not_probeable():
    """The PDU must not be in the probe list, and the reason is measured.

    The plan called for a bare-``\\r`` probe at 9600 on the assumption that a CR
    merely re-prompts an idle console. On firmware 1.3.4 it does not: a CR
    *submits an empty line* to whichever login field is current, so successive
    probes walk the authentication state machine (``Login Name`` → ``Login
    Password`` → a ~15 s ``Please wait for authentication....`` → ``Login
    Failed``).

    Measured consequences, any one of which disqualifies probing:

    - **Unreliable.** Five consecutive probe calls against the real device
      returned ``None, cyberpower_pdu41002, None, None, None`` — the vendor
      string only appears in one of the three states.
    - **Not read-only.** The device recorded ``Login authorization failure via
      Console`` in its own event log for the probe traffic.
    - **Disruptive.** The console answers nothing during the ~15 s
      authentication delay, so probing can lock out the driver behind it.

    So this asserts an *absence*. Re-adding a PDU probe should fail here and
    send the reader to the note on ``SERIAL_PROBES``.
    """
    keys = [p.device_key for p in discovery.SERIAL_PROBES]
    assert "cyberpower_pdu41002" not in keys


def test_the_pdu_is_identified_passively_by_its_udev_symlink(tmp_path):
    """The replacement path: exact, and it writes nothing.

    ``deploy/udev/62-benchctrl-ftdi.rules`` binds the adapter's serial number to
    ``/dev/benchctrl/pdu41002``, which is better evidence than any probe reply —
    it cannot be confused by another device's echo, and identifying the bench no
    longer writes bytes to a mains contactor's control port.
    """
    port = tmp_path / "ttyUSB0"
    port.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "pdu41002").symlink_to(port)

    assert (
        discovery.identify_by_symlink(str(port), symlink_dir=str(link_dir))
        == "cyberpower_pdu41002"
    )


def test_symlink_identification_opens_nothing(tmp_path, monkeypatch):
    """Passive means passive.

    The whole justification for this path over a probe is that it does not
    touch the device, so the test forbids ``serial.Serial`` outright rather than
    trusting the implementation to keep being read-only.
    """
    import serial

    def boom(*args, **kwargs):  # pragma: no cover - must never run
        raise AssertionError("symlink identification opened the port")

    monkeypatch.setattr(serial, "Serial", boom)

    port = tmp_path / "ttyUSB0"
    port.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "pdu41002").symlink_to(port)

    assert (
        discovery.identify_by_symlink(str(port), symlink_dir=str(link_dir))
        == "cyberpower_pdu41002"
    )


def test_a_symlink_for_a_different_port_does_not_claim_this_one(tmp_path):
    """The symlink identifies *a* port, not every port.

    ``/dev/ttyUSB0`` is assigned in enumeration order, so the case that matters
    is two FTDI adapters present and the symlink pointing at the other one.
    Claiming the wrong port here would hand the PDU driver a foreign device and
    let it issue ``oltctrl`` at it.
    """
    pdu = tmp_path / "ttyUSB0"
    other = tmp_path / "ttyUSB1"
    pdu.write_text("")
    other.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "pdu41002").symlink_to(pdu)

    assert discovery.identify_by_symlink(str(other), symlink_dir=str(link_dir)) is None


def test_missing_udev_rules_are_not_an_error(tmp_path):
    """A developer laptop has no ``/dev/benchctrl``. That is unidentified, not
    broken — the same contract the probe path has."""
    port = tmp_path / "ttyUSB0"
    port.write_text("")
    assert (
        discovery.identify_by_symlink(
            str(port), symlink_dir=str(tmp_path / "nope")
        )
        is None
    )


def test_an_unknown_symlink_name_is_ignored(tmp_path):
    """Only names in ``SYMLINK_KEYS`` identify anything.

    ``60-benchctrl-ch341.rules`` already creates ``/dev/benchctrl/ch341`` for a
    *bridge*, which says nothing about what is attached to it. Treating any
    symlink in the directory as an identification would map that bridge to
    whatever key happened to be listed.
    """
    port = tmp_path / "ttyUSB0"
    port.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "ch341").symlink_to(port)

    assert discovery.identify_by_symlink(str(port), symlink_dir=str(link_dir)) is None


def test_probing_consults_the_symlink_before_writing_anything(monkeypatch, tmp_path):
    """Ordering inside ``probe_serial_identity``.

    A device identifiable passively must never be written to, so the symlink
    check has to come first. Enforced by making every serial open fail: if the
    symlink path is consulted first, the PDU is still identified.
    """
    import serial

    def boom(*args, **kwargs):
        raise AssertionError("probed a device that the symlink already identified")

    monkeypatch.setattr(serial, "Serial", boom)

    port = tmp_path / "ttyUSB0"
    port.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "pdu41002").symlink_to(port)
    monkeypatch.setattr(discovery, "SYMLINK_DIR", str(link_dir))

    assert discovery.probe_serial_identity(str(port)) == "cyberpower_pdu41002"


def test_scan_serial_identifies_the_pdu_without_probing(monkeypatch, tmp_path):
    """The wiring, not just the helper.

    ``scan_serial()`` is what ``discover()`` calls, and it must not need
    ``probe=True`` to name the PDU — otherwise the one device where probing is
    unacceptable is also the one device that requires it. The FT232R's
    ``0403:6001`` is in ``GENERIC_BRIDGES``, so without this it comes back
    unidentified.
    """
    port = tmp_path / "ttyUSB0"
    port.write_text("")
    link_dir = tmp_path / "benchctrl"
    link_dir.mkdir()
    (link_dir / "pdu41002").symlink_to(port)
    monkeypatch.setattr(discovery, "SYMLINK_DIR", str(link_dir))
    _patch_ports(
        monkeypatch,
        [FakePort(str(port), vid=0x0403, pid=0x6001, serial_number="B0027PK6")],
    )

    found = discovery.scan_serial()
    assert len(found) == 1
    assert found[0].device_key == "cyberpower_pdu41002"
    assert found[0].confidence == EXACT
    assert found[0].identified
    assert "udev symlink" in found[0].note


def test_every_serial_probe_is_a_write_not_a_command():
    """A probe is sent to a port whose device is *unknown* — that is the entire
    premise — so any probe that could be a meaningful command on some other
    instrument is a hazard. Bounded to short, inert requests."""
    for probe in discovery.SERIAL_PROBES:
        assert probe.request.endswith((b"\r", b"\n"))
        assert len(probe.request) <= 16, f"{probe.device_key}: probe too chatty"
        lowered = probe.request.lower()
        for dangerous in (b"off", b"act", b"reboot", b"reset"):
            assert dangerous not in lowered, (
                f"{probe.device_key}: probe {probe.request!r} could be a command"
            )


def test_no_probe_marker_is_a_substring_of_its_own_request():
    """The bug that made discovery call a mains PDU a resistance box.

    The QR10x probe writes ``AT+DEV.TYPE?`` and matched on ``DEV.TYPE`` — a
    substring of what it had just sent. The PDU's serial console echoes, so it
    replied with the probe text and matched. Any marker that a bare echo can
    satisfy identifies *every echoing device* as the probed one, which for this
    bench means misidentifying the one device that switches mains.
    """
    for probe in discovery.SERIAL_PROBES:
        echoed = probe.request.decode("ascii", errors="replace")
        for marker in probe.markers:
            assert marker not in echoed, (
                f"{probe.device_key}: marker {marker!r} is satisfied by an echo "
                f"of the probe itself ({echoed!r})"
            )


def test_no_two_probes_can_match_the_same_reply():
    """Ordering decides ties, so an ambiguous marker set is silently wrong.

    Whichever probe is listed first wins, and the first-listed is not
    necessarily the correct answer — that is precisely how the echo bug
    presented, as a QR10x identification of a PDU rather than as no match.
    """
    for probe in discovery.SERIAL_PROBES:
        for other in discovery.SERIAL_PROBES:
            if other.device_key == probe.device_key:
                continue
            for marker in probe.markers:
                for other_marker in other.markers:
                    assert marker not in other_marker, (
                        f"{probe.device_key}'s {marker!r} also matches "
                        f"{other.device_key}'s {other_marker!r}"
                    )


def test_the_pdu_is_not_misidentified_as_a_qr10x(monkeypatch):
    """The concrete false positive, through the real probe loop.

    With the old ``DEV.TYPE`` marker this returned ``eastwood_qr10x`` for a PDU
    — discovery naming a mains switch as a programmable resistor, which is the
    worst possible direction for that error. Checked in both session states,
    since the console echoes either way.

    On real hardware the 115200-vs-9600 baud mismatch happened to mask this (the
    PDU returned framing garbage instead of its echo), but the simulator shares
    a pty with no baud emulation and so sees the echo directly. Both are worth
    covering: the marker is wrong regardless of which devices it happens to
    spare.
    """
    from benchctrl.sim.pdu41002 import SimulatedPDU41002

    for require_login in (True, False):
        with SimulatedPDU41002(require_login=require_login) as sim:
            assert discovery.probe_serial_identity(sim.port, timeout=0.3) is None


def test_the_qr10x_still_identifies_after_the_marker_fix():
    """The tightened marker must not cost the identification it exists for."""
    from benchctrl.sim.qr10x import SimulatedQR10x

    with SimulatedQR10x() as sim:
        assert discovery.probe_serial_identity(sim.port) == "eastwood_qr10x"


def test_each_probe_carries_its_own_baud_rate():
    """The generalisation survives having only one probe left.

    ``probe_serial_identity`` used to hardcode 115200. The PDU sits behind the
    same class of bridge at 9600, which is what forced the per-probe baud
    rate — and although the PDU is no longer probed, the hardcoding was still a
    latent bug: the next 9600 device would silently fail to identify. Asserted
    per-probe rather than as "more than one baud exists", which stopped being
    true when the PDU probe was removed.
    """
    for probe in discovery.SERIAL_PROBES:
        assert probe.baudrate > 0

    import inspect

    source = inspect.getsource(discovery.probe_serial_identity)
    assert "probe.baudrate" in source, "the baud rate is hardcoded again"


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


# --------------------------------------------------------------------------
# Probing from a scan — opt-in only
# --------------------------------------------------------------------------
#
# probe_serial_identity() existed for a release with nothing calling it, which
# is why eastwood_qr10x — the one key with no VID/PID signature — could never be
# identified and its bench slot read NO ID forever. Wiring it in is the fix; the
# hazard the wiring introduces is that a probe *writes* AT+DEV.TYPE? to a port
# whose occupant is unknown, and the dashboard re-scans every 30 s. Hence: off
# unless asked, and never at anything the signature table has already claimed.

#: A CH340 bridge, i.e. the one situation where VID/PID cannot decide.
BRIDGE_IDS = dict(vid=0x1A86, pid=0x7523)


class ProbeSpy:
    """Stands in for :py:func:`probe_serial_identity`, recording every call.

    Recording is the point: the safety property is about probes *not happening*,
    and a test that only inspected the returned devices would pass just as well
    against code that probed everything and discarded the answers.
    """

    def __init__(self, replies=None, raises=False):
        self.paths: list[str] = []
        self._replies = replies or {}
        self._raises = raises

    def __call__(self, path, timeout=1.0):
        self.paths.append(path)
        if self._raises:
            raise OSError(f"probe of {path} exploded")
        return self._replies.get(path)


def _patch_probe(monkeypatch, spy):
    monkeypatch.setattr(discovery, "probe_serial_identity", spy)
    return spy


def test_a_scan_never_probes_unless_asked(monkeypatch):
    """The safety-critical default, on both entry points a poll goes through.

    ``inventory()`` is what the agent's ``agent.discover`` serves and what the
    bench dashboard calls every 30 s. If probing were on by default, that timer
    would write AT commands into whatever is plugged into the bench for as long
    as the panel is up, with nobody having asked for it.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy({"/dev/ttyUSB0": "eastwood_qr10x"}))
    _patch_ports(monkeypatch, [FakePort("/dev/ttyUSB0", **BRIDGE_IDS)])

    found = discovery.discover(usbtmc=False, visa=False)
    inv = discovery.inventory(usbtmc=False, visa=False)

    assert spy.paths == [], "a default scan wrote bytes to an unknown device"
    # And the device is still reported, just unclaimed — the pre-probe answer.
    assert [d.device_key for d in found] == [None]
    assert inv["identified"] == 0


def test_probing_identifies_a_qr10x_behind_a_bridge(monkeypatch):
    """The gap this closes, end to end against the real simulator.

    No monkeypatched probe here: a real ``AT+DEV.TYPE?`` goes down a real pty
    and the reply comes back from the QR10x sim, so this fails if the wiring
    reaches ``probe_serial_identity`` with something it cannot open. The CH340
    ids are the honest setup — that bridge is where the real unit sits.
    """
    from benchctrl.sim.qr10x import SimulatedQR10x

    with SimulatedQR10x() as sim:
        _patch_ports(monkeypatch, [FakePort(sim.port, **BRIDGE_IDS)])

        found = discovery.discover(usbtmc=False, visa=False, probe=True)

    assert [d.device_key for d in found] == ["eastwood_qr10x"]
    # A reply is strong evidence about the protocol on the port, not the
    # certainty a signature gives, and EXACT here would licence a caller to skip
    # the confirmation the module's own docstring demands.
    assert found[0].confidence == HEURISTIC
    assert "probe" in found[0].note
    assert "QR10x" in found[0].label


def test_an_identified_instrument_is_never_probed(monkeypatch):
    """Writing AT commands at a power supply is the hazard here.

    The input is deliberately one that *only* the identified check rejects: a
    claimed device that is also sitting on a generic bridge's VID/PID. A test
    using a real instrument's ids (the Arc's, say) would pass against code with
    no identified check at all, because the bridge check would reject it anyway
    — the two guards would be indistinguishable.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy({"/dev/ttyUSB0": "eastwood_qr10x"}))
    psu = DiscoveredDevice(
        path="/dev/ttyUSB0",
        transport="serial",
        device_key="rigol_dp2031",
        label="Rigol DP2000-series power supply",
        confidence=EXACT,
        **BRIDGE_IDS,
    )

    result = discovery.probe_unidentified([psu])

    assert spy.paths == [], "probed an instrument the signature table had claimed"
    assert result == [psu], "an identified device must come back untouched"


def test_only_devices_behind_a_known_bridge_are_probed(monkeypatch):
    """An unrecognised VID/PID is unidentified for a different reason.

    A bare Arduino (2341:0043) is unclaimed because nobody has written its
    signature, not because its ids are uninformative. Probing every unidentified
    device would mean writing to arbitrary hardware on the bench to learn
    something a signature entry should have told us.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy())
    _patch_ports(monkeypatch, [FakePort("/dev/ttyACM3", vid=0x2341, pid=0x0043)])
    _patch_find_all(monkeypatch, [])

    found = discovery.discover(usbtmc=False, visa=False, probe=True)

    assert spy.paths == []
    assert not found[0].identified


def test_a_non_serial_device_is_not_probed_even_on_bridge_ids(monkeypatch):
    """``AT+DEV.TYPE?`` is a serial-line protocol; a TMC node is not a serial port.

    A **contract** test on ``probe_unidentified``, not a reachability one: no scan
    is known to produce a usbtmc node carrying a CH340's ids. But the function is
    public and takes any list, so its guarantee has to hold for the list it is
    handed rather than for the one ``discover()`` happens to build.

    The ids are the CH340's deliberately. With an instrument's own ids the bridge
    check would reject this device too and the test would pass against a version
    with no transport check at all — which is exactly what happened: the first
    draft of this suite let that mutant survive.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy({"/dev/usbtmc0": "eastwood_qr10x"}))
    node = DiscoveredDevice(
        path="/dev/usbtmc0", transport="usbtmc", confidence=UNKNOWN, **BRIDGE_IDS
    )

    assert discovery.probe_unidentified([node]) == [node]
    assert spy.paths == [], "wrote an AT command into a USB-TMC instrument node"


def test_a_bridge_with_no_tty_is_not_probed(monkeypatch):
    """``path="auto"`` is a placeholder, not something to open.

    The driverless-bridge scanner reports a CH340 the kernel never bound, so
    reaching it means claiming the chip over libusb and standing up a pty.
    Doing that inside a scan would take the USB claim out from under whatever
    the caller opens next, to answer a question it did not ask.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy())
    _patch_ports(monkeypatch, [])
    _patch_find_all(monkeypatch, [FakeUsbDevice()])

    found = discovery.discover(usbtmc=False, visa=False, probe=True)

    assert [d.path for d in found] == ["auto"]
    assert spy.paths == [], "opened a bridge the caller had not asked us to claim"


def test_a_silent_device_is_left_unidentified_rather_than_claimed():
    """Silence is not evidence, in either direction.

    A real Arc sim behind bridge ids: it receives the probe and says nothing
    useful back. Claiming it anyway would put the wrong driver against a real
    instrument, and the reverse error — marking it "definitely not ours" — would
    be just as wrong, because a QR10x that is powered down, mid-boot, or held
    open by another process is also silent.
    """
    from benchctrl.sim.otii_arc import SimulatedOtiiArc

    with SimulatedOtiiArc() as sim:
        before = [
            DiscoveredDevice(
                path=sim.port,
                transport="serial",
                label="CH340 USB-serial bridge",
                confidence=UNKNOWN,
                note="generic USB-serial bridge — probe to identify",
                **BRIDGE_IDS,
            )
        ]

        after = discovery.probe_unidentified(before, timeout=0.3)

    assert [d.device_key for d in after] == [None]
    assert after[0].confidence == UNKNOWN
    assert after == before, "a non-answer must not change what the scan reported"


def test_a_probe_that_fails_does_not_break_the_scan(monkeypatch):
    """One unresponsive device must not cost the operator the whole inventory.

    ``probe_serial_identity`` swallows its own serial errors, but it is a
    module-level name, so the scan cannot assume that of whatever is bound to
    it. The second port is the assertion that matters: a scan that aborted
    part-way would report a bench with an instrument missing, which reads as
    unplugged hardware.
    """
    spy = _patch_probe(monkeypatch, ProbeSpy(raises=True))
    _patch_ports(
        monkeypatch,
        [
            FakePort("/dev/ttyUSB0", **BRIDGE_IDS),
            FakePort("/dev/ttyACM0", vid=0x0FCE, pid=0xD1E6, product="Arc"),
        ],
    )

    found = discovery.discover(usbtmc=False, visa=False, probe=True)

    assert spy.paths == ["/dev/ttyUSB0"]
    assert [d.path for d in found] == ["/dev/ttyACM0", "/dev/ttyUSB0"]
    assert [d.device_key for d in found] == ["otii_arc", None]


def test_inventory_can_opt_in_to_probing(monkeypatch):
    """The remote agent's own entry point, since that is where an operator asks.

    ``agent.discover`` serves ``inventory()``, so a probe that only reached
    through ``discover()`` would leave the remote caller — the one host that
    cannot look at the USB bus itself — unable to ever identify the QR10x.
    """
    _patch_probe(monkeypatch, ProbeSpy({"/dev/ttyUSB0": "eastwood_qr10x"}))
    _patch_ports(monkeypatch, [FakePort("/dev/ttyUSB0", **BRIDGE_IDS)])

    inv = discovery.inventory(usbtmc=False, visa=False, probe=True)

    assert inv["identified"] == 1
    assert "eastwood_qr10x" in inv["by_device_key"]
    assert inv["by_device_key"]["eastwood_qr10x"][0]["confidence"] == HEURISTIC


def test_every_probe_label_is_a_real_device_key():
    """A probe returning a key nothing can open would be a worse failure than
    the NO ID it replaces: the panel would show the slot as found while the
    registry had no driver for it."""
    from benchctrl.config import DEVICE_KEYS

    for key in discovery.PROBE_LABELS:
        assert key in DEVICE_KEYS


# --------------------------------------------------------------------------
# visa_resource_for — the one place a VISA driver resolves its resource
#
# Each driver used to scan list_resources() itself and substring-match a hex
# VID/PID. That made an attached instrument invisible to its own driver on any
# board using pyvisa-py, which renders the ids in decimal. The SDM4065A was
# fixed for this; the two Rigols kept the bug for another release, and it is
# what made the bench panel report NOT SERVED for three connected instruments.
# --------------------------------------------------------------------------

#: The bench board's real resource strings, as pyvisa-py renders them. 6833 is
#: 0x1AB1, 42152 is 0xA4A8, 3601 is 0x0E11, 62700 is 0xF4EC, 4640 is 0x1220.
BOARD_RESOURCES = (
    "ASRL/dev/ttyACM0::INSTR",
    "ASRL/dev/ttyS0::INSTR",
    "USB0::62700::4640::SDM46A0CA00021::0::INSTR",
    "USB0::6833::3601::DL3D232300106::0::INSTR",
    "USB0::6833::42152::DP2A243500269::0::INSTR",
)


def _patch_visa(monkeypatch, resources):
    """Make discovery see exactly ``resources`` on the VISA bus, nothing else."""

    class FakeRM:
        def list_resources(self):
            return tuple(resources)

        def close(self):
            pass

    # Bind the real scanner before patching the name it lives under, or the
    # replacement calls itself.
    real_scan_visa = discovery.scan_visa
    monkeypatch.setattr(
        discovery, "scan_visa", lambda *a, **kw: real_scan_visa(FakeRM())
    )
    monkeypatch.setattr(
        discovery, "_visa_resource_names", lambda *a, **kw: sorted(resources)
    )


@pytest.mark.parametrize(
    ("key", "expected"),
    [
        ("rigol_dp2031", "USB0::6833::42152::DP2A243500269::0::INSTR"),
        ("rigol_dl3031a", "USB0::6833::3601::DL3D232300106::0::INSTR"),
        ("siglent_sdm4065a", "USB0::62700::4640::SDM46A0CA00021::0::INSTR"),
    ],
)
def test_a_decimal_resource_resolves_for_every_visa_instrument(
    monkeypatch, key, expected
):
    """The bug, on the board's own resource strings.

    Parametrised across all three deliberately: the DMM was fixed first and the
    two Rigols were not, so a per-driver test is exactly what let this survive.
    One test that must hold for every VISA instrument cannot be half-applied.
    """
    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, BOARD_RESOURCES)

    assert discovery.visa_resource_for(key) == expected


def test_a_hex_resource_still_resolves(monkeypatch):
    """The other common case: a laptop with NI-VISA installed."""
    _patch_ports(monkeypatch, [])
    _patch_visa(
        monkeypatch,
        ("USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR",
         "USB0::0x1ab1::0x0E11::DL3D232300106::INSTR"),
    )

    assert discovery.visa_resource_for("rigol_dp2031").endswith("DP2A243500269::INSTR")
    assert discovery.visa_resource_for("rigol_dl3031a").endswith("DL3D232300106::INSTR")


def test_two_rigols_are_told_apart_on_pid_alone(monkeypatch):
    """The supply and the load share Rigol's VID and differ only by PID.

    A matcher that checked the VID and stopped would return whichever came
    first, and ramping a supply when you asked for a load is the worst outcome
    on this list.
    """
    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, BOARD_RESOURCES)

    psu = discovery.visa_resource_for("rigol_dp2031")
    load = discovery.visa_resource_for("rigol_dl3031a")
    assert psu != load
    assert "42152" in psu and "3601" in load


def test_an_absent_instrument_raises_the_drivers_own_error(monkeypatch):
    """A driver's public contract is its own exception type, not this module's.

    Callers already catch RigolDP2031ConnectionError; centralising the lookup
    must not quietly change what they have to catch.
    """
    from benchctrl.drivers.rigol_dp2031.driver import RigolDP2031ConnectionError

    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, ("USB0::62700::4640::SDM46A0CA00021::0::INSTR",))

    with pytest.raises(RigolDP2031ConnectionError):
        discovery.visa_resource_for(
            "rigol_dp2031", error=RigolDP2031ConnectionError
        )


def test_the_error_names_what_visa_actually_reported(monkeypatch):
    """The failure this replaced said "no DP2000 found" while listing the very
    resource it had just rejected. Keeping the list in the message is what made
    the bug findable at all, so it stays — an instrument that is present but
    unmatched must not look identical to an unplugged one."""
    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, ("USB0::62700::4640::SDM46A0CA00021::0::INSTR",))

    with pytest.raises(discovery.BenchConnectionError) as exc:
        discovery.visa_resource_for("rigol_dp2031")
    msg = str(exc.value)
    assert "SDM46A0CA00021" in msg, "the operator cannot see what was rejected"
    assert "0x1ab1" in msg.lower() and "0xa4a8" in msg.lower(), "what was sought"
    assert "udev" in msg, "the usual cause on a board using pyvisa-py"


def test_two_identical_instruments_are_never_picked_between(monkeypatch):
    """Two supplies on one bench is rare; choosing one at random is not an
    option. The caller must pass resource= and say which rail it means."""
    _patch_ports(monkeypatch, [])
    _patch_visa(
        monkeypatch,
        ("USB0::6833::42152::DP2A243500269::0::INSTR",
         "USB0::6833::42152::DP2A999999999::0::INSTR"),
    )

    with pytest.raises(discovery.BenchConnectionError, match="several"):
        discovery.visa_resource_for("rigol_dp2031")


def test_a_serial_resource_is_never_returned_for_a_visa_instrument(monkeypatch):
    """The Arc is on /dev/ttyACM0 and the lookup must not reach for it.

    Serial and usbtmc scans are switched off in the lookup, so a bench where the
    supply is unplugged cannot resolve to whatever else happens to be attached.
    """
    _patch_ports(
        monkeypatch,
        [FakePort("/dev/ttyACM0", vid=0x0FCE, pid=0xD1E6, product="Arc")],
    )
    _patch_visa(monkeypatch, ())

    with pytest.raises(discovery.BenchConnectionError):
        discovery.visa_resource_for("rigol_dp2031")
    # And the Arc's own lookup is unaffected by the VISA bus being empty.
    assert [d.path for d in discovery.find_for("otii_arc")] == ["/dev/ttyACM0"]


@pytest.mark.parametrize(
    ("module", "expected"),
    [
        ("benchctrl.drivers.rigol_dp2031.driver",
         "USB0::6833::42152::DP2A243500269::0::INSTR"),
        ("benchctrl.drivers.rigol_dl3031a.driver",
         "USB0::6833::3601::DL3D232300106::0::INSTR"),
        ("benchctrl.drivers.siglent_sdm4065a.driver",
         "USB0::62700::4640::SDM46A0CA00021::0::INSTR"),
    ],
)
def test_each_driver_actually_uses_the_shared_lookup(monkeypatch, module, expected):
    """Through the driver's own entry point, not the helper it should call.

    The tests above pass against a driver still carrying the old hex substring
    matcher, because they exercise visa_resource_for() directly. That is exactly
    the shape of the gap that let this bug survive a release: the lookup was
    correct and two of the three drivers did not use it. This asserts the wiring
    — revert any _autodiscover to its substring version and this fails.
    """
    import importlib

    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, BOARD_RESOURCES)
    drv = importlib.import_module(module)

    class FakeRM:
        def list_resources(self):
            return BOARD_RESOURCES

        def close(self):
            pass

    assert drv._autodiscover(FakeRM()) == expected


def test_a_kernel_usbtmc_node_is_not_returned_as_a_visa_resource(monkeypatch):
    """Why the lookup switches the serial and usbtmc scans off.

    ``/dev/usbtmc0`` is a real, correctly-identified DP2031 — but it is a
    character device, not something ``rm.open_resource()`` can open. This
    combination is reachable on the bench board: the kernel can bind usbtmc
    while pyvisa-py still cannot read the descriptors for want of a udev rule.

    Without the scan filters the lookup returns that path and the driver fails
    deep inside pyvisa with a confusing error, instead of saying plainly that
    no VISA resource for the supply was found. A test with an empty bus cannot
    tell the two apart — this one can.
    """
    _patch_ports(monkeypatch, [])
    _patch_visa(monkeypatch, ())
    monkeypatch.setattr(
        discovery,
        "scan_usbtmc",
        lambda: [
            DiscoveredDevice(
                path="/dev/usbtmc0",
                transport="usbtmc",
                device_key="rigol_dp2031",
                vid=0x1AB1,
                pid=0xA4A8,
                confidence=EXACT,
            )
        ],
    )

    with pytest.raises(discovery.BenchConnectionError) as exc:
        discovery.visa_resource_for("rigol_dp2031")
    assert "/dev/usbtmc0" not in str(exc.value).split("VISA reported")[0]


def test_a_borrowed_resource_manager_is_never_closed(monkeypatch):
    """``pyvisa.ResourceManager()`` is a singleton, so closing "our own" closes
    the caller's.

    Introduced and caught in the same session: centralising the lookup made it
    build its own manager and close it afterwards, which invalidated the handle
    the driver was about to call ``open_resource`` on. Both Rigols then failed
    with ``InvalidSession: Invalid session handle`` on the *correct* resource
    string — a strictly worse failure than the one being fixed, because it looks
    like broken hardware rather than a lookup miss. The DMM was unaffected only
    because it already passed its manager through.
    """
    closed = []

    class FakeRM:
        def list_resources(self):
            return BOARD_RESOURCES

        def close(self):
            closed.append(True)

    rm = FakeRM()
    _patch_ports(monkeypatch, [])
    assert discovery.visa_resource_for("rigol_dp2031", resource_manager=rm)
    assert not closed, "closed a ResourceManager it did not create"

    # And on the failure path, which builds the resource list for the message.
    with pytest.raises(discovery.BenchConnectionError):
        discovery.visa_resource_for("eastwood_qr10x", resource_manager=rm)
    assert not closed, "closed the caller's manager while formatting an error"


@pytest.mark.parametrize(
    "module",
    [
        "benchctrl.drivers.rigol_dp2031.driver",
        "benchctrl.drivers.rigol_dl3031a.driver",
        "benchctrl.drivers.siglent_sdm4065a.driver",
    ],
)
def test_no_driver_closes_the_manager_it_was_handed(monkeypatch, module):
    """The wiring half of the above, through each driver's own entry point.

    ``_autodiscover`` is called with the manager ``open()`` is about to use, so a
    driver that drops it on the floor gets an InvalidSession one line later.
    """
    import importlib

    closed = []

    class FakeRM:
        def list_resources(self):
            return BOARD_RESOURCES

        def close(self):
            closed.append(True)

    _patch_ports(monkeypatch, [])
    drv = importlib.import_module(module)
    assert drv._autodiscover(FakeRM())
    assert not closed, f"{module} closed the ResourceManager it was given"

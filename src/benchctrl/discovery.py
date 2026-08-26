"""Unified instrument discovery across every driver.

Before this module there were three unrelated mechanisms: the Arc filtered
``list_ports.comports()`` by VID/PID, the two Rigol drivers string-matched
``rm.list_resources()``, and the QR10x had none at all — you passed it a port
name and hoped. Each answered "where is *my* device"; none answered "what is
plugged into this bench", which is precisely the question the remote agent
has to answer for a host that cannot see the USB bus.

The driver-specific helpers still exist and still return their own types;
they now delegate here so there is one signature table to maintain.

Confidence
----------
Not every instrument is identifiable. Devices behind a generic USB-serial
bridge (CH340, FTDI, CP210x) share their VID/PID with thousands of unrelated
products, so a match on those is a *guess*. Every result carries a
:py:attr:`DiscoveredDevice.confidence` and callers must not silently open a
``heuristic`` match — probe it, or make the operator choose.

A probe result is graded ``heuristic`` too, and deliberately: it is evidence
about the *protocol* spoken on a port, which is much better than a shared
VID/PID but still not the certainty a signature match gives. ``exact`` stays
reserved for the signature table.
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field, replace
from typing import Iterable, Optional

from benchctrl.exceptions import BenchConnectionError

log = logging.getLogger("benchctrl.discovery")

EXACT = "exact"
HEURISTIC = "heuristic"
UNKNOWN = "unknown"


@dataclass(frozen=True)
class DriverSignature:
    """How to recognise one instrument on the bus."""

    device_key: str
    label: str
    vid: Optional[int] = None
    pid: Optional[int] = None
    transport: str = "serial"
    confidence: str = EXACT
    #: Substrings that, if present in the USB product/description, raise a
    #: heuristic match toward a positive identification.
    product_hints: tuple[str, ...] = ()
    note: str = ""


#: The single source of truth for instrument identification.
#:
#: The QR10x entry is deliberately absent: the codebase has never recorded a
#: VID/PID for it, and guessing a generic USB-bridge ID would produce
#: confident-looking false positives on any bench with an Arduino or an
#: ESP32 plugged in. It is discovered by probe instead — see
#: :py:func:`probe_serial_identity`.
SIGNATURES: tuple[DriverSignature, ...] = (
    DriverSignature(
        device_key="otii_arc",
        label="Qoitech Otii Arc / Arc Pro",
        vid=0x0FCE,
        pid=0xD1E6,
        transport="serial",
        confidence=EXACT,
        product_hints=("arc",),
    ),
    DriverSignature(
        device_key="rigol_dl3031a",
        label="Rigol DL3000-series electronic load",
        vid=0x1AB1,
        pid=0x0E11,
        transport="usbtmc",
        confidence=EXACT,
        product_hints=("dl30",),
    ),
    DriverSignature(
        device_key="rigol_dp2031",
        label="Rigol DP2000-series power supply",
        vid=0x1AB1,
        pid=0xA4A8,
        transport="usbtmc",
        confidence=EXACT,
        product_hints=("dp20",),
    ),
    DriverSignature(
        device_key="siglent_sdm4065a",
        label="Siglent SDM4000-series digital multimeter",
        vid=0xF4EC,
        pid=0x1220,
        transport="usbtmc",
        confidence=EXACT,
        product_hints=("sdm40",),
        # Siglent shares this VID/PID across the SDM4045A/4055A/4065A, so the
        # match identifies the *family*. The driver is model-specific (the
        # 4065A has a 1 MΩ top resistance range where the 4055A has 2 MΩ), so
        # a caller that must be sure of the model has to read ``*IDN?``.
        note="SDM4045A/4055A/4065A share this ID; check *IDN? for the model",
    ),
    DriverSignature(
        device_key="ontrak_adu218",
        label="Ontrak ADU218 USB relay / digital I/O interface",
        vid=0x0A07,
        pid=0x00DA,
        transport="usbfs",
        confidence=EXACT,
        product_hints=("adu218",),
        # The PID is per-model across the whole ADU family (the ADU208 is
        # 0x00D8, the ADU100 0x0064), so this identifies the exact model rather
        # than a family -- unlike the Siglent entry above. Do not widen it: the
        # ADU208 has mechanical relays with different switching limits, and the
        # driver's timing and CPS assumptions are the AQZ207's.
        #
        # ``transport="usbfs"`` is not a serial port, which matters twice:
        # :py:func:`scan_usbfs` is what finds it, and ``_is_probe_candidate``
        # requires ``transport == "serial"`` -- so this device is unprobeable by
        # construction. That is the right answer for a relay board and it comes
        # free, so nothing here relies on it: the signature identifies the
        # device from its descriptor alone and writes nothing.
        note="identification is passive; usbfs devices are never probed",
    ),
)

#: Labels for keys only a probe can return. Kept beside :py:data:`SIGNATURES`
#: rather than in it, because an entry in that table means "this VID/PID *is*
#: this instrument" and no VID/PID means that for the QR10x.
PROBE_LABELS: dict[str, str] = {
    "eastwood_qr10x": "Eastwood QR10x programmable resistance",
}

#: The path :py:func:`scan_driverless_bridges` reports for a bridge with no tty.
#: Not a device node, so there is nothing for a probe to open — see
#: :py:func:`_is_probe_candidate`.
AUTO_PATH = "auto"

#: Generic USB-serial bridges. A device behind one of these is *something*,
#: but the VID/PID says nothing about what.
GENERIC_BRIDGES: dict[tuple[int, int], str] = {
    (0x1A86, 0x7523): "CH340 USB-serial bridge",
    (0x1A86, 0x55D4): "CH9102 USB-serial bridge",
    (0x0403, 0x6001): "FTDI FT232 USB-serial bridge",
    (0x0403, 0x6015): "FTDI FT231X USB-serial bridge",
    (0x10C4, 0xEA60): "Silicon Labs CP210x USB-serial bridge",
    (0x067B, 0x2303): "Prolific PL2303 USB-serial bridge",
}

#: Device keys identifiable from a udev symlink, keyed by the symlink's basename
#: under :py:data:`SYMLINK_DIR`. Populated by ``deploy/udev/*.rules``.
#:
#: The *passive* counterpart to :py:data:`SERIAL_PROBES`: exact rather than
#: heuristic, and it writes nothing to the device. For the PDU41002 that is the
#: only acceptable option — probing it authenticates badly against a mains
#: switch; see the note on ``SERIAL_PROBES``.
SYMLINK_KEYS: dict[str, str] = {
    "pdu41002": "cyberpower_pdu41002",
}

#: Human labels for symlink-identified devices, for the inventory listing.
SYMLINK_LABELS: dict[str, str] = {
    "cyberpower_pdu41002": "CyberPower PDU41002 8-outlet switched PDU",
}

#: Where ``deploy/udev/*.rules`` place their symlinks.
SYMLINK_DIR = "/dev/benchctrl"


@dataclass(frozen=True)
class DiscoveredDevice:
    """One thing found on the bus."""

    path: str
    transport: str
    device_key: Optional[str] = None
    label: str = ""
    vid: Optional[int] = None
    pid: Optional[int] = None
    serial_number: Optional[str] = None
    description: str = ""
    manufacturer: Optional[str] = None
    product: Optional[str] = None
    confidence: str = UNKNOWN
    note: str = ""
    extra: dict = field(default_factory=dict)

    @property
    def identified(self) -> bool:
        """True when we know which driver owns this device."""
        return self.device_key is not None

    @property
    def usb_id(self) -> Optional[str]:
        if self.vid is None or self.pid is None:
            return None
        return f"{self.vid:04x}:{self.pid:04x}"

    def to_dict(self) -> dict:
        return {
            "path": self.path,
            "transport": self.transport,
            "device_key": self.device_key,
            "label": self.label,
            "usb_id": self.usb_id,
            "serial_number": self.serial_number,
            "description": self.description,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "confidence": self.confidence,
            "note": self.note,
            **({"extra": self.extra} if self.extra else {}),
        }

    def __str__(self) -> str:
        who = self.label or self.description or "unidentified"
        sn = f" sn={self.serial_number}" if self.serial_number else ""
        usb = f" [{self.usb_id}]" if self.usb_id else ""
        return f"{self.path} — {who}{usb}{sn} ({self.confidence})"


# ---------------------------------------------------------------------------
# Scanners
# ---------------------------------------------------------------------------


def scan_serial() -> list[DiscoveredDevice]:
    """Enumerate CDC-ACM / USB-serial ports and identify what we can."""
    try:
        import serial.tools.list_ports as list_ports
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        log.warning("pyserial unavailable; serial discovery disabled")
        return []

    found: list[DiscoveredDevice] = []
    for p in list_ports.comports():
        vid = getattr(p, "vid", None)
        pid = getattr(p, "pid", None)
        product = getattr(p, "product", None)
        sig = _match_signature(vid, pid, "serial")

        if sig is not None:
            found.append(
                DiscoveredDevice(
                    path=p.device,
                    transport="serial",
                    device_key=sig.device_key,
                    label=sig.label,
                    vid=vid,
                    pid=pid,
                    serial_number=getattr(p, "serial_number", None),
                    description=p.description or "",
                    manufacturer=getattr(p, "manufacturer", None),
                    product=product,
                    confidence=sig.confidence,
                    note=sig.note,
                )
            )
            continue

        # Passive identification for devices behind a generic bridge, where
        # VID/PID cannot decide. Safe to do here — unlike probing — because it
        # only resolves symlinks. It is how the PDU41002 gets identified at all:
        # probing it writes into its login prompt (see SERIAL_PROBES).
        by_symlink = identify_by_symlink(p.device)
        if by_symlink is not None:
            found.append(
                DiscoveredDevice(
                    path=p.device,
                    transport="serial",
                    device_key=by_symlink,
                    label=SYMLINK_LABELS.get(by_symlink, by_symlink),
                    vid=vid,
                    pid=pid,
                    serial_number=getattr(p, "serial_number", None),
                    description=p.description or "",
                    manufacturer=getattr(p, "manufacturer", None),
                    product=product,
                    confidence=EXACT,
                    note=(
                        "identified by udev symlink, not VID/PID — open it by "
                        f"{SYMLINK_DIR}/ path so the binding survives "
                        "re-enumeration"
                    ),
                )
            )
            continue

        bridge = GENERIC_BRIDGES.get((vid, pid)) if vid and pid else None
        found.append(
            DiscoveredDevice(
                path=p.device,
                transport="serial",
                device_key=None,
                label=bridge or "",
                vid=vid,
                pid=pid,
                serial_number=getattr(p, "serial_number", None),
                description=p.description or "",
                manufacturer=getattr(p, "manufacturer", None),
                product=product,
                confidence=UNKNOWN,
                note=(
                    "generic USB-serial bridge — identity cannot be inferred "
                    "from VID/PID; probe to identify"
                )
                if bridge
                else "",
            )
        )
    return found


def scan_driverless_bridges() -> list[DiscoveredDevice]:
    """CH340 adapters the kernel has *not* bound to a tty.

    :py:func:`scan_serial` cannot see these by construction:
    ``list_ports.comports()`` enumerates ttys, and the whole problem on a
    kernel without ``CONFIG_USB_SERIAL_CH341`` is that no tty exists. Without
    this scanner the QR10x is simply absent from the inventory on the Uno Q,
    which reads as "not plugged in" when it is plugged in and usable.

    The reported ``path`` is ``"auto"``, not a device node — there is no node
    until :py:mod:`benchctrl.transports.autoserial` creates a pty, and that is
    also the value to pass as ``port`` to open it.
    """
    try:
        from benchctrl.transports.ch341 import CH340_PID, CH340_VID, CH341Device
    except ImportError:  # pragma: no cover - pyusb is optional
        return []

    try:
        import serial.tools.list_ports as list_ports

        bound = any(
            getattr(p, "vid", None) == CH340_VID and getattr(p, "pid", None) == CH340_PID
            for p in list_ports.comports()
        )
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        bound = False
    if bound:
        # The kernel has it; scan_serial already reported it, and reporting it
        # again as driverless would be wrong as well as duplicated.
        return []

    try:
        devices = CH341Device.find_all()
    except BenchConnectionError:
        # pyusb missing or no USB access — not a discovery failure, just no
        # answer available from this scanner.
        return []

    found: list[DiscoveredDevice] = []
    for dev in devices:
        found.append(
            DiscoveredDevice(
                path="auto",
                transport="serial",
                device_key=None,
                label=GENERIC_BRIDGES.get((CH340_VID, CH340_PID), "CH340 bridge"),
                vid=CH340_VID,
                pid=CH340_PID,
                serial_number=_string_of_safe(dev, "iSerialNumber"),
                description="CH340 with no kernel driver bound",
                manufacturer=_string_of_safe(dev, "iManufacturer"),
                product=_string_of_safe(dev, "iProduct"),
                confidence=UNKNOWN,
                note=(
                    "no kernel ch341 driver, so no /dev/ttyUSB* — reachable via "
                    "the userspace CH341 driver. Open with port='auto'. Identity "
                    "cannot be inferred from VID/PID; probe to identify."
                ),
            )
        )
    return found


def _string_of_safe(dev, index_attr: str) -> Optional[str]:
    """A USB string descriptor by its index attribute, or None if unreadable.

    Reading these needs write access to ``/dev/bus/usb/*`` (pyusb raises
    ``ValueError: The device has no langid`` without it), which is precisely
    what the udev rule grants. A device we cannot name is still worth
    reporting, so this must not raise.
    """
    try:
        import usb.util

        return usb.util.get_string(dev, getattr(dev, index_attr))
    except Exception:  # noqa: BLE001 - any failure means "unknown", not an error
        return None


def scan_usbtmc() -> list[DiscoveredDevice]:
    """Enumerate kernel USB-TMC nodes (``/dev/usbtmc*``).

    Present on Linux when the ``usbtmc`` driver has bound the instrument.
    The node itself carries no VID/PID, so identification comes from sysfs
    where available.
    """
    found: list[DiscoveredDevice] = []
    for path in sorted(glob.glob("/dev/usbtmc*")):
        vid, pid, serial_no, product, manufacturer = _usbtmc_sysfs_identity(path)
        sig = _match_signature(vid, pid, "usbtmc")
        found.append(
            DiscoveredDevice(
                path=path,
                transport="usbtmc",
                device_key=sig.device_key if sig else None,
                label=sig.label if sig else (product or ""),
                vid=vid,
                pid=pid,
                serial_number=serial_no,
                description=product or "",
                manufacturer=manufacturer,
                product=product,
                confidence=sig.confidence if sig else UNKNOWN,
            )
        )
    return found


def scan_usbfs() -> list[DiscoveredDevice]:
    """Enumerate USB devices driven through raw usbdevfs rather than a tty.

    Today that means the ADU218: it is USB HID, and ``usbhid`` ignores Ontrak
    devices deliberately (``hid_ignore_list`` in the kernel's ``hid-quirks.c``),
    so there is no ``/dev/hidraw*`` node and no tty either. The only evidence it
    exists is its sysfs entry and its ``/dev/bus/usb`` node, which is what this
    reads.

    **Entirely passive.** It reads sysfs attributes the kernel already populated
    during enumeration — no node is opened, no interface is claimed, and nothing
    is written. That matters for a board whose outputs are relays: a scan must
    never be the thing that actuates one, and this cannot, because it never
    acquires a handle capable of it.

    This scanner exists in the same change as the ADU218's
    :py:data:`SIGNATURES` entry, and the two must never be separated. A
    signature with no scanner is worse than no entry at all: the dashboard's
    "on bus" denominator would count the device as scannable, find nothing, and
    report ``NOT FOUND`` for an instrument that is plugged in, served, and open.
    A missing signature merely leaves a device unidentified; a missing scanner
    makes the panel confidently wrong.

    Returns an empty list rather than raising when the transport cannot be
    scanned at all — no driver package, or no ``/sys/bus/usb`` (a container, a
    non-Linux host, a sandboxed test runner). An unscannable transport is not a
    discovery error, and the alternative was measured: ``enumerate_devices()``
    raises for a missing sysfs root, which is correct for a *driver* about to
    open a device and wrong for a scan. Letting it propagate took out every
    other transport's results too, because :py:func:`discover` builds one merged
    list — so a machine with no USB sysfs reported no VISA instruments either.
    """
    try:
        from benchctrl.drivers.ontrak_adu218.usbfs import (
            Adu218LinkError,
            enumerate_devices,
        )
    except ImportError as exc:  # pragma: no cover - driver always present
        log.debug("usbfs scan unavailable: %s", exc)
        return []

    try:
        devices = enumerate_devices()
    except Adu218LinkError as exc:
        log.debug("usbfs scan found no enumerable bus: %s", exc)
        return []

    sig = _match_signature(0x0A07, 0x00DA, "usbfs")
    found: list[DiscoveredDevice] = []
    for device in devices:
        found.append(
            DiscoveredDevice(
                path=device.path,
                transport="usbfs",
                device_key=sig.device_key if sig else None,
                label=sig.label if sig else (device.product or ""),
                vid=0x0A07,
                pid=0x00DA,
                serial_number=device.serial,
                description=device.product or "",
                manufacturer=device.manufacturer,
                product=device.product,
                confidence=sig.confidence if sig else UNKNOWN,
            )
        )
    return found


def scan_visa(resource_manager=None) -> list[DiscoveredDevice]:
    """Enumerate VISA resources, if a VISA backend is installed.

    Returns an empty list rather than raising when pyvisa is absent — VISA
    is an optional extra, and a bench with only serial instruments is a
    perfectly valid bench.
    """
    try:
        import pyvisa
    except ImportError:
        log.debug("pyvisa not installed; VISA discovery skipped")
        return []

    close_after = False
    rm = resource_manager
    if rm is None:
        try:
            rm = pyvisa.ResourceManager()
            close_after = True
        except Exception as exc:
            log.debug("no usable VISA backend: %s", exc)
            return []

    try:
        resources = sorted(rm.list_resources())
    except Exception as exc:
        # Warning, not debug: an empty list here is indistinguishable from "no
        # VISA instruments plugged in", so at debug level a total loss of the
        # VISA bus looks exactly like an idle bench. That is precisely how the
        # closed-singleton bug stayed hidden — the dashboard rendered NOT FOUND
        # for three connected instruments and the service log said nothing at
        # its normal level.
        log.warning(
            "VISA list_resources failed, so this scan reports no VISA "
            "instruments even if some are connected: %s",
            exc,
        )
        return []
    finally:
        if close_after:
            try:
                rm.close()
            except Exception:
                pass

    found: list[DiscoveredDevice] = []
    for res in resources:
        vid, pid = _visa_usb_ids(res)
        sig = _match_signature(vid, pid, "usbtmc")
        found.append(
            DiscoveredDevice(
                path=res,
                transport="visa",
                device_key=sig.device_key if sig else None,
                label=sig.label if sig else "",
                vid=vid,
                pid=pid,
                confidence=sig.confidence if sig else UNKNOWN,
            )
        )
    return found


def discover(
    *,
    serial: bool = True,
    usbtmc: bool = True,
    visa: bool = True,
    usbfs: bool = True,
    probe: bool = False,
    resource_manager=None,
) -> list[DiscoveredDevice]:
    """Scan every enabled transport and return one merged inventory.

    Results are de-duplicated: a VISA ``USB0::0x1AB1::...`` resource and the
    ``/dev/usbtmc0`` node it is bound to are the same instrument, and
    reporting both would make a bench look twice as populated as it is. The
    VISA form wins because that is what the driver needs to open it.

    ``resource_manager`` is passed through to :py:func:`scan_visa`, which will
    not close a manager it did not create. A caller that holds one must pass it:
    ``pyvisa.ResourceManager()`` is a singleton, so closing a second handle
    invalidates the first.

    ``probe`` is **off by default and must stay that way**. A scan is otherwise
    read-only — it reads USB descriptors and sysfs — whereas probing *writes* to
    a port whose occupant is by definition unknown. The bench dashboard re-scans
    every 30 s, so a probing default would have the panel type at whatever is
    plugged into the bench, forever, unasked. Opt in only where a caller has a
    reason to want an identity badly enough to send bytes for it: the CLI's
    explicit ``--probe``, or an operator asking the agent "what is that". See
    :py:func:`probe_unidentified` for what is probed, which is a much narrower
    set than "everything unidentified".

    That default hardened from a preference into a rule when a switched PDU
    joined the probe candidates: one of the ports a probe would write to is a
    mains contactor's control port, and enumerating a bench must never be the
    thing that writes bytes to it.
    """
    found: list[DiscoveredDevice] = []
    if serial:
        found.extend(scan_serial())
        # Bridges with no tty. Additive by construction: scan_serial only
        # reports ttys, and this only reports adapters that have none.
        found.extend(scan_driverless_bridges())
    visa_devices = scan_visa(resource_manager) if visa else []
    if usbtmc:
        seen_usb = {(d.vid, d.pid, d.serial_number) for d in visa_devices}
        for dev in scan_usbtmc():
            if (dev.vid, dev.pid, dev.serial_number) in seen_usb:
                continue
            found.append(dev)
    found.extend(visa_devices)
    if usbfs:
        # Additive by construction, like scan_driverless_bridges: a usbdevfs
        # device has no tty and no usbtmc node, so nothing above can have
        # reported it and there is nothing to de-duplicate against.
        found.extend(scan_usbfs())
    if probe:
        found = probe_unidentified(found)
    return sorted(found, key=lambda d: (d.transport, d.path))


def find_for(device_key: str, **kwargs) -> list[DiscoveredDevice]:
    """Every discovered device belonging to ``device_key``."""
    return [d for d in discover(**kwargs) if d.device_key == device_key]


def visa_resource_for(
    device_key: str,
    *,
    error: type = BenchConnectionError,
    resource_manager=None,
) -> str:
    """The one VISA resource string for ``device_key``, or raise.

    The single place a VISA-based driver's ``open(resource=None)`` should get its
    resource from. Each driver used to scan ``rm.list_resources()`` itself and
    substring-match a hex VID/PID, which is wrong twice over:

    * **Backends disagree on the radix.** The same DP2031 is
      ``USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR`` under NI-VISA and
      ``USB0::6833::42152::DP2A243500269::0::INSTR`` under pyvisa-py. Looking for
      the literal text ``0x1ab1`` finds nothing in the decimal form, so on
      exactly the boards that need pyvisa-py (no kernel ``usbtmc`` module) the
      instrument was listed by ``list_resources()`` and invisible to its own
      driver. Bench-verified on the Uno Q, where the error names the very
      resource string it just rejected.
    * **Substring matching is wrong in principle.** A serial number can contain
      the digits of a VID, so the match can land on the wrong field.

    :py:func:`_visa_usb_ids` parses the ``::``-separated fields and accepts both
    radixes, and going through :py:func:`find_for` means there is one signature
    table rather than one per driver — which is what this module was introduced
    to do.

    Args:
        device_key: which instrument to find.
        error: exception class to raise, so a driver keeps its own error type in
            its public contract instead of leaking this module's.
        resource_manager: the caller's ResourceManager. **Pass it** if you hold
            one. ``pyvisa.ResourceManager()`` is a *singleton* — a second call
            returns the same object — so a scan that helpfully created "its own"
            and closed it afterwards would close the caller's, and its next
            ``open_resource`` fails with ``InvalidSession: Invalid session
            handle``. Bench-verified on the Uno Q, where it turned a working
            supply into an unopenable one while the DMM (which passes its manager
            through) kept working.

    Raises:
        error: if none are found, or if several are — never a silent pick. Two
            identical supplies on a bench is rare and choosing one at random is
            how you ramp the wrong rail.
    """
    matches = [
        d.path
        for d in find_for(
            device_key, serial=False, usbtmc=False, resource_manager=resource_manager
        )
    ]
    if len(matches) == 1:
        return matches[0]
    sig = next((s for s in SIGNATURES if s.device_key == device_key), None)
    what = sig.label if sig else device_key
    if not matches:
        ids = f" (VID {sig.vid:#06x} / PID {sig.pid:#06x})" if sig else ""
        raise error(
            f"no {what}{ids} found. VISA reported: "
            f"{_visa_resource_names(resource_manager)!r}. "
            f"Check the USB cable, and that the VISA backend can see USB devices "
            f"(pyvisa-py needs pyusb + libusb, and a udev rule to read the "
            f"device's string descriptors)."
        )
    raise error(
        f"several {what} devices found: {matches!r}. Pass resource= to choose."
    )


def _visa_resource_names(resource_manager=None) -> list[str]:
    """Every VISA resource, for an error message. Never raises.

    Only closes a manager it created itself — see
    :py:func:`visa_resource_for` on why closing the caller's is destructive.
    """
    try:
        import pyvisa

        rm = resource_manager
        if rm is not None:
            return sorted(rm.list_resources())
        rm = pyvisa.ResourceManager()
        try:
            return sorted(rm.list_resources())
        finally:
            rm.close()
    except Exception as exc:  # noqa: BLE001 - decorating an error, not raising one
        return [f"<VISA unavailable: {exc}>"]


def unidentified(**kwargs) -> list[DiscoveredDevice]:
    """Devices present but unclaimed — candidates for a probe."""
    return [d for d in discover(**kwargs) if not d.identified]


def inventory(*, probe: bool = False, **kwargs) -> dict:
    """A JSON-friendly bench summary, for the CLI and the remote agent.

    ``probe`` is spelled out here rather than left to ``**kwargs`` because this
    is the function the agent's ``agent.discover`` and the dashboard's 30 s
    inventory poll call. The default that keeps a repeating poll from writing to
    unknown hardware should be visible at the entry point that does the polling,
    not inferred two frames down — see :py:func:`discover`.
    """
    devices = discover(probe=probe, **kwargs)
    by_key: dict[str, list[dict]] = {}
    for d in devices:
        by_key.setdefault(d.device_key or "_unidentified", []).append(d.to_dict())
    return {
        "count": len(devices),
        "identified": sum(1 for d in devices if d.identified),
        "devices": [d.to_dict() for d in devices],
        "by_device_key": by_key,
    }


# ---------------------------------------------------------------------------
# Probing
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SerialProbe:
    """One "write these bytes at this baud and see who answers" attempt.

    Probes are tried in order and the first match wins, so the ordering in
    :py:data:`SERIAL_PROBES` is part of the behaviour, not incidental.

    Attributes:
        device_key: what a match identifies.
        baudrate: probes are baud-specific. Two of our devices now sit behind
            the *same* FTDI bridge at different rates, so a single hardcoded
            baud can no longer identify the bus.
        request: bytes to write. Always a write, never a bare read: a silent
            device is indistinguishable from an absent one otherwise.
        markers: substrings, **any** of which means "this is it". A tuple
            rather than one string because a device with a login prompt answers
            differently depending on whether a session is already open, and
            both answers identify it. Matching is case-sensitive.
        label: human description, for logs.
    """

    device_key: str
    baudrate: int
    request: bytes
    markers: tuple[str, ...]
    label: str = ""


#: Probe attempts, in order. First match wins.
#:
#: **The PDU41002 is deliberately not in this list**, and that absence is a
#: finding rather than an omission. The plan called for a bare-``\r`` probe at
#: 9600 on the assumption that a CR merely re-prompts an idle console. Measured
#: on firmware 1.3.4, it does not: a CR *submits an empty line* to whatever
#: login field is current, so successive CRs walk the device's authentication
#: state machine —
#:
#:   CR 1 -> ``\r\n\r\nLogin Name : ``      (identifiable)
#:   CR 2 -> ``\r\nLogin Password : ``      (empty username submitted)
#:   CR 3 -> ``Please wait for authentication....`` then, ~15 s later,
#:           ``Login Failed`` and back to ``Login Name : ``
#:
#: Three consequences, each fatal to the idea of probing this device:
#:
#: 1. **A probe cannot identify it reliably.** The vendor string appears in only
#:    one of the three states, so the answer depends on what touched the console
#:    beforehand. Measured directly: five consecutive probe calls returned
#:    ``None, cyberpower_pdu41002, None, None, None``.
#: 2. **It is not read-only.** The device logged
#:    ``Login authorization failure via Console`` for the probe traffic — a
#:    discovery sweep writing authentication failures into a mains switch's
#:    audit log, which also makes a real intrusion harder to spot.
#: 3. **It blocks the console for ~15 s.** During the authentication delay the
#:    port answers nothing, so a probe can leave the device unusable to the
#:    driver that follows it.
#:
#: No inert alternative exists: opening the port, and writing ``?``, DEL or NUL
#: without a terminator, all produce **no reply at all**, so there is nothing to
#: match on. The PDU is therefore identified *passively* by its udev symlink
#: (``deploy/udev/62-benchctrl-ftdi.rules``, keyed on the adapter's serial
#: number) — see :py:func:`identify_by_symlink`. That is strictly better than a
#: probe anyway: it is exact rather than heuristic, and it writes nothing.
SERIAL_PROBES: tuple[SerialProbe, ...] = (
    SerialProbe(
        device_key="eastwood_qr10x",
        baudrate=115200,
        request=b"AT+DEV.TYPE?\r\n",
        # The leading "+" and trailing "=" are both load-bearing. A marker of
        # "DEV.TYPE" is a substring of this probe's own request, so any device
        # that ECHOES what it receives matches it — and the PDU echoes on its
        # serial console. Measured at 9600: the PDU answered
        # `b'AT+DEV.TYPE\n....'`, which "DEV.TYPE" matched, so discovery
        # identified a mains switch as a programmable resistor. The QR10x's real
        # reply is `+DEV.TYPE=QR101A-…`; an echoed request ends in `?`, never
        # `=`.
        #
        # At 115200 the PDU returns framing garbage (all-zero bytes) rather than
        # its echo, so the baud mismatch masked this — but nothing guarantees a
        # future device behind the same bridge shares that luck, and a marker
        # satisfied by its own echo is wrong regardless of who it happens to
        # spare.
        markers=("+DEV.TYPE=",),
        label="Eastwood QR10x (AT command set)",
    ),
)


def identify_by_symlink(
    path: str, symlink_dir: Optional[str] = None
) -> Optional[str]:
    """Identify a serial device by a udev symlink pointing at it.

    Passive: resolves symlinks and compares paths. Nothing is opened and nothing
    is written, which is what makes it usable on a device that switches mains.

    Args:
        path: the ``/dev/tty*`` node to identify.
        symlink_dir: where to look. Defaults to :py:data:`SYMLINK_DIR`, read at
            *call* time rather than as a default argument value — a
            ``symlink_dir: str = SYMLINK_DIR`` default binds at import, which
            silently ignores anyone (tests included) monkeypatching the module
            constant.

    Returns the device key, or None when no symlink resolves to ``path``. Never
    raises — a missing ``/dev/benchctrl`` just means the udev rules are not
    installed, which is a normal state on a developer laptop.
    """
    import os

    if symlink_dir is None:
        symlink_dir = SYMLINK_DIR

    try:
        target = os.path.realpath(path)
        entries = os.listdir(symlink_dir)
    except OSError as exc:
        log.debug("no symlink identification for %s: %s", path, exc)
        return None

    for name in entries:
        key = SYMLINK_KEYS.get(name)
        if key is None:
            continue
        try:
            if os.path.realpath(os.path.join(symlink_dir, name)) == target:
                return key
        except OSError:  # pragma: no cover - a symlink vanishing mid-scan
            continue
    return None


def probe_serial_identity(path: str, timeout: float = 1.0) -> Optional[str]:
    """Ask an unidentified serial device what it is.

    Only for devices behind a generic bridge, where VID/PID cannot decide — the
    QR10x's CH340 and the PDU's FT232R are both in
    :py:data:`GENERIC_BRIDGES`.

    Tries :py:func:`identify_by_symlink` first, because it is exact and writes
    nothing, then each entry in :py:data:`SERIAL_PROBES` in order, reopening the
    port at that probe's baud rate. Stops at the first match.

    **This function writes to the port**, so it is opt-in rather than part of
    ``discover()``. The PDU41002 is not probeable at all — a bare CR submits an
    empty login line, and repeated probes log authentication failures on it and
    block its console for ~15 s — so it relies entirely on the symlink path
    above. See the note on :py:data:`SERIAL_PROBES`.

    Returns the matching device key, or None. Never raises: probing is
    best-effort by nature, and a device that ignores us is simply not one of
    ours.
    """
    symlinked = identify_by_symlink(path)
    if symlinked is not None:
        return symlinked

    try:
        import serial
    except ImportError:  # pragma: no cover
        return None

    for probe in SERIAL_PROBES:
        try:
            with serial.Serial(path, probe.baudrate, timeout=timeout) as ser:
                ser.reset_input_buffer()
                ser.write(probe.request)
                ser.flush()
                reply = ser.read(256).decode("ascii", errors="replace")
                if any(marker in reply for marker in probe.markers):
                    return probe.device_key
        except Exception as exc:
            log.debug(
                "probe %s of %s failed (expected for foreign devices): %s",
                probe.device_key,
                path,
                exc,
            )
    return None


def _is_probe_candidate(dev: DiscoveredDevice) -> bool:
    """Whether it is acceptable to write bytes at this device.

    Three conditions, each of which has to hold:

    * **Not already identified.** The signature table decided, so there is
      nothing to learn and everything to lose: ``AT+DEV.TYPE?`` sent at a power
      supply is a stray command on an instrument that may be driving a rail.
      This is the guard that matters — the others only avoid wasted time.
    * **Serial transport.** ``AT+DEV.TYPE?`` is a serial-line protocol; there is
      no reason to write it into a USB-TMC node or a VISA resource.
    * **Behind a known generic bridge.** That is the only situation where
      VID/PID *cannot* decide and a probe is the sole remaining answer. A device
      on an unrecognised VID/PID is unidentified for a different reason — nobody
      has written its signature yet — and poking arbitrary hardware to discover
      that is not a trade this module makes.

    ``path == "auto"`` is excluded as a consequence of the last condition being
    about openable ports: the driverless-bridge scanner reports a placeholder,
    not a node, and probing it would mean claiming the chip over libusb behind
    the caller's back.
    """
    if dev.identified:
        return False
    if dev.transport != "serial":
        return False
    if dev.path == AUTO_PATH:
        return False
    return (dev.vid, dev.pid) in GENERIC_BRIDGES


def probe_unidentified(
    devices: Iterable[DiscoveredDevice], *, timeout: float = 1.0
) -> list[DiscoveredDevice]:
    """Identify what it can by probing, returning an upgraded list.

    Written as a pure-ish transform over a scan result so the decision of
    *whether* to probe stays at the caller's level (see :py:func:`discover`)
    and the decision of *what* is safe to probe lives in exactly one place,
    :py:func:`_is_probe_candidate`.

    A successful reply grades :py:data:`HEURISTIC`, not :py:data:`EXACT`. The
    device answered our protocol, which is strong evidence and far better than
    a shared VID/PID — but ``exact`` in this module means "the signature table
    says so", and a QR10x cannot be told from some other AT-speaking box that
    happens to accept ``DEV.TYPE`` on the strength of one reply.

    Silence is **not** evidence of absence, so a non-answer leaves the device
    exactly as the scan reported it: still unidentified, still noted as
    probeable. A CH340 whose device is powered down, mid-boot, or held open by
    another process is silent, and demoting it to "definitely not ours" would
    make the bench panel assert an absence it has not established.

    Order and length are preserved: callers de-duplicate and sort on the way
    out, and a probe that dropped a silent device would delete it from the
    inventory for failing to answer.
    """
    upgraded: list[DiscoveredDevice] = []
    for dev in devices:
        if not _is_probe_candidate(dev):
            upgraded.append(dev)
            continue
        key = _probe_quietly(dev.path, timeout)
        if key is None:
            upgraded.append(dev)
            continue
        log.info("probe identified %s as %s", dev.path, key)
        upgraded.append(
            replace(
                dev,
                device_key=key,
                label=PROBE_LABELS.get(key, dev.label),
                confidence=HEURISTIC,
                note=f"identified by serial probe, not by VID/PID ({dev.usb_id})",
            )
        )
    return upgraded


def _probe_quietly(path: str, timeout: float) -> Optional[str]:
    """:py:func:`probe_serial_identity`, but immune to a caller's monkeypatch.

    ``probe_serial_identity`` already swallows everything a serial port can
    throw. This exists because it is a module-level name a test or a caller can
    replace, and one substitute that raises must not abort a scan of a bench
    with five instruments on it — the same reason
    :py:func:`scan_driverless_bridges` treats a USB access failure as "no
    answer" rather than a discovery failure.
    """
    try:
        return probe_serial_identity(path, timeout=timeout)
    except Exception as exc:  # noqa: BLE001 - a failed probe is "unknown", not fatal
        log.debug("probe of %s raised: %s", path, exc)
        return None


# ---------------------------------------------------------------------------
# internals
# ---------------------------------------------------------------------------


def _match_signature(
    vid: Optional[int], pid: Optional[int], transport: str
) -> Optional[DriverSignature]:
    if vid is None or pid is None:
        return None
    for sig in SIGNATURES:
        if sig.vid == vid and sig.pid == pid:
            return sig
    return None


def _visa_usb_ids(resource: str) -> tuple[Optional[int], Optional[int]]:
    """Pull VID/PID out of ``USB0::0x1AB1::0x0E11::SN::INSTR``."""
    parts = resource.split("::")
    ids: list[int] = []
    for part in parts[1:3]:
        try:
            ids.append(int(part, 16) if part.lower().startswith("0x") else int(part))
        except ValueError:
            return None, None
    if len(ids) != 2:
        return None, None
    return ids[0], ids[1]


def _usbtmc_sysfs_identity(
    path: str,
) -> tuple[Optional[int], Optional[int], Optional[str], Optional[str], Optional[str]]:
    """Read idVendor/idProduct/serial from sysfs for a ``/dev/usbtmcN`` node."""
    name = os.path.basename(path)
    base = f"/sys/class/usbmisc/{name}/device"
    if not os.path.isdir(base):
        base = f"/sys/class/usb/{name}/device"
    if not os.path.isdir(base):
        return None, None, None, None, None

    def read(rel: str) -> Optional[str]:
        for depth in ("", "../", "../../"):
            try:
                with open(os.path.join(base, depth + rel)) as f:
                    return f.read().strip()
            except OSError:
                continue
        return None

    def read_hex(rel: str) -> Optional[int]:
        raw = read(rel)
        try:
            return int(raw, 16) if raw else None
        except ValueError:
            return None

    return (
        read_hex("idVendor"),
        read_hex("idProduct"),
        read("serial"),
        read("product"),
        read("manufacturer"),
    )


def format_inventory(devices: Iterable[DiscoveredDevice]) -> str:
    """Human-readable listing for the CLI."""
    devices = list(devices)
    if not devices:
        return "No instruments found."
    lines = []
    for d in devices:
        marker = "*" if d.identified else " "
        key = d.device_key or "?"
        lines.append(f"{marker} {key:<16} {d}")
    identified = sum(1 for d in devices if d.identified)
    lines.append(f"\n{len(devices)} device(s), {identified} identified.")
    return "\n".join(lines)

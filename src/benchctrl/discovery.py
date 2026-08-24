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
"""

from __future__ import annotations

import glob
import logging
import os
from dataclasses import dataclass, field
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
)

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
    except Exception as exc:  # pragma: no cover - backend-specific
        log.debug("VISA list_resources failed: %s", exc)
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
) -> list[DiscoveredDevice]:
    """Scan every enabled transport and return one merged inventory.

    Results are de-duplicated: a VISA ``USB0::0x1AB1::...`` resource and the
    ``/dev/usbtmc0`` node it is bound to are the same instrument, and
    reporting both would make a bench look twice as populated as it is. The
    VISA form wins because that is what the driver needs to open it.

    **Passive.** Nothing here writes to a device; identification is by VID/PID
    and sysfs only. Devices behind a generic bridge therefore come back
    unidentified, and turning them into a device key needs an explicit
    :py:func:`probe_serial_identity` call. That separation is worth keeping now
    that one probe candidate is a switched PDU: enumerating a bench must never
    be the thing that writes bytes to a mains contactor's control port.
    """
    found: list[DiscoveredDevice] = []
    if serial:
        found.extend(scan_serial())
        # Bridges with no tty. Additive by construction: scan_serial only
        # reports ttys, and this only reports adapters that have none.
        found.extend(scan_driverless_bridges())
    visa_devices = scan_visa() if visa else []
    if usbtmc:
        seen_usb = {(d.vid, d.pid, d.serial_number) for d in visa_devices}
        for dev in scan_usbtmc():
            if (dev.vid, dev.pid, dev.serial_number) in seen_usb:
                continue
            found.append(dev)
    found.extend(visa_devices)
    return sorted(found, key=lambda d: (d.transport, d.path))


def find_for(device_key: str, **kwargs) -> list[DiscoveredDevice]:
    """Every discovered device belonging to ``device_key``."""
    return [d for d in discover(**kwargs) if d.device_key == device_key]


def unidentified(**kwargs) -> list[DiscoveredDevice]:
    """Devices present but unclaimed — candidates for a probe."""
    return [d for d in discover(**kwargs) if not d.identified]


def inventory(**kwargs) -> dict:
    """A JSON-friendly bench summary, for the CLI and the remote agent."""
    devices = discover(**kwargs)
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
        marker: substring whose presence in the reply means "this is it".
        label: human description, for logs.
    """

    device_key: str
    baudrate: int
    request: bytes
    marker: str
    label: str = ""


#: Probe attempts, in order. Kept ordered most-specific-first.
#:
#: **One of these devices switches mains power.** The PDU probe is a bare ``\r``
#: — the gentlest thing that makes this CLI answer — and it cannot switch
#: anything: no outlet verb is sent, and a CR at either the login prompt or the
#: ready prompt merely re-prompts. It is still the reason ``discover()`` keeps
#: ``probe=False`` as its default: writing bytes to unknown serial ports is
#: acceptable for a resistance box and worth a deliberate opt-in when one of the
#: candidates is a power distribution unit.
SERIAL_PROBES: tuple[SerialProbe, ...] = (
    SerialProbe(
        device_key="eastwood_qr10x",
        baudrate=115200,
        request=b"AT+DEV.TYPE?\r\n",
        marker="DEV.TYPE",
        label="Eastwood QR10x (AT command set)",
    ),
    SerialProbe(
        device_key="cyberpower_pdu41002",
        baudrate=9600,
        # A bare CR: the PDU's CLI answers with either its ready prompt or a
        # login prompt, both of which are unambiguous and neither of which
        # changes any state.
        request=b"\r",
        marker="CyberPower",
        label="CyberPower PDU41002 (switched PDU — CLI prompt)",
    ),
)


def probe_serial_identity(path: str, timeout: float = 1.0) -> Optional[str]:
    """Ask an unidentified serial device what it is.

    Only for devices behind a generic bridge, where VID/PID cannot decide —
    which is now the common case rather than an edge one: the QR10x and the
    PDU41002 both appear as FTDI/CH-class bridges, and the PDU's
    ``(0x0403, 0x6001)`` is already in :py:data:`GENERIC_BRIDGES`.

    Tries each entry in :py:data:`SERIAL_PROBES` in order, reopening the port at
    that probe's baud rate. Returns the matching device key, or None. Never
    raises: probing is best-effort by nature, and a device that ignores us is
    simply not one of ours.
    """
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
                if probe.marker in reply:
                    return probe.device_key
        except Exception as exc:
            log.debug(
                "probe %s of %s failed (expected for foreign devices): %s",
                probe.device_key,
                path,
                exc,
            )
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

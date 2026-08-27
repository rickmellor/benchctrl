"""HID feature-report transport for the CP2112, over ``/dev/hidraw``.

This is the CP2112's equivalent of the PDU's ``links.py``: a thin byte-level
seam with no protocol knowledge, so every report layout, parser and safety rule
lives once in :py:mod:`.driver` above it.

Why hidraw and not usbfs
------------------------
The ADU218 driver in this repo talks raw USBDEVFS, and the obvious assumption
is that a second HID device wants the same transport. It does not, and both
halves of the reason were measured on the board rather than assumed:

1. ``usbhid`` **does** claim the CP2112 — its interface appears under
   ``/sys/bus/usb/drivers/usbhid/`` and it binds ``hid-generic`` — so a
   ``/dev/hidraw`` node exists. Contrast the ADU218, which ``usbhid``
   deliberately ignores via the kernel's ``hid_ignore_list``, leaving no node
   at all and forcing the usbfs path. Claiming a USB interface the kernel
   already owns would need ``USBDEVFS_DISCONNECT``, i.e. tearing a bound
   driver off the device. hidraw is the sanctioned route.
2. ``hid-cp2112`` is **not** built for this kernel (nothing matching
   ``*cp2112*`` under ``/lib/modules``). Where that module *is* present it
   binds the chip and exposes it as a kernel I2C adapter, and userspace HID
   access then contends with a driver that believes it owns the SMBus engine.
   Here nothing competes for it. A driver that assumed otherwise would work on
   this board and fail confusingly on a distro kernel, so
   :py:func:`open_hidraw` reports what it finds instead of guessing.

Both routes stay dependency-free: ``os`` + ``fcntl`` + ``ctypes``, no
``hidapi``, no ``cython-hidapi`` wheel to build on a board with no compiler.

Feature reports, not endpoint writes
------------------------------------
The CP2112's GPIO commands are HID **feature** reports. They travel through the
``HIDIOCSFEATURE``/``HIDIOCGFEATURE`` ioctls, *not* through ``write()`` on the
interrupt OUT endpoint — writing report 0x04 as an output report is silently
ignored by the chip, which is a genuinely confusing failure because the write
succeeds and returns the full byte count. Only the SMBus data transfers
(reports 0x10-0x17) are true output/input reports.

Consequence worth stating because it looks like a mistake: the node is opened
``O_RDWR`` even for a pure read. ``HIDIOCGFEATURE`` sends a GET_REPORT request
over the control pipe, so the kernel requires write access on the fd.
"""

from __future__ import annotations

import ctypes
import fcntl
import glob
import logging
import os
from typing import Optional

log = logging.getLogger("benchctrl.drivers.silabs_cp2112.hidraw")

#: Silicon Labs. Note ``10c4:ea60`` is the far more common CP210x *UART*
#: bridge — a different chip, a different protocol, and it presents a tty.
#: Matching it here would open a serial adapter and send it GPIO reports.
VENDOR_ID = 0x10C4
PRODUCT_ID = 0xEA90

# ---------------------------------------------------------------------------
# _IOC arithmetic.
#
# Computed, never hardcoded — the same reasoning as the ADU218's usbfs.py. The
# payload *size* is packed into the request number, so a constant lifted from a
# 64-bit header is simply the wrong number on a 32-bit userland, and it fails
# as EINVAL or (worse) as a partially-transferred buffer rather than as an
# obvious type error. Deriving it means one code path for both ABIs.
# ---------------------------------------------------------------------------
_IOC_NRBITS = 8
_IOC_TYPEBITS = 8
_IOC_SIZEBITS = 14

_IOC_NRSHIFT = 0
_IOC_TYPESHIFT = _IOC_NRSHIFT + _IOC_NRBITS
_IOC_SIZESHIFT = _IOC_TYPESHIFT + _IOC_TYPEBITS
_IOC_DIRSHIFT = _IOC_SIZESHIFT + _IOC_SIZEBITS

_IOC_WRITE = 1
_IOC_READ = 2

#: ``'H'`` is the HID ioctl type letter; 0x06/0x07 are SET/GET feature.
_HID_IOC_TYPE = "H"
_HIDIOCSFEATURE_NR = 0x06
_HIDIOCGFEATURE_NR = 0x07

#: A feature report is at most 64 bytes on this chip; the report-id byte makes
#: 64 the largest buffer we ever hand an ioctl. Bounding it keeps a bad length
#: from being encoded into a plausible-looking request number.
MAX_REPORT_BYTES = 64


def _ioc(direction: int, typ: str, nr: int, size: int) -> int:
    return (
        (direction << _IOC_DIRSHIFT)
        | (ord(typ) << _IOC_TYPESHIFT)
        | (nr << _IOC_NRSHIFT)
        | (size << _IOC_SIZESHIFT)
    )


def hidiocsfeature(size: int) -> int:
    """``HIDIOCSFEATURE(size)`` — send a feature report (SET_REPORT)."""
    return _ioc(_IOC_WRITE | _IOC_READ, _HID_IOC_TYPE, _HIDIOCSFEATURE_NR, size)


def hidiocgfeature(size: int) -> int:
    """``HIDIOCGFEATURE(size)`` — fetch a feature report (GET_REPORT)."""
    return _ioc(_IOC_WRITE | _IOC_READ, _HID_IOC_TYPE, _HIDIOCGFEATURE_NR, size)


class HidrawError(RuntimeError):
    """Transport-level failure. The driver wraps this in its own hierarchy."""


def _sysfs_attr(hidraw_name: str, attr: str) -> Optional[str]:
    """Read one USB attribute for a hidraw node, or None.

    Walks ``/sys/class/hidraw/<name>/device`` upward looking for the USB device
    node that carries ``idVendor``/``idProduct``/``serial``. The hop count is
    not fixed — a hidraw node hangs off the HID device, which hangs off the USB
    *interface*, which hangs off the USB device — and it differs between a
    directly-attached device and one behind hubs (this CP2112 sits four hubs
    deep at 1-1.2.3.2.3). So walk rather than assume a depth.
    """
    path = f"/sys/class/hidraw/{hidraw_name}/device"
    try:
        real = os.path.realpath(path)
    except OSError:
        return None
    for _ in range(8):
        candidate = os.path.join(real, attr)
        if os.path.exists(candidate):
            try:
                with open(candidate) as fh:
                    return fh.read().strip()
            except OSError:
                return None
        parent = os.path.dirname(real)
        if parent == real or parent == "/sys":
            return None
        real = parent
    return None


def find_hidraw_nodes(
    *,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
    serial: Optional[str] = None,
) -> list[str]:
    """Return ``/dev/hidraw*`` paths matching a VID/PID, sorted, maybe filtered.

    Enumerating by VID/PID rather than trusting a node number is not tidiness.
    ``hidrawN`` is assigned in HID-enumeration order across *every* HID device
    on the system, so the CP2112 moves when an unrelated keyboard is replugged
    — hidraw2 on this board was hidraw1 before a keyboard was attached. The
    neighbouring nodes here are the keyboard, so opening the wrong number does
    not fail cleanly: it reads keystrokes.
    """
    out: list[str] = []
    for path in sorted(glob.glob("/sys/class/hidraw/hidraw*")):
        name = os.path.basename(path)
        vid = _sysfs_attr(name, "idVendor")
        pid = _sysfs_attr(name, "idProduct")
        if vid is None or pid is None:
            continue
        if int(vid, 16) != vendor_id or int(pid, 16) != product_id:
            continue
        if serial is not None:
            found = _sysfs_attr(name, "serial")
            if found is None or found.upper() != serial.upper():
                continue
        out.append(f"/dev/{name}")
    return out


def read_serial(path: str) -> Optional[str]:
    """The USB serial number behind a hidraw path, or None."""
    return _sysfs_attr(os.path.basename(path), "serial")


class HidrawLink:
    """A CP2112 feature-report pipe. Carries bytes; knows no report layouts.

    Offers the shape the rest of the repo assumes of a link — ``is_open`` and
    ``close()`` — plus the two feature-report primitives, which is the whole
    surface :py:mod:`.driver` needs.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._fd: Optional[int] = None

    def open(self) -> None:
        if self._fd is not None:
            raise HidrawError(f"{self.path} is already open")
        try:
            # O_RDWR even for reads: HIDIOCGFEATURE issues a control-pipe
            # GET_REPORT, which the kernel gates on write access.
            self._fd = os.open(self.path, os.O_RDWR)
        except PermissionError as e:
            raise HidrawError(
                f"cannot open {self.path}: {e}. The kernel creates hidraw nodes "
                f"root:root 0600; install deploy/udev/64-benchctrl-cp2112.rules "
                f"and re-trigger udev to grant the dialout group access."
            ) from e
        except FileNotFoundError as e:
            raise HidrawError(
                f"{self.path} does not exist. Is the CP2112 attached? "
                f"Expected a hidraw node for "
                f"{VENDOR_ID:04x}:{PRODUCT_ID:04x}."
            ) from e
        except OSError as e:
            raise HidrawError(f"cannot open {self.path}: {e}") from e

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def _require_fd(self) -> int:
        if self._fd is None:
            raise HidrawError(f"{self.path} is not open")
        return self._fd

    def get_feature(self, report_id: int, length: int) -> bytes:
        """Fetch a feature report. ``length`` excludes the report-id byte.

        Returns the payload only, with the echoed report id stripped — callers
        deal in report *contents*, and an off-by-one on that byte is exactly
        the bug that makes a direction mask look like a level mask.
        """
        if not 0 <= report_id <= 0xFF:
            raise HidrawError(f"report id {report_id!r} out of range")
        if not 1 <= length <= MAX_REPORT_BYTES - 1:
            raise HidrawError(f"length {length!r} out of range for a feature report")
        fd = self._require_fd()
        buf = ctypes.create_string_buffer(length + 1)
        buf[0] = bytes([report_id])
        try:
            fcntl.ioctl(fd, hidiocgfeature(length + 1), buf, True)
        except OSError as e:
            raise HidrawError(
                f"HIDIOCGFEATURE(0x{report_id:02X}) on {self.path} failed: {e}"
            ) from e
        raw = bytes(buf.raw[: length + 1])
        if raw[0] != report_id:
            # The chip echoes the id back. A mismatch means the response is not
            # the report we asked for, and parsing it as such would be silent
            # nonsense.
            raise HidrawError(
                f"feature report id mismatch: asked 0x{report_id:02X}, "
                f"got 0x{raw[0]:02X}"
            )
        return raw[1:]

    def set_feature(self, report_id: int, payload: bytes) -> None:
        """Send a feature report. ``payload`` excludes the report-id byte."""
        if not 0 <= report_id <= 0xFF:
            raise HidrawError(f"report id {report_id!r} out of range")
        if not 1 <= len(payload) <= MAX_REPORT_BYTES - 1:
            raise HidrawError(
                f"payload of {len(payload)} bytes out of range for a feature report"
            )
        fd = self._require_fd()
        data = bytes([report_id]) + bytes(payload)
        buf = ctypes.create_string_buffer(data, len(data))
        try:
            fcntl.ioctl(fd, hidiocsfeature(len(data)), buf, True)
        except OSError as e:
            raise HidrawError(
                f"HIDIOCSFEATURE(0x{report_id:02X}) on {self.path} failed: {e}"
            ) from e

    def close(self) -> None:
        if self._fd is not None:
            try:
                os.close(self._fd)
            except OSError:  # pragma: no cover - best-effort
                pass
            self._fd = None

    def __repr__(self) -> str:
        return f"HidrawLink(path={self.path!r}, open={self.is_open})"

"""A stdlib-only USB link to an Ontrak ADU218, over raw USBDEVFS ioctls.

**Zero dependencies.** ``fcntl`` + ``ctypes`` + ``os`` only, which is the whole
reason this module exists rather than a pyusb/libusb transport like
:py:mod:`benchctrl.transports.ch341`. The bench agent host has no pip, so a
compiled wheel would have made the device unreachable there.

Why raw ioctls at all
---------------------
The ADU218 is USB **HID class** (``bInterfaceClass 0x03``), not a serial
device. There is no tty and no ``/dev/hidraw*`` node, so neither pyserial nor
the hidraw route applies. What is left is ``/dev/bus/usb/BBB/DDD`` and the
usbdevfs ioctl interface.

That the interface is *available* to claim is not luck. ``usbhid`` is built in
on this board and has bound the keyboard on the same bus; it leaves the ADU218
alone because upstream deliberately ignores it — ``hid-quirks.c``'s
``hid_ignore_list`` covers Ontrak (``hid-ids.h`` defines
``USB_VENDOR_ID_ONTRAK 0x0a07`` and ``USB_DEVICE_ID_ONTRAK_ADU100 0x0064``;
this device's ``0x00da`` is ``0x0064 + 118``). So ``CLAIMINTERFACE`` succeeds
with nothing to detach, on any mainline kernel, and a kernel update will not
take the path away. ``adutux.c`` (``CONFIG_USB_ADUTUX``) exists for the same
six product ids but is disabled here; its ``write()`` adds no header, so the
framing below is identical on both routes.

Using ``USBDEVFS_BULK`` on *interrupt* endpoints is contractual, not emergent
------------------------------------------------------------------------------
Both of this device's endpoints are interrupt, so a "bulk" ioctl on them looks
wrong. It is documented: ``devio.c``'s ``do_proc_bulk()`` validates only that
the endpoint exists, that the interface is claimed, and that
``wMaxPacketSize`` is nonzero; it then branches on ``USB_ENDPOINT_XFER_INT``,
rewrites the pipe to ``PIPE_INTERRUPT`` and calls ``usb_fill_int_urb()`` with
the descriptor's ``bInterval``. The behaviour is described in ``message.c``'s
kerneldoc and has been present since v2.6.15 (the code moved in ``ae8709b296d8``,
v5.16, without changing meaning).

The residual risk is honest: this guarantee lives in kerneldoc and code, not in
the uapi header. ``SUBMITURB``/``REAPURB`` would be the "proper" async path and
was **rejected** because ``struct usbdevfs_urb`` has no timeout field and
``reap_as()`` takes none — and on a device whose only error signal is silence,
a read without a timeout is a hang.

Never add ``USBDEVFS_ALLOW_SUSPEND``
-----------------------------------
Autosuspend cannot strike mid-session: ``usbdev_open()`` takes a runtime-PM
reference held until release. Opting in would trade that guarantee for a device
that stops answering after an idle period, which — silence being the only error
signal — is indistinguishable from a protocol fault.

Wire format
-----------
Every packet is 8 bytes in both directions. Byte 0 is the report id and must be
exactly ``0x01`` (``0x00``, ``0x02`` and bare ASCII are all ignored by the
device, measured). Bytes 1.. carry the ASCII command, NUL-padded — so a 7-byte
payload. Commands are case-insensitive. Replies are prefixed the same way.

**A queued reply outlives its command.** Interrupt-IN replies sit on EP
``0x81`` until read, so a skipped or failed read leaves an answer behind and the
*next* query returns the *previous* command's value — a silent wrong answer
rather than an error. Hence :py:meth:`Adu218UsbfsLink.drain` on open, and hence
the driver above this seam pairing every command with its expected reply width.

See ``tests/fixtures/adu218/`` for the captures behind each claim; believe them
over the vendor manual, which contradicts itself in at least one place.
"""

from __future__ import annotations

import ctypes
import errno
import fcntl
import logging
import os
from typing import Optional

log = logging.getLogger("benchctrl.drivers.ontrak_adu218.usbfs")

#: Ontrak's vendor id, and the ADU218 specifically.
#:
#: Do not widen this to other Ontrak product ids. The ADU208 is a different
#: product whose relays are mechanical rather than PhotoMOS SSRs, with
#: different switching-rate limits — sharing a driver would silently apply the
#: wrong duty limits to real hardware.
VENDOR_ID = 0x0A07
PRODUCT_ID = 0x00DA

#: Interface and endpoints, read off the live descriptor (not assumed):
#: one interface, two 8-byte interrupt endpoints, ``bInterval`` 10.
INTERFACE = 0
EP_OUT = 0x01
EP_IN = 0x81
PACKET_SIZE = 8

#: Byte 0 of every packet. The device ignores any other value.
REPORT_ID = 0x01

#: So a command is at most 7 ASCII bytes.
MAX_COMMAND_LEN = PACKET_SIZE - 1

#: Default read timeout, in milliseconds. **Measured, not inferred.**
#:
#: 440 timed transfers on hardware: idle ``PK``/``RPK0`` peaked at 16.65 ms
#: (median 15.99), and the path that actually decides driver correctness — the
#: ``RPK0`` read-back issued immediately after a relay command — peaked at
#: 16.68 ms over 40 actuations with zero disagreements. 200 ms is a ~12x margin.
#:
#: Every documented silence still registers as a timeout at this value, with no
#: replies left queued afterwards, so shortening the timeout does not turn an
#: error into a stale success. See ``tests/fixtures/adu218/latency.txt`` and
#: ``latency_after_command.txt``.
DEFAULT_TIMEOUT_MS = 200

#: ``bInterval`` is 10 at low speed, so ~10-20 ms is the protocol floor. A
#: timeout below this would fail *correct* reads; it is not a tuning knob.
MIN_TIMEOUT_MS = 25

_SYSFS_USB = "/sys/bus/usb/devices"
_DEV_BUS_USB = "/dev/bus/usb"


class Adu218LinkError(RuntimeError):
    """Transport-level failure. The driver wraps this in its own hierarchy."""


class Adu218LinkTimeout(Adu218LinkError, TimeoutError):  # noqa: N818 - see below
    """A read produced nothing within the timeout.

    Named for the condition rather than with an ``Error`` suffix (hence the
    ``noqa``) because it *is* a :py:class:`TimeoutError` and reads as one at the
    call site; ``Adu218LinkTimeoutError`` would be the only name in the file
    that fought its own base class.

    Also inherits :py:class:`TimeoutError` so callers can catch either. On this
    device a timeout is **ambiguous by design**: it is how an unknown command
    reports itself, how a valid command with a bad argument reports itself, and
    what a write-only command correctly produces. Only the caller's per-command
    expectation can tell those apart, which is why that table lives in the
    driver rather than here.
    """


def _ioc(direction: int, type_: int, nr: int, size: int) -> int:
    """Linux ``_IOC`` for the asm-generic layout used by every arch we run on."""
    return (direction << 30) | (size << 16) | (type_ << 8) | nr


_IOC_WRITE = 1
_IOC_READ = 2


class _BulkTransfer(ctypes.Structure):
    """``struct usbdevfs_bulktransfer``."""

    _fields_ = [
        ("ep", ctypes.c_uint),
        ("len", ctypes.c_uint),
        ("timeout", ctypes.c_uint),  # milliseconds
        ("data", ctypes.c_void_p),
    ]


#: ``USBDEVFS_CLAIMINTERFACE`` / ``RELEASEINTERFACE`` / ``BULK``.
#:
#: Computed rather than hardcoded because ``BULK`` embeds
#: ``sizeof(struct usbdevfs_bulktransfer)``, which is 24 on 64-bit (0xC0185502)
#: and 16 on 32-bit (0xC0105502). The board is aarch64 today; an armhf agent
#: would silently get ``ENOTTY`` from a hardcoded constant.
USBDEVFS_CLAIMINTERFACE = _ioc(_IOC_READ, ord("U"), 15, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_RELEASEINTERFACE = _ioc(_IOC_READ, ord("U"), 16, ctypes.sizeof(ctypes.c_uint))
USBDEVFS_BULK = _ioc(_IOC_READ | _IOC_WRITE, ord("U"), 2, ctypes.sizeof(_BulkTransfer))


class Adu218Device:
    """One enumerated ADU218: where it is, and which unit it is.

    ``serial`` comes from sysfs rather than the raw descriptor because the
    device node exposes only the binary descriptors — string descriptors need a
    control transfer, and sysfs has already done it. ``firmware`` is
    deliberately absent: this unit reports ``bcdDevice 0000``, so there is no
    version to report and inventing one from the product string would be a
    guess dressed as a fact.
    """

    __slots__ = ("path", "bus", "device", "serial", "product", "manufacturer")

    def __init__(
        self,
        path: str,
        bus: int,
        device: int,
        serial: Optional[str],
        product: Optional[str],
        manufacturer: Optional[str],
    ) -> None:
        self.path = path
        self.bus = bus
        self.device = device
        self.serial = serial
        self.product = product
        self.manufacturer = manufacturer

    def __repr__(self) -> str:
        return (
            f"Adu218Device(path={self.path!r}, bus={self.bus}, "
            f"device={self.device}, serial={self.serial!r})"
        )


def _read_sysfs(directory: str, name: str) -> Optional[str]:
    try:
        with open(os.path.join(directory, name)) as handle:
            return handle.read().strip()
    except (OSError, UnicodeDecodeError):
        return None


def enumerate_devices(
    *,
    vendor_id: int = VENDOR_ID,
    product_id: int = PRODUCT_ID,
    sysfs_root: str = _SYSFS_USB,
    dev_root: str = _DEV_BUS_USB,
) -> list[Adu218Device]:
    """Find every matching device, via sysfs.

    sysfs rather than walking ``/dev/bus/usb`` and parsing descriptors, for two
    reasons: the serial number is only available here (see
    :py:class:`Adu218Device`), and reading it costs no device I/O at all —
    which matters because ``discover()`` is deliberately conservative about
    writing to unidentified hardware.

    Results are sorted by ``(bus, device)`` so enumeration order is stable.
    """
    found: list[Adu218Device] = []
    try:
        entries = sorted(os.listdir(sysfs_root))
    except OSError as exc:
        raise Adu218LinkError(f"cannot list {sysfs_root}: {exc}") from exc

    for entry in entries:
        directory = os.path.join(sysfs_root, entry)
        vendor = _read_sysfs(directory, "idVendor")
        product = _read_sysfs(directory, "idProduct")
        if vendor is None or product is None:
            continue
        try:
            if int(vendor, 16) != vendor_id or int(product, 16) != product_id:
                continue
        except ValueError:  # pragma: no cover - malformed sysfs
            continue
        busnum = _read_sysfs(directory, "busnum")
        devnum = _read_sysfs(directory, "devnum")
        if busnum is None or devnum is None:  # pragma: no cover - mid-unplug
            continue
        try:
            bus, device = int(busnum), int(devnum)
        except ValueError:  # pragma: no cover - malformed sysfs
            continue
        found.append(
            Adu218Device(
                path=os.path.join(dev_root, f"{bus:03d}", f"{device:03d}"),
                bus=bus,
                device=device,
                serial=_read_sysfs(directory, "serial"),
                product=_read_sysfs(directory, "product"),
                manufacturer=_read_sysfs(directory, "manufacturer"),
            )
        )
    found.sort(key=lambda d: (d.bus, d.device))
    return found


def find_device(
    *,
    serial: Optional[str] = None,
    path: Optional[str] = None,
    **kwargs: object,
) -> Adu218Device:
    """Locate exactly one ADU218.

    ``path`` pins a device node directly; ``serial`` selects a unit by its USB
    serial number. With neither, exactly one device must be present — **an
    ambiguous match raises rather than picking the first.** Silently choosing
    among relay boards is how a test energises the wrong bench.

    ``/dev/bus/usb/BBB/DDD`` is assigned in enumeration order and changes on
    every re-plug, so ``path`` is for diagnosis; ``serial`` is what a config
    should pin.
    """
    devices = enumerate_devices(**kwargs)  # type: ignore[arg-type]
    if path is not None:
        wanted = os.path.realpath(path)
        for device in devices:
            if os.path.realpath(device.path) == wanted:
                return device
        # Honour an explicit path even if sysfs did not list it: a caller who
        # names a node is entitled to a clear open() failure rather than a
        # confusing "no ADU218 found".
        return Adu218Device(
            path=path, bus=-1, device=-1, serial=None, product=None, manufacturer=None
        )
    if serial is not None:
        matches = [d for d in devices if d.serial == serial]
        if not matches:
            seen = ", ".join(repr(d.serial) for d in devices) or "none"
            raise Adu218LinkError(
                f"no ADU218 with serial {serial!r} (serials present: {seen})"
            )
        if len(matches) > 1:  # pragma: no cover - duplicate serials
            raise Adu218LinkError(
                f"{len(matches)} devices report serial {serial!r}; cannot choose"
            )
        return matches[0]
    if not devices:
        raise Adu218LinkError(
            f"no ADU218 found (looked for USB {VENDOR_ID:04x}:{PRODUCT_ID:04x} "
            f"under {_SYSFS_USB})"
        )
    if len(devices) > 1:
        serials = ", ".join(repr(d.serial) for d in devices)
        raise Adu218LinkError(
            f"{len(devices)} ADU218 devices present (serials: {serials}); "
            "pass serial= to choose one rather than relying on order"
        )
    return devices[0]


class Adu218UsbfsLink:
    """An 8-byte packet channel to one ADU218.

    Offers the repo's informal four-method I/O duck type — ``write``, ``read``,
    ``is_open``, ``close`` — so it sits in the same seam as the PDU41002's
    links and :py:mod:`benchctrl.transports`. Note ``is_open`` means *the link
    is connected*, matching ``agent/registry.py``, and says nothing about any
    relay contact; relay state never borrows the word (a relay's "open" is the
    opposite sign).

    Deliberately **not** in :py:mod:`benchctrl.transports`: that package holds
    things that yield a serial-shaped object, and this is a packet channel with
    a single consumer. Promote it on the second Ontrak device.
    """

    def __init__(
        self,
        *,
        serial: Optional[str] = None,
        path: Optional[str] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
    ) -> None:
        if timeout_ms < MIN_TIMEOUT_MS:
            raise ValueError(
                f"timeout_ms={timeout_ms} is below the {MIN_TIMEOUT_MS} ms "
                f"protocol floor (bInterval is 10 at low speed, so a shorter "
                f"timeout would fail correct reads, not just slow ones)"
            )
        self._wanted_serial = serial
        self._wanted_path = path
        self.timeout_ms = timeout_ms
        self._fd: Optional[int] = None
        self._claimed = False
        self._device: Optional[Adu218Device] = None

    # -- identity -----------------------------------------------------------

    @property
    def device(self) -> Optional[Adu218Device]:
        """Which unit this link is attached to. ``None`` before ``open()``."""
        return self._device

    @property
    def is_open(self) -> bool:
        """Whether the *link* is connected. Never a relay contact position."""
        return self._fd is not None

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        """Open the device node, claim interface 0, and drain stale replies.

        The drain is not tidiness. A reply left queued from a previous process
        would be returned to *this* process's first query, as that query's
        answer — the exact failure that invalidated the first framing capture.
        """
        if self._fd is not None:
            return
        device = find_device(serial=self._wanted_serial, path=self._wanted_path)
        try:
            fd = os.open(device.path, os.O_RDWR)
        except OSError as exc:
            if exc.errno == errno.EACCES:
                raise Adu218LinkError(
                    f"permission denied opening {device.path}: usbdevfs nodes are "
                    f"root:root 0664 by default, and interrupt transfers need "
                    f"WRITE access. Install deploy/udev/63-benchctrl-adu218.rules "
                    f"(MODE=0660 GROUP=dialout) and re-plug the device."
                ) from exc
            raise Adu218LinkError(f"cannot open {device.path}: {exc}") from exc

        self._fd = fd
        self._device = device
        try:
            self._claim()
        except Adu218LinkError:
            self.close()
            raise
        drained = self.drain()
        if drained:
            log.warning(
                "drained %d stale repl%s from EP %#04x on open; a previous "
                "process left %s unread",
                drained,
                "y" if drained == 1 else "ies",
                EP_IN,
                "it" if drained == 1 else "them",
            )

    def _claim(self) -> None:
        assert self._fd is not None
        interface = ctypes.c_uint(INTERFACE)
        try:
            fcntl.ioctl(self._fd, USBDEVFS_CLAIMINTERFACE, interface)
        except OSError as exc:
            if exc.errno == errno.EBUSY:
                raise Adu218LinkError(
                    f"interface {INTERFACE} is busy. This is unexpected: usbhid "
                    f"ignores Ontrak devices by design (hid_ignore_list), so "
                    f"nothing should hold it. Check whether adutux is loaded "
                    f"(CONFIG_USB_ADUTUX) or another benchctrl process has the "
                    f"device open — two writers on a relay board is the hazard "
                    f"KNOWN_LIMITATIONS N-4 exists to prevent."
                ) from exc
            raise Adu218LinkError(
                f"CLAIMINTERFACE {INTERFACE} failed on {self._device}: {exc}"
            ) from exc
        self._claimed = True

    def close(self) -> None:
        """Release the interface and close the node. Idempotent.

        Does **not** change relay state. A relay left energised stays
        energised — the driver's ``reset_relays()`` is the explicit way to
        de-energise, and hiding it here would make ``close()`` a switching
        operation that no caller asked for.
        """
        fd, claimed = self._fd, self._claimed
        self._fd = None
        self._claimed = False
        if fd is None:
            return
        if claimed:
            try:
                interface = ctypes.c_uint(INTERFACE)
                fcntl.ioctl(fd, USBDEVFS_RELEASEINTERFACE, interface)
            except OSError as exc:  # pragma: no cover - best-effort
                log.debug("RELEASEINTERFACE failed (continuing to close): %s", exc)
        try:
            os.close(fd)
        except OSError as exc:  # pragma: no cover - best-effort
            log.debug("close(%d) failed: %s", fd, exc)

    def __enter__(self) -> "Adu218UsbfsLink":
        self.open()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    # -- transfers ----------------------------------------------------------

    def _transfer(self, endpoint: int, buffer: ctypes.Array, timeout_ms: int) -> int:
        if self._fd is None:
            raise Adu218LinkError("link is not open")
        transfer = _BulkTransfer(
            ep=endpoint,
            len=len(buffer),
            timeout=timeout_ms,
            data=ctypes.cast(buffer, ctypes.c_void_p),
        )
        try:
            return fcntl.ioctl(self._fd, USBDEVFS_BULK, transfer)
        except OSError as exc:
            if exc.errno == errno.ETIMEDOUT:
                raise Adu218LinkTimeout(
                    f"no reply on EP {endpoint:#04x} within {timeout_ms} ms"
                ) from exc
            if exc.errno in (errno.ENODEV, errno.ESHUTDOWN):
                raise Adu218LinkError(
                    f"device disappeared during transfer on EP {endpoint:#04x} "
                    f"(unplugged, or the port lost power): {exc}"
                ) from exc
            raise Adu218LinkError(
                f"USBDEVFS_BULK on EP {endpoint:#04x} failed: {exc}"
            ) from exc

    def write(self, command: str, *, timeout_ms: Optional[int] = None) -> None:
        """Send one ASCII command, framed as an 8-byte report.

        The device gives **no acknowledgement of a write**, and no error for an
        unknown command — so a successful return here means the packet reached
        the device, nothing more. Whether the device understood it can only be
        established by reading back the affected state.
        """
        payload = command.encode("ascii", errors="strict")
        if not payload:
            raise ValueError("empty command")
        if len(payload) > MAX_COMMAND_LEN:
            raise ValueError(
                f"command {command!r} is {len(payload)} bytes; the 8-byte report "
                f"leaves only {MAX_COMMAND_LEN} after the mandatory 0x01 prefix"
            )
        raw = bytearray(PACKET_SIZE)
        raw[0] = REPORT_ID
        raw[1 : 1 + len(payload)] = payload
        buffer = (ctypes.c_ubyte * PACKET_SIZE).from_buffer(raw)
        self._transfer(
            EP_OUT, buffer, self.timeout_ms if timeout_ms is None else timeout_ms
        )

    def read(self, *, timeout_ms: Optional[int] = None) -> str:
        """Read one reply, returning its ASCII payload without the prefix.

        Raises :py:class:`Adu218LinkTimeout` on silence. Silence is not
        necessarily an error — see that class — so the caller decides.
        """
        buffer = (ctypes.c_ubyte * PACKET_SIZE)()
        count = self._transfer(
            EP_IN, buffer, self.timeout_ms if timeout_ms is None else timeout_ms
        )
        raw = bytes(buffer)[: max(0, count)]
        if not raw:  # pragma: no cover - a zero-length interrupt reply
            return ""
        if raw[0] != REPORT_ID:
            raise Adu218LinkError(
                f"reply began with {raw[0]:#04x}, expected the {REPORT_ID:#04x} "
                f"report id; framing is out of sync (raw={raw!r})"
            )
        return raw[1:].split(b"\x00", 1)[0].decode("ascii", errors="replace")

    def query(self, command: str, *, timeout_ms: Optional[int] = None) -> str:
        """Write then read. Convenience only — no extra pairing guarantee.

        The pairing that matters (which commands answer at all, and with how
        many characters) is the driver's per-command table, because it cannot be
        derived from the wire: a write-only command's silence is byte-identical
        to an unknown command's.
        """
        self.write(command, timeout_ms=timeout_ms)
        return self.read(timeout_ms=timeout_ms)

    def drain(self, *, timeout_ms: Optional[int] = None, limit: int = 64) -> int:
        """Discard every queued reply. Returns how many were thrown away.

        Uses the **full** read timeout, not a short one. A drain that gives up
        early is worse than no drain: it leaves the reply queued *and* reports
        the queue clean, so the very failure it exists to prevent — the next
        query returning the previous command's answer — happens anyway, now
        with a log line saying it could not. One 200 ms wait per ``open()`` is
        not worth optimising away.

        ``limit`` bounds the loop so a device stuck emitting replies cannot
        hang ``open()`` forever; exceeding it is reported, not swallowed.
        """
        discarded = 0
        while discarded < limit:
            try:
                self.read(timeout_ms=timeout_ms)
            except Adu218LinkTimeout:
                return discarded
            except Adu218LinkError:
                # Framing already broken; a drain must not raise over it.
                return discarded + 1
            discarded += 1
        raise Adu218LinkError(
            f"drained {limit} replies from EP {EP_IN:#04x} and more are still "
            f"queued; the device is not idle and framing cannot be trusted"
        )

    def __repr__(self) -> str:
        serial = self._device.serial if self._device else self._wanted_serial
        return (
            f"Adu218UsbfsLink(serial={serial!r}, "
            f"timeout_ms={self.timeout_ms}, open={self.is_open})"
        )

"""Userspace CH341 USB-serial driver, for kernels built without ``ch341``.

Why this exists: Arduino's Uno Q kernel is configured with

    CONFIG_USB_SERIAL=m                    # core present
    # CONFIG_USB_SERIAL_CH341 is not set   # this driver absent
    # CONFIG_USB_SERIAL_GENERIC is not set # no fallback either

so a CH340/CH341 bridge enumerates (``product: USB Serial``) but its
interface binds no driver and no ``/dev/ttyUSB*`` appears. The Eastwood
QR10x reaches its instrument through exactly such a bridge, which makes the
device unreachable on that board. Force-loading a prebuilt ``ch341.ko`` is
not an option either: ``CONFIG_MODULE_FORCE_LOAD`` is off, and no
``linux-headers`` package exists for the running kernel to build against.

So we speak the chip's protocol ourselves over libusb, and expose the result
as a **real pty**, which means :py:class:`~benchctrl.drivers.eastwood_qr10x.QR10x`
opens it with unmodified ``serial.Serial`` and cannot tell the difference.
Nothing in the driver layer changes.

The protocol here is the register-level interface the in-tree Linux
``drivers/usb/serial/ch341.c`` drives; the constants below are named after
their counterparts there so the two can be read side by side.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Optional

from benchctrl.exceptions import BenchConnectionError, BenchValueError

log = logging.getLogger("benchctrl.transports.ch341")

#: The one VID/PID pair we claim. Deliberately narrow: 1a86:7523 is the
#: classic CH340G. Other CH34x variants (CH9102 at 1a86:55d4) use a
#: different register layout and are NOT handled by this code.
CH340_VID = 0x1A86
CH340_PID = 0x7523

# --- control requests (ch341.c) --------------------------------------------
CH341_REQ_READ_VERSION = 0x5F
CH341_REQ_WRITE_REG = 0x9A
CH341_REQ_READ_REG = 0x95
CH341_REQ_SERIAL_INIT = 0xA1
CH341_REQ_MODEM_CTRL = 0xA4

CH341_REG_BREAK = 0x05
CH341_REG_PRESCALER = 0x12
CH341_REG_DIVISOR = 0x13
CH341_REG_LCR = 0x18
CH341_REG_LCR2 = 0x25

# --- line control bits ----------------------------------------------------
CH341_LCR_ENABLE_RX = 0x80
CH341_LCR_ENABLE_TX = 0x40
CH341_LCR_MARK_SPACE = 0x20
CH341_LCR_PAR_EVEN = 0x10
CH341_LCR_ENABLE_PAR = 0x08
CH341_LCR_STOP_BITS_2 = 0x04
CH341_LCR_CS8 = 0x03
CH341_LCR_CS7 = 0x02
CH341_LCR_CS6 = 0x01
CH341_LCR_CS5 = 0x00

#: Modem control bits, active-low in the wire encoding (see ch341.c).
CH341_BIT_RTS = 1 << 6
CH341_BIT_DTR = 1 << 5

#: The chip's baud generator runs off 12 MHz.
CH341_OSC_HZ = 12_000_000

#: (prescaler bits, divisor factor) — index is the "fact/ps" pair from
#: ch341.c's table. Ordered slowest first so the search picks the largest
#: divisor that fits, which minimises rounding error.
_PRESCALE_TABLE = (
    (0x00, 1024),   # ps=0, fact=0
    (0x01, 128),    # ps=1, fact=0
    (0x02, 16),     # ps=2, fact=0
    (0x03, 2),      # ps=3, fact=0
    (0x07, 1),      # ps=3, fact=1
)


def _baud_registers(baudrate: int) -> tuple[int, int]:
    """Return ``(prescaler_byte, divisor_byte)`` for ``baudrate``.

    Mirrors ch341.c's ``ch341_get_divisor``. The chip computes

        baud = 12_000_000 / (factor * (256 - divisor))

    so we walk the prescaler table from the largest factor down and take the
    first combination whose divisor lands in the representable 2..255 window.
    """
    if baudrate <= 0:
        raise BenchValueError(f"baudrate must be positive, got {baudrate}")

    best: Optional[tuple[int, int, float]] = None
    for ps_byte, factor in _PRESCALE_TABLE:
        divisor = round(CH341_OSC_HZ / (factor * baudrate))
        # 254..2: the chip rejects 0/1, and 256-divisor must stay positive.
        if not 2 <= divisor <= 255:
            continue
        actual = CH341_OSC_HZ / (factor * divisor)
        error = abs(actual - baudrate) / baudrate
        if best is None or error < best[2]:
            best = (ps_byte, 256 - divisor, error)

    if best is None:
        raise BenchValueError(
            f"baudrate {baudrate} is not representable by a CH340 "
            f"(supported range is roughly 46..3000000)"
        )

    ps_byte, div_byte, error = best
    # 2% is the usual UART framing tolerance; past that, bytes corrupt in
    # ways that look like a protocol bug rather than a clock problem.
    if error > 0.02:
        raise BenchValueError(
            f"baudrate {baudrate} cannot be generated within 2% "
            f"(closest is {error * 100:.1f}% off) — pick a standard rate"
        )
    log.debug(
        "ch341: baud %d -> prescaler 0x%02x divisor 0x%02x (%.2f%% error)",
        baudrate, ps_byte, div_byte, error * 100,
    )
    return ps_byte, div_byte


def _lcr_for(bytesize: int, parity: str, stopbits: int) -> int:
    """Build the line-control register value."""
    lcr = CH341_LCR_ENABLE_RX | CH341_LCR_ENABLE_TX
    lcr |= {5: CH341_LCR_CS5, 6: CH341_LCR_CS6, 7: CH341_LCR_CS7, 8: CH341_LCR_CS8}[
        bytesize
    ]
    if parity != "N":
        lcr |= CH341_LCR_ENABLE_PAR
        if parity == "E":
            lcr |= CH341_LCR_PAR_EVEN
        elif parity == "M":
            lcr |= CH341_LCR_PAR_EVEN | CH341_LCR_MARK_SPACE
        elif parity == "S":
            lcr |= CH341_LCR_MARK_SPACE
        elif parity != "O":
            raise BenchValueError(f"unsupported parity {parity!r}")
    if stopbits == 2:
        lcr |= CH341_LCR_STOP_BITS_2
    elif stopbits != 1:
        raise BenchValueError(f"unsupported stopbits {stopbits!r}")
    return lcr


class CH341Device:
    """One CH340 bridge, driven over libusb.

    Read is buffered internally by a background thread: the chip streams into
    a bulk-in endpoint whether or not anyone is asking, and a poll-on-demand
    design loses bytes between calls.
    """

    #: The chip prepends no header to bulk-in data, but the interrupt
    #: endpoint carries modem-status changes we don't need. Bulk only.
    _READ_TIMEOUT_MS = 100

    def __init__(self, dev, *, baudrate: int = 115200, bytesize: int = 8,
                 parity: str = "N", stopbits: int = 1) -> None:
        self._dev = dev
        self._baudrate = baudrate
        self._bytesize = bytesize
        self._parity = parity
        self._stopbits = stopbits

        self._ep_in = None
        self._ep_out = None
        self._rx = bytearray()
        self._rx_lock = threading.Lock()
        self._stop = threading.Event()
        self._reader: Optional[threading.Thread] = None
        self._closed = False
        self.read_errors = 0

    # --- discovery ------------------------------------------------------

    @classmethod
    def find_all(cls) -> list:
        """Every CH340 on the bus, as raw pyusb device objects."""
        usb_core = _import_usb()
        return list(
            usb_core.find(find_all=True, idVendor=CH340_VID, idProduct=CH340_PID)
        )

    @classmethod
    def open(cls, *, serial_number: Optional[str] = None, index: int = 0,
             baudrate: int = 115200, bytesize: int = 8, parity: str = "N",
             stopbits: int = 1) -> "CH341Device":
        """Open a CH340. Picks by ``serial_number`` if given, else ``index``."""
        devices = cls.find_all()
        if not devices:
            raise BenchConnectionError(
                f"no CH340 bridge found ({CH340_VID:04x}:{CH340_PID:04x}). "
                f"Check the cable, and that the device is on this host's USB bus."
            )
        chosen = None
        if serial_number is not None:
            for d in devices:
                if _string_of(d, "serial_number") == serial_number:
                    chosen = d
                    break
            if chosen is None:
                raise BenchConnectionError(
                    f"no CH340 with serial {serial_number!r}; found "
                    f"{[_string_of(d, 'serial_number') for d in devices]}"
                )
        else:
            if index >= len(devices):
                raise BenchConnectionError(
                    f"CH340 index {index} out of range ({len(devices)} present)"
                )
            chosen = devices[index]

        obj = cls(chosen, baudrate=baudrate, bytesize=bytesize,
                  parity=parity, stopbits=stopbits)
        obj._connect()
        return obj

    # --- lifecycle ------------------------------------------------------

    def _connect(self) -> None:
        usb_core = _import_usb()
        try:
            # Most CH340s expose exactly one configuration; set it explicitly
            # so a device left in a half-configured state still works.
            try:
                self._dev.set_configuration()
            except usb_core.USBError as exc:
                # Already configured is fine; anything else is not.
                if "Device or resource busy" in str(exc):
                    raise BenchConnectionError(
                        "the CH340 is claimed by another driver or process. "
                        "If a kernel ch341 module is loaded, this userspace "
                        "driver is unnecessary — use /dev/ttyUSB* instead."
                    ) from exc
                log.debug("ch341: set_configuration: %s (continuing)", exc)

            cfg = self._dev.get_active_configuration()
            intf = cfg[(0, 0)]
            for ep in intf:
                is_in = usb_core.util.endpoint_direction(ep.bEndpointAddress) == (
                    usb_core.util.ENDPOINT_IN
                )
                is_bulk = usb_core.util.endpoint_type(ep.bmAttributes) == (
                    usb_core.util.ENDPOINT_TYPE_BULK
                )
                if is_bulk and is_in:
                    self._ep_in = ep
                elif is_bulk and not is_in:
                    self._ep_out = ep
            if self._ep_in is None or self._ep_out is None:
                raise BenchConnectionError(
                    "CH340 did not expose the expected bulk in/out endpoint "
                    "pair — is this really a CH340G (1a86:7523)?"
                )
        except usb_core.USBError as exc:
            raise BenchConnectionError(f"could not claim the CH340: {exc}") from exc

        self._init_chip()

        self._stop.clear()
        self._reader = threading.Thread(
            target=self._read_loop, name="ch341-reader", daemon=True
        )
        self._reader.start()

    def _init_chip(self) -> None:
        """The startup sequence from ch341.c's ``ch341_configure``."""
        version = self._ctrl_in(CH341_REQ_READ_VERSION, 0, 0, 2)
        log.debug("ch341: chip version %s", version.hex() if version else "?")

        # Undocumented but required: the in-tree driver issues this before
        # any register write, and the chip ignores baud settings without it.
        self._ctrl_out(CH341_REQ_SERIAL_INIT, 0, 0)
        self.set_line_settings(
            baudrate=self._baudrate,
            bytesize=self._bytesize,
            parity=self._parity,
            stopbits=self._stopbits,
        )
        # Assert DTR+RTS. The QR10x opens with dsrdtr=False/rtscts=False, i.e.
        # no hardware flow control, but the lines still need to be driven or
        # some bridges hold the device in reset.
        self.set_modem_lines(dtr=True, rts=True)

    def set_line_settings(self, *, baudrate: int, bytesize: int = 8,
                          parity: str = "N", stopbits: int = 1) -> None:
        """Program baud rate and framing."""
        ps_byte, div_byte = _baud_registers(baudrate)
        lcr = _lcr_for(bytesize, parity, stopbits)

        # Registers are written in pairs: (PRESCALER, DIVISOR) then
        # (LCR, LCR2). value = low reg | high reg << 8, index = the bytes.
        self._ctrl_out(
            CH341_REQ_WRITE_REG,
            CH341_REG_PRESCALER | (CH341_REG_DIVISOR << 8),
            ps_byte | (div_byte << 8),
        )
        self._ctrl_out(
            CH341_REQ_WRITE_REG,
            CH341_REG_LCR | (CH341_REG_LCR2 << 8),
            lcr,
        )
        self._baudrate, self._bytesize = baudrate, bytesize
        self._parity, self._stopbits = parity, stopbits

    def set_modem_lines(self, *, dtr: bool, rts: bool) -> None:
        """Drive DTR/RTS. The wire encoding is active-low."""
        value = 0
        if dtr:
            value |= CH341_BIT_DTR
        if rts:
            value |= CH341_BIT_RTS
        self._ctrl_out(CH341_REQ_MODEM_CTRL, ~value & 0xFF, 0)

    # --- control transfers ----------------------------------------------

    def _ctrl_out(self, request: int, value: int, index: int) -> None:
        usb_core = _import_usb()
        try:
            self._dev.ctrl_transfer(0x40, request, value, index, None, 1000)
        except usb_core.USBError as exc:
            raise BenchConnectionError(
                f"CH340 control write 0x{request:02x} failed: {exc}"
            ) from exc

    def _ctrl_in(self, request: int, value: int, index: int, length: int) -> bytes:
        usb_core = _import_usb()
        try:
            return bytes(
                self._dev.ctrl_transfer(0xC0, request, value, index, length, 1000)
            )
        except usb_core.USBError as exc:
            raise BenchConnectionError(
                f"CH340 control read 0x{request:02x} failed: {exc}"
            ) from exc

    # --- I/O ------------------------------------------------------------

    def _read_loop(self) -> None:
        """Drain the bulk-in endpoint into ``self._rx`` until stopped."""
        usb_core = _import_usb()
        size = self._ep_in.wMaxPacketSize
        while not self._stop.is_set():
            try:
                data = self._ep_in.read(size, self._READ_TIMEOUT_MS)
            except usb_core.USBError as exc:
                # A timeout on an idle line is the normal case, not an error.
                if _is_timeout(exc):
                    continue
                if self._stop.is_set():
                    break
                self.read_errors += 1
                log.warning("ch341: bulk read failed: %s", exc)
                # Back off so a persistent failure doesn't spin a hot loop.
                time.sleep(0.05)
                continue
            if data:
                with self._rx_lock:
                    self._rx.extend(bytes(data))

    def read(self, size: int = 1024) -> bytes:
        """Return up to ``size`` buffered bytes, without waiting."""
        with self._rx_lock:
            if not self._rx:
                return b""
            chunk = bytes(self._rx[:size])
            del self._rx[: len(chunk)]
            return chunk

    @property
    def in_waiting(self) -> int:
        with self._rx_lock:
            return len(self._rx)

    def reset_input_buffer(self) -> None:
        with self._rx_lock:
            self._rx.clear()

    def write(self, data: bytes) -> int:
        """Write ``data`` to the bulk-out endpoint."""
        if self._closed:
            raise BenchConnectionError("CH340 is closed")
        usb_core = _import_usb()
        size = self._ep_out.wMaxPacketSize
        sent = 0
        try:
            # Chunk to the endpoint's packet size: libusb accepts more, but
            # some CH340 revisions drop the tail of an oversized transfer.
            while sent < len(data):
                n = self._ep_out.write(data[sent : sent + size], 1000)
                if n <= 0:
                    break
                sent += n
        except usb_core.USBError as exc:
            raise BenchConnectionError(f"CH340 write failed: {exc}") from exc
        return sent

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._stop.set()
        if self._reader is not None:
            # Slightly longer than one read timeout, so the thread exits on
            # its own rather than being abandoned mid-transfer.
            self._reader.join(timeout=1.0)
            self._reader = None
        usb_core = _import_usb()
        try:
            usb_core.util.dispose_resources(self._dev)
        except Exception as exc:  # noqa: BLE001
            log.warning("ch341: releasing the device failed: %s", exc)

    @property
    def is_open(self) -> bool:
        return not self._closed

    def __enter__(self) -> "CH341Device":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "open" if not self._closed else "closed"
        return f"<CH341Device {self._baudrate}baud {state}>"


def _import_usb():
    """Import pyusb, with an error that says how to fix a missing one."""
    try:
        import usb.core
        import usb.util
    except ImportError as exc:
        raise BenchConnectionError(
            "the userspace CH341 driver needs pyusb. It is pure Python, so "
            "on a board with no pip: unzip the pyusb wheel next to benchctrl "
            "the same way pyserial is deployed (see docs/remote.md)."
        ) from exc
    return usb.core


def _is_timeout(exc) -> bool:
    """Whether a USBError is a benign transfer timeout."""
    # errno 110 == ETIMEDOUT on Linux; the string check covers backends that
    # do not populate errno.
    return getattr(exc, "errno", None) == 110 or "timeout" in str(exc).lower()


def _string_of(dev, attr: str) -> Optional[str]:
    """Read a USB string descriptor, tolerating devices that have none."""
    try:
        return getattr(dev, attr, None)
    except Exception:  # noqa: BLE001 - unreadable descriptor is not an error
        return None

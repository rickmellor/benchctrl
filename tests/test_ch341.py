"""Userspace CH341 driver: register math, and the pty bridge end to end.

The register values here are cross-checked against the in-tree Linux
``drivers/usb/serial/ch341.c``. That matters more than it looks: a divisor
that is merely self-consistent will still corrupt every byte on the wire, and
the failure looks like a protocol bug rather than a clock error.
"""

from __future__ import annotations

import os
import stat
import threading
import time

import pytest
import serial

from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.exceptions import BenchValueError
from benchctrl.sim.qr10x import SimulatedQR10x
from benchctrl.transports.ch341 import (
    CH341_LCR_CS8,
    CH341_LCR_ENABLE_RX,
    CH341_LCR_ENABLE_TX,
    CH341_LCR_STOP_BITS_2,
    CH341_OSC_HZ,
    _baud_registers,
    _lcr_for,
)
from benchctrl.transports.ptybridge import PtySerialBridge

_FACTOR_OF_PRESCALER = {0x00: 1024, 0x01: 128, 0x02: 16, 0x03: 2, 0x07: 1}


def _decode(ps_byte: int, div_byte: int) -> float:
    """What the chip will actually generate for these registers."""
    return CH341_OSC_HZ / (_FACTOR_OF_PRESCALER[ps_byte] * (256 - div_byte))


# --------------------------------------------------------------------------
# Baud rate registers
# --------------------------------------------------------------------------


def test_115200_matches_the_linux_driver():
    """The rate the QR10x runs at, cross-checked against ch341.c."""
    assert _baud_registers(115200) == (0x03, 0xCC)


@pytest.mark.parametrize(
    "baud", [2400, 4800, 9600, 19200, 38400, 57600, 115200, 230400, 921600]
)
def test_standard_rates_land_within_uart_tolerance(baud):
    ps, div = _baud_registers(baud)
    error = abs(_decode(ps, div) - baud) / baud
    # 2% is the usual framing limit; every standard rate should be far inside.
    assert error < 0.02, f"{baud} off by {error:.2%}"


def test_divisor_stays_in_the_representable_window():
    """The chip rejects divisors of 0 and 1; 256-div must stay positive."""
    for baud in (50, 300, 1200, 115200, 3_000_000):
        try:
            ps, div = _baud_registers(baud)
        except BenchValueError:
            continue
        assert 2 <= 256 - div <= 255, f"{baud} produced out-of-range divisor"
        assert 0 <= div <= 0xFF


def test_unrepresentable_rate_is_rejected_not_silently_wrong():
    """A rate the chip cannot generate must raise, not quietly mis-clock.

    Silently programming the nearest rate is the dangerous failure: the port
    opens, and every byte comes back corrupted.
    """
    with pytest.raises(BenchValueError, match="not representable|within 2%"):
        _baud_registers(7_000_000)


def test_nonpositive_baud_is_rejected():
    with pytest.raises(BenchValueError):
        _baud_registers(0)
    with pytest.raises(BenchValueError):
        _baud_registers(-115200)


# --------------------------------------------------------------------------
# Line control register
# --------------------------------------------------------------------------


def test_lcr_8n1_matches_the_linux_driver():
    """8N1 is 0xc3 in ch341.c: RX|TX enable plus CS8."""
    assert _lcr_for(8, "N", 1) == 0xC3
    assert _lcr_for(8, "N", 1) == (
        CH341_LCR_ENABLE_RX | CH341_LCR_ENABLE_TX | CH341_LCR_CS8
    )


def test_lcr_always_enables_both_directions():
    """A missing enable bit yields a port that opens but never transfers."""
    for bytesize in (5, 6, 7, 8):
        for parity in ("N", "E", "O"):
            for stopbits in (1, 2):
                lcr = _lcr_for(bytesize, parity, stopbits)
                assert lcr & CH341_LCR_ENABLE_RX
                assert lcr & CH341_LCR_ENABLE_TX


def test_lcr_two_stop_bits_sets_its_bit():
    assert _lcr_for(8, "N", 2) & CH341_LCR_STOP_BITS_2
    assert not _lcr_for(8, "N", 1) & CH341_LCR_STOP_BITS_2


def test_lcr_rejects_unsupported_framing():
    with pytest.raises(BenchValueError):
        _lcr_for(8, "N", 3)
    with pytest.raises(BenchValueError):
        _lcr_for(8, "Z", 1)
    with pytest.raises(KeyError):
        _lcr_for(9, "N", 1)


# --------------------------------------------------------------------------
# The pty bridge
# --------------------------------------------------------------------------


class FakeUsbSerial:
    """Stands in for CH341Device: the four methods the bridge uses.

    A mock is the right tool here — the point is to prove the *bridge* moves
    bytes both ways, with no USB stack in the picture.
    """

    def __init__(self) -> None:
        self.to_host = bytearray()
        self.from_host = bytearray()
        self._lock = threading.Lock()
        self.closed = False

    def read(self, size: int = 1024) -> bytes:
        with self._lock:
            chunk = bytes(self.to_host[:size])
            del self.to_host[: len(chunk)]
            return chunk

    def write(self, data: bytes) -> int:
        with self._lock:
            self.from_host.extend(data)
        return len(data)

    def close(self) -> None:
        self.closed = True

    @property
    def is_open(self) -> bool:
        return not self.closed

    # test helper
    def queue_from_device(self, data: bytes) -> None:
        with self._lock:
            self.to_host.extend(data)


def _wait_for(predicate, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return False


@pytest.fixture
def bridge():
    fake = FakeUsbSerial()
    b = PtySerialBridge(fake, name="test").start()
    try:
        yield b, fake
    finally:
        b.close()


def test_bridge_exposes_a_port_pyserial_can_open(bridge):
    b, _ = bridge
    assert b.port.startswith("/dev/pts/")
    # The whole design rests on this: an unmodified serial.Serial opens it.
    with serial.Serial(b.port, baudrate=115200, timeout=0.2) as ser:
        assert ser.is_open


def test_bytes_written_by_the_driver_reach_the_device(bridge):
    b, fake = bridge
    with serial.Serial(b.port, baudrate=115200, timeout=0.2) as ser:
        ser.write(b"AT+DEV.SN?\r\n")
        ser.flush()
        assert _wait_for(lambda: bytes(fake.from_host) == b"AT+DEV.SN?\r\n"), (
            f"device saw {bytes(fake.from_host)!r}"
        )


def test_bytes_from_the_device_reach_the_driver(bridge):
    b, fake = bridge
    with serial.Serial(b.port, baudrate=115200, timeout=1.0) as ser:
        fake.queue_from_device(b"+DEV.SN=12345\r\n")
        assert _wait_for(lambda: ser.in_waiting > 0)
        assert ser.read(ser.in_waiting) == b"+DEV.SN=12345\r\n"


def test_bridge_is_binary_safe(bridge):
    """CR/LF must survive verbatim.

    A pty in cooked mode rewrites \\r to \\n, which silently corrupts any
    framed protocol. SerialLoopback sets raw mode; this proves it holds
    through the bridge.
    """
    b, fake = bridge
    payload = bytes(range(256))
    with serial.Serial(b.port, baudrate=115200, timeout=0.2) as ser:
        ser.write(payload)
        ser.flush()
        assert _wait_for(lambda: len(fake.from_host) >= 256)
        assert bytes(fake.from_host) == payload


def test_close_releases_the_usb_device(bridge):
    b, fake = bridge
    b.close()
    assert fake.closed
    assert not b.is_open


def test_close_is_idempotent(bridge):
    b, fake = bridge
    b.close()
    b.close()
    assert fake.closed


def test_byte_counters_track_both_directions(bridge):
    b, fake = bridge
    with serial.Serial(b.port, baudrate=115200, timeout=1.0) as ser:
        ser.write(b"ping")
        ser.flush()
        fake.queue_from_device(b"pong!")
        assert _wait_for(lambda: b.host_to_device_bytes >= 4)
        assert _wait_for(lambda: b.device_to_host_bytes >= 5)


def test_pty_appears_before_any_driver_opens_it(bridge):
    """The pty must exist as a character device, not just as a string.

    ``QR10x.open`` hands the path to pyserial, which stats it — a bridge that
    returned a plausible-looking path would fail only at that point.
    """
    b, _ = bridge
    assert stat.S_ISCHR(os.stat(b.port).st_mode)


# --------------------------------------------------------------------------
# End to end: the real QR10x driver, through the real bridge
# --------------------------------------------------------------------------


class LoopbackBackedDevice:
    """A bridge "device" whose far side is another pty, not USB.

    This is the seam that lets the *whole* stack above libusb be tested off
    hardware: the real ``PtySerialBridge`` and the real ``QR10x`` driver run
    unmodified, and only the CH341 register layer is absent — and that layer
    is pinned separately against ch341.c above.

    ``read`` is non-blocking to match :py:class:`CH341Device.read`, which
    returns whatever its background reader has buffered.
    """

    def __init__(self, port: str) -> None:
        self._ser = serial.Serial(port, baudrate=115200, timeout=0)

    def read(self, size: int = 1024) -> bytes:
        waiting = self._ser.in_waiting
        if not waiting:
            return b""
        return self._ser.read(min(size, waiting))

    def write(self, data: bytes) -> int:
        return self._ser.write(data)

    def close(self) -> None:
        self._ser.close()

    @property
    def is_open(self) -> bool:
        return self._ser.is_open


class _ChunkLimitedDevice(LoopbackBackedDevice):
    """Hands back at most ``chunk`` bytes per read.

    Models a bulk-in endpoint delivering a reply across several packets, so a
    multi-line response costs several pump iterations and the bridge's poll
    interval lands *inside* the response rather than only around it.
    """

    def __init__(self, port: str, *, chunk: int = 4) -> None:
        super().__init__(port)
        self._chunk = chunk

    def read(self, size: int = 1024) -> bytes:
        return super().read(min(size, self._chunk))


@pytest.fixture
def bridged_qr10x():
    """SimulatedQR10x <-> pty <-> PtySerialBridge <-> pty <-> QR10x driver."""
    sim = SimulatedQR10x(settle_s=0.0).start()
    device = LoopbackBackedDevice(sim.port)
    b = PtySerialBridge(device, name="sim-ch341").start()
    qr = None
    try:
        qr = QR10x.open(b.port)
        yield qr, b, sim
    finally:
        if qr is not None:
            qr.close()
        b.close()
        sim.close()


def test_driver_reads_identity_through_the_bridge(bridged_qr10x):
    """The proof the design rests on: an unmodified driver over the bridge.

    This is the same call the on-hardware verification makes. If the AT
    round-trip works here, a failure on the board is in the USB layer, not in
    the driver, the bridge, or the protocol.
    """
    qr, _, _ = bridged_qr10x
    info = qr.info()
    assert info.device_type == "QR101A-1M-R1"
    assert info.serial == "SIM-QR10X-0001"
    assert info.firmware_version == "5.967KS"


def test_driver_readbacks_work_through_the_bridge(bridged_qr10x):
    """Live queries, not just identity — a real request/response each way."""
    qr, _, _ = bridged_qr10x
    assert isinstance(qr.get_setpoint(), float)
    assert isinstance(qr.actual_resistance(), float)


def test_repeated_round_trips_stay_in_sync(bridged_qr10x):
    """Consecutive commands must not leave stale bytes behind.

    A bridge that mis-framed replies would show up here as the second query
    reading the tail of the first one's response.
    """
    qr, _, _ = bridged_qr10x
    for _ in range(5):
        info = qr.info()
        assert info.serial == "SIM-QR10X-0001", "a reply got mis-framed"


def test_a_chunked_reply_is_not_truncated_by_poll_gaps(bridged_qr10x):
    """A multi-packet reply must survive the bridge's polling.

    The QR10x infers end-of-response from silence rather than a terminator, so
    a bridge that stalls mid-response can make a live device look like it
    finished talking, truncating the reply.

    The device here hands back 4 bytes per ``read`` — as a real bulk-in
    endpoint does when a reply spans packets — so each chunk costs one pump
    iteration and the gaps land *inside* the response.

    On the exact threshold: the driver's 60 ms quiet window is not the binding
    constraint, because pyserial's ``timeout=0.2`` blocking read absorbs
    shorter gaps. Truncation was measured to start above ~200 ms (a 100 ms
    poll still passes; 250 ms fails), so this test's chunked round-trip is
    evidence for the *shape* of the failure, and the bound below is what pins
    the constant.
    """
    qr, b, sim = bridged_qr10x
    # Pin the constant directly. The behavioural half cannot discriminate 5 ms
    # from 100 ms, so without this a large regression would slip through.
    assert PtySerialBridge._POLL_S <= 0.02

    b.close()  # rebuild the bridge with a chunk-limited device
    trickle = _ChunkLimitedDevice(sim.port, chunk=4)
    slow = PtySerialBridge(trickle, name="trickle").start()
    try:
        qr2 = QR10x.open(slow.port)
        try:
            # A multi-line reply, delivered 4 bytes per pump iteration.
            info = qr2.info()
            assert info.serial == "SIM-QR10X-0001", "the reply was truncated"
            assert info.firmware_version == "5.967KS", "the reply was truncated"
        finally:
            qr2.close()
    finally:
        slow.close()


def test_bridge_counted_traffic_in_both_directions(bridged_qr10x):
    """Guards against a vacuous pass: bytes really crossed the bridge."""
    qr, b, _ = bridged_qr10x
    qr.info()
    assert b.host_to_device_bytes > 0
    assert b.device_to_host_bytes > 0


def test_a_failing_device_write_does_not_kill_the_pump(bridge):
    """One bad write must not strand the pty.

    If the pump thread died on an exception the port would go permanently
    silent, which reads as a wedged instrument. The driver's own timeout is
    the better signal, so the bridge logs and carries on.
    """
    b, fake = bridge
    original = fake.write
    calls = {"n": 0}

    def flaky(data):
        calls["n"] += 1
        if calls["n"] == 1:
            raise OSError("simulated USB stall")
        return original(data)

    fake.write = flaky
    with serial.Serial(b.port, baudrate=115200, timeout=0.5) as ser:
        ser.write(b"first")
        ser.flush()
        assert _wait_for(lambda: calls["n"] >= 1)
        # The pump survived: a later write still lands.
        ser.write(b"second")
        ser.flush()
        assert _wait_for(lambda: b"second" in bytes(fake.from_host))

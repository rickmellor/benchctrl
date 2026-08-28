"""The ADU218's USBDEVFS link seam, without a USB bus.

Two things here are unlike the other driver test suites, and both follow from
this link talking to the kernel through ``fcntl.ioctl`` rather than to a device
through a byte stream:

1. **The ioctl numbers are asserted against measured constants.**
   :py:mod:`benchctrl.drivers.ontrak_adu218.usbfs` *computes* them from
   ``sizeof(struct usbdevfs_bulktransfer)`` so a 32-bit agent gets the right
   value instead of ``ENOTTY``. That flexibility is exactly what could silently
   produce a wrong number, so ``TestIoctlConstants`` pins the 64-bit results to
   the literals that were observed working on hardware
   (``tests/fixtures/adu218/link_hardware.txt``). If the derivation ever drifts,
   these fail on a laptop rather than on a bench with relays attached.

2. **There is no simulator.** A ``SimDevice`` models a *protocol*; this module's
   whole job is the ioctl layer *underneath* the protocol, so simulating it
   would mean simulating ``fcntl.ioctl`` — i.e. asserting that my model of the
   kernel matches my model of the kernel. The parts worth testing without
   hardware are the ones that are pure logic: device selection, argument
   rejection, framing, and lifecycle. Those are covered here. The ioctl path
   itself is covered by ``link_hardware.txt`` and ``link_dmm_witness.txt``,
   which are real captures, and the fixtures win over anything asserted here.

**What is deliberately NOT tested here:** that ``USBDEVFS_BULK`` works on an
interrupt endpoint, that ``CLAIMINTERFACE`` succeeds with ``usbhid`` loaded,
that a 200 ms timeout never truncates a real reply, and that a write reaches a
physical contact. None can be established off-hardware; all four are in the
fixture captures.
"""

from __future__ import annotations

import ctypes
import errno
import os

import pytest

from benchctrl.drivers.ontrak_adu218 import usbfs
from benchctrl.drivers.ontrak_adu218.usbfs import (
    Adu218Device,
    Adu218LinkError,
    Adu218LinkTimeout,
    Adu218UsbfsLink,
    enumerate_devices,
    find_device,
)


def _fake_sysfs(tmp_path, *entries):
    """Build a sysfs-shaped tree.

    Each entry is a dict of attribute name -> contents, so a test can omit an
    attribute to model a device mid-unplug rather than only well-formed ones.
    """
    root = tmp_path / "sysfs"
    root.mkdir(exist_ok=True)
    for name, attrs in entries:
        directory = root / name
        directory.mkdir()
        for key, value in attrs.items():
            (directory / key).write_text(value + "\n")
    return str(root)


ADU218_ATTRS = {
    "idVendor": "0a07",
    "idProduct": "00da",
    "busnum": "1",
    "devnum": "15",
    "serial": "E02246",
    "product": "ADU218 USB Relay I/O Interface",
    "manufacturer": "www.ontrak.net",
}


def _other(serial, devnum):
    return dict(ADU218_ATTRS, serial=serial, devnum=str(devnum))


class TestIoctlConstants:
    """The computed ioctl numbers must equal the ones seen working.

    These are not a restatement of the implementation. ``_ioc()`` composes a
    direction, a type, a number and a *size*; the size comes from ``ctypes``,
    so a struct-definition slip (a wrong field type, a missing field) changes
    the constant without changing any code that looks wrong. The literals below
    are transcribed from a session that successfully switched a relay.
    """

    def test_claim_matches_measured(self):
        assert usbfs.USBDEVFS_CLAIMINTERFACE == 0x8004550F

    def test_release_matches_measured(self):
        assert usbfs.USBDEVFS_RELEASEINTERFACE == 0x80045510

    def test_bulk_matches_measured(self):
        assert usbfs.USBDEVFS_BULK == 0xC0185502

    def test_bulk_encodes_the_struct_size(self):
        """0xC0185502's ``18`` nibble pair *is* ``sizeof`` — 24 on 64-bit.

        This is the field that makes the constant architecture-dependent, and
        the reason it is derived rather than copied.
        """
        assert ctypes.sizeof(usbfs._BulkTransfer) == 24
        assert (usbfs.USBDEVFS_BULK >> 16) & 0x3FFF == 24

    def test_bulk_would_be_the_32bit_value_on_a_32bit_build(self):
        """A 4-byte pointer gives 16, and 0xC0105502 — the documented armhf value."""
        assert usbfs._ioc(3, ord("U"), 2, 16) == 0xC0105502

    def test_descriptor_facts_match_the_device(self):
        """Endpoints and packet size, read off the live descriptor.

        Hardcoding the wrong endpoint would fail at runtime, but hardcoding
        ``PACKET_SIZE`` too large would silently send trailing garbage, so it
        is pinned too.
        """
        assert (usbfs.VENDOR_ID, usbfs.PRODUCT_ID) == (0x0A07, 0x00DA)
        assert (usbfs.EP_OUT, usbfs.EP_IN) == (0x01, 0x81)
        assert usbfs.PACKET_SIZE == 8
        assert usbfs.INTERFACE == 0
        assert usbfs.REPORT_ID == 0x01
        assert usbfs.MAX_COMMAND_LEN == 7


class TestEnumeration:
    def test_finds_the_device_and_reads_its_strings(self, tmp_path):
        root = _fake_sysfs(tmp_path, ("1-1.2.3.2.2", ADU218_ATTRS))
        (found,) = enumerate_devices(sysfs_root=root, dev_root="/dev/bus/usb")
        assert found.serial == "E02246"
        assert found.bus == 1 and found.device == 15
        assert found.product == "ADU218 USB Relay I/O Interface"

    def test_path_is_zero_padded_three_digits(self, tmp_path):
        """usbdevfs pads: device 15 is ``015``, not ``15``.

        An unpadded path opens nothing, and the failure would look like an
        absent device rather than a formatting bug.
        """
        root = _fake_sysfs(tmp_path, ("x", dict(ADU218_ATTRS, busnum="2", devnum="7")))
        (found,) = enumerate_devices(sysfs_root=root)
        assert found.path == "/dev/bus/usb/002/007"

    def test_ignores_other_devices(self, tmp_path):
        root = _fake_sysfs(
            tmp_path,
            ("hub", {"idVendor": "1d6b", "idProduct": "0002", "busnum": "1", "devnum": "1"}),
            ("kbd", {"idVendor": "046d", "idProduct": "c31c", "busnum": "1", "devnum": "3"}),
            ("adu", ADU218_ATTRS),
        )
        assert [d.serial for d in enumerate_devices(sysfs_root=root)] == ["E02246"]

    def test_ignores_a_different_ontrak_product(self, tmp_path):
        """The ADU208 shares the vendor id and must not be matched.

        Its relays are mechanical rather than PhotoMOS, with different
        switching-rate limits, so sharing a driver would apply the wrong duty
        limits to real hardware. This is the test behind the "do not widen"
        comment on PRODUCT_ID.
        """
        root = _fake_sysfs(tmp_path, ("adu208", dict(ADU218_ATTRS, idProduct="00d8")))
        assert enumerate_devices(sysfs_root=root) == []

    def test_skips_an_entry_missing_busnum(self, tmp_path):
        """A device unplugged mid-walk loses attributes; that is not an error."""
        partial = {k: v for k, v in ADU218_ATTRS.items() if k != "busnum"}
        root = _fake_sysfs(tmp_path, ("half", partial), ("whole", ADU218_ATTRS))
        assert [d.serial for d in enumerate_devices(sysfs_root=root)] == ["E02246"]

    def test_serial_is_optional(self, tmp_path):
        """Absent serial yields None rather than raising — the unit still works."""
        no_serial = {k: v for k, v in ADU218_ATTRS.items() if k != "serial"}
        root = _fake_sysfs(tmp_path, ("x", no_serial))
        assert enumerate_devices(sysfs_root=root)[0].serial is None

    def test_order_is_stable_by_bus_then_device(self, tmp_path):
        """Sorted numerically, not by sysfs listing order or string order.

        Device 9 must precede device 10 — a lexicographic sort would invert
        them and make "the first device" mean different units on different
        boards.
        """
        root = _fake_sysfs(
            tmp_path,
            ("z", _other("C", 10)),
            ("a", _other("B", 9)),
            ("m", dict(ADU218_ATTRS, serial="A", busnum="1", devnum="2")),
        )
        assert [d.serial for d in enumerate_devices(sysfs_root=root)] == ["A", "B", "C"]

    def test_missing_sysfs_root_raises_with_the_path(self, tmp_path):
        with pytest.raises(Adu218LinkError, match="cannot list"):
            enumerate_devices(sysfs_root=str(tmp_path / "nope"))


class TestFindDevice:
    """Selection. The dangerous outcome is picking the wrong relay board."""

    def test_single_device_needs_no_hint(self, tmp_path):
        root = _fake_sysfs(tmp_path, ("x", ADU218_ATTRS))
        assert find_device(sysfs_root=root).serial == "E02246"

    def test_two_devices_refuses_rather_than_choosing(self, tmp_path):
        """**The safety-relevant case.** Never silently pick the first.

        Two ADU218s means two benches. Returning ``devices[0]`` would energise
        whichever one enumerated first, which depends on cabling order.
        """
        root = _fake_sysfs(tmp_path, ("a", _other("E02246", 15)), ("b", _other("E09999", 16)))
        with pytest.raises(Adu218LinkError) as exc:
            find_device(sysfs_root=root)
        assert "pass serial=" in str(exc.value)
        assert "E02246" in str(exc.value) and "E09999" in str(exc.value)

    def test_serial_selects_among_several(self, tmp_path):
        root = _fake_sysfs(tmp_path, ("a", _other("E02246", 15)), ("b", _other("E09999", 16)))
        assert find_device(serial="E09999", sysfs_root=root).device == 16

    def test_unknown_serial_lists_what_was_present(self, tmp_path):
        """The error must name the serials found, or the operator is guessing."""
        root = _fake_sysfs(tmp_path, ("a", ADU218_ATTRS))
        with pytest.raises(Adu218LinkError) as exc:
            find_device(serial="NOPE", sysfs_root=root)
        assert "E02246" in str(exc.value)

    def test_no_device_names_the_vid_pid(self, tmp_path):
        root = _fake_sysfs(tmp_path)
        with pytest.raises(Adu218LinkError, match="0a07:00da"):
            find_device(sysfs_root=root)

    def test_path_selects_a_listed_device(self, tmp_path):
        root = _fake_sysfs(tmp_path, ("a", _other("E02246", 15)), ("b", _other("E09999", 16)))
        got = find_device(path="/dev/bus/usb/001/016", sysfs_root=root)
        assert got.serial == "E09999"

    def test_path_wins_over_ambiguity(self, tmp_path):
        """An explicit path is unambiguous by construction, so two devices is fine."""
        root = _fake_sysfs(tmp_path, ("a", _other("E02246", 15)), ("b", _other("E09999", 16)))
        assert find_device(path="/dev/bus/usb/001/015", sysfs_root=root).serial == "E02246"

    def test_unlisted_path_is_honoured_for_a_clear_open_failure(self, tmp_path):
        """A named node the caller believes in should fail at ``open()``.

        Raising "no ADU218 found" here would send the operator hunting for a
        missing device when the real answer is a bad path.
        """
        root = _fake_sysfs(tmp_path)
        got = find_device(path="/dev/bus/usb/003/004", sysfs_root=root)
        assert got.path == "/dev/bus/usb/003/004" and got.serial is None


class TestConstruction:
    """Argument checks that must fail before any relay is reachable."""

    def test_sub_floor_timeout_rejected_at_construction(self):
        """Below ``bInterval``, correct reads fail — so this is not a tuning knob.

        Rejecting in ``__init__`` rather than at first read means a bad config
        cannot reach the device at all.
        """
        with pytest.raises(ValueError, match="protocol floor"):
            Adu218UsbfsLink(timeout_ms=5)

    def test_the_floor_itself_is_accepted(self):
        assert Adu218UsbfsLink(timeout_ms=usbfs.MIN_TIMEOUT_MS).timeout_ms == 25

    def test_default_timeout_keeps_its_measured_margin(self):
        """200 ms against a measured 16.68 ms worst case — see latency fixtures.

        Pinned because shrinking it is the kind of "harmless" tuning that would
        invalidate the captured evidence for it.
        """
        assert usbfs.DEFAULT_TIMEOUT_MS == 200
        assert usbfs.DEFAULT_TIMEOUT_MS / 16.68 > 10

    def test_construction_touches_no_hardware(self):
        """No lookup, no open, no ioctl until ``open()`` is called."""
        link = Adu218UsbfsLink(serial="E02246")
        assert link.is_open is False
        assert link.device is None

    def test_transfers_before_open_raise_rather_than_using_a_null_fd(self):
        link = Adu218UsbfsLink()
        for call in (lambda: link.write("PK"), lambda: link.read(), lambda: link.query("PK")):
            with pytest.raises(Adu218LinkError, match="not open"):
                call()

    def test_close_before_open_is_a_no_op(self):
        Adu218UsbfsLink().close()


class TestFraming:
    """The 8-byte report, asserted on the bytes handed to the ioctl.

    ``_transfer`` is replaced so the framing is checked without a device. That
    is the whole point: what reaches the wire is pure logic, and it is where a
    wrong report id or a missing NUL pad would hide.
    """

    @pytest.fixture
    def link(self):
        link = Adu218UsbfsLink()
        sent = []

        def fake_transfer(endpoint, buffer, timeout_ms):
            sent.append((endpoint, bytes(buffer), timeout_ms))
            return len(buffer)

        link._transfer = fake_transfer  # type: ignore[assignment]
        link._fd = 999  # satisfy the not-open guard without an fd
        link.sent = sent  # type: ignore[attr-defined]
        return link

    def test_report_id_prefixes_the_command(self, link):
        """Byte 0 must be 0x01. Measured: the device ignores anything else."""
        link.write("SK0")
        endpoint, raw, _ = link.sent[0]
        assert raw[0] == 0x01
        assert raw[1:4] == b"SK0"

    def test_padded_to_eight_bytes_with_nuls(self, link):
        link.write("PK")
        _, raw, _ = link.sent[0]
        assert len(raw) == 8
        assert raw == b"\x01PK\x00\x00\x00\x00\x00"

    def test_writes_go_to_the_out_endpoint(self, link):
        link.write("PK")
        assert link.sent[0][0] == usbfs.EP_OUT

    def test_maximum_length_command_fits_exactly(self, link):
        link.write("MK000AB")
        _, raw, _ = link.sent[0]
        assert raw == b"\x01MK000AB"

    def test_eight_byte_command_rejected(self, link):
        """Seven is the limit; the eighth byte is the report id's."""
        with pytest.raises(ValueError, match="leaves only 7"):
            link.write("MK000ABC")
        assert link.sent == []

    def test_empty_command_rejected(self, link):
        with pytest.raises(ValueError, match="empty"):
            link.write("")
        assert link.sent == []

    def test_non_ascii_rejected_before_the_wire(self, link):
        with pytest.raises(UnicodeEncodeError):
            link.write("PKé")
        assert link.sent == []

    def test_per_call_timeout_overrides_the_default(self, link):
        link.write("PK", timeout_ms=1234)
        assert link.sent[0][2] == 1234

    def test_default_timeout_used_when_not_given(self, link):
        link.write("PK")
        assert link.sent[0][2] == usbfs.DEFAULT_TIMEOUT_MS


class TestReadPath:
    """Reply parsing, including the framing-desync case."""

    def _link_returning(self, *replies):
        link = Adu218UsbfsLink()
        link._fd = 999
        queue = list(replies)

        def fake_transfer(endpoint, buffer, timeout_ms):
            if endpoint == usbfs.EP_OUT:
                return len(buffer)
            if not queue:
                raise Adu218LinkTimeout("no reply")
            payload = queue.pop(0)
            for i, byte in enumerate(payload[: len(buffer)]):
                buffer[i] = byte
            return len(payload)

        link._transfer = fake_transfer  # type: ignore[assignment]
        return link

    def test_strips_the_report_id_and_nul_padding(self):
        link = self._link_returning(b"\x01000\x00\x00\x00\x00")
        assert link.read() == "000"

    def test_reads_from_the_in_endpoint(self):
        link = self._link_returning(b"\x01000\x00\x00\x00\x00")
        seen = []
        inner = link._transfer

        def spy(endpoint, buffer, timeout_ms):
            seen.append(endpoint)
            return inner(endpoint, buffer, timeout_ms)

        link._transfer = spy  # type: ignore[assignment]
        link.read()
        assert seen == [usbfs.EP_IN]

    def test_a_wrong_report_id_raises_instead_of_returning_garbage(self):
        """Framing desync must be loud.

        Silently stripping byte 0 would turn a resync problem into a plausible
        wrong value — and on a relay board a plausible wrong value is worse
        than an exception.
        """
        link = self._link_returning(b"\x02" + b"111" + b"\x00" * 4)
        with pytest.raises(Adu218LinkError, match="out of sync"):
            link.read()

    def test_query_writes_then_reads(self):
        link = self._link_returning(b"\x01" + b"1" + b"\x00" * 6)
        assert link.query("RPK0") == "1"

    def test_timeout_is_also_a_builtin_timeout_error(self):
        """So a caller can catch ``TimeoutError`` without importing ours."""
        link = self._link_returning()
        with pytest.raises(TimeoutError):
            link.read()
        assert issubclass(Adu218LinkTimeout, Adu218LinkError)


class TestDrain:
    """Drain-on-open. The bug it prevents is a *silent wrong answer*."""

    def _link_with_queue(self, depth, *, floods=False):
        link = Adu218UsbfsLink()
        link._fd = 999
        state = {"left": depth}
        timeouts = []

        def fake_transfer(endpoint, buffer, timeout_ms):
            timeouts.append(timeout_ms)
            if floods or state["left"] > 0:
                state["left"] -= 1
                buffer[0] = 0x01
                buffer[1] = ord("0")
                return 8
            raise Adu218LinkTimeout("empty")

        link._transfer = fake_transfer  # type: ignore[assignment]
        link.timeouts = timeouts  # type: ignore[attr-defined]
        return link

    def test_empty_queue_drains_zero(self):
        assert self._link_with_queue(0).drain() == 0

    def test_counts_what_it_discarded(self):
        assert self._link_with_queue(3).drain() == 3

    def test_uses_the_full_timeout_not_a_short_one(self):
        """A drain that gives up early is worse than no drain at all.

        It leaves the reply queued *and* reports the queue clean, so the next
        query still returns the previous command's answer — now with a log line
        claiming the queue was checked.
        """
        link = self._link_with_queue(0)
        link.drain()
        assert link.timeouts == [usbfs.DEFAULT_TIMEOUT_MS]

    def test_a_flooding_device_raises_rather_than_hanging_open(self):
        """Bounded, so a stuck device cannot block ``open()`` indefinitely."""
        link = self._link_with_queue(0, floods=True)
        with pytest.raises(Adu218LinkError, match="not idle"):
            link.drain(limit=8)

    def test_a_framing_error_ends_the_drain_without_raising(self):
        """Drain runs *because* framing is suspect; it must not raise over it."""
        link = Adu218UsbfsLink()
        link._fd = 999

        def fake_transfer(endpoint, buffer, timeout_ms):
            buffer[0] = 0x02  # wrong report id -> Adu218LinkError from read()
            return 8

        link._transfer = fake_transfer  # type: ignore[assignment]
        assert link.drain() == 1


class TestLifecycle:
    """``open``/``close`` against a real fd on a pipe, so no USB is involved.

    The ioctls are stubbed; what is under test is the bookkeeping — that a
    failed claim does not leak an fd, that close is idempotent, and that close
    does not switch anything.
    """

    @pytest.fixture
    def plumbing(self, tmp_path, monkeypatch):
        node = tmp_path / "usbnode"
        node.write_bytes(b"")
        device = Adu218Device(str(node), 1, 15, "E02246", "ADU218", "www.ontrak.net")
        monkeypatch.setattr(usbfs, "find_device", lambda **kw: device)
        calls = []

        def fake_ioctl(fd, request, arg):
            calls.append(request)
            if request == usbfs.USBDEVFS_BULK:
                raise OSError(errno.ETIMEDOUT, "timed out")
            return 0

        monkeypatch.setattr(usbfs.fcntl, "ioctl", fake_ioctl)
        return calls

    def test_open_claims_then_drains(self, plumbing):
        link = Adu218UsbfsLink(serial="E02246")
        link.open()
        assert link.is_open
        assert plumbing[0] == usbfs.USBDEVFS_CLAIMINTERFACE
        assert usbfs.USBDEVFS_BULK in plumbing  # the drain ran
        link.close()

    def test_open_is_idempotent(self, plumbing):
        link = Adu218UsbfsLink()
        link.open()
        before = len(plumbing)
        link.open()
        assert len(plumbing) == before  # no second claim, no second drain
        link.close()

    def test_close_releases_then_closes_and_repeats_safely(self, plumbing):
        link = Adu218UsbfsLink()
        link.open()
        fd = link._fd
        link.close()
        assert usbfs.USBDEVFS_RELEASEINTERFACE in plumbing
        assert not link.is_open
        link.close()  # idempotent
        with pytest.raises(OSError):
            os.fstat(fd)  # the fd really is gone, not merely forgotten

    def test_close_emits_no_relay_command(self, plumbing):
        """``close()`` must not de-energise.

        A teardown that silently drops relays makes every ``with`` block a
        bench event. ``reset_relays()`` is the explicit way, and the only way.
        """
        link = Adu218UsbfsLink()
        link.open()
        plumbing.clear()
        link.close()
        assert usbfs.USBDEVFS_BULK not in plumbing

    def test_a_failed_claim_closes_the_fd(self, tmp_path, monkeypatch):
        """Otherwise a retry loop leaks fds and the second attempt gets EBUSY
        from the process's own abandoned handle."""
        node = tmp_path / "n"
        node.write_bytes(b"")
        monkeypatch.setattr(
            usbfs, "find_device", lambda **kw: Adu218Device(str(node), 1, 1, "S", None, None)
        )
        opened = []
        real_open = os.open
        monkeypatch.setattr(usbfs.os, "open", lambda *a, **k: opened.append(real_open(*a, **k)) or opened[-1])
        monkeypatch.setattr(
            usbfs.fcntl, "ioctl", lambda *a: (_ for _ in ()).throw(OSError(errno.EBUSY, "busy"))
        )
        link = Adu218UsbfsLink()
        with pytest.raises(Adu218LinkError, match="busy"):
            link.open()
        assert not link.is_open
        with pytest.raises(OSError):
            os.fstat(opened[0])

    def test_eacces_names_the_udev_rule(self, tmp_path, monkeypatch):
        """The default node is root:root 0664 and interrupt transfers need write.

        Without this message the operator sees a bare EACCES on a device they
        can already read, which reads as a kernel problem rather than a missing
        udev rule.
        """
        monkeypatch.setattr(
            usbfs, "find_device", lambda **kw: Adu218Device("/nonexistent/x", 1, 1, "S", None, None)
        )
        monkeypatch.setattr(
            usbfs.os, "open", lambda *a, **k: (_ for _ in ()).throw(OSError(errno.EACCES, "denied"))
        )
        with pytest.raises(Adu218LinkError) as exc:
            Adu218UsbfsLink().open()
        assert "63-benchctrl-adu218.rules" in str(exc.value)
        assert "dialout" in str(exc.value)

    def test_context_manager_closes_on_exception(self, plumbing):
        link = Adu218UsbfsLink()
        with pytest.raises(RuntimeError), link:
            assert link.is_open
            raise RuntimeError("boom")
        assert not link.is_open


class TestTransferErrors:
    """errno translation. Each class needs a different operator response."""

    @pytest.mark.parametrize(
        "err,exc,match",
        [
            (errno.ETIMEDOUT, Adu218LinkTimeout, "within"),
            (errno.ENODEV, Adu218LinkError, "disappeared"),
            (errno.ESHUTDOWN, Adu218LinkError, "disappeared"),
            (errno.EPIPE, Adu218LinkError, "USBDEVFS_BULK"),
        ],
    )
    def test_errno_maps_to_a_distinct_message(self, monkeypatch, err, exc, match):
        link = Adu218UsbfsLink()
        link._fd = 999
        monkeypatch.setattr(
            usbfs.fcntl, "ioctl", lambda *a: (_ for _ in ()).throw(OSError(err, "x"))
        )
        with pytest.raises(exc, match=match):
            link.read()

    def test_unplug_is_not_reported_as_a_timeout(self, monkeypatch):
        """The distinction matters: a timeout may be a legitimate silence, an
        unplug never is. Conflating them would have a caller retry a device
        that is gone."""
        link = Adu218UsbfsLink()
        link._fd = 999
        monkeypatch.setattr(
            usbfs.fcntl, "ioctl", lambda *a: (_ for _ in ()).throw(OSError(errno.ENODEV, "x"))
        )
        with pytest.raises(Adu218LinkError) as exc:
            link.read()
        assert not isinstance(exc.value, Adu218LinkTimeout)


class TestRepr:
    """``__repr__`` is where secrets and noise leak. There is no secret here —
    but the serial is the identity that matters, so it must be present."""

    def test_repr_shows_serial_and_state(self):
        text = repr(Adu218UsbfsLink(serial="E02246"))
        assert "E02246" in text and "open=False" in text

    def test_device_repr_shows_path_and_serial(self):
        text = repr(Adu218Device("/dev/bus/usb/001/015", 1, 15, "E02246", "p", "m"))
        assert "001/015" in text and "E02246" in text

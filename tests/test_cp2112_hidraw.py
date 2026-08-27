"""Ioctl encoding for the CP2112's hidraw transport.

This file exists because the simulator cannot cover it. The sim substitutes at
the link seam, so everything from report layouts upward is tested against it —
but the ``_IOC`` arithmetic that turns "get a 5-byte feature report" into a
request number is *below* that seam and would be equally green whether it were
right or wrong. A passing sim suite is not evidence about this code.

The check is against the canonical values the C macros expand to, computed
independently:

    HIDIOCGFEATURE(len) = _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x07, len)
    HIDIOCSFEATURE(len) = _IOC(_IOC_WRITE|_IOC_READ, 'H', 0x06, len)

with dir=3 at bit 30, size at bit 16, type ``'H'`` (0x48) at bit 8, nr at bit 0.
So ``HIDIOCGFEATURE(5)`` is ``0xC0054807``: the ``05`` in the middle is the
length, embedded in the request number, which is the entire reason these must be
computed per call rather than hoisted into a constant.
"""

from __future__ import annotations

import pytest

from benchctrl.drivers.silabs_cp2112.hidraw import (
    MAX_REPORT_BYTES,
    PRODUCT_ID,
    VENDOR_ID,
    HidrawError,
    HidrawLink,
    hidiocgfeature,
    hidiocsfeature,
)


def _reference_ioc(direction: int, typ: str, nr: int, size: int) -> int:
    """The macro, written out longhand from asm-generic/ioctl.h.

    Deliberately a second, independent expression rather than a call into the
    module under test -- comparing a function to itself proves nothing.
    """
    return (direction << 30) | (size << 16) | (ord(typ) << 8) | nr


@pytest.mark.parametrize("size", [1, 2, 4, 5, 13, 63, 64])
def test_get_feature_request_matches_the_c_macro(size: int) -> None:
    assert hidiocgfeature(size) == _reference_ioc(3, "H", 0x07, size)


@pytest.mark.parametrize("size", [1, 2, 4, 5, 13, 63, 64])
def test_set_feature_request_matches_the_c_macro(size: int) -> None:
    assert hidiocsfeature(size) == _reference_ioc(3, "H", 0x06, size)


def test_known_request_numbers_are_exact() -> None:
    """Spot-check literal values, so a refactor of the bit arithmetic is caught.

    These are the numbers strace prints, which makes them the ones worth
    pinning: if a future change breaks the encoding, this is the assertion that
    names the expected value rather than merely reporting a mismatch between two
    computed quantities.
    """
    assert hidiocgfeature(5) == 0xC0054807
    assert hidiocsfeature(5) == 0xC0054806
    assert hidiocgfeature(2) == 0xC0024807
    assert hidiocsfeature(3) == 0xC0034806


def test_size_is_actually_in_the_request_number() -> None:
    """The property that makes a hardcoded constant wrong across ABIs."""
    assert hidiocgfeature(2) != hidiocgfeature(3)
    assert (hidiocgfeature(7) >> 16) & 0x3FFF == 7


def test_get_and_set_are_distinct() -> None:
    """0x06 vs 0x07 -- transposing them would send a write as a read."""
    assert hidiocgfeature(4) != hidiocsfeature(4)


def test_vendor_and_product_ids_are_the_smbus_bridge() -> None:
    """10c4:ea60 is the CP210x UART bridge -- a different chip entirely.

    Matching it would open a serial adapter and send it GPIO feature reports.
    """
    assert (VENDOR_ID, PRODUCT_ID) == (0x10C4, 0xEA90)
    assert PRODUCT_ID != 0xEA60


class TestLinkArgumentValidation:
    """Bounds are checked before the ioctl, not by it.

    An out-of-range length would otherwise be *encoded* into a plausible-looking
    request number and rejected as EINVAL, or worse, partially transferred.
    """

    def test_unopened_link_refuses_reads(self) -> None:
        link = HidrawLink("/dev/hidraw-does-not-exist")
        with pytest.raises(HidrawError, match="not open"):
            link.get_feature(0x02, 4)

    def test_unopened_link_refuses_writes(self) -> None:
        link = HidrawLink("/dev/hidraw-does-not-exist")
        with pytest.raises(HidrawError, match="not open"):
            link.set_feature(0x02, b"\x00\x00\x00\x00")

    def test_missing_node_names_the_device_rather_than_erroring_bare(self) -> None:
        link = HidrawLink("/dev/hidraw-definitely-not-present")
        with pytest.raises(HidrawError, match="does not exist"):
            link.open()

    def test_max_report_bytes_leaves_room_for_the_report_id(self) -> None:
        """The id byte is part of the buffer, so payloads cap one below."""
        assert MAX_REPORT_BYTES == 64

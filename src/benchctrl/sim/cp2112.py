"""A simulated CP2112, standing in at the *link* seam rather than behind a pty.

Every other simulator in this package subclasses
:py:class:`~benchctrl.sim.base.SimDevice` and speaks its protocol down a pty
loopback, which lets the real driver run unmodified against a real file
descriptor. That is not available here and the reason is structural, not
laziness: the CP2112's GPIO commands are HID **feature reports**, carried by
``HIDIOCSFEATURE``/``HIDIOCGFEATURE`` ioctls on a ``/dev/hidraw`` node. A pty
does not implement HID ioctls, and no amount of byte-level fidelity would make
it. So this class implements the four-method
:py:class:`~benchctrl.drivers.silabs_cp2112.hidraw.HidrawLink` surface instead,
and the driver above that seam is exercised in full and unmodified.

What that buys, and what it costs, stated plainly so nobody over-trusts a green
suite: everything from report layouts upward is genuinely tested — masks,
read-modify-write, the open-drain bit sense, verification, policy, restore-on-
close. The ioctl encoding itself is *not*, which is why
``_IOC``/``HIDIOCGFEATURE`` arithmetic is separately checked against the
canonical macro values in ``tests/test_cp2112_hidraw.py`` rather than being
taken on trust from a passing sim.

Fidelity notes — this models chip behaviour that a naive fake would get wrong,
and each one is a bug the driver could otherwise ship with:

* **Writes to an input are silently ignored**, exactly as the chip does. A fake
  that honoured them would let ``set_line_asserted`` appear to work on an
  unconfigured pin, which is the single most likely real-world mistake.
* **Open-drain cannot force a line high.** A pin with a modelled external
  pull-down reads 0 even when released, so read-back verification has something
  real to catch. An open-drain output that always read back as commanded would
  make :py:class:`CP2112VerifyError` untestable and therefore untrustworthy.
* **An undriven (input) pin latches 1**, which is what the real chip's input
  buffer does on a floating pin — the observation that made commissioning
  confusing, since a high-impedance voltmeter reads ~0 V on the same net.
* **The report id is echoed** in a get, and a mismatch is what the driver keys
  its protocol check on.
* **Pins revert to inputs on a device reset** (datasheet §7).
"""

from __future__ import annotations

import logging

from benchctrl.drivers.silabs_cp2112.hidraw import HidrawError

log = logging.getLogger("benchctrl.sim.cp2112")

#: Obviously synthetic, following the QR10x sim's reasoning: a simulator that
#: claims a real unit's serial makes captured logs impossible to attribute.
#: The real bench unit is 00EC63C9.
DEFAULT_SERIAL = "SIM0CP2112"

#: Part 0x0C is a CP2112; rev 0x03 matches the unit on the bench, so the sim
#: answers the same identity shape the driver was written against.
DEFAULT_PART_NUMBER = 0x0C
DEFAULT_DEVICE_VERSION = 0x03


class SimulatedCP2112:
    """A CP2112 that answers feature reports in-process.

    Args:
        pull_downs: bitmask of pins modelled as externally pulled *down*. An
            open-drain output cannot pull these high, so releasing one still
            reads 0 — which is how read-back verification gets tested.
        externally_driven_high: bitmask of pins modelled as driven high by
            something stronger than this chip can sink. Asserting one fails to
            take effect, the other direction of the same failure.
        part_number: overridable so a test can present a non-CP2112 and prove
            ``open(verify_identity=True)`` refuses it.
    """

    def __init__(
        self,
        *,
        serial: str = DEFAULT_SERIAL,
        part_number: int = DEFAULT_PART_NUMBER,
        device_version: int = DEFAULT_DEVICE_VERSION,
        pull_downs: int = 0x00,
        externally_driven_high: int = 0x00,
        path: str = "/dev/hidraw-sim-cp2112",
    ) -> None:
        self.path = path
        self.serial = serial
        self.part_number = part_number
        self.device_version = device_version
        self.pull_downs = pull_downs & 0xFF
        self.externally_driven_high = externally_driven_high & 0xFF

        # Datasheet §7: all eight pins are inputs after reset.
        self.direction = 0x00
        self.push_pull = 0x00
        self.special = 0x00
        self.clock_divider = 0x00
        #: The output latch. Power-on default is high (released) for open-drain.
        self.latch = 0xFF

        self._open = False
        #: Every feature report exchanged, so a test can assert the exact bytes
        #: emitted -- the same technique as the PDU sim's ``command_log``, and
        #: how "no report ever writes an unmasked whole port" is proven.
        self.report_log: list[tuple[str, int, bytes]] = []
        self.reset_count = 0

    # -- HidrawLink surface --------------------------------------------

    def open(self) -> None:
        if self._open:
            raise HidrawError(f"{self.path} is already open")
        self._open = True

    @property
    def is_open(self) -> bool:
        return self._open

    def close(self) -> None:
        self._open = False

    def _require_open(self) -> None:
        if not self._open:
            raise HidrawError(f"{self.path} is not open")

    # -- the modelled chip ---------------------------------------------

    def _effective_levels(self) -> int:
        """What the input buffer would latch on each pin.

        An output pin driving low reads 0. A released open-drain output, or any
        input, reads 1 -- unless something external holds it down. This is where
        the fake stops agreeing with the driver by construction and starts
        modelling a net.
        """
        levels = 0
        for pin in range(8):
            bit = 1 << pin
            if self.direction & bit:  # output
                driving_low = not (self.latch & bit)
                if self.push_pull & bit:
                    level = not driving_low
                else:
                    # Open-drain: can pull low, cannot force high.
                    if driving_low:
                        level = False
                    else:
                        level = not (self.pull_downs & bit)
                    if self.externally_driven_high & bit:
                        # Something stronger wins; the chip cannot sink it.
                        level = True
                if level:
                    levels |= bit
            else:
                # Floating input latches 1 unless pulled down externally.
                if not (self.pull_downs & bit):
                    levels |= bit
        return levels

    def get_feature(self, report_id: int, length: int) -> bytes:
        self._require_open()
        if report_id == 0x05:  # Get Version Information
            payload = bytes((self.part_number, self.device_version))
        elif report_id == 0x02:  # Get GPIO Configuration
            payload = bytes(
                (self.direction, self.push_pull, self.special, self.clock_divider)
            )
        elif report_id == 0x03:  # Get GPIO
            payload = bytes((self._effective_levels(),))
        else:
            raise HidrawError(
                f"simulated CP2112 has no feature report 0x{report_id:02X}; the "
                f"SMBus reports (0x10-0x17) are deliberately unimplemented"
            )
        if len(payload) != length:
            raise HidrawError(
                f"report 0x{report_id:02X} is {len(payload)} bytes, "
                f"caller asked for {length}"
            )
        self.report_log.append(("get", report_id, payload))
        return payload

    def set_feature(self, report_id: int, payload: bytes) -> None:
        self._require_open()
        payload = bytes(payload)
        self.report_log.append(("set", report_id, payload))
        if report_id == 0x01:  # Reset Device
            self.reset_count += 1
            self.direction = 0x00
            self.push_pull = 0x00
            self.special = 0x00
            self.latch = 0xFF
        elif report_id == 0x02:  # Set GPIO Configuration
            if len(payload) != 4:
                raise HidrawError("report 0x02 takes 4 bytes")
            self.direction, self.push_pull, self.special, self.clock_divider = payload
        elif report_id == 0x04:  # Set GPIO
            if len(payload) != 2:
                raise HidrawError("report 0x04 takes 2 bytes")
            value, mask = payload
            # Only masked bits change, and only on pins configured as outputs.
            # The chip silently ignores the rest -- modelled, because a fake
            # that honoured a write to an input would hide the most likely
            # caller mistake there is.
            effective = mask & self.direction
            self.latch = (self.latch & ~effective) | (value & effective)
        else:
            raise HidrawError(
                f"simulated CP2112 has no settable report 0x{report_id:02X}"
            )

    def __repr__(self) -> str:
        return (
            f"SimulatedCP2112(path={self.path!r}, direction=0x{self.direction:02X}, "
            f"latch=0x{self.latch:02X}, open={self._open})"
        )


def make_cp2112(**kwargs: object) -> SimulatedCP2112:
    """Factory, matching the naming in :py:mod:`benchctrl.sim.factories`."""
    return SimulatedCP2112(**kwargs)  # type: ignore[arg-type]

"""Silicon Labs CP2112 GPIO driver — open-drain control lines for the bench.

What this is for
----------------
A cheap, dedicated **hardware reset line**. During the i.MX8 Zephyr bring-up an
Otii Arc Pro was tied up doing nothing but pulling a reset pin low, which is an
expensive way to use a source-measure unit. This driver puts that job on a
~$10 bridge so the Arc stays free for measurement.

Only the GPIO half of the chip is implemented. The CP2112 is primarily a
USB-to-SMBus bridge (reports 0x10-0x17), and its I2C engine is deliberately out
of scope: nothing on the bench needs it, and an untested I2C master that can
issue writes to arbitrary slave addresses is a liability rather than a feature.
See ``KNOWN_LIMITATIONS.md``.

Open-drain is the whole point
-----------------------------
Every output this driver configures is **open-drain**, and
:py:meth:`CP2112.set_line_asserted` is the only way to move a pin. That is a
safety property, not a stylistic preference:

* A reset line is normally pulled up by the *target*, often to a rail this chip
  does not share. An open-drain output can pull that net low and it can release
  it; it can never source into it. Push-pull on the same net is two drivers
  fighting through their output transistors — tens of mA through a pin rated for
  a few, and the damage is to the DUT, not the $10 bridge.
* Open-drain also fails safe on unplug. When the CP2112 is reset or removed its
  pins revert to inputs (high-Z), which *releases* reset rather than holding the
  target down. A push-pull pin left low would hold a DUT in reset with nothing
  attached to explain why.

So push-pull is not exposed. The chip supports it (datasheet §7) and adding it
would be four lines, but there is no bench use for it here and its failure mode
lands on hardware that costs more than this device. ``KNOWN_LIMITATIONS.md``
records the omission as deliberate.

The vocabulary is deliberately *asserted*/*released* rather than high/low.
Reset lines are active-low, so "on" is ambiguous in the one place ambiguity is
expensive; ``asserted=True`` means "reset is being applied" and always means
pulling the net toward ground.

Protocol provenance
-------------------
The datasheet (§10, ``references/CP2112_Rev1.2_DS.pdf``) defers the HID
protocol to "AN495: CP2112 Interface Specification", which is not on disk. The
report map below was recovered instead from the device's own 258-byte HID
**report descriptor**, which is better evidence than a PDF: it is what this
unit's firmware declares. The decoded sizes match the published CP2112 layout
exactly, which is the cross-check.

    id    size  kind      meaning
    0x01   1 B  Feature   Reset Device
    0x02   4 B  Feature   Get/Set GPIO Configuration
    0x03   1 B  Feature   Get GPIO
    0x04   2 B  Feature   Set GPIO
    0x05   2 B  Feature   Get Version Information
    0x06  13 B  Feature   Get/Set SMBus Configuration   (not used)
    0x10-0x17    Out/In   SMBus transfers               (not used)

Facts from the datasheet that shape the code, each cited where used: all eight
pins come up as **inputs** after any reset or re-plug (§7); open-drain logic
high is a pull-up to VIO *internal* to the chip; external pull-ups may not
exceed 5 V; GPIO.0/GPIO.1/GPIO.7 carry alternate TX-toggle/RX-toggle/clock-out
functions (Table 10); and GPIO pins "are not recommended for real-time
signaling" because every transition costs a USB round trip.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from benchctrl.drivers.silabs_cp2112.hidraw import (
    PRODUCT_ID,
    VENDOR_ID,
    HidrawError,
    HidrawLink,
    find_hidraw_nodes,
    read_serial,
)

log = logging.getLogger("benchctrl.drivers.silabs_cp2112")

#: Feature report ids, from the device's own report descriptor (see module doc).
REPORT_RESET_DEVICE = 0x01
REPORT_GPIO_CONFIG = 0x02
REPORT_GPIO_GET = 0x03
REPORT_GPIO_SET = 0x04
REPORT_VERSION = 0x05

#: Datasheet §12: part number 0x0C identifies a CP2112.
PART_NUMBER_CP2112 = 0x0C

#: Eight GPIOs, 0-7.
LINE_COUNT = 8

#: Package pin per GPIO, datasheet Table 4. Carried because the whole reason
#: this driver needed a bench sweep to commission is that breakout silkscreens
#: are unreliable — being able to print "GPIO.7 is package pin 12" makes a
#: wiring dispute settleable.
PACKAGE_PINS: dict[int, int] = {0: 23, 1: 22, 2: 21, 3: 20, 4: 15, 5: 14, 6: 13, 7: 12}

#: Datasheet Table 10. These pins have an alternate function the chip can drive
#: on its own; the driver refuses to configure one as an output unless the
#: caller says the alternate function is off (see :py:meth:`CP2112.set_line_mode`).
ALTERNATE_FUNCTIONS: dict[int, str] = {
    0: "TX Toggle",
    1: "RX Toggle",
    7: "CLK Output",
}

#: A pulse shorter than this cannot be honoured: each transition is a separate
#: USB control transfer, so the floor is set by bus scheduling (1 ms frames on
#: this full-speed device), not by the chip. Datasheet §7 warns GPIO is "not
#: recommended for real-time signaling" for exactly this reason. Rather than
#: silently produce a 3 ms pulse when asked for 100 µs, the driver rejects it.
MIN_PULSE_S = 0.005


class CP2112Error(RuntimeError):
    """Base for every CP2112 failure."""


class CP2112ConnectionError(CP2112Error, ConnectionError):
    """The device could not be opened, or vanished mid-operation."""


class CP2112ProtocolError(CP2112Error):
    """The device answered, but not in a shape the protocol allows."""


class CP2112ValueError(CP2112Error, ValueError):
    """A caller argument is out of range or the wrong type."""


class CP2112PolicyError(CP2112Error):
    """Refused by driver policy: an unallowlisted line, or an alternate-function pin."""


class CP2112VerifyError(CP2112Error):
    """A write was accepted but the read-back disagreed."""


@dataclass(frozen=True)
class CP2112Info:
    """Identity, the closest thing this chip has to an ``*IDN?`` response."""

    part_number: int
    device_version: int
    serial: Optional[str]
    path: str

    @property
    def is_cp2112(self) -> bool:
        return self.part_number == PART_NUMBER_CP2112

    def __str__(self) -> str:
        return (
            f"Silicon Labs CP2112 (part 0x{self.part_number:02X}, "
            f"rev 0x{self.device_version:02X}, serial {self.serial or 'unknown'})"
        )


@dataclass(frozen=True)
class CP2112LineState:
    """One GPIO's configuration and level, as read back from the chip."""

    index: int
    is_output: bool
    push_pull: bool
    level: bool
    alternate_function: Optional[str] = None

    @property
    def open_drain(self) -> bool:
        """True when configured as an output that can only pull low.

        Meaningful only for an output; the chip's push-pull bit is don't-care
        for an input, so this deliberately reads False for one rather than
        reporting a bit that has no effect.
        """
        return self.is_output and not self.push_pull

    @property
    def asserted(self) -> Optional[bool]:
        """True when *we* are pulling this line toward ground (active-low sense).

        ``None`` for an input, and the guard is the point rather than tidiness.
        Without it, an input sitting on a net that something **else** is holding
        low reports ``asserted=True`` — which reads as "we are holding this DUT
        in reset" when we are holding nothing and merely observing a third
        party. On a reset line that is the most expensive possible confusion,
        because it invites a caller to "release" a line it never held and
        conclude the target should now be running.

        ``level`` is still there for anyone who wants the raw latch, with the
        high-Z caveat in :py:meth:`CP2112.read_levels` attached. This property
        answers a narrower question: is this driver asserting the line?

        Found on hardware. Both call sites in ``mcp_tools.py`` had independently
        written ``s.asserted if s.is_output else None`` to compensate, which is
        the tell — a property every caller has to correct has the wrong
        contract, and the next caller would not have known to.
        """
        if not self.is_output:
            return None
        return not self.level


@dataclass(frozen=True)
class CP2112GpioConfig:
    """The raw four bytes of report 0x02, kept so restores are exact."""

    direction: int
    push_pull: int
    special: int
    clock_divider: int

    def to_bytes(self) -> bytes:
        return bytes(
            (self.direction, self.push_pull, self.special, self.clock_divider)
        )

    @classmethod
    def from_bytes(cls, raw: bytes) -> "CP2112GpioConfig":
        if len(raw) != 4:
            raise CP2112ProtocolError(
                f"GPIO configuration report should be 4 bytes, got {len(raw)}"
            )
        return cls(raw[0], raw[1], raw[2], raw[3])

    @property
    def clock_output_enabled(self) -> bool:
        """Datasheet Table 10: bit 0 of ``special`` gates GPIO.7's clock output.

        Load-bearing for safety, not informational. If the chip is driving a
        48 MHz-ish clock on GPIO.7, configuring that pin as a reset output means
        two drivers on one net, and the symptom would be an intermittently
        released reset rather than a clean failure.
        """
        return bool(self.special & 0x01)


def _coerce_line(index: object) -> int:
    """Validate a GPIO index.

    ``bool`` is rejected *before* ``int`` on purpose — it is a subclass, so
    ``set_line_asserted(True, True)`` would otherwise silently mean line 1.
    Same guard as ``_coerce_channel`` in the DP2031 driver.
    """
    if isinstance(index, bool):
        raise CP2112ValueError(
            f"line index must be an int 0-{LINE_COUNT - 1}, got a bool ({index!r}); "
            f"bool is an int subclass, so this is almost certainly a swapped argument"
        )
    if not isinstance(index, int):
        raise CP2112ValueError(
            f"line index must be an int 0-{LINE_COUNT - 1}, got {type(index).__name__}"
        )
    if not 0 <= index < LINE_COUNT:
        raise CP2112ValueError(
            f"line index {index} out of range; the CP2112 has "
            f"{LINE_COUNT} GPIOs (0-{LINE_COUNT - 1})"
        )
    return index


class CP2112:
    """A CP2112 driving open-drain control lines.

    Implements no Protocol from :py:mod:`benchctrl.interfaces`. Per
    ``CONTRIBUTING.md`` convention 3 a Protocol is defined when the *second*
    instance of a shape lands, and this is the first digital-I/O device on the
    bench whose job is control rather than measurement. Generalising a
    ``ControlLine`` Protocol from a single sample would bake in this chip's
    quirks — notably that its pins are all-or-nothing per report and that a
    transition costs a USB round trip. ``rigol_dp2031/__init__.py`` is the
    precedent for saying so out loud instead of implying conformance.

    Single-writer, per ``KNOWN_LIMITATIONS.md`` §N-4: one driver object per
    device key, so one writer claim covers the whole chip. GPIO configuration is
    a single shared register — two objects would each read-modify-write it and
    silently clobber the other's directions.
    """

    def __init__(
        self,
        link: HidrawLink,
        *,
        allowed_lines: Iterable[int],
        serial: Optional[str] = None,
    ) -> None:
        self._link = link
        self._serial = serial
        self._info: Optional[CP2112Info] = None
        #: Set at open() so close() can put the chip back exactly as found.
        self._as_found: Optional[CP2112GpioConfig] = None
        self._allowed = self._validate_allowlist(allowed_lines)

    # -- construction --------------------------------------------------

    @staticmethod
    def _validate_allowlist(allowed_lines: Iterable[int]) -> frozenset[int]:
        if allowed_lines is None:
            raise CP2112ValueError(
                "allowed_lines is required; there is no implicit all-lines default"
            )
        try:
            items = list(allowed_lines)
        except TypeError as e:
            raise CP2112ValueError(
                f"allowed_lines must be an iterable of ints, got "
                f"{type(allowed_lines).__name__}"
            ) from e
        return frozenset(_coerce_line(i) for i in items)

    @classmethod
    def open(
        cls,
        path: Optional[str] = None,
        *,
        allowed_lines: Iterable[int],
        serial: Optional[str] = None,
        verify_identity: bool = True,
    ) -> "CP2112":
        """Open a CP2112 and record its as-found GPIO configuration.

        ``allowed_lines`` is required and keyword-only — deliberately not
        ``Optional`` with an all-lines default. This chip's pins may be wired to
        a DUT's reset, to an enable, or to nothing, and the driver cannot tell
        which. An allowlist fails closed on a typo; a denylist would silently
        widen when someone rewires the breakout. Same reasoning as the PDU's
        ``allowed_outlets``.

        **Nothing is configured or driven here.** Open is observational: it
        reads identity and the existing GPIO configuration and leaves every pin
        exactly as found. Per ``AGENTS.md``, "never energise an output without
        knowing what is attached" — and at open() time this driver does not.
        """
        if path is None:
            nodes = find_hidraw_nodes(serial=serial)
            if not nodes:
                want = f" serial {serial}" if serial else ""
                raise CP2112ConnectionError(
                    f"no CP2112 found: no hidraw node for "
                    f"{VENDOR_ID:04x}:{PRODUCT_ID:04x}{want}. Is it attached, and "
                    f"is deploy/udev/64-benchctrl-cp2112.rules installed?"
                )
            if len(nodes) > 1 and serial is None:
                raise CP2112ConnectionError(
                    f"{len(nodes)} CP2112 devices found ({', '.join(nodes)}); "
                    f"pass serial= to choose one. Refusing to guess, because the "
                    f"wrong guess drives a different board's reset line."
                )
            path = nodes[0]

        link = HidrawLink(path)
        try:
            link.open()
        except HidrawError as e:
            raise CP2112ConnectionError(str(e)) from e

        found_serial = serial or read_serial(path)
        dev = cls(link, allowed_lines=allowed_lines, serial=found_serial)
        try:
            info = dev.read_identity()
            if verify_identity and not info.is_cp2112:
                raise CP2112ProtocolError(
                    f"device at {path} reports part number "
                    f"0x{info.part_number:02X}, expected "
                    f"0x{PART_NUMBER_CP2112:02X} (CP2112). Refusing to send GPIO "
                    f"reports to an unidentified HID device."
                )
            dev._as_found = dev.read_gpio_config()
        except Exception:
            link.close()
            raise
        log.info(
            "CP2112 opened at %s (%s), allowed lines %s, as-found config %s",
            path,
            found_serial or "no serial",
            sorted(dev._allowed),
            dev._as_found.to_bytes().hex(" ") if dev._as_found else "?",
        )
        return dev

    # -- reads ---------------------------------------------------------

    @property
    def is_open(self) -> bool:
        return self._link.is_open

    @property
    def path(self) -> str:
        return self._link.path

    @property
    def serial(self) -> Optional[str]:
        return self._serial

    @property
    def line_count(self) -> int:
        return LINE_COUNT

    @property
    def allowed_lines(self) -> frozenset[int]:
        return self._allowed

    @property
    def info(self) -> CP2112Info:
        """Cached identity. Matches the SDM4065A's ``info`` property shape."""
        if self._info is None:
            return self.read_identity()
        return self._info

    def read_identity(self) -> CP2112Info:
        """Report 0x05: part number and device revision."""
        raw = self._get(REPORT_VERSION, 2)
        info = CP2112Info(
            part_number=raw[0],
            device_version=raw[1],
            serial=self._serial,
            path=self._link.path,
        )
        self._info = info
        return info

    def read_gpio_config(self) -> CP2112GpioConfig:
        """Report 0x02: direction, push-pull, special-function and clock divider."""
        return CP2112GpioConfig.from_bytes(self._get(REPORT_GPIO_CONFIG, 4))

    def read_levels(self) -> int:
        """Report 0x03: the eight pin levels as a bitmask, bit N = GPIO.N.

        For an input this is the *sensed* level; for an output it is the driven
        latch. Worth knowing when interpreting a reading: an undriven pin is
        high-Z, and the chip's input buffer will latch a 1 on it even when a
        high-impedance voltmeter measures ~0 V on the same net. That is not a
        contradiction and it is not a fault — commissioning this device turned on
        exactly that observation. A level therefore identifies nothing on its
        own; only a level you can *change* does.
        """
        return self._get(REPORT_GPIO_GET, 1)[0]

    def read_line_state(self, index: int) -> CP2112LineState:
        """Configuration and level for one GPIO."""
        line = _coerce_line(index)
        cfg = self.read_gpio_config()
        levels = self.read_levels()
        return self._state_from(line, cfg, levels)

    def read_line_states(self) -> dict[int, CP2112LineState]:
        """Configuration and level for all eight GPIOs, in two round trips."""
        cfg = self.read_gpio_config()
        levels = self.read_levels()
        return {i: self._state_from(i, cfg, levels) for i in range(LINE_COUNT)}

    def _state_from(
        self, line: int, cfg: CP2112GpioConfig, levels: int
    ) -> CP2112LineState:
        bit = 1 << line
        return CP2112LineState(
            index=line,
            is_output=bool(cfg.direction & bit),
            push_pull=bool(cfg.push_pull & bit),
            level=bool(levels & bit),
            alternate_function=ALTERNATE_FUNCTIONS.get(line),
        )

    def line_is_asserted(self, index: int) -> Optional[bool]:
        """True when *we* are pulling the line toward ground (active-low sense).

        ``None`` for an input, propagated from
        :py:attr:`CP2112LineState.asserted` rather than flattened to ``False``.
        Flattening would be worse than the bug it hides: "nobody is asserting
        this" and "we cannot tell, because this pin is an input" are different
        facts, and only the second one means the caller is asking the wrong
        object about the state of a reset line.
        """
        return self.read_line_state(index).asserted

    # -- writes (all names carry a dispatch mutator prefix) -------------

    def set_line_mode(
        self,
        index: int,
        *,
        output: bool,
        allow_alternate_function: bool = False,
    ) -> CP2112LineState:
        """Configure one GPIO as an open-drain output or as an input.

        Returns the verified read-back state, never ``None`` — a control line is
        not fire-and-forget.

        Only open-drain outputs can be requested; see the module docstring for
        why push-pull is not exposed. The other seven pins' bits are preserved
        by read-modify-write, so configuring one line cannot disturb a line
        someone else's test is holding.

        ``allow_alternate_function`` is the gate on GPIO.0/1/7 (Table 10). Those
        pins can be driven by the chip itself, and for GPIO.7 the driver also
        checks the clock-enable bit and refuses regardless of this flag if the
        clock is actually running — a caller asserting "the alternate function
        is off" does not make it off.
        """
        line = self._require_allowed(index)
        alt = ALTERNATE_FUNCTIONS.get(line)
        if alt is not None and output and not allow_alternate_function:
            raise CP2112PolicyError(
                f"GPIO.{line} (package pin {PACKAGE_PINS[line]}) carries the "
                f"alternate function {alt!r} (datasheet Table 10). Configuring it "
                f"as an output while that function is active puts two drivers on "
                f"one net. Pass allow_alternate_function=True once you have "
                f"confirmed it is disabled."
            )

        cfg = self.read_gpio_config()
        bit = 1 << line
        if line == 7 and output and cfg.clock_output_enabled:
            raise CP2112PolicyError(
                "GPIO.7's clock output is enabled (special=0x"
                f"{cfg.special:02X}), so the chip is already driving that pin. "
                "Refusing to configure it as an output; disable the clock first. "
                "allow_alternate_function does not override a measured conflict."
            )

        # Direction bit set == output. Push-pull bit CLEAR == open-drain, which
        # is why this masks the bit off rather than setting it.
        new = CP2112GpioConfig(
            direction=(cfg.direction | bit) if output else (cfg.direction & ~bit),
            push_pull=cfg.push_pull & ~bit,
            special=cfg.special,
            clock_divider=cfg.clock_divider,
        )
        self._set(REPORT_GPIO_CONFIG, new.to_bytes())

        state = self.read_line_state(line)
        if state.is_output != output:
            raise CP2112VerifyError(
                f"GPIO.{line} direction read back as "
                f"{'output' if state.is_output else 'input'} after asking for "
                f"{'output' if output else 'input'}"
            )
        if output and state.push_pull:
            raise CP2112VerifyError(
                f"GPIO.{line} read back as push-pull after being configured "
                f"open-drain; refusing to use it as a control line"
            )
        return state

    def set_line_asserted(
        self, index: int, asserted: bool, *, verify: bool = True
    ) -> CP2112LineState:
        """Assert (pull low) or release a line. The only way to move a pin.

        ``asserted=True`` pulls the net toward ground; ``False`` releases it, so
        an external or the chip's internal pull-up takes it high. Deliberately
        *not* named for high/low: reset lines are active-low, so "set_line_high"
        is ambiguous precisely where a mistake holds a DUT in reset.

        The mask byte of report 0x04 is set to this one line, so the command
        cannot disturb the other seven even if the value byte were wrong. That
        is defence in depth against exactly the read-modify-write bug that a
        whole-port write invites.

        Returns the verified read-back state.
        """
        line = self._require_allowed(index)
        state = self.read_line_state(line)
        if not state.is_output:
            raise CP2112PolicyError(
                f"GPIO.{line} is an input; call set_line_mode(output=True) first. "
                f"Writing report 0x04 to an input is silently ignored by the chip, "
                f"which would look like a dead reset line."
            )
        if state.push_pull:
            raise CP2112PolicyError(
                f"GPIO.{line} is configured push-pull, which this driver never "
                f"sets. Refusing to drive it: push-pull into a target's pulled-up "
                f"reset net is two drivers fighting."
            )

        bit = 1 << line
        value = 0x00 if asserted else bit
        self._set(REPORT_GPIO_SET, bytes((value, bit)))
        if not verify:
            return state

        after = self.read_line_state(line)
        if after.asserted != asserted:
            # Three outcomes, not two. ``asserted`` is None when the pin came
            # back as an *input*, which is a different fault from a line that
            # would not move — and naming it "released" would be a false claim
            # about the level on a reset line. It should be unreachable (§N-4
            # gives one writer per device), so if it ever fires the message has
            # to say what was actually seen rather than the nearest bool.
            if after.asserted is None:
                seen = "as an input, so it is no longer driven at all"
            else:
                seen = "asserted" if after.asserted else "released"
            raise CP2112VerifyError(
                f"GPIO.{line} read back {seen} after asking for "
                f"{'asserted' if asserted else 'released'}. If this line is "
                f"externally driven, the CP2112 cannot pull it and open-drain "
                f"cannot force it."
            )
        return after

    def trigger_reset_pulse(
        self, index: int, *, duration_s: float = 0.1, settle_s: float = 0.0
    ) -> CP2112LineState:
        """Assert a line for ``duration_s``, then release it. The reset primitive.

        This is the Otii-Arc-Pro-replacement operation: pull a target's reset
        low, hold, release. It exists as one method rather than two calls
        because the release must happen even if the hold is interrupted —
        leaving a DUT held in reset by a ``KeyboardInterrupt`` is the failure
        this prevents, and a ``finally`` in the driver is more reliable than one
        in every caller.

        ``settle_s`` waits *after* release, for callers that immediately measure
        and would otherwise sample a still-booting target.

        Pulses shorter than :py:data:`MIN_PULSE_S` are rejected rather than
        silently stretched: each transition is its own USB control transfer, so
        the achievable floor is bus scheduling, not the chip (datasheet §7 warns
        GPIO is "not recommended for real-time signaling"). A caller that needs
        a microsecond-accurate pulse needs different hardware, and should be
        told so rather than handed a 3 ms one.
        """
        line = self._require_allowed(index)
        if not isinstance(duration_s, (int, float)) or isinstance(duration_s, bool):
            raise CP2112ValueError(
                f"duration_s must be a number, got {type(duration_s).__name__}"
            )
        if duration_s < MIN_PULSE_S:
            raise CP2112ValueError(
                f"duration_s={duration_s} is below the {MIN_PULSE_S} s floor. Each "
                f"GPIO transition is a separate USB control transfer, so a shorter "
                f"request cannot be honoured and would silently become ~3 ms. Use "
                f"a timing-capable instrument for sub-5 ms pulses."
            )
        if settle_s < 0:
            raise CP2112ValueError(f"settle_s must be >= 0, got {settle_s}")

        self.set_line_asserted(line, True)
        try:
            time.sleep(duration_s)
        finally:
            # Release even on interrupt: a held reset is the one state that
            # leaves the bench worse than untouched.
            state = self.set_line_asserted(line, False)
        if settle_s:
            time.sleep(settle_s)
        log.info("GPIO.%d reset pulse: %.3f s asserted", line, duration_s)
        return state

    def reset_lines(self) -> CP2112GpioConfig:
        """Return every allowed line to an input (high-Z), releasing anything held.

        Only the allowlisted lines are touched, so this cannot disturb a pin the
        allowlist deliberately excludes. Returns the resulting configuration.

        Inputs are the chip's own post-reset state (datasheet §7), so this is
        "as the hardware would come up", not an invented safe state. For an
        open-drain reset line, high-Z *is* released.
        """
        cfg = self.read_gpio_config()
        mask = 0
        for line in sorted(self._allowed):
            mask |= 1 << line
        # Release before going high-Z. Redundant, since an input cannot pull,
        # but it means the latch is not left holding a 0 for whenever the pin is
        # next configured as an output.
        if mask and (cfg.direction & mask):
            self._set(REPORT_GPIO_SET, bytes((mask & 0xFF, mask & 0xFF)))
        new = CP2112GpioConfig(
            direction=cfg.direction & ~mask,
            push_pull=cfg.push_pull & ~mask,
            special=cfg.special,
            clock_divider=cfg.clock_divider,
        )
        self._set(REPORT_GPIO_CONFIG, new.to_bytes())
        return self.read_gpio_config()

    # -- policy and transport ------------------------------------------

    def _require_allowed(self, index: object) -> int:
        line = _coerce_line(index)
        if line not in self._allowed:
            raise CP2112PolicyError(
                f"GPIO.{line} (package pin {PACKAGE_PINS[line]}) is not in "
                f"allowed_lines={sorted(self._allowed)}. The driver cannot know "
                f"what a pin is wired to, so lines are opt-in per instrument."
            )
        return line

    def _get(self, report_id: int, length: int) -> bytes:
        try:
            return self._link.get_feature(report_id, length)
        except HidrawError as e:
            raise CP2112ConnectionError(str(e)) from e

    def _set(self, report_id: int, payload: bytes) -> None:
        try:
            self._link.set_feature(report_id, payload)
        except HidrawError as e:
            raise CP2112ConnectionError(str(e)) from e

    # -- teardown ------------------------------------------------------

    def close(self, *, restore: bool = True) -> None:
        """Release the device, first restoring the as-found GPIO configuration.

        ``close()`` deliberately has a side effect on the hardware, which is
        unusual in this repo and needs the justification: a driver that merely
        closed the fd would leave an open-drain line still **asserted**, holding
        a DUT in reset with no process left to release it and nothing on the
        bench to explain why. Restoring the configuration captured at open() is
        the only exit that cannot strand the target.

        Restoration is best-effort and never raises — a failure here must not
        mask the exception that prompted the close.
        """
        if restore and self._as_found is not None and self._link.is_open:
            try:
                # Release everything we were allowed to drive before changing
                # directions, so no latch is left holding a 0.
                mask = 0
                for line in sorted(self._allowed):
                    mask |= 1 << line
                if mask:
                    self._link.set_feature(
                        REPORT_GPIO_SET, bytes((mask & 0xFF, mask & 0xFF))
                    )
                self._link.set_feature(
                    REPORT_GPIO_CONFIG, self._as_found.to_bytes()
                )
            except Exception as e:  # pragma: no cover - best-effort
                log.warning("could not restore CP2112 GPIO configuration: %s", e)
        self._link.close()

    def __enter__(self) -> "CP2112":
        return self

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"CP2112(path={self._link.path!r}, serial={self._serial!r}, "
            f"allowed_lines={sorted(self._allowed)}, open={self.is_open})"
        )

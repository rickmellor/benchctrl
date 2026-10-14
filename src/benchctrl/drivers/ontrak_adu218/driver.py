"""Ontrak ADU218 USB relay / digital-input interface.

8 PhotoMOS solid-state relays (Panasonic AQZ207, 1 A at 120 V AC or DC) and 8
optically-isolated digital inputs on one USB HID device, driven over
:py:mod:`~benchctrl.drivers.ontrak_adu218.usbfs` with **zero dependencies**.

Everything asserted here is backed by a capture in ``tests/fixtures/adu218/``.
Where the vendor manual disagrees with a capture, the capture wins — the manual
contradicts itself in at least one place (it calls the both-ports read ``RI`` in
its summary table and ``PI`` in its description; only ``PI`` answers).

Three device properties shape the whole design
----------------------------------------------

**1. Silence is the only error signal, so response expectations cannot be
inferred.** An unknown command, a valid command with an out-of-range argument,
a malformed argument, and a write-only command are *byte-identical* on the
wire: nothing comes back, no error string, no sentinel (``errors.txt``). So
there is no runtime way to ask "was that understood?" — every command needs a
declared ``responsive`` flag, and :py:data:`_COMMAND_SPECS` is that declaration.

The flag is **never** derived from the mnemonic, even though the pattern nearly
holds. Responsive commands start with ``R`` or ``P``, and bare ``DB``/``WD``
answer while their ``n``-suffixed setters do not — but **``RKn`` starts with
``R`` and is write-only**, and it is the most-called command on the device. A
mnemonic-based rule would wait 200 ms for a reply that never comes on every
single de-energise.

**2. An unread reply outlives its command.** Interrupt-IN replies sit on the
endpoint until read, so a skipped read makes the *next* query return the
*previous* command's answer — a silent wrong value, not an exception. This is
why the link drains on open, why every responsive command has a declared width,
and why ``verify=`` re-reads rather than trusting a write.

**3. Out-of-range behaviour is undocumented, so the host validates.** ``MK300``
exceeds a byte; if the device aliases rather than rejecting it, a whole-port
write could energise relays nobody asked for. Every argument is range-checked
here, and separately the *rendered* command is matched against a whitelist
before the write. Those two guards are deliberately independent: the first
catches a bad argument and produces a useful message, the second catches a bad
*rendering* — a format-string slip that a value check cannot see.

Polarity, and the word "open"
-----------------------------
``is_open`` means **the link is connected**, exactly as everywhere else in
benchctrl (``agent/registry.py`` publishes it as the ``"open"`` key for every
served device). For a relay that word points the opposite way: an *open* contact
is not conducting, while an *open* driver is connected. So no relay-facing name
uses open/close/opened/closed, and relay state is reported as **energised**:
``True`` means conducting, matching the device's own ``RPKn``=1.

``close()`` closes the link and **deliberately leaves the relays exactly where
they were** — see its docstring.

No contact-resistance figure is reported, ever
----------------------------------------------
The same closed relay measured 6.14 Ω, then 10.69 Ω, then 10.65 Ω across three
sessions, milliohm-stable within each, with the step traced to re-seated
screw-clamped probes rather than to the relay. A threshold set from any one of
those numbers would have misread the others. Verification keys on the device's
own state read-back; an external witness keys on a DMM's over-range sentinel.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional

from benchctrl.drivers.ontrak_adu218.usbfs import (
    Adu218Device,
    Adu218LinkError,
    Adu218LinkTimeout,
    Adu218UsbfsLink,
)

log = logging.getLogger("benchctrl.drivers.ontrak_adu218")


# ---------------------------------------------------------------------------
# Exceptions
# ---------------------------------------------------------------------------


class ADU218Error(RuntimeError):
    """Base class for ADU218 driver errors."""


class ADU218ConnectionError(ADU218Error):
    """Could not open or claim the USB device, or lost it mid-session."""


class ADU218ProtocolError(ADU218Error):
    """A reply arrived but had the wrong shape.

    Distinct from a timeout: this means the device *answered* and the answer
    could not be believed — a width that disagrees with
    :py:data:`_COMMAND_SPECS`, or a non-numeric payload where a number is
    required. On a device whose only error signal is silence, an answer of the
    wrong shape is the strongest evidence available that framing has desynced.
    """


class ADU218TimeoutError(ADU218Error, TimeoutError):
    """A responsive command did not answer.

    **This is ambiguous by construction and the driver cannot disambiguate
    it.** The device is silent for an unknown command, for a valid command with
    a bad argument, and for a command that legitimately has no reply. Reaching
    this exception means a command declared ``responsive`` in
    :py:data:`_COMMAND_SPECS` produced nothing, which is either a wrong entry in
    that table or a device that has stopped answering.
    """


class ADU218ValueError(ADU218Error, ValueError):
    """A host-side range or type check failed, before anything was sent."""


class ADU218PolicyError(ADU218Error):
    """A relay outside ``allowed_relays`` was targeted.

    Deliberately **not** a :py:class:`ValueError` subclass: the index was
    perfectly valid for the hardware, and the refusal is a configured policy
    rather than a malformed request. A caller catching ``ValueError`` to mean
    "I passed something wrong" should not swallow this.
    """


# ---------------------------------------------------------------------------
# Hardware limits, all measured or manual-stated
# ---------------------------------------------------------------------------

#: Relays are ``K0``–``K7`` — eight of them, **zero-indexed**.
RELAY_COUNT = 8

#: The fastest the vendor recommends switching a relay, in cycles per second
#: (manual, Relay Outputs spec table, and the CAUTION beside it: *"Power
#: dissipation of PhotoMOS relays increases with switching speed. At full-load
#: rating, the maximum recommended switching speed is 1 CPS. The ADU218 is not
#: recommended for PWM applications."*)
#:
#: **Documented, deliberately not enforced.** The figure is qualified by *at full
#: load*, and the driver cannot know the load — nothing in USB, HID or the ADU
#: command set reports what a contact is switching. A hard rate limit would
#: therefore throttle a dry-contact sweep, which is most of what this bench does,
#: on the strength of a condition it cannot observe. It is here so a caller
#: writing a toggle loop against a real load has the number, and so nobody reads
#: the absence of a limit as evidence there isn't one. Compare the ADU208's
#: mechanical relays at 10 CPS: the solid-state part is the *slower* one to cycle
#: under load, which inverts the usual expectation.
RELAY_MAX_SWITCH_HZ = 1.0

#: Eight digital inputs, but as **two ports of four**: PORT A lines 0-3 and
#: PORT B lines 0-3. Confirmed by ``reads.txt``, where ``RPA4``/``RPB4`` are
#: silent while ``RPA3``/``RPB3`` answer.
#:
#: The two ranges are the reason there is no single shared index validator in
#: this module. One validator covering relays (0..7) and inputs (0..3) would be
#: a live off-by-four: ``RPA5`` would pass the check and then vanish into the
#: device's silence, surfacing as a timeout rather than as a bad argument.
INPUT_PORTS = ("A", "B")
INPUT_LINES_PER_PORT = 4
INPUT_COUNT = len(INPUT_PORTS) * INPUT_LINES_PER_PORT

#: Event counters, one per digital input.
#:
#: Counters count **low-to-high transitions only** — one count per cycle, not
#: one per edge (manual §6c). Measured on this unit against a 10 Hz square wave
#: on PA3: the counter read 10.030 counts/s while host level-sampling saw 9.997
#: rising *and* 9.997 falling edges per second. Had it counted both edges the
#: ratio would have been 2.0, not 1.003.
COUNTER_COUNT = 8

#: The widest value a counter reports: ``REn`` returns 5 digits, ``00000``.
COUNTER_MAX = 65535

#: The highest input frequency the counters are rated for (manual, Event
#: Counters spec table). Above this the count is not trustworthy, and the driver
#: cannot detect the overrun — a too-fast signal simply under-counts silently.
#: Relevant to :py:data:`DEBOUNCE_MS`: the 100 µs setting cannot be told apart
#: from the 1 ms one below about 500 Hz, so its effect is only observable in the
#: top of this range.
COUNTER_MAX_FREQUENCY_HZ = 1000

#: De-bounce settings. **Three, not four.** Ontrak's web page lists a fourth
#: (``NONE``) but the manual bounds ``n`` to 0..2 and the captures show 0/1/2.
#: The same four-option string appears on the ADU208 and ADU228 pages, so it
#: reads as shared boilerplate rather than a per-product spec.
DEBOUNCE_SETTINGS = (0, 1, 2)

#: What each de-bounce setting actually *means*, in milliseconds (manual §6c).
#:
#: **The ordering is inverted: a higher setting is a SHORTER filter.** This is
#: the whole reason the mapping is spelled out here rather than left to the
#: device. ``DEBOUNCE_SETTINGS`` alone invites two wrong guesses — that 0 means
#: "off" (it is the *longest* filter, 10 ms) and that a bigger number filters
#: harder (2 is the *weakest*, 100 µs). An operator wanting maximum contact
#: de-bounce would reach for 2 and get the least filtering available.
#:
#: 1 (1 ms) is the device default and what this unit reported out of the box.
DEBOUNCE_MS = {0: 10.0, 1: 1.0, 2: 0.1}

#: Watchdog settings, and the timeout each selects.
#:
#: ``WD1``'s trip time was measured by bisecting the silence window:
#: **(0.90, 1.10] s**, i.e. the documented 1 s. An earlier capture's "3.7 s" was
#: observation latency, not a trip time — see ``watchdog.txt``.
WATCHDOG_SETTINGS = (0, 1, 2, 3)
WATCHDOG_TIMEOUT_S = {0: None, 1: 1.0, 2: 10.0, 3: 60.0}

#: Whole-port relay write: ``MKddd`` takes a **zero-padded three-digit decimal**
#: byte. ``MK9999`` is silent; ``MK300`` is out of range and is rejected here
#: rather than offered to the device, because aliasing behaviour is undocumented
#: and the failure mode would be energising relays nobody named.
RELAY_MASK_MAX = 255


# ---------------------------------------------------------------------------
# The command table: what may be sent, and what answers
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class _CommandSpec:
    """One command family: how it is spelled, and whether it replies.

    ``width`` is the exact ASCII payload length of the reply, taken from
    ``reads.txt`` rather than from the manual — the manual gives an explicit
    width for some commands and only an example for others (``RPKn``, ``RPyn``,
    ``DB``, ``WD``), and an example is not a specification. Checking the width
    is how a desynced reply becomes an exception instead of a plausible value.
    """

    pattern: re.Pattern
    responsive: bool
    width: Optional[int]
    what: str


def _spec(regex: str, responsive: bool, width: Optional[int], what: str) -> _CommandSpec:
    return _CommandSpec(re.compile(regex), responsive, width, what)


#: **Every command this driver can emit, and nothing else.**
#:
#: This is the emitted-command whitelist as well as the response-width table.
#: A command that matches no entry is refused before the write, so a rendering
#: slip cannot reach the device — including one that produced a *valid* command
#: nobody intended. Note what is absent and stays absent: there is no
#: aggregate-relay form beyond the explicitly-bounded ``MKddd``, and no vendor
#: verb outside this list is reachable from any method.
#:
#: ``responsive`` is stated per family, never inferred. ``RKn`` is the trap: it
#: starts with ``R`` like every read, and it is write-only.
_COMMAND_SPECS: tuple[_CommandSpec, ...] = (
    # -- relay reads --
    _spec(r"^PK$", True, 3, "all relays as a decimal byte"),
    _spec(r"^RPK[0-7]$", True, 1, "one relay"),
    # -- relay writes (silent) --
    _spec(r"^SK[0-7]$", False, None, "energise one relay"),
    _spec(r"^RK[0-7]$", False, None, "de-energise one relay -- WRITE-ONLY despite the R"),
    _spec(r"^MK(?:0[0-9][0-9]|1[0-9][0-9]|2[0-4][0-9]|25[0-5])$", False, None, "whole relay port"),
    # -- input reads --
    _spec(r"^P[AB]$", True, 2, "one input port as a decimal nibble"),
    _spec(r"^RP[AB]$", True, 4, "one input port in binary, MSB first"),
    _spec(r"^RP[AB][0-3]$", True, 1, "one input line"),
    _spec(r"^PI$", True, 3, "both input ports as a decimal byte"),
    # -- counters --
    _spec(r"^RE[0-7]$", True, 5, "read one event counter"),
    _spec(r"^RC[0-7]$", True, 5, "read AND CLEAR one event counter"),
    # -- settings --
    _spec(r"^DB$", True, 1, "read the de-bounce setting"),
    _spec(r"^DB[0-2]$", False, None, "set the de-bounce"),
    _spec(r"^WD$", True, 1, "read the watchdog setting"),
    _spec(r"^WD[0-3]$", False, None, "set AND ARM the watchdog"),
)


def command_spec(command: str) -> _CommandSpec:
    """Look up a rendered command, or refuse it.

    Public so a test can assert the whitelist directly rather than inferring it
    from driver behaviour — the guarantee "no method can emit an unlisted
    command" is worth testing at the gate as well as through it.
    """
    for spec in _COMMAND_SPECS:
        if spec.pattern.match(command):
            return spec
    raise ADU218ValueError(
        f"{command!r} is not in this driver's command whitelist. Nothing was "
        f"sent. Either the argument checks let something through or a command "
        f"was mis-rendered; both are bugs here, not device conditions."
    )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class ADU218Info:
    """Identity, read from the USB descriptor and sysfs rather than the device.

    The ADU218 has **no identity command** — no ``*IDN?`` equivalent, nothing in
    the manual's verb list. So unlike every other driver in this repo, identity
    costs no round trip and cannot fail mid-session: it is fixed at ``open()``
    from what the kernel already enumerated.

    **There is no firmware field, deliberately.** This unit reports
    ``bcdDevice 0000``, so there is no version to report; deriving one from the
    product string would be a guess wearing a measurement's name. If a future
    unit reports a real ``bcdDevice``, add the field then — an always-``None``
    field invites callers to treat its absence as "old firmware".
    """

    model: str
    serial: Optional[str]
    manufacturer: Optional[str]
    product: Optional[str]
    vendor_id: int
    product_id: int
    bus: int
    device: int
    relay_count: int
    input_count: int

    def to_dict(self) -> dict:
        return {
            "model": self.model,
            "serial": self.serial,
            "manufacturer": self.manufacturer,
            "product": self.product,
            "vendor_id": self.vendor_id,
            "product_id": self.product_id,
            "bus": self.bus,
            "device": self.device,
            "relay_count": self.relay_count,
            "input_count": self.input_count,
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class OntrakADU218:
    """An Ontrak ADU218, over raw USBDEVFS.

    Method names are a safety decision, not a style one. ``agent/dispatch.py``
    derives which calls need a writer claim **purely from name prefixes**,
    walking the class with no driver-declared override — so a method named
    ``close_relay()`` would be remotely callable with *no claim*. Every write
    here takes an existing prefix (``set_``, ``reset``, ``clear_``) and no read
    does. ``tests/test_bench_adu218.py`` pins that both ways; renaming a method
    without checking it is how relay control loses its gate.

    Typical use::

        with OntrakADU218.open(serial="E02246") as adu:
            adu.set_relay_state(0, True)      # returns the verified read-back
            adu.relay_states()                # {0: True, 1: False, ...}
            adu.reset_relays()                # MK000 — all de-energised
    """

    #: Advertised so callers do not hardcode 8 in two places.
    relay_count = RELAY_COUNT
    input_count = INPUT_COUNT

    def __init__(
        self,
        *,
        serial: Optional[str] = None,
        path: Optional[str] = None,
        timeout_ms: int = 200,
        allowed_relays: Optional[tuple] = None,
        link: Optional[Adu218UsbfsLink] = None,
    ) -> None:
        """Construct without touching hardware. Use :py:meth:`open`.

        ``link`` injects a substitute transport (the simulator, or a fake) and
        is the only way to exercise this class without a USB bus. It is keyword
        only and undocumented in the SDK docs on purpose: a caller who passes it
        by accident would get a driver that talks to nothing.

        It is annotated as an :py:class:`Adu218UsbfsLink` rather than a bare
        ``object`` or a Protocol because that is the actual contract: the
        simulator *subclasses* the production link and replaces only the ioctl
        and the device node, deliberately, so framing and the desync check stay
        shipping code under test. A duck-typed annotation here would let a
        substitute skip that framing and pass type checking while diverging from
        the wire — the exact failure the subclassing was chosen to prevent.
        """
        self._allowed = self._coerce_allowed(allowed_relays)
        if link is not None:
            self._link = link
        else:
            self._link = Adu218UsbfsLink(serial=serial, path=path, timeout_ms=timeout_ms)
        self._info: Optional[ADU218Info] = None
        # The watchdog's own setting is unreadable-with-meaning: WD returns 0
        # both for "timed out" and for "never enabled". So the driver holds what
        # it last commanded, and read_watchdog_tripped() compares the two.
        self._watchdog_setting = 0

    # -- construction -------------------------------------------------------

    @staticmethod
    def _coerce_allowed(allowed_relays: Optional[tuple]) -> frozenset:
        """Default to every relay, and say why that differs from the PDU.

        The PDU41002 makes ``allowed_outlets`` **mandatory** with no "all"
        default, because a typo there de-powers mains. This device is different
        in kind — 1 A signal-level SSRs, wired to instrument leads, and the
        operator's explicit instruction was that relays toggle freely by default
        with the hardware watchdog available as the per-test interlock. So the
        default is permissive and the allowlist is available to narrow it.

        That is a decision about *this* bench, so it is recorded rather than
        implied: if an ADU218 is ever wired to something that must not switch,
        pass ``allowed_relays`` and the refusal is enforced below.

        **What the allowlist covers.** Closing a contact, not opening one:
        :py:meth:`set_relay_state` refuses to energise an unlisted relay but
        will always de-energise one, and :py:meth:`reset_relays` bypasses the
        list entirely. Both keep the safe state reachable on exactly the benches
        most carefully configured. :py:meth:`set_relay_port` is the exception and
        enforces on the whole mask, because ``MKddd`` moves all eight lines in
        one indivisible command — there is no de-energise-only form of it.
        """
        if allowed_relays is None:
            return frozenset(range(RELAY_COUNT))
        coerced = set()
        for item in allowed_relays:
            coerced.add(_coerce_relay_index(item))
        return frozenset(coerced)

    @classmethod
    def open(
        cls,
        *,
        serial: Optional[str] = None,
        path: Optional[str] = None,
        timeout_ms: int = 200,
        allowed_relays: Optional[tuple] = None,
        link: Optional[Adu218UsbfsLink] = None,
        disarm_watchdog: bool = True,
    ) -> "OntrakADU218":
        """Connect, and replace the inherited watchdog state with a known one.

        ``disarm_watchdog=True`` sends ``WD0`` at connect. This is not tidiness:
        ``WD``'s value is only interpretable against a driver-held expectation
        (see :py:meth:`read_watchdog_tripped`), and a fresh process has no
        expectation to compare against — the first ``WD`` read after a restart
        is ambiguous *by construction*. Writing a known value replaces an
        inherited unknown. Pass ``disarm_watchdog=False`` only to inspect what a
        previous session left behind, and expect that ambiguity.

        **Relays are deliberately left alone.** Power-on relay state is
        undocumented, and USB suspend explicitly holds outputs in their last
        state — including when the host suspends the device *because no handle
        is open* — so a closed handle can coexist with energised outputs
        indefinitely. This reads the state and reports it; it drives ``MK000``
        only when :py:meth:`reset_relays` is called explicitly.
        """
        device = cls(
            serial=serial,
            path=path,
            timeout_ms=timeout_ms,
            allowed_relays=allowed_relays,
            link=link,
        )
        device._connect(disarm_watchdog=disarm_watchdog)
        return device

    def _connect(self, *, disarm_watchdog: bool = True) -> None:
        try:
            self._link.open()
        except Adu218LinkError as exc:
            raise ADU218ConnectionError(str(exc)) from exc

        if disarm_watchdog:
            self.set_watchdog(0)

        # Read, do not drive. See open()'s docstring on suspend holding outputs.
        try:
            energised = self.relay_states()
        except ADU218Error:
            self.close()
            raise
        live = sorted(index for index, on in energised.items() if on)
        if live:
            log.warning(
                "ADU218 %s has relays %s already energised at connect; leaving "
                "them as found (call reset_relays() to de-energise)",
                self._describe(),
                live,
            )

    def _describe(self) -> str:
        device = getattr(self._link, "device", None)
        serial = getattr(device, "serial", None)
        return serial or "<unknown serial>"

    # -- lifecycle ----------------------------------------------------------

    @property
    def is_open(self) -> bool:
        """Whether the **link** is connected.

        Framework meaning, matching ``agent/registry.py`` and every transport in
        the repo. It says nothing about any relay contact — and note the two
        senses of "open" point opposite ways here, which is why relay state is
        reported as *energised* instead.
        """
        return bool(getattr(self._link, "is_open", False))

    def close(self) -> None:
        """Release the device. **Does not de-energise the relays.**

        A relay left conducting stays conducting. That is deliberate: a teardown
        that silently drops contacts would make every ``with`` block a bench
        event, and a driver has no way to know whether an energised relay is
        holding something that must not be interrupted. :py:meth:`reset_relays`
        is the explicit way, and the only way.

        It also does not disarm the watchdog, for a sharper reason: if ``WD`` is
        armed, releasing the device *is* the silence it exists to detect, and
        the relays will drop on their own within the configured interval. That
        is the interlock working. Disarming here would defeat exactly the case
        it was armed for.
        """
        try:
            self._link.close()
        except Adu218LinkError as exc:  # pragma: no cover - best-effort teardown
            log.debug("closing ADU218 link: %s", exc)

    def __enter__(self) -> "OntrakADU218":
        if not self.is_open:
            self._connect()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return (
            f"OntrakADU218(serial={self._describe()!r}, open={self.is_open}, "
            f"allowed_relays={sorted(self._allowed)})"
        )

    # -- the single path to the wire ---------------------------------------

    def _send(self, command: str) -> Optional[str]:
        """Emit one whitelisted command and read its reply if one is declared.

        **Every** command goes through here, so the whitelist, the response
        expectation and the width check cannot be bypassed by a new method.

        Silence from a command declared responsive raises rather than returning
        ``None``: with silence as the device's only error signal, a ``None`` that
        callers must remember to check is exactly the shape that becomes an
        unnoticed wrong answer.
        """
        spec = command_spec(command)
        try:
            self._link.write(command)
        except Adu218LinkTimeout as exc:
            raise ADU218TimeoutError(f"writing {command!r}: {exc}") from exc
        except Adu218LinkError as exc:
            raise ADU218ConnectionError(f"writing {command!r}: {exc}") from exc

        if not spec.responsive:
            return None

        try:
            reply = self._link.read()
        except Adu218LinkTimeout as exc:
            raise ADU218TimeoutError(
                f"{command!r} ({spec.what}) is declared responsive but answered "
                f"nothing. On this device silence is also how an unknown command "
                f"and a bad argument present, so either the command table is "
                f"wrong or the device has stopped answering: {exc}"
            ) from exc
        except Adu218LinkError as exc:
            raise ADU218ProtocolError(f"reading the reply to {command!r}: {exc}") from exc

        if spec.width is not None and len(reply) != spec.width:
            raise ADU218ProtocolError(
                f"{command!r} ({spec.what}) replied {reply!r}, which is "
                f"{len(reply)} characters; {spec.width} were expected. A reply of "
                f"the wrong width means the request and response streams have "
                f"desynchronised, so this value belongs to another command."
            )
        return reply

    def _send_int(self, command: str, *, maximum: int) -> int:
        """A responsive command whose payload is a decimal integer."""
        reply = self._send(command)
        assert reply is not None  # guaranteed: _send raises for a silent responsive
        if not reply.isdigit():
            raise ADU218ProtocolError(
                f"{command!r} replied {reply!r}, which is not a decimal number"
            )
        value = int(reply)
        if value > maximum:
            raise ADU218ProtocolError(
                f"{command!r} replied {value}, above the {maximum} this command can mean"
            )
        return value

    # -- identity -----------------------------------------------------------

    def read_identity(self) -> ADU218Info:
        """Identity from the USB descriptor. No round trip, and cached.

        Cached because it cannot change while the handle is open: the values come
        from the enumeration the kernel already did, and a re-enumeration would
        invalidate the handle itself.
        """
        if self._info is None:
            device = getattr(self._link, "device", None)
            if device is None:
                raise ADU218ConnectionError("identity is unavailable before open()")
            self._info = _info_from_device(device)
        return self._info

    # -- relay reads --------------------------------------------------------

    def relay_state(self, index: int) -> bool:
        """One relay. ``True`` means **energised — the contact is conducting**.

        Polarity is stated rather than left to the method name, because a bool
        whose sense is inferred is exactly the defect shape where the second
        caller reads it backwards. ``True`` matches ``set_relay_state(i, True)``
        and the device's own ``RPKn``=1.
        """
        return self._send_int(f"RPK{_coerce_relay_index(index)}", maximum=1) == 1

    def relay_states(self) -> dict:
        """Every relay in **one** round trip, via ``PK``'s decimal byte.

        Preferred over eight ``RPKn`` reads for more than economy: eight reads
        are eight separate instants, so a state that changes mid-sweep yields a
        mixture that never existed. ``PK`` is one sample of all eight.
        """
        value = self._send_int("PK", maximum=RELAY_MASK_MAX)
        return {index: bool(value & (1 << index)) for index in range(RELAY_COUNT)}

    def relay_mask(self) -> int:
        """The raw ``PK`` byte, bit *n* being relay *n*.

        Exposed because it is what :py:meth:`set_relay_port` takes, so a
        read-modify-write does not have to round-trip through a dict.
        """
        return self._send_int("PK", maximum=RELAY_MASK_MAX)

    # -- relay writes -------------------------------------------------------

    def set_relay_state(self, index: int, on: bool, *, verify: bool = True) -> bool:
        """Energise or de-energise one relay. Returns the **verified** state.

        Returns the read-back rather than ``None`` because a write is
        unacknowledged on this device — ``SKn``/``RKn`` answer nothing, which is
        also how an unknown command answers. A switch this driver cannot confirm
        is a switch it should not claim, so the return value is the confirmation
        and discarding it is the caller's choice to make explicitly.

        ``verify=False`` skips the read-back and returns the *commanded* value.
        It exists for the throughput case, and it is a real downgrade: the
        return then means "we sent it", not "it happened".
        """
        relay = _coerce_relay_index(index)
        if not isinstance(on, bool):
            raise ADU218ValueError(
                f"on must be a bool, got {type(on).__name__} {on!r}. Refusing to "
                f"guess: a truthy 0/1 or a string is how a caller ends up "
                f"energising the opposite of what they meant."
            )
        # The allowlist is checked only on the energising direction, and the
        # asymmetry is deliberate: it exists to prevent an unintended *closed*
        # contact, so extending it to de-energising would make a narrower policy
        # the more dangerous one -- an operator who listed one relay would be
        # unable to open the other seven. :py:meth:`reset_relays` makes the same
        # trade for the same reason, and :py:meth:`set_relay_port` cannot,
        # because ``MKddd`` writes both directions in one indivisible command.
        if on and relay not in self._allowed:
            raise ADU218PolicyError(
                f"relay {relay} is not in allowed_relays "
                f"{sorted(self._allowed)}; nothing was sent. De-energising it "
                f"is always permitted -- the allowlist guards closing contacts."
            )
        self._send(f"{'SK' if on else 'RK'}{relay}")
        if not verify:
            return on
        actual = self.relay_state(relay)
        if actual != on:
            raise ADU218ProtocolError(
                f"relay {relay} was commanded {'energised' if on else 'de-energised'} "
                f"but reads back {'energised' if actual else 'de-energised'}. The "
                f"device accepted the command silently and did not act on it."
            )
        return actual

    def set_relay_port(self, mask: int, *, verify: bool = True) -> int:
        """Write all eight relays at once, as a bitmask. Returns the read-back.

        One ``MKddd`` is a single simultaneous transition, which per-relay writes
        cannot be — they pass through intermediate combinations that may be
        electrically meaningful.

        **"Simultaneous" here means the command is indivisible, not that the
        contacts have been measured to move together.** Eight ``SKn``/``RKn``
        writes are eight USB transfers, so the port demonstrably visits
        ``0b10101000`` on the way to ``0b10101010``; one ``MKddd`` is one
        transfer, so it does not. That is the claim, and it is the one the
        allowlist reasoning below depends on. Contact-to-contact skew *within* a
        single ``MKddd`` is **unmeasured and unclaimed**: the verification here is
        a ``PK`` read-back, which reports the landed state and can say nothing
        about timing. The manual gives no per-relay switching time to compare
        against either — only :py:data:`RELAY_MAX_SWITCH_HZ`, which bounds the
        repetition rate rather than the transition. A caller whose circuit cares
        about skew (a make-before-break sequence, say) must measure it on their
        own bench; nothing in this driver or its tests establishes it.

        **The allowlist is enforced on the whole mask, not on the diff.** A mask
        naming a disallowed relay is refused even when that relay's requested
        value matches its current one, because "no change requested" depends on a
        read that could be stale, and a policy that holds only when the device
        agrees is not a policy.
        """
        value = _coerce_mask(mask)
        forbidden = sorted(
            index
            for index in range(RELAY_COUNT)
            if (value & (1 << index)) and index not in self._allowed
        )
        if forbidden:
            raise ADU218PolicyError(
                f"mask {value:#010b} would energise relays {forbidden}, which are "
                f"not in allowed_relays {sorted(self._allowed)}; nothing was sent"
            )
        self._send(f"MK{value:03d}")
        if not verify:
            return value
        actual = self.relay_mask()
        if actual != value:
            raise ADU218ProtocolError(
                f"relay port was commanded {value:#010b} but reads back "
                f"{actual:#010b}; the device accepted the command and did not act"
            )
        return actual

    def reset_relays(self, *, verify: bool = True) -> int:
        """De-energise every relay — ``MK000``, the safe state.

        Bypasses the allowlist deliberately, and it is the **only** method that
        does. The allowlist exists to stop unintended *energising*; a rule that
        prevented de-energising would make a narrower policy a more dangerous
        one, and would mean the safe state was unreachable on exactly the benches
        most carefully configured.
        """
        self._send("MK000")
        if not verify:
            return 0
        actual = self.relay_mask()
        if actual != 0:
            raise ADU218ProtocolError(
                f"reset_relays() commanded MK000 but the port reads back "
                f"{actual:#010b}; some relays are still energised"
            )
        return actual

    @property
    def allowed_relays(self) -> frozenset:
        """Which relays may be energised. Every relay may always be *de*-energised."""
        return self._allowed

    # -- digital inputs -----------------------------------------------------

    def input_state(self, port: str, index: int) -> bool:
        """One input line. ``True`` means the opto-isolator is conducting.

        ``port`` is ``"A"`` or ``"B"``, ``index`` is **0..3** — four lines per
        port, not eight. ``RPA4`` is silent on hardware.
        """
        letter = _coerce_port(port)
        line = _coerce_input_index(index)
        return self._send_int(f"RP{letter}{line}", maximum=1) == 1

    def input_states(self) -> dict:
        """Both ports, as ``{"A": (l0, l1, l2, l3), "B": (...)}``.

        Two round trips, one per port, because no single command returns both in
        a per-line form — ``PI`` gives both but as a packed decimal byte.

        Note the reordering: ``RPy`` replies **MSB-first**, so its leftmost
        character is line 3 and the tuple below is reversed relative to the
        wire. Indexing the reply string directly is an off-by-three that reads
        correctly for the all-zero case every unwired bench produces.
        """
        states = {}
        for letter in INPUT_PORTS:
            reply = self._send(f"RP{letter}")
            assert reply is not None
            if any(character not in "01" for character in reply):
                raise ADU218ProtocolError(
                    f"RP{letter} replied {reply!r}, which is not four binary digits"
                )
            states[letter] = tuple(character == "1" for character in reversed(reply))
        return states

    def input_port_mask(self, port: str) -> int:
        """One input port's four lines as a nibble via ``Py`` — 0..15.

        The per-port counterpart to :py:meth:`relay_mask`, and the reason it
        exists is narrower than "completeness": ``Py`` is the only input read
        whose reply is **LSB-weighted decimal**, so bit 0 of the returned value
        is line 0 with no reordering. Every other per-port form needs a
        transformation — ``RPy`` is MSB-first text (see :py:meth:`input_states`)
        and ``PI`` packs both ports into one byte. A caller that wants one
        port's bits and wants to trust their positions should use this.

        ``PI`` is still the right call for all eight lines at once; this is not
        a cheaper route to the same answer, it is a *different* answer — one
        port, with the other port's state absent rather than masked off.
        """
        letter = _coerce_port(port)
        return self._send_int(f"P{letter}", maximum=15)

    def input_mask(self) -> int:
        """Both input ports as one byte via ``PI`` — **PORT A is the low nibble**.

        One instant for all eight lines, where :py:meth:`input_states` is two.
        """
        return self._send_int("PI", maximum=255)

    # -- event counters -----------------------------------------------------

    def read_counter(self, index: int) -> int:
        """Read one event counter **without clearing it** — ``REn``.

        Prefer this and difference host-side wherever the value matters. See
        :py:meth:`clear_counter` for why.
        """
        return self._send_int(f"RE{_coerce_counter_index(index)}", maximum=COUNTER_MAX)

    def read_counters(self) -> dict:
        """All eight counters. Eight round trips — there is no aggregate read."""
        return {index: self.read_counter(index) for index in range(COUNTER_COUNT)}

    def clear_counter(self, index: int) -> int:
        """Read **and clear** one counter — ``RCn``. Returns the value cleared.

        ``RCn`` is the only responsive command on this device that mutates state,
        which makes it the only one that **must never be retried**: if the reply
        is lost after the device has already cleared, the count is gone
        permanently and a retry returns 0, indistinguishable from "no events".

        So this method does not retry, and the value is returned rather than
        discarded because discarding it loses data irrecoverably.
        """
        return self._send_int(f"RC{_coerce_counter_index(index)}", maximum=COUNTER_MAX)

    # -- de-bounce ----------------------------------------------------------

    def read_debounce(self) -> int:
        """The de-bounce setting, 0..2.

        This is the device's raw setting number, not a duration. Use
        :py:meth:`read_debounce_ms` if what you want is the filter width —
        the two run in *opposite* directions.
        """
        value = self._send_int("DB", maximum=max(DEBOUNCE_SETTINGS))
        return value

    def read_debounce_ms(self) -> float:
        """The de-bounce filter width in milliseconds: 10.0, 1.0 or 0.1.

        Provided because the setting number is actively misleading on its own:
        **0 is the longest filter (10 ms) and 2 the shortest (100 µs)**, so
        reaching for the biggest number to get the most de-bouncing does the
        opposite. See :py:data:`DEBOUNCE_MS`.
        """
        return DEBOUNCE_MS[self.read_debounce()]

    def set_debounce(self, setting: int) -> int:
        """Set the input de-bounce. Returns the verified read-back.

        ``setting`` is the device's number, not a duration, and **higher means
        a shorter filter**: 0 = 10 ms, 1 = 1 ms (default), 2 = 100 µs. See
        :py:data:`DEBOUNCE_MS`.

        Three settings, not the four Ontrak's web page lists — see
        :py:data:`DEBOUNCE_SETTINGS`.
        """
        value = _coerce_choice(setting, DEBOUNCE_SETTINGS, "debounce setting")
        self._send(f"DB{value}")
        actual = self.read_debounce()
        if actual != value:
            raise ADU218ProtocolError(
                f"de-bounce was set to {value} but reads back {actual}"
            )
        return actual

    # -- watchdog -----------------------------------------------------------

    def read_watchdog(self) -> int:
        """The device's watchdog setting, 0..3.

        **Reading this refeeds the timer.** Every command does, invalid ones
        included, so this cannot be used to monitor an armed watchdog without
        also keeping it alive — see :py:meth:`set_watchdog`. It answers "what is
        the setting now", and after a trip it answers 0.
        """
        return self._send_int("WD", maximum=max(WATCHDOG_SETTINGS))

    @property
    def watchdog_setting(self) -> int:
        """What this driver last commanded. No I/O, so it never refeeds.

        Held because the device's own value is not self-interpreting: ``WD``
        reads 0 both for "timed out" and for "never enabled". This is the other
        half of that comparison, and it is why :py:meth:`open` writes a known
        value at connect — a fresh process would otherwise have nothing to
        compare against.
        """
        return self._watchdog_setting

    def set_watchdog(self, setting: int) -> int:
        """Set **and arm** the hardware watchdog. Returns the setting.

        ``0`` disables; ``1``/``2``/``3`` select 1 s, 10 s and 1 minute. There is
        no separate arm step — ``WDn`` does both — so a nonzero value takes
        effect immediately.

        **This is an interlock, and arming it changes the meaning of every other
        call.** On timeout the device de-energises all relays itself, with no
        benchctrl process, no GPIO and no kernel driver in the decision path:
        a wedged agent, a killed process, an unplugged cable and a panicking
        kernel all produce the same silence, so all de-energise the load. That
        is the point, and it is why this is the interlock rather than a software
        trip hook.

        Three consequences the caller owns:

        1. **Every relay's state now depends on call frequency.** One blocking
           call to another instrument can exceed the interval and drop a relay
           the driver was told to hold. ``WD1`` (1 s, measured) is unusable for a
           general bench; ``WD3`` (1 minute) is the longest available and the
           only setting a run loop can plausibly meet.
        2. **Any command refeeds the timer** — including invalid ones. So a
           health-check loop reading ``PK``, or a dashboard polling device state,
           keeps the deadman fed however wedged the control path is. The feed
           must live on the control path only.
        3. **This driver offers no keep-alive thread, deliberately.** A
           background feeder would hide (1) and guarantee (2): it would keep the
           watchdog fed precisely while the thing it protects against was
           happening, which is the inert-interlock shape — a mechanism that looks
           wired and cannot fire.
        """
        value = _coerce_choice(setting, WATCHDOG_SETTINGS, "watchdog setting")
        self._send(f"WD{value}")
        self._watchdog_setting = value
        if value:
            log.warning(
                "ADU218 %s watchdog ARMED at WD%d (%s s): all relays will "
                "de-energise if no command is sent within that interval, and "
                "ANY command refeeds the timer",
                self._describe(),
                value,
                WATCHDOG_TIMEOUT_S[value],
            )
        return value

    def read_watchdog_tripped(self) -> bool:
        """Whether an armed watchdog has fired since it was armed.

        ``WD`` self-clearing to 0 is the *only* distinguishable trace a timeout
        left, and it is weaker than it looks: 0 means both "timed out" and "never
        enabled". So this compares the device's value against
        :py:attr:`watchdog_setting`, the expectation this driver holds.

        **It can only report a trip that already happened.** Reading refeeds the
        timer, so polling this in a loop keeps the watchdog alive and guarantees
        the answer stays ``False`` — it is for reading *after* a suspected
        silence, not for monitoring during one. A driver restart also loses the
        expectation, which is what :py:meth:`open`'s ``disarm_watchdog`` exists
        to bound.
        """
        if not self._watchdog_setting:
            return False
        if self.read_watchdog() == 0:
            self._watchdog_setting = 0
            return True
        return False


# ---------------------------------------------------------------------------
# Argument coercion
#
# Separate functions per range rather than one shared validator: relays are
# 0..7 and input lines are 0..3, so a single check would accept RPA5 and let
# the device swallow it into silence -- surfacing as a timeout, three layers
# from the bad argument that caused it.
# ---------------------------------------------------------------------------


def _coerce_index(value: object, limit: int, what: str) -> int:
    """Reject ``bool`` before ``int``, and reject non-integers outright.

    ``bool`` is an ``int`` subclass, so ``relay_state(True)`` would silently
    mean relay 1. The DP2031 driver's ``_coerce_channel`` makes the same check
    for the same reason.
    """
    if isinstance(value, bool):
        raise ADU218ValueError(
            f"{what} must be an int, not the bool {value!r}. bool is an int "
            f"subclass, so this would have meant {what} {int(value)}."
        )
    if not isinstance(value, int):
        raise ADU218ValueError(f"{what} must be an int, got {type(value).__name__} {value!r}")
    if not 0 <= value < limit:
        raise ADU218ValueError(
            f"{what} must be 0..{limit - 1}, got {value}. Out-of-range arguments "
            f"are answered with silence by this device, so they are rejected here."
        )
    return value


def _coerce_relay_index(value: object) -> int:
    return _coerce_index(value, RELAY_COUNT, "relay index")


def _coerce_input_index(value: object) -> int:
    return _coerce_index(value, INPUT_LINES_PER_PORT, "input line index")


def _coerce_counter_index(value: object) -> int:
    return _coerce_index(value, COUNTER_COUNT, "counter index")


def _coerce_port(value: object) -> str:
    """Input port letter. Accepts either case; rejects anything else."""
    if not isinstance(value, str):
        raise ADU218ValueError(f"input port must be a str, got {type(value).__name__} {value!r}")
    letter = value.strip().upper()
    if letter not in INPUT_PORTS:
        raise ADU218ValueError(f"input port must be one of {INPUT_PORTS}, got {value!r}")
    return letter


def _coerce_mask(value: object) -> int:
    if isinstance(value, bool):
        raise ADU218ValueError(f"relay mask must be an int, not the bool {value!r}")
    if not isinstance(value, int):
        raise ADU218ValueError(
            f"relay mask must be an int, got {type(value).__name__} {value!r}"
        )
    if not 0 <= value <= RELAY_MASK_MAX:
        raise ADU218ValueError(
            f"relay mask must be 0..{RELAY_MASK_MAX}, got {value}. MK takes a "
            f"three-digit byte; larger values are silently ignored by the device "
            f"and their aliasing behaviour is undocumented."
        )
    return value


def _coerce_choice(value: object, allowed: tuple, what: str) -> int:
    if isinstance(value, bool):
        raise ADU218ValueError(f"{what} must be an int, not the bool {value!r}")
    if not isinstance(value, int):
        raise ADU218ValueError(f"{what} must be an int, got {type(value).__name__} {value!r}")
    if value not in allowed:
        raise ADU218ValueError(f"{what} must be one of {allowed}, got {value}")
    return value


def _info_from_device(device: Adu218Device) -> ADU218Info:
    """Build identity from an enumerated device. No I/O."""
    return ADU218Info(
        model="ADU218",
        serial=getattr(device, "serial", None),
        manufacturer=getattr(device, "manufacturer", None),
        product=getattr(device, "product", None),
        vendor_id=0x0A07,
        product_id=0x00DA,
        bus=getattr(device, "bus", -1),
        device=getattr(device, "device", -1),
        relay_count=RELAY_COUNT,
        input_count=INPUT_COUNT,
    )

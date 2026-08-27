"""A simulated Ontrak ADU218 relay / digital-input interface.

Unlike every other simulator in this package, this one is **not** behind a pty.
The ADU218 is USB HID: its transport is ``USBDEVFS_BULK`` ioctls on two
interrupt endpoints, and there is no byte stream to loop back. Simulating the
ioctl layer would mean asserting that my model of the kernel matches my model of
the kernel, which is why ``tests/test_usbfs_adu218.py`` ships no fake for it
either.

So the seam is one level up, and it is deliberately as thin as the repo allows:
:py:class:`SimulatedAdu218Link` **subclasses the production link** and overrides
:py:meth:`~benchctrl.drivers.ontrak_adu218.usbfs.Adu218UsbfsLink._transfer` —
the single chokepoint through which every ioctl passes — plus the lifecycle
members that would otherwise open a real device node (``open``, ``close``,
``is_open``, and ``__repr__`` so the sim is identifiable in a log). Everything
else stays production code: the mandatory ``0x01`` report framing, the 8-byte
NUL padding, the report-id check that catches a desync, the
``ETIMEDOUT``-to-timeout mapping, and ``drain()``. Only the syscall and the
device node are replaced; the protocol is not.

``test_the_sim_is_the_production_link_with_one_override`` pins that override set
so it cannot quietly grow — the moment it does, these tests stop covering the
shipping code they are trusted to cover.

Fixture provenance
------------------
The canned replies are **generated in the same shapes captured from hardware**
and pinned against ``tests/fixtures/adu218/reads.txt`` by
``test_every_reply_width_matches_the_reads_capture`` in
``tests/test_bench_adu218.py``, which parses the capture *at test time* rather
than trusting a transcription of it. (An earlier version of this docstring cited
``tests/test_sim_adu218.py``, which does not exist — worth naming the test, not
just the file, since an unverifiable provenance citation is the same problem the
pin exists to prevent.) ``sim/qr10x.py``'s docstring records what happens
otherwise: a simulator
built from the same misreading as the driver agrees with it, and the pair passes
every test while both are wrong. Concretely, the widths here (``PK``=3,
``RPKn``=1, ``Py``=2, ``RPy``=4, ``PI``=3, ``REn``=5, ``DB``=1, ``WD``=1) are
the *measured* ones, and the manual gives only examples for four of them.

Three device behaviours this models on purpose, because they are the ones a
driver gets wrong
-----------------------------------------------------------------------------

**1. Silence.** An unknown command, a valid command with an out-of-range
argument, and a write-only command all produce *nothing* — byte-identical on
the wire, no error string, no sentinel. So the sim queues no reply and the
driver's read times out, exactly as on hardware. Critically, the session is
**not** poisoned: the next valid command answers normally (``errors.txt``).

**2. An unread reply outlives its command.** Replies go on a queue, not into a
"last response" slot. A driver that skips a read gets the *previous* command's
answer to its *next* query — a silently wrong value rather than an exception.
That is the failure that invalidated the first hardware framing capture, and a
sim that overwrote a single slot could not reproduce it.

**3. The watchdog runs on an injectable clock, and *any* command refeeds it.**
Including invalid ones. See :py:meth:`SimulatedADU218.advance`.
"""

from __future__ import annotations

import ctypes
import logging
import re
import threading
from typing import Callable, Optional

from benchctrl.drivers.ontrak_adu218.usbfs import (
    EP_IN,
    EP_OUT,
    PACKET_SIZE,
    REPORT_ID,
    Adu218Device,
    Adu218LinkError,
    Adu218LinkTimeout,
    Adu218UsbfsLink,
)

log = logging.getLogger("benchctrl.sim.adu218")

#: Obviously synthetic, so a captured log can never be misattributed to the real
#: unit on the bench (whose serial is ``E02246``).
DEFAULT_SERIAL = "SIM-ADU218-0001"

#: Watchdog intervals in seconds, keyed by setting. ``WD1``'s value was measured
#: by bisection on hardware — (0.90, 1.10] s — so the documented 1 s stands.
WATCHDOG_TIMEOUT_S = {0: None, 1: 1.0, 2: 10.0, 3: 60.0}

_RELAYS = 8
_LINES_PER_PORT = 4
_COUNTERS = 8
_COUNTER_MODULO = 65536


class SimulatedADU218:
    """The device model: relays, inputs, counters, de-bounce, watchdog.

    Holds no transport. Feed it decoded ASCII commands with
    :py:meth:`handle` and it appends replies to :py:attr:`replies`, or appends
    nothing where the real device is silent.

    Args:
        relays: initial relay bitmask. Defaults to 0 (all de-energised).
            Settable because the real device's power-on state is *undocumented*
            and USB suspend holds outputs in their last state, so a driver must
            not assume 0 — a test needs to construct the case it must not
            assume away.
        debounce: initial de-bounce setting. Defaults to **1**, which is what
            the hardware reported out of the box (``reads.txt``). Not 0: a sim
            that defaults every setting to zero cannot catch a driver that
            reports a hardcoded 0 instead of reading.
        clock: monotonic seconds source for the watchdog. Defaults to a
            **manual** clock frozen at 0 that only moves via
            :py:meth:`advance`, so watchdog tests are deterministic rather than
            wall-clock races. Pass ``time.monotonic`` for real time.
    """

    def __init__(
        self,
        *,
        relays: int = 0,
        debounce: int = 1,
        watchdog: int = 0,
        serial: str = DEFAULT_SERIAL,
        clock: Optional[Callable[[], float]] = None,
    ) -> None:
        if not 0 <= relays <= 255:
            raise ValueError(f"relays must be a 0..255 bitmask, got {relays}")
        if debounce not in (0, 1, 2):
            raise ValueError(f"debounce must be 0, 1 or 2, got {debounce}")
        if watchdog not in WATCHDOG_TIMEOUT_S:
            raise ValueError(f"watchdog must be 0..3, got {watchdog}")

        self._lock = threading.RLock()
        self.serial = serial
        self.relays = relays
        self.inputs = 0  # bit n: PORT A lines 0-3 low, PORT B lines 0-3 high
        self.counters = [0] * _COUNTERS
        self.debounce = debounce
        self.watchdog = watchdog

        #: Every command the device received, decoded, in order — including the
        #: ones it answered with silence. This is how a test asserts the exact
        #: bytes a driver emitted, which is the only way to prove a negative
        #: like "no method can ever send an unlisted command".
        self.command_log: list[str] = []

        #: Replies waiting to be read, oldest first. A queue, not a slot — see
        #: the module docstring.
        self.replies: list[str] = []

        self._manual_time = 0.0
        # Flag rather than comparing ``self._clock is self._manual_clock``:
        # each attribute access on a bound method builds a *new* object, so
        # that identity test is always False and advance() would refuse to run
        # even on the default clock.
        self._manual = clock is None
        self._clock = clock if clock is not None else self._manual_clock
        self._fed_at = self._clock()
        #: Set when an armed watchdog expired. Latched for tests; the device
        #: itself leaves no trace beyond ``WD`` reading 0.
        self.watchdog_trips = 0

    # -- the injectable clock ----------------------------------------------

    def _manual_clock(self) -> float:
        return self._manual_time

    def advance(self, seconds: float) -> None:
        """Move the manual clock forward, then settle the watchdog.

        This is how the watchdog is tested without sleeping. Note the ordering
        that matters on real hardware and is reproduced here: the timer is
        refed by **any** command, valid or not, so a test that polls state
        between advances will find the watchdog still armed no matter how much
        total time passed. That is not a sim artefact — it is why the driver
        offers no keep-alive thread.
        """
        if not self._manual:
            raise RuntimeError(
                "advance() only works with the default manual clock; this "
                "simulator was constructed with an external clock"
            )
        with self._lock:
            self._manual_time += seconds
            self._settle_watchdog()

    def _settle_watchdog(self) -> None:
        """De-energise every relay if an armed watchdog has expired."""
        if not self.watchdog:
            return
        timeout = WATCHDOG_TIMEOUT_S[self.watchdog]
        assert timeout is not None
        if self._clock() - self._fed_at <= timeout:
            return
        log.info(
            "sim ADU218 watchdog WD%d expired after %.3f s: de-energising all relays",
            self.watchdog,
            timeout,
        )
        self.relays = 0
        # The device self-clears the setting, which is the *only* trace a
        # timeout leaves -- and it is indistinguishable from "never enabled".
        self.watchdog = 0
        self.watchdog_trips += 1

    # -- inputs, driven by the test rather than by the wire ----------------

    def set_input(self, port: str, line: int, asserted: bool) -> None:
        """Assert or release one input line, counting the rising edge.

        The digital inputs are not writable over USB — a test is standing in
        for whatever is wired to the terminal block. A 0 -> 1 transition bumps
        that line's event counter, which is the only way ``REn``/``RCn`` ever
        become non-zero and therefore the only way a counter test is worth
        anything.
        """
        letter = port.strip().upper()
        if letter not in ("A", "B"):
            raise ValueError(f"port must be 'A' or 'B', got {port!r}")
        if not 0 <= line < _LINES_PER_PORT:
            raise ValueError(f"line must be 0..{_LINES_PER_PORT - 1}, got {line}")
        index = line + (0 if letter == "A" else _LINES_PER_PORT)
        with self._lock:
            was = bool(self.inputs & (1 << index))
            if asserted:
                self.inputs |= 1 << index
            else:
                self.inputs &= ~(1 << index)
            if asserted and not was:
                self.counters[index] = (self.counters[index] + 1) % _COUNTER_MODULO

    def relay_state(self, index: int) -> bool:
        """Whether relay ``index`` is energised. For assertions, not the wire."""
        return bool(self.relays & (1 << index))

    # -- the command engine ------------------------------------------------

    def handle(self, command: str) -> None:
        """Process one ASCII command, queueing a reply only if one is due.

        Order is load-bearing and matches hardware: the watchdog is settled
        **before** the command is interpreted (so a command arriving after the
        deadline finds the relays already dropped), and the timer is refed
        **after** (so that command extends the next interval, whether or not it
        was understood).
        """
        with self._lock:
            self._settle_watchdog()
            self.command_log.append(command)
            try:
                reply = self._dispatch(command.upper())
            finally:
                # Refeed unconditionally: an unknown command refeeds the timer
                # on hardware, so a bug that spams garbage keeps the deadman
                # alive. Modelling this is the point.
                self._fed_at = self._clock()
            if reply is not None:
                self.replies.append(reply)

    def _dispatch(self, command: str) -> Optional[str]:
        """Return the reply payload, or ``None`` where the device is silent."""
        # -- relays ---------------------------------------------------------
        if command == "PK":
            return f"{self.relays:03d}"
        match = re.fullmatch(r"RPK([0-9])", command)
        if match:
            index = int(match.group(1))
            if index >= _RELAYS:
                return None  # out of range == silence
            return "1" if self.relays & (1 << index) else "0"
        match = re.fullmatch(r"(SK|RK)([0-9])", command)
        if match:
            index = int(match.group(2))
            if index >= _RELAYS:
                return None
            if match.group(1) == "SK":
                self.relays |= 1 << index
            else:
                self.relays &= ~(1 << index)
            return None  # write-only -- RKn starts with R and still answers nothing
        match = re.fullmatch(r"MK([0-9]{3})", command)
        if match:
            value = int(match.group(1))
            if value > 255:
                # Exactly three digits cannot exceed 999; 256..999 is the real
                # device's undocumented territory, so the sim is silent rather
                # than inventing an aliasing rule the driver might come to rely
                # on. The driver rejects these host-side and never sends them.
                return None
            self.relays = value
            return None

        # -- digital inputs -------------------------------------------------
        match = re.fullmatch(r"P([AB])", command)
        if match:
            return f"{self._port_nibble(match.group(1)):02d}"
        match = re.fullmatch(r"RP([AB])", command)
        if match:
            nibble = self._port_nibble(match.group(1))
            # MSB first: the leftmost character is line 3. A driver that
            # indexes this string directly is off by three, and reads
            # correctly for the all-zero case every unwired bench produces.
            return f"{nibble:04b}"
        match = re.fullmatch(r"RP([AB])([0-9])", command)
        if match:
            line = int(match.group(2))
            if line >= _LINES_PER_PORT:
                return None  # RPA4 is silent on hardware, RPA3 answers
            nibble = self._port_nibble(match.group(1))
            return "1" if nibble & (1 << line) else "0"
        if command == "PI":
            return f"{self.inputs:03d}"
        if command == "RI":
            # The manual's summary table calls the both-ports read RI and its
            # description calls it PI. Only PI answers. Modelled so a driver
            # that follows the summary table fails here rather than on a bench.
            return None

        # -- counters -------------------------------------------------------
        match = re.fullmatch(r"(RE|RC)([0-9])", command)
        if match:
            index = int(match.group(2))
            if index >= _COUNTERS:
                return None
            value = self.counters[index]
            if match.group(1) == "RC":
                # Read AND clear. The only responsive command that mutates
                # state, hence the only one a retry destroys data on.
                self.counters[index] = 0
            return f"{value:05d}"

        # -- settings -------------------------------------------------------
        if command == "DB":
            return f"{self.debounce:d}"
        match = re.fullmatch(r"DB([0-9])", command)
        if match:
            value = int(match.group(1))
            if value > 2:
                return None  # three settings, not the four the web page lists
            self.debounce = value
            return None
        if command == "WD":
            return f"{self.watchdog:d}"
        match = re.fullmatch(r"WD([0-9])", command)
        if match:
            value = int(match.group(1))
            if value > 3:
                return None
            self.watchdog = value  # sets AND arms; no separate arm step
            return None

        return None  # unknown command: silence, and the session survives it

    def _port_nibble(self, letter: str) -> int:
        shift = 0 if letter == "A" else _LINES_PER_PORT
        return (self.inputs >> shift) & 0x0F


class SimulatedAdu218Link(Adu218UsbfsLink):
    """The production USBDEVFS link with the ioctl replaced by a device model.

    Subclassing rather than reimplementing is the whole point: framing, the
    ``0x01`` report id, NUL padding and stripping, the report-id desync check,
    the timeout mapping and ``drain()`` are all the shipping code paths. If any
    of them regress, sim-mode tests fail — which would not be true of a
    hand-written stand-in offering the same four methods.

    The device model is available as :py:attr:`device_model` so a test can
    assert relay state, inspect ``command_log``, drive inputs, and advance the
    watchdog clock without going through the wire.
    """

    def __init__(
        self,
        *,
        device_model: Optional[SimulatedADU218] = None,
        serial: Optional[str] = None,
        path: Optional[str] = None,
        timeout_ms: int = 200,
        **model_kwargs: object,
    ) -> None:
        super().__init__(serial=serial, path=path, timeout_ms=timeout_ms)
        self.device_model = device_model or SimulatedADU218(
            serial=serial or DEFAULT_SERIAL,
            **model_kwargs,  # type: ignore[arg-type]
        )
        self._sim_open = False

    # -- lifecycle: no enumeration, no node, no claim ----------------------

    def open(self) -> None:
        """Attach to the model. Still drains, because ``open()`` still should.

        Skips :py:func:`find_device`, ``os.open`` and ``CLAIMINTERFACE`` — there
        is no ``/dev/bus/usb`` node — but keeps the drain, so the sim exercises
        the same startup sequence and a queued reply left by a previous test is
        cleared the same way.
        """
        if self._sim_open:
            return
        self._sim_open = True
        self._device = Adu218Device(
            path=f"<sim>/{self.device_model.serial}",
            bus=1,
            device=0,
            serial=self.device_model.serial,
            product="ADU218 USB Relay I/O Interface",
            manufacturer="www.ontrak.net",
        )
        drained = self.drain()
        if drained:  # pragma: no cover - only when a test pre-queues replies
            log.warning("sim drained %d stale replies on open", drained)

    def close(self) -> None:
        """Detach. Leaves the model's relays exactly where they are.

        Matching the real link, and for the same reason: a teardown that
        silently dropped contacts would make ``with`` a bench event. A test can
        therefore assert that closing the driver did *not* de-energise anything.
        """
        self._sim_open = False

    @property
    def is_open(self) -> bool:
        return self._sim_open

    # -- the only overridden syscall ---------------------------------------

    def _transfer(self, endpoint: int, buffer: ctypes.Array, timeout_ms: int) -> int:
        if not self._sim_open:
            raise Adu218LinkError("link is not open")

        raw = bytes(bytearray(buffer))

        if endpoint == EP_OUT:
            if raw[0] != REPORT_ID:
                # The device ignores anything not prefixed 0x01 -- specifically
                # 0x01, not "any non-ASCII lead byte": bare ASCII, 0x00 and 0x02
                # were all measured silent (framing.txt). A sim that accepted
                # them would let a driver ship with the wrong prefix.
                log.debug("sim ignoring packet with report id %#04x", raw[0])
                return len(buffer)
            command = raw[1:].split(b"\x00", 1)[0].decode("ascii", errors="replace")
            if command:
                self.device_model.handle(command)
            return len(buffer)

        if endpoint == EP_IN:
            if not self.device_model.replies:
                raise Adu218LinkTimeout(
                    f"no reply on EP {endpoint:#04x} within {timeout_ms} ms"
                )
            payload = self.device_model.replies.pop(0).encode("ascii")
            packet = bytearray(PACKET_SIZE)
            packet[0] = REPORT_ID
            packet[1 : 1 + len(payload)] = payload
            for index in range(PACKET_SIZE):
                buffer[index] = packet[index]
            # Real hardware always returns a full wMaxPacketSize=8 packet, with
            # the payload NUL-padded; the production read() strips at the NUL.
            return PACKET_SIZE

        raise Adu218LinkError(  # pragma: no cover - the driver uses two endpoints
            f"sim has no endpoint {endpoint:#04x}"
        )

    def __repr__(self) -> str:
        return (
            f"SimulatedAdu218Link(serial={self.device_model.serial!r}, "
            f"open={self.is_open}, relays={self.device_model.relays:#010b})"
        )

    # -- SimDevice-shaped conveniences ------------------------------------
    #
    # This is not a SimDevice: there is no pty, no loopback and no tick thread,
    # because there is no byte stream. But sim/factories.py binds a lifetime by
    # calling close(), and _bind_lifetime expects that method on the sim object
    # too, so the name is kept aligned rather than inventing a second verb.

    def start(self) -> None:
        """Present the SimDevice lifecycle. Nothing runs in the background."""
        self.open()

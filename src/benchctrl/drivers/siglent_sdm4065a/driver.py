"""Siglent SDM4065A 6½-digit bench digital multimeter driver.

The SDM4000A series (SDM4045A / SDM4055A / SDM4065A) is a USB-TMC + LAN
SCPI-controlled bench DMM. This driver targets the **SDM4065A**
specifically, which matters more than it sounds: the remote manual
documents the whole family and the models genuinely differ, so a
constant copied from the wrong column is a silent measurement bug.
Every model-split value below records the SDM4065A figure *and* the
SDM4055A one it was chosen over.

Like the Rigol drivers, this speaks **VISA** (via pyvisa) over USB-TMC.
Any VISA backend works; ``pyvisa-py`` + ``pyusb`` is the interesting one
because it implements USB-TMC entirely in userspace over libusb, so this
driver works on boards whose kernel has no ``usbtmc`` module (the
Arduino Uno Q, for one).

Typical use::

    from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A

    with SiglentSDM4065A.open() as dmm:      # auto-discover by VID/PID
        print(dmm.info())
        print(dmm.measure_resistance(range_=200))     # 2-wire, 200 Ω range
        print(dmm.measure_dc_voltage())

Measuring low resistance
------------------------
For 2-wire resistance the datasheet's accuracy specs assume you have
taken a **null** (Siglent calls it "Ref") first; without one, add
**0.2 Ω** of lead/contact error (datasheet note [6]). At 100 Ω that is
0.2%, which swamps milliohm-level detail. :py:meth:`null_now` exists for
exactly this: short the leads, call it, then measure. 4-wire
(:py:meth:`measure_resistance_4wire`) avoids the issue entirely but
needs sense leads wired.

This driver measures; it sources nothing and has no output to arm, so
there is no safe-stop concern. Input protection is the operator's job:
respect the terminal ratings printed on the instrument.
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from typing import Optional, Union

log = logging.getLogger("benchctrl.drivers.siglent_sdm4065a.driver")

#: Siglent's USB-TMC VID, and the SDM4065A's PID. Confirmed against the
#: real instrument's descriptor (``f4ec:1220``), not just the manual.
SIGLENT_USB_VID = 0xF4EC
SDM4065A_USB_PID = 0x1220

#: Returned by every measurement query when the input exceeds the selected
#: manual range (remote manual §5.5, §5.7 and elsewhere: "displays the word
#: Overload on the front panel and returns 9.9E37"). It is a sentinel, not a
#: reading — 9.9e37 volts is not a number any caller should do arithmetic on,
#: so the driver raises instead of returning it.
OVERLOAD_SENTINEL = 9.9e37

#: Anything at or above this is the sentinel. Compared with a margin rather
#: than ``==`` because the value survives a float round-trip through the
#: instrument's ASCII formatting, and an exact comparison is fragile.
_OVERLOAD_THRESHOLD = 9.0e37

#: SCPI node for autozero — ``ZERO:AUTO``, **not** the ``AZ`` of §7.4.7.
#:
#: The manual documents ``[SENSe:]{RESistance|FRESistance}:AZ[:STATe]``. That
#: mnemonic does not exist on this instrument. Bench-measured on firmware
#: 0.0.0.20: every ``AZ`` spelling — ``RESistance:AZ``, ``:AZ:STATe``,
#: ``SENSe:`` prefixed, ``AZERo`` — is rejected with ``-113,"Undefined
#: header"``, for reads *and* writes, on all four functions. ``ZERO:AUTO``
#: is accepted and round-trips correctly on all four.
#:
#: This matters beyond a spelling fix. A *query* of a nonexistent header gets
#: no reply, so the read times out; the aborted USB-TMC transfer then strands
#: the bulk endpoints (every later transfer fails with ``Errno 110`` while
#: endpoint-0 control transfers keep working), and neither
#: :py:meth:`SiglentSDM4065A.clear_device_buffers` nor a libusb port reset
#: recovers it — it takes a front-panel power cycle. So following the manual
#: here costs physical access to the bench. Hence a named constant: this is
#: the one place a well-meaning reader might "correct" the code back to what
#: §7.4.7 says.
AUTOZERO_NODE = "ZERO:AUTO"


# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class SDM4065AError(RuntimeError):
    """Base class for SDM4065A driver errors."""


class SDM4065AConnectionError(SDM4065AError):
    """Couldn't open the VISA resource, or lost the connection mid-session."""


class SDM4065ACommandError(SDM4065AError):
    """Device reported a non-zero error from ``SYSTem:ERRor?``.

    ``code`` is the SCPI error number; ``message`` is the instrument's own
    human-readable string.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"SDM4065A SCPI error {code}: {message}")


class SDM4065AValueError(SDM4065AError, ValueError):
    """Client-side range / type check failed before anything was sent."""


class SDM4065ATimeoutError(SDM4065AError, TimeoutError):
    """Device didn't answer a query within the VISA timeout."""


class SDM4065AOverloadError(SDM4065AError):
    """The input exceeded the selected range; the reading is meaningless.

    Carries the function and range that were active so the caller knows what
    to widen. Raised rather than returned because ``9.9e37`` propagating into
    a calculation produces plausible-looking nonsense downstream.
    """

    def __init__(self, function: str, range_: Optional[Union[float, str]] = None):
        self.function = function
        self.range = range_
        where = f" on range {range_}" if range_ is not None else ""
        super().__init__(
            f"{function} input overloaded{where} — the instrument returned its "
            f"overload sentinel (9.9E37). Widen the range or enable autorange."
        )


# ---------------------------------------------------------------------------
# Identity
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class SDM4065AInfo:
    """Identity read from ``*IDN?``.

    Attributes:
        manufacturer: e.g. ``"Siglent Technologies"``.
        model: e.g. ``"SDM4065A"``.
        serial: e.g. ``"SDM4XCAX7R1234"``.
        firmware: e.g. ``"1.01.01.15"``.
        resource: the VISA resource string this device is bound to.
    """

    manufacturer: str
    model: str
    serial: str
    firmware: str
    resource: str

    def to_dict(self) -> dict:
        return {
            "manufacturer": self.manufacturer,
            "model": self.model,
            "serial": self.serial,
            "firmware": self.firmware,
            "resource": self.resource,
        }


# ---------------------------------------------------------------------------
# Model-specific parameter tables
# ---------------------------------------------------------------------------

#: Resistance ranges, in ohms (remote manual §3.6, §7.4.5).
#:
#: SDM4065A: {200, 2k, 20k, 200k, **1M**, 10M, 100M}
#: SDM4055A: {200, 2k, 20k, 200k, **2M**, 10M, 100M}   <- NOT this model
#:
#: The 1M-vs-2M split is the trap: sending 2000000 here would be accepted by
#: a laxer driver and then quantised by the instrument to something else.
#:
#: §7.4.5 also claims the default is 2 kΩ. It is not the reset state — see
#: :py:data:`DEFAULT_RESISTANCE_RANGE` for what was actually measured.
RESISTANCE_RANGES: tuple[float, ...] = (
    200.0,
    2_000.0,
    20_000.0,
    200_000.0,
    1_000_000.0,
    10_000_000.0,
    100_000_000.0,
)

#: What a bare ``CONFigure:RESistance`` (and ``*RST``) actually leaves behind:
#: **autorange on**, with ``RESistance:RANGe?`` reporting 200 Ω.
#:
#: Bench-measured on firmware 0.0.0.20, because the manual is not usable here:
#: after ``*RST`` or a bare ``CONFigure:RESistance``, ``RANGe:AUTO?`` answers
#: ``1`` and ``RANGe?`` answers ``+2.00000000E+02``. An explicit
#: ``CONFigure:RESistance 200`` turns autorange *off* and pins 200 Ω.
#:
#: So ``RANGe?`` alone cannot tell you how the instrument will behave — with
#: autorange on it reports the range currently selected, which moves with the
#: input. That is why the driver reads :py:meth:`SiglentSDM4065A.get_autorange`
#: alongside it, and why accuracy work should pin the range explicitly rather
#: than trust a post-``CONFigure`` readback: the number looks identical in both
#: states, but only one of them is stable.
#:
#: Pin it with a *numeric* argument specifically. Bench-measured, the ``DEF``
#: forms disagree with each other: ``RESistance:RANGe DEF`` selects 2 kΩ and
#: turns autorange off (as §7.4.5's note promises), while
#: ``CONFigure:RESistance DEF`` selects 2 kΩ and leaves autorange **on**.
#: Reported as bug 4; the driver never sends ``DEF``.
DEFAULT_RESISTANCE_RANGE = 200.0

#: Whether autorange is on after ``*RST`` / a bare ``CONFigure`` — it is.
#: Named alongside the range because the pair is the whole fact; the range
#: value on its own is ambiguous (see above).
DEFAULT_RESISTANCE_AUTORANGE = True

#: DC voltage ranges, in volts (remote manual §5.7). The 1000 V range is
#: DC-only; AC tops out at 750 V.
DC_VOLTAGE_RANGES: tuple[float, ...] = (0.2, 2.0, 20.0, 200.0, 1000.0)
AC_VOLTAGE_RANGES: tuple[float, ...] = (0.2, 2.0, 20.0, 200.0, 750.0)

#: Integration time in power-line cycles (remote manual §7.4.1).
#:
#: SDM4065A: {100, 10, 1, 0.1, 0.01, 0.001}, default 10
#: SDM4055A: {10, 1, 0.01} mapped to a Slow/Medium/Fast menu   <- NOT this model
#:
#: A discrete set, so an off-list value is rejected client-side: the
#: instrument would otherwise coerce silently and the caller would believe it
#: had asked for an integration time it never got — and NPLC is exactly what
#: the datasheet's noise adder is indexed on.
NPLC_VALUES: tuple[float, ...] = (100.0, 10.0, 1.0, 0.1, 0.01, 0.001)

#: USBTMC class-request codes (USBTMC spec Table 15) used by
#: :py:meth:`SiglentSDM4065A.clear_device_buffers`. Hard-coded rather than
#: imported from pyvisa-py, which does not export them.
_TMC_INITIATE_CLEAR = 5
_TMC_CHECK_CLEAR_STATUS = 6
_TMC_STATUS_SUCCESS = 1
DEFAULT_NPLC = 10.0

#: ``[SENSe:]FUNCtion`` strings, mapped from friendly names to the SCPI form
#: (remote manual §7.1). ``FUNC?`` answers with the *short* form in quotation
#: marks, e.g. ``"CURR:AC"`` — see :py:meth:`SiglentSDM4065A.get_function`.
_FUNCTION_MAP = {
    "dcv": "VOLT:DC",
    "voltage:dc": "VOLT:DC",
    "volt:dc": "VOLT:DC",
    "acv": "VOLT:AC",
    "voltage:ac": "VOLT:AC",
    "volt:ac": "VOLT:AC",
    "dci": "CURR:DC",
    "current:dc": "CURR:DC",
    "curr:dc": "CURR:DC",
    "aci": "CURR:AC",
    "current:ac": "CURR:AC",
    "curr:ac": "CURR:AC",
    "resistance": "RES",
    "res": "RES",
    "2w": "RES",
    "fresistance": "FRES",
    "fres": "FRES",
    "4w": "FRES",
    "capacitance": "CAP",
    "cap": "CAP",
    "frequency": "FREQ",
    "freq": "FREQ",
    "period": "PER",
    "per": "PER",
    "continuity": "CONT",
    "cont": "CONT",
    "diode": "DIOD",
    "diod": "DIOD",
    "temperature": "TEMP",
    "temp": "TEMP",
}


def _format_range(range_: Optional[Union[float, str]]) -> Optional[str]:
    """Render a range argument for a SCPI command.

    ``None`` means "send no argument at all" (the instrument keeps its current
    setting), which is different from ``"AUTO"`` (switch autoranging on).
    """
    if range_ is None:
        return None
    if isinstance(range_, str):
        token = range_.strip().upper()
        if token not in ("AUTO", "MIN", "MAX", "DEF"):
            raise SDM4065AValueError(
                f"range must be a number or one of AUTO/MIN/MAX/DEF, "
                f"got {range_!r}"
            )
        return token
    # Numbers go out in scientific notation: the instrument parses plain
    # decimals too, but 1e8 as "100000000" is easy to miscount by a digit.
    return f"{float(range_):G}"


def _validate_range(
    range_: Optional[Union[float, str]],
    allowed: tuple[float, ...],
    what: str,
) -> None:
    """Reject a numeric range that isn't one this model offers.

    Only exact members are accepted. Rounding up to the next range would be a
    friendlier-looking API and a worse one: the ``% of range`` term in the
    accuracy spec is computed on full scale, so a silently widened range
    quietly degrades the accuracy the caller is relying on.
    """
    if range_ is None or isinstance(range_, str):
        return
    value = float(range_)
    if value not in allowed:
        pretty = ", ".join(f"{v:G}" for v in allowed)
        raise SDM4065AValueError(
            f"{value:G} is not an SDM4065A {what} range. Valid: {pretty} "
            f"(or AUTO). Note the SDM4055A's ranges differ — check the model."
        )


class SiglentSDM4065A:
    """A Siglent SDM4065A bench DMM over VISA / USB-TMC.

    Open with :py:meth:`open`, which auto-discovers by VID/PID, or pass an
    explicit VISA resource string to pick a specific unit.
    """

    #: Default per-query timeout (ms). Generous on purpose: one reading at
    #: 100 PLC takes ~2 s at 50 Hz mains, and ``SAMPle:COUNt`` multiplies
    #: that. See :py:meth:`reading_timeout_ms`.
    DEFAULT_TIMEOUT_MS = 10_000

    def __init__(
        self,
        instrument,  # pyvisa Resource — typed loosely so pyvisa stays optional
        *,
        resource_string: str,
        owns_resource_manager: bool = False,
        resource_manager=None,
    ):
        self._inst = instrument
        self._resource = resource_string
        # Retained for API compatibility, but no longer gates anything: a
        # ResourceManager is a process-wide singleton, so no driver can own one.
        # See :py:meth:`close`.
        self._owns_rm = owns_resource_manager
        self._rm = resource_manager
        self._info: Optional[SDM4065AInfo] = None
        self._closed = False
        # Tracked so the VISA timeout can be resized whenever either factor
        # changes — see :py:meth:`_resize_timeout`. Seeded with the
        # instrument's own post-``*RST`` defaults (§7.4.1) rather than read
        # back, so construction stays free of I/O.
        self._nplc = 10.0
        self._samples = 1
        self._base_timeout_ms = self.DEFAULT_TIMEOUT_MS
        # Shadow autozero state, because the readback query wedges this
        # firmware — see :py:meth:`get_autozero`. Seeded with the documented
        # post-``*RST`` defaults: OFF for resistance (§7.4.7), ON for DC
        # voltage and current (§7.6.9, §7.2.9).
        self._autozero = {"RES": False, "FRES": False, "VOLT": True, "CURR": True}

    # ------------------------------------------------------------------
    # Construction / teardown
    # ------------------------------------------------------------------

    @classmethod
    def open(
        cls,
        resource: Optional[str] = None,
        *,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> "SiglentSDM4065A":
        """Open a VISA session to an SDM4065A.

        With ``resource=None``, scans USB resources for the Siglent SDM4065A
        VID/PID. Pass a resource string (e.g.
        ``"USB0::0xF4EC::0x1220::SDM4XCAX7R1234::INSTR"`` or
        ``"TCPIP::192.168.1.7::INSTR"``) to target one device.
        """
        try:
            import pyvisa
        except ImportError as e:
            raise SDM4065AConnectionError(
                "pyvisa is required for the SDM4065A driver — install with "
                "`pip install benchctrl[bench-visa]`. On a board with no pip, "
                "unzip the pyvisa and pyvisa-py wheels next to benchctrl; "
                "pyvisa-py speaks USB-TMC over libusb, so no kernel usbtmc "
                "module is needed."
            ) from e

        try:
            rm = pyvisa.ResourceManager()
        except Exception as e:
            raise SDM4065AConnectionError(
                f"could not initialize VISA resource manager: {e}"
            ) from e

        if resource is None:
            resource = _autodiscover(rm)

        try:
            inst = rm.open_resource(resource)
        except Exception as e:
            raise SDM4065AConnectionError(
                f"could not open VISA resource {resource!r}: {e}"
            ) from e

        inst.timeout = timeout_ms
        inst.read_termination = read_termination
        inst.write_termination = write_termination

        dmm = cls(
            inst,
            resource_string=resource,
            owns_resource_manager=True,
            resource_manager=rm,
        )
        dmm._base_timeout_ms = timeout_ms
        return dmm

    def close(self) -> None:
        """Release *this instrument's* VISA session. Safe to call more than once.

        Deliberately does not close the ResourceManager, even when
        ``owns_resource_manager`` was set. ``pyvisa.ResourceManager()`` is a
        **singleton**: the manager this driver built in :py:meth:`connect` is the
        same underlying object as the agent's and every other driver's, so there
        is no such thing as a manager this driver owns. Closing it invalidates
        the shared session for everyone — bench-verified on the Uno Q, where
        closing the DMM made ``list_resources()`` raise ``InvalidSession`` for the
        whole agent, which ``discovery.scan_visa`` reports as an empty bus, so the
        HDMI dashboard showed the supply, load and DMM as NOT FOUND while all
        three were plugged in and working.

        Releasing ``self._inst`` is what frees the USB endpoint; the manager holds
        no per-instrument resource worth reclaiming. The process exiting is what
        closes the manager, plus the agent's own deliberate shutdown path.
        """
        if self._closed:
            return
        try:
            self._inst.close()
        except Exception:
            log.debug("error closing VISA instrument", exc_info=True)
        self._closed = True

    @property
    def is_connected(self) -> bool:
        """Whether the session is still open (the ``session.resolve`` contract)."""
        return not self._closed

    def __enter__(self) -> "SiglentSDM4065A":
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Nothing to disarm: a DMM sources nothing. Unlike the load and the
        # supply, there is no output to switch off on the way out.
        self.close()

    def __repr__(self) -> str:
        state = "open" if not self._closed else "closed"
        return f"<SiglentSDM4065A {self._resource} {state}>"

    # ------------------------------------------------------------------
    # Low-level transport
    # ------------------------------------------------------------------

    def write(self, command: str) -> None:
        """Send a SCPI command that expects no response."""
        if self._closed:
            raise SDM4065AConnectionError("instrument is closed")
        try:
            self._inst.write(command)
        except Exception as e:
            raise SDM4065AConnectionError(f"write({command!r}) failed: {e}") from e

    def query(self, command: str) -> str:
        """Send a SCPI query and return the trimmed response."""
        if self._closed:
            raise SDM4065AConnectionError("instrument is closed")
        try:
            raw = self._inst.query(command)
        except Exception as e:
            if "timeout" in str(e).lower() or "VI_ERROR_TMO" in str(e):
                raise SDM4065ATimeoutError(
                    f"query({command!r}) timed out — a long integration time "
                    f"(high NPLC) or a large SAMPle:COUNt needs a longer "
                    f"timeout_ms than the current setting"
                ) from e
            raise SDM4065AConnectionError(f"query({command!r}) failed: {e}") from e
        return raw.strip()

    def query_float(self, command: str) -> float:
        """Query and parse one float."""
        s = self.query(command)
        try:
            return float(s)
        except ValueError as e:
            raise SDM4065AError(
                f"expected a number from {command!r}, got {s!r}"
            ) from e

    def query_floats(self, command: str) -> list[float]:
        """Query and parse a comma-separated reading list.

        ``READ?`` with ``SAMPle:COUNt > 1`` answers with several values on one
        line (remote manual §3.6's example returns
        ``+6.71881065E+01, +6.83543086E+01``), so splitting is not optional.
        """
        s = self.query(command)
        out = []
        for piece in s.split(","):
            piece = piece.strip()
            if not piece:
                continue
            try:
                out.append(float(piece))
            except ValueError as e:
                raise SDM4065AError(
                    f"expected numbers from {command!r}, got {s!r}"
                ) from e
        if not out:
            raise SDM4065AError(f"no readings in response to {command!r}: {s!r}")
        return out

    # ------------------------------------------------------------------
    # Identity and housekeeping
    # ------------------------------------------------------------------

    def info(self) -> SDM4065AInfo:
        """Read and parse ``*IDN?``. Cached after the first call."""
        if self._info is None:
            raw = self.query("*IDN?")
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 4:
                raise SDM4065AError(f"unexpected *IDN? response: {raw!r}")
            self._info = SDM4065AInfo(
                manufacturer=parts[0],
                model=parts[1],
                serial=parts[2],
                firmware=parts[3],
                resource=self._resource,
            )
        return self._info

    def reset(self) -> None:
        """``*RST`` — restore factory defaults.

        Resets the measurement function to DC voltage and NPLC to 10
        (manual §7.1, §7.4.1), so any configuration must follow, not precede.
        """
        self.write("*RST")
        self._nplc = 10.0
        self._samples = 1
        self._resize_timeout()
        self._autozero = {"RES": False, "FRES": False, "VOLT": True, "CURR": True}

    def clear_status(self) -> None:
        """``*CLS`` — clear the status registers, then drain the error queue.

        The ``*CLS`` write alone is not enough on this instrument. IEEE 488.2
        requires ``*CLS`` to empty the error queue; firmware 0.0.0.20 does not.
        Bench-measured: after queueing two ``-113`` errors, both are still
        there after ``*CLS``, still there after ``*RST``, and one even survives
        closing and reopening the VISA session. The only thing that removes an
        entry is reading it.

        Left undrained the queue reaches its depth and then answers ``-350
        "Queue overflow"`` to everything, so a later check reports a stale
        error from an unrelated command — which reads as the instrument
        rejecting a command it actually accepted. So this reads entries out
        until the queue answers "no error".

        Bounded rather than looped-until-clean: a queue that never empties
        would otherwise hang here, and this method is called in ``finally``
        blocks where hanging is the worst outcome. Draining is best-effort by
        design — see :py:meth:`drain_errors` for the count.
        """
        self.write("*CLS")
        self.drain_errors()

    def drain_errors(self, limit: int = 32) -> list[tuple[int, str]]:
        """Read the error queue empty and return what was in it.

        Useful after deliberately sending something the instrument may reject,
        and as the workaround for ``*CLS`` not clearing the queue (see
        :py:meth:`clear_status`).

        Stops after ``limit`` entries so a queue that refuses to empty cannot
        hang the caller. The default comfortably exceeds this instrument's
        queue depth; a full return list means it stopped early, not that the
        queue is now clean.
        """
        drained: list[tuple[int, str]] = []
        for _ in range(limit):
            try:
                err = self.last_error()
            except SDM4065AError:
                # A malformed reply mid-drain is not worth failing a cleanup
                # path over; stop and report what was collected.
                break
            if err is None:
                break
            drained.append(err)
        return drained

    def self_test(self) -> bool:
        """``*TST?`` — True when the instrument reports itself healthy."""
        return self.query("*TST?").strip().startswith("0")

    #: Bit 5 of the Standard Event Status Register — Command Error (IEEE 488.2).
    #:
    #: Set when the instrument rejects a header it does not recognise. This is
    #: the *reliable* error signal on firmware 0.0.0.20: see
    #: :py:meth:`command_error` for why the error queue is not.
    ESR_COMMAND_ERROR = 1 << 5

    def standard_event_status(self) -> int:
        """``*ESR?`` — read and clear the Standard Event Status Register.

        Read-destructive, as IEEE 488.2 requires: bench-measured, a second read
        immediately after the first returns 0. So a caller wanting to attribute
        a bit to a specific command must clear first, send, then read — which is
        what :py:meth:`command_error` does.

        Unlike the error queue, this register *is* emptied by ``*CLS``.
        """
        raw = self.query("*ESR?")
        try:
            return int(raw.strip())
        except ValueError:
            raise SDM4065AError(f"unexpected *ESR? response: {raw!r}") from None

    def command_error(self) -> bool:
        """Whether the instrument flagged a Command Error since the last check.

        Prefer this to :py:meth:`last_error` when the question is merely "did
        that command get rejected?". The error queue on firmware 0.0.0.20 is
        not dependable for it:

        * ``*CLS`` does not empty the queue (see :py:meth:`clear_status`), so
          an unread entry from an earlier command is reported against a later
          one.
        * Once the queue has overflowed it can latch into answering
          ``0,"No Error"`` permanently. Bench-measured: after an overflow,
          ``SYSTem:ERRor?`` reported no error immediately after a deliberately
          bogus header, while ``*ESR?`` correctly returned 32 for the same
          command. Only a power cycle restored the queue.

        ``*ESR?`` stayed accurate throughout, and being a single read-clear
        register it cannot accumulate stale state. The cost is detail: this
        says a command was rejected, not which error code — use
        :py:meth:`last_error` when the code itself matters and the queue is
        known good.
        """
        return bool(self.standard_event_status() & self.ESR_COMMAND_ERROR)

    def last_error(self) -> Optional[tuple[int, str]]:
        """Pop one entry from the error queue, or None when it's empty.

        The instrument answers ``0,"No error"`` when clear — with the case of
        "error" varying between firmware states, hence the code-only check
        below rather than a string comparison.

        On firmware 0.0.0.20 a None here does **not** prove the last command
        was accepted; see :py:meth:`command_error` for the failure mode and the
        alternative.
        """
        raw = self.query("SYSTem:ERRor?")
        parts = raw.split(",", 1)
        try:
            code = int(parts[0].strip())
        except (ValueError, IndexError):
            raise SDM4065AError(f"unexpected SYSTem:ERRor? response: {raw!r}") from None
        if code == 0:
            return None
        message = parts[1].strip().strip('"') if len(parts) > 1 else ""
        return code, message

    def raise_if_error(self) -> None:
        """Raise :py:class:`SDM4065ACommandError` if the queue holds an error."""
        err = self.last_error()
        if err is not None:
            raise SDM4065ACommandError(err[0], err[1])

    # ------------------------------------------------------------------
    # Function selection
    # ------------------------------------------------------------------

    def set_function(self, function: str) -> None:
        """Select the measurement function, e.g. ``"dcv"``, ``"res"``, ``"4w"``.

        Per manual §7.1 the previous function's range/resolution are
        remembered and restored if you switch back.
        """
        key = function.strip().lower()
        scpi = _FUNCTION_MAP.get(key)
        if scpi is None:
            pretty = ", ".join(sorted(set(_FUNCTION_MAP)))
            raise SDM4065AValueError(
                f"unknown function {function!r}. Valid: {pretty}"
            )
        # The argument is a quoted string here — FUNC VOLT:DC without quotes
        # is a syntax error on this instrument family.
        self.write(f'FUNCtion "{scpi}"')

    def get_function(self) -> str:
        """The active function's SCPI short form, e.g. ``"VOLT"``, ``"CURR:AC"``.

        The instrument answers *with* quotation marks (manual §7.1: "The short
        form of the selected function is returned in quotation marks"), so they
        are stripped — a caller comparing against ``"RES"`` should not have to
        know about the quoting.
        """
        return self.query("FUNCtion?").strip().strip('"')

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def _measure(
        self,
        scpi_function: str,
        range_: Optional[Union[float, str]],
        allowed: tuple[float, ...] = (),
        what: str = "",
    ) -> float:
        if allowed:
            _validate_range(range_, allowed, what or scpi_function)
        arg = _format_range(range_)
        command = f"MEASure:{scpi_function}?"
        if arg is not None:
            command += f" {arg}"
        value = self.query_float(command)
        self._check_overload(value, scpi_function, range_)
        return value

    @staticmethod
    def _check_overload(
        value: float,
        function: str,
        range_: Optional[Union[float, str]] = None,
    ) -> None:
        if abs(value) >= _OVERLOAD_THRESHOLD:
            raise SDM4065AOverloadError(function, range_)

    def measure_dc_voltage(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One DC voltage reading, in volts (``MEAS:VOLT:DC?``)."""
        return self._measure("VOLTage:DC", range_, DC_VOLTAGE_RANGES, "DC voltage")

    def measure_ac_voltage(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One AC voltage reading, in volts RMS (``MEAS:VOLT:AC?``)."""
        return self._measure("VOLTage:AC", range_, AC_VOLTAGE_RANGES, "AC voltage")

    def measure_dc_current(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One DC current reading, in amps (``MEAS:CURR:DC?``)."""
        return self._measure("CURRent:DC", range_)

    def measure_ac_current(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One AC current reading, in amps RMS (``MEAS:CURR:AC?``)."""
        return self._measure("CURRent:AC", range_)

    def measure_resistance(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One **2-wire** resistance reading, in ohms (``MEAS:RES?``).

        Accuracy caveat worth repeating: the datasheet's resistance specs
        assume 4-wire, or 2-wire with a null taken. Without a null, add 0.2 Ω
        for lead and contact resistance (datasheet note [6]) — which at 100 Ω
        is 0.2%, far larger than the range's 1-year spec. Use
        :py:meth:`measure_resistance_4wire`, or null.

        **But do not combine this with a null**: ``MEAS:RES?`` is
        ``CONFigure`` + ``READ?``, and ``CONFigure`` clears the null state,
        the null value *and* the range (§7.4). After :py:meth:`null_now`,
        read with :py:meth:`read_nulled` or :py:meth:`read` instead.
        """
        return self._measure("RESistance", range_, RESISTANCE_RANGES, "resistance")

    def measure_resistance_4wire(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One **4-wire** resistance reading, in ohms (``MEAS:FRES?``).

        Needs separate source and sense leads. This is the accurate way to
        measure low resistance: lead resistance drops out, so no null is
        required for the datasheet spec to hold.
        """
        return self._measure("FRESistance", range_, RESISTANCE_RANGES, "resistance")

    def measure_capacitance(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One capacitance reading, in farads (``MEAS:CAP?``)."""
        return self._measure("CAPacitance", range_)

    def measure_frequency(
        self, range_: Optional[Union[float, str]] = None
    ) -> float:
        """One frequency reading, in hertz (``MEAS:FREQ?``)."""
        return self._measure("FREQuency", range_)

    def measure_period(self, range_: Optional[Union[float, str]] = None) -> float:
        """One period reading, in seconds (``MEAS:PER?``)."""
        return self._measure("PERiod", range_)

    def measure_continuity(self) -> float:
        """One continuity reading, in ohms (``MEAS:CONT?``)."""
        return self._measure("CONTinuity", None)

    def measure_diode(self) -> float:
        """One diode-junction reading, in volts (``MEAS:DIOD?``)."""
        return self._measure("DIODe", None)

    def measure_temperature(
        self,
        probe: Optional[str] = None,
        type_: Optional[str] = None,
    ) -> float:
        """One temperature reading (``MEAS:TEMP?``).

        ``probe`` is ``"RTD"`` or ``"THER"``; ``type_`` is the sensor type
        (``"PT100"`` for RTD, or e.g. ``"KITS90"`` for a thermocouple).
        Units follow :py:meth:`set_temperature_unit`.
        """
        command = "MEASure:TEMPerature?"
        if probe is not None:
            token = probe.strip().upper()
            if token not in ("RTD", "THER", "DEFAULT", "DEF"):
                raise SDM4065AValueError(
                    f"probe must be RTD, THER or DEFault, got {probe!r}"
                )
            command += f" {token}"
            if type_ is not None:
                command += f",{type_.strip().upper()}"
        elif type_ is not None:
            raise SDM4065AValueError(
                "type_ requires probe — the sensor type is meaningless without "
                "knowing whether it is an RTD or a thermocouple"
            )
        value = self.query_float(command)
        self._check_overload(value, "TEMPerature")
        return value

    # ------------------------------------------------------------------
    # Configure / trigger / fetch
    # ------------------------------------------------------------------

    def configure_resistance(
        self,
        range_: Optional[Union[float, str]] = None,
        *,
        four_wire: bool = False,
    ) -> None:
        """``CONF:RES`` / ``CONF:FRES`` — set up without triggering a reading.

        Resets that function's measurement parameters to defaults, including
        NPLC back to 10 (manual §7.4.1's note), so call
        :py:meth:`set_nplc` *after* this, never before.
        """
        _validate_range(range_, RESISTANCE_RANGES, "resistance")
        head = "CONFigure:FRESistance" if four_wire else "CONFigure:RESistance"
        arg = _format_range(range_)
        self.write(head if arg is None else f"{head} {arg}")
        # CONFigure resets this function's parameters, so the tracked copies
        # must follow or they would report a configuration that is no longer
        # loaded. Autozero's default is OFF for resistance (§7.4.7).
        self._nplc = 10.0
        self._resize_timeout()
        self._autozero["FRES" if four_wire else "RES"] = False

    def configure_dc_voltage(
        self, range_: Optional[Union[float, str]] = None
    ) -> None:
        """``CONF:VOLT:DC`` — set up DC voltage without triggering."""
        _validate_range(range_, DC_VOLTAGE_RANGES, "DC voltage")
        arg = _format_range(range_)
        head = "CONFigure:VOLTage:DC"
        self.write(head if arg is None else f"{head} {arg}")
        self._nplc = 10.0
        self._resize_timeout()
        self._autozero["VOLT"] = True      # §7.6.9: DC voltage defaults ON

    def get_configuration(self) -> str:
        """``CONF?`` — the present function, range and resolution.

        The instrument answers with a quoted string like
        ``VOLT +2.00000000E-01,+2.00000000E-08`` (manual §3.1); the quotes are
        stripped, the rest returned verbatim.
        """
        return self.query("CONFigure?").strip().strip('"')

    def set_sample_count(self, count: int) -> None:
        """``SAMPle:COUNt`` — readings per trigger."""
        if count < 1:
            raise SDM4065AValueError(f"sample count must be >= 1, got {count}")
        self.write(f"SAMPle:COUNt {int(count)}")
        self._samples = int(count)
        self._resize_timeout()

    def get_sample_count(self) -> int:
        """The configured ``SAMPle:COUNt``."""
        return int(self.query_float("SAMPle:COUNt?"))

    def read(self) -> list[float]:
        """``READ?`` — trigger and return the reading(s).

        Always a list: with ``SAMPle:COUNt > 1`` the instrument returns
        several comma-separated values, and a caller that assumed a scalar
        would silently keep only the first.
        """
        values = self.query_floats("READ?")
        for v in values:
            self._check_overload(v, self.get_function())
        return values

    def initiate(self) -> None:
        """``INIT`` — start an acquisition into memory, without reading it out."""
        self.write("INITiate")

    def fetch(self) -> list[float]:
        """``FETCh?`` — retrieve readings taken by a previous :py:meth:`initiate`."""
        return self.query_floats("FETCh?")

    def abort(self) -> None:
        """``ABORt`` — stop an acquisition in progress."""
        self.write("ABORt")

    # ------------------------------------------------------------------
    # Integration time, autozero, null
    # ------------------------------------------------------------------

    def set_nplc(self, nplc: float, *, function: str = "RESistance") -> None:
        """Set integration time in power-line cycles for ``function``.

        Valid on the SDM4065A: 100, 10, 1, 0.1, 0.01, 0.001 (default 10).
        Rejected client-side rather than passed through, because the
        instrument would coerce an off-list value silently and the caller
        would then trust an integration time it never got — and the
        datasheet's noise adder is indexed on exactly this number.

        Higher is slower and quieter: 100 PLC is ~2 s per reading at 50 Hz.
        """
        if float(nplc) not in NPLC_VALUES:
            pretty = ", ".join(f"{v:G}" for v in NPLC_VALUES)
            raise SDM4065AValueError(
                f"{nplc:G} is not an SDM4065A NPLC value. Valid: {pretty}. "
                f"(The SDM4055A accepts only 10/1/0.01 — check the model.)"
            )
        self.write(f"{function}:NPLC {float(nplc):G}")
        self._nplc = float(nplc)
        self._resize_timeout()

    def get_nplc(self, *, function: str = "RESistance") -> float:
        """The configured integration time in power-line cycles."""
        return self.query_float(f"{function}:NPLC?")

    def set_autozero(self, enable: bool, *, function: str = "RESistance") -> None:
        """Enable or disable autozero for ``function``.

        Autozero makes the instrument measure and subtract its own internal
        offset around each reading, which removes offset drift at the cost of
        roughly halving the reading rate. For resistance it defaults **OFF**
        (§7.4.7) — worth knowing, because it means a low-resistance
        measurement is *not* autozeroed unless you ask.

        Sends ``<function>:ZERO:AUTO``, **not** the ``:AZ`` of §7.4.7 — see
        :py:data:`AUTOZERO_NODE` for why the manual cannot be followed here.
        """
        self.write(f"{function}:{AUTOZERO_NODE} {'ON' if enable else 'OFF'}")
        self._autozero[self._az_key(function)] = bool(enable)

    @staticmethod
    def _az_key(function: str) -> str:
        """Canonical key for autozero shadow state — ``FRES`` before ``RES``.

        Ordering matters: ``"FRESistance".startswith("RES")`` is False but
        ``"RES" in "FRESistance"`` is True, so a substring test would collapse
        the two functions into one and 2-wire would inherit 4-wire's setting.
        """
        upper = function.upper()
        for key in ("FRES", "RES", "VOLT", "CURR"):
            if upper.startswith(key):
                return key
        return upper

    def get_autozero(self, *, function: str = "RESistance") -> bool:
        """Whether autozero is on for ``function``, read back from the meter.

        A real readback, via ``<function>:ZERO:AUTO?``. Bench-verified to
        round-trip on all four functions (resistance, 4-wire resistance, DC
        volts, DC current) on firmware 0.0.0.20.

        Also refreshes the tracked copy that :py:meth:`get_autozero_cached`
        returns, so the two cannot drift apart once this has been called.
        """
        result = self.query(f"{function}:{AUTOZERO_NODE}?").strip() in ("1", "ON")
        self._autozero[self._az_key(function)] = result
        return result

    def get_autozero_cached(self, *, function: str = "RESistance") -> bool:
        """Autozero state as last commanded, without touching the instrument.

        Seeded from the documented post-``*RST`` defaults, updated by
        :py:meth:`set_autozero`, and re-seeded by :py:meth:`reset` and the
        ``configure_*`` methods. Exists for callers that must not add SCPI
        traffic — e.g. inside a timed measurement loop — and for reporting
        configuration alongside a reading without a round trip per field.

        Prefer :py:meth:`get_autozero`, which asks the instrument. This value
        is only a record of what was commanded, so it will disagree with the
        instrument if the setting is changed from the front panel.
        """
        return self._autozero[self._az_key(function)]

    def set_autorange(self, enable: bool, *, function: str = "RESistance") -> None:
        """Enable or disable autoranging for ``function``.

        Manual ranging is faster (no range hunting) and is what you want when
        the value is known — as it is when reading a commanded resistance.
        """
        self.write(f"{function}:RANGe:AUTO {'ON' if enable else 'OFF'}")

    def set_range(
        self, range_: float, *, function: str = "RESistance"
    ) -> None:
        """Pin ``function`` to a fixed range, disabling autorange."""
        if function.upper().startswith(("RES", "FRES")):
            _validate_range(range_, RESISTANCE_RANGES, "resistance")
        self.write(f"{function}:RANGe {_format_range(range_)}")

    def get_autorange(self, *, function: str = "RESistance") -> bool:
        """Whether autoranging is on for ``function``.

        Needed to interpret :py:meth:`get_range`, which returns the same number
        whether the range was pinned or merely selected by autorange — see
        :py:data:`DEFAULT_RESISTANCE_RANGE`. For accuracy work the distinction
        is the point: only a pinned range keeps the "% of range" error term
        fixed across readings.
        """
        return self.query(f"{function}:RANGe:AUTO?").strip() in ("1", "ON")

    def get_range(self, *, function: str = "RESistance") -> float:
        """The active range for ``function``.

        With autorange on this is whichever range the instrument has currently
        selected, which moves with the input; pair it with
        :py:meth:`get_autorange` before treating it as a configuration.
        """
        return self.query_float(f"{function}:RANGe?")

    def set_null(
        self, enable: bool, *, function: str = "RESistance"
    ) -> None:
        """Enable or disable null / "Ref" (relative) mode for ``function``.

        Beware a side effect the manual buries in a bullet (§7.4.2):
        *enabling* null also switches ``NULL:VALue:AUTO`` on, which makes the
        instrument overwrite the null value with its own next reading. So
        ``set_null_value(x)`` then ``set_null(True)`` does **not** null by
        ``x``. :py:meth:`null_now` orders these correctly; if you drive them
        by hand, set the state first and the value second, or use
        :py:meth:`set_null_auto` deliberately.
        """
        self.write(f"{function}:NULL:STATe {'ON' if enable else 'OFF'}")

    def set_null_auto(
        self, enable: bool, *, function: str = "RESistance"
    ) -> None:
        """Enable/disable automatic null-value selection (``NULL:VALue:AUTO``).

        With this on, the *first* reading taken becomes the null offset for
        every subsequent one, and the instrument then turns this flag back off
        by itself (§7.4.4). Convenient interactively, wrong for a repeatable
        measurement: whatever happened to be connected at that instant
        silently becomes the zero. Prefer :py:meth:`null_now`.
        """
        self.write(f"{function}:NULL:VALue:AUTO {'ON' if enable else 'OFF'}")

    def get_null_auto(self, *, function: str = "RESistance") -> bool:
        """Whether automatic null-value selection is armed."""
        return self.query(f"{function}:NULL:VALue:AUTO?").strip() in ("1", "ON")

    def get_null(self, *, function: str = "RESistance") -> bool:
        """Whether null mode is on for ``function``."""
        return self.query(f"{function}:NULL:STATe?").strip() in ("1", "ON")

    def set_null_value(
        self, value: float, *, function: str = "RESistance"
    ) -> None:
        """Set the null offset subtracted from readings, in the function's units.

        Range is ±110 MΩ for resistance (§7.4.3). Setting an explicit value
        also disables ``NULL:VALue:AUTO``, which is what you want — see
        :py:meth:`set_null`.
        """
        if function.upper().startswith(("RES", "FRES")) and abs(float(value)) > 110e6:
            raise SDM4065AValueError(
                f"resistance null value must be within ±110 MΩ, got {value:G}"
            )
        self.write(f"{function}:NULL:VALue {float(value):G}")

    def get_null_value(self, *, function: str = "RESistance") -> float:
        """The null offset currently subtracted."""
        return self.query_float(f"{function}:NULL:VALue?")

    def null_now(
        self,
        *,
        function: str = "RESistance",
        samples: int = 1,
    ) -> float:
        """Measure the present input and install it as the null offset.

        The 2-wire low-resistance recipe: short the test leads, call this,
        then measure. Without it the datasheet adds 0.2 Ω of lead and contact
        error to every 2-wire resistance reading (note [6]).

        ``samples`` averages that many readings before storing the offset. The
        offset is a *constant* subtracted from every later measurement, so
        noise captured here is not averaged away by anything downstream — it
        becomes systematic error. Averaging costs a second and removes that.

        **Call this after configuring the range, and take subsequent readings
        with** :py:meth:`read` **, not** :py:meth:`measure_resistance`.
        Two instrument behaviours combine to make that non-negotiable:

        * ``MEASure:RES?`` is ``CONFigure`` + ``READ?`` rolled together, and
          ``CONFigure`` resets null state, null value *and* range to defaults
          (the "set to its default value after a Factory Reset or CONFigure
          function" note throughout §7.4). So every ``measure_*`` call throws
          the null away — and the range with it, back to 2 kΩ. Hence this
          method uses ``READ?``, which triggers without reconfiguring.
        * Enabling the null state arms ``NULL:VALue:AUTO`` (§7.4.2), which
          makes the instrument overwrite the offset with its own next
          reading. So the state goes on *first*, then the explicit value.
          The natural value-then-state order silently nulls by the wrong
          number.
        * §7.4.3 says writing a value disarms AUTO. **On firmware 0.0.0.20 it
          does not** — bench-measured: after ``STATe ON`` then ``VALue``,
          ``NULL:VALue:AUTO?`` still answered ``1``. So AUTO is turned off
          explicitly afterwards rather than assumed. Without that the
          instrument overwrites the offset with its next reading and the null
          is a no-op that leaves every result *looking* nulled.

        Returns the offset actually stored, read back from the instrument
        rather than assumed, so a caller can log the number that is really
        being subtracted from its measurements.
        """
        # READ? rather than MEASure:...? — see the note above. This also means
        # the caller's range and NPLC survive, which is the whole point of
        # nulling on the range you intend to measure on.
        self.set_null(False, function=function)
        readings: list[float] = []
        for _ in range(max(1, int(samples))):
            readings.extend(self.read())
        offset = sum(readings) / len(readings)

        self.set_null(True, function=function)
        self.set_null_value(offset, function=function)
        # Not redundant with the value write — see the AUTO note above.
        self.set_null_auto(False, function=function)

        stored = self.get_null_value(function=function)
        log.info(
            "sdm4065a: null offset for %s: measured %.6g over %d reading(s), "
            "instrument stored %.6g",
            function,
            offset,
            len(readings),
            stored,
        )
        return stored

    def read_nulled(self, *, function: str = "RESistance") -> float:
        """One nulled reading, averaging nothing — ``READ?`` after a null.

        Exists to make the correct call obvious at the point of use. Reaching
        for :py:meth:`measure_resistance` after :py:meth:`null_now` silently
        discards the null and the range; this does not.
        """
        if not self.get_null(function=function):
            raise SDM4065AError(
                f"{function} null is not enabled — call null_now() first, or "
                f"use read()/measure_resistance() if no null is wanted"
            )
        values = self.read()
        return values[0]

    def set_temperature_unit(self, unit: str) -> None:
        """``UNIT:TEMPerature`` — ``"C"``, ``"F"`` or ``"K"``."""
        token = unit.strip().upper()
        if token not in ("C", "F", "K"):
            raise SDM4065AValueError(f"unit must be C, F or K, got {unit!r}")
        self.write(f"UNIT:TEMPerature {token}")

    def get_temperature_unit(self) -> str:
        """The configured temperature unit."""
        return self.query("UNIT:TEMPerature?").strip().strip('"')

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def reading_timeout_ms(nplc: float, samples: int = 1, mains_hz: float = 50.0) -> int:
        """A VISA timeout that comfortably covers ``samples`` at ``nplc``.

        One reading takes about ``nplc / mains_hz`` seconds of integration,
        plus per-reading overhead. Defaults to 50 Hz because assuming 60 would
        *underestimate* the time on a 50 Hz grid, and an undersized timeout
        surfaces as a spurious "instrument not responding".
        """
        integration_s = (nplc / mains_hz) * max(1, samples)
        return int((integration_s * 2.0 + 2.0) * 1000)

    def _resize_timeout(self) -> None:
        """Widen the VISA timeout to cover the configured integration time.

        Called from :py:meth:`set_nplc` and :py:meth:`set_sample_count`,
        because a timeout that is too short for the *configured* measurement is
        not a tuning problem — it is a wedged instrument. Aborting a ``READ?``
        mid-integration leaves the reply queued in the device's bulk-IN
        endpoint and stops it draining bulk-OUT, so every later transfer fails
        (``Errno 110``) even though the USB link is fine and endpoint-0 control
        transfers still succeed. Recovering needs
        :py:meth:`clear_device_buffers`, and on this unit's firmware
        (0.0.0.20) not even that was enough — it took a front-panel power
        cycle.

        Bench-measured on firmware 0.0.0.20, at the 100 NPLC the datasheet's
        accuracy figures require (note [1]): one reading takes **2.09 s**, so
        the 10 s default covers it — but **5 readings take 10.14 s**, which it
        does not. A default that is *nearly* enough is the dangerous kind,
        because the failure reads as an unreliable instrument rather than as a
        configuration mistake.

        Never shrinks below the timeout the session was opened with, so an
        explicit ``timeout_ms`` stays a floor rather than being silently
        overridden by a low-NPLC configuration.
        """
        needed = self.reading_timeout_ms(self._nplc, self._samples)
        target = max(needed, self._base_timeout_ms)
        try:
            if self._inst.timeout != target:
                self._inst.timeout = target
                log.debug(
                    "sdm4065a: timeout -> %d ms for NPLC %g x %d sample(s)",
                    target,
                    self._nplc,
                    self._samples,
                )
        except Exception:
            # A backend that won't accept a timeout change is not a reason to
            # fail the configuration command that just succeeded.
            log.debug("sdm4065a: could not resize the VISA timeout", exc_info=True)

    def clear_device_buffers(self) -> bool:
        """USBTMC ``INITIATE_CLEAR`` — flush a wedged endpoint pair.

        The class-level recovery for an interrupted transfer: it tells the
        device firmware to discard queued input and output. Control transfers
        go over endpoint 0, which stays alive when both bulk endpoints are
        timing out, so this can work when a plain read cannot.

        Returns True when the device reported the clear complete. Best-effort
        by design — returns False rather than raising, because this is what a
        caller reaches for when things are *already* broken, and it is not a
        guaranteed fix: on firmware 0.0.0.20 a clear succeeded at the USB
        level (``STATUS_SUCCESS``) while the SCPI parser stayed stuck. Also a
        no-op on non-USB sessions (LAN), which have no such request.
        """
        if self._closed or not self._resource.upper().startswith("USB"):
            return False
        try:
            import usb.util  # noqa: PLC0415  (optional, USB-only path)

            dev = self._inst.visalib.sessions[self._inst.session].interface.usb_dev
            intf = dev.get_active_configuration()[(0, 0)].bInterfaceNumber
            rtype = usb.util.build_request_type(
                usb.util.CTRL_IN,
                usb.util.CTRL_TYPE_CLASS,
                usb.util.CTRL_RECIPIENT_INTERFACE,
            )
            if dev.ctrl_transfer(rtype, _TMC_INITIATE_CLEAR, 0, intf, 1,
                                 timeout=3000)[0] != _TMC_STATUS_SUCCESS:
                return False
            for _ in range(10):
                time.sleep(0.2)
                status = dev.ctrl_transfer(
                    rtype, _TMC_CHECK_CLEAR_STATUS, 0, intf, 2, timeout=3000
                )
                if status[0] == _TMC_STATUS_SUCCESS:
                    return True
            return False
        except Exception:
            log.debug("sdm4065a: USBTMC clear failed", exc_info=True)
            return False


def _is_sdm4065a_resource(resource: str) -> bool:
    """True when a VISA resource string names an SDM4065A.

    Parses the ``::``-separated fields rather than substring-matching the
    whole string, because **VISA backends disagree on the radix**. For the
    same physical meter:

    * NI-VISA / Keysight IO:  ``USB0::0xF4EC::0x1220::SDM46A0CA00021::INSTR``
    * pyvisa-py:              ``USB0::62700::4640::SDM46A0CA00021::0::INSTR``

    62700 is 0xF4EC and 4640 is 0x1220 — the same numbers in decimal. An
    earlier version of this function looked for the literal text ``F4EC``,
    which meant the meter was visible to ``list_resources()`` and invisible
    to this driver on exactly the boards that need pyvisa-py (no kernel
    ``usbtmc`` module). Bench-verified on the Uno Q: the failure looked
    identical to an unplugged instrument.

    Substring matching is also wrong in principle — a serial number can
    contain the digits of a VID, so it could match the wrong field.
    """
    parts = resource.split("::")
    if len(parts) < 3 or not parts[0].upper().startswith("USB"):
        return False

    def as_int(token: str) -> Optional[int]:
        token = token.strip()
        try:
            return int(token, 16) if token.lower().startswith("0x") else int(token)
        except ValueError:
            return None

    return (
        as_int(parts[1]) == SIGLENT_USB_VID and as_int(parts[2]) == SDM4065A_USB_PID
    )


def _autodiscover(rm) -> str:
    """Find exactly one SDM4065A among the VISA resources.

    Matches on VID/PID rather than parsing model names out of ``*IDN?``:
    opening every resource to interrogate it would disturb other
    instruments on the bus, and this bench has five.
    """
    resources = sorted(rm.list_resources())
    vid = f"{SIGLENT_USB_VID:#06x}".upper().replace("0X", "0x")
    matches = [r for r in resources if _is_sdm4065a_resource(r)]
    if not matches:
        raise SDM4065AConnectionError(
            f"no SDM4065A found ({vid}:{SDM4065A_USB_PID:#06x}). VISA reported: "
            f"{list(resources)!r}. Check the USB cable, and that the VISA "
            f"backend can see USB devices (pyvisa-py needs pyusb + libusb)."
        )
    if len(matches) > 1:
        raise SDM4065AConnectionError(
            f"several SDM4065As found: {matches!r}. Pass resource= to choose."
        )
    return matches[0]


def discover() -> list[str]:
    """Every SDM4065A VISA resource string visible on this host.

    Returns an empty list when VISA or a backend is unavailable, since
    "cannot look" and "nothing there" are the same answer to a caller
    deciding whether to offer the device.
    """
    try:
        import pyvisa
    except ImportError:
        return []
    try:
        rm = pyvisa.ResourceManager()
    except Exception:
        return []
    try:
        return [r for r in sorted(rm.list_resources()) if _is_sdm4065a_resource(r)]
    finally:
        try:
            rm.close()
        except Exception:
            log.debug("error closing VISA resource manager", exc_info=True)

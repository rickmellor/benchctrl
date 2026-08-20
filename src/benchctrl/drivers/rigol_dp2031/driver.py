"""Rigol DP2031 triple-output programmable DC power supply driver.

The DP2000 series (DP2031 today) is a USB-TMC SCPI-controlled triple
linear PSU:

- CH1, CH2: 0–32 V, 0–3 A each (Range 1)
- CH3:      0–6 V,  0–5 A      (Range 1)

CH1+CH2 can be internally paired in SERies (→ 64 V) or PARallel
(→ 6 A) via :py:meth:`set_channel_pair`. Tracking mode (CH1 ↔ CH2)
is exposed separately. All three channels carry independent OVP and
OCP protection.

This driver speaks **USB-TMC via pyvisa** only. A VISA backend must be
installed — NI-VISA, Keysight IO Libraries, or pyvisa-py with a USB
backend. Rigol Ultra Sigma installs a working backend. LAN, RS232 and
GPIB transports are deliberately not surfaced — they would not be
testable from over USB and the user has scoped this driver to USB
only.

Typical use::

    from benchctrl.drivers.rigol_dp2031 import RigolDP2031, DP2031Channel

    with RigolDP2031.open() as psu:               # auto-discover by VID/PID
        print(psu.info())
        psu.reset()
        psu.set_voltage(DP2031Channel.CH1, 3.3)
        psu.set_current(DP2031Channel.CH1, 0.500)
        psu.set_ovp_level(DP2031Channel.CH1, 3.8)
        psu.set_ovp_enabled(DP2031Channel.CH1, True)
        psu.set_output(DP2031Channel.CH1, True)
        v = psu.measure_voltage(DP2031Channel.CH1)
        i = psu.measure_current(DP2031Channel.CH1)
        psu.set_output(DP2031Channel.CH1, False)

Safety
------
Enabling an output drives voltage onto the corresponding output
terminals. Verify your DUT can tolerate the configured voltage and
that the current limit + OVP / OCP are armed before calling
:py:meth:`set_output`. The driver does not enforce DUT-side ratings.
The context manager / :py:meth:`close` disables all three channels
best-effort on exit so an exception doesn't leave outputs hot.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from enum import IntEnum
from typing import Optional, Union

log = logging.getLogger("benchctrl.drivers.rigol_dp2031.driver")

# Rigol's USB-TMC VID and the DP2000-family PID. Confirmed by bench
# scan on 2026-05-30 against the DP2031 sample we have.
RIGOL_USB_VID = 0x1AB1
DP2000_USB_PID = 0xA4A8

# ---------------------------------------------------------------------------
# Channel enum
# ---------------------------------------------------------------------------


class DP2031Channel(IntEnum):
    """Identifies one of the DP2031's three output channels.

    Values are 1 / 2 / 3 so the enum can be passed transparently
    where the SCPI syntax `:SOURce<n>:…` needs an integer index.
    """

    CH1 = 1
    CH2 = 2
    CH3 = 3


# ChannelLike accepts either the enum or a bare int, for ergonomics
# in REPL / scenario scripts.
ChannelLike = Union[DP2031Channel, int]


# ---------------------------------------------------------------------------
# Exception hierarchy — same shape as the DL3031A driver
# ---------------------------------------------------------------------------


class RigolDP2031Error(RuntimeError):
    """Base class for Rigol DP2031 driver errors."""


class RigolDP2031ConnectionError(RigolDP2031Error):
    """Couldn't open the VISA resource, or lost connection mid-session."""


class RigolDP2031CommandError(RigolDP2031Error):
    """Device returned a non-zero error from ``:SYSTem:ERRor[:NEXT]?``.

    ``code`` is the SCPI error number; ``message`` is the
    manufacturer's human-readable string.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"DP2031 SCPI error {code}: {message}")


class RigolDP2031ValueError(RigolDP2031Error, ValueError):
    """Client-side range / type check failed before sending."""


class RigolDP2031TimeoutError(RigolDP2031Error, TimeoutError):
    """Device didn't respond to a query within the VISA timeout."""


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RigolDP2031Info:
    """Identity read from ``*IDN?``.

    Attributes:
        manufacturer: e.g. ``"Rigol Technologies"``.
        model: e.g. ``"DP2031"``.
        serial: e.g. ``"DP2A243500269"``.
        firmware: e.g. ``"01.00.01.00.16"``.
        resource: the VISA resource string this device is bound to.
    """

    manufacturer: str
    model: str
    serial: str
    firmware: str
    resource: str


# ---------------------------------------------------------------------------
# Per-channel envelopes — Range 1 (the default; Range 2 is an option flag
# we haven't yet bench-verified, see plan §5.1 / O-2). Format: (min, max).
# ---------------------------------------------------------------------------

_CHANNEL_LIMITS: dict[int, dict[str, tuple[float, float]]] = {
    1: {
        "v":       (0.0, 32.0),
        "i":       (0.0, 3.0),
        "ovp":     (0.001, 35.2),
        "ocp":     (0.001, 3.3),
    },
    2: {
        "v":       (0.0, 32.0),
        "i":       (0.0, 3.0),
        "ovp":     (0.001, 35.2),
        "ocp":     (0.001, 3.3),
    },
    3: {
        "v":       (0.0, 6.0),
        "i":       (0.0, 5.0),
        "ovp":     (0.001, 6.6),
        "ocp":     (0.001, 5.5),
    },
}

# What the device returns from `:OUTPut:CVCC?` / `:OUTPut:MODE?`.
_REGULATION_VALUES = {"CV", "CC", "UR"}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class RigolDP2031:
    """Control a Rigol DP2031 triple-output PSU over VISA / USB-TMC.

    Construct via :py:meth:`open` rather than the constructor directly
    so the VISA resource manager lifetime is managed for you. Channel
    arguments accept either :py:class:`DP2031Channel` or a bare ``int``
    in ``{1, 2, 3}``.
    """

    DEFAULT_TIMEOUT_MS = 2000

    def __init__(
        self,
        instrument,  # pyvisa Resource — typed loosely so pyvisa is optional
        *,
        resource_string: str,
        owns_resource_manager: bool = False,
        resource_manager=None,
    ):
        self._inst = instrument
        self._resource = resource_string
        self._owns_rm = owns_resource_manager
        self._rm = resource_manager
        self._info: Optional[RigolDP2031Info] = None
        self._closed = False

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
    ) -> RigolDP2031:
        """Open a VISA session to a DP2031.

        If ``resource`` is None, auto-discovers by scanning USB
        resources for the Rigol DP2000 VID/PID. Pass an explicit VISA
        resource string (e.g.
        ``"USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR"``) to target a
        specific device.
        """
        try:
            import pyvisa
        except ImportError as e:
            raise RigolDP2031ConnectionError(
                "pyvisa is required for the DP2031 driver — "
                "install with `pip install benchctrl[bench-visa]`"
            ) from e

        try:
            rm = pyvisa.ResourceManager()
        except Exception as e:
            raise RigolDP2031ConnectionError(
                f"could not initialize VISA resource manager: {e}"
            ) from e

        if resource is None:
            resource = _autodiscover(rm)

        try:
            inst = rm.open_resource(resource)
        except Exception as e:
            raise RigolDP2031ConnectionError(
                f"could not open VISA resource {resource!r}: {e}"
            ) from e

        inst.timeout = timeout_ms
        inst.read_termination = read_termination
        inst.write_termination = write_termination

        return cls(
            inst,
            resource_string=resource,
            owns_resource_manager=True,
            resource_manager=rm,
        )

    def close(self) -> None:
        """Release the VISA session. Safe to call multiple times."""
        if self._closed:
            return
        try:
            self._inst.close()
        except Exception:
            log.debug("error closing VISA instrument", exc_info=True)
        if self._owns_rm and self._rm is not None:
            try:
                self._rm.close()
            except Exception:
                log.debug("error closing VISA resource manager", exc_info=True)
        self._closed = True

    def __enter__(self) -> RigolDP2031:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Best-effort safety: disable all three channels on exit so we
        # don't leave outputs hot if the caller forgets.
        for ch in DP2031Channel:
            try:
                self.set_output(ch, False)
            except Exception:
                log.warning(
                    "DP2031 set_output(%s, False) failed during __exit__",
                    ch.name, exc_info=True,
                )
        self.close()

    # ------------------------------------------------------------------
    # Low-level transport
    # ------------------------------------------------------------------

    def write(self, command: str) -> None:
        """Send a SCPI command (no response expected).

        Raises :py:class:`RigolDP2031ConnectionError` on VISA failure.
        """
        if self._closed:
            raise RigolDP2031ConnectionError("instrument is closed")
        try:
            self._inst.write(command)
        except Exception as e:
            raise RigolDP2031ConnectionError(
                f"write({command!r}) failed: {e}"
            ) from e

    def query(self, command: str) -> str:
        """Send a SCPI query and return the trimmed response string."""
        if self._closed:
            raise RigolDP2031ConnectionError("instrument is closed")
        try:
            raw = self._inst.query(command)
        except Exception as e:
            if "timeout" in str(e).lower() or "VI_ERROR_TMO" in str(e):
                raise RigolDP2031TimeoutError(
                    f"query({command!r}) timed out"
                ) from e
            raise RigolDP2031ConnectionError(
                f"query({command!r}) failed: {e}"
            ) from e
        return raw.strip()

    def query_float(self, command: str) -> float:
        s = self.query(command)
        try:
            return float(s)
        except ValueError as e:
            raise RigolDP2031Error(
                f"expected number from {command!r}, got {s!r}"
            ) from e

    def query_int(self, command: str) -> int:
        s = self.query(command)
        try:
            return int(s)
        except ValueError as e:
            raise RigolDP2031Error(
                f"expected integer from {command!r}, got {s!r}"
            ) from e

    # ------------------------------------------------------------------
    # Identity and housekeeping
    # ------------------------------------------------------------------

    def info(self) -> RigolDP2031Info:
        """Read ``*IDN?`` and parse into a structured record.

        Result is cached after the first call.
        """
        if self._info is None:
            raw = self.query("*IDN?")
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 4:
                raise RigolDP2031Error(f"unexpected *IDN? response: {raw!r}")
            self._info = RigolDP2031Info(
                manufacturer=parts[0],
                model=parts[1],
                serial=parts[2],
                firmware=parts[3],
                resource=self._resource,
            )
        return self._info

    def reset(self) -> None:
        """``*RST`` — restore factory default state."""
        self.write("*RST")

    def clear_status(self) -> None:
        """``*CLS`` — clear status registers and error queue."""
        self.write("*CLS")

    def last_error(self) -> Optional[tuple[int, str]]:
        """Read one entry from the error queue (``:SYSTem:ERRor[:NEXT]?``).

        Returns ``None`` if the queue is empty (code 0). Otherwise a
        ``(code, message)`` tuple. Read repeatedly until ``None`` to
        fully drain the queue.
        """
        raw = self.query(":SYSTem:ERRor?")
        m = re.match(r'^\s*(-?\d+)\s*,\s*"?([^"]*)"?\s*$', raw)
        if not m:
            raise RigolDP2031Error(f"unexpected :SYST:ERR? response: {raw!r}")
        code = int(m.group(1))
        message = m.group(2).strip()
        if code == 0:
            return None
        return (code, message)

    def raise_if_error(self) -> None:
        """Convenience: check :SYST:ERR? and raise if non-zero."""
        err = self.last_error()
        if err is not None:
            code, msg = err
            raise RigolDP2031CommandError(code, msg)

    # ------------------------------------------------------------------
    # Channel selection
    # ------------------------------------------------------------------

    def select_channel(self, channel: ChannelLike) -> None:
        """Set the device's "current" channel via ``:INSTrument:NSELect``.

        The driver's per-channel writes always set the channel
        explicitly on every call, so calling this directly is rarely
        needed; it's exposed because the front-panel and the
        :py:meth:`current_channel` query both reflect this state.
        """
        ch = _coerce_channel(channel)
        self.write(f":INSTrument:NSELect {ch}")

    def current_channel(self) -> DP2031Channel:
        """Returns the channel the device considers "selected"."""
        n = self.query_int(":INSTrument:NSELect?")
        if n not in {1, 2, 3}:
            raise RigolDP2031Error(
                f"unexpected :INSTrument:NSELect? response: {n}"
            )
        return DP2031Channel(n)

    # ------------------------------------------------------------------
    # Source — voltage / current setpoints
    # ------------------------------------------------------------------

    def set_voltage(self, channel: ChannelLike, volts: float) -> None:
        """Set the channel's voltage setpoint (V).

        Range:
            - CH1 / CH2: 0 – 32 V
            - CH3:        0 – 6 V

        Does NOT enable the output — use :py:meth:`set_output`.
        """
        ch = _coerce_channel(channel)
        _validate(volts, _CHANNEL_LIMITS[ch]["v"], f"voltage (CH{ch})")
        self.write(f":SOURce{ch}:VOLTage:LEVel:IMMediate {volts:.6f}")

    def get_voltage(self, channel: ChannelLike) -> float:
        """Read back the channel's voltage setpoint (V)."""
        ch = _coerce_channel(channel)
        return self.query_float(f":SOURce{ch}:VOLTage:LEVel:IMMediate?")

    def set_current(self, channel: ChannelLike, amps: float) -> None:
        """Set the channel's current limit / setpoint (A).

        Range:
            - CH1 / CH2: 0 – 3 A   (Range 1)
            - CH3:        0 – 5 A   (Range 1)

        On CH1/CH2, the device's low-current sampling mode
        (``:SYSTem:SAMPling LOW``) extends readback resolution to 1 µA
        for currents below ~11 mA — that's a measurement-side feature,
        not a setpoint constraint.
        """
        ch = _coerce_channel(channel)
        _validate(amps, _CHANNEL_LIMITS[ch]["i"], f"current (CH{ch})")
        self.write(f":SOURce{ch}:CURRent:LEVel:IMMediate {amps:.6f}")

    def get_current(self, channel: ChannelLike) -> float:
        """Read back the channel's current setpoint (A)."""
        ch = _coerce_channel(channel)
        return self.query_float(f":SOURce{ch}:CURRent:LEVel:IMMediate?")

    # ------------------------------------------------------------------
    # Output enable
    # ------------------------------------------------------------------

    def set_output(self, channel: ChannelLike, on: bool) -> None:
        """Enable or disable the channel's output.

        SAFETY: enabling the output drives the configured voltage onto
        the terminals through the configured current limit. Verify
        DUT-side ratings + OVP / OCP arming before calling with
        ``on=True``.
        """
        ch = _coerce_channel(channel)
        self.write(f":OUTPut:STATe CH{ch},{'ON' if on else 'OFF'}")

    def get_output(self, channel: ChannelLike) -> bool:
        ch = _coerce_channel(channel)
        s = self.query(f":OUTPut:STATe? CH{ch}").upper()
        if s in ("ON", "1"):
            return True
        if s in ("OFF", "0"):
            return False
        raise RigolDP2031Error(
            f"unexpected :OUTPut:STATe? CH{ch} response: {s!r}"
        )

    def set_output_all(self, on: bool) -> None:
        """Enable or disable all three outputs in one command.

        Uses the device's ``:OUTPut:STATe ALL,<bool>`` form. SAFETY
        warning of :py:meth:`set_output` applies in triplicate.
        """
        self.write(f":OUTPut:STATe ALL,{'ON' if on else 'OFF'}")

    def output_regulation(self, channel: ChannelLike) -> str:
        """Return the channel's regulation state.

        One of:
            - ``"CV"`` — constant voltage (load is below current limit)
            - ``"CC"`` — constant current (current limit is active)
            - ``"UR"`` — unregulated (output off or out of compliance)
        """
        ch = _coerce_channel(channel)
        s = self.query(f":OUTPut:CVCC? CH{ch}").upper()
        if s not in _REGULATION_VALUES:
            raise RigolDP2031Error(
                f"unexpected :OUTPut:CVCC? CH{ch} response: {s!r}"
            )
        return s

    # ------------------------------------------------------------------
    # Protection — OVP (over-voltage protection)
    # ------------------------------------------------------------------

    def set_ovp_level(self, channel: ChannelLike, volts: float) -> None:
        """Set the OVP trip threshold (V).

        Range:
            - CH1 / CH2: 1 mV – 35.2 V
            - CH3:        1 mV – 6.6 V

        OVP is independent per channel. Setting the level does NOT
        enable it; call :py:meth:`set_ovp_enabled` separately.
        """
        ch = _coerce_channel(channel)
        _validate(volts, _CHANNEL_LIMITS[ch]["ovp"], f"OVP level (CH{ch})")
        self.write(f":OUTPut:OVP:VALue CH{ch},{volts:.6f}")

    def get_ovp_level(self, channel: ChannelLike) -> float:
        ch = _coerce_channel(channel)
        return self.query_float(f":OUTPut:OVP:VALue? CH{ch}")

    def set_ovp_enabled(self, channel: ChannelLike, on: bool) -> None:
        """Arm or disarm OVP on the channel."""
        ch = _coerce_channel(channel)
        self.write(f":OUTPut:OVP:STATe CH{ch},{'ON' if on else 'OFF'}")

    def get_ovp_enabled(self, channel: ChannelLike) -> bool:
        ch = _coerce_channel(channel)
        s = self.query(f":OUTPut:OVP:STATe? CH{ch}").upper()
        if s in ("ON", "1"):
            return True
        if s in ("OFF", "0"):
            return False
        raise RigolDP2031Error(
            f"unexpected :OUTPut:OVP:STATe? CH{ch} response: {s!r}"
        )

    # ------------------------------------------------------------------
    # Protection — OCP (over-current protection)
    # ------------------------------------------------------------------

    def set_ocp_level(self, channel: ChannelLike, amps: float) -> None:
        """Set the OCP trip threshold (A).

        Range:
            - CH1 / CH2: 1 mA – 3.3 A
            - CH3:        1 mA – 5.5 A

        OCP is independent per channel. Setting the level does NOT
        enable it; call :py:meth:`set_ocp_enabled` separately.
        """
        ch = _coerce_channel(channel)
        _validate(amps, _CHANNEL_LIMITS[ch]["ocp"], f"OCP level (CH{ch})")
        self.write(f":OUTPut:OCP:VALue CH{ch},{amps:.6f}")

    def get_ocp_level(self, channel: ChannelLike) -> float:
        ch = _coerce_channel(channel)
        return self.query_float(f":OUTPut:OCP:VALue? CH{ch}")

    def set_ocp_enabled(self, channel: ChannelLike, on: bool) -> None:
        """Arm or disarm OCP on the channel."""
        ch = _coerce_channel(channel)
        self.write(f":OUTPut:OCP:STATe CH{ch},{'ON' if on else 'OFF'}")

    def get_ocp_enabled(self, channel: ChannelLike) -> bool:
        ch = _coerce_channel(channel)
        s = self.query(f":OUTPut:OCP:STATe? CH{ch}").upper()
        if s in ("ON", "1"):
            return True
        if s in ("OFF", "0"):
            return False
        raise RigolDP2031Error(
            f"unexpected :OUTPut:OCP:STATe? CH{ch} response: {s!r}"
        )

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def measure_voltage(self, channel: ChannelLike) -> float:
        """Measure the channel's output voltage (V).

        Reads the actual voltage at the output terminals (or at the
        remote-sense inputs if 4-wire sense is enabled), not the
        setpoint. Returns 0 when the output is off.
        """
        ch = _coerce_channel(channel)
        return self.query_float(f":MEASure:VOLTage:DC? CH{ch}")

    def measure_current(self, channel: ChannelLike) -> float:
        """Measure the channel's output current (A)."""
        ch = _coerce_channel(channel)
        return self.query_float(f":MEASure:CURRent:DC? CH{ch}")

    def measure_power(self, channel: ChannelLike) -> float:
        """Measure the channel's output power (W)."""
        ch = _coerce_channel(channel)
        return self.query_float(f":MEASure:POWer:DC? CH{ch}")

    def measure_all(self, channel: ChannelLike) -> dict[str, float]:
        """Return ``{voltage_V, current_A, power_W}`` in one query.

        Uses the device's ``:MEASure:ALL? CHn`` form — one round-trip
        instead of three.
        """
        ch = _coerce_channel(channel)
        raw = self.query(f":MEASure:ALL? CH{ch}")
        parts = [p.strip() for p in raw.split(",")]
        if len(parts) != 3:
            raise RigolDP2031Error(
                f"expected 3 comma-separated values from "
                f":MEASure:ALL? CH{ch}, got {raw!r}"
            )
        try:
            v, i, p = (float(parts[0]), float(parts[1]), float(parts[2]))
        except ValueError as e:
            raise RigolDP2031Error(
                f"non-numeric value in :MEASure:ALL? CH{ch} response: {raw!r}"
            ) from e
        return {"voltage_V": v, "current_A": i, "power_W": p}

    def measure_all_channels(self) -> dict[int, dict[str, float]]:
        """Convenience: call :py:meth:`measure_all` on every channel.

        Returns ``{1: {...}, 2: {...}, 3: {...}}``. Three round-trips
        — not atomic. For tight polling loops, prefer per-channel
        :py:meth:`measure_all` calls scoped to what you need.
        """
        return {int(ch): self.measure_all(ch) for ch in DP2031Channel}

    # ------------------------------------------------------------------
    # Protection — OVP / OCP trip + clear + delay
    # ------------------------------------------------------------------

    def clear_ovp(self, channel: ChannelLike) -> None:
        """Clear a latched OVP trip on the channel.

        Per the manual, the ``:OUTPut:OVP:CLEar`` form clears the
        latched alarm but does NOT re-enable the output — the
        operator (or :py:meth:`set_output`) must re-enable it
        explicitly.
        """
        ch = _coerce_channel(channel)
        self.write(f":OUTPut:OVP:CLEar CH{ch}")

    def clear_ocp(self, channel: ChannelLike) -> None:
        """Clear a latched OCP trip on the channel.

        Like :py:meth:`clear_ovp`, this clears the latch but does not
        re-enable the output.
        """
        ch = _coerce_channel(channel)
        self.write(f":OUTPut:OCP:CLEar CH{ch}")

    def ovp_tripped(self, channel: ChannelLike) -> bool:
        """Has the channel's OVP latched?"""
        ch = _coerce_channel(channel)
        return self.query_int(f":OUTPut:OVP:ALAR? CH{ch}") == 1

    def ocp_tripped(self, channel: ChannelLike) -> bool:
        """Has the channel's OCP latched?"""
        ch = _coerce_channel(channel)
        return self.query_int(f":OUTPut:OCP:ALAR? CH{ch}") == 1

    def ovp_questionable(self, channel: ChannelLike) -> bool:
        """Same value as :py:meth:`ovp_tripped` via the ``QUES`` query.

        Manual exposes both ``ALAR`` and ``QUES`` queries that return
        the same latched alarm bit; we expose both for completeness.
        """
        ch = _coerce_channel(channel)
        return self.query_int(f":OUTPut:OVP:QUES? CH{ch}") == 1

    def ocp_questionable(self, channel: ChannelLike) -> bool:
        """Same value as :py:meth:`ocp_tripped` via the ``QUES`` query."""
        ch = _coerce_channel(channel)
        return self.query_int(f":OUTPut:OCP:QUES? CH{ch}") == 1

    def set_ocp_delay_ms(self, channel: ChannelLike, milliseconds: int) -> None:
        """Set the OCP integration / debounce delay in milliseconds.

        Range 0 – 1000 ms. The over-current alarm only fires once the
        threshold has been exceeded continuously for the delay window.
        Useful for absorbing inrush spikes.
        """
        ch = _coerce_channel(channel)
        if not isinstance(milliseconds, int) or isinstance(milliseconds, bool):
            raise RigolDP2031ValueError(
                f"OCP delay must be int ms, got {milliseconds!r}"
            )
        if milliseconds < 0 or milliseconds > 1000:
            raise RigolDP2031ValueError(
                f"OCP delay must be 0–1000 ms, got {milliseconds}"
            )
        self.write(f":OUTPut:OCP:DELay CH{ch},{milliseconds}")

    def get_ocp_delay_ms(self, channel: ChannelLike) -> int:
        """Read the OCP delay in milliseconds.

        Note the device returns a string with a unit suffix (e.g.
        ``"200ms"``) rather than a plain number. We parse and return
        the int.
        """
        ch = _coerce_channel(channel)
        raw = self.query(f":OUTPut:OCP:DELay? CH{ch}")
        return _parse_delay_ms(raw)

    # ------------------------------------------------------------------
    # IEEE 488.2 status / OPC / options
    # ------------------------------------------------------------------

    def event_status_register(self) -> int:
        """``*ESR?`` — read+clear the standard event status register."""
        return self.query_int("*ESR?")

    def set_event_status_enable(self, mask: int) -> None:
        """``*ESE <mask>`` — set the event status enable mask (0–255)."""
        _validate_mask(mask, "event status enable")
        self.write(f"*ESE {mask}")

    def get_event_status_enable(self) -> int:
        return self.query_int("*ESE?")

    def status_byte(self) -> int:
        """``*STB?`` — read the status byte register."""
        return self.query_int("*STB?")

    def set_service_request_enable(self, mask: int) -> None:
        """``*SRE <mask>`` — set the service request enable mask (0–255)."""
        _validate_mask(mask, "service request enable")
        self.write(f"*SRE {mask}")

    def get_service_request_enable(self) -> int:
        return self.query_int("*SRE?")

    def mark_op_complete(self) -> None:
        """``*OPC`` — set the OPC bit when pending operations finish."""
        self.write("*OPC")

    def wait_op_complete(self) -> int:
        """``*OPC?`` — block until pending operations finish; returns 1."""
        return self.query_int("*OPC?")

    def wait(self) -> None:
        """``*WAI`` — block subsequent commands until pending ops finish."""
        self.write("*WAI")

    def self_test(self) -> int:
        """``*TST?`` — runs the device self-test; returns 0 on pass, non-zero on fail."""
        return self.query_int("*TST?")

    def installed_options(self) -> list[str]:
        """``*OPT?`` — list installed option strings.

        Returns ``[]`` when no options are installed (the device
        returns ``"NONE"`` rather than an empty string in that case).
        """
        raw = self.query("*OPT?").strip()
        if not raw or raw.upper() == "NONE":
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def set_power_on_status_clear(self, on: bool) -> None:
        """``*PSC <bool>`` — set the power-on status-clear flag."""
        self.write(f"*PSC {1 if on else 0}")

    def get_power_on_status_clear(self) -> bool:
        return self.query_int("*PSC?") == 1

    def save_state(self, slot: int) -> None:
        """``*SAV n`` — save device state to slot 0–9 (file ``RIGOLn.RSF``)."""
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise RigolDP2031ValueError(f"slot must be int 0–9, got {slot!r}")
        if slot < 0 or slot > 9:
            raise RigolDP2031ValueError(f"slot must be 0–9, got {slot}")
        self.write(f"*SAV {slot}")

    def recall_state(self, slot: int) -> None:
        """``*RCL n`` — load device state from slot 0–9."""
        if not isinstance(slot, int) or isinstance(slot, bool):
            raise RigolDP2031ValueError(f"slot must be int 0–9, got {slot!r}")
        if slot < 0 or slot > 9:
            raise RigolDP2031ValueError(f"slot must be 0–9, got {slot}")
        self.write(f"*RCL {slot}")

    # ------------------------------------------------------------------
    # :STATus subsystem — operation / questionable / per-channel summary
    # ------------------------------------------------------------------

    def operation_condition(self) -> int:
        """``:STATus:OPERation:CONDition?`` — current operation condition register."""
        return self.query_int(":STATus:OPERation:CONDition?")

    def set_operation_enable(self, mask: int) -> None:
        _validate_mask(mask, "operation enable", maximum=65535)
        self.write(f":STATus:OPERation:ENABle {mask}")

    def get_operation_enable(self) -> int:
        return self.query_int(":STATus:OPERation:ENABle?")

    def operation_event(self) -> int:
        """Latched operation event register. Read clears."""
        return self.query_int(":STATus:OPERation:EVENt?")

    def preset_status(self) -> None:
        """``:STATus:PRESet`` — reset operation and questionable enable masks to defaults."""
        self.write(":STATus:PRESet")

    def set_questionable_enable(self, mask: int) -> None:
        _validate_mask(mask, "questionable enable", maximum=65535)
        self.write(f":STATus:QUEStionable:ENABle {mask}")

    def get_questionable_enable(self) -> int:
        return self.query_int(":STATus:QUEStionable:ENABle?")

    def questionable_event(self) -> int:
        """Latched questionable event register. Read clears."""
        return self.query_int(":STATus:QUEStionable:EVENt?")

    def set_instrument_enable(self, mask: int) -> None:
        _validate_mask(mask, "instrument enable", maximum=65535)
        self.write(f":STATus:QUEStionable:INSTrument:ENABle {mask}")

    def get_instrument_enable(self) -> int:
        return self.query_int(":STATus:QUEStionable:INSTrument:ENABle?")

    def instrument_event(self) -> int:
        return self.query_int(":STATus:QUEStionable:INSTrument:EVENt?")

    def channel_condition(self, channel: ChannelLike) -> int:
        """Per-channel questionable condition register.

        Bits per manual: 0=Vunreg, 1=Iunreg, 2=OVP, 3=OCP, 4=OTP.
        """
        ch = _coerce_channel(channel)
        return self.query_int(
            f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:CONDition?"
        )

    def set_channel_status_enable(self, channel: ChannelLike, mask: int) -> None:
        ch = _coerce_channel(channel)
        _validate_mask(mask, f"channel status enable (CH{ch})", maximum=65535)
        self.write(
            f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:ENABle {mask}"
        )

    def get_channel_status_enable(self, channel: ChannelLike) -> int:
        ch = _coerce_channel(channel)
        return self.query_int(
            f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:ENABle?"
        )

    def channel_status_event(self, channel: ChannelLike) -> int:
        ch = _coerce_channel(channel)
        return self.query_int(
            f":STATus:QUEStionable:INSTrument:ISUMmary{ch}:EVENt?"
        )

    def health_check(self) -> dict:
        """Poll the device's questionable + per-channel status registers.

        Returns a structured snapshot:

        - ``otp_global``: bit 4 of the top questionable register —
          over-temperature alarm.
        - ``fan_failure``: bit 11 of the top questionable register.
        - ``ch{1,2,3}``: per-channel dict with ``vunreg`` / ``iunreg``
          / ``ovp`` / ``ocp`` / ``otp`` bools.
        - ``error_queue``: first non-zero ``:SYSTem:ERRor?`` entry, if any.

        Reads the EVENT registers, which clears them — call once,
        store the snapshot.
        """
        top_event = self.questionable_event()
        result: dict = {
            "otp_global": bool(top_event & (1 << 4)),
            "fan_failure": bool(top_event & (1 << 11)),
            "questionable_event_raw": top_event,
        }
        for ch in DP2031Channel:
            cond = self.channel_condition(ch)
            result[f"ch{int(ch)}"] = {
                "vunreg": bool(cond & (1 << 0)),
                "iunreg": bool(cond & (1 << 1)),
                "ovp": bool(cond & (1 << 2)),
                "ocp": bool(cond & (1 << 3)),
                "otp": bool(cond & (1 << 4)),
                "condition_raw": cond,
            }
        err = self.last_error()
        result["error_queue"] = (
            {"code": err[0], "message": err[1]} if err is not None else None
        )
        return result

    # ------------------------------------------------------------------
    # System basics — beeper / brightness / lock / language / power-on
    # ------------------------------------------------------------------

    def beep_once(self) -> None:
        """``:SYSTem:BEEPer:IMMediate`` — one beep."""
        self.write(":SYSTem:BEEPer:IMMediate")

    def set_beeper(self, on: bool) -> None:
        """Enable or disable the beeper globally."""
        self.write(f":SYSTem:BEEPer:STATe {'ON' if on else 'OFF'}")

    def get_beeper(self) -> bool:
        return self.query_int(":SYSTem:BEEPer:STATe?") == 1

    def set_brightness(self, percent: int) -> None:
        """Set display brightness (1–100 %)."""
        if not isinstance(percent, int) or isinstance(percent, bool):
            raise RigolDP2031ValueError(
                f"brightness must be int 1–100, got {percent!r}"
            )
        if percent < 1 or percent > 100:
            raise RigolDP2031ValueError(
                f"brightness must be 1–100 %, got {percent}"
            )
        self.write(f":SYSTem:BRIGhtness {percent}")

    def get_brightness(self) -> int:
        return self.query_int(":SYSTem:BRIGhtness?")

    def scpi_version(self) -> str:
        """``:SYSTem:VERSion?`` — SCPI standard version string (e.g. ``"1999.0"``)."""
        return self.query(":SYSTem:VERSion?")

    def set_keyboard_lock(self, on: bool) -> None:
        """Lock or unlock the front-panel keypad."""
        self.write(f":SYSTem:KLOCk:STATe {'ON' if on else 'OFF'}")

    def get_keyboard_lock(self) -> bool:
        return self.query_int(":SYSTem:KLOCk:STATe?") == 1

    def set_touchscreen_lock(self, on: bool) -> None:
        """Lock or unlock the touchscreen."""
        self.write(f":SYSTem:TLOCk {'ON' if on else 'OFF'}")

    def get_touchscreen_lock(self) -> bool:
        return self.query_int(":SYSTem:TLOCk?") == 1

    def set_remote(self) -> None:
        """``:SYSTem:REMote`` — put the device into remote mode (locks front panel)."""
        self.write(":SYSTem:REMote")

    def set_local(self) -> None:
        """``:SYSTem:LOCal`` — return control to the front panel."""
        self.write(":SYSTem:LOCal")

    def set_remote_lock(self, on: bool) -> None:
        """``:SYSTem:RWLock <bool>`` — set/clear remote-with-lockout state.

        The single ``:SYSTem:RWLock`` form (no boolean arg) is a
        compatibility short-hand for ``ON``; we always send the
        explicit boolean for clarity.
        """
        self.write(f":SYSTem:RWLock:STATe {'ON' if on else 'OFF'}")

    _INTERFACE_STATE_VALUES = ("LOCal", "REMote", "RWLock")

    def set_interface_state(self, mode: str) -> None:
        """Set the remote-interface lock state.

        Accepts ``"LOCal"`` / ``"REMote"`` / ``"RWLock"`` (any case;
        we send the canonical case below).
        """
        norm = mode.strip()
        for canonical in self._INTERFACE_STATE_VALUES:
            if norm.upper() == canonical.upper() or norm.upper() == canonical[:3].upper():
                self.write(f":SYSTem:COMMunicate:RLSTate {canonical}")
                return
        raise RigolDP2031ValueError(
            f"interface state must be LOCal / REMote / RWLock, got {mode!r}"
        )

    def get_interface_state(self) -> str:
        return self.query(":SYSTem:COMMunicate:RLSTate?")

    def set_power_on_mode(self, mode: str) -> None:
        """Set what state the device boots into: ``"DEFault"`` or ``"LAST"``."""
        norm = mode.strip().upper()
        if norm in ("DEFAULT", "DEF"):
            self.write(":SYSTem:POWEron DEFault")
        elif norm == "LAST":
            self.write(":SYSTem:POWEron LAST")
        else:
            raise RigolDP2031ValueError(
                f"power-on mode must be DEFault or LAST, got {mode!r}"
            )

    def get_power_on_mode(self) -> str:
        """Returns the device's reply verbatim (typically the long form,
        e.g. ``"DEFAULT"`` or ``"LAST"``)."""
        return self.query(":SYSTem:POWEron?")

    def set_screen_saver(self, on: bool) -> None:
        """Enable or disable the display screen-saver."""
        self.write(f":SYSTem:SAVer {'ON' if on else 'OFF'}")

    def get_screen_saver(self) -> bool:
        return self.query_int(":SYSTem:SAVer?") == 1

    # Short → canonical SCPI short-form. The query returns the long
    # form (e.g. ``"ENGLISH"``), which we also accept on input.
    _LANGUAGE_ALIASES = {
        "EN": "EN", "ENGLISH": "EN",
        "CH": "CH", "CHINESE": "CH",
        "DE": "DE", "GERMAN": "DE",
        "ES": "ES", "SPANISH": "ES",
        "FR": "FR", "FRENCH": "FR",
    }

    def set_language(self, language: str) -> None:
        """Set front-panel language: ``"EN"`` / ``"CH"`` / ``"DE"`` / ``"ES"`` / ``"FR"``
        (long forms ``"ENGLISH"`` etc. also accepted)."""
        norm = language.strip().upper()
        scpi = self._LANGUAGE_ALIASES.get(norm)
        if scpi is None:
            raise RigolDP2031ValueError(
                f"language must be one of EN / CH / DE / ES / FR (or long form), "
                f"got {language!r}"
            )
        self.write(f":SYSTem:LANGuage:TYPE {scpi}")

    def get_language(self) -> str:
        """Returns the device's reply verbatim (typically the long form,
        e.g. ``"ENGLISH"``)."""
        return self.query(":SYSTem:LANGuage:TYPE?")

    # ------------------------------------------------------------------
    # Phase C — channel pair, tracking, sync, remote sense, sampling,
    # voltage/current step, APPLy convenience, bounds queries
    # ------------------------------------------------------------------

    _PAIR_MODES = {"OFF": "OFF", "SER": "SERies", "SERIES": "SERies",
                   "PAR": "PARallel", "PARALLEL": "PARallel"}

    def set_channel_pair(self, mode: str) -> None:
        """Set the CH1+CH2 internal-pair mode.

        Accepts ``"OFF"`` / ``"SERies"`` / ``"PARallel"`` (any case;
        short forms ``"SER"`` / ``"PAR"`` also accepted).

        SAFETY: ``"SERies"`` ties CH1+ to CH2- internally for up to
        64 V composite. ``"PARallel"`` ties CH1 and CH2 in parallel
        for 6 A composite. Disconnect any external load between CH1
        and CH2 before switching pair modes — see manual safety notes.

        NOTE: bench-discovered on this firmware (01.00.01.00.16),
        the ``PARallel`` mode write is silently rejected — the
        ``:OUTPut:PAIR?`` query returns ``"OFF"`` afterwards. Use
        :py:meth:`get_channel_pair` after the call to verify the
        device accepted it.
        """
        norm = mode.strip().upper()
        scpi = self._PAIR_MODES.get(norm)
        if scpi is None:
            raise RigolDP2031ValueError(
                f"channel-pair mode must be OFF / SERies / PARallel, got {mode!r}"
            )
        self.write(f":OUTPut:PAIR {scpi}")

    def get_channel_pair(self) -> str:
        """Returns ``"OFF"`` / ``"SERIES"`` / ``"PARALLEL"`` (the
        device's verbatim reply, normalised to upper case)."""
        return self.query(":OUTPut:PAIR?").strip().upper()

    def set_tracking(self, on: bool) -> None:
        """Enable CH1 ↔ CH2 voltage tracking (``:OUTPut:TRACk``).

        When tracking is enabled, changing CH1's voltage setpoint
        automatically updates CH2 to match (and vice versa). The
        manual documents this as equivalent to
        ``:SYSTem:TMODe SYNCHRONOUS`` (see
        :py:meth:`set_track_mode`).
        """
        self.write(f":OUTPut:TRACk {'ON' if on else 'OFF'}")

    def get_tracking(self) -> bool:
        return self.query_int(":OUTPut:TRACk?") == 1

    _TMODE_ALIASES = {
        "SYNC": "SYNC", "SYNCHRONOUS": "SYNC",
        "INDE": "INDE", "INDEPENDENT": "INDE",
    }

    def set_track_mode(self, mode: str) -> None:
        """Set the CH1↔CH2 track mode via ``:SYSTem:TMODe``.

        Accepts ``"SYNC"`` / ``"SYNCHRONOUS"`` (links CH1+CH2) or
        ``"INDE"`` / ``"INDEPENDENT"`` (decoupled). Functionally
        equivalent to :py:meth:`set_tracking` — the device exposes
        both wire forms for compatibility.
        """
        norm = mode.strip().upper()
        scpi = self._TMODE_ALIASES.get(norm)
        if scpi is None:
            raise RigolDP2031ValueError(
                f"track mode must be SYNC / INDE (or SYNCHRONOUS / "
                f"INDEPENDENT), got {mode!r}"
            )
        self.write(f":SYSTem:TMODe {scpi}")

    def get_track_mode(self) -> str:
        """Returns the device's reply verbatim, typically the long form
        ``"SYNCHRONOUS"`` or ``"INDEPENDENT"``."""
        return self.query(":SYSTem:TMODe?").strip().upper()

    def set_output_sync(self, on: bool) -> None:
        """Enable simultaneous CH1+CH2 output on/off via ``:SYSTem:SYNC``.

        When sync is on, enabling either channel enables both. This is
        meaningful in conjunction with tracking mode.
        """
        self.write(f":SYSTem:SYNC {'ON' if on else 'OFF'}")

    def get_output_sync(self) -> bool:
        return self.query_int(":SYSTem:SYNC?") == 1

    # Remote sense (4-wire)

    def set_remote_sense(self, channel, on: bool) -> None:
        """Enable or disable 4-wire remote sense for a channel.

        Accepts ``DP2031Channel`` / int 1/2/3, or the literal string
        ``"ALL"`` to apply to every channel in one wire command.
        """
        sel = _coerce_channel_or_all(channel)
        self.write(f":SYSTem:SENSe {sel},{'ON' if on else 'OFF'}")

    def get_remote_sense(self, channel: ChannelLike) -> bool:
        """Read the 4-wire sense state for one channel. Per the manual
        the query form requires an explicit channel (no ALL on read)."""
        ch = _coerce_channel(channel)
        return self.query_int(f":SYSTem:SENSe? CH{ch}") == 1

    # Sampling mode (CH1/CH2 low-current measurement extension)

    _SAMPLING_MODES = ("AUTO", "HIGH", "LOW")

    def set_sampling_mode(self, mode: str) -> None:
        """Set the current-measurement sampling mode for CH1/CH2.

        ``"AUTO"`` (default) auto-ranges between high (~mA / A) and
        low (sub-mA, 1 µA resolution below ~11 mA) ranges. ``"HIGH"``
        forces high range; ``"LOW"`` forces low range. Has no effect
        on CH3 (single range). In PARallel mode the device forces
        HIGH and rejects writes.
        """
        norm = mode.strip().upper()
        if norm not in self._SAMPLING_MODES:
            raise RigolDP2031ValueError(
                f"sampling mode must be AUTO / HIGH / LOW, got {mode!r}"
            )
        self.write(f":SYSTem:SAMPling {norm}")

    def get_sampling_mode(self) -> str:
        return self.query(":SYSTem:SAMPling?").strip().upper()

    # Voltage / current step + UP / DOWN

    def set_voltage_step(self, channel: ChannelLike, volts: float) -> None:
        """Set the step increment for ``:SOURce<n>:VOLTage UP|DOWN``.

        Range: same envelope as the voltage setpoint (step can't
        exceed the channel's max voltage). The default step on this
        firmware after ``*RST`` is 0.1 V on CH1/CH2 and 0.01 V on CH3.
        """
        ch = _coerce_channel(channel)
        _validate(volts, _CHANNEL_LIMITS[ch]["v"], f"voltage step (CH{ch})")
        self.write(f":SOURce{ch}:VOLTage:LEVel:IMMediate:STEP {volts:.6f}")

    def get_voltage_step(self, channel: ChannelLike) -> float:
        ch = _coerce_channel(channel)
        return self.query_float(
            f":SOURce{ch}:VOLTage:LEVel:IMMediate:STEP?"
        )

    def set_current_step(self, channel: ChannelLike, amps: float) -> None:
        """Set the step increment for ``:SOURce<n>:CURRent UP|DOWN``."""
        ch = _coerce_channel(channel)
        _validate(amps, _CHANNEL_LIMITS[ch]["i"], f"current step (CH{ch})")
        self.write(f":SOURce{ch}:CURRent:LEVel:IMMediate:STEP {amps:.6f}")

    def get_current_step(self, channel: ChannelLike) -> float:
        ch = _coerce_channel(channel)
        return self.query_float(
            f":SOURce{ch}:CURRent:LEVel:IMMediate:STEP?"
        )

    def step_voltage_up(self, channel: ChannelLike) -> None:
        """Increment CHn voltage by the current step (set via
        :py:meth:`set_voltage_step`)."""
        ch = _coerce_channel(channel)
        self.write(f":SOURce{ch}:VOLTage:LEVel:IMMediate UP")

    def step_voltage_down(self, channel: ChannelLike) -> None:
        ch = _coerce_channel(channel)
        self.write(f":SOURce{ch}:VOLTage:LEVel:IMMediate DOWN")

    def step_current_up(self, channel: ChannelLike) -> None:
        ch = _coerce_channel(channel)
        self.write(f":SOURce{ch}:CURRent:LEVel:IMMediate UP")

    def step_current_down(self, channel: ChannelLike) -> None:
        ch = _coerce_channel(channel)
        self.write(f":SOURce{ch}:CURRent:LEVel:IMMediate DOWN")

    # APPLy convenience

    def apply(
        self,
        channel: ChannelLike,
        voltage: Optional[float] = None,
        current: Optional[float] = None,
    ) -> None:
        """One-shot V/I configuration via the ``:APPLy`` shorthand.

        Equivalent to :py:meth:`select_channel` + :py:meth:`set_voltage`
        + :py:meth:`set_current`. Omitted arguments leave the current
        setpoint unchanged.
        """
        ch = _coerce_channel(channel)
        if voltage is not None:
            _validate(voltage, _CHANNEL_LIMITS[ch]["v"],
                      f"apply voltage (CH{ch})")
        if current is not None:
            _validate(current, _CHANNEL_LIMITS[ch]["i"],
                      f"apply current (CH{ch})")
        if voltage is None and current is None:
            # bare :APPLy CHn just selects the channel
            self.write(f":APPLy CH{ch}")
        elif current is None:
            self.write(f":APPLy CH{ch},{voltage:.6f}")
        elif voltage is None:
            # APPLy doesn't accept a current-only positional form, so
            # explicitly set the current via the SOURce path. This
            # also matches "leave V alone" semantics.
            self.set_current(ch, current)
        else:
            self.write(f":APPLy CH{ch},{voltage:.6f},{current:.6f}")

    def query_applied(
        self,
        channel: ChannelLike,
        option: Optional[str] = None,
    ) -> Union[tuple[str, float, float], float]:
        """Read the channel's APPLy-form configuration.

        - ``option=None`` → returns ``(rated_string, voltage, current)``
          where ``rated_string`` looks like ``"CH1:32V/3A"``.
        - ``option="VOLT"`` → returns the voltage setpoint as a float.
        - ``option="CURR"`` → returns the current setpoint as a float.
        """
        ch = _coerce_channel(channel)
        if option is None:
            raw = self.query(f":APPLy? CH{ch}")
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) != 3:
                raise RigolDP2031Error(
                    f"expected 'rated,V,I' from :APPLy? CH{ch}, got {raw!r}"
                )
            try:
                return (parts[0], float(parts[1]), float(parts[2]))
            except ValueError as e:
                raise RigolDP2031Error(
                    f"non-numeric V/I in :APPLy? CH{ch} response: {raw!r}"
                ) from e
        norm = option.strip().upper()
        if norm in ("VOLT", "VOLTAGE"):
            return self.query_float(f":APPLy? CH{ch},VOLT")
        if norm in ("CURR", "CURRENT"):
            return self.query_float(f":APPLy? CH{ch},CURR")
        raise RigolDP2031ValueError(
            f"option must be None / 'VOLT' / 'CURR', got {option!r}"
        )

    # Device-reported bounds (alternative to the driver's static envelope)

    def voltage_bounds(self, channel: ChannelLike) -> tuple[float, float, float]:
        """Return ``(min, max, default)`` voltage bounds reported by the device.

        Bench-verified on this firmware: the device's ``MAX`` is the
        nominal envelope plus ~5 % headroom (e.g. 33.6 V on CH1,
        whose nominal is 32 V). The driver's own validation uses the
        nominal envelope; pass setpoints up to the device-reported
        ``MAX`` if you need that headroom and are comfortable bypassing
        the driver's pre-write check.
        """
        ch = _coerce_channel(channel)
        return (
            self.query_float(f":SOURce{ch}:VOLTage? MIN"),
            self.query_float(f":SOURce{ch}:VOLTage? MAX"),
            self.query_float(f":SOURce{ch}:VOLTage? DEF"),
        )

    def current_bounds(self, channel: ChannelLike) -> tuple[float, float, float]:
        """Return ``(min, max, default)`` current bounds reported by the device."""
        ch = _coerce_channel(channel)
        return (
            self.query_float(f":SOURce{ch}:CURRent? MIN"),
            self.query_float(f":SOURce{ch}:CURRent? MAX"),
            self.query_float(f":SOURce{ch}:CURRent? DEF"),
        )

    # ------------------------------------------------------------------
    # Phase D — Timer (Arb sequencer)
    # ------------------------------------------------------------------

    def set_timer_enabled(self, on: bool) -> None:
        """Arm or disarm the Timer generator (``:TIMEr:STATe``)."""
        self.write(f":TIMEr:STATe {'ON' if on else 'OFF'}")

    def get_timer_enabled(self) -> bool:
        return self.query_int(":TIMEr:STATe?") == 1

    def set_timer_channel(self, channel: ChannelLike) -> None:
        """Select which channel the Timer editor (groups + templates) targets."""
        ch = _coerce_channel(channel)
        self.write(f":TIMEr:CHANnel CH{ch}")

    def get_timer_channel(self) -> DP2031Channel:
        s = self.query(":TIMEr:CHANnel?").strip().upper()
        for ch in DP2031Channel:
            if s == f"CH{int(ch)}":
                return ch
        raise RigolDP2031Error(f"unexpected :TIMEr:CHANnel? response: {s!r}")

    def set_timer_cycles(self, count: Optional[int]) -> None:
        """Set how many times the Timer sequence repeats.

        ``count = None`` or ``0`` → infinite (``I``).
        Otherwise an int 1–99999 → ``N, <count>``.
        """
        if count is None or count == 0:
            self.write(":TIMEr:CYCLEs I")
            return
        if not isinstance(count, int) or isinstance(count, bool):
            raise RigolDP2031ValueError(
                f"timer cycles must be int 1–99999 or None/0 for infinite, got {count!r}"
            )
        if count < 1 or count > 99999:
            raise RigolDP2031ValueError(
                f"timer cycles must be 1–99999, got {count}"
            )
        self.write(f":TIMEr:CYCLEs N,{count}")

    def get_timer_cycles(self) -> Optional[int]:
        """Return the cycles setting. ``None`` means infinite."""
        raw = self.query(":TIMEr:CYCLEs?").strip()
        norm = raw.replace(" ", "").upper()
        if norm == "I":
            return None
        # Format: "N,5" (we may also see "N, 5" with whitespace)
        if norm.startswith("N,"):
            try:
                return int(norm[2:])
            except ValueError:
                pass
        raise RigolDP2031Error(f"could not parse :TIMEr:CYCLEs? reply {raw!r}")

    _TIMER_END_STATES = ("OFF", "LAST")

    def set_timer_end_state(self, mode: str) -> None:
        """Set behaviour after the Timer sequence ends: ``"OFF"`` or ``"LAST"``."""
        norm = mode.strip().upper()
        if norm not in self._TIMER_END_STATES:
            raise RigolDP2031ValueError(
                f"timer end state must be OFF or LAST, got {mode!r}"
            )
        self.write(f":TIMEr:ENDState {norm}")

    def get_timer_end_state(self) -> str:
        return self.query(":TIMEr:ENDState?").strip().upper()

    _TIMER_RUN_MODES = {
        "CONT": "CONTinue", "CONTINUE": "CONTinue",
        "SING": "SINGle", "SINGLE": "SINGle",
    }

    def set_timer_run_mode(self, mode: str) -> None:
        """Set Timer execution mode: ``"CONTinue"`` or ``"SINGle"``."""
        scpi = self._TIMER_RUN_MODES.get(mode.strip().upper())
        if scpi is None:
            raise RigolDP2031ValueError(
                f"timer run mode must be CONTinue or SINGle, got {mode!r}"
            )
        self.write(f":TIMEr:RUN {scpi}")

    def get_timer_run_mode(self) -> str:
        return self.query(":TIMEr:RUN?").strip().upper()

    _TIMER_TRIGGER_SOURCES = {
        "MAN": "MANual", "MANUAL": "MANual",
        "BUS": "BUS",
    }

    def set_timer_trigger(self, source: str) -> None:
        """Set Timer trigger source: ``"MANual"`` (front-panel / *TRG) or ``"BUS"``."""
        scpi = self._TIMER_TRIGGER_SOURCES.get(source.strip().upper())
        if scpi is None:
            raise RigolDP2031ValueError(
                f"timer trigger must be MANual or BUS, got {source!r}"
            )
        self.write(f":TIMEr:TRIG {scpi}")

    def get_timer_trigger(self) -> str:
        return self.query(":TIMEr:TRIG?").strip().upper()

    def set_timer_group_index(self, index: int) -> None:
        """Position the Timer editor on group ``index`` (1-based, ≤ 512)."""
        if not isinstance(index, int) or isinstance(index, bool):
            raise RigolDP2031ValueError(f"group index must be int, got {index!r}")
        if index < 1 or index > 512:
            raise RigolDP2031ValueError(f"group index must be 1–512, got {index}")
        self.write(f":TIMEr:GROUP:INDEx {index}")

    def get_timer_group_index(self) -> int:
        return self.query_int(":TIMEr:GROUP:INDEx?")

    def set_timer_group_params(
        self, voltage: float, current: float, dwell_s: float,
    ) -> None:
        """Write V / I / dwell into the currently-selected group.

        Validates against the channel envelope of whichever channel the
        Timer is currently targeting (call :py:meth:`set_timer_channel`
        first). Dwell range is 0.001 – 3600 s.
        """
        ch = int(self.get_timer_channel())
        _validate(voltage, _CHANNEL_LIMITS[ch]["v"], f"timer V (CH{ch})")
        _validate(current, _CHANNEL_LIMITS[ch]["i"], f"timer I (CH{ch})")
        if not isinstance(dwell_s, (int, float)) or isinstance(dwell_s, bool):
            raise RigolDP2031ValueError(
                f"timer dwell must be numeric, got {dwell_s!r}"
            )
        if dwell_s < 0.001 or dwell_s > 3600:
            raise RigolDP2031ValueError(
                f"timer dwell must be 0.001–3600 s, got {dwell_s}"
            )
        self.write(
            f":TIMEr:GROUP:PARAmeter {voltage:.6f},{current:.6f},{dwell_s:.6f}"
        )

    def get_timer_group_params(
        self, count: int = 1,
    ) -> list[tuple[int, float, float, float]]:
        """Read back up to ``count`` Timer groups starting at the current index.

        Returns a list of ``(index, voltage, current, dwell_s)`` tuples.
        The device replies in IEEE 488.2 arbitrary-block format
        (``#NX...X<payload>``); :py:func:`_query_block_payload` strips
        the header.
        """
        if count < 1:
            raise RigolDP2031ValueError(f"count must be ≥ 1, got {count}")
        raw = self.query(f":TIMEr:GROUP:PARAmeter? {count}")
        payload = _query_block_payload(raw)
        return _parse_timer_group_payload(payload)

    def delete_timer_groups(self, count: int = 1) -> None:
        """Delete ``count`` Timer groups starting at the current index."""
        if count < 1:
            raise RigolDP2031ValueError(f"count must be ≥ 1, got {count}")
        self.write(f":TIMEr:GROUP:DELete {count}")

    # ---------------- Timer templates (auto-construct groups) ----------------

    _TIMER_TEMPLATES = (
        "SINE", "PULSE", "RAMP",
        "UP", "DN", "UPDN",
        "RISE", "FALL",
    )

    def set_timer_template(self, template: str) -> None:
        """Pick the Timer template shape.

        Accepts: ``SINE``, ``PULSE``, ``RAMP``, ``UP``, ``DN``, ``UPDN``,
        ``RISE``, ``FALL``.
        """
        norm = template.strip().upper()
        if norm not in self._TIMER_TEMPLATES:
            raise RigolDP2031ValueError(
                f"template must be one of {self._TIMER_TEMPLATES}, got {template!r}"
            )
        self.write(f":TIMEr:TEMPlet:SELect {norm}")

    def get_timer_template(self) -> str:
        return self.query(":TIMEr:TEMPlet:SELect?").strip().upper()

    def construct_timer_from_template(self) -> None:
        """Render the configured template into the Timer group editor."""
        self.write(":TIMEr:TEMPlet:CONSTruct")

    _TEMPLATE_OBJECTS = {"V": "V", "C": "C", "VOLT": "V", "CURR": "C"}

    def set_timer_template_object(
        self, obj: str, paired_value: Optional[float] = None,
    ) -> None:
        """Pick which dimension the template varies (V or C) and the
        constant value of the paired dimension."""
        norm = self._TEMPLATE_OBJECTS.get(obj.strip().upper())
        if norm is None:
            raise RigolDP2031ValueError(
                f"template object must be V or C, got {obj!r}"
            )
        if paired_value is None:
            self.write(f":TIMEr:TEMPlet:OBJect {norm}")
        else:
            self.write(f":TIMEr:TEMPlet:OBJect {norm},{paired_value:.6f}")

    def get_timer_template_object(self) -> str:
        return self.query(":TIMEr:TEMPlet:OBJect?").strip().upper()

    def set_timer_template_max(self, value: float) -> None:
        self.write(f":TIMEr:TEMPlet:MAXValue {value:.6f}")

    def get_timer_template_max(self) -> float:
        return self.query_float(":TIMEr:TEMPlet:MAXValue?")

    def set_timer_template_min(self, value: float) -> None:
        self.write(f":TIMEr:TEMPlet:MINValue {value:.6f}")

    def get_timer_template_min(self) -> float:
        return self.query_float(":TIMEr:TEMPlet:MINValue?")

    def set_timer_template_period(self, seconds: float) -> None:
        if seconds < 0.001 or seconds > 3600:
            raise RigolDP2031ValueError(
                f"template period must be 0.001–3600 s, got {seconds}"
            )
        self.write(f":TIMEr:TEMPlet:PERIod {seconds:.6f}")

    def get_timer_template_period(self) -> float:
        return self.query_float(":TIMEr:TEMPlet:PERIod?")

    def set_timer_template_points(self, points: int) -> None:
        if not isinstance(points, int) or isinstance(points, bool):
            raise RigolDP2031ValueError(f"points must be int, got {points!r}")
        if points < 1 or points > 512:
            raise RigolDP2031ValueError(f"points must be 1–512, got {points}")
        self.write(f":TIMEr:TEMPlet:POINTs {points}")

    def get_timer_template_points(self) -> int:
        return self.query_int(":TIMEr:TEMPlet:POINTs?")

    # ---------------- Timer convenience: program in one call --------------

    def program_timer(
        self,
        channel: ChannelLike,
        steps: list[tuple[float, float, float]],
        *,
        cycles: Optional[int] = 1,
        end_state: str = "OFF",
        run_mode: str = "CONTinue",
        trigger: str = "MANual",
    ) -> None:
        """Program a complete Timer sequence on ``channel`` in one call.

        Each step is ``(voltage_V, current_A, dwell_s)``. Steps are
        validated against the channel envelope and dwell limits before
        any wire writes happen. After programming, the Timer is left
        disarmed — caller must :py:meth:`set_timer_enabled` and (for
        BUS trigger source) :py:meth:`mark_op_complete` / ``*TRG`` /
        :py:meth:`set_output` separately.

        Mirrors the DL3031A driver's ``program_list()`` shape.
        """
        ch = _coerce_channel(channel)
        if not steps:
            raise RigolDP2031ValueError("steps must contain at least one entry")
        if len(steps) > 512:
            raise RigolDP2031ValueError(
                f"timer supports 1–512 steps, got {len(steps)}"
            )
        # Pre-validate every step before we touch the wire
        for i, step in enumerate(steps, 1):
            if len(step) != 3:
                raise RigolDP2031ValueError(
                    f"step {i} must be (V, I, dwell_s), got {step!r}"
                )
            v, current, t = step
            _validate(v, _CHANNEL_LIMITS[ch]["v"], f"step {i} V (CH{ch})")
            _validate(current, _CHANNEL_LIMITS[ch]["i"],
                      f"step {i} I (CH{ch})")
            if not isinstance(t, (int, float)) or isinstance(t, bool):
                raise RigolDP2031ValueError(
                    f"step {i} dwell must be numeric, got {t!r}"
                )
            if t < 0.001 or t > 3600:
                raise RigolDP2031ValueError(
                    f"step {i} dwell must be 0.001–3600 s, got {t}"
                )
        # Disarm before editing
        self.set_timer_enabled(False)
        self.set_timer_channel(ch)
        # Write each group
        for idx, (v, current, t) in enumerate(steps, 1):
            self.set_timer_group_index(idx)
            # Bypass set_timer_group_params' channel-query (we already
            # know the channel; saves a round-trip per step)
            self.write(
                f":TIMEr:GROUP:PARAmeter {v:.6f},{current:.6f},{t:.6f}"
            )
        self.set_timer_cycles(cycles)
        self.set_timer_end_state(end_state)
        self.set_timer_run_mode(run_mode)
        self.set_timer_trigger(trigger)

    # ------------------------------------------------------------------
    # Phase D — Analyzer (power / IoT energy capture)
    # ------------------------------------------------------------------

    def set_analyzer_enabled(self, on: bool) -> None:
        self.write(f":ANALyzer:STATe {'ON' if on else 'OFF'}")

    def get_analyzer_enabled(self) -> bool:
        return self.query_int(":ANALyzer:STATe?") == 1

    _ANALYZER_TYPES = {"COM": "COM", "COMMON": "COM",
                       "CURR": "CURR", "CURRENT": "CURR"}

    def set_analyzer_type(self, type_: str) -> None:
        """Analyzer mode: ``"COM"`` (common — selects V/I/P per channel) or
        ``"CURR"`` (pulse-current analysis)."""
        scpi = self._ANALYZER_TYPES.get(type_.strip().upper())
        if scpi is None:
            raise RigolDP2031ValueError(
                f"analyzer type must be COM or CURR, got {type_!r}"
            )
        self.write(f":ANALyzer:TYPE {scpi}")

    def get_analyzer_type(self) -> str:
        return self.query(":ANALyzer:TYPE?").strip().upper()

    _ANALYZER_COMMON_OBJECTS = {
        "CH1_V", "CH1_C", "CH1_P",
        "CH2_V", "CH2_C", "CH2_P",
        "CH3_V", "CH3_C", "CH3_P",
    }

    def set_analyzer_common_objects(self, *objects: str) -> None:
        """In COM mode, select 1–3 channel/quantity objects to capture.

        Each object is one of ``CH1_V``, ``CH1_C``, ``CH1_P``, ``CH2_V``,
        ``CH2_C``, ``CH2_P``, ``CH3_V``, ``CH3_C``, ``CH3_P``.

        BENCH-OBSERVED FIRMWARE BUG (DP2031 FW 01.00.01.00.16):
        writing to this command over USB-TMC causes the device's
        VISA interface to return ``VI_ERROR_SYSTEM_ERROR``. The
        wire-form is per-spec; the device-side handler is broken.
        Tracked separately; the front-panel UI for this feature
        works correctly.
        """
        if not 1 <= len(objects) <= 3:
            raise RigolDP2031ValueError(
                f"analyzer COM mode takes 1–3 objects, got {len(objects)}"
            )
        normed = []
        for obj in objects:
            norm = obj.strip().upper()
            if norm not in self._ANALYZER_COMMON_OBJECTS:
                raise RigolDP2031ValueError(
                    f"analyzer object must be CHx_{{V,C,P}}, got {obj!r}"
                )
            normed.append(norm)
        self.write(":ANALyzer:COMMon:MEASure:TYPE " + ",".join(normed))

    def get_analyzer_common_objects(self) -> list[str]:
        raw = self.query(":ANALyzer:COMMon:MEASure:TYPE?").strip()
        if not raw:
            return []
        return [p.strip().upper() for p in raw.split(",") if p.strip()]

    def set_analyzer_save(self, on: bool) -> None:
        """Enable / disable the analyzer's data-log-to-file feature."""
        self.write(f":ANALyzer:SAVE:STATe {'ON' if on else 'OFF'}")

    def get_analyzer_save(self) -> bool:
        return self.query_int(":ANALyzer:SAVE:STATe?") == 1

    def set_analyzer_save_path(self, path: str) -> None:
        """Set the analyzer log-file path (e.g. ``"C:/RA.ROF"``)."""
        self.write(f":ANALyzer:SAVE:ROUTe {path}")

    def get_analyzer_save_path(self) -> str:
        return self.query(":ANALyzer:SAVE:ROUTe?").strip()

    # ------------------------------------------------------------------
    # Phase D — Trigger I/O (D1-D4 rear digital lines)
    # ------------------------------------------------------------------

    _TRIGGER_LINES = ("D1", "D2", "D3", "D4")
    _TRIGGER_IN_TYPES = ("RISE", "FALL", "HIGH", "LOW")
    _TRIGGER_IN_RESPONSES = ("ON", "OFF", "ALTER")
    _TRIGGER_OUT_POLARITIES = {"POS": "POSitive", "POSITIVE": "POSitive",
                               "NEG": "NEGative", "NEGATIVE": "NEGative"}

    def _coerce_trigger_line(self, line: str) -> str:
        norm = line.strip().upper()
        if norm not in self._TRIGGER_LINES:
            raise RigolDP2031ValueError(
                f"trigger line must be D1/D2/D3/D4, got {line!r}"
            )
        return norm

    def set_trigger_in_enabled(self, line: str, on: bool) -> None:
        d = self._coerce_trigger_line(line)
        self.write(f":TRIGger:IN:ENABle {d},{'ON' if on else 'OFF'}")

    def get_trigger_in_enabled(self, line: str) -> bool:
        d = self._coerce_trigger_line(line)
        return self.query_int(f":TRIGger:IN:ENABle? {d}") == 1

    def set_trigger_in_type(self, line: str, type_: str) -> None:
        """RISE / FALL / HIGH / LOW."""
        d = self._coerce_trigger_line(line)
        norm = type_.strip().upper()
        if norm not in self._TRIGGER_IN_TYPES:
            raise RigolDP2031ValueError(
                f"trigger type must be RISE/FALL/HIGH/LOW, got {type_!r}"
            )
        self.write(f":TRIGger:IN:TYPE {d},{norm}")

    def get_trigger_in_type(self, line: str) -> str:
        d = self._coerce_trigger_line(line)
        return self.query(f":TRIGger:IN:TYPE? {d}").strip().upper()

    def set_trigger_in_source(self, line: str, channels) -> None:
        """Set which channels respond to the trigger input on ``line``.

        ``channels`` is a list of :py:class:`DP2031Channel` / int 1-3
        with ≥ 1 entry. To "clear" the source (make the trigger
        ineffective), use :py:meth:`set_trigger_in_enabled` with
        ``on=False``; the device's firmware rejects writes of
        ``NONE`` to this field with SCPI error -141.
        """
        d = self._coerce_trigger_line(line)
        if isinstance(channels, str):
            raise RigolDP2031ValueError(
                f"trigger source must be a list of channels, not a string; "
                f"to disable use set_trigger_in_enabled({line!r}, False)"
            )
        coerced = [f"CH{_coerce_channel(c)}" for c in channels]
        if not coerced:
            raise RigolDP2031ValueError(
                "trigger source must include ≥ 1 channel; "
                "to disable use set_trigger_in_enabled(line, False)"
            )
        self.write(f":TRIGger:IN:SOURce {d}," + ",".join(coerced))

    def get_trigger_in_source(self, line: str) -> list[str]:
        """Return the source channels as a list of ``"CH1"`` / ``"CH2"`` /
        ``"CH3"`` strings, or an empty list when source is ``NONE``."""
        d = self._coerce_trigger_line(line)
        raw = self.query(f":TRIGger:IN:SOURce? {d}").strip().upper()
        if not raw or raw == "NONE":
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def set_trigger_in_response(self, line: str, response: str) -> None:
        """ON / OFF / ALTER (toggle)."""
        d = self._coerce_trigger_line(line)
        norm = response.strip().upper()
        if norm not in self._TRIGGER_IN_RESPONSES:
            raise RigolDP2031ValueError(
                f"trigger response must be ON/OFF/ALTER, got {response!r}"
            )
        self.write(f":TRIGger:IN:RESPonse {d},{norm}")

    def get_trigger_in_response(self, line: str) -> str:
        d = self._coerce_trigger_line(line)
        return self.query(f":TRIGger:IN:RESPonse? {d}").strip().upper()

    def trigger_in_immediate(self) -> None:
        """Fire an immediate trigger event regardless of input line state."""
        self.write(":TRIGger:IN:IMMEdiate")

    def set_trigger_out_enabled(self, line: str, on: bool) -> None:
        d = self._coerce_trigger_line(line)
        self.write(f":TRIGger:OUT:ENABle {d},{'ON' if on else 'OFF'}")

    def get_trigger_out_enabled(self, line: str) -> bool:
        d = self._coerce_trigger_line(line)
        return self.query_int(f":TRIGger:OUT:ENABle? {d}") == 1

    def set_trigger_out_source(self, line: str, channel: ChannelLike) -> None:
        """Single channel — the trigger output fires when that channel
        changes state."""
        d = self._coerce_trigger_line(line)
        ch = _coerce_channel(channel)
        self.write(f":TRIGger:OUT:SOURce {d},CH{ch}")

    def get_trigger_out_source(self, line: str) -> str:
        d = self._coerce_trigger_line(line)
        return self.query(f":TRIGger:OUT:SOURce? {d}").strip().upper()

    def set_trigger_out_polarity(self, line: str, polarity: str) -> None:
        """POSitive or NEGative."""
        d = self._coerce_trigger_line(line)
        scpi = self._TRIGGER_OUT_POLARITIES.get(polarity.strip().upper())
        if scpi is None:
            raise RigolDP2031ValueError(
                f"polarity must be POSitive or NEGative, got {polarity!r}"
            )
        self.write(f":TRIGger:OUT:POLArity {d},{scpi}")

    def get_trigger_out_polarity(self, line: str) -> str:
        d = self._coerce_trigger_line(line)
        return self.query(f":TRIGger:OUT:POLArity? {d}").strip().upper()

    # ------------------------------------------------------------------
    # Phase D — Memory / file system (internal C disk + USB)
    # ------------------------------------------------------------------

    def list_files(self) -> list[str]:
        """Return the current directory's filenames as a list."""
        raw = self.query(":MEMory:CATalog?").strip()
        if not raw:
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def change_directory(self, path: str) -> None:
        self.write(f":MEMory:CDIRectory {path}")

    def current_directory(self) -> str:
        return self.query(":MEMory:CDIRectory?").strip()

    def make_directory(self, name: str) -> None:
        """Create a subdirectory. Note: C disk doesn't support folders —
        use the external USB disks (D:/, E:/)."""
        self.write(f":MEMory:MDIRectory {name}")

    def delete_file(self, filename: str) -> None:
        self.write(f":MEMory:DELete {filename}")

    def store_file(self, filename: str) -> None:
        """Save the current state to a file (``.RSF`` for state files,
        ``.RTF`` for Arb)."""
        self.write(f":MEMory:STORe {filename}")

    def load_file(self, filename: str) -> None:
        """Load device state or Arb sequence from a file."""
        self.write(f":MEMory:LOAD {filename}")

    def external_disks(self) -> list[str]:
        """Return mounted external USB disk roots (e.g. ``["D:/"]``)."""
        raw = self.query(":MEMory:DISK?").strip()
        if not raw or raw.upper() == "NONE":
            return []
        return [p.strip() for p in raw.split(",") if p.strip()]

    def set_file_locked(self, filename: str, locked: bool) -> None:
        """Lock or unlock a file on the C disk (USB disks don't support lock)."""
        self.write(f":MEMory:LOCK {filename},{'ON' if locked else 'OFF'}")

    def get_file_locked(self, filename: str) -> bool:
        return self.query_int(f":MEMory:LOCK? {filename}") == 1

    def file_exists(self, filename: str) -> bool:
        return self.query_int(f":MEMory:VALid? {filename}") == 1

    # ------------------------------------------------------------------
    # Phase D — License install + screenshot
    # ------------------------------------------------------------------

    def install_license(self, license_key: str) -> None:
        """Install an option license key.

        SAFETY: incorrect or rejected keys produce a SCPI error. Drain
        the error queue after install to detect rejection.
        """
        self.write(f":LIC:SET {license_key}")

    def screenshot_bytes(self) -> bytes:
        """Capture the device's display as a bitmap. Returns raw BMP bytes.

        The reply is wrapped in an IEEE 488.2 arbitrary-block envelope
        (``#NX...X<bmp>``) which we strip before returning, so the
        caller gets a clean BMP byte stream starting with ``b"BM"``.
        We temporarily bump the VISA timeout to 10 s because the
        capture takes several seconds.
        """
        if self._closed:
            raise RigolDP2031ConnectionError("instrument is closed")
        original_timeout = self._inst.timeout
        try:
            self._inst.timeout = max(original_timeout, 10000)
            self._inst.write(":SYSTem:PRINt?")
            data = self._inst.read_raw()
        except Exception as e:
            raise RigolDP2031ConnectionError(
                f"screenshot read failed: {e}"
            ) from e
        finally:
            self._inst.timeout = original_timeout
        return _strip_block_header_bytes(bytes(data))

    def save_screenshot(self, path: str) -> int:
        """Capture and save a BMP screenshot to ``path``. Returns bytes written."""
        data = self.screenshot_bytes()
        from pathlib import Path
        Path(path).write_bytes(data)
        return len(data)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _coerce_channel(channel: ChannelLike) -> int:
    """Validate and convert a channel argument to an int 1/2/3."""
    if isinstance(channel, DP2031Channel):
        return int(channel)
    if isinstance(channel, bool):
        # bool is an int subclass — reject explicitly to avoid
        # `set_voltage(True, 3.3)` silently meaning channel 1.
        raise RigolDP2031ValueError(
            f"channel must be 1, 2, or 3 (or a DP2031Channel); got bool {channel!r}"
        )
    if isinstance(channel, int) and channel in (1, 2, 3):
        return channel
    raise RigolDP2031ValueError(
        f"channel must be 1, 2, or 3 (or a DP2031Channel); got {channel!r}"
    )


def _validate(value: float, bounds: tuple[float, float], label: str) -> None:
    """Raise RigolDP2031ValueError if value is outside [min, max]."""
    if not isinstance(value, (int, float)) or isinstance(value, bool):
        raise RigolDP2031ValueError(f"{label} must be numeric, got {value!r}")
    lo, hi = bounds
    if value < lo or value > hi:
        raise RigolDP2031ValueError(
            f"{label} must be in [{lo}, {hi}], got {value}"
        )


def _coerce_channel_or_all(channel) -> str:
    """Validate and convert a channel argument that may also be the
    literal ``"ALL"`` (case-insensitive). Returns the SCPI form
    (``"CH1"`` / ``"CH2"`` / ``"CH3"`` / ``"ALL"``).
    """
    if isinstance(channel, str) and channel.strip().upper() == "ALL":
        return "ALL"
    return f"CH{_coerce_channel(channel)}"


def _validate_mask(value: int, label: str, *, maximum: int = 255) -> None:
    """Raise RigolDP2031ValueError if value isn't a non-negative int ≤ maximum."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RigolDP2031ValueError(f"{label} mask must be int, got {value!r}")
    if value < 0 or value > maximum:
        raise RigolDP2031ValueError(
            f"{label} mask must be 0–{maximum}, got {value}"
        )


def _strip_block_header_bytes(data: bytes) -> bytes:
    """Strip an IEEE 488.2 arbitrary-block header from binary data.

    Format: ``#NX...X<payload>`` where the leading ``#`` is byte 0x23
    (``b'#'``), ``N`` is a single ASCII digit (1-9), and ``X...X`` is
    ``N`` ASCII digits giving the payload length in bytes.

    If the data doesn't start with ``b'#'`` it's returned as-is (some
    transports may already strip the header). Trailing terminator
    bytes (newline / null) are also stripped from the tail.
    """
    if not data.startswith(b"#"):
        return data.rstrip(b"\x00\r\n")
    if len(data) < 2:
        return data
    try:
        n_digits = int(chr(data[1]))
    except ValueError:
        return data
    header_end = 2 + n_digits
    if len(data) < header_end:
        return data
    try:
        payload_len = int(data[2:header_end].decode("ascii"))
    except (ValueError, UnicodeDecodeError):
        return data
    payload = data[header_end:header_end + payload_len]
    return payload


def _query_block_payload(raw: str) -> str:
    """Strip an IEEE 488.2 arbitrary-block header from a query reply.

    Block format: ``#NX...X<payload>`` where ``N`` is a single digit
    (1-9) indicating how many digits the length field has, ``X...X`` is
    the length in characters, and ``<payload>`` is the payload itself.
    Some firmware appends a trailing NUL byte or newline which is
    stripped.

    Returns just the payload string. Raises :py:class:`RigolDP2031Error`
    if the input doesn't start with ``#`` or the length header is
    malformed.
    """
    s = raw
    # Strip trailing whitespace + nulls
    s = s.rstrip("\x00\r\n\t ")
    if not s.startswith("#"):
        raise RigolDP2031Error(
            f"expected IEEE 488.2 block reply starting with '#', got {raw!r}"
        )
    if len(s) < 2:
        raise RigolDP2031Error(f"block reply too short: {raw!r}")
    try:
        n_digits = int(s[1])
    except ValueError as e:
        raise RigolDP2031Error(
            f"block reply length-digit must be 0-9, got {s[1]!r}"
        ) from e
    header_end = 2 + n_digits
    if len(s) < header_end:
        raise RigolDP2031Error(
            f"block reply too short for declared header: {raw!r}"
        )
    try:
        payload_len = int(s[2:header_end])
    except ValueError as e:
        raise RigolDP2031Error(
            f"block reply length field must be numeric, got {s[2:header_end]!r}"
        ) from e
    payload = s[header_end:header_end + payload_len]
    if len(payload) < payload_len:
        # Some firmware (DP2031 included) appends a NUL inside the
        # payload window; tolerate by returning what we got.
        pass
    return payload


def _parse_timer_group_payload(
    payload: str,
) -> list[tuple[int, float, float, float]]:
    """Parse the inner payload of `:TIMEr:GROUP:PARAmeter?`.

    Format: ``index,V,I,t;index,V,I,t;...`` with optional trailing
    semicolon. Returns a list of ``(index, voltage, current, dwell_s)``
    tuples.
    """
    out: list[tuple[int, float, float, float]] = []
    for chunk in payload.split(";"):
        chunk = chunk.strip().strip("\x00")
        if not chunk:
            continue
        parts = chunk.split(",")
        if len(parts) != 4:
            raise RigolDP2031Error(
                f"timer group chunk must be 'idx,V,I,t', got {chunk!r}"
            )
        try:
            out.append((
                int(parts[0]),
                float(parts[1]),
                float(parts[2]),
                float(parts[3]),
            ))
        except ValueError as e:
            raise RigolDP2031Error(
                f"non-numeric value in timer group chunk {chunk!r}"
            ) from e
    return out


def _parse_delay_ms(raw: str) -> int:
    """Parse ``:OUTPut:OCP:DELay?`` style response.

    The device returns the value with a ``ms`` suffix (e.g. ``"200ms"``)
    on this firmware. Some firmware revs may emit a plain number; we
    accept both.
    """
    s = raw.strip().lower()
    # Strip optional trailing unit
    for suffix in ("ms",):
        if s.endswith(suffix):
            s = s[: -len(suffix)].strip()
            break
    try:
        return int(float(s))
    except ValueError as e:
        raise RigolDP2031Error(
            f"could not parse OCP delay reply {raw!r}"
        ) from e


def _autodiscover(rm) -> str:
    """The VISA resource string for the one attached DP2000-family supply.

    Delegates to :py:func:`benchctrl.discovery.visa_resource_for`, which parses
    the resource's ``::`` fields instead of substring-matching a hex VID/PID.
    This function used to do the latter, and it made the supply **invisible to
    its own driver** on any board using pyvisa-py: that backend renders the same
    device as ``USB0::6833::42152::DP2A243500269::0::INSTR``, so a search for the
    text ``0x1ab1`` found nothing and the error listed the very resource it had
    just rejected. Bench-verified on the Uno Q. See that function for the full
    reasoning; the identical bug was fixed in the SDM4065A driver first.

    ``rm`` is passed through and must be: ``pyvisa.ResourceManager()`` is a
    singleton, so a scan that made its own handle and closed it would close this
    one, and the caller's next ``open_resource`` would fail with
    ``InvalidSession``. That is not hypothetical — it is what happened on the
    bench board between the two halves of this fix.
    """
    from benchctrl import discovery

    return discovery.visa_resource_for(
        "rigol_dp2031", error=RigolDP2031ConnectionError, resource_manager=rm
    )

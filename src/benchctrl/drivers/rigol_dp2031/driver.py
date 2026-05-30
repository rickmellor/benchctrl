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


def _validate_mask(value: int, label: str, *, maximum: int = 255) -> None:
    """Raise RigolDP2031ValueError if value isn't a non-negative int ≤ maximum."""
    if not isinstance(value, int) or isinstance(value, bool):
        raise RigolDP2031ValueError(f"{label} mask must be int, got {value!r}")
    if value < 0 or value > maximum:
        raise RigolDP2031ValueError(
            f"{label} mask must be 0–{maximum}, got {value}"
        )


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
    """Scan VISA resources for a Rigol DP2000-family USB device.

    Returns the resource string if exactly one DP2000 is found. If
    multiple are connected, raises with the candidate list so the
    caller can pick explicitly.
    """
    resources = sorted(rm.list_resources())
    vid = f"0x{RIGOL_USB_VID:04X}".lower()
    pid = f"0x{DP2000_USB_PID:04X}".lower()
    matches = [r for r in resources
               if "usb" in r.lower() and vid in r.lower() and pid in r.lower()]
    if len(matches) == 1:
        return matches[0]
    if len(matches) > 1:
        raise RigolDP2031ConnectionError(
            f"multiple Rigol DP2000 devices found — pass an explicit resource "
            f"string. Candidates: {matches}"
        )
    raise RigolDP2031ConnectionError(
        f"no Rigol DP2000 (VID 0x{RIGOL_USB_VID:04X} / PID 0x{DP2000_USB_PID:04X}) "
        f"found in VISA resource list: {resources}"
    )

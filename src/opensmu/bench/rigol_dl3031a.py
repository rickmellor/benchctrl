"""Rigol DL3031A programmable DC electronic load driver.

The DL3000 series (DL3021A / DL3031A) is a USB-TMC + LAN + RS232
SCPI-controlled programmable electronic load. The DL3031A model can
sink up to 150 V / 60 A / 350 W and operates in CC, CV, CR, or CP
mode plus several built-in test modes (transient, LIST, battery
discharge, OCP/OPP).

This driver uses **VISA** (via pyvisa) over USB-TMC. A VISA backend
must be installed — NI-VISA, Keysight IO Libraries, or pyvisa-py with
a USB backend like libusb. The Rigol Ultra Sigma installer bundles a
VISA backend that works out of the box.

Typical use::

    from opensmu.bench import RigolDL3031A

    with RigolDL3031A.open() as load:        # auto-discover by VID/PID
        print(load.info())
        load.reset()
        load.set_mode("CC")
        load.set_current_range(6.0)          # 6 A range — low / "MIN"
        load.set_current(0.030)              # 30 mA
        load.set_input(True)
        v = load.measure_voltage()
        i = load.measure_current()
        load.set_input(False)

Safety
------
This is a real electronic load — verify voltage/current limits and
the connected DUT can deliver / withstand what you ask for before
calling ``set_input(True)``. The driver does not enforce DUT-side
ratings.
"""
from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import Optional, Union

log = logging.getLogger("opensmu.bench.rigol_dl3031a")

# Rigol's USB-TMC VID/PID for the DL3000 family.
RIGOL_USB_VID = 0x1AB1
DL3000_USB_PID = 0x0E11

# ---------------------------------------------------------------------------
# Exception hierarchy
# ---------------------------------------------------------------------------


class RigolDLError(RuntimeError):
    """Base class for Rigol DL3031A driver errors."""


class RigolDLConnectionError(RigolDLError):
    """Couldn't open the VISA resource, or lost connection mid-session."""


class RigolDLCommandError(RigolDLError):
    """Device returned a non-zero error from ``:SYSTem:ERRor?``.

    ``code`` is the SCPI error number; ``message`` is the manufacturer's
    human-readable string.
    """

    def __init__(self, code: int, message: str):
        self.code = code
        self.message = message
        super().__init__(f"DL3031A SCPI error {code}: {message}")


class RigolDLValueError(RigolDLError, ValueError):
    """Client-side range / type check failed before sending."""


class RigolDLTimeoutError(RigolDLError, TimeoutError):
    """Device didn't respond to a query within the VISA timeout."""


# ---------------------------------------------------------------------------
# Device info
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RigolDLInfo:
    """Identity read from ``*IDN?``.

    Attributes:
        manufacturer: e.g. ``"RIGOL TECHNOLOGIES"``.
        model: e.g. ``"DL3031A"``.
        serial: e.g. ``"DL3D232300106"``.
        firmware: e.g. ``"00.01.05.00.01"``.
        resource: the VISA resource string this device is bound to.
    """

    manufacturer: str
    model: str
    serial: str
    firmware: str
    resource: str


# Modes accepted by :SOURce:FUNCtion. Map both the SCPI form (CURRent
# / VOLTage / RESistance / POWer) and the friendly two-letter form
# (CC / CV / CR / CP) to the canonical SCPI keyword to send.
_MODE_SET_MAP = {
    "CC": "CURRent",
    "CV": "VOLTage",
    "CR": "RESistance",
    "CP": "POWer",
    "CURR": "CURRent",
    "VOLT": "VOLTage",
    "RES": "RESistance",
    "POW": "POWer",
    "CURRENT": "CURRent",
    "VOLTAGE": "VOLTage",
    "RESISTANCE": "RESistance",
    "POWER": "POWer",
}

# What the device returns from :SOURce:FUNCtion? — already two-letter.
_MODE_QUERY_RETURNS = {"CC", "CV", "CR", "CP"}

# Top-level regulation source — controls which subsystem drives the
# load: FIXed (the :SOURce:FUNCtion + setpoint), LIST (programmed
# sequence), WAVe (waveform display), BATTery (battery discharge),
# OCP / OPP (protection tests).
_FUNC_MODE_VALUES = {"FIXed", "FIX", "LIST", "WAVe", "WAV",
                     "BATTery", "BATT", "OCP", "OPP"}

# Transient sub-mode within CC TRANsient: CONTinuous (periodic),
# PULSe (single pulse on trigger), TOGGle (alternate).
_CC_TRAN_MODES = {"CONTinuous", "CONT", "PULSe", "PULS", "TOGGle", "TOGG"}

# LIST step end behavior — per manual {LAST|OFF}:
# - LAST: hold final step's value
# - OFF:  disable the input when the list completes
_LIST_END_VALUES = {"LAST", "OFF"}


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class RigolDL3031A:
    """Control a Rigol DL3031A programmable DC electronic load over VISA.

    Construct via :py:meth:`open` rather than the constructor directly
    so the VISA resource manager lifetime is managed for you.
    """

    # Default per-query timeout (ms). The device usually replies within
    # < 50 ms; we leave plenty of headroom for slow USB hubs.
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
        self._info: Optional[RigolDLInfo] = None
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
    ) -> RigolDL3031A:
        """Open a VISA session to a DL3031A.

        If ``resource`` is None, auto-discovers by scanning USB resources
        for the Rigol DL3000 VID/PID. Pass an explicit VISA resource
        string (e.g. ``"USB0::0x1AB1::0x0E11::DL3D232300106::INSTR"``
        or ``"TCPIP::192.168.1.5::INSTR"``) to target a specific device.
        """
        try:
            import pyvisa
        except ImportError as e:
            raise RigolDLConnectionError(
                "pyvisa is required for the DL3031A driver — "
                "install with `pip install opensmu[bench-visa]`"
            ) from e

        try:
            rm = pyvisa.ResourceManager()
        except Exception as e:
            raise RigolDLConnectionError(
                f"could not initialize VISA resource manager: {e}"
            ) from e

        if resource is None:
            resource = _autodiscover(rm)

        try:
            inst = rm.open_resource(resource)
        except Exception as e:
            raise RigolDLConnectionError(
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

    def __enter__(self) -> RigolDL3031A:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        # Best-effort safety: disable the input on exit so we don't
        # leave the load sinking current if the caller forgets.
        try:
            self.set_input(False)
        except Exception:
            log.debug("set_input(False) failed during __exit__", exc_info=True)
        self.close()

    # ------------------------------------------------------------------
    # Low-level transport
    # ------------------------------------------------------------------

    def write(self, command: str) -> None:
        """Send a SCPI command (no response expected).

        Raises :py:class:`RigolDLConnectionError` on VISA failure.
        """
        if self._closed:
            raise RigolDLConnectionError("instrument is closed")
        try:
            self._inst.write(command)
        except Exception as e:
            raise RigolDLConnectionError(f"write({command!r}) failed: {e}") from e

    def query(self, command: str) -> str:
        """Send a SCPI query and return the trimmed response string."""
        if self._closed:
            raise RigolDLConnectionError("instrument is closed")
        try:
            raw = self._inst.query(command)
        except Exception as e:
            # pyvisa wraps VISA timeouts in VI_ERROR_TMO — try to surface them
            # as RigolDLTimeoutError so callers can catch specifically.
            if "timeout" in str(e).lower() or "VI_ERROR_TMO" in str(e):
                raise RigolDLTimeoutError(
                    f"query({command!r}) timed out"
                ) from e
            raise RigolDLConnectionError(
                f"query({command!r}) failed: {e}"
            ) from e
        return raw.strip()

    def query_float(self, command: str) -> float:
        """Query and parse the response as a float."""
        s = self.query(command)
        try:
            return float(s)
        except ValueError as e:
            raise RigolDLError(
                f"expected number from {command!r}, got {s!r}"
            ) from e

    def query_int(self, command: str) -> int:
        """Query and parse the response as an int."""
        s = self.query(command)
        try:
            return int(s)
        except ValueError as e:
            raise RigolDLError(
                f"expected integer from {command!r}, got {s!r}"
            ) from e

    # ------------------------------------------------------------------
    # Identity and housekeeping
    # ------------------------------------------------------------------

    def info(self) -> RigolDLInfo:
        """Read ``*IDN?`` and parse into a structured record.

        Result is cached after the first call.
        """
        if self._info is None:
            raw = self.query("*IDN?")
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 4:
                raise RigolDLError(f"unexpected *IDN? response: {raw!r}")
            self._info = RigolDLInfo(
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
        """Read one entry from the error queue (``:SYSTem:ERRor?``).

        Returns ``None`` if the queue is empty (code 0). Otherwise a
        ``(code, message)`` tuple. Read repeatedly until ``None`` to
        fully drain the queue.
        """
        raw = self.query(":SYSTem:ERRor?")
        m = re.match(r'^\s*(-?\d+)\s*,\s*"?([^"]*)"?\s*$', raw)
        if not m:
            raise RigolDLError(f"unexpected :SYST:ERR? response: {raw!r}")
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
            raise RigolDLCommandError(code, msg)

    # ------------------------------------------------------------------
    # Mode / input
    # ------------------------------------------------------------------

    def set_mode(self, mode: str) -> None:
        """Set the static operation mode. Accepts ``"CC"`` / ``"CV"`` /
        ``"CR"`` / ``"CP"`` (or their long forms ``"CURRent"`` etc.)."""
        scpi = _MODE_SET_MAP.get(mode.upper())
        if scpi is None:
            raise RigolDLValueError(
                f"mode must be one of CC/CV/CR/CP, got {mode!r}"
            )
        self.write(f":SOURce:FUNCtion {scpi}")

    def get_mode(self) -> str:
        """Returns ``"CC"`` / ``"CV"`` / ``"CR"`` / ``"CP"``."""
        s = self.query(":SOURce:FUNCtion?").upper()
        if s not in _MODE_QUERY_RETURNS:
            raise RigolDLError(f"unexpected :FUNC? response: {s!r}")
        return s

    def set_input(self, on: bool) -> None:
        """Enable or disable the load input. With input off, the load
        presents high impedance and sinks no current."""
        self.write(f":SOURce:INPut:STATe {1 if on else 0}")

    def get_input(self) -> bool:
        return self.query_int(":SOURce:INPut:STATe?") == 1

    # ------------------------------------------------------------------
    # Per-mode setpoints
    # ------------------------------------------------------------------

    def set_current(self, amps: float) -> None:
        """CC-mode setpoint in amps. Honored when ``:FUNC == CC``."""
        if amps < 0:
            raise RigolDLValueError(f"current must be ≥ 0, got {amps}")
        self.write(f":SOURce:CURRent:LEVel:IMMediate {amps:.6f}")

    def get_current(self) -> float:
        return self.query_float(":SOURce:CURRent:LEVel:IMMediate?")

    def set_voltage(self, volts: float) -> None:
        """CV-mode setpoint in volts."""
        if volts < 0:
            raise RigolDLValueError(f"voltage must be ≥ 0, got {volts}")
        self.write(f":SOURce:VOLTage:LEVel:IMMediate {volts:.6f}")

    def get_voltage(self) -> float:
        return self.query_float(":SOURce:VOLTage:LEVel:IMMediate?")

    def set_resistance(self, ohms: float) -> None:
        """CR-mode setpoint in ohms."""
        if ohms <= 0:
            raise RigolDLValueError(f"resistance must be > 0, got {ohms}")
        self.write(f":SOURce:RESistance:LEVel:IMMediate {ohms:.6f}")

    def get_resistance(self) -> float:
        return self.query_float(":SOURce:RESistance:LEVel:IMMediate?")

    def set_power(self, watts: float) -> None:
        """CP-mode setpoint in watts."""
        if watts < 0:
            raise RigolDLValueError(f"power must be ≥ 0, got {watts}")
        self.write(f":SOURce:POWer:LEVel:IMMediate {watts:.6f}")

    def get_power(self) -> float:
        return self.query_float(":SOURce:POWer:LEVel:IMMediate?")

    # ------------------------------------------------------------------
    # Ranges and slew
    # ------------------------------------------------------------------

    def set_current_range(self, amps: float) -> None:
        """Set the CC / transient current range. The device picks the
        nearest hardware range that covers ``amps`` (low-range: ~6 A,
        high-range: 60 A for the DL3031A)."""
        self.write(f":SOURce:CURRent:RANGe {amps:.6f}")

    def get_current_range(self) -> float:
        return self.query_float(":SOURce:CURRent:RANGe?")

    def set_voltage_range(self, volts: float) -> None:
        """Set the CV / measurement voltage range (low: ~36 V, high: 150 V
        for the DL3031A)."""
        self.write(f":SOURce:VOLTage:RANGe {volts:.6f}")

    def get_voltage_range(self) -> float:
        return self.query_float(":SOURce:VOLTage:RANGe?")

    def set_slew(self, amps_per_us: float) -> None:
        """Symmetric current slew rate in A/µs for CC and transient mode.
        Higher slew = sharper transient edge; lower = gentler ramps."""
        if amps_per_us <= 0:
            raise RigolDLValueError(
                f"slew must be > 0, got {amps_per_us}"
            )
        self.write(f":SOURce:CURRent:SLEW:BOTH {amps_per_us:.6f}")

    def get_slew(self) -> float:
        return self.query_float(":SOURce:CURRent:SLEW:BOTH?")

    # ------------------------------------------------------------------
    # Measurements
    # ------------------------------------------------------------------

    def measure_voltage(self) -> float:
        """Trigger a fresh integration and return input voltage (V).

        Blocking — at the default 10 NPLC this takes ~200 ms. For
        higher sample rates use :py:meth:`fetch_voltage` instead.
        """
        return self.query_float(":MEASure:VOLTage:DC?")

    def measure_current(self) -> float:
        """Trigger a fresh integration and return input current (A).

        Blocking (see :py:meth:`measure_voltage`)."""
        return self.query_float(":MEASure:CURRent:DC?")

    def measure_power(self) -> float:
        return self.query_float(":MEASure:POWer:DC?")

    def measure_resistance(self) -> float:
        return self.query_float(":MEASure:RESistance:DC?")

    def measure_all(self) -> dict[str, float]:
        """Trigger fresh V / I / P / R measurements. Four sequential
        integrations (~800 ms at 10 NPLC) — use :py:meth:`fetch_all`
        instead for fast loops."""
        return {
            "voltage_V": self.measure_voltage(),
            "current_A": self.measure_current(),
            "power_W": self.measure_power(),
            "resistance_ohm": self.measure_resistance(),
        }

    # ------------------------------------------------------------------
    # :FETCh: — non-blocking reads of the device's continuously-updated
    # measurement registers. Use these in high-rate sample loops.
    # ------------------------------------------------------------------

    def fetch_voltage(self) -> float:
        """Return the last-measured voltage without triggering a new
        integration. ~10-20 ms USB-TMC round-trip; suitable for ≥ 50 Hz
        sampling."""
        return self.query_float(":FETCh:VOLTage:DC?")

    def fetch_current(self) -> float:
        return self.query_float(":FETCh:CURRent:DC?")

    def fetch_power(self) -> float:
        return self.query_float(":FETCh:POWer:DC?")

    def fetch_resistance(self) -> float:
        return self.query_float(":FETCh:RESistance:DC?")

    def fetch_all(self) -> dict[str, float]:
        """Non-blocking V / I / P / R snapshot — four fast SCPI queries
        (~40-80 ms total) against the device's continuously-updated
        measurement registers."""
        return {
            "voltage_V": self.fetch_voltage(),
            "current_A": self.fetch_current(),
            "power_W": self.fetch_power(),
            "resistance_ohm": self.fetch_resistance(),
        }

    # ------------------------------------------------------------------
    # Trigger system (:TRIGger)
    # ------------------------------------------------------------------

    def set_trigger_source(self, source: str) -> None:
        """Pick what fires a LIST / transient / OCP / OPP sequence.

        - ``BUS`` — the host issues :py:meth:`trigger_now` (``:TRIGger``)
        - ``EXTernal`` — rear-panel digital I/O trigger pulse
        - ``MANUal`` — front-panel TRAN key
        """
        s = source.strip().upper()
        if s not in {"BUS", "EXT", "EXTERNAL", "MAN", "MANUAL"}:
            raise RigolDLValueError(
                f"trigger source must be BUS/EXTernal/MANUal, got {source!r}"
            )
        canonical = {"BUS": "BUS", "EXT": "EXTernal", "EXTERNAL": "EXTernal",
                     "MAN": "MANUal", "MANUAL": "MANUal"}[s]
        self.write(f":TRIGger:SOURce {canonical}")

    def get_trigger_source(self) -> str:
        return self.query(":TRIGger:SOURce?")

    def trigger_now(self) -> None:
        """Fire a software (BUS) trigger. Requires
        :py:meth:`set_trigger_source` to be ``BUS``."""
        self.write(":TRIGger")

    # ------------------------------------------------------------------
    # Top-level regulation source (:SOURce:FUNCtion:MODE)
    # ------------------------------------------------------------------

    def set_function_mode(self, mode: str) -> None:
        """Choose which subsystem drives the input regulation.

        - ``FIXed`` — the static :py:meth:`set_mode` setpoint
        - ``LIST`` — programmed step sequence (see :py:meth:`program_list`)
        - ``WAVe`` — waveform display
        - ``BATTery`` — battery discharge (see :py:meth:`configure_battery_test`)
        - ``OCP`` / ``OPP`` — protection-trip tests
        """
        m = mode.strip()
        if m not in _FUNC_MODE_VALUES:
            raise RigolDLValueError(
                f"function mode must be one of FIXed/LIST/WAVe/BATTery/OCP/OPP, "
                f"got {mode!r}"
            )
        self.write(f":SOURce:FUNCtion:MODE {m}")

    def get_function_mode(self) -> str:
        """Returns FIX / LIST / WAV / BATT / OCP / OPP."""
        return self.query(":SOURce:FUNCtion:MODE?").upper()

    # ------------------------------------------------------------------
    # LIST sequence mode
    # ------------------------------------------------------------------

    def list_set_mode(self, mode: str) -> None:
        """Set the per-step regulation mode (CC/CV/CR/CP) for LIST."""
        scpi = _MODE_SET_MAP.get(mode.upper())
        if scpi is None:
            raise RigolDLValueError(
                f"LIST mode must be CC/CV/CR/CP, got {mode!r}"
            )
        # LIST:MODE uses the two-letter form not the long form
        short = {"CURRent": "CC", "VOLTage": "CV",
                 "RESistance": "CR", "POWer": "CP"}[scpi]
        self.write(f":SOURce:LIST:MODE {short}")

    def list_set_range(self, value: float) -> None:
        """Set the current range used during LIST execution."""
        self.write(f":SOURce:LIST:RANGe {value:.6f}")

    def list_set_count(self, count: int) -> None:
        """How many times the entire list cycles. 0 = infinite."""
        if count < 0 or count > 99_999:
            raise RigolDLValueError(
                f"LIST count must be in [0, 99999], got {count}"
            )
        self.write(f":SOURce:LIST:COUNt {int(count)}")

    def list_set_step_count(self, steps: int) -> None:
        """Set the LIST step count.

        Per the manual's application instance: ``:SOUR:LIST:STEP N``
        means the device runs steps indexed ``0..N`` inclusive, i.e.
        N+1 total steps. So a 3-step list sends ``STEP 2``.

        Pass ``steps`` as the **total** number of steps you want to
        execute (≥ 3); the driver subtracts 1 internally to match the
        firmware's convention. Maximum is 513 (firmware accepts
        STEP up to 512).
        """
        if not 3 <= steps <= 513:
            raise RigolDLValueError(
                f"LIST step count must be in [3, 513], got {steps}"
            )
        self.write(f":SOURce:LIST:STEP {int(steps) - 1}")

    def list_set_step(self, step_index: int, level: float, width_s: float,
                      slew_A_per_us: Optional[float] = None) -> None:
        """Program one step of the list.

        ``step_index`` is zero-based. ``level`` is interpreted per
        :py:meth:`list_set_mode` (A in CC, V in CV, Ω in CR, W in CP).
        ``width_s`` is the dwell time (50 µs to 3600 s). Optional
        ``slew_A_per_us`` sets that step's slew rate.
        """
        if step_index < 0:
            raise RigolDLValueError(f"step_index must be ≥ 0, got {step_index}")
        if not 0.00005 <= width_s <= 3600.0:
            raise RigolDLValueError(
                f"step width must be in [50 µs, 3600 s], got {width_s} s"
            )
        self.write(f":SOURce:LIST:LEVel {int(step_index)},{level:.6f}")
        self.write(f":SOURce:LIST:WIDth {int(step_index)},{width_s:.6f}")
        if slew_A_per_us is not None:
            if slew_A_per_us <= 0:
                raise RigolDLValueError(
                    f"step slew must be > 0, got {slew_A_per_us}"
                )
            self.write(f":SOURce:LIST:SLEW {int(step_index)},{slew_A_per_us:.6f}")

    def list_set_slew(self, step_index: int, amps_per_us: float) -> None:
        """Set the per-step slew rate (A/µs).

        Per the manual, ``:SOUR:LIST:SLEW`` takes ``<step>,<value>`` —
        it's per-step, not global. Apply to every step you've
        programmed for a uniform slew.
        """
        if step_index < 0:
            raise RigolDLValueError(f"step_index must be ≥ 0, got {step_index}")
        if amps_per_us <= 0:
            raise RigolDLValueError(f"LIST slew must be > 0, got {amps_per_us}")
        self.write(f":SOURce:LIST:SLEW {int(step_index)},{amps_per_us:.6f}")

    def list_set_end(self, behavior: str) -> None:
        """What the load does when the list ends.

        - ``LAST`` — hold the final step's value
        - ``OFF``  — disable the input when the list completes
        """
        b = behavior.strip()
        if b not in _LIST_END_VALUES:
            raise RigolDLValueError(
                f"LIST end behavior must be LAST or OFF, got {behavior!r}"
            )
        self.write(f":SOURce:LIST:END {b}")

    def program_list(
        self,
        steps: "list[tuple[float, float]]",
        *,
        mode: str = "CC",
        count: int = 1,
        range_value: Optional[float] = None,
        slew_A_per_us: Optional[float] = None,
        end_behavior: str = "OFF",
        trigger_source: str = "BUS",
    ) -> None:
        """Convenience: program an entire list from a Python sequence.

        ``steps`` is a list of ``(level, width_s)`` tuples (3..513).
        Sets mode, range, count, step count, end behavior, pushes
        each step (with ``slew_A_per_us`` applied to every step when
        provided), sets the trigger source, and switches
        ``:SOUR:FUNC:MODE`` to LIST.

        After programming: with ``trigger_source="BUS"`` (default), call
        ``:py:meth:`trigger_now`` to start. With ``"MANUal"`` the user
        presses the front-panel TRAN key.
        """
        if not 3 <= len(steps) <= 513:
            raise RigolDLValueError(
                f"LIST requires 3..513 steps, got {len(steps)}"
            )
        self.list_set_mode(mode)
        if range_value is not None:
            self.list_set_range(range_value)
        self.list_set_count(count)
        self.list_set_step_count(len(steps))
        for i, (level, width) in enumerate(steps):
            self.list_set_step(i, level, width, slew_A_per_us)
        self.list_set_end(end_behavior)
        self.set_trigger_source(trigger_source)
        self.set_function_mode("LIST")

    # ------------------------------------------------------------------
    # CC transient mode (:SOURce:CURRent:TRANsient)
    # ------------------------------------------------------------------

    def transient_set_mode(self, mode: str) -> None:
        """CC transient sub-mode: CONTinuous / PULSe / TOGGle.

        - CONTinuous — periodic A↔B pulse stream after trigger
        - PULSe — single A pulse on trigger, returns to B
        - TOGGle — A↔B toggle each trigger
        """
        m = mode.strip()
        if m not in _CC_TRAN_MODES:
            raise RigolDLValueError(
                f"transient mode must be CONTinuous/PULSe/TOGGle, got {mode!r}"
            )
        self.write(f":SOURce:CURRent:TRANsient:MODE {m}")

    def transient_set_a_level(self, amps: float) -> None:
        """High-level current (A)."""
        self.write(f":SOURce:CURRent:TRANsient:ALEVel {amps:.6f}")

    def transient_set_b_level(self, amps: float) -> None:
        """Low-level current (A)."""
        self.write(f":SOURce:CURRent:TRANsient:BLEVel {amps:.6f}")

    def transient_set_a_width(self, seconds: float) -> None:
        """Dwell time at A (s)."""
        self.write(f":SOURce:CURRent:TRANsient:AWIDth {seconds:.6f}")

    def transient_set_b_width(self, seconds: float) -> None:
        """Dwell time at B (s)."""
        self.write(f":SOURce:CURRent:TRANsient:BWIDth {seconds:.6f}")

    def transient_set_frequency(self, hz: float) -> None:
        """Pulse-stream frequency (kHz per the manual, but accepts Hz
        as a real number)."""
        self.write(f":SOURce:CURRent:TRANsient:FREQuency {hz:.6f}")

    def transient_set_duty(self, percent: float) -> None:
        """A-level duty cycle (%) in continuous mode."""
        self.write(f":SOURce:CURRent:TRANsient:ADUTy {percent:.6f}")

    def transient_enable(self, on: bool) -> None:
        """Arm or disarm the transient generator (:SOURce:TRANsient[:STATe])."""
        self.write(f":SOURce:TRANsient:STATe {1 if on else 0}")

    def configure_transient_pulse(
        self,
        *,
        a_level_A: float,
        b_level_A: float,
        a_width_s: float,
        b_width_s: float,
        mode: str = "CONTinuous",
    ) -> None:
        """Convenience: configure CC TOGG/PULS/CONT transient in one call."""
        self.set_mode("CC")
        self.transient_set_mode(mode)
        self.transient_set_a_level(a_level_A)
        self.transient_set_b_level(b_level_A)
        self.transient_set_a_width(a_width_s)
        self.transient_set_b_width(b_width_s)

    # ------------------------------------------------------------------
    # Battery discharge mode (:SOURce:BATTary)
    # ------------------------------------------------------------------

    def battery_set_range(self, amps: float) -> None:
        self.write(f":SOURce:BATTary:RANGe {amps:.6f}")

    def battery_set_level(self, amps: float) -> None:
        """Discharge current (A) — like a CC load."""
        self.write(f":SOURce:BATTary:LEVel:IMMediate {amps:.6f}")

    def battery_set_voltage_stop(self, volts: float, enabled: bool = True) -> None:
        """Cell voltage at which to stop discharge."""
        self.write(f":SOURce:BATTary:VSTop {volts:.6f}")
        self.write(f":SOURce:BATTary:VENabstop {1 if enabled else 0}")

    def battery_set_capacity_stop(self, mAh: float, enabled: bool = True) -> None:
        """Drawn capacity at which to stop discharge."""
        self.write(f":SOURce:BATTary:CSTop {mAh:.6f}")
        self.write(f":SOURce:BATTary:CENabstop {1 if enabled else 0}")

    def battery_set_time_stop(self, seconds: float, enabled: bool = True) -> None:
        """Wall-clock time at which to stop discharge."""
        self.write(f":SOURce:BATTary:TIMestop {seconds:.6f}")
        self.write(f":SOURce:BATTary:TENabstop {1 if enabled else 0}")

    def battery_set_von(self, volts: float) -> None:
        """Voltage threshold below which the load won't engage."""
        self.write(f":SOURce:BATTary:VON {volts:.6f}")

    def configure_battery_test(
        self,
        *,
        current_A: float,
        v_stop_V: Optional[float] = None,
        capacity_stop_mAh: Optional[float] = None,
        time_stop_s: Optional[float] = None,
        von_V: Optional[float] = None,
        range_A: Optional[float] = None,
    ) -> None:
        """Convenience: configure a battery discharge test in one call.

        Stop conditions are individually enabled iff the corresponding
        argument is non-None. Provide at least one stop condition or
        the discharge runs until something else (panic-stop) ends it.
        """
        if range_A is not None:
            self.battery_set_range(range_A)
        self.battery_set_level(current_A)
        if von_V is not None:
            self.battery_set_von(von_V)
        if v_stop_V is not None:
            self.battery_set_voltage_stop(v_stop_V, enabled=True)
        if capacity_stop_mAh is not None:
            self.battery_set_capacity_stop(capacity_stop_mAh, enabled=True)
        if time_stop_s is not None:
            self.battery_set_time_stop(time_stop_s, enabled=True)
        self.set_function_mode("BATTery")

    def battery_stats(self) -> dict[str, float]:
        """Current battery-discharge stats from the firmware:
        capacity (mAh), energy (Wh), discharge time (s), and the
        instantaneous V/I."""
        return {
            "capacity_mAh": self.query_float(":FETCh:CAPability?"),
            "energy_Wh": self.query_float(":FETCh:WATThours?"),
            "discharge_time_s": self.query_float(":FETCh:DISChargingTime?"),
            "voltage_V": self.fetch_voltage(),
            "current_A": self.fetch_current(),
        }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _autodiscover(rm) -> str:
    """Scan VISA resources for a Rigol DL3000-family USB device."""
    resources = rm.list_resources()
    # USB resources look like USB0::0x1AB1::0x0E11::SN::INSTR
    vid = f"0x{RIGOL_USB_VID:04X}".lower()
    pid = f"0x{DL3000_USB_PID:04X}".lower()
    for r in resources:
        rl = r.lower()
        if "usb" in rl and vid in rl and pid in rl:
            return r
    raise RigolDLConnectionError(
        f"no Rigol DL3000 (VID 0x{RIGOL_USB_VID:04X} / PID 0x{DL3000_USB_PID:04X}) "
        f"found in VISA resource list: {resources}"
    )

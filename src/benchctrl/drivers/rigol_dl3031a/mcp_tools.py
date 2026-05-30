"""MCP tool surface for the Rigol DL3031A programmable electronic load.

Per the v1.0 driver-symmetric architecture, each driver owns its own
MCP tools and exposes them via :py:func:`register_mcp_tools`. The
top-level :py:mod:`benchctrl.mcp` orchestrator calls this function at
startup to register every DL3031A tool on the shared :py:class:`FastMCP`
server.

Connection state (``_dl3031a``) lives in this module. Tests can mutate
the singleton via this module to inject fakes.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("benchctrl.drivers.rigol_dl3031a.mcp_tools")

# Kept as Any to avoid eagerly importing pyvisa (the actual class is
# RigolDL3031A — pulled in lazily inside dl3031a_open).
_dl3031a = None
_dl3031a_lock = threading.RLock()


def _get_dl3031a():
    from benchctrl.drivers.rigol_dl3031a.driver import RigolDLConnectionError
    if _dl3031a is None:
        raise RigolDLConnectionError(
            "DL3031A not open — call dl3031a_open() first."
        )
    return _dl3031a


def dl3031a_open(resource: Optional[str] = None) -> dict:
    """Open a VISA session to a Rigol DL3031A programmable electronic load.

    If ``resource`` is None, auto-discovers by scanning for the Rigol
    DL3000 USB VID/PID (0x1AB1 / 0x0E11). Pass an explicit VISA
    resource string (USB or LXI) to target a specific device.

    Returns the device's *IDN? identity on success.
    """
    global _dl3031a
    from benchctrl.drivers.rigol_dl3031a import RigolDL3031A
    with _dl3031a_lock:
        if _dl3031a is not None:
            info = _dl3031a.info()
            return {
                "error": "DL3031A already open",
                "guidance": "Call dl3031a_close() before reopening.",
                "current_resource": info.resource,
            }
        _dl3031a = RigolDL3031A.open(resource)
    info = _dl3031a.info()
    return {
        "resource": info.resource,
        "info": {
            "manufacturer": info.manufacturer, "model": info.model,
            "serial": info.serial, "firmware": info.firmware,
        },
    }


def dl3031a_close() -> dict:
    """Close the DL3031A connection. Also disables the load input."""
    global _dl3031a
    with _dl3031a_lock:
        if _dl3031a is None:
            return {"closed": False, "note": "no DL3031A was open"}
        input_off_error: Optional[str] = None
        try:
            _dl3031a.set_input(False)
        except Exception as e:
            input_off_error = f"{type(e).__name__}: {e}"
            log.warning("DL3031A set_input(False) failed during close: %s",
                        input_off_error)
        _dl3031a.close()
        _dl3031a = None
    result = {"closed": True}
    if input_off_error is not None:
        # SAFETY: the load may still be sinking current. Caller MUST
        # verify the DUT is disconnected or the load is physically off.
        result["input_off_failed"] = input_off_error
        result["warning"] = (
            "load input may still be enabled — verify DUT is safe"
        )
    return result


def dl3031a_info() -> dict:
    """Identity and resource string from ``*IDN?``."""
    info = _get_dl3031a().info()
    return {
        "manufacturer": info.manufacturer, "model": info.model,
        "serial": info.serial, "firmware": info.firmware,
        "resource": info.resource,
    }


def dl3031a_reset() -> dict:
    """``*RST`` — restore factory defaults (mode CC, input OFF, all zero)."""
    _get_dl3031a().reset()
    return {"reset": True}


def dl3031a_set_mode(mode: str) -> dict:
    """Set the load's operation mode: ``"CC"`` / ``"CV"`` / ``"CR"`` / ``"CP"``."""
    dl = _get_dl3031a()
    dl.set_mode(mode)
    return {"mode": dl.get_mode()}


def dl3031a_get_mode() -> dict:
    return {"mode": _get_dl3031a().get_mode()}


def dl3031a_set_input(on: bool) -> dict:
    """Enable or disable the load input.

    SAFETY-CRITICAL: enabling the input lets the load start sinking
    current from the connected DUT. Confirm the setpoint, mode, and
    DUT compatibility before passing ``on=True``.
    """
    dl = _get_dl3031a()
    dl.set_input(on)
    return {"input_on": dl.get_input()}


def dl3031a_get_input() -> dict:
    return {"input_on": _get_dl3031a().get_input()}


def dl3031a_set_current(amps: float) -> dict:
    """CC-mode setpoint (A). Honored when ``:FUNC == CC``."""
    dl = _get_dl3031a()
    dl.set_current(amps)
    return {"current_setpoint_A": dl.get_current()}


def dl3031a_set_voltage(volts: float) -> dict:
    """CV-mode setpoint (V). Honored when ``:FUNC == CV``."""
    dl = _get_dl3031a()
    dl.set_voltage(volts)
    return {"voltage_setpoint_V": dl.get_voltage()}


def dl3031a_set_resistance(ohms: float) -> dict:
    """CR-mode setpoint (Ω). Honored when ``:FUNC == CR``."""
    dl = _get_dl3031a()
    dl.set_resistance(ohms)
    return {"resistance_setpoint_ohm": dl.get_resistance()}


def dl3031a_set_power(watts: float) -> dict:
    """CP-mode setpoint (W). Honored when ``:FUNC == CP``."""
    dl = _get_dl3031a()
    dl.set_power(watts)
    return {"power_setpoint_W": dl.get_power()}


def dl3031a_set_current_range(amps: float) -> dict:
    """Set the CC / transient current range (DL3031A: ~6 A low / 60 A high)."""
    dl = _get_dl3031a()
    dl.set_current_range(amps)
    return {"current_range_A": dl.get_current_range()}


def dl3031a_set_voltage_range(volts: float) -> dict:
    """Set the CV / measurement voltage range (DL3031A: ~36 V low / 150 V high)."""
    dl = _get_dl3031a()
    dl.set_voltage_range(volts)
    return {"voltage_range_V": dl.get_voltage_range()}


def dl3031a_set_slew(amps_per_us: float) -> dict:
    """Symmetric current slew rate (A/µs) for CC and transient mode."""
    dl = _get_dl3031a()
    dl.set_slew(amps_per_us)
    return {"slew_A_per_us": dl.get_slew()}


def dl3031a_measure() -> dict:
    """Measure V / I / P / R at the load terminals (one shot each)."""
    return _get_dl3031a().measure_all()


def dl3031a_last_error() -> dict:
    """Read one entry from the SCPI error queue. Returns ``{"code": 0}`` if clean."""
    err = _get_dl3031a().last_error()
    if err is None:
        return {"code": 0, "message": "No error"}
    return {"code": err[0], "message": err[1]}


def dl3031a_clear_status() -> dict:
    """``*CLS`` — clear status registers and the error queue."""
    _get_dl3031a().clear_status()
    return {"cleared": True}


def dl3031a_raise_if_error() -> dict:
    """Drain the error queue and raise on the first non-zero code.
    Returns ``{"ok": true}`` if no errors were queued; otherwise the
    tool surface raises (which the MCP layer will report as an
    error to the caller)."""
    _get_dl3031a().raise_if_error()
    return {"ok": True}


def dl3031a_get_current() -> dict:
    """Return the CC-mode current setpoint (A)."""
    return {"current_setpoint_A": _get_dl3031a().get_current()}


def dl3031a_get_voltage() -> dict:
    """Return the CV-mode voltage setpoint (V)."""
    return {"voltage_setpoint_V": _get_dl3031a().get_voltage()}


def dl3031a_get_resistance() -> dict:
    """Return the CR-mode resistance setpoint (Ω)."""
    return {"resistance_setpoint_ohm": _get_dl3031a().get_resistance()}


def dl3031a_get_power() -> dict:
    """Return the CP-mode power setpoint (W)."""
    return {"power_setpoint_W": _get_dl3031a().get_power()}


def dl3031a_get_current_range() -> dict:
    return {"current_range_A": _get_dl3031a().get_current_range()}


def dl3031a_get_voltage_range() -> dict:
    return {"voltage_range_V": _get_dl3031a().get_voltage_range()}


def dl3031a_get_slew() -> dict:
    return {"slew_A_per_us": _get_dl3031a().get_slew()}


def dl3031a_get_trigger_source() -> dict:
    return {"trigger_source": _get_dl3031a().get_trigger_source()}


def dl3031a_measure_voltage() -> dict:
    """Single-shot voltage measurement (~200 ms integration)."""
    return {"voltage_V": _get_dl3031a().measure_voltage()}


def dl3031a_measure_current() -> dict:
    """Single-shot current measurement (~200 ms integration)."""
    return {"current_A": _get_dl3031a().measure_current()}


def dl3031a_measure_power() -> dict:
    return {"power_W": _get_dl3031a().measure_power()}


def dl3031a_measure_resistance() -> dict:
    return {"resistance_ohm": _get_dl3031a().measure_resistance()}


def dl3031a_fetch_voltage() -> dict:
    """Non-blocking V read (~1 ms). Reads the device's continuously-updated
    measurement register without triggering a fresh integration."""
    return {"voltage_V": _get_dl3031a().fetch_voltage()}


def dl3031a_fetch_current() -> dict:
    """Non-blocking I read (~1 ms)."""
    return {"current_A": _get_dl3031a().fetch_current()}


def dl3031a_fetch_power() -> dict:
    return {"power_W": _get_dl3031a().fetch_power()}


def dl3031a_fetch_resistance() -> dict:
    return {"resistance_ohm": _get_dl3031a().fetch_resistance()}


def dl3031a_fetch() -> dict:
    """Non-blocking V / I / P / R snapshot — reads the device's
    continuously-updated measurement register without triggering a fresh
    integration. Suitable for fast polling loops where ``dl3031a_measure``
    (which forces a ~200 ms integration per call) would be too slow."""
    return _get_dl3031a().fetch_all()


def dl3031a_set_function_mode(mode: str) -> dict:
    """Choose which subsystem drives the input regulation:
    ``FIXed`` (static :SOUR:FUNC + setpoint), ``LIST`` (programmed sequence),
    ``WAVe``, ``BATTery``, ``OCP``, ``OPP``."""
    dl = _get_dl3031a()
    dl.set_function_mode(mode)
    return {"function_mode": dl.get_function_mode()}


def dl3031a_get_function_mode() -> dict:
    return {"function_mode": _get_dl3031a().get_function_mode()}


def dl3031a_trigger() -> dict:
    """Fire a software (BUS) trigger. Starts a LIST or transient
    sequence after the trigger source has been set to ``BUS``."""
    _get_dl3031a().trigger_now()
    return {"triggered": True}


def dl3031a_set_trigger_source(source: str) -> dict:
    """Pick the trigger source: ``BUS`` (software / MCP),
    ``EXTernal`` (rear-panel I/O), or ``MANUal`` (front-panel TRAN key)."""
    dl = _get_dl3031a()
    dl.set_trigger_source(source)
    return {"trigger_source": dl.get_trigger_source()}


def dl3031a_program_list(
    steps: list,
    mode: str = "CC",
    count: int = 1,
    range_value: Optional[float] = None,
    slew_A_per_us: Optional[float] = None,
    end_behavior: str = "OFF",
    trigger_source: str = "BUS",
) -> dict:
    """Program a LIST sequence on the DL3031A and switch the device into
    LIST regulation mode.

    ``steps`` is a list of ``[level, width_s]`` pairs (2-512 entries,
    but **NOT 4** — STEP=4 is a firmware bug that fires no steps;
    use 3 or 5 with appropriate ``count`` for the same total time).
    ``level`` units depend on ``mode``: A for CC, V for CV, Ω for CR, W for CP.
    ``width_s`` accepts 50 µs to 3600 s. ``count = 0`` means infinite.

    LIST execution runs entirely in the device's firmware with
    deterministic timing — the right tool for sub-100 ms TX bursts and
    other transients that USB-TMC round-trips can't keep up with.
    After programming, call ``dl3031a_set_input(True)`` to start.
    """
    dl = _get_dl3031a()
    step_tuples = [(float(s[0]), float(s[1])) for s in steps]
    dl.program_list(
        steps=step_tuples, mode=mode, count=count,
        range_value=range_value, slew_A_per_us=slew_A_per_us,
        end_behavior=end_behavior, trigger_source=trigger_source,
    )
    return {
        "function_mode": dl.get_function_mode(),
        "n_steps": len(step_tuples),
        "count": count,
        "trigger_source": trigger_source,
    }


def dl3031a_configure_transient_pulse(
    a_level_A: float,
    b_level_A: float,
    a_width_s: float,
    b_width_s: float,
    mode: str = "CONTinuous",
) -> dict:
    """Configure CC transient mode in one call.

    The load toggles between ``a_level_A`` and ``b_level_A`` with
    widths ``a_width_s`` / ``b_width_s``. ``mode`` is one of:

    - ``CONTinuous`` — periodic A↔B pulse stream after trigger
    - ``PULSe`` — single A pulse on trigger, returns to B
    - ``TOGGle`` — A↔B toggle on each trigger

    Arm with ``dl3031a_transient_enable(True)`` + ``dl3031a_set_input(True)``.
    """
    dl = _get_dl3031a()
    dl.configure_transient_pulse(
        a_level_A=a_level_A, b_level_A=b_level_A,
        a_width_s=a_width_s, b_width_s=b_width_s, mode=mode,
    )
    return {"mode": dl.get_mode(), "transient_mode": mode}


def dl3031a_transient_enable(on: bool) -> dict:
    """Arm (``on=True``) or disarm the transient generator
    (``:SOURce:TRANsient:STATe``)."""
    _get_dl3031a().transient_enable(on)
    return {"transient_armed": on}


def dl3031a_configure_battery_test(
    current_A: float,
    v_stop_V: Optional[float] = None,
    capacity_stop_mAh: Optional[float] = None,
    time_stop_s: Optional[float] = None,
    von_V: Optional[float] = None,
    range_A: Optional[float] = None,
) -> dict:
    """Configure the DL3031A's built-in battery-discharge mode.

    The load sinks ``current_A`` until any provided stop condition
    fires (voltage drops to ``v_stop_V``, capacity reaches
    ``capacity_stop_mAh``, or time exceeds ``time_stop_s``). At least
    one stop condition is strongly recommended.

    Start with ``dl3031a_set_input(True)``; monitor with
    ``dl3031a_battery_stats``.
    """
    dl = _get_dl3031a()
    dl.configure_battery_test(
        current_A=current_A, v_stop_V=v_stop_V,
        capacity_stop_mAh=capacity_stop_mAh, time_stop_s=time_stop_s,
        von_V=von_V, range_A=range_A,
    )
    return {"function_mode": dl.get_function_mode()}


def dl3031a_battery_stats() -> dict:
    """Current battery-discharge stats from the firmware: capacity
    (mAh), energy (Wh), discharge time (s), instantaneous V/I."""
    return _get_dl3031a().battery_stats()


_TOOLS = (
    dl3031a_open, dl3031a_close, dl3031a_info, dl3031a_reset,
    dl3031a_set_mode, dl3031a_get_mode,
    dl3031a_set_input, dl3031a_get_input,
    dl3031a_set_current, dl3031a_set_voltage, dl3031a_set_resistance, dl3031a_set_power,
    dl3031a_set_current_range, dl3031a_set_voltage_range, dl3031a_set_slew,
    dl3031a_measure, dl3031a_last_error, dl3031a_clear_status, dl3031a_raise_if_error,
    dl3031a_get_current, dl3031a_get_voltage, dl3031a_get_resistance, dl3031a_get_power,
    dl3031a_get_current_range, dl3031a_get_voltage_range, dl3031a_get_slew,
    dl3031a_get_trigger_source,
    dl3031a_measure_voltage, dl3031a_measure_current,
    dl3031a_measure_power, dl3031a_measure_resistance,
    dl3031a_fetch_voltage, dl3031a_fetch_current,
    dl3031a_fetch_power, dl3031a_fetch_resistance, dl3031a_fetch,
    dl3031a_set_function_mode, dl3031a_get_function_mode,
    dl3031a_trigger, dl3031a_set_trigger_source,
    dl3031a_program_list, dl3031a_configure_transient_pulse,
    dl3031a_transient_enable,
    dl3031a_configure_battery_test, dl3031a_battery_stats,
)


def register_mcp_tools(mcp) -> None:
    """Register every DL3031A MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

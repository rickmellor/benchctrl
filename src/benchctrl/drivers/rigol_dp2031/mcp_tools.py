"""MCP tool surface for the Rigol DP2031 programmable PSU.

Per the v1.0 driver-symmetric architecture, this module owns the
DP2031's MCP tools and exposes them via :py:func:`register_mcp_tools`.
The top-level :py:mod:`benchctrl.mcp` orchestrator calls this function
at startup to register every DP2031 tool on the shared
:py:class:`FastMCP` server.

Connection state (``_dp2031``) lives in this module. Tests can mutate
the singleton via this module to inject fakes.

Phase A surface only: identity / housekeeping / channel selection /
per-channel V/I setpoints + output enable + OVP/OCP levels + measurement.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("benchctrl.drivers.rigol_dp2031.mcp_tools")

# Singleton DP2031 connection. Kept as Any to avoid eagerly importing
# pyvisa at module-load time.
_dp2031 = None
_dp2031_lock = threading.RLock()


def _get_dp2031():
    from benchctrl.drivers.rigol_dp2031.driver import RigolDP2031ConnectionError
    if _dp2031 is None:
        raise RigolDP2031ConnectionError(
            "DP2031 not open — call dp2031_open() first."
        )
    return _dp2031


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def dp2031_open(resource: Optional[str] = None) -> dict:
    """Open a VISA session to a Rigol DP2031 programmable PSU.

    If ``resource`` is None, auto-discovers by scanning for the Rigol
    DP2000 USB VID/PID (0x1AB1 / 0xA4A8). Pass an explicit VISA
    resource string to target a specific device.

    Returns the device's *IDN? identity on success.
    """
    global _dp2031
    from benchctrl.drivers.rigol_dp2031 import RigolDP2031
    with _dp2031_lock:
        if _dp2031 is not None:
            info = _dp2031.info()
            return {
                "error": "DP2031 already open",
                "guidance": "Call dp2031_close() before reopening.",
                "current_resource": info.resource,
            }
        _dp2031 = RigolDP2031.open(resource)
    info = _dp2031.info()
    return {
        "resource": info.resource,
        "info": {
            "manufacturer": info.manufacturer, "model": info.model,
            "serial": info.serial, "firmware": info.firmware,
        },
    }


def dp2031_close() -> dict:
    """Close the DP2031 connection. Best-effort disables all 3 outputs first.

    SAFETY: failure to disable an output during teardown is reported in
    the response dict — verify the DUT side if you see
    ``outputs_off_failed``.
    """
    global _dp2031
    with _dp2031_lock:
        if _dp2031 is None:
            return {"closed": False, "note": "no DP2031 was open"}
        failures: list[str] = []
        for ch in (1, 2, 3):
            try:
                _dp2031.set_output(ch, False)
            except Exception as e:
                failures.append(f"CH{ch}: {type(e).__name__}: {e}")
                log.warning(
                    "DP2031 set_output(CH%d, False) failed during close: %s",
                    ch, e,
                )
        _dp2031.close()
        _dp2031 = None
    result: dict = {"closed": True}
    if failures:
        result["outputs_off_failed"] = failures
        result["warning"] = (
            "one or more channels may still be enabled — verify DUT is safe"
        )
    return result


def dp2031_info() -> dict:
    """Identity from ``*IDN?``: manufacturer, model, serial, firmware."""
    info = _get_dp2031().info()
    return {
        "manufacturer": info.manufacturer, "model": info.model,
        "serial": info.serial, "firmware": info.firmware,
        "resource": info.resource,
    }


def dp2031_reset() -> dict:
    """``*RST`` — restore factory defaults. Does NOT clear the error queue."""
    _get_dp2031().reset()
    return {"reset": True}


def dp2031_clear_status() -> dict:
    """``*CLS`` — clear status registers and the error queue."""
    _get_dp2031().clear_status()
    return {"cleared": True}


def dp2031_last_error() -> dict:
    """One entry from the SCPI error queue. Returns ``{"code": 0}`` if clean."""
    err = _get_dp2031().last_error()
    if err is None:
        return {"code": 0, "message": "No error"}
    return {"code": err[0], "message": err[1]}


def dp2031_raise_if_error() -> dict:
    """Check the error queue and raise on the first non-zero code."""
    _get_dp2031().raise_if_error()
    return {"ok": True}


# ---------------------------------------------------------------------------
# Channel selection
# ---------------------------------------------------------------------------


def dp2031_select_channel(channel: int) -> dict:
    """Set the device's "current" channel (1, 2, or 3).

    Most per-channel tools take ``channel`` as an explicit arg, so this
    is rarely needed — it's exposed because the front panel and
    :py:func:`dp2031_current_channel` reflect this state.
    """
    _get_dp2031().select_channel(channel)
    return {"channel": channel}


def dp2031_current_channel() -> dict:
    return {"channel": int(_get_dp2031().current_channel())}


# ---------------------------------------------------------------------------
# Setpoints — voltage / current
# ---------------------------------------------------------------------------


def dp2031_set_voltage(channel: int, volts: float) -> dict:
    """Set CHn voltage setpoint (V). Does NOT enable the output.

    CH1 / CH2 range 0–32 V; CH3 range 0–6 V.
    """
    _get_dp2031().set_voltage(channel, volts)
    return {"channel": channel, "voltage_V": volts}


def dp2031_get_voltage(channel: int) -> dict:
    return {"channel": channel, "voltage_V": _get_dp2031().get_voltage(channel)}


def dp2031_set_current(channel: int, amps: float) -> dict:
    """Set CHn current setpoint / limit (A). Does NOT enable the output.

    CH1 / CH2 range 0–3 A; CH3 range 0–5 A.
    """
    _get_dp2031().set_current(channel, amps)
    return {"channel": channel, "current_A": amps}


def dp2031_get_current(channel: int) -> dict:
    return {"channel": channel, "current_A": _get_dp2031().get_current(channel)}


# ---------------------------------------------------------------------------
# Output enable
# ---------------------------------------------------------------------------


def dp2031_set_output(channel: int, on: bool) -> dict:
    """Enable or disable CHn's output.

    SAFETY-CRITICAL: enabling drives the configured voltage onto the
    output terminals through the configured current limit. Verify DUT
    ratings and OVP / OCP arming before calling with ``on=True``.
    """
    _get_dp2031().set_output(channel, on)
    return {"channel": channel, "output_on": on}


def dp2031_get_output(channel: int) -> dict:
    return {"channel": channel, "output_on": _get_dp2031().get_output(channel)}


def dp2031_set_output_all(on: bool) -> dict:
    """Enable or disable all three outputs in one command.

    SAFETY-CRITICAL: see :py:func:`dp2031_set_output`. Applies to all
    three channels at once.
    """
    _get_dp2031().set_output_all(on)
    return {"output_all_on": on}


def dp2031_output_regulation(channel: int) -> dict:
    """``CV`` / ``CC`` / ``UR`` — what's regulating CHn right now."""
    return {
        "channel": channel,
        "regulation": _get_dp2031().output_regulation(channel),
    }


# ---------------------------------------------------------------------------
# Protection — OVP
# ---------------------------------------------------------------------------


def dp2031_set_ovp_level(channel: int, volts: float) -> dict:
    """Set CHn over-voltage trip threshold (V). Does NOT arm OVP."""
    _get_dp2031().set_ovp_level(channel, volts)
    return {"channel": channel, "ovp_level_V": volts}


def dp2031_get_ovp_level(channel: int) -> dict:
    return {"channel": channel,
            "ovp_level_V": _get_dp2031().get_ovp_level(channel)}


def dp2031_set_ovp_enabled(channel: int, on: bool) -> dict:
    """Arm or disarm CHn OVP."""
    _get_dp2031().set_ovp_enabled(channel, on)
    return {"channel": channel, "ovp_enabled": on}


def dp2031_get_ovp_enabled(channel: int) -> dict:
    return {"channel": channel,
            "ovp_enabled": _get_dp2031().get_ovp_enabled(channel)}


# ---------------------------------------------------------------------------
# Protection — OCP
# ---------------------------------------------------------------------------


def dp2031_set_ocp_level(channel: int, amps: float) -> dict:
    """Set CHn over-current trip threshold (A). Does NOT arm OCP."""
    _get_dp2031().set_ocp_level(channel, amps)
    return {"channel": channel, "ocp_level_A": amps}


def dp2031_get_ocp_level(channel: int) -> dict:
    return {"channel": channel,
            "ocp_level_A": _get_dp2031().get_ocp_level(channel)}


def dp2031_set_ocp_enabled(channel: int, on: bool) -> dict:
    """Arm or disarm CHn OCP."""
    _get_dp2031().set_ocp_enabled(channel, on)
    return {"channel": channel, "ocp_enabled": on}


def dp2031_get_ocp_enabled(channel: int) -> dict:
    return {"channel": channel,
            "ocp_enabled": _get_dp2031().get_ocp_enabled(channel)}


# ---------------------------------------------------------------------------
# Measurements
# ---------------------------------------------------------------------------


def dp2031_measure_voltage(channel: int) -> dict:
    return {"channel": channel,
            "voltage_V": _get_dp2031().measure_voltage(channel)}


def dp2031_measure_current(channel: int) -> dict:
    return {"channel": channel,
            "current_A": _get_dp2031().measure_current(channel)}


def dp2031_measure_power(channel: int) -> dict:
    return {"channel": channel,
            "power_W": _get_dp2031().measure_power(channel)}


def dp2031_measure_all(channel: int) -> dict:
    """V / I / P for CHn in one round-trip."""
    out = _get_dp2031().measure_all(channel)
    out["channel"] = channel
    return out


def dp2031_measure_all_channels() -> dict:
    """V / I / P for all three channels. Three round-trips, not atomic."""
    return {"channels": _get_dp2031().measure_all_channels()}


_TOOLS = (
    # Connection + identity
    dp2031_open, dp2031_close, dp2031_info,
    dp2031_reset, dp2031_clear_status,
    dp2031_last_error, dp2031_raise_if_error,
    # Channel selection
    dp2031_select_channel, dp2031_current_channel,
    # Setpoints
    dp2031_set_voltage, dp2031_get_voltage,
    dp2031_set_current, dp2031_get_current,
    # Output enable
    dp2031_set_output, dp2031_get_output,
    dp2031_set_output_all, dp2031_output_regulation,
    # OVP
    dp2031_set_ovp_level, dp2031_get_ovp_level,
    dp2031_set_ovp_enabled, dp2031_get_ovp_enabled,
    # OCP
    dp2031_set_ocp_level, dp2031_get_ocp_level,
    dp2031_set_ocp_enabled, dp2031_get_ocp_enabled,
    # Measurements
    dp2031_measure_voltage, dp2031_measure_current,
    dp2031_measure_power, dp2031_measure_all,
    dp2031_measure_all_channels,
)


def register_mcp_tools(mcp) -> None:
    """Register every Phase-A DP2031 MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

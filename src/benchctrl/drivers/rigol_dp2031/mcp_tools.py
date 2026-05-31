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


# ---------------------------------------------------------------------------
# Protection — OVP / OCP trip + clear + delay (Phase B)
# ---------------------------------------------------------------------------


def dp2031_clear_ovp(channel: int) -> dict:
    """Clear a latched OVP trip on CHn. Does NOT re-enable the output."""
    _get_dp2031().clear_ovp(channel)
    return {"channel": channel, "ovp_cleared": True}


def dp2031_clear_ocp(channel: int) -> dict:
    """Clear a latched OCP trip on CHn. Does NOT re-enable the output."""
    _get_dp2031().clear_ocp(channel)
    return {"channel": channel, "ocp_cleared": True}


def dp2031_ovp_tripped(channel: int) -> dict:
    """Is CHn's OVP currently latched?"""
    return {"channel": channel,
            "ovp_tripped": _get_dp2031().ovp_tripped(channel)}


def dp2031_ocp_tripped(channel: int) -> dict:
    """Is CHn's OCP currently latched?"""
    return {"channel": channel,
            "ocp_tripped": _get_dp2031().ocp_tripped(channel)}


def dp2031_set_ocp_delay_ms(channel: int, milliseconds: int) -> dict:
    """Set CHn OCP integration / debounce delay (0–1000 ms)."""
    _get_dp2031().set_ocp_delay_ms(channel, milliseconds)
    return {"channel": channel, "ocp_delay_ms": milliseconds}


def dp2031_get_ocp_delay_ms(channel: int) -> dict:
    return {"channel": channel,
            "ocp_delay_ms": _get_dp2031().get_ocp_delay_ms(channel)}


# ---------------------------------------------------------------------------
# IEEE 488.2 status / OPC / options
# ---------------------------------------------------------------------------


def dp2031_event_status_register() -> dict:
    """``*ESR?`` — standard event status register."""
    return {"esr": _get_dp2031().event_status_register()}


def dp2031_set_event_status_enable(mask: int) -> dict:
    _get_dp2031().set_event_status_enable(mask)
    return {"ese": mask}


def dp2031_get_event_status_enable() -> dict:
    return {"ese": _get_dp2031().get_event_status_enable()}


def dp2031_status_byte() -> dict:
    """``*STB?`` — status byte."""
    return {"stb": _get_dp2031().status_byte()}


def dp2031_set_service_request_enable(mask: int) -> dict:
    _get_dp2031().set_service_request_enable(mask)
    return {"sre": mask}


def dp2031_get_service_request_enable() -> dict:
    return {"sre": _get_dp2031().get_service_request_enable()}


def dp2031_wait_op_complete() -> dict:
    """``*OPC?`` — block until pending operations finish."""
    return {"opc": _get_dp2031().wait_op_complete()}


def dp2031_self_test() -> dict:
    """``*TST?`` — device self-test; ``"result": 0`` is pass."""
    return {"result": _get_dp2031().self_test()}


def dp2031_installed_options() -> dict:
    return {"options": _get_dp2031().installed_options()}


def dp2031_set_power_on_status_clear(on: bool) -> dict:
    _get_dp2031().set_power_on_status_clear(on)
    return {"psc": on}


def dp2031_save_state(slot: int) -> dict:
    """Save device state to slot 0–9 (file ``RIGOLn.RSF``)."""
    _get_dp2031().save_state(slot)
    return {"saved_to_slot": slot}


def dp2031_recall_state(slot: int) -> dict:
    """Load device state from slot 0–9."""
    _get_dp2031().recall_state(slot)
    return {"recalled_from_slot": slot}


# ---------------------------------------------------------------------------
# :STATus subsystem
# ---------------------------------------------------------------------------


def dp2031_operation_event() -> dict:
    """Latched operation event register (read clears)."""
    return {"operation_event": _get_dp2031().operation_event()}


def dp2031_questionable_event() -> dict:
    """Latched questionable event register (read clears)."""
    return {"questionable_event": _get_dp2031().questionable_event()}


def dp2031_channel_status_event(channel: int) -> dict:
    """Per-channel questionable event register (read clears)."""
    return {"channel": channel,
            "event": _get_dp2031().channel_status_event(channel)}


def dp2031_health_check() -> dict:
    """Snapshot of questionable + per-channel status registers.

    Returns a structured dict with ``otp_global``, ``fan_failure``,
    per-channel ``ch1/ch2/ch3`` bits (``vunreg`` / ``iunreg`` /
    ``ovp`` / ``ocp`` / ``otp``), and the first non-zero error
    queue entry. Reads the event registers — call once and store.
    """
    return _get_dp2031().health_check()


# ---------------------------------------------------------------------------
# System basics — beeper / brightness / lock / language / power-on
# ---------------------------------------------------------------------------


def dp2031_beep_once() -> dict:
    """Single beep."""
    _get_dp2031().beep_once()
    return {"beeped": True}


def dp2031_set_beeper(on: bool) -> dict:
    _get_dp2031().set_beeper(on)
    return {"beeper_on": on}


def dp2031_get_beeper() -> dict:
    return {"beeper_on": _get_dp2031().get_beeper()}


def dp2031_set_brightness(percent: int) -> dict:
    """Display brightness (1–100 %)."""
    _get_dp2031().set_brightness(percent)
    return {"brightness_percent": percent}


def dp2031_get_brightness() -> dict:
    return {"brightness_percent": _get_dp2031().get_brightness()}


def dp2031_scpi_version() -> dict:
    return {"scpi_version": _get_dp2031().scpi_version()}


def dp2031_set_keyboard_lock(on: bool) -> dict:
    _get_dp2031().set_keyboard_lock(on)
    return {"keyboard_locked": on}


def dp2031_set_touchscreen_lock(on: bool) -> dict:
    _get_dp2031().set_touchscreen_lock(on)
    return {"touchscreen_locked": on}


def dp2031_set_remote() -> dict:
    """Put device into remote mode (locks front panel)."""
    _get_dp2031().set_remote()
    return {"remote": True}


def dp2031_set_local() -> dict:
    """Return control to the front panel."""
    _get_dp2031().set_local()
    return {"remote": False}


def dp2031_set_screen_saver(on: bool) -> dict:
    _get_dp2031().set_screen_saver(on)
    return {"screen_saver_on": on}


def dp2031_set_language(language: str) -> dict:
    """Set front-panel language: EN/CH/DE/ES/FR (long forms accepted)."""
    _get_dp2031().set_language(language)
    return {"language": language}


def dp2031_set_power_on_mode(mode: str) -> dict:
    """Set boot state: ``"DEFault"`` or ``"LAST"``."""
    _get_dp2031().set_power_on_mode(mode)
    return {"power_on_mode": mode}


# ---------------------------------------------------------------------------
# Phase C — pair / tracking / sense / sampling / step / apply / bounds
# ---------------------------------------------------------------------------


def dp2031_set_channel_pair(mode: str) -> dict:
    """Set CH1+CH2 pair mode: ``"OFF"`` / ``"SERies"`` / ``"PARallel"``.

    SAFETY-CRITICAL: ``"SERies"`` ties CH1+ to CH2- for up to 64 V
    composite; ``"PARallel"`` ties CH1 and CH2 in parallel for 6 A
    composite. Disconnect any external load between CH1 and CH2 before
    switching. Bench-discovered: ``PARallel`` may silently no-op on
    some firmware — call :py:func:`dp2031_get_channel_pair` to verify.
    """
    _get_dp2031().set_channel_pair(mode)
    return {"channel_pair": mode}


def dp2031_get_channel_pair() -> dict:
    return {"channel_pair": _get_dp2031().get_channel_pair()}


def dp2031_set_tracking(on: bool) -> dict:
    """Enable / disable CH1↔CH2 voltage tracking."""
    _get_dp2031().set_tracking(on)
    return {"tracking": on}


def dp2031_get_tracking() -> dict:
    return {"tracking": _get_dp2031().get_tracking()}


def dp2031_set_track_mode(mode: str) -> dict:
    """``:SYSTem:TMODe`` — alias for :py:func:`dp2031_set_tracking`.

    Accepts ``"SYNC"`` / ``"INDE"`` (long forms ``SYNCHRONOUS`` /
    ``INDEPENDENT`` also accepted).
    """
    _get_dp2031().set_track_mode(mode)
    return {"track_mode": mode}


def dp2031_get_track_mode() -> dict:
    return {"track_mode": _get_dp2031().get_track_mode()}


def dp2031_set_output_sync(on: bool) -> dict:
    """Enable simultaneous CH1+CH2 output on/off (meaningful in tracking mode)."""
    _get_dp2031().set_output_sync(on)
    return {"output_sync": on}


def dp2031_get_output_sync() -> dict:
    return {"output_sync": _get_dp2031().get_output_sync()}


def dp2031_set_remote_sense(channel, on: bool) -> dict:
    """Enable / disable 4-wire remote sense for CHn (or ``"ALL"``).

    SAFETY: with remote sense ON, the sense leads must be wired to
    the DUT. With them unconnected the channel will drive its output
    to the maximum the OVP allows trying to "find" the sense voltage.
    """
    _get_dp2031().set_remote_sense(channel, on)
    return {"channel": channel, "remote_sense": on}


def dp2031_get_remote_sense(channel: int) -> dict:
    return {"channel": channel,
            "remote_sense": _get_dp2031().get_remote_sense(channel)}


def dp2031_set_sampling_mode(mode: str) -> dict:
    """Set current-measurement sampling mode for CH1/CH2: ``AUTO/HIGH/LOW``.

    LOW mode extends readback resolution to 1 µA for currents below
    ~11 mA at the cost of slower acquisition. Has no effect on CH3.
    """
    _get_dp2031().set_sampling_mode(mode)
    return {"sampling_mode": mode}


def dp2031_get_sampling_mode() -> dict:
    return {"sampling_mode": _get_dp2031().get_sampling_mode()}


def dp2031_set_voltage_step(channel: int, volts: float) -> dict:
    """Set the step size for ``dp2031_step_voltage_up`` / ``_down`` on CHn."""
    _get_dp2031().set_voltage_step(channel, volts)
    return {"channel": channel, "voltage_step_V": volts}


def dp2031_get_voltage_step(channel: int) -> dict:
    return {"channel": channel,
            "voltage_step_V": _get_dp2031().get_voltage_step(channel)}


def dp2031_set_current_step(channel: int, amps: float) -> dict:
    """Set the step size for ``dp2031_step_current_up`` / ``_down`` on CHn."""
    _get_dp2031().set_current_step(channel, amps)
    return {"channel": channel, "current_step_A": amps}


def dp2031_get_current_step(channel: int) -> dict:
    return {"channel": channel,
            "current_step_A": _get_dp2031().get_current_step(channel)}


def dp2031_step_voltage_up(channel: int) -> dict:
    """Increment CHn voltage by its configured step."""
    _get_dp2031().step_voltage_up(channel)
    return {"channel": channel, "stepped": "voltage up"}


def dp2031_step_voltage_down(channel: int) -> dict:
    """Decrement CHn voltage by its configured step."""
    _get_dp2031().step_voltage_down(channel)
    return {"channel": channel, "stepped": "voltage down"}


def dp2031_step_current_up(channel: int) -> dict:
    """Increment CHn current setpoint by its configured step."""
    _get_dp2031().step_current_up(channel)
    return {"channel": channel, "stepped": "current up"}


def dp2031_step_current_down(channel: int) -> dict:
    """Decrement CHn current setpoint by its configured step."""
    _get_dp2031().step_current_down(channel)
    return {"channel": channel, "stepped": "current down"}


def dp2031_apply(
    channel: int,
    voltage: Optional[float] = None,
    current: Optional[float] = None,
) -> dict:
    """One-shot V/I configuration via ``:APPLy`` — equivalent to
    select_channel + set_voltage + set_current."""
    _get_dp2031().apply(channel, voltage=voltage, current=current)
    out: dict = {"channel": channel}
    if voltage is not None:
        out["voltage_V"] = voltage
    if current is not None:
        out["current_A"] = current
    return out


def dp2031_query_applied(channel: int, option: Optional[str] = None) -> dict:
    """Read the APPLy-form V/I config for CHn.

    Without ``option`` returns the full tuple as
    ``{"rated", "voltage_V", "current_A"}``; with
    ``option="VOLT"`` / ``"CURR"`` returns just that value.
    """
    result = _get_dp2031().query_applied(channel, option=option)
    if option is None:
        rated, v, i = result
        return {"channel": channel, "rated": rated,
                "voltage_V": v, "current_A": i}
    key = "voltage_V" if option.strip().upper().startswith("V") else "current_A"
    return {"channel": channel, key: result}


def dp2031_voltage_bounds(channel: int) -> dict:
    """Device-reported ``(min, max, default)`` voltage bounds for CHn.

    Note: the device's ``MAX`` includes ~5 % headroom over the nominal
    envelope (e.g. 33.6 V on CH1, whose nominal is 32 V).
    """
    lo, hi, dflt = _get_dp2031().voltage_bounds(channel)
    return {"channel": channel,
            "min_V": lo, "max_V": hi, "default_V": dflt}


def dp2031_current_bounds(channel: int) -> dict:
    """Device-reported ``(min, max, default)`` current bounds for CHn."""
    lo, hi, dflt = _get_dp2031().current_bounds(channel)
    return {"channel": channel,
            "min_A": lo, "max_A": hi, "default_A": dflt}


# ---------------------------------------------------------------------------
# Phase D — Timer (Arb sequencer)
# ---------------------------------------------------------------------------


def dp2031_set_timer_enabled(on: bool) -> dict:
    """Arm or disarm the Timer generator."""
    _get_dp2031().set_timer_enabled(on)
    return {"timer_enabled": on}


def dp2031_get_timer_enabled() -> dict:
    return {"timer_enabled": _get_dp2031().get_timer_enabled()}


def dp2031_set_timer_channel(channel: int) -> dict:
    _get_dp2031().set_timer_channel(channel)
    return {"timer_channel": channel}


def dp2031_get_timer_channel() -> dict:
    return {"timer_channel": int(_get_dp2031().get_timer_channel())}


def dp2031_set_timer_cycles(count: Optional[int] = None) -> dict:
    """Set Timer cycle count. ``count`` 1–99999, or None/0 for infinite."""
    _get_dp2031().set_timer_cycles(count)
    return {"timer_cycles": count or "infinite"}


def dp2031_get_timer_cycles() -> dict:
    c = _get_dp2031().get_timer_cycles()
    return {"timer_cycles": c if c is not None else "infinite"}


def dp2031_set_timer_end_state(mode: str) -> dict:
    """``OFF`` (disable output at end) or ``LAST`` (hold last step's value)."""
    _get_dp2031().set_timer_end_state(mode)
    return {"timer_end_state": mode}


def dp2031_get_timer_end_state() -> dict:
    return {"timer_end_state": _get_dp2031().get_timer_end_state()}


def dp2031_set_timer_run_mode(mode: str) -> dict:
    """``CONTinue`` (loop) or ``SINGle`` (one pass)."""
    _get_dp2031().set_timer_run_mode(mode)
    return {"timer_run_mode": mode}


def dp2031_get_timer_run_mode() -> dict:
    return {"timer_run_mode": _get_dp2031().get_timer_run_mode()}


def dp2031_set_timer_trigger(source: str) -> dict:
    """``MANual`` or ``BUS``."""
    _get_dp2031().set_timer_trigger(source)
    return {"timer_trigger": source}


def dp2031_get_timer_trigger() -> dict:
    return {"timer_trigger": _get_dp2031().get_timer_trigger()}


def dp2031_get_timer_group_params(count: int = 1) -> dict:
    """Read back ``count`` Timer groups starting at the current group index.

    Returns a list of ``{"index", "voltage_V", "current_A", "dwell_s"}``
    dicts.
    """
    rows = _get_dp2031().get_timer_group_params(count)
    return {
        "groups": [
            {"index": idx, "voltage_V": v, "current_A": i, "dwell_s": t}
            for idx, v, i, t in rows
        ]
    }


def dp2031_delete_timer_groups(count: int = 1) -> dict:
    """Delete ``count`` Timer groups starting at the current group index."""
    _get_dp2031().delete_timer_groups(count)
    return {"deleted_count": count}


def dp2031_program_timer(
    channel: int,
    steps: list,
    cycles: Optional[int] = 1,
    end_state: str = "OFF",
    run_mode: str = "CONTinue",
    trigger: str = "MANual",
) -> dict:
    """Program a complete Timer sequence on CHn in one call.

    Each step in ``steps`` is ``[voltage_V, current_A, dwell_s]``.
    Steps are validated against the channel envelope before any
    wire writes happen. The Timer is left disarmed — call
    ``dp2031_set_timer_enabled(True)`` and arm the trigger separately.

    SAFETY: subsequent Timer execution can drive significant
    voltages and currents on CHn. Confirm DUT-side ratings.
    """
    step_tuples = [
        (float(s[0]), float(s[1]), float(s[2])) for s in steps
    ]
    _get_dp2031().program_timer(
        channel,
        step_tuples,
        cycles=cycles,
        end_state=end_state,
        run_mode=run_mode,
        trigger=trigger,
    )
    return {
        "channel": channel,
        "steps_written": len(step_tuples),
        "cycles": cycles or "infinite",
        "end_state": end_state,
        "trigger": trigger,
    }


# Timer template subsystem (less commonly used; surface only the most
# useful parts — full template surface is on the SDK if needed)


def dp2031_set_timer_template(template: str) -> dict:
    """Pick template: SINE / PULSE / RAMP / UP / DN / UPDN / RISE / FALL."""
    _get_dp2031().set_timer_template(template)
    return {"template": template}


def dp2031_construct_timer_from_template() -> dict:
    """Render the configured template into the Timer group editor."""
    _get_dp2031().construct_timer_from_template()
    return {"constructed": True}


# ---------------------------------------------------------------------------
# Phase D — Analyzer
# ---------------------------------------------------------------------------


def dp2031_set_analyzer_enabled(on: bool) -> dict:
    _get_dp2031().set_analyzer_enabled(on)
    return {"analyzer_enabled": on}


def dp2031_get_analyzer_enabled() -> dict:
    return {"analyzer_enabled": _get_dp2031().get_analyzer_enabled()}


def dp2031_set_analyzer_type(type_: str) -> dict:
    """``COM`` (V/I/P selection) or ``CURR`` (pulse-current analysis)."""
    _get_dp2031().set_analyzer_type(type_)
    return {"analyzer_type": type_}


def dp2031_get_analyzer_type() -> dict:
    return {"analyzer_type": _get_dp2031().get_analyzer_type()}


def dp2031_set_analyzer_common_objects(objects: list) -> dict:
    """Select 1–3 analyzer COM-mode objects (``CHx_V`` / ``CHx_C`` / ``CHx_P``).

    KNOWN BUG: this command triggers VI_ERROR_SYSTEM_ERROR on
    DP2031 firmware 01.00.01.00.16 over USB-TMC. The wire-form is
    per-spec; the device-side handler is defective.
    """
    _get_dp2031().set_analyzer_common_objects(*objects)
    return {"objects": list(objects)}


def dp2031_set_analyzer_save(on: bool) -> dict:
    """Enable the analyzer's log-to-file feature."""
    _get_dp2031().set_analyzer_save(on)
    return {"analyzer_save": on}


def dp2031_set_analyzer_save_path(path: str) -> dict:
    """Set the analyzer log file path (e.g. ``C:/RA.ROF``)."""
    _get_dp2031().set_analyzer_save_path(path)
    return {"analyzer_save_path": path}


# ---------------------------------------------------------------------------
# Phase D — Trigger I/O (D1-D4 rear digital lines)
# ---------------------------------------------------------------------------


def dp2031_set_trigger_in_enabled(line: str, on: bool) -> dict:
    _get_dp2031().set_trigger_in_enabled(line, on)
    return {"line": line, "trigger_in_enabled": on}


def dp2031_get_trigger_in_enabled(line: str) -> dict:
    return {"line": line,
            "trigger_in_enabled": _get_dp2031().get_trigger_in_enabled(line)}


def dp2031_set_trigger_in_type(line: str, type_: str) -> dict:
    """RISE / FALL / HIGH / LOW."""
    _get_dp2031().set_trigger_in_type(line, type_)
    return {"line": line, "trigger_in_type": type_}


def dp2031_get_trigger_in_type(line: str) -> dict:
    return {"line": line,
            "trigger_in_type": _get_dp2031().get_trigger_in_type(line)}


def dp2031_set_trigger_in_source(line: str, channels: list) -> dict:
    """List of channels (e.g. ``[1, 2]``) that respond to the trigger."""
    _get_dp2031().set_trigger_in_source(line, channels)
    return {"line": line, "channels": list(channels)}


def dp2031_get_trigger_in_source(line: str) -> dict:
    return {"line": line,
            "channels": _get_dp2031().get_trigger_in_source(line)}


def dp2031_set_trigger_in_response(line: str, response: str) -> dict:
    """ON / OFF / ALTER (toggle)."""
    _get_dp2031().set_trigger_in_response(line, response)
    return {"line": line, "trigger_in_response": response}


def dp2031_trigger_in_immediate() -> dict:
    """Fire one immediate trigger event regardless of line state."""
    _get_dp2031().trigger_in_immediate()
    return {"triggered": True}


def dp2031_set_trigger_out_enabled(line: str, on: bool) -> dict:
    _get_dp2031().set_trigger_out_enabled(line, on)
    return {"line": line, "trigger_out_enabled": on}


def dp2031_set_trigger_out_source(line: str, channel: int) -> dict:
    """Single channel — the trigger output fires when that channel changes state."""
    _get_dp2031().set_trigger_out_source(line, channel)
    return {"line": line, "channel": channel}


def dp2031_set_trigger_out_polarity(line: str, polarity: str) -> dict:
    """POSitive or NEGative."""
    _get_dp2031().set_trigger_out_polarity(line, polarity)
    return {"line": line, "polarity": polarity}


# ---------------------------------------------------------------------------
# Phase D — Memory / file system
# ---------------------------------------------------------------------------


def dp2031_list_files() -> dict:
    """List filenames in the current directory."""
    return {"files": _get_dp2031().list_files()}


def dp2031_change_directory(path: str) -> dict:
    """Change to ``path`` (e.g. ``C:/`` or ``D:/folder``)."""
    _get_dp2031().change_directory(path)
    return {"directory": path}


def dp2031_current_directory() -> dict:
    return {"directory": _get_dp2031().current_directory()}


def dp2031_delete_file(filename: str) -> dict:
    _get_dp2031().delete_file(filename)
    return {"deleted": filename}


def dp2031_store_file(filename: str) -> dict:
    """Save device state to ``filename`` (.RSF for state, .RTF for Arb)."""
    _get_dp2031().store_file(filename)
    return {"stored": filename}


def dp2031_load_file(filename: str) -> dict:
    """Load device state or Arb sequence from ``filename``."""
    _get_dp2031().load_file(filename)
    return {"loaded": filename}


def dp2031_external_disks() -> dict:
    """List mounted external USB disk roots."""
    return {"disks": _get_dp2031().external_disks()}


def dp2031_file_exists(filename: str) -> dict:
    return {"exists": _get_dp2031().file_exists(filename)}


# ---------------------------------------------------------------------------
# Phase D — License + screenshot
# ---------------------------------------------------------------------------


def dp2031_install_license(license_key: str) -> dict:
    """Install an option license key. Check ``dp2031_last_error`` after."""
    _get_dp2031().install_license(license_key)
    return {"license_install_attempted": True}


def dp2031_save_screenshot(path: str) -> dict:
    """Capture the display as a BMP and save to ``path``."""
    bytes_written = _get_dp2031().save_screenshot(path)
    return {"path": path, "bytes": bytes_written}


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
    # Phase B — protection trip / clear / delay
    dp2031_clear_ovp, dp2031_clear_ocp,
    dp2031_ovp_tripped, dp2031_ocp_tripped,
    dp2031_set_ocp_delay_ms, dp2031_get_ocp_delay_ms,
    # IEEE 488.2 status
    dp2031_event_status_register,
    dp2031_set_event_status_enable, dp2031_get_event_status_enable,
    dp2031_status_byte,
    dp2031_set_service_request_enable, dp2031_get_service_request_enable,
    dp2031_wait_op_complete, dp2031_self_test,
    dp2031_installed_options, dp2031_set_power_on_status_clear,
    dp2031_save_state, dp2031_recall_state,
    # :STATus subsystem
    dp2031_operation_event, dp2031_questionable_event,
    dp2031_channel_status_event, dp2031_health_check,
    # System basics
    dp2031_beep_once, dp2031_set_beeper, dp2031_get_beeper,
    dp2031_set_brightness, dp2031_get_brightness,
    dp2031_scpi_version,
    dp2031_set_keyboard_lock, dp2031_set_touchscreen_lock,
    dp2031_set_remote, dp2031_set_local,
    dp2031_set_screen_saver,
    dp2031_set_language, dp2031_set_power_on_mode,
    # Phase C — pair / tracking / sense / sampling
    dp2031_set_channel_pair, dp2031_get_channel_pair,
    dp2031_set_tracking, dp2031_get_tracking,
    dp2031_set_track_mode, dp2031_get_track_mode,
    dp2031_set_output_sync, dp2031_get_output_sync,
    dp2031_set_remote_sense, dp2031_get_remote_sense,
    dp2031_set_sampling_mode, dp2031_get_sampling_mode,
    # Phase C — step + apply + bounds
    dp2031_set_voltage_step, dp2031_get_voltage_step,
    dp2031_set_current_step, dp2031_get_current_step,
    dp2031_step_voltage_up, dp2031_step_voltage_down,
    dp2031_step_current_up, dp2031_step_current_down,
    dp2031_apply, dp2031_query_applied,
    dp2031_voltage_bounds, dp2031_current_bounds,
    # Phase D — Timer
    dp2031_set_timer_enabled, dp2031_get_timer_enabled,
    dp2031_set_timer_channel, dp2031_get_timer_channel,
    dp2031_set_timer_cycles, dp2031_get_timer_cycles,
    dp2031_set_timer_end_state, dp2031_get_timer_end_state,
    dp2031_set_timer_run_mode, dp2031_get_timer_run_mode,
    dp2031_set_timer_trigger, dp2031_get_timer_trigger,
    dp2031_get_timer_group_params, dp2031_delete_timer_groups,
    dp2031_program_timer,
    dp2031_set_timer_template, dp2031_construct_timer_from_template,
    # Phase D — Analyzer
    dp2031_set_analyzer_enabled, dp2031_get_analyzer_enabled,
    dp2031_set_analyzer_type, dp2031_get_analyzer_type,
    dp2031_set_analyzer_common_objects,
    dp2031_set_analyzer_save, dp2031_set_analyzer_save_path,
    # Phase D — Trigger I/O
    dp2031_set_trigger_in_enabled, dp2031_get_trigger_in_enabled,
    dp2031_set_trigger_in_type, dp2031_get_trigger_in_type,
    dp2031_set_trigger_in_source, dp2031_get_trigger_in_source,
    dp2031_set_trigger_in_response, dp2031_trigger_in_immediate,
    dp2031_set_trigger_out_enabled,
    dp2031_set_trigger_out_source, dp2031_set_trigger_out_polarity,
    # Phase D — Memory
    dp2031_list_files, dp2031_change_directory, dp2031_current_directory,
    dp2031_delete_file, dp2031_store_file, dp2031_load_file,
    dp2031_external_disks, dp2031_file_exists,
    # Phase D — License + screenshot
    dp2031_install_license, dp2031_save_screenshot,
)


def register_mcp_tools(mcp) -> None:
    """Register every Phase-A DP2031 MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

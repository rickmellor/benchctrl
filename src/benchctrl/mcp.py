"""MCP server orchestrator — exposes every driver's tools to MCP clients.

Designed for use with Claude Code, Claude Desktop, or any other
MCP-aware client. Run as ``benchctrl-mcp`` or ``python -m benchctrl.mcp``.

v1.0 architecture
-----------------
Each driver in :py:mod:`benchctrl.drivers` owns its MCP surface. The
driver-specific tools live in ``<driver>.mcp_tools`` modules and a
:py:func:`register_mcp_tools` function on each registers them on the
shared :py:class:`FastMCP` server. This module is the orchestrator —
it creates the server, calls each driver's registration, and adds the
cross-driver / framework tools (battery analytics, recording I/O on
saved files).

Connection model
----------------
The Arc SMU opens on first tool call and is held for the process
lifetime. The QR10x and DL3031A are opened explicitly via their own
``*_open`` tools. Closing the server (or each driver's ``*_disconnect``
/ ``*_close``) releases the device.

Safety
------
Only one tool drives voltage onto Arc output terminals: ``enable_output``.
It refuses unless the caller passes ``confirm_dut_attached=True`` AND a
``current_limit`` has been set. The DL3031A's ``set_input(True)`` is the
analogous load gate.

Concurrency
-----------
Single-client serialization assumed. The per-driver lock guards
open/close transitions only — per-tool calls don't take the lock,
since the singleton mutates only on open/close.
"""

from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from benchctrl.battery import (
    Battery,
    BatteryProfile,
    DischargeProfile,
    DischargeStep,
    DutyCycle,
    Emulator,
    EmulatorConfig,
    ExitConditions,
    Profiler,
    ProfilerConfig,
    duty_cycle_from_recording,
    estimate_life_constant_current,
    estimate_life_from_profile,
)
from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as _pdu41002_tools
from benchctrl.drivers.silabs_cp2112 import mcp_tools as _cp2112_tools
from benchctrl.drivers.eastwood_qr10x import mcp_tools as _qr10x_tools
from benchctrl.drivers.otii_arc import mcp_tools as _arc_tools
from benchctrl.drivers.rigol_dl3031a import mcp_tools as _dl3031a_tools
from benchctrl.drivers.rigol_dp2031 import mcp_tools as _dp2031_tools
from benchctrl.drivers.siglent_sdm4065a import mcp_tools as _sdm4065a_tools
from benchctrl.recording import Recording

log = logging.getLogger("benchctrl.mcp")

mcp = FastMCP("benchctrl")


# ---------------------------------------------------------------------------
# Driver tool registration — each driver owns its MCP surface.
# ---------------------------------------------------------------------------

_arc_tools.register_mcp_tools(mcp)
_qr10x_tools.register_mcp_tools(mcp)
_dl3031a_tools.register_mcp_tools(mcp)
_dp2031_tools.register_mcp_tools(mcp)
_sdm4065a_tools.register_mcp_tools(mcp)
_pdu41002_tools.register_mcp_tools(mcp)
_cp2112_tools.register_mcp_tools(mcp)


# ---------------------------------------------------------------------------
# Re-exports for backward-compatible imports (tests and external scripts
# may do ``from benchctrl.mcp import set_voltage`` or similar).
# ---------------------------------------------------------------------------

# Arc tools
from benchctrl.drivers.otii_arc.mcp_tools import (
    _close_smu,
    _get_smu,
    _render_plot_png,
    _save_recording_by_extension,
    _smu_state,
    _statistics_dict,
    disable_output,
    disconnect,
    enable_output,
    get_gpi,
    info,
    list_channels,
    live,
    reconnect,
    record,
    set_4wire,
    set_current_limit,
    set_current_limit_enabled,
    set_exp_5v,
    set_exp_voltage,
    set_gpo,
    set_power_regulation,
    set_range,
    set_uart,
    set_voltage,
    state,
    take_snapshot,
    versions,
    write_uart_tx,
)

# QR10x tools
from benchctrl.drivers.eastwood_qr10x.mcp_tools import (
    qr10x_actual_resistance,
    qr10x_close,
    qr10x_decr,
    qr10x_get_safety_limit,
    qr10x_get_setpoint,
    qr10x_get_temperature,
    qr10x_incr,
    qr10x_info,
    qr10x_open,
    qr10x_set_resistance,
    qr10x_set_safety_limit,
)

# DL3031A tools
from benchctrl.drivers.rigol_dl3031a.mcp_tools import (
    dl3031a_battery_stats,
    dl3031a_clear_status,
    dl3031a_close,
    dl3031a_configure_battery_test,
    dl3031a_configure_transient_pulse,
    dl3031a_fetch,
    dl3031a_fetch_current,
    dl3031a_fetch_power,
    dl3031a_fetch_resistance,
    dl3031a_fetch_voltage,
    dl3031a_get_current,
    dl3031a_get_current_range,
    dl3031a_get_function_mode,
    dl3031a_get_input,
    dl3031a_get_mode,
    dl3031a_get_power,
    dl3031a_get_resistance,
    dl3031a_get_slew,
    dl3031a_get_trigger_source,
    dl3031a_get_voltage,
    dl3031a_get_voltage_range,
    dl3031a_info,
    dl3031a_last_error,
    dl3031a_measure,
    dl3031a_measure_current,
    dl3031a_measure_power,
    dl3031a_measure_resistance,
    dl3031a_measure_voltage,
    dl3031a_open,
    dl3031a_program_list,
    dl3031a_raise_if_error,
    dl3031a_reset,
    dl3031a_set_current,
    dl3031a_set_current_range,
    dl3031a_set_function_mode,
    dl3031a_set_input,
    dl3031a_set_mode,
    dl3031a_set_power,
    dl3031a_set_resistance,
    dl3031a_set_slew,
    dl3031a_set_trigger_source,
    dl3031a_set_voltage,
    dl3031a_set_voltage_range,
    dl3031a_transient_enable,
    dl3031a_trigger,
)

# DP2031 tools (Phase A + B surface)
from benchctrl.drivers.rigol_dp2031.mcp_tools import (
    # Phase A
    dp2031_clear_status,
    dp2031_close,
    dp2031_current_channel,
    dp2031_get_current,
    dp2031_get_ocp_enabled,
    dp2031_get_ocp_level,
    dp2031_get_output,
    dp2031_get_ovp_enabled,
    dp2031_get_ovp_level,
    dp2031_get_voltage,
    dp2031_info,
    dp2031_last_error,
    dp2031_measure_all,
    dp2031_measure_all_channels,
    dp2031_measure_current,
    dp2031_measure_power,
    dp2031_measure_voltage,
    dp2031_open,
    dp2031_output_regulation,
    dp2031_raise_if_error,
    dp2031_reset,
    dp2031_select_channel,
    dp2031_set_current,
    dp2031_set_ocp_enabled,
    dp2031_set_ocp_level,
    dp2031_set_output,
    dp2031_set_output_all,
    dp2031_set_ovp_enabled,
    dp2031_set_ovp_level,
    dp2031_set_voltage,
    # Phase B — protection trip / clear / delay
    dp2031_beep_once,
    dp2031_channel_status_event,
    dp2031_clear_ocp,
    dp2031_clear_ovp,
    dp2031_event_status_register,
    dp2031_get_beeper,
    dp2031_get_brightness,
    dp2031_get_event_status_enable,
    dp2031_get_ocp_delay_ms,
    dp2031_get_service_request_enable,
    dp2031_health_check,
    dp2031_installed_options,
    dp2031_ocp_tripped,
    dp2031_operation_event,
    dp2031_ovp_tripped,
    dp2031_questionable_event,
    dp2031_recall_state,
    dp2031_save_state,
    dp2031_scpi_version,
    dp2031_self_test,
    dp2031_set_beeper,
    dp2031_set_brightness,
    dp2031_set_event_status_enable,
    dp2031_set_keyboard_lock,
    dp2031_set_language,
    dp2031_set_local,
    dp2031_set_ocp_delay_ms,
    dp2031_set_power_on_mode,
    dp2031_set_power_on_status_clear,
    dp2031_set_remote,
    dp2031_set_screen_saver,
    dp2031_set_service_request_enable,
    dp2031_set_touchscreen_lock,
    dp2031_status_byte,
    dp2031_wait_op_complete,
    # Phase C
    dp2031_apply,
    dp2031_current_bounds,
    dp2031_get_channel_pair,
    dp2031_get_current_step,
    dp2031_get_output_sync,
    dp2031_get_remote_sense,
    dp2031_get_sampling_mode,
    dp2031_get_track_mode,
    dp2031_get_tracking,
    dp2031_get_voltage_step,
    dp2031_query_applied,
    dp2031_set_channel_pair,
    dp2031_set_current_step,
    dp2031_set_output_sync,
    dp2031_set_remote_sense,
    dp2031_set_sampling_mode,
    dp2031_set_track_mode,
    dp2031_set_tracking,
    dp2031_set_voltage_step,
    dp2031_step_current_down,
    dp2031_step_current_up,
    dp2031_step_voltage_down,
    dp2031_step_voltage_up,
    dp2031_voltage_bounds,
    # Phase D — Timer
    dp2031_construct_timer_from_template,
    dp2031_delete_timer_groups,
    dp2031_get_timer_channel,
    dp2031_get_timer_cycles,
    dp2031_get_timer_enabled,
    dp2031_get_timer_end_state,
    dp2031_get_timer_group_params,
    dp2031_get_timer_run_mode,
    dp2031_get_timer_trigger,
    dp2031_program_timer,
    dp2031_set_timer_channel,
    dp2031_set_timer_cycles,
    dp2031_set_timer_enabled,
    dp2031_set_timer_end_state,
    dp2031_set_timer_run_mode,
    dp2031_set_timer_template,
    dp2031_set_timer_trigger,
    # Phase D — Analyzer
    dp2031_get_analyzer_enabled,
    dp2031_get_analyzer_type,
    dp2031_set_analyzer_common_objects,
    dp2031_set_analyzer_enabled,
    dp2031_set_analyzer_save,
    dp2031_set_analyzer_save_path,
    dp2031_set_analyzer_type,
    # Phase D — Trigger I/O
    dp2031_get_trigger_in_enabled,
    dp2031_get_trigger_in_source,
    dp2031_get_trigger_in_type,
    dp2031_set_trigger_in_enabled,
    dp2031_set_trigger_in_response,
    dp2031_set_trigger_in_source,
    dp2031_set_trigger_in_type,
    dp2031_set_trigger_out_enabled,
    dp2031_set_trigger_out_polarity,
    dp2031_set_trigger_out_source,
    dp2031_trigger_in_immediate,
    # Phase D — Memory
    dp2031_change_directory,
    dp2031_current_directory,
    dp2031_delete_file,
    dp2031_external_disks,
    dp2031_file_exists,
    dp2031_list_files,
    dp2031_load_file,
    dp2031_store_file,
    # Phase D — License + screenshot
    dp2031_install_license,
    dp2031_save_screenshot,
)

# SDM4065A tools
from benchctrl.drivers.siglent_sdm4065a.mcp_tools import (
    sdm4065a_abort,
    sdm4065a_clear_device_buffers,
    sdm4065a_clear_status,
    sdm4065a_close,
    sdm4065a_command_error,
    sdm4065a_drain_errors,
    sdm4065a_configure_dc_voltage,
    sdm4065a_configure_resistance,
    sdm4065a_fetch,
    sdm4065a_get_autorange,
    sdm4065a_get_autozero,
    sdm4065a_get_configuration,
    sdm4065a_get_function,
    sdm4065a_get_nplc,
    sdm4065a_get_null,
    sdm4065a_get_range,
    sdm4065a_get_sample_count,
    sdm4065a_get_temperature_unit,
    sdm4065a_info,
    sdm4065a_initiate,
    sdm4065a_last_error,
    sdm4065a_measure_ac_current,
    sdm4065a_measure_ac_voltage,
    sdm4065a_measure_capacitance,
    sdm4065a_measure_continuity,
    sdm4065a_measure_dc_current,
    sdm4065a_measure_dc_voltage,
    sdm4065a_measure_diode,
    sdm4065a_measure_frequency,
    sdm4065a_measure_period,
    sdm4065a_measure_resistance,
    sdm4065a_measure_resistance_4wire,
    sdm4065a_measure_temperature,
    sdm4065a_null_now,
    sdm4065a_open,
    sdm4065a_query,
    sdm4065a_raise_if_error,
    sdm4065a_read,
    sdm4065a_read_nulled,
    sdm4065a_reading_timeout_ms,
    sdm4065a_reset,
    sdm4065a_self_test,
    sdm4065a_set_autorange,
    sdm4065a_set_autozero,
    sdm4065a_set_function,
    sdm4065a_set_nplc,
    sdm4065a_set_null,
    sdm4065a_set_null_auto,
    sdm4065a_set_null_value,
    sdm4065a_set_range,
    sdm4065a_set_sample_count,
    sdm4065a_set_temperature_unit,
    sdm4065a_standard_event_status,
    sdm4065a_write,
)

# PDU41002 tools — read-only in this build: nothing here switches mains.
from benchctrl.drivers.silabs_cp2112.mcp_tools import (
    cp2112_allowed_lines,
    cp2112_close,
    cp2112_info,
    cp2112_line_state,
    cp2112_line_states,
    cp2112_open,
    cp2112_reset_lines,
    cp2112_set_line_asserted,
    cp2112_set_line_mode,
    cp2112_trigger_reset_pulse,
)
from benchctrl.drivers.cyberpower_pdu41002.mcp_tools import (
    pdu41002_allowed_outlets,
    pdu41002_clear_outlet_command,
    pdu41002_close,
    pdu41002_info,
    pdu41002_measure_frequency,
    pdu41002_measure_load,
    pdu41002_measure_voltage,
    pdu41002_open,
    pdu41002_outlet_config,
    pdu41002_outlet_state,
    pdu41002_outlet_states,
    pdu41002_reset_outlet,
    pdu41002_set_outlet_state,
    pdu41002_status,
    pdu41002_transport,
)


# ---------------------------------------------------------------------------
# Cross-driver / framework tools — these aren't owned by any single driver.
#   - plot_recording / recording_summary / export_recording: work on saved
#     .opensmu files, no live SMU required
#   - battery_*: analytics + emulator/profiler that use any SMU implementing
#     the SourceMeasurementUnit Protocol. The emulator/profiler today drive
#     the Arc since it's the only Protocol implementer, but the *tools* are
#     framework-level — they don't know about wire formats.
# ---------------------------------------------------------------------------


@mcp.tool()
def plot_recording(
    input_path: str,
    output_png: str,
    channels: Optional[list[str]] = None,
    title: Optional[str] = None,
) -> dict:
    """Render a matplotlib quick-look PNG from a saved ``.opensmu`` file.

    Loads the recording from disk (no SMU connection needed) and writes
    one subplot per channel with a shared x-axis.

    Args:
        input_path: path to a ``.opensmu`` file.
        output_png: path for the rendered PNG.
        channels: optional list of channel codes to plot (defaults to all).
        title: optional plot title (defaults to the recording's name).

    Requires ``benchctrl[plot]`` installed. Useful for "open this saved
    capture and show me what it looks like" without ever touching the
    SMU.
    """
    rec = Recording.load(input_path)
    out = _render_plot_png(rec, output_png, channels=channels, title=title)
    return {
        "input": input_path,
        "output": str(out),
        "channels": sorted({c.code for c in rec.channels}),
        "name": rec.name,
    }


@mcp.tool()
def recording_summary(input_path: str) -> dict:
    """Inspect a saved ``.opensmu`` file without running a new capture.

    Returns name, start/end times, offset, and per-channel statistics
    (the same shape ``record`` returns for a live capture). Useful for
    "tell me about this capture" or "compare these two runs" workflows.

    Does not require an SMU connection.
    """
    rec = Recording.load(input_path)
    return {
        "input": input_path,
        "name": rec.name,
        "offset_s": rec.offset,
        "start_time": rec.start_time.isoformat() if rec.start_time else None,
        "end_time": rec.end_time.isoformat() if rec.end_time else None,
        "device_info": rec.device_info,
        "channels": _statistics_dict(rec),
    }


@mcp.tool()
def export_recording(
    input_path: str,
    output_path: str,
) -> dict:
    """Convert a saved ``.opensmu`` recording to another format.

    Output format is selected by the ``output_path`` extension:
    ``.csv`` / ``.json`` / ``.parquet`` / ``.opensmu``. Parquet output
    requires ``benchctrl[parquet]`` installed.

    Useful for "share this recording in parquet" or "give me CSV for
    the spreadsheet" without re-running the capture.
    """
    rec = Recording.load(input_path)
    out = _save_recording_by_extension(rec, Path(output_path))
    return {
        "input": input_path,
        "output": str(out),
        "format": out.suffix.lstrip("."),
    }


# ---------------------------------------------------------------------------
# Battery profile / analytics tools (no live SMU required)
# ---------------------------------------------------------------------------


@mcp.tool()
def battery_profile_summary(path: str) -> dict:
    """Load a battery profile JSON and return a human-readable summary.

    Compatible with profiles produced by Qoitech's Otii application (bundled
    profiles live in ``%LOCALAPPDATA%\\otii3\\app-*\\resources\\batteryprofiles``).
    Returns nominal voltage and capacity, cutoff voltage, all temperatures
    covered, the discharge profile (high/low pulse load), and metadata for
    every discharge table.

    No SMU connection required.
    """
    p = BatteryProfile.load(path)
    return {"input": path, **p.summary()}


@mcp.tool()
def battery_life_estimate(
    capacity_mAh: float,
    active_current_A: float,
    active_time_s: float,
    sleep_current_A: float,
    sleep_time_s: float,
    self_discharge_per_month_pct: float = 0.0,
    safety_margin_pct: float = 0.0,
) -> dict:
    """Estimate battery life with a constant-current model.

    Treats the cell as a flat-voltage capacity reservoir. Pass:
        - cell capacity (mAh)
        - active phase current (A) + duration (s)
        - sleep phase current (A) + duration (s)
        - optional self-discharge rate (% of capacity per month)
        - optional safety margin (% of capacity reserved)

    Returns runtime in seconds + human-readable form, average current,
    iterations, capacity consumed, self-discharge loss.

    No SMU connection and no battery profile required.
    """
    duty = DutyCycle(
        active_current_A=active_current_A,
        active_time_s=active_time_s,
        sleep_current_A=sleep_current_A,
        sleep_time_s=sleep_time_s,
    )
    est = estimate_life_constant_current(
        capacity_mAh=capacity_mAh,
        duty_cycle=duty,
        self_discharge_per_month_pct=self_discharge_per_month_pct,
        safety_margin_pct=safety_margin_pct,
    )
    return est.to_dict()


@mcp.tool()
def battery_life_estimate_from_profile(
    profile_path: str,
    active_current_A: float,
    active_time_s: float,
    sleep_current_A: float,
    sleep_time_s: float,
    temperature: Optional[float] = None,
    self_discharge_per_month_pct: float = 0.0,
    safety_margin_pct: float = 0.0,
    cutoff_voltage_V: Optional[float] = None,
) -> dict:
    """Estimate battery life by iterating against a battery profile's discharge curve.

    More accurate than the constant-current estimator when the cell's
    OCV varies significantly over discharge. Matches Otii's Battery Life
    Calculator semantics.

    Args:
        profile_path: path to a battery profile JSON (Otii-format).
        active_current_A / active_time_s: active phase load.
        sleep_current_A / sleep_time_s: sleep phase load.
        temperature: optional table selector for multi-temperature profiles.
        self_discharge_per_month_pct: optional self-discharge rate.
        safety_margin_pct: optional reserved-capacity fraction.
        cutoff_voltage_V: optional override of the profile's cutoff voltage.

    Returns runtime + iterations + final voltage + stop reason.
    No SMU connection required.
    """
    profile = BatteryProfile.load(profile_path)
    duty = DutyCycle(
        active_current_A=active_current_A,
        active_time_s=active_time_s,
        sleep_current_A=sleep_current_A,
        sleep_time_s=sleep_time_s,
    )
    est = estimate_life_from_profile(
        profile=profile,
        duty_cycle=duty,
        temperature=temperature,
        self_discharge_per_month_pct=self_discharge_per_month_pct,
        safety_margin_pct=safety_margin_pct,
        cutoff_voltage=cutoff_voltage_V,
    )
    out = est.to_dict()
    out["profile_path"] = profile_path
    out["profile_battery"] = {
        "manufacturer": profile.battery.manufacturer,
        "model": profile.battery.model,
        "nominal_voltage_V": profile.nominal_voltage,
        "nominal_capacity_mAh": profile.nominal_capacity_mAh,
    }
    return out


@mcp.tool()
def battery_life_from_recording(
    recording_path: str,
    active_window_start_s: float,
    active_window_end_s: float,
    sleep_window_start_s: float,
    sleep_window_end_s: float,
    profile_path: Optional[str] = None,
    capacity_mAh: Optional[float] = None,
    self_discharge_per_month_pct: float = 0.0,
    safety_margin_pct: float = 0.0,
    temperature: Optional[float] = None,
) -> dict:
    """Estimate battery life using duty-cycle data extracted from a saved recording.

    Otii's "Get from selection" workflow: you observed an active region
    and a sleep region in a captured run, the tool averages main current
    over each window and uses those as the active/sleep load.

    Either ``profile_path`` (uses profile-based estimator) OR
    ``capacity_mAh`` (uses constant-current estimator) must be provided.
    """
    if profile_path is None and capacity_mAh is None:
        return {
            "error": "REFUSED: must provide profile_path or capacity_mAh",
            "guidance": (
                "Pass profile_path=<path to a battery profile JSON> for "
                "the curve-aware estimator, or capacity_mAh=<float> for "
                "the constant-current estimator."
            ),
        }
    rec = Recording.load(recording_path)
    duty = duty_cycle_from_recording(
        rec,
        active_window=(active_window_start_s, active_window_end_s),
        sleep_window=(sleep_window_start_s, sleep_window_end_s),
    )
    if profile_path is not None:
        profile = BatteryProfile.load(profile_path)
        est = estimate_life_from_profile(
            profile=profile,
            duty_cycle=duty,
            temperature=temperature,
            self_discharge_per_month_pct=self_discharge_per_month_pct,
            safety_margin_pct=safety_margin_pct,
        )
    else:
        assert capacity_mAh is not None  # for type-checker
        est = estimate_life_constant_current(
            capacity_mAh=capacity_mAh,
            duty_cycle=duty,
            self_discharge_per_month_pct=self_discharge_per_month_pct,
            safety_margin_pct=safety_margin_pct,
        )
    out = est.to_dict()
    out["recording_path"] = recording_path
    out["duty_cycle_from_recording"] = {
        "active_current_A": duty.active_current_A,
        "active_time_s": duty.active_time_s,
        "sleep_current_A": duty.sleep_current_A,
        "sleep_time_s": duty.sleep_time_s,
    }
    if profile_path:
        out["profile_path"] = profile_path
    return out


@mcp.tool()
def battery_profile_lookup(
    path: str,
    used_capacity_mAh: float,
    temperature: Optional[float] = None,
) -> dict:
    """Interpolate the OCV and ESR of a battery profile at a given used capacity.

    Args:
        path: path to a battery profile JSON.
        used_capacity_mAh: how much capacity has been drawn from the cell, in mAh.
        temperature: optional temperature selector. If the profile contains
            multiple discharge tables, the nearest temperature is used.
            If only one table is present, this argument is optional.

    Returns the interpolated open-circuit voltage (V), equivalent series
    resistance (Ω), and the temperature of the table used. Useful for
    "at 50% SoC on a CR2032, what's the cell voltage I should expect?".
    """
    p = BatteryProfile.load(path)
    table = p.select_table(temperature=temperature)
    return {
        "input": path,
        "used_capacity_mAh": used_capacity_mAh,
        "temperature": table.temperature,
        "temperature_unit": table.temperature_unit,
        "ocv_V": table.ocv_at(used_capacity_mAh),
        "esr_ohm": table.esr_at(used_capacity_mAh),
    }


@mcp.tool()
def battery_profiler_estimate_duration(
    capacity_mAh: float,
    high_current_A: float,
    high_time_s: float,
    low_current_A: float,
    low_time_s: float,
) -> dict:
    """Estimate how long a profiler run will take, before kicking one off.

    Rough math: charge consumed per cycle = sum of high+low current*time;
    cycles to depletion = capacity / charge_per_cycle; wall-clock duration =
    cycles * (high_time + low_time + measurement overhead).

    Use this before calling :py:meth:`battery_profiler_run` so you know
    whether to start a 10 s run or a 12 hr one.
    """
    cycle_time_s = high_time_s + low_time_s
    if cycle_time_s <= 0:
        return {"error": "high_time_s + low_time_s must be > 0"}
    cycle_charge_mAh = (
        (high_current_A * high_time_s) + (low_current_A * low_time_s)
    ) / 3.6
    if cycle_charge_mAh <= 0:
        return {"error": "no net charge drawn per cycle"}
    cycles = capacity_mAh / cycle_charge_mAh
    raw_seconds = cycles * cycle_time_s
    # Overhead: measurement window + relaxation per cycle (~0.6 s default)
    overhead_seconds = cycles * 0.6
    total_seconds = raw_seconds + overhead_seconds

    from benchctrl.battery.calculator import _humanize_seconds

    return {
        "estimated_cycles": cycles,
        "estimated_duration_s": total_seconds,
        "estimated_duration_human": _humanize_seconds(total_seconds),
        "cycle_time_s": cycle_time_s,
        "cycle_charge_mAh": cycle_charge_mAh,
    }


@mcp.tool()
def battery_profiler_run(
    output_path: str,
    high_current_A: float,
    high_time_s: float,
    low_current_A: float,
    low_time_s: float,
    capacity_mAh: float,
    nominal_voltage_V: float,
    manufacturer: str = "",
    model: str = "",
    temperature: float = 25.0,
    cutoff_voltage_V: float = 0.0,
    cutoff_ocv_V: float = 0.0,
    max_iterations: int = 0,
) -> dict:
    """Run a full battery profiler discharge and save the resulting profile.

    SAFETY-CRITICAL: this draws current from a real battery connected to
    the SMU's output terminals. Verify the battery + connections before
    calling. The output is enabled for the duration of the run; the
    cell will be drained according to the configured discharge profile.

    DURATION WARNING: profiling can take many hours. Call
    :py:meth:`battery_profiler_estimate_duration` first to know what
    you're committing to. Most MCP clients will time out before a real
    profiling run completes — prefer the Python API
    (``benchctrl.battery.Profiler``) for anything beyond a short demo.

    The discharge profile uses **current mode only** in phase 3 — power
    and resistance modes are tracked for later.
    """
    smu = _get_smu()
    config = ProfilerConfig(
        discharge_profile=DischargeProfile(
            low=DischargeStep("current", low_current_A, low_time_s),
            high=DischargeStep("current", high_current_A, high_time_s),
            exit_conditions=ExitConditions(
                iterations=max_iterations,
                ocv=cutoff_ocv_V,
                voltage=cutoff_voltage_V,
            ),
        ),
        battery=Battery(
            capacity=capacity_mAh,
            capacity_unit="mAh",
            voltage=nominal_voltage_V,
            voltage_unit="V",
            manufacturer=manufacturer,
            model=model,
        ),
        temperature=temperature,
    )
    profiler = Profiler(smu, config)
    result = profiler.run()
    out = result.profile.save(output_path)
    return {
        "saved_to": str(out),
        "iterations": len(result.samples),
        "runtime_s": result.runtime_s,
        "stop_reason": result.stop_reason,
        "aborted": result.aborted,
        "first_sample": result.samples[0].__dict__ if result.samples else None,
        "last_sample": result.samples[-1].__dict__ if result.samples else None,
    }


# ---------------------------------------------------------------------------
# Battery emulator — stateful background loop attached to the SMU
# ---------------------------------------------------------------------------

_emulator: Optional[Emulator] = None


@mcp.tool()
def battery_emulator_start(
    profile_path: str,
    initial_soc: float = 1.0,
    series: int = 1,
    parallel: int = 1,
    temperature: Optional[float] = None,
    soc_tracking: bool = True,
    safety_max_voltage_V: float = 5.0,
    current_limit_A: float = 0.5,
    voltage_range: Optional[str] = None,
    update_interval_s: float = 0.01,
    safety_max_used_mAh: Optional[float] = None,
    soc_floor: float = 0.0,
) -> dict:
    """Start the battery emulator with a host-side control loop.

    SAFETY-CRITICAL: drives voltage onto the output terminals. The
    initial output voltage is the battery's open-circuit voltage at
    ``initial_soc``. Connect your DUT before calling; verify
    ``safety_max_voltage_V`` matches what the DUT can tolerate.

    A background thread runs at ``update_interval_s`` (default 100 Hz):

    1. Reads main current from the device.
    2. Integrates to update state of charge (if ``soc_tracking=True``).
    3. Looks up OCV(SoC) and ESR(SoC) from the profile.
    4. Applies series / parallel multipliers.
    5. Writes ``V = OCV − I·ESR`` (clamped to safety cap).

    The emulator continues until ``battery_emulator_stop`` is called,
    or one of the safety limits fires (``safety_max_used_mAh`` reached,
    ``soc_floor`` reached).

    Only one emulator can run at a time. Returns the initial state.
    """
    global _emulator
    if _emulator is not None:
        return {
            "error": "REFUSED: emulator already running",
            "guidance": "Call battery_emulator_stop() first.",
        }
    smu = _get_smu()
    profile = BatteryProfile.load(profile_path)
    config = EmulatorConfig(
        profile=profile,
        initial_soc=initial_soc,
        series=series,
        parallel=parallel,
        temperature=temperature,
        soc_tracking=soc_tracking,
        safety_max_voltage_V=safety_max_voltage_V,
        current_limit_A=current_limit_A,
        voltage_range=voltage_range,
        update_interval_s=update_interval_s,
        safety_max_used_mAh=safety_max_used_mAh,
        soc_floor=soc_floor,
    )
    _emulator = Emulator(smu, config)
    _emulator.start()
    return {
        "started": True,
        "profile_path": profile_path,
        "state": _emulator.state().__dict__,
    }


@mcp.tool()
def battery_emulator_state() -> dict:
    """Snapshot the running emulator's state.

    Returns SoC, used capacity (mAh), OCV (V), ESR (Ω), output voltage,
    measured current, runtime, iteration count, running flag, stop
    reason if stopped.
    """
    if _emulator is None:
        return {"error": "no emulator running", "guidance": "Call battery_emulator_start() first."}
    return _emulator.state().__dict__


@mcp.tool()
def battery_emulator_stop() -> dict:
    """Stop the running emulator and disable the output.

    Idempotent — safe to call when no emulator is running.
    """
    global _emulator
    if _emulator is None:
        return {"stopped": False, "note": "no emulator was running"}
    final = _emulator.state()
    _emulator.stop()
    state = _emulator.state()
    _emulator = None
    return {
        "stopped": True,
        "final_state": state.__dict__,
        "iterations_at_stop": final.iteration,
    }


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def main() -> None:
    """``benchctrl-mcp`` entry point. Runs the FastMCP server on stdio."""
    try:
        mcp.run()
    finally:
        _close_smu()


if __name__ == "__main__":
    main()

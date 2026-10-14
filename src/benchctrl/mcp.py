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
import sys

from mcp.server.fastmcp import FastMCP

from benchctrl import framework_tools as _framework_tools
from benchctrl.drivers.bench_vision import mcp_tools as _vision_tools
from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as _pdu41002_tools
from benchctrl.drivers.silabs_cp2112 import mcp_tools as _cp2112_tools
from benchctrl.drivers.eastwood_qr10x import mcp_tools as _qr10x_tools
from benchctrl.drivers.ontrak_adu218 import mcp_tools as _adu218_tools
from benchctrl.drivers.otii_arc import mcp_tools as _arc_tools
from benchctrl.drivers.rigol_dl3031a import mcp_tools as _dl3031a_tools
from benchctrl.drivers.rigol_dp2031 import mcp_tools as _dp2031_tools
from benchctrl.drivers.siglent_sdg1032x import mcp_tools as _sdg1032x_tools
from benchctrl.drivers.siglent_sdm4065a import mcp_tools as _sdm4065a_tools
from benchctrl.config import DEVICE_KEYS

log = logging.getLogger("benchctrl.mcp")

mcp = FastMCP("benchctrl")


# ---------------------------------------------------------------------------
# Tool registration — each driver owns its MCP surface, and the cross-driver
# framework tools own theirs. Registering all nine the same way is what keeps
# ``_TOOLS`` and ``mcp.list_tools()`` in agreement, which the generated CLI
# depends on: it enumerates the ``_TOOLS`` tuples so it need not import
# FastMCP. A tool declared here with a bare ``@mcp.tool()`` would be invisible
# to that route and would silently vanish from the CLI.
# ---------------------------------------------------------------------------

_arc_tools.register_mcp_tools(mcp)
_qr10x_tools.register_mcp_tools(mcp)
_dl3031a_tools.register_mcp_tools(mcp)
_dp2031_tools.register_mcp_tools(mcp)
_sdm4065a_tools.register_mcp_tools(mcp)
_sdg1032x_tools.register_mcp_tools(mcp)
_pdu41002_tools.register_mcp_tools(mcp)
_cp2112_tools.register_mcp_tools(mcp)
_adu218_tools.register_mcp_tools(mcp)
_vision_tools.register_mcp_tools(mcp)
_framework_tools.register_mcp_tools(mcp)


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
    dp2031_disable_outputs,
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

# SDG1032X tools — setters return the instrument's read-back; no error queue.
from benchctrl.drivers.siglent_sdg1032x.mcp_tools import (
    sdg1032x_apply_channel_copy,
    sdg1032x_apply_equal_phase,
    sdg1032x_close,
    sdg1032x_disable_outputs,
    sdg1032x_get_arb,
    sdg1032x_get_basic_wave,
    sdg1032x_get_burst,
    sdg1032x_get_buzzer,
    sdg1032x_get_clock,
    sdg1032x_get_combine,
    sdg1032x_get_coupling,
    sdg1032x_get_harmonics,
    sdg1032x_get_invert,
    sdg1032x_get_lan_config,
    sdg1032x_get_language,
    sdg1032x_get_modulation,
    sdg1032x_get_number_format,
    sdg1032x_get_output,
    sdg1032x_get_phase_mode,
    sdg1032x_get_power_on_config,
    sdg1032x_get_protection,
    sdg1032x_get_screen_saver,
    sdg1032x_get_sweep,
    sdg1032x_get_sync,
    sdg1032x_info,
    sdg1032x_list_arbs,
    sdg1032x_open,
    sdg1032x_operation_complete,
    sdg1032x_query,
    sdg1032x_read_arb,
    sdg1032x_read_counter,
    sdg1032x_read_screen,
    sdg1032x_reset,
    sdg1032x_select_arb,
    sdg1032x_set_amplitude,
    sdg1032x_set_basic_wave,
    sdg1032x_set_burst,
    sdg1032x_set_buzzer,
    sdg1032x_set_clock,
    sdg1032x_set_combine,
    sdg1032x_set_counter,
    sdg1032x_set_coupling,
    sdg1032x_set_frequency,
    sdg1032x_set_harmonics,
    sdg1032x_set_invert,
    sdg1032x_set_lan_config,
    sdg1032x_set_language,
    sdg1032x_set_max_amplitude,
    sdg1032x_set_modulation,
    sdg1032x_set_number_format,
    sdg1032x_set_offset,
    sdg1032x_set_output,
    sdg1032x_set_output_load,
    sdg1032x_set_output_polarity,
    sdg1032x_set_phase,
    sdg1032x_set_phase_mode,
    sdg1032x_set_power_on_config,
    sdg1032x_set_protection,
    sdg1032x_set_screen_saver,
    sdg1032x_set_sweep,
    sdg1032x_set_sync,
    sdg1032x_set_wave_type,
    sdg1032x_trigger_burst,
    sdg1032x_trigger_key,
    sdg1032x_trigger_sweep,
    sdg1032x_write,
    sdg1032x_write_arb,
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

# Vision tools
from benchctrl.drivers.bench_vision.mcp_tools import (
    vision_classify,
    vision_clear_crop,
    vision_close,
    vision_detect,
    vision_frame,
    vision_info,
    vision_open,
    vision_set_crop,
    vision_set_exposure_us,
    vision_set_fps,
    vision_set_gain_db,
    vision_status,
    vision_trigger_capture,
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

# ADU218 tools. Switching here is signal-level (1 A SSRs on instrument leads),
# not mains — but adu218_set_watchdog arms a hardware interlock that
# de-energises every relay on silence, so it is not a read-only surface either.
from benchctrl.drivers.ontrak_adu218.mcp_tools import (
    adu218_allowed_relays,
    adu218_clear_counter,
    adu218_close,
    adu218_counter,
    adu218_counters,
    adu218_debounce,
    adu218_info,
    adu218_input_port_mask,
    adu218_input_state,
    adu218_input_states,
    adu218_open,
    adu218_relay_state,
    adu218_relay_states,
    adu218_reset_relays,
    adu218_set_debounce,
    adu218_set_relay_port,
    adu218_set_relay_state,
    adu218_set_watchdog,
    adu218_watchdog,
)


# ---------------------------------------------------------------------------
# Vision: capture-and-label (cross-device — it commands the PDU or CP2112 and
# captures with the camera). See docs/vision.md § Label loop.
# ---------------------------------------------------------------------------


@mcp.tool()
def vision_label_capture(spec: dict, out_dir: str, sanity: bool = False) -> dict:
    """Build a labelled frame set from states benchctrl commands itself.

    ``spec`` is the label-loop spec (``docs/vision.md`` § Label loop): a name, a
    list of states — each an actuator to command (a PDU outlet, a CP2112 line,
    or the bench box's own ``sysfs_led``) and the label frames taken in that
    state receive — plus frames per state, rounds, settle time, and optional
    camera setup (crop, exposure, gain). The loop commands each state, waits,
    fires N ``seq``-tagged captures, writes ``frames/<label>/<seq>.jpg`` under
    ``out_dir`` with ``manifest.json`` and ``labels.csv``, and **restores every
    actuator to how it was found** — also when something fails half way.

    Uses the devices already opened by ``vision_open`` and, as the spec needs
    them, ``pdu41002_open`` / ``cp2112_open``; their allow-lists apply
    unchanged (an outlet or line outside them is refused by the driver). The
    ``sysfs_led`` actuator drives a host LED and is only meaningful when this
    server runs on the bench box itself.

    A frame that comes back with the wrong ``seq`` is discarded, never
    labelled. ``sanity=True`` stores a mean-brightness read of
    ``spec.sanity_roi`` beside each frame (needs Pillow on this host) so a
    frame whose pixels disagree with its label can be flagged.

    Returns the manifest summary: frame counts per label, discards, whether
    the restore succeeded, and the spec digest to cite from a training run.
    """
    from benchctrl.vision.labelloop import (
        LabelSpec,
        build_actuators,
        mean_brightness,
        run_label_capture,
    )

    parsed = LabelSpec.from_dict(spec)
    kinds = {s.actuator["device"] for s in parsed.states}
    devices: dict = {}
    if "cyberpower_pdu41002" in kinds:
        devices["cyberpower_pdu41002"] = _pdu41002_tools._get_pdu()
    if "silabs_cp2112" in kinds:
        devices["silabs_cp2112"] = _cp2112_tools._get_dev()
    manifest = run_label_capture(
        _vision_tools._get_vision(),
        build_actuators(devices),
        parsed,
        out_dir,
        sanity=mean_brightness if sanity else None,
    )
    return manifest.summary()


# ---------------------------------------------------------------------------
# Cross-driver / framework tools — these aren't owned by any single driver.
#   - plot_recording / recording_summary / export_recording: work on saved
#     .opensmu files, no live SMU required
#   - battery_*: analytics + emulator/profiler that use any SMU implementing
#     the SourceMeasurementUnit Protocol. The emulator/profiler today drive
#     the Arc since it's the only Protocol implementer, but the *tools* are
#     framework-level — they don't know about wire formats.
#
# They live in :py:mod:`benchctrl.framework_tools` and are registered above
# like every driver. Re-exported here so ``from benchctrl.mcp import
# plot_recording`` keeps working.
# ---------------------------------------------------------------------------

from benchctrl.framework_tools import (  # noqa: E402
    battery_emulator_start,
    battery_emulator_state,
    battery_emulator_stop,
    battery_life_estimate,
    battery_life_estimate_from_profile,
    battery_life_from_recording,
    battery_profile_lookup,
    battery_profile_summary,
    battery_profiler_estimate_duration,
    battery_profiler_run,
    export_recording,
    plot_recording,
    recording_summary,
)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------


def install_config() -> None:
    """Bind devices to local / remote / sim before the first tool call.

    Split out of :py:func:`main` so it is testable without starting a stdio
    server. Without this, :py:func:`benchctrl.session.resolve` reads a module
    global that is never populated, so ``BENCHCTRL_REMOTE`` /
    ``BENCHCTRL_SIM_DEVICES`` / ``~/.config/benchctrl/config.json`` are inert
    and a device the operator asked to *simulate* opens the real instrument
    instead. That failure is silent and points the wrong way — the tools
    cannot tell the three apart, which is the whole point of the seam.

    A malformed config is fatal on purpose. The alternative is to warn and
    carry on, which means driving hardware the operator did not ask for.
    """
    from benchctrl import session

    cfg = session.configure_from_environment()
    if cfg.is_all_local:
        log.debug("benchctrl-mcp: nothing configured; every device is local")
        return
    # Announced on stderr rather than through ``log``, because an MCP server
    # inherits no logging configuration from its client and stdout is the
    # JSON-RPC channel. A non-local binding is exactly the thing an operator
    # must be able to see they got.
    for key in DEVICE_KEYS:
        mode = cfg.mode_for(key)
        if mode != "local":
            print(f"benchctrl-mcp: {key} -> {mode}", file=sys.stderr)


def main() -> None:
    """``benchctrl-mcp`` entry point. Runs the FastMCP server on stdio."""
    from benchctrl import session

    install_config()
    try:
        mcp.run()
    finally:
        _close_smu()
        # Remote devices outlive this process unless the client disconnects
        # cleanly: the agent reads a clean disconnect as consent to drop the
        # writer claim and drive an armed device to its safe state.
        session.shutdown()


if __name__ == "__main__":
    main()

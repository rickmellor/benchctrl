"""Write-risk tiers for every generated CLI subcommand.

The CLI generates one subcommand per MCP tool. That is the point — new tools
appear automatically — but a *gate* cannot be generated, because risk is not a
property of a function's name. This module is the explicit classification, one
entry per tool, checked for completeness by a test that fails when a tool is
added without a tier.

Why not reuse ``dispatch.is_mutator()``
--------------------------------------
The obvious design is to import the agent's own predicate so the CLI gate can
never drift from the wire gate. It was measured and rejected:
``is_mutator("write")`` is **False** — the predicate is a bare name-prefix
match over 16 prefixes (``set_``, ``enable_``, ``reset``, …), and
``sdm4065a_write`` sends arbitrary SCPI while matching none of them. A CLI that
trusted it would file raw instrument writes under "safe, no confirmation". The
predicate is right for what it does (it is a *floor* on the writer claim, not a
risk model) and wrong for this.

The same reasoning cuts the other way too: ``take_snapshot`` and ``dp2031_apply``
match a mutator prefix, and ``pdu41002_reset_outlet`` cycles mains, while
``dp2031_step_voltage_up`` moves a real output and matches nothing.

Tiers
-----
``READ``
    Observes. No confirmation, no gate. A read that costs time (a 500 ms DMM
    integration) is still a read.

``TIER2``
    Changes instrument state that cannot itself energise anything, or changes a
    setpoint while the output is off. Runs without confirmation, because
    requiring ``--yes`` for ``set_voltage`` would train the operator to pass it
    reflexively — which is how a confirmation stops being one.

``TIER1``
    Drives, energises, switches, or hands raw bytes to an instrument. Requires
    ``--yes``. This is where an output turns on, a relay closes, mains switches,
    or arbitrary SCPI is passed through.

``TIER1_ENV``
    Tier 1 plus a second factor: an environment variable the operator must set,
    naming the specific hazard. Reserved for operations whose consequence
    outlives the command — arming a hardware watchdog that will trip later, or
    switching mains — where "I typed --yes" is not evidence the operator knew
    what was attached.

Curated omission is the fourth mechanism
----------------------------------------
Some operations are not tiered because they are **not generated at all**. That
is deliberate and it is the dominant safety pattern in this repo: ``menumode``
on the PDU (a one-way trap), ``console telnet enable`` (silently disables SSH),
``allow_alternate_function`` on the CP2112 (an operator observation, not a
parameter), ``calibrate`` and ``firmware_upgrade`` (in the agent's
``GLOBAL_DENY``). Reflection cannot see an omission, so :py:data:`SUPPRESSED`
records the ones that are *parameters of generated tools* and must be stripped
from the CLI surface even though the tool itself ships.
"""

from __future__ import annotations

READ = "read"
TIER2 = "tier2"
TIER1 = "tier1"
TIER1_ENV = "tier1_env"

#: Tiers in increasing order of what the operator must do.
TIERS = (READ, TIER2, TIER1, TIER1_ENV)

#: Environment variable required by each ``TIER1_ENV`` tool. The variable
#: *names the hazard*, so the operator's authorisation is specific rather than
#: blanket — setting one does not enable the other.
ENV_GATES = {
    "pdu41002_set_outlet_state": "BENCHCTRL_PDU_ALLOW_SWITCHING",
    "pdu41002_reset_outlet": "BENCHCTRL_PDU_ALLOW_SWITCHING",
    "adu218_set_watchdog": "BENCHCTRL_ADU218_ARM_WATCHDOG",
}

#: Tool parameters the CLI must never expose as a flag, with the reason. These
#: are not "advanced options" — each is a decision that requires standing at
#: the bench, so a flag would let a caller assert something it cannot know.
#:
#: Only parameters that *exist* on a generated tool belong here; a suppression
#: for an absent parameter protects nothing while reading as though it does, and
#: a test asserts every entry is real. The stronger protections are already one
#: layer up: ``allow_alternate_function`` and ``confirm_dut_attached``-style
#: operator gates are absent from the MCP tool signatures altogether, so the CLI
#: inherits their absence rather than having to strip them.
SUPPRESSED: dict[str, dict[str, str]] = {
    "pdu41002_set_outlet_state": {
        "verify": (
            "read-back verification is the only evidence a mains contactor "
            "moved — oltctrl acknowledges nothing — so it must not be "
            "disableable from a command line"
        ),
    },
    "adu218_set_relay_state": {
        "verify": (
            "same reason as the PDU: the read-back is the evidence, and a flag "
            "to skip it is a flag to stop checking"
        ),
    },
    "adu218_set_relay_port": {
        "verify": (
            "eight relays at once by mask — the case where a read-back matters "
            "most, because a partially-applied mask is indistinguishable from "
            "an applied one without it"
        ),
    },
    "adu218_reset_relays": {
        "verify": (
            "de-energising is the safe direction, which is exactly why the "
            "confirmation must survive: an operator opening every relay in a "
            "hurry is the one who most needs to know it actually happened"
        ),
    },
}


# ---------------------------------------------------------------------------
# The table. One entry per tool, grouped by module, in ``_TOOLS`` order so a
# diff against the driver reads straight down.
# ---------------------------------------------------------------------------

TOOL_TIERS: dict[str, str] = {
    # --- otii_arc: the SMU. Its output is the most easily-forgotten live thing
    # on the bench, so enable_output is Tier 1 even though the driver already
    # demands confirm_dut_attached.
    "info": READ,
    "state": READ,
    "versions": READ,
    "list_channels": READ,
    "set_voltage": TIER2,
    "set_current_limit": TIER2,
    "set_exp_voltage": TIER1,  # drives the expansion port rail
    "set_exp_5v": TIER1,  # ditto, fixed 5 V
    "set_range": TIER2,
    "set_4wire": TIER2,
    "set_current_limit_enabled": TIER2,
    "set_uart": TIER2,
    "set_gpo": TIER1,  # a GPO drives a line that may be wired to anything
    "set_power_regulation": TIER2,
    "enable_output": TIER1,
    "disable_output": TIER2,  # de-energising is the safe direction
    "live": READ,
    "take_snapshot": READ,  # matches a mutator prefix; samples, changes nothing
    "record": READ,
    "write_uart_tx": TIER1,  # arbitrary bytes out of the UART to the DUT
    "get_gpi": READ,
    "reconnect": TIER2,
    "disconnect": TIER2,
    # --- eastwood_qr10x: a programmable resistance standard. Nothing it does
    # sources power; the safety limit is the one setting that matters.
    "qr10x_open": TIER2,
    "qr10x_close": TIER2,
    "qr10x_info": READ,
    "qr10x_set_resistance": TIER2,
    "qr10x_get_setpoint": READ,
    "qr10x_actual_resistance": READ,
    "qr10x_set_safety_limit": TIER1,  # widening it removes a guard
    "qr10x_get_safety_limit": READ,
    "qr10x_get_temperature": READ,
    "qr10x_incr": TIER2,
    "qr10x_decr": TIER2,
    # --- rigol_dl3031a: an electronic load. set_input(True) sinks current,
    # which is the load-side equivalent of enabling an output.
    "dl3031a_open": TIER2,
    "dl3031a_close": TIER2,
    "dl3031a_info": READ,
    "dl3031a_reset": TIER2,
    "dl3031a_set_mode": TIER2,
    "dl3031a_get_mode": READ,
    "dl3031a_set_input": TIER1,
    "dl3031a_get_input": READ,
    "dl3031a_set_current": TIER2,
    "dl3031a_set_voltage": TIER2,
    "dl3031a_set_resistance": TIER2,
    "dl3031a_set_power": TIER2,
    "dl3031a_set_current_range": TIER2,
    "dl3031a_set_voltage_range": TIER2,
    "dl3031a_set_slew": TIER2,
    "dl3031a_measure": READ,
    "dl3031a_last_error": READ,
    "dl3031a_clear_status": TIER2,
    "dl3031a_raise_if_error": READ,
    "dl3031a_get_current": READ,
    "dl3031a_get_voltage": READ,
    "dl3031a_get_resistance": READ,
    "dl3031a_get_power": READ,
    "dl3031a_get_current_range": READ,
    "dl3031a_get_voltage_range": READ,
    "dl3031a_get_slew": READ,
    "dl3031a_get_trigger_source": READ,
    "dl3031a_measure_voltage": READ,
    "dl3031a_measure_current": READ,
    "dl3031a_measure_power": READ,
    "dl3031a_measure_resistance": READ,
    "dl3031a_fetch_voltage": READ,
    "dl3031a_fetch_current": READ,
    "dl3031a_fetch_power": READ,
    "dl3031a_fetch_resistance": READ,
    "dl3031a_fetch": READ,
    "dl3031a_set_function_mode": TIER2,
    "dl3031a_get_function_mode": READ,
    "dl3031a_trigger": TIER1,  # fires the configured transient/list into the DUT
    "dl3031a_set_trigger_source": TIER2,
    "dl3031a_program_list": TIER2,  # loads a list; trigger is what runs it
    "dl3031a_configure_transient_pulse": TIER2,
    "dl3031a_transient_enable": TIER1,  # begins sinking a pulsed load
    "dl3031a_configure_battery_test": TIER2,
    "dl3031a_battery_stats": READ,
    # --- rigol_dp2031: a three-channel supply. Largest surface in the repo,
    # and most of it is instrument housekeeping rather than output control.
    "dp2031_open": TIER2,
    "dp2031_close": TIER2,
    "dp2031_info": READ,
    "dp2031_reset": TIER2,
    "dp2031_clear_status": TIER2,
    "dp2031_last_error": READ,
    "dp2031_raise_if_error": READ,
    "dp2031_select_channel": TIER2,
    "dp2031_current_channel": READ,
    "dp2031_set_voltage": TIER2,
    "dp2031_get_voltage": READ,
    "dp2031_set_current": TIER2,
    "dp2031_get_current": READ,
    "dp2031_set_output": TIER1,
    "dp2031_get_output": READ,
    "dp2031_set_output_all": TIER1,  # all three channels at once
    "dp2031_output_regulation": READ,
    "dp2031_set_ovp_level": TIER2,
    "dp2031_get_ovp_level": READ,
    "dp2031_set_ovp_enabled": TIER1,  # disabling OVP removes a DUT's protection
    "dp2031_get_ovp_enabled": READ,
    "dp2031_set_ocp_level": TIER2,
    "dp2031_get_ocp_level": READ,
    "dp2031_set_ocp_enabled": TIER1,  # ditto for OCP
    "dp2031_get_ocp_enabled": READ,
    "dp2031_measure_voltage": READ,
    "dp2031_measure_current": READ,
    "dp2031_measure_power": READ,
    "dp2031_measure_all": READ,
    "dp2031_measure_all_channels": READ,
    "dp2031_clear_ovp": TIER1,  # clears a trip: the output can come back live
    "dp2031_clear_ocp": TIER1,
    "dp2031_ovp_tripped": READ,
    "dp2031_ocp_tripped": READ,
    "dp2031_set_ocp_delay_ms": TIER2,
    "dp2031_get_ocp_delay_ms": READ,
    "dp2031_event_status_register": READ,
    "dp2031_set_event_status_enable": TIER2,
    "dp2031_get_event_status_enable": READ,
    "dp2031_status_byte": READ,
    "dp2031_set_service_request_enable": TIER2,
    "dp2031_get_service_request_enable": READ,
    "dp2031_wait_op_complete": READ,
    "dp2031_self_test": TIER2,
    "dp2031_installed_options": READ,
    "dp2031_set_power_on_status_clear": TIER2,
    "dp2031_save_state": TIER2,
    "dp2031_recall_state": TIER1,  # a recalled state can carry output-on
    "dp2031_operation_event": READ,
    "dp2031_questionable_event": READ,
    "dp2031_channel_status_event": READ,
    "dp2031_health_check": READ,
    "dp2031_beep_once": TIER2,
    "dp2031_set_beeper": TIER2,
    "dp2031_get_beeper": READ,
    "dp2031_set_brightness": TIER2,
    "dp2031_get_brightness": READ,
    "dp2031_scpi_version": READ,
    "dp2031_set_keyboard_lock": TIER2,
    "dp2031_set_touchscreen_lock": TIER2,
    "dp2031_set_remote": TIER2,
    "dp2031_set_local": TIER2,
    "dp2031_set_screen_saver": TIER2,
    "dp2031_set_language": TIER2,
    "dp2031_set_power_on_mode": TIER1,  # decides whether the output comes up live
    "dp2031_set_channel_pair": TIER2,
    "dp2031_get_channel_pair": READ,
    "dp2031_set_tracking": TIER1,  # slaves one channel's output to another
    "dp2031_get_tracking": READ,
    "dp2031_set_track_mode": TIER2,
    "dp2031_get_track_mode": READ,
    "dp2031_set_output_sync": TIER1,  # one enable then drives several outputs
    "dp2031_get_output_sync": READ,
    "dp2031_set_remote_sense": TIER2,
    "dp2031_get_remote_sense": READ,
    "dp2031_set_sampling_mode": TIER2,
    "dp2031_get_sampling_mode": READ,
    "dp2031_set_voltage_step": TIER2,
    "dp2031_get_voltage_step": READ,
    "dp2031_set_current_step": TIER2,
    "dp2031_get_current_step": READ,
    "dp2031_step_voltage_up": TIER2,
    "dp2031_step_voltage_down": TIER2,
    "dp2031_step_current_up": TIER2,
    "dp2031_step_current_down": TIER2,
    "dp2031_apply": TIER2,  # sets V and I together; does not enable the output
    "dp2031_query_applied": READ,
    "dp2031_voltage_bounds": READ,
    "dp2031_current_bounds": READ,
    "dp2031_set_timer_enabled": TIER1,  # runs a stored output sequence unattended
    "dp2031_get_timer_enabled": READ,
    "dp2031_set_timer_channel": TIER2,
    "dp2031_get_timer_channel": READ,
    "dp2031_set_timer_cycles": TIER2,
    "dp2031_get_timer_cycles": READ,
    "dp2031_set_timer_end_state": TIER2,
    "dp2031_get_timer_end_state": READ,
    "dp2031_set_timer_run_mode": TIER2,
    "dp2031_get_timer_run_mode": READ,
    "dp2031_set_timer_trigger": TIER2,
    "dp2031_get_timer_trigger": READ,
    "dp2031_get_timer_group_params": READ,
    "dp2031_delete_timer_groups": TIER2,
    "dp2031_program_timer": TIER2,
    "dp2031_set_timer_template": TIER2,
    "dp2031_construct_timer_from_template": TIER2,
    "dp2031_set_analyzer_enabled": TIER2,
    "dp2031_get_analyzer_enabled": READ,
    "dp2031_set_analyzer_type": TIER2,
    "dp2031_get_analyzer_type": READ,
    "dp2031_set_analyzer_common_objects": TIER2,
    "dp2031_set_analyzer_save": TIER2,
    "dp2031_set_analyzer_save_path": TIER2,
    "dp2031_set_trigger_in_enabled": TIER1,  # an external edge can then drive output
    "dp2031_get_trigger_in_enabled": READ,
    "dp2031_set_trigger_in_type": TIER2,
    "dp2031_get_trigger_in_type": READ,
    "dp2031_set_trigger_in_source": TIER2,
    "dp2031_get_trigger_in_source": READ,
    "dp2031_set_trigger_in_response": TIER2,
    "dp2031_trigger_in_immediate": TIER1,  # fires that response now
    "dp2031_set_trigger_out_enabled": TIER2,
    "dp2031_set_trigger_out_source": TIER2,
    "dp2031_set_trigger_out_polarity": TIER2,
    "dp2031_list_files": READ,
    "dp2031_change_directory": TIER2,
    "dp2031_current_directory": READ,
    "dp2031_delete_file": TIER1,  # destroys instrument-side data irreversibly
    "dp2031_store_file": TIER2,
    "dp2031_load_file": TIER1,  # a loaded state can carry output-on
    "dp2031_external_disks": READ,
    "dp2031_file_exists": READ,
    "dp2031_install_license": TIER1,  # writes a licence key into the instrument
    "dp2031_save_screenshot": TIER2,
    # --- siglent_sdm4065a: a DMM. Measures rather than drives, so almost
    # everything is READ — except the two raw-SCPI escapes at the end, which
    # are exactly the tools dispatch.is_mutator() calls reads.
    "sdm4065a_open": TIER2,
    "sdm4065a_close": TIER2,
    "sdm4065a_info": READ,
    "sdm4065a_reset": TIER2,
    "sdm4065a_clear_status": TIER2,
    "sdm4065a_drain_errors": TIER2,
    "sdm4065a_self_test": TIER2,
    "sdm4065a_last_error": READ,
    "sdm4065a_command_error": READ,
    "sdm4065a_standard_event_status": READ,
    "sdm4065a_raise_if_error": READ,
    "sdm4065a_set_function": TIER2,
    "sdm4065a_get_function": READ,
    "sdm4065a_measure_dc_voltage": READ,
    "sdm4065a_measure_ac_voltage": READ,
    "sdm4065a_measure_dc_current": READ,
    "sdm4065a_measure_ac_current": READ,
    "sdm4065a_measure_resistance": READ,
    "sdm4065a_measure_resistance_4wire": READ,
    "sdm4065a_measure_capacitance": READ,
    "sdm4065a_measure_frequency": READ,
    "sdm4065a_measure_period": READ,
    "sdm4065a_measure_continuity": READ,
    "sdm4065a_measure_diode": READ,  # sources a small test current, into a diode
    "sdm4065a_measure_temperature": READ,
    "sdm4065a_configure_resistance": TIER2,
    "sdm4065a_configure_dc_voltage": TIER2,
    "sdm4065a_get_configuration": READ,
    "sdm4065a_set_sample_count": TIER2,
    "sdm4065a_get_sample_count": READ,
    "sdm4065a_read": READ,
    "sdm4065a_read_nulled": READ,
    "sdm4065a_initiate": TIER2,
    "sdm4065a_fetch": READ,
    "sdm4065a_abort": TIER2,
    "sdm4065a_clear_device_buffers": TIER2,
    "sdm4065a_set_nplc": TIER2,
    "sdm4065a_get_nplc": READ,
    "sdm4065a_set_range": TIER2,
    "sdm4065a_get_range": READ,
    "sdm4065a_get_autorange": READ,
    "sdm4065a_set_autorange": TIER2,
    "sdm4065a_set_autozero": TIER2,
    "sdm4065a_get_autozero": READ,
    "sdm4065a_null_now": TIER2,
    "sdm4065a_set_null": TIER2,
    "sdm4065a_get_null": READ,
    "sdm4065a_set_null_value": TIER2,
    "sdm4065a_set_null_auto": TIER2,
    "sdm4065a_set_temperature_unit": TIER2,
    "sdm4065a_get_temperature_unit": READ,
    "sdm4065a_reading_timeout_ms": READ,
    "sdm4065a_write": TIER1,  # arbitrary SCPI. is_mutator() says False.
    "sdm4065a_query": TIER1,  # a query string can carry a setting command
    # --- cyberpower_pdu41002: switches mains. The only device where the
    # write surface is smaller than the read surface, and the only one where
    # a write can de-power the rest of the bench.
    "pdu41002_open": TIER2,
    "pdu41002_close": TIER2,
    "pdu41002_info": READ,
    "pdu41002_status": READ,
    "pdu41002_measure_load": READ,
    "pdu41002_measure_voltage": READ,
    "pdu41002_measure_frequency": READ,
    "pdu41002_outlet_states": READ,
    "pdu41002_outlet_state": READ,
    "pdu41002_outlet_config": READ,
    "pdu41002_allowed_outlets": READ,
    "pdu41002_set_outlet_state": TIER1_ENV,
    "pdu41002_reset_outlet": TIER1_ENV,  # power-cycles: an off *and* an on
    "pdu41002_clear_outlet_command": TIER2,  # cancels a pending delayed action
    "pdu41002_transport": READ,
    # --- silabs_cp2112: DUT reset lines. Asserting one holds a DUT in reset,
    # which is disruptive but not energising.
    "cp2112_open": TIER2,
    "cp2112_close": TIER2,
    "cp2112_info": READ,
    "cp2112_line_states": READ,
    "cp2112_line_state": READ,
    "cp2112_allowed_lines": READ,
    "cp2112_set_line_mode": TIER2,
    "cp2112_set_line_asserted": TIER1,  # drives a line into a DUT
    "cp2112_trigger_reset_pulse": TIER1,
    "cp2112_reset_lines": TIER2,  # releases every line: the safe direction
    # --- ontrak_adu218: eight relays and a hardware watchdog. Every write is
    # exposed (the operator's requirement), and the watchdog is the one
    # operation whose consequence outlives the command.
    "adu218_open": TIER2,
    "adu218_close": TIER2,
    "adu218_info": READ,
    "adu218_relay_states": READ,
    "adu218_relay_state": READ,
    "adu218_set_relay_state": TIER1,  # closes a relay contact
    "adu218_set_relay_port": TIER1,  # eight at once, by mask
    "adu218_reset_relays": TIER2,  # opens every relay: the safe direction
    "adu218_allowed_relays": READ,
    "adu218_input_states": READ,
    "adu218_input_state": READ,
    "adu218_input_port_mask": READ,
    "adu218_counters": READ,
    "adu218_counter": READ,
    "adu218_clear_counter": TIER2,  # loses evidence, drives nothing
    "adu218_debounce": READ,
    "adu218_set_debounce": TIER2,
    "adu218_watchdog": READ,
    "adu218_set_watchdog": TIER1_ENV,  # arms; WDn both sets and arms
    # --- framework tools: analytics on saved files, plus the two that attach
    # to a live SMU and drive it for hours.
    "plot_recording": READ,
    "recording_summary": READ,
    "export_recording": READ,
    "battery_profile_summary": READ,
    "battery_life_estimate": READ,
    "battery_life_estimate_from_profile": READ,
    "battery_life_from_recording": READ,
    "battery_profile_lookup": READ,
    "battery_profiler_estimate_duration": READ,
    "battery_profiler_run": TIER1,  # discharges a real cell, for hours
    "battery_emulator_start": TIER1,  # drives voltage onto the terminals
    "battery_emulator_state": READ,
    "battery_emulator_stop": TIER2,
}


def tier_for(tool_name: str) -> str:
    """Return the tier for ``tool_name``.

    Raises :py:class:`KeyError` rather than defaulting. A default of ``READ``
    would let a new tool ship ungated; a default of ``TIER1`` would be safe but
    silent, and the operator would learn about the omission from a confirmation
    prompt on a read. Failing loudly puts it in front of whoever added the tool.
    """
    try:
        return TOOL_TIERS[tool_name]
    except KeyError:
        raise KeyError(
            f"{tool_name!r} has no entry in benchctrl.cli_tiers.TOOL_TIERS. "
            f"A generated CLI subcommand must be classified explicitly — add "
            f"it with the reasoning, do not derive it from the name."
        ) from None


def env_gate_for(tool_name: str) -> str | None:
    """The environment variable gating ``tool_name``, or None."""
    return ENV_GATES.get(tool_name)


def suppressed_params(tool_name: str) -> dict[str, str]:
    """Parameters of ``tool_name`` the CLI must not expose, name -> reason."""
    return SUPPRESSED.get(tool_name, {})

"""Cross-driver / framework MCP tools — owned by no single driver.

These are the tools that do not belong to an instrument:

  - ``plot_recording`` / ``recording_summary`` / ``export_recording`` work on
    saved ``.opensmu`` files and need no live device at all.
  - ``battery_*`` are analytics plus the emulator/profiler, which drive any
    SMU implementing the :py:class:`SourceMeasurementUnit` Protocol. Today
    that is only the Arc, but the *tools* are framework-level — they know
    nothing about wire formats.

Why this is a module and not a block in :py:mod:`benchctrl.mcp`
--------------------------------------------------------------
Every driver exposes its MCP surface as a ``_TOOLS`` tuple plus a
:py:func:`register_mcp_tools`, which is what lets a consumer enumerate the
surface *without* constructing a :py:class:`FastMCP` server — the generated
CLI does exactly that, and importing ``benchctrl.mcp`` costs ~0.64 s and hard
-depends on the optional ``[mcp]`` extra. These 13 tools were the only ones
declared with a bare ``@mcp.tool()`` against the module-level server, so they
were invisible to that route. Giving them the same shape as the eight drivers
makes the surface uniform: ``_TOOLS`` and ``mcp.list_tools()`` agree.

The functions themselves are unchanged, decorators aside.
"""

from __future__ import annotations

from pathlib import Path
from typing import Optional

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

# The SMU singleton and the recording I/O helpers live with the Arc's tools,
# because the Arc is the only Protocol implementer and its ``*_open`` /
# ``disconnect`` tools own the lifecycle. Importing them here rather than
# duplicating keeps one singleton: a framework tool and an Arc tool called in
# the same session must see the same device.
from benchctrl.drivers.otii_arc.mcp_tools import (
    _get_smu,
    _render_plot_png,
    _save_recording_by_extension,
    _statistics_dict,
)
from benchctrl.recording import Recording


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


def battery_emulator_state() -> dict:
    """Snapshot the running emulator's state.

    Returns SoC, used capacity (mAh), OCV (V), ESR (Ω), output voltage,
    measured current, runtime, iteration count, running flag, stop
    reason if stopped.
    """
    if _emulator is None:
        return {"error": "no emulator running", "guidance": "Call battery_emulator_start() first."}
    return _emulator.state().__dict__


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


_TOOLS = (
    plot_recording,
    recording_summary,
    export_recording,
    battery_profile_summary,
    battery_life_estimate,
    battery_life_estimate_from_profile,
    battery_life_from_recording,
    battery_profile_lookup,
    battery_profiler_estimate_duration,
    battery_profiler_run,
    battery_emulator_start,
    battery_emulator_state,
    battery_emulator_stop,
)


def register_mcp_tools(mcp) -> None:
    """Register every framework MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

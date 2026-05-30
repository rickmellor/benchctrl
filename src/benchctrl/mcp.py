"""MCP server exposing the Arc Pro SMU as tools any MCP client can call.

Designed for use with Claude Code, Claude Desktop, or any other MCP-aware
client. Run as ``benchctrl-mcp`` or ``python -m benchctrl.mcp``.

Connection model
----------------
The server opens the SMU on the first tool call and holds the connection
for the lifetime of the process. Only one process can hold the device at
a time — closing the server (or calling ``disconnect()``) releases it so
another client can take over.

Safety
------
Only one tool drives voltage onto the output terminals: ``enable_output``.
It refuses unless the caller passes ``confirm_dut_attached=True`` AND a
``current_limit`` has been set. Every other tool is non-destructive on a
setup with nothing connected to the output.

Concurrency
-----------
The server assumes **single-client serialization**: one MCP client
sends tool calls sequentially. The ``_qr10x_lock`` / ``_dl3031a_lock``
mutexes only guard open/close transitions against concurrent
``*_open`` and ``*_close`` calls; per-tool calls do not take the lock
because the singleton is only mutated by open/close. Running two
concurrent clients against the same server is unsupported — a tool
call against a freshly-closed instrument may dereference None and
crash inside the underlying transport. If you need multi-client
access, run separate server processes per client and have them
hold separate sessions to the device.
"""

from __future__ import annotations

import logging
import struct
import threading
import time
from pathlib import Path
from typing import Optional

log = logging.getLogger("benchctrl.mcp")

from mcp.server.fastmcp import FastMCP

from benchctrl import SMU, Channel, Recording
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
from benchctrl.bench import QR10x
# RigolDL3031A is imported lazily inside the tool functions so the MCP
# server doesn't hard-require pyvisa to start.
from benchctrl.channels import WIRE_ID_TO_CHANNEL
from benchctrl.exceptions import BenchError
from benchctrl.protocol import iter_frames, iter_samples
from benchctrl.samples import compute_statistics

mcp = FastMCP("benchctrl")

_smu: Optional[SMU] = None
_lock = threading.RLock()


# ---------------------------------------------------------------------------
# Connection lifecycle
# ---------------------------------------------------------------------------


def _get_smu() -> SMU:
    """Return the singleton SMU, opening it if not already connected."""
    global _smu
    with _lock:
        if _smu is None or not _smu.is_connected:
            _smu = SMU.open()
        return _smu


def _close_smu() -> None:
    global _smu
    with _lock:
        if _smu is not None:
            try:
                _smu.close()
            except Exception:
                pass
            _smu = None


# ---------------------------------------------------------------------------
# State snapshot helper
# ---------------------------------------------------------------------------


def _smu_state(smu: SMU) -> dict:
    """Compact host-cached state snapshot for inclusion in tool responses."""
    return {
        "is_connected": smu.is_connected,
        "port": smu.info.port if smu.info else None,
        "voltage_V": smu.voltage,
        "main_current_A": smu.main_current,
        "current_limit_A": smu.current_limit,
        "exp_voltage_V": smu.exp_voltage,
        "exp_5v_enabled": smu.exp_5v_enabled,
        "adc_resistor_ohm": smu.adc_resistor,
        "output_enabled": smu.output_enabled,
        "range": smu.range,
        "four_wire_enabled": smu.four_wire_enabled,
        "current_limit_enabled": smu.current_limit_enabled,
        "uart_enabled": smu.uart_enabled,
        "uart_baudrate": smu.uart_baudrate,
        "supply_mode": smu.supply_mode,
        "power_regulation": smu.power_regulation,
        "gpo": dict(smu.gpo),
        "enabled_channels": sorted(c.code for c in smu.enabled_channels),
        "legacy_sink_enabled": smu.legacy_sink_enabled,
    }


# ---------------------------------------------------------------------------
# Tools — information / inspection
# ---------------------------------------------------------------------------


@mcp.tool()
def info() -> dict:
    """Get device identity: port, name, firmware/hardware version, serial id.

    Connects to the device if not already connected. Returns ``None`` for
    fields that cannot be read.
    """
    smu = _get_smu()
    out: dict = {
        "is_connected": smu.is_connected,
        "port": smu.info.port if smu.info else None,
        "name": None,
        "hw_version": None,
        "fw_version": None,
        "device_id": None,
    }
    try:
        out["name"] = smu.get_device_name()
        out["hw_version"] = smu.get_hw_version()
        out["fw_version"] = smu.get_fw_version()
        out["device_id"] = smu.get_device_id()
    except BenchError as exc:
        out["read_error"] = str(exc)
    return out


@mcp.tool()
def state() -> dict:
    """Return every cached setpoint and the current connection state.

    All values reflect what was last written via this server (or whatever
    other client held the device before this one). The device itself has
    no general-purpose query-current-value command on the wire; cached
    state is the source of truth. See ``versions()`` for read-from-device
    metadata.
    """
    return _smu_state(_get_smu())


@mcp.tool()
def versions() -> dict:
    """Query the device's hardware/firmware versions and serial id."""
    smu = _get_smu()
    return {
        "name": smu.get_device_name(),
        "hw_version": smu.get_hw_version(),
        "fw_version": smu.get_fw_version(),
        "device_id": smu.get_device_id(),
    }


@mcp.tool()
def list_channels() -> dict:
    """List every measurement channel the device supports with metadata.

    Returns the static channel inventory (code, wire id, label, unit,
    native sample rate, whether toggleable). Use ``record`` or ``live``
    to actually capture values from these channels.
    """
    return {
        "channels": [
            {
                "code": c.code,
                "wire_id": c.wire_id,
                "label": c.label,
                "unit": c.unit,
                "sample_rate_hz": c.sample_rate,
                "subtype": c.subtype,
                "toggleable": c.toggleable,
            }
            for c in Channel
        ]
    }


# ---------------------------------------------------------------------------
# Tools — setpoints (do not enable output)
# ---------------------------------------------------------------------------


@mcp.tool()
def set_voltage(volts: float) -> dict:
    """Set the main output voltage in volts (does NOT enable output).

    Range: 0.0 to 5.5 V. The device caps around 3.5 V in low range —
    call ``set_range('high')`` first to drive above ~3.5 V.
    """
    smu = _get_smu()
    smu.set_voltage(volts)
    return {"set_voltage_V": volts, "state": _smu_state(smu)}


@mcp.tool()
def set_current_limit(amps: float) -> dict:
    """Set the over-current / max current limit in amps.

    Range: 0.001 to 5.0 A. This is required before ``enable_output`` will
    accept a confirmation — it bounds DUT damage in fault conditions.
    """
    smu = _get_smu()
    smu.set_current_limit(amps)
    return {"set_current_limit_A": amps, "state": _smu_state(smu)}


@mcp.tool()
def set_exp_voltage(volts: float) -> dict:
    """Set the expansion-port digital voltage in volts (1.2 to 5.0)."""
    smu = _get_smu()
    smu.set_exp_voltage(volts)
    return {"set_exp_voltage_V": volts, "state": _smu_state(smu)}


@mcp.tool()
def set_exp_5v(enabled: bool) -> dict:
    """Enable / disable the EXP-port 5V output."""
    smu = _get_smu()
    smu.set_exp_5v(enabled)
    return {"exp_5v_enabled": enabled, "state": _smu_state(smu)}


@mcp.tool()
def set_range(range_: str) -> dict:
    """Set the measurement range: ``'low'`` or ``'high'``.

    ``'low'`` covers 0 to ~3.5 V with the finest current resolution.
    ``'high'`` is needed to drive above ~3.5 V.
    """
    smu = _get_smu()
    smu.set_range(range_)
    return {"set_range": range_, "state": _smu_state(smu)}


@mcp.tool()
def set_4wire(enabled: bool) -> dict:
    """Enable / disable 4-wire (sense) measurement mode."""
    smu = _get_smu()
    smu.set_four_wire(enabled)
    return {"four_wire_enabled": enabled, "state": _smu_state(smu)}


@mcp.tool()
def set_current_limit_enabled(enabled: bool) -> dict:
    """Enable source-current-limit (CC) mode (True) or cut-off mode (False)."""
    smu = _get_smu()
    smu.set_current_limit_enabled(enabled)
    return {"current_limit_enabled": enabled, "state": _smu_state(smu)}


@mcp.tool()
def set_uart(enabled: bool, baudrate: Optional[int] = None) -> dict:
    """Enable / disable the UART decoder. Optionally set baud first."""
    smu = _get_smu()
    smu.set_uart(enabled, baudrate=baudrate)
    return {
        "uart_enabled": enabled,
        "uart_baudrate": smu.uart_baudrate,
        "state": _smu_state(smu),
    }


@mcp.tool()
def set_gpo(pin: int, state_on: bool) -> dict:
    """Set GPO pin (1, 2, or 3) state. Pin 3 is the TX pin used as a GPO."""
    smu = _get_smu()
    smu.set_gpo(pin, state_on)
    return {"gpo": dict(smu.gpo), "state": _smu_state(smu)}


@mcp.tool()
def set_power_regulation(mode: str) -> dict:
    """Set power regulation mode: ``'voltage'``, ``'current'``, ``'inline'``, or ``'off'``."""
    smu = _get_smu()
    smu.set_power_regulation(mode)
    return {"power_regulation": mode, "state": _smu_state(smu)}


# ---------------------------------------------------------------------------
# Tools — output enable (the one that drives voltage onto the terminals)
# ---------------------------------------------------------------------------


@mcp.tool()
def enable_output(confirm_dut_attached: bool = False) -> dict:
    """Enable the SMU's main output (drives voltage onto the terminals).

    SAFETY-CRITICAL: this turns on the output. The configured voltage
    appears on the output terminals through the configured current
    limit. If your DUT can't tolerate the voltage, you can damage it.

    To call this, you must:
      - have set a current_limit (bounds fault current)
      - have set a voltage (so we know what's about to be driven)
      - pass ``confirm_dut_attached=True`` after verifying both are safe

    All three guards are required. The tool refuses with a structured
    error response otherwise. Match that error's ``guidance`` field
    when relaying back to the user.
    """
    smu = _get_smu()
    if smu.current_limit is None:
        return {
            "error": "REFUSED: no current_limit set",
            "guidance": (
                "Call set_current_limit(amps) first. The current limit bounds "
                "DUT damage if your circuit shorts."
            ),
        }
    if smu.voltage is None:
        return {
            "error": "REFUSED: no voltage set",
            "guidance": (
                "Call set_voltage(volts) first so we know what's about to be driven."
            ),
        }
    if not confirm_dut_attached:
        return {
            "error": "REFUSED: confirm_dut_attached=False",
            "guidance": (
                f"Output is about to be driven at {smu.voltage} V through a "
                f"{smu.current_limit} A current limit. Verify the DUT can "
                "tolerate this voltage and the current limit is safe, then "
                "retry with confirm_dut_attached=True."
            ),
            "voltage_V": smu.voltage,
            "current_limit_A": smu.current_limit,
        }
    smu.set_output(True)
    return {
        "output_enabled": True,
        "voltage_V": smu.voltage,
        "current_limit_A": smu.current_limit,
    }


@mcp.tool()
def disable_output() -> dict:
    """Disable the main output. Always safe to call."""
    smu = _get_smu()
    smu.set_output(False)
    return {"output_enabled": False}


# ---------------------------------------------------------------------------
# Tools — measurement / capture
# ---------------------------------------------------------------------------


@mcp.tool()
def live(channel: str = "mv", timeout_s: float = 1.5) -> dict:
    """Read the next sample value for ``channel``.

    Returns the latest single value from the device's live stream.
    Cannot be used while a recording is active.

    Example: ``live("mc")`` returns the next main-current sample.
    """
    smu = _get_smu()
    ch = Channel.coerce(channel)
    value = smu.read_value(ch, timeout=timeout_s)
    return {
        "channel": ch.code,
        "value": value,
        "unit": ch.unit,
        "label": ch.label,
    }


@mcp.tool()
def take_snapshot(duration_s: float = 0.5) -> dict:
    """Drain a brief window of inbound samples and return latest per channel.

    Useful for "what's the instantaneous reading on every channel right
    now?". Returns one entry per channel that appeared in the window.
    """
    smu = _get_smu()
    raw = smu.read_raw(duration_s)
    latest: dict[str, dict] = {}
    for fr in iter_frames(raw):
        for rec in iter_samples(fr.payload):
            ch = WIRE_ID_TO_CHANNEL.get(rec.channel_id)
            if ch is None:
                continue
            latest[ch.code] = {
                "value": rec.value,
                "unit": ch.unit,
                "label": ch.label,
            }
    return {"duration_s": duration_s, "channels": latest}


def _save_recording_by_extension(rec: Recording, path: Path) -> Path:
    """Dispatch save() based on file extension.

    Accepts ``.csv``, ``.json``, ``.opensmu``, ``.parquet``. Anything else
    is normalised to ``.opensmu``. Parquet requires ``benchctrl[parquet]``
    installed; a clear ``ImportError`` propagates otherwise.
    """
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return rec.save_csv(path)
    if suffix == ".json":
        return rec.save_json(path)
    if suffix == ".parquet":
        return rec.save_parquet(path)
    if suffix != ".opensmu":
        path = path.with_suffix(".opensmu")
    return rec.save(path)


def _statistics_dict(rec: Recording) -> dict:
    """Per-channel statistics in JSON-friendly form."""
    out: dict = {}
    for ch_code in sorted({c.code for c in rec.channels}):
        ch = Channel.from_code(ch_code)
        stats = rec.statistics(ch)
        out[ch_code] = {
            "samples": stats.sample_count,
            "min": stats.min,
            "max": stats.max,
            "average": stats.average,
            "rms": stats.rms,
            "duration_s": stats.duration,
            "energy_J": stats.energy,
            "charge_C": stats.charge,
            "unit": ch.unit,
            "label": ch.label,
            "sample_rate_hz": ch.sample_rate,
        }
    return out


def _render_plot_png(
    rec: Recording,
    output_png: str | Path,
    *,
    channels: Optional[list[str]] = None,
    title: Optional[str] = None,
) -> Path:
    """Render a matplotlib quick-look PNG. Closes the figure after saving."""
    import matplotlib

    matplotlib.use("Agg")  # headless backend — no display required
    import matplotlib.pyplot as plt

    ch_objs = None if channels is None else [Channel.coerce(c) for c in channels]
    fig = rec.plot(channels=ch_objs, show=False, title=title)
    out = Path(output_png)
    fig.savefig(out, dpi=150, bbox_inches="tight")
    plt.close(fig)
    return out


@mcp.tool()
def record(
    seconds: float,
    channels: Optional[list[str]] = None,
    save_path: Optional[str] = None,
    name: str = "recording",
    plot_png: Optional[str] = None,
) -> dict:
    """Run a synchronous recording for ``seconds`` seconds; return summary stats.

    Args:
        seconds: recording duration.
        channels: list of channel codes (default ``["mc", "mv"]``).
            Enabling ``mc`` auto-includes ``mp``; enabling ``ac`` auto-includes ``ap``.
        save_path: optional file path. Extension auto-detected:
            ``.csv`` / ``.json`` / ``.opensmu`` (native binary) /
            ``.parquet`` (requires ``benchctrl[parquet]``). Other extensions
            are saved as ``.opensmu``.
        name: recording name (stored in metadata).
        plot_png: optional path; if given, also renders a matplotlib
            quick-look PNG (one subplot per channel). Requires
            ``benchctrl[plot]`` installed.

    Returns per-channel statistics (sample_count, min, max, average, rms,
    charge for current channels, energy for power channels), the file
    path if saved, and the PNG path if plotted.
    """
    smu = _get_smu()
    ch_codes = channels or ["mc", "mv"]
    smu.disable_all_channels()
    smu.enable_channels(*ch_codes)
    with smu.record(name=name) as rec:
        time.sleep(seconds)

    out: dict = {
        "name": rec.name,
        "duration_s": seconds,
        "channels": _statistics_dict(rec),
    }

    if save_path:
        out["saved_to"] = str(_save_recording_by_extension(rec, Path(save_path)))

    if plot_png:
        out["plotted_to"] = str(_render_plot_png(rec, plot_png))

    return out


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


# ---------------------------------------------------------------------------
# Tools — battery profiles (v0.5.0 — phase 1 of battery feature suite)
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
# Battery emulator (v0.8.0)
# ---------------------------------------------------------------------------
#
# The emulator is stateful — it runs in a background control loop. MCP
# tools work on a singleton instance attached to the SMU.

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
# QR10x programmable resistance (benchctrl.bench)
# ---------------------------------------------------------------------------

_qr10x: Optional[QR10x] = None
_qr10x_lock = threading.RLock()


def _get_qr10x() -> QR10x:
    from benchctrl.bench.qr10x import QR10xConnectionError
    if _qr10x is None or not _qr10x.is_open:
        raise QR10xConnectionError(
            "QR10x not open — call qr10x_open(port=...) first."
        )
    return _qr10x


@mcp.tool()
def qr10x_open(port: str = "COM7", baudrate: int = 115200) -> dict:
    """Open a connection to the Eastwood QR10x programmable resistor.

    The QR10x exposes a USB-Serial interface (CH340 chip) at 115200 8N1
    and accepts AT commands. Returns its identity info on success.

    Only one connection at a time. Call ``qr10x_close()`` first if
    already open on a different port.
    """
    global _qr10x
    with _qr10x_lock:
        if _qr10x is not None and _qr10x.is_open:
            return {
                "error": "QR10x already open",
                "guidance": "Call qr10x_close() before reopening on a different port.",
                "current_port": _qr10x.port,
            }
        _qr10x = QR10x.open(port, baudrate=baudrate)
    return {"port": _qr10x.port, "info": _qr10x.info().to_dict()}


@mcp.tool()
def qr10x_close() -> dict:
    """Close the QR10x connection so the port can be reopened."""
    global _qr10x
    with _qr10x_lock:
        if _qr10x is None:
            return {"closed": False, "note": "no QR10x was open"}
        _qr10x.close()
        _qr10x = None
    return {"closed": True}


@mcp.tool()
def qr10x_info() -> dict:
    """Get the QR10x's identity and static specs."""
    return _get_qr10x().info().to_dict()


@mcp.tool()
def qr10x_set_resistance(ohms: float) -> dict:
    """Set the QR10x's output resistance (Ω).

    Will be clamped to ``RLIMIT`` if a safety limit is configured. The
    device will pick the achievable value closest to the setpoint; read
    back via ``qr10x_actual_resistance()`` to see the actual achieved
    resistance (PV).
    """
    qr = _get_qr10x()
    out = qr.set_resistance(ohms)
    out["actual_resistance_ohm"] = qr.actual_resistance()
    return out


@mcp.tool()
def qr10x_get_setpoint() -> dict:
    """Get the current resistance setpoint (Ω) — what was last commanded."""
    return {"setpoint_ohm": _get_qr10x().get_setpoint()}


@mcp.tool()
def qr10x_actual_resistance() -> dict:
    """Get the actual achieved resistance (PV, Ω) — what the device produced."""
    return {"actual_resistance_ohm": _get_qr10x().actual_resistance()}


@mcp.tool()
def qr10x_set_safety_limit(ohms: float) -> dict:
    """Set the QR10x's minimum-resistance safety limit (Ω).

    Any subsequent set_resistance below this value is clamped
    device-side. Set this to keep power within the device's rating for
    your source voltage. At 3.2 V (e.g. AA pair) the rated 1 W gives a
    minimum safe R of ~12 Ω; at 5 V it's ~25 Ω.
    """
    return _get_qr10x().set_safety_limit(ohms)


@mcp.tool()
def qr10x_get_safety_limit() -> dict:
    """Get the QR10x's minimum-resistance safety limit (Ω)."""
    return {"safety_limit_ohm": _get_qr10x().get_safety_limit()}


@mcp.tool()
def qr10x_get_temperature() -> dict:
    """Get the QR10x's internal temperature sensor (°C)."""
    return {"temperature_C": _get_qr10x().get_temperature()}


@mcp.tool()
def qr10x_incr(delta_ohm: float) -> dict:
    """Step the QR10x setpoint up by ``delta_ohm`` Ω."""
    qr = _get_qr10x()
    out = qr.incr(delta_ohm)
    out["actual_resistance_ohm"] = qr.actual_resistance()
    return out


@mcp.tool()
def qr10x_decr(delta_ohm: float) -> dict:
    """Step the QR10x setpoint down by ``delta_ohm`` Ω."""
    qr = _get_qr10x()
    out = qr.decr(delta_ohm)
    out["actual_resistance_ohm"] = qr.actual_resistance()
    return out


# ---------------------------------------------------------------------------
# Rigol DL3031A programmable DC electronic load (benchctrl.bench)
# ---------------------------------------------------------------------------

_dl3031a = None  # actually Optional[RigolDL3031A] but kept Any to avoid eager import
_dl3031a_lock = threading.RLock()


def _get_dl3031a():
    from benchctrl.bench.rigol_dl3031a import RigolDLConnectionError
    if _dl3031a is None:
        raise RigolDLConnectionError(
            "DL3031A not open — call dl3031a_open() first."
        )
    return _dl3031a


@mcp.tool()
def dl3031a_open(resource: Optional[str] = None) -> dict:
    """Open a VISA session to a Rigol DL3031A programmable electronic load.

    If ``resource`` is None, auto-discovers by scanning for the Rigol
    DL3000 USB VID/PID (0x1AB1 / 0x0E11). Pass an explicit VISA
    resource string (USB or LXI) to target a specific device.

    Returns the device's *IDN? identity on success.
    """
    global _dl3031a
    from benchctrl.bench import RigolDL3031A
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


@mcp.tool()
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


@mcp.tool()
def dl3031a_info() -> dict:
    """Identity and resource string from ``*IDN?``."""
    info = _get_dl3031a().info()
    return {
        "manufacturer": info.manufacturer, "model": info.model,
        "serial": info.serial, "firmware": info.firmware,
        "resource": info.resource,
    }


@mcp.tool()
def dl3031a_reset() -> dict:
    """``*RST`` — restore factory defaults (mode CC, input OFF, all zero)."""
    _get_dl3031a().reset()
    return {"reset": True}


@mcp.tool()
def dl3031a_set_mode(mode: str) -> dict:
    """Set the load's operation mode: ``"CC"`` / ``"CV"`` / ``"CR"`` / ``"CP"``."""
    dl = _get_dl3031a()
    dl.set_mode(mode)
    return {"mode": dl.get_mode()}


@mcp.tool()
def dl3031a_get_mode() -> dict:
    return {"mode": _get_dl3031a().get_mode()}


@mcp.tool()
def dl3031a_set_input(on: bool) -> dict:
    """Enable or disable the load input.

    SAFETY-CRITICAL: enabling the input lets the load start sinking
    current from the connected DUT. Confirm the setpoint, mode, and
    DUT compatibility before passing ``on=True``.
    """
    dl = _get_dl3031a()
    dl.set_input(on)
    return {"input_on": dl.get_input()}


@mcp.tool()
def dl3031a_get_input() -> dict:
    return {"input_on": _get_dl3031a().get_input()}


@mcp.tool()
def dl3031a_set_current(amps: float) -> dict:
    """CC-mode setpoint (A). Honored when ``:FUNC == CC``."""
    dl = _get_dl3031a()
    dl.set_current(amps)
    return {"current_setpoint_A": dl.get_current()}


@mcp.tool()
def dl3031a_set_voltage(volts: float) -> dict:
    """CV-mode setpoint (V). Honored when ``:FUNC == CV``."""
    dl = _get_dl3031a()
    dl.set_voltage(volts)
    return {"voltage_setpoint_V": dl.get_voltage()}


@mcp.tool()
def dl3031a_set_resistance(ohms: float) -> dict:
    """CR-mode setpoint (Ω). Honored when ``:FUNC == CR``."""
    dl = _get_dl3031a()
    dl.set_resistance(ohms)
    return {"resistance_setpoint_ohm": dl.get_resistance()}


@mcp.tool()
def dl3031a_set_power(watts: float) -> dict:
    """CP-mode setpoint (W). Honored when ``:FUNC == CP``."""
    dl = _get_dl3031a()
    dl.set_power(watts)
    return {"power_setpoint_W": dl.get_power()}


@mcp.tool()
def dl3031a_set_current_range(amps: float) -> dict:
    """Set the CC / transient current range (DL3031A: ~6 A low / 60 A high)."""
    dl = _get_dl3031a()
    dl.set_current_range(amps)
    return {"current_range_A": dl.get_current_range()}


@mcp.tool()
def dl3031a_set_voltage_range(volts: float) -> dict:
    """Set the CV / measurement voltage range (DL3031A: ~36 V low / 150 V high)."""
    dl = _get_dl3031a()
    dl.set_voltage_range(volts)
    return {"voltage_range_V": dl.get_voltage_range()}


@mcp.tool()
def dl3031a_set_slew(amps_per_us: float) -> dict:
    """Symmetric current slew rate (A/µs) for CC and transient mode."""
    dl = _get_dl3031a()
    dl.set_slew(amps_per_us)
    return {"slew_A_per_us": dl.get_slew()}


@mcp.tool()
def dl3031a_measure() -> dict:
    """Measure V / I / P / R at the load terminals (one shot each)."""
    return _get_dl3031a().measure_all()


@mcp.tool()
def dl3031a_last_error() -> dict:
    """Read one entry from the SCPI error queue. Returns ``{"code": 0}`` if clean."""
    err = _get_dl3031a().last_error()
    if err is None:
        return {"code": 0, "message": "No error"}
    return {"code": err[0], "message": err[1]}


@mcp.tool()
def dl3031a_clear_status() -> dict:
    """``*CLS`` — clear status registers and the error queue."""
    _get_dl3031a().clear_status()
    return {"cleared": True}


@mcp.tool()
def dl3031a_raise_if_error() -> dict:
    """Drain the error queue and raise on the first non-zero code.
    Returns ``{"ok": true}`` if no errors were queued; otherwise the
    tool surface raises (which the MCP layer will report as an
    error to the caller)."""
    _get_dl3031a().raise_if_error()
    return {"ok": True}


@mcp.tool()
def dl3031a_get_current() -> dict:
    """Return the CC-mode current setpoint (A)."""
    return {"current_setpoint_A": _get_dl3031a().get_current()}


@mcp.tool()
def dl3031a_get_voltage() -> dict:
    """Return the CV-mode voltage setpoint (V)."""
    return {"voltage_setpoint_V": _get_dl3031a().get_voltage()}


@mcp.tool()
def dl3031a_get_resistance() -> dict:
    """Return the CR-mode resistance setpoint (Ω)."""
    return {"resistance_setpoint_ohm": _get_dl3031a().get_resistance()}


@mcp.tool()
def dl3031a_get_power() -> dict:
    """Return the CP-mode power setpoint (W)."""
    return {"power_setpoint_W": _get_dl3031a().get_power()}


@mcp.tool()
def dl3031a_get_current_range() -> dict:
    return {"current_range_A": _get_dl3031a().get_current_range()}


@mcp.tool()
def dl3031a_get_voltage_range() -> dict:
    return {"voltage_range_V": _get_dl3031a().get_voltage_range()}


@mcp.tool()
def dl3031a_get_slew() -> dict:
    return {"slew_A_per_us": _get_dl3031a().get_slew()}


@mcp.tool()
def dl3031a_get_trigger_source() -> dict:
    return {"trigger_source": _get_dl3031a().get_trigger_source()}


@mcp.tool()
def dl3031a_measure_voltage() -> dict:
    """Single-shot voltage measurement (~200 ms integration)."""
    return {"voltage_V": _get_dl3031a().measure_voltage()}


@mcp.tool()
def dl3031a_measure_current() -> dict:
    """Single-shot current measurement (~200 ms integration)."""
    return {"current_A": _get_dl3031a().measure_current()}


@mcp.tool()
def dl3031a_measure_power() -> dict:
    return {"power_W": _get_dl3031a().measure_power()}


@mcp.tool()
def dl3031a_measure_resistance() -> dict:
    return {"resistance_ohm": _get_dl3031a().measure_resistance()}


@mcp.tool()
def dl3031a_fetch_voltage() -> dict:
    """Non-blocking V read (~1 ms). Reads the device's continuously-updated
    measurement register without triggering a fresh integration."""
    return {"voltage_V": _get_dl3031a().fetch_voltage()}


@mcp.tool()
def dl3031a_fetch_current() -> dict:
    """Non-blocking I read (~1 ms)."""
    return {"current_A": _get_dl3031a().fetch_current()}


@mcp.tool()
def dl3031a_fetch_power() -> dict:
    return {"power_W": _get_dl3031a().fetch_power()}


@mcp.tool()
def dl3031a_fetch_resistance() -> dict:
    return {"resistance_ohm": _get_dl3031a().fetch_resistance()}


@mcp.tool()
def dl3031a_fetch() -> dict:
    """Non-blocking V / I / P / R snapshot — reads the device's
    continuously-updated measurement register without triggering a fresh
    integration. Suitable for fast polling loops where ``dl3031a_measure``
    (which forces a ~200 ms integration per call) would be too slow."""
    return _get_dl3031a().fetch_all()


@mcp.tool()
def dl3031a_set_function_mode(mode: str) -> dict:
    """Choose which subsystem drives the input regulation:
    ``FIXed`` (static :SOUR:FUNC + setpoint), ``LIST`` (programmed sequence),
    ``WAVe``, ``BATTery``, ``OCP``, ``OPP``."""
    dl = _get_dl3031a()
    dl.set_function_mode(mode)
    return {"function_mode": dl.get_function_mode()}


@mcp.tool()
def dl3031a_get_function_mode() -> dict:
    return {"function_mode": _get_dl3031a().get_function_mode()}


@mcp.tool()
def dl3031a_trigger() -> dict:
    """Fire a software (BUS) trigger. Starts a LIST or transient
    sequence after the trigger source has been set to ``BUS``."""
    _get_dl3031a().trigger_now()
    return {"triggered": True}


@mcp.tool()
def dl3031a_set_trigger_source(source: str) -> dict:
    """Pick the trigger source: ``BUS`` (software / MCP),
    ``EXTernal`` (rear-panel I/O), or ``MANUal`` (front-panel TRAN key)."""
    dl = _get_dl3031a()
    dl.set_trigger_source(source)
    return {"trigger_source": dl.get_trigger_source()}


@mcp.tool()
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


@mcp.tool()
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


@mcp.tool()
def dl3031a_transient_enable(on: bool) -> dict:
    """Arm (``on=True``) or disarm the transient generator
    (``:SOURce:TRANsient:STATe``)."""
    _get_dl3031a().transient_enable(on)
    return {"transient_armed": on}


@mcp.tool()
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


@mcp.tool()
def dl3031a_battery_stats() -> dict:
    """Current battery-discharge stats from the firmware: capacity
    (mAh), energy (Wh), discharge time (s), instantaneous V/I."""
    return _get_dl3031a().battery_stats()


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
# Tools — UART text + miscellaneous
# ---------------------------------------------------------------------------


@mcp.tool()
def write_uart_tx(text: str) -> dict:
    """Send text out the UART TX pin.

    Requires the UART decoder to be enabled (``set_uart(True, baudrate=...)``).
    """
    smu = _get_smu()
    smu.write_tx(text)
    return {"wrote_bytes": len(text.encode("utf-8"))}


@mcp.tool()
def get_gpi(pin: int) -> dict:
    """Read the state of one of the GPI pins (1 or 2)."""
    smu = _get_smu()
    return {"pin": pin, "state": smu.get_gpi(pin)}


# ---------------------------------------------------------------------------
# Tools — connection management
# ---------------------------------------------------------------------------


@mcp.tool()
def reconnect() -> dict:
    """Close and reopen the SMU connection. Useful if the device became unresponsive."""
    _close_smu()
    smu = _get_smu()
    return {"is_connected": smu.is_connected, "port": smu.info.port if smu.info else None}


@mcp.tool()
def disconnect() -> dict:
    """Close the SMU connection so another client can hold the device."""
    _close_smu()
    return {"is_connected": False}


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

"""MCP server exposing the Arc Pro SMU as tools any MCP client can call.

Designed for use with Claude Code, Claude Desktop, or any other MCP-aware
client. Run as ``opensmu-mcp`` or ``python -m opensmu.mcp``.

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
"""

from __future__ import annotations

import struct
import threading
import time
from pathlib import Path
from typing import Optional

from mcp.server.fastmcp import FastMCP

from opensmu import SMU, Channel
from opensmu.channels import WIRE_ID_TO_CHANNEL
from opensmu.exceptions import SMUError
from opensmu.protocol import iter_frames, iter_samples

mcp = FastMCP("opensmu")

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
    except SMUError as exc:
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


@mcp.tool()
def record(
    seconds: float,
    channels: Optional[list[str]] = None,
    save_path: Optional[str] = None,
    name: str = "recording",
) -> dict:
    """Run a synchronous recording for ``seconds`` seconds; return summary stats.

    Args:
        seconds: recording duration.
        channels: list of channel codes (default ``["mc", "mv"]``).
            Enabling ``mc`` auto-includes ``mp``; enabling ``ac`` auto-includes ``ap``.
        save_path: optional file path. ``.csv``/``.json``/``.opensmu``
            (native binary) extensions are auto-detected; any other
            extension is saved as native ``.opensmu``.
        name: recording name (recorded in metadata).

    Returns per-channel statistics (sample_count, min, max, average, rms,
    charge for current channels, energy for power channels) and the file
    path if saved.
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
        "channels": {},
    }
    for ch_code in sorted({c.code for c in rec.channels}):
        ch = Channel.from_code(ch_code)
        stats = rec.statistics(ch)
        out["channels"][ch_code] = {
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
        }

    if save_path:
        p = Path(save_path)
        suffix = p.suffix.lower()
        if suffix == ".csv":
            rec.save_csv(p)
        elif suffix == ".json":
            rec.save_json(p)
        else:
            if suffix != ".opensmu":
                p = p.with_suffix(".opensmu")
            rec.save(p)
        out["saved_to"] = str(p)

    return out


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
    """``opensmu-mcp`` entry point. Runs the FastMCP server on stdio."""
    try:
        mcp.run()
    finally:
        _close_smu()


if __name__ == "__main__":
    main()

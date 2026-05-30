"""MCP tool surface for the Eastwood QR10x programmable resistor.

Per the v1.0 driver-symmetric architecture, each driver owns its own
MCP tools and exposes them via :py:func:`register_mcp_tools`. The
top-level :py:mod:`benchctrl.mcp` orchestrator calls this function at
startup to register every QR10x tool on the shared :py:class:`FastMCP`
server.

Connection state (``_qr10x``) lives in this module. Tests can mutate
the singleton via this module to inject fakes.
"""

from __future__ import annotations

import threading
from typing import Optional

from benchctrl.drivers.eastwood_qr10x import QR10x

_qr10x: Optional[QR10x] = None
_qr10x_lock = threading.RLock()


def _get_qr10x() -> QR10x:
    from benchctrl.drivers.eastwood_qr10x.driver import QR10xConnectionError
    if _qr10x is None or not _qr10x.is_open:
        raise QR10xConnectionError(
            "QR10x not open — call qr10x_open(port=...) first."
        )
    return _qr10x


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


def qr10x_close() -> dict:
    """Close the QR10x connection so the port can be reopened."""
    global _qr10x
    with _qr10x_lock:
        if _qr10x is None:
            return {"closed": False, "note": "no QR10x was open"}
        _qr10x.close()
        _qr10x = None
    return {"closed": True}


def qr10x_info() -> dict:
    """Get the QR10x's identity and static specs."""
    return _get_qr10x().info().to_dict()


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


def qr10x_get_setpoint() -> dict:
    """Get the current resistance setpoint (Ω) — what was last commanded."""
    return {"setpoint_ohm": _get_qr10x().get_setpoint()}


def qr10x_actual_resistance() -> dict:
    """Get the actual achieved resistance (PV, Ω) — what the device produced."""
    return {"actual_resistance_ohm": _get_qr10x().actual_resistance()}


def qr10x_set_safety_limit(ohms: float) -> dict:
    """Set the QR10x's minimum-resistance safety limit (Ω).

    Any subsequent set_resistance below this value is clamped
    device-side. Set this to keep power within the device's rating for
    your source voltage. At 3.2 V (e.g. AA pair) the rated 1 W gives a
    minimum safe R of ~12 Ω; at 5 V it's ~25 Ω.
    """
    return _get_qr10x().set_safety_limit(ohms)


def qr10x_get_safety_limit() -> dict:
    """Get the QR10x's minimum-resistance safety limit (Ω)."""
    return {"safety_limit_ohm": _get_qr10x().get_safety_limit()}


def qr10x_get_temperature() -> dict:
    """Get the QR10x's internal temperature sensor (°C)."""
    return {"temperature_C": _get_qr10x().get_temperature()}


def qr10x_incr(delta_ohm: float) -> dict:
    """Step the QR10x setpoint up by ``delta_ohm`` Ω."""
    qr = _get_qr10x()
    out = qr.incr(delta_ohm)
    out["actual_resistance_ohm"] = qr.actual_resistance()
    return out


def qr10x_decr(delta_ohm: float) -> dict:
    """Step the QR10x setpoint down by ``delta_ohm`` Ω."""
    qr = _get_qr10x()
    out = qr.decr(delta_ohm)
    out["actual_resistance_ohm"] = qr.actual_resistance()
    return out


_TOOLS = (
    qr10x_open,
    qr10x_close,
    qr10x_info,
    qr10x_set_resistance,
    qr10x_get_setpoint,
    qr10x_actual_resistance,
    qr10x_set_safety_limit,
    qr10x_get_safety_limit,
    qr10x_get_temperature,
    qr10x_incr,
    qr10x_decr,
)


def register_mcp_tools(mcp) -> None:
    """Register every QR10x MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

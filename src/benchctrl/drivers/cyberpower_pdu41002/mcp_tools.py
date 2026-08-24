"""MCP tool surface for the CyberPower PDU41002 switched PDU.

Per the v1.0 driver-symmetric architecture, each driver owns its own MCP tools
and exposes them via :py:func:`register_mcp_tools`. The top-level
:py:mod:`benchctrl.mcp` orchestrator calls this function at startup to register
every PDU41002 tool on the shared :py:class:`FastMCP` server.

Connection state (``_pdu``) lives in this module. Tests can mutate the singleton
via this module to inject fakes.

**This is the first driver whose MCP surface can cut mains power** — and, at
this stage, deliberately cannot. Every tool here is read-only; outlet switching
arrives with the driver's mutators. Two consequences shape the module:

- The docstrings are the safety interface. They are what a model reads before
  calling, so each one states plainly what it does *not* do, and
  :py:func:`pdu41002_open` states that the allowlist is the operator's decision
  rather than something a model should widen on its own initiative.
- ``pdu41002_open`` takes no password parameter at all. Not "defaults to None" —
  absent. A model must not be able to put a credential in a tool-call argument,
  where it would be logged in the conversation transcript. The password comes
  from ``BENCHCTRL_PDU_PASSWORD`` in the server's environment.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Sequence

log = logging.getLogger("benchctrl.drivers.cyberpower_pdu41002.mcp_tools")

_pdu = None
_pdu_lock = threading.RLock()


def _get_pdu():
    from benchctrl.drivers.cyberpower_pdu41002.driver import PDU41002ConnectionError

    # Take the lock: pdu41002_open/pdu41002_close mutate this global from other
    # threads, and reading it unguarded would be a race.
    with _pdu_lock:
        if _pdu is None:
            raise PDU41002ConnectionError(
                "PDU41002 not open — call pdu41002_open() first."
            )
        return _pdu


def pdu41002_open(
    port: Optional[str] = None,
    host: Optional[str] = None,
    allowed_outlets: Optional[Sequence[int]] = None,
    username: str = "admin",
) -> dict:
    """Open a CLI session to a CyberPower PDU41002 switched PDU.

    Pass **exactly one** transport:

    - ``port`` — the serial console, e.g. ``"/dev/ttyUSB0"``.
    - ``host`` — the network path over SSH, e.g. ``"pdu-benchctrl"``.

    Passing both is an error rather than a silent preference, so the run log can
    always say which wire a command travelled on.

    ``allowed_outlets`` is the set of outlet numbers (1-8) this session is
    permitted to switch **later**, once switching tools exist. It defaults to
    empty, meaning "switch nothing". Widening it authorises cutting mains to
    whatever is plugged into those outlets: treat it as an operator decision and
    ask rather than guessing. Nothing in this module can switch an outlet today.

    There is no password parameter. The credential is read from
    ``BENCHCTRL_PDU_PASSWORD`` in the server's environment; if that is unset,
    this call fails with a message naming it.

    Two device quirks worth knowing before calling:

    - **Only one CLI session exists across all transports.** If another session
      is logged in — including one left behind by a process that closed its port
      without logging out — this fails with a "single session" error *after* the
      password is accepted. That is not a bad credential.
    - Authentication over SSH takes around 7.5 seconds. The call is slow, not
      stuck.

    Returns the device identity on success.
    """
    global _pdu
    from benchctrl import session
    from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

    with _pdu_lock:
        if _pdu is not None:
            return {
                "error": "PDU41002 already open",
                "guidance": "Call pdu41002_close() before reopening.",
                "current_transport": _pdu.transport,
            }
        _pdu = session.resolve(
            "cyberpower_pdu41002",
            opener=CyberPowerPDU41002.open,
            open_kwargs={
                "port": port,
                "host": host,
                "allowed_outlets": tuple(allowed_outlets or ()),
                "username": username,
            },
        )
    info = _pdu.read_identity()
    return {
        "transport": _pdu.transport,
        "outlet_count": _pdu.outlet_count,
        "allowed_outlets": sorted(_pdu.allowed_outlets),
        "info": info.to_dict(),
    }


def pdu41002_close() -> dict:
    """Close the session, releasing the device's CLI for other transports.

    Sends ``exit`` first, which is required rather than polite: this PDU allows
    one CLI session at a time and does **not** drop it when the connection
    closes. Skipping it leaves the PDU unreachable from the other transport,
    with a symptom that looks like a wrong password.

    Never changes outlet state. Whatever is powered stays powered.
    """
    global _pdu
    with _pdu_lock:
        if _pdu is None:
            return {"closed": False, "note": "PDU41002 was not open"}
        _pdu.close()
        _pdu = None
    return {"closed": True}


def pdu41002_info() -> dict:
    """Identity from ``sys show``: name, location, contact, model, versions, MAC."""
    return _get_pdu().read_identity().to_dict()


def pdu41002_status() -> dict:
    """Whole-device metering from ``devsta show``.

    Load in amps / watts / VA, power factor, input voltage and frequency, plus
    the peak-load and energy totals the device accumulates.

    ``power_factor`` is ``null`` at zero load — the device prints ``----`` and
    that is normal, not a fault.

    This is the *device total* across all outlets. The PDU41002 does not meter
    outlets individually, so there is no per-outlet current to ask for.
    """
    return _get_pdu().read_device_status().to_dict()


def pdu41002_measure_load() -> dict:
    """Total device load in amps."""
    return {"load_A": _get_pdu().measure_load_A()}


def pdu41002_measure_voltage() -> dict:
    """Input mains voltage in volts."""
    return {"voltage_V": _get_pdu().measure_voltage_V()}


def pdu41002_measure_frequency() -> dict:
    """Input mains frequency in hertz."""
    return {"frequency_Hz": _get_pdu().measure_frequency_Hz()}


def pdu41002_outlet_states() -> dict:
    """Every outlet's on/off state in one round trip.

    ``true`` means the outlet is energised. Keys are outlet numbers as strings,
    because JSON object keys are always strings.

    Read-only — this reports state, it does not change it.
    """
    states = _get_pdu().outlet_states()
    return {
        "outlets": {str(k): v for k, v in sorted(states.items())},
        "on_count": sum(1 for v in states.values() if v),
    }


def pdu41002_outlet_state(index: int) -> dict:
    """Whether one outlet is energised. ``on: true`` means mains is present.

    ``index`` is 1-8. Aggregate targets such as ``"all"`` are deliberately
    unsupported throughout this driver.
    """
    pdu = _get_pdu()
    return {
        "index": index,
        "on": pdu.outlet_state(index),
        "name": pdu.outlet_name(index),
    }


def pdu41002_outlet_config() -> dict:
    """Per-outlet switching delays from ``oltcfg``.

    Each outlet has a configurable on-delay, off-delay and reboot duration.
    These are the numbers that determine how long a switch takes to settle, so
    they are worth reading before concluding that an outlet failed to respond.
    """
    cfg = _get_pdu().read_outlet_config()
    return {"outlets": {str(k): v.to_dict() for k, v in sorted(cfg.items())}}


def pdu41002_allowed_outlets() -> dict:
    """Which outlets this session may switch, and which the governor may cut.

    Reports the policy, and states plainly that no switching tool exists yet, so
    a model cannot conclude from an allowlist that it has a way to use it.
    """
    pdu = _get_pdu()
    return {
        "allowed_outlets": sorted(pdu.allowed_outlets),
        "panic_outlets": sorted(pdu.panic_outlets),
        "outlet_count": pdu.outlet_count,
        "switching_available": False,
        "note": (
            "Read-only build: no tool in this server can change outlet state. "
            "allowed_outlets is set when the session is opened and is an "
            "operator decision."
        ),
    }


def pdu41002_transport() -> dict:
    """Which wire this session uses: ``"serial"`` or ``"ssh"``.

    Worth recording alongside any action, and worth knowing when a second
    session fails to open: the device permits only one CLI session across both
    transports at once.
    """
    pdu = _get_pdu()
    return {"transport": pdu.transport, "is_open": pdu.is_open}


_TOOLS = (
    pdu41002_open,
    pdu41002_close,
    pdu41002_info,
    pdu41002_status,
    pdu41002_measure_load,
    pdu41002_measure_voltage,
    pdu41002_measure_frequency,
    pdu41002_outlet_states,
    pdu41002_outlet_state,
    pdu41002_outlet_config,
    pdu41002_allowed_outlets,
    pdu41002_transport,
)


def register_mcp_tools(mcp) -> None:
    """Register every PDU41002 MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

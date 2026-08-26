"""MCP tool surface for the Silicon Labs CP2112 GPIO bridge.

Per the driver-symmetric architecture, each driver owns its own MCP tools and
exposes them via :py:func:`register_mcp_tools`; :py:mod:`benchctrl.mcp` calls
this at startup.

**Two tools here can hold a DUT in reset.** ``cp2112_set_line_asserted`` and
``cp2112_trigger_reset_pulse`` move real hardware, and the first one *latches* —
asserting a line and never releasing it leaves a target held down indefinitely.
So the docstrings are part of the safety interface, since they are what a model
reads before calling: each says what the tool does to the DUT, and the asserting
tool names the release call explicitly rather than assuming the caller infers
it.

``cp2112_open`` takes ``allowed_lines`` but the driver enforces it regardless of
what a model believes about the wiring. A model should treat widening the
allowlist as an operator decision to *ask about*, never to infer — the driver
cannot tell whether GPIO.3 is a reset line, an enable, or nothing at all, and
the answer lives on the bench rather than in any config.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional, Sequence

log = logging.getLogger("benchctrl.drivers.silabs_cp2112.mcp_tools")

_dev = None
_dev_lock = threading.RLock()


def _get_dev():
    from benchctrl.drivers.silabs_cp2112.driver import CP2112ConnectionError

    # Take the lock: cp2112_open/cp2112_close mutate this global from other
    # threads, and reading it unguarded would be a race.
    with _dev_lock:
        if _dev is None:
            raise CP2112ConnectionError(
                "CP2112 not open — call cp2112_open() first."
            )
        return _dev


def cp2112_open(
    path: Optional[str] = None,
    serial: Optional[str] = None,
    allowed_lines: Optional[Sequence[int]] = None,
) -> dict:
    """Open a CP2112 HID bridge for open-drain control-line use.

    ``allowed_lines`` is the set of GPIO indices (0-7) this session may
    configure or drive. It defaults to empty, meaning "drive nothing". Listing a
    line authorises pulling it low, which on a reset line holds the attached
    target in reset — so the list should come from whoever wired the bench, not
    from inference about what a pin is probably for.

    ``path`` is optional: with a single CP2112 attached the device is found by
    USB VID/PID. Pass ``serial`` to disambiguate when two are present; the
    driver refuses to guess rather than risk resetting the wrong board.

    Opening changes nothing on the hardware. Every pin keeps the direction and
    level it already had, and the as-found configuration is recorded so
    ``cp2112_close`` can restore it.
    """
    global _dev
    from benchctrl.drivers.silabs_cp2112.driver import CP2112

    with _dev_lock:
        if _dev is not None:
            raise RuntimeError("CP2112 already open — call cp2112_close() first.")
        dev = CP2112.open(
            path,
            serial=serial,
            allowed_lines=tuple(allowed_lines or ()),
        )
        _dev = dev
    info = dev.read_identity()
    return {
        "path": info.path,
        "serial": info.serial,
        "part_number": f"0x{info.part_number:02X}",
        "device_version": f"0x{info.device_version:02X}",
        "allowed_lines": sorted(dev.allowed_lines),
    }


def cp2112_close() -> dict:
    """Close the CP2112, restoring the GPIO configuration found at open.

    This **releases any asserted line**, which matters: closing without
    restoring would leave a target held in reset with no process left to
    release it.
    """
    global _dev
    with _dev_lock:
        if _dev is None:
            return {"closed": False, "reason": "not open"}
        _dev.close()
        _dev = None
    return {"closed": True}


def cp2112_info() -> dict:
    """Identity of the open CP2112: part number, revision and USB serial."""
    info = _get_dev().read_identity()
    return {
        "part_number": f"0x{info.part_number:02X}",
        "device_version": f"0x{info.device_version:02X}",
        "serial": info.serial,
        "path": info.path,
        "is_cp2112": info.is_cp2112,
    }


def cp2112_line_states() -> dict:
    """Direction, drive mode and level for all eight GPIOs.

    A caution on interpreting ``level``: an undriven pin is high-impedance and
    its input buffer latches 1, so a level of 1 does **not** prove anything is
    connected, and a high-impedance voltmeter can read ~0 V on the same net
    without either reading being wrong. A level identifies a pin only when you
    can make it *change*.
    """
    dev = _get_dev()
    states = dev.read_line_states()
    return {
        "lines": [
            {
                "index": i,
                "direction": "output" if s.is_output else "input",
                "drive": "open-drain" if s.open_drain else ("input" if not s.is_output else "push-pull"),
                "level": int(s.level),
                "asserted": s.asserted if s.is_output else None,
                "alternate_function": s.alternate_function,
                "allowed": i in dev.allowed_lines,
            }
            for i, s in sorted(states.items())
        ]
    }


def cp2112_line_state(index: int) -> dict:
    """Direction, drive mode and level for one GPIO."""
    s = _get_dev().read_line_state(index)
    return {
        "index": s.index,
        "direction": "output" if s.is_output else "input",
        "open_drain": s.open_drain,
        "level": int(s.level),
        "asserted": s.asserted if s.is_output else None,
        "alternate_function": s.alternate_function,
    }


def cp2112_allowed_lines() -> dict:
    """Which GPIO indices this session may configure or drive."""
    dev = _get_dev()
    return {"allowed_lines": sorted(dev.allowed_lines), "line_count": dev.line_count}


def cp2112_set_line_mode(index: int, output: bool) -> dict:
    """Configure one GPIO as an **open-drain output** or as an input.

    Outputs are always open-drain; push-pull is not offered. An open-drain pin
    can pull a net low and release it but never source into it, which is what
    makes it safe on a reset line the target pulls up to its own rail.

    Configuring a pin as an input makes it high-impedance, which *releases*
    anything it was holding.

    GPIO.0, GPIO.1 and GPIO.7 carry alternate chip functions (TX toggle, RX
    toggle, clock output). Configuring one as an output is refused unless the
    operator has confirmed the alternate function is off — this tool does not
    offer the override, because that confirmation is a bench observation rather
    than something a model can establish.
    """
    s = _get_dev().set_line_mode(index, output=bool(output))
    return {
        "index": s.index,
        "direction": "output" if s.is_output else "input",
        "open_drain": s.open_drain,
        "level": int(s.level),
    }


def cp2112_set_line_asserted(index: int, asserted: bool) -> dict:
    """Pull a control line low (``asserted=true``) or release it.

    **This latches.** On a reset line, ``asserted=true`` holds the attached
    target in reset and it stays held until something releases it — call this
    again with ``asserted=false``, or ``cp2112_reset_lines``, or
    ``cp2112_close``. If you want a bounded pulse, use
    ``cp2112_trigger_reset_pulse`` instead, which cannot leave the line held.

    ``asserted`` rather than high/low deliberately: reset lines are active-low,
    so "high" is ambiguous exactly where a mistake is expensive.

    The result is read back and verified. If the line does not move, the call
    fails rather than reporting success — an open-drain output cannot pull a net
    that something stronger is holding high, and that is worth knowing.
    """
    s = _get_dev().set_line_asserted(index, bool(asserted))
    return {"index": s.index, "asserted": s.asserted, "level": int(s.level)}


def cp2112_trigger_reset_pulse(
    index: int, duration_s: float = 0.1, settle_s: float = 0.0
) -> dict:
    """Assert a line for ``duration_s`` seconds, then release it.

    The intended way to reset a DUT: it cannot leave the line held, because the
    release happens even if the hold is interrupted.

    ``settle_s`` waits after release, for when the next thing you do is measure
    and would otherwise sample a still-booting target.

    Pulses below 5 ms are refused rather than silently stretched: every GPIO
    transition is a separate USB control transfer, so short pulses are bounded
    by bus scheduling and not by the chip. Sub-millisecond timing needs a
    different instrument.
    """
    s = _get_dev().trigger_reset_pulse(
        index, duration_s=duration_s, settle_s=settle_s
    )
    return {
        "index": s.index,
        "pulsed_s": duration_s,
        "asserted": s.asserted,
        "level": int(s.level),
    }


def cp2112_reset_lines() -> dict:
    """Return every allowed line to an input, releasing anything held.

    High-impedance is the chip's own power-on state, so this is "as the hardware
    would come up" rather than an invented safe state. For an open-drain reset
    line, high-Z *is* released. Lines outside ``allowed_lines`` are not touched.
    """
    cfg = _get_dev().reset_lines()
    return {
        "direction": f"0x{cfg.direction:02X}",
        "push_pull": f"0x{cfg.push_pull:02X}",
    }


_TOOLS = (
    cp2112_open,
    cp2112_close,
    cp2112_info,
    cp2112_line_states,
    cp2112_line_state,
    cp2112_allowed_lines,
    cp2112_set_line_mode,
    cp2112_set_line_asserted,
    cp2112_trigger_reset_pulse,
    cp2112_reset_lines,
)


def register_mcp_tools(mcp) -> None:
    """Register every CP2112 MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

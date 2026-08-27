"""MCP tool surface for the Ontrak ADU218 relay / digital-input interface.

Per the v1.0 driver-symmetric architecture, each driver owns its own MCP tools
and exposes them via :py:func:`register_mcp_tools`. The top-level
:py:mod:`benchctrl.mcp` orchestrator calls this function at startup.

Connection state (``_adu218``) lives in this module. Tests can mutate the
singleton via this module to inject fakes.

Two things are said in almost every docstring below, because an LLM caller has
no other way to learn them:

- **Relay state is reported as energised, never "open" or "closed".** ``is_open``
  in this repo means *the link is connected*, which is the opposite sign to an
  open contact. ``on: true`` means the relay is conducting.
- **The device never reports an error.** An unknown command, a bad argument and
  a write-only command are all answered with silence, so a switch is only
  confirmed by reading the state back. That is why the switching tool verifies
  by default and why turning verification off changes what the result means.
"""

from __future__ import annotations

import threading
from typing import Optional

from benchctrl.drivers.ontrak_adu218 import OntrakADU218
from benchctrl.drivers.ontrak_adu218.driver import DEBOUNCE_MS

_adu218: Optional[OntrakADU218] = None
_adu218_lock = threading.RLock()


def _get_adu218() -> OntrakADU218:
    from benchctrl.drivers.ontrak_adu218.driver import ADU218ConnectionError

    with _adu218_lock:
        if _adu218 is None or not _adu218.is_open:
            raise ADU218ConnectionError(
                "ADU218 not open — call adu218_open() first."
            )
        return _adu218


def adu218_open(
    serial: Optional[str] = None,
    allowed_relays: Optional[list] = None,
    disarm_watchdog: bool = True,
) -> dict:
    """Open a connection to the Ontrak ADU218 relay / digital-input interface.

    8 solid-state relays (1 A at 120 V AC or DC) and 8 optically-isolated
    digital inputs, over USB. There is no port or address to supply: the device
    is found by its USB descriptor. Pass ``serial`` only when more than one
    ADU218 is attached — with several present and no serial, this fails rather
    than picking one by enumeration order.

    ``allowed_relays`` restricts which relays this session may **energise**;
    de-energising is always permitted, so a narrow allowlist can never make the
    safe state unreachable. It defaults to all eight, which is correct for this
    bench (signal-level SSRs on instrument leads) and is a deliberate difference
    from the mains PDU, where an allowlist is mandatory.

    ``disarm_watchdog`` sends ``WD0`` at connect. Leave it ``true``: the
    watchdog's own setting reads 0 both for "timed out" and "never enabled", so
    a fresh session has no way to interpret an inherited value.

    **Relays are deliberately left exactly as found.** Power-on relay state is
    undocumented and USB suspend holds outputs in their last state, so opening
    reports what is energised rather than assuming or clearing it. If any relay
    is already on, it appears in ``already_energised`` — treat that as
    information about the bench, not as something to tidy up unasked.
    """
    global _adu218
    from benchctrl import session

    with _adu218_lock:
        if _adu218 is not None:
            return {
                "error": "ADU218 already open",
                "guidance": "Call adu218_close() before reopening.",
            }
        _adu218 = session.resolve(
            "ontrak_adu218",
            opener=OntrakADU218.open,
            open_kwargs={
                "serial": serial,
                "allowed_relays": tuple(allowed_relays) if allowed_relays else None,
                "disarm_watchdog": disarm_watchdog,
            },
        )
    states = _adu218.relay_states()
    return {
        "info": _adu218.read_identity().to_dict(),
        "relay_count": _adu218.relay_count,
        "input_count": _adu218.input_count,
        "allowed_relays": sorted(_adu218.allowed_relays),
        "already_energised": sorted(i for i, on in states.items() if on),
    }


def adu218_close() -> dict:
    """Close the link. **Relays keep their current state.**

    A relay that is conducting stays conducting after this call. That is
    deliberate — the driver cannot know whether an energised relay is holding
    something that must not be interrupted. Call ``adu218_reset_relays()``
    first if you want everything de-energised, and say so rather than assuming.

    The watchdog is also left armed if it was armed. Releasing the device *is*
    the silence the watchdog exists to detect, so the relays will drop on their
    own within the configured interval — that is the interlock working.
    """
    global _adu218
    with _adu218_lock:
        if _adu218 is None:
            return {"closed": False, "note": "ADU218 was not open"}
        _adu218.close()
        _adu218 = None
    return {"closed": True}


def adu218_info() -> dict:
    """Identity from the USB descriptor: model, serial, manufacturer, bus address.

    Costs no round trip — this device has no identity command, so the values
    come from the enumeration the kernel already did. There is no firmware
    version because the device reports none (``bcdDevice`` is 0000); an absent
    field here means the device does not say, not that the driver failed to ask.
    """
    return _get_adu218().read_identity().to_dict()


def adu218_relay_states() -> dict:
    """Every relay's state in one round trip. ``true`` means **energised**.

    One command, so all eight values are from the same instant. Keys are relay
    numbers (0-7) as strings, because JSON object keys are always strings.

    Read-only. This reports state and does not change it.
    """
    adu = _get_adu218()
    states = adu.relay_states()
    return {
        "relays": {str(k): v for k, v in sorted(states.items())},
        "energised": sorted(k for k, v in states.items() if v),
        "mask": sum(1 << k for k, v in states.items() if v),
    }


def adu218_relay_state(index: int) -> dict:
    """Whether one relay is energised. ``on: true`` means the contact conducts.

    ``index`` is 0-7 — this device is **zero-indexed**, unlike the PDU's
    1-indexed outlets.
    """
    return {"index": index, "on": _get_adu218().relay_state(index)}


def adu218_set_relay_state(index: int, on: bool, verify: bool = True) -> dict:
    """**Switch one relay.** This physically makes or breaks a circuit.

    The relays are 1 A solid-state (PhotoMOS) devices rated to 120 V AC or DC.
    Whatever is wired across the terminal block will be connected or
    disconnected — on this bench that includes instrument sense leads, so a
    switch mid-measurement changes what an instrument is reading.

    Only relays in ``allowed_relays`` may be **energised**; de-energising is
    always allowed. Check ``adu218_allowed_relays`` first, and if the relay you
    need is not listed, ask the operator rather than reopening with a wider list.

    Args:
        index: relay number, 0-7. One relay per call. Use
            ``adu218_set_relay_port`` for a simultaneous multi-relay transition.
        on: ``true`` energises (conducting), ``false`` de-energises.
        verify: read the relay back and confirm it moved. Leave this ``true``.
            The device does not acknowledge writes at all, so with
            ``verify=false`` the reported ``state`` is what was *requested*, not
            what the hardware did.

    Returns:
        ``state`` is the verified state read back from the device, and
        ``verified`` says whether it was actually checked.
    """
    state = _get_adu218().set_relay_state(index, on, verify=verify)
    return {
        "index": index,
        "requested": on,
        "state": state,
        "verified": bool(verify),
    }


def adu218_set_relay_port(mask: int, verify: bool = True) -> dict:
    """**Switch all eight relays at once** from a bitmask. Bit *n* is relay *n*.

    Every bit physically makes or breaks a circuit — the same 1 A solid-state
    contacts ``adu218_set_relay_state`` drives, eight at a time. On this bench
    that includes instrument sense leads, so this call can change what another
    instrument is reading.

    One command, so all eight relays transition simultaneously. Per-relay calls
    cannot do that — they pass through intermediate combinations that may be
    electrically meaningful.

    ``mask`` is 0-255. Note this sets **every** relay: a bit that is 0
    de-energises that relay even if you did not intend to touch it. Read
    ``adu218_relay_states`` first and modify the ``mask`` it returns if you only
    mean to change some.

    Any relay the mask would energise must be in ``allowed_relays``, checked
    against the whole mask rather than against what would change.
    """
    result = _get_adu218().set_relay_port(mask, verify=verify)
    return {
        "requested_mask": mask,
        "mask": result,
        "energised": sorted(i for i in range(8) if result & (1 << i)),
        "verified": bool(verify),
    }


def adu218_reset_relays(verify: bool = True) -> dict:
    """**De-energise every relay** — the safe state.

    Breaks all eight circuits in one simultaneous transition. This ignores
    ``allowed_relays`` on purpose: the allowlist exists to prevent unintended
    *energising*, and a rule that blocked de-energising would make a narrower
    policy the more dangerous one.
    """
    return {"mask": _get_adu218().reset_relays(verify=verify), "energised": []}


def adu218_allowed_relays() -> dict:
    """Which relays this session may energise.

    Authoritative — do not infer it from ``relay_count``. Everything outside the
    list is refused by the driver. De-energising and ``adu218_reset_relays`` are
    never restricted.
    """
    adu = _get_adu218()
    return {
        "allowed_relays": sorted(adu.allowed_relays),
        "relay_count": adu.relay_count,
        "note": (
            "Relays outside allowed_relays cannot be energised. The list is "
            "fixed when the session opens and is an operator decision: ask "
            "rather than reopening to widen it. De-energising is always allowed."
        ),
    }


def adu218_input_states() -> dict:
    """All eight digital inputs. ``true`` means the opto-isolator is conducting.

    The inputs are arranged as **two ports of four**: port A lines 0-3 and port
    B lines 0-3. That is why an index here is 0-3 while a relay index is 0-7.

    Read-only, and there is nothing to configure — these follow whatever is
    wired to the terminal block.
    """
    adu = _get_adu218()
    states = adu.input_states()
    return {
        "ports": {port: list(lines) for port, lines in states.items()},
        "mask": adu.input_mask(),
    }


def adu218_input_state(port: str, index: int) -> dict:
    """Whether one digital input is asserted.

    ``port`` is ``"A"`` or ``"B"``; ``index`` is **0-3**, four lines per port.
    """
    return {
        "port": port.upper(),
        "index": index,
        "asserted": _get_adu218().input_state(port, index),
    }


def adu218_input_port_mask(port: str) -> dict:
    """One input port's four lines as a nibble, 0-15. ``port`` is ``"A"`` or ``"B"``.

    Bit 0 is line 0 — this is the one input read that needs no reordering, which
    is why it is offered separately from ``adu218_input_states``. Use that tool
    for all eight lines; use this when you want one port's bits and want to
    trust their positions.
    """
    letter = port.upper()
    value = _get_adu218().input_port_mask(port)
    return {
        "port": letter,
        "mask": value,
        "lines": [bool(value & (1 << line)) for line in range(4)],
    }


def adu218_counters() -> dict:
    """Every input's event counter, **without clearing any of them**.

    The device counts transitions on each digital input in hardware, so events
    are caught between polls. Counters wrap at 65535.

    Prefer this over ``adu218_clear_counter`` and subtract successive readings
    yourself — see that tool for why.
    """
    counters = _get_adu218().read_counters()
    return {"counters": {str(k): v for k, v in sorted(counters.items())}}


def adu218_counter(index: int) -> dict:
    """One input's event count, without clearing it. ``index`` is 0-7."""
    return {"index": index, "count": _get_adu218().read_counter(index)}


def adu218_clear_counter(index: int) -> dict:
    """Read **and clear** one event counter. The returned value is destroyed.

    This is the only command on the device that both answers and changes state,
    which makes it the only one that must never be retried: if the reply is lost
    after the device has already cleared, the count is gone permanently and a
    retry reports 0 — indistinguishable from "no events happened". The driver
    therefore does not retry it, and the count in the result is the only copy.

    Prefer ``adu218_counter`` and difference successive readings. Use this only
    when you specifically want the counter zeroed.
    """
    return {"index": index, "count_cleared": _get_adu218().clear_counter(index)}


def adu218_debounce() -> dict:
    """The input de-bounce setting: 0, 1 or 2, plus what it means in ms.

    **A higher setting is a shorter filter**: 0 = 10 ms, 1 = 1 ms (default),
    2 = 100 us. ``debounce_ms`` is returned alongside the raw setting because
    the number on its own reads backwards.

    Three settings, not the four Ontrak's web page lists — that page's fourth
    option is shared boilerplate across several products.
    """
    setting = _get_adu218().read_debounce()
    return {"debounce": setting, "debounce_ms": DEBOUNCE_MS[setting]}


def adu218_set_debounce(setting: int) -> dict:
    """Set the input de-bounce filter. ``setting`` is 0, 1 or 2.

    **Higher means a shorter filter**: 0 = 10 ms, 1 = 1 ms (default),
    2 = 100 us. To de-bounce a noisy contact as hard as possible, pass 0, not 2.

    Affects how the digital inputs and their event counters respond to fast
    transitions. Note the setting has no observable effect on a clean signal
    slower than a few hundred Hz — every filter width is shorter than such a
    signal's half-period. Returns the verified read-back.
    """
    actual = _get_adu218().set_debounce(setting)
    return {"debounce": actual, "debounce_ms": DEBOUNCE_MS[actual]}


def adu218_watchdog() -> dict:
    """The hardware watchdog's state.

    ``setting`` is what the device reports now; ``expected`` is what this
    session last commanded. They differ only when the watchdog has fired, which
    is the *only* trace a timeout leaves — the device self-clears to 0, and 0 is
    also what "never enabled" looks like.

    **Reading this refeeds the timer.** Every command does, so polling this in a
    loop keeps an armed watchdog alive and guarantees ``tripped`` stays
    ``false``. Use it after a suspected interruption, not to monitor for one.
    """
    adu = _get_adu218()
    # Order matters: read_watchdog_tripped() clears the driver-held expectation
    # when it detects a trip, so ``expected`` has to be captured first or it
    # would always equal ``setting`` and the comparison would report nothing.
    expected = adu.watchdog_setting
    tripped = adu.read_watchdog_tripped()
    return {
        "setting": adu.read_watchdog(),
        "expected": expected,
        "tripped": tripped,
        "note": (
            "Reading the watchdog refeeds its timer. Any command does, "
            "including an invalid one."
        ),
    }


def adu218_set_watchdog(setting: int) -> dict:
    """**Arm or disarm the hardware watchdog.** Arming changes what silence means.

    ``0`` disables it. ``1``, ``2`` and ``3`` select 1 second, 10 seconds and 1
    minute. There is no separate arm step — setting a nonzero value arms it
    immediately.

    While armed, **the device de-energises all eight relays by itself** if it
    receives no command within the interval. No software is in that decision
    path, which is the point: a wedged process, a killed agent, an unplugged
    cable and a panicking kernel all look identical to the device and all drop
    the load.

    Three consequences you own once you arm it:

    1. Every relay's state now depends on how often you call. A single slow
       call to another instrument can exceed the interval and drop a relay you
       were told to hold. ``1`` (one second, measured) is unusable for general
       work; ``3`` (one minute) is the longest available.
    2. **Any** command refeeds the timer, including an invalid one and including
       a plain state read. So a status-polling loop silently neuters the
       watchdog — the feed must come from whatever is actually controlling the
       test.
    3. There is no keep-alive helper here, deliberately. A background feeder
       would keep the watchdog fed precisely while the failure it guards against
       was happening.

    Do not arm this speculatively. Arm it when a test needs the interlock, and
    disarm it when that test ends.
    """
    adu = _get_adu218()
    value = adu.set_watchdog(setting)
    from benchctrl.drivers.ontrak_adu218.driver import WATCHDOG_TIMEOUT_S

    return {
        "setting": value,
        "timeout_s": WATCHDOG_TIMEOUT_S[value],
        "armed": bool(value),
        "warning": (
            "All relays will de-energise if no command reaches the device "
            "within the interval. Any command refeeds the timer."
            if value
            else "Watchdog disabled; relays will hold their state indefinitely."
        ),
    }


_TOOLS = (
    adu218_open,
    adu218_close,
    adu218_info,
    adu218_relay_states,
    adu218_relay_state,
    adu218_set_relay_state,
    adu218_set_relay_port,
    adu218_reset_relays,
    adu218_allowed_relays,
    adu218_input_states,
    adu218_input_state,
    adu218_input_port_mask,
    adu218_counters,
    adu218_counter,
    adu218_clear_counter,
    adu218_debounce,
    adu218_set_debounce,
    adu218_watchdog,
    adu218_set_watchdog,
)


def register_mcp_tools(mcp) -> None:
    """Register every ADU218 MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

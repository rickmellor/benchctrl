"""MCP tool surface for the Siglent SDM4065A bench digital multimeter.

Per the v1.0 driver-symmetric architecture, each driver owns its own MCP
tools and exposes them via :py:func:`register_mcp_tools`. The top-level
:py:mod:`benchctrl.mcp` orchestrator calls this function at startup to
register every SDM4065A tool on the shared :py:class:`FastMCP` server.

Connection state (``_sdm4065a``) lives in this module. Tests can mutate the
singleton via this module to inject fakes.

There is no safety-critical tool here: a DMM sources nothing, so unlike the
load and the supply there is no output an agent can accidentally energise.
The failure mode to guard against is a *wrong number*, so the tools that
affect accuracy — range, NPLC, null — say so in their docstrings, which are
what the model actually reads before calling them.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

log = logging.getLogger("benchctrl.drivers.siglent_sdm4065a.mcp_tools")

# Kept as Any to avoid eagerly importing pyvisa (the actual class is
# SiglentSDM4065A — pulled in lazily inside sdm4065a_open).
_sdm4065a = None
_sdm4065a_lock = threading.RLock()


def _get_sdm4065a():
    from benchctrl.drivers.siglent_sdm4065a.driver import SDM4065AConnectionError

    # Take the lock: sdm4065a_open/sdm4065a_close mutate this global from
    # other threads, and reading it unguarded would be a race.
    with _sdm4065a_lock:
        if _sdm4065a is None:
            raise SDM4065AConnectionError(
                "SDM4065A not open — call sdm4065a_open() first."
            )
        return _sdm4065a


def sdm4065a_open(
    resource: Optional[str] = None, timeout_ms: int = 10_000
) -> dict:
    """Open a VISA session to a Siglent SDM4065A digital multimeter.

    If ``resource`` is None, auto-discovers by scanning for the Siglent
    SDM4065A USB VID/PID (0xF4EC / 0x1220). Pass an explicit VISA
    resource string (USB or LXI, e.g. ``"TCPIP::192.168.1.7::INSTR"``) to
    target a specific device.

    ``timeout_ms`` defaults to 10 s, which covers a single reading at any
    integration time. Raise it for large ``sample_count`` at high NPLC —
    see ``sdm4065a_reading_timeout_ms``.

    Returns the device's ``*IDN?`` identity on success.
    """
    global _sdm4065a
    from benchctrl import session
    from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A

    with _sdm4065a_lock:
        if _sdm4065a is not None:
            info = _sdm4065a.info()
            return {
                "error": "SDM4065A already open",
                "guidance": "Call sdm4065a_close() before reopening.",
                "current_resource": info.resource,
            }
        _sdm4065a = session.resolve(
            "siglent_sdm4065a",
            opener=SiglentSDM4065A.open,
            open_kwargs={"resource": resource, "timeout_ms": timeout_ms},
        )
    info = _sdm4065a.info()
    return {
        "resource": info.resource,
        "info": {
            "manufacturer": info.manufacturer,
            "model": info.model,
            "serial": info.serial,
            "firmware": info.firmware,
        },
    }


def sdm4065a_close() -> dict:
    """Close the SDM4065A connection.

    Nothing needs disarming — the meter sources no power — so unlike the
    load and supply this cannot leave the bench in an unsafe state.
    """
    global _sdm4065a
    with _sdm4065a_lock:
        if _sdm4065a is None:
            return {"closed": False, "note": "no SDM4065A was open"}
        _sdm4065a.close()
        _sdm4065a = None
    return {"closed": True}


def sdm4065a_info() -> dict:
    """Identity and resource string from ``*IDN?``."""
    info = _get_sdm4065a().info()
    return {
        "manufacturer": info.manufacturer,
        "model": info.model,
        "serial": info.serial,
        "firmware": info.firmware,
        "resource": info.resource,
    }


def sdm4065a_reset() -> dict:
    """``*RST`` — factory defaults (DC voltage, NPLC 10, null off, autorange).

    Wipes range, NPLC and null settings, so do any configuration *after*
    this, never before.
    """
    _get_sdm4065a().reset()
    return {"reset": True}


def sdm4065a_clear_status() -> dict:
    """``*CLS`` — clear status registers, and read the error queue empty.

    Both steps are needed: on this instrument ``*CLS`` does not actually empty
    the error queue (nor does ``*RST``), so entries have to be read out. Left
    undrained the queue fills and then answers "Queue overflow" to everything,
    which makes an unrelated command look like it failed.

    Call this after anything that may have queued an error, before relying on
    ``sdm4065a_last_error``. ``discarded`` lists what was thrown away.
    """
    dmm = _get_sdm4065a()
    # drain_errors first so the reply can say what was thrown away;
    # clear_status() itself returns None (matching every other driver) and
    # would drain silently.
    discarded = _errors_as_dicts(dmm.drain_errors())
    dmm.clear_status()
    return {"cleared": True, "discarded": discarded}


def sdm4065a_drain_errors(limit: int = 32) -> dict:
    """Read the error queue empty and return every entry that was in it.

    Use when ``sdm4065a_last_error`` reports something that looks stale, or
    after deliberately sending a command that may be rejected. Unlike
    ``sdm4065a_last_error`` (which pops one entry) this empties the queue.

    A returned list of exactly ``limit`` entries means it stopped early rather
    than reaching the end, so the queue may still hold more.
    """
    return {"errors": _errors_as_dicts(_get_sdm4065a().drain_errors(limit=limit))}


def _errors_as_dicts(errors) -> list:
    """``[(code, message)]`` -> ``[{"code": ..., "message": ...}]``.

    The SDK returns tuples, which is right for Python callers. Tools return
    named fields instead, matching ``sdm4065a_last_error`` — a bare pair would
    leave a model guessing which element is the code.
    """
    return [{"code": code, "message": message} for code, message in errors]


def sdm4065a_self_test() -> dict:
    """``*TST?`` — run the instrument's self-test."""
    return {"passed": _get_sdm4065a().self_test()}


def sdm4065a_last_error() -> dict:
    """Read one entry from the SCPI error queue. ``{"code": 0}`` if clean.

    ``{"code": 0}`` does **not** prove the previous command was accepted: this
    instrument's error queue can stop reporting altogether. Use
    ``sdm4065a_command_error`` to ask whether a command was rejected.
    """
    err = _get_sdm4065a().last_error()
    if err is None:
        return {"code": 0, "message": "No error"}
    return {"code": err[0], "message": err[1]}


def sdm4065a_command_error() -> dict:
    """Whether the instrument rejected a command — the reliable check.

    Reads ``*ESR?`` bit 5 (Command Error) instead of the error queue, and
    reports the whole register too. Prefer this to ``sdm4065a_last_error`` for
    "did that work?", because the error queue on this unit is unreliable: it
    does not get cleared by ``*CLS``, and once it has overflowed it can latch
    into answering "No Error" to everything while ``*ESR?`` stays correct.

    The register is read-destructive, so this reports errors since the last
    call to it (or to ``sdm4065a_clear_status``) and then resets. Call
    ``sdm4065a_clear_status`` first if you need the answer to be attributable
    to one specific command. The trade-off is detail: this tells you a command
    was rejected, not which error code — ``sdm4065a_last_error`` has the code
    when the queue is behaving.
    """
    dmm = _get_sdm4065a()
    status = dmm.standard_event_status()
    return {
        "command_error": bool(status & dmm.ESR_COMMAND_ERROR),
        "esr": status,
    }


def sdm4065a_standard_event_status() -> dict:
    """``*ESR?`` — the raw Standard Event Status Register, read-and-clear.

    For callers that want bits other than Command Error (bit 5). See
    ``sdm4065a_command_error`` for the common case; note that either tool
    clears the register, so the two cannot both report the same event.
    """
    return {"esr": _get_sdm4065a().standard_event_status()}


def sdm4065a_raise_if_error() -> dict:
    """Raise if the instrument's error queue holds an error."""
    _get_sdm4065a().raise_if_error()
    return {"ok": True}


def sdm4065a_set_function(function: str) -> dict:
    """Select the measurement function.

    Accepts ``"dcv"``, ``"acv"``, ``"dci"``, ``"aci"``, ``"res"`` (2-wire),
    ``"fres"`` / ``"4w"`` (4-wire), ``"cap"``, ``"freq"``, ``"per"``,
    ``"cont"``, ``"diode"``, ``"temp"``.
    """
    dmm = _get_sdm4065a()
    dmm.set_function(function)
    return {"function": dmm.get_function()}


def sdm4065a_get_function() -> dict:
    """The active function's SCPI short form, e.g. ``"VOLT"``, ``"RES"``."""
    return {"function": _get_sdm4065a().get_function()}


# --- one-shot measurements ------------------------------------------------


def sdm4065a_measure_dc_voltage(range_V: Optional[float] = None) -> dict:
    """One DC voltage reading, in volts.

    ``range_V`` must be one of 0.2 / 2 / 20 / 200 / 1000, or omit it for
    autorange.
    """
    return {"voltage_V": _get_sdm4065a().measure_dc_voltage(range_V)}


def sdm4065a_measure_ac_voltage(range_V: Optional[float] = None) -> dict:
    """One AC voltage reading, in volts RMS.

    ``range_V`` must be one of 0.2 / 2 / 20 / 200 / 750 (AC does not reach
    1000 V), or omit it for autorange.
    """
    return {"voltage_V_rms": _get_sdm4065a().measure_ac_voltage(range_V)}


def sdm4065a_measure_dc_current(range_A: Optional[float] = None) -> dict:
    """One DC current reading, in amps."""
    return {"current_A": _get_sdm4065a().measure_dc_current(range_A)}


def sdm4065a_measure_ac_current(range_A: Optional[float] = None) -> dict:
    """One AC current reading, in amps RMS."""
    return {"current_A_rms": _get_sdm4065a().measure_ac_current(range_A)}


def sdm4065a_measure_resistance(range_ohm: Optional[float] = None) -> dict:
    """One **2-wire** resistance reading, in ohms.

    ``range_ohm`` must be one of 200 / 2e3 / 20e3 / 200e3 / 1e6 / 1e7 / 1e8
    (note: 1 MΩ — the SDM4055A's 2 MΩ range does not exist on this model),
    or omit it for autorange.

    ACCURACY WARNING: 2-wire readings include ~0.2 Ω of test-lead and
    contact resistance unless nulled. At 100 Ω that is a 0.2% error, far
    larger than the instrument's own spec. For low resistance either use
    ``sdm4065a_measure_resistance_4wire``, or call ``sdm4065a_null_now``
    with the leads shorted and then read with ``sdm4065a_read`` — *not*
    with this tool, which reconfigures and thereby discards the null.
    """
    return {"resistance_ohm": _get_sdm4065a().measure_resistance(range_ohm)}


def sdm4065a_measure_resistance_4wire(range_ohm: Optional[float] = None) -> dict:
    """One **4-wire** resistance reading, in ohms.

    Requires separate source and sense leads. This is the accurate way to
    measure low resistance: lead resistance drops out of the result, so no
    null is needed for the datasheet accuracy spec to hold.
    """
    return {
        "resistance_ohm": _get_sdm4065a().measure_resistance_4wire(range_ohm)
    }


def sdm4065a_measure_capacitance(range_F: Optional[float] = None) -> dict:
    """One capacitance reading, in farads."""
    return {"capacitance_F": _get_sdm4065a().measure_capacitance(range_F)}


def sdm4065a_measure_frequency(range_Hz: Optional[float] = None) -> dict:
    """One frequency reading, in hertz."""
    return {"frequency_Hz": _get_sdm4065a().measure_frequency(range_Hz)}


def sdm4065a_measure_period(range_s: Optional[float] = None) -> dict:
    """One period reading, in seconds."""
    return {"period_s": _get_sdm4065a().measure_period(range_s)}


def sdm4065a_measure_continuity() -> dict:
    """One continuity reading, in ohms."""
    return {"resistance_ohm": _get_sdm4065a().measure_continuity()}


def sdm4065a_measure_diode() -> dict:
    """One diode-junction forward-voltage reading, in volts."""
    return {"voltage_V": _get_sdm4065a().measure_diode()}


def sdm4065a_measure_temperature(
    probe: Optional[str] = None, sensor_type: Optional[str] = None
) -> dict:
    """One temperature reading, in the configured unit.

    ``probe`` is ``"RTD"`` or ``"THER"``; ``sensor_type`` is the sensor
    (``"PT100"`` for RTD, e.g. ``"KITS90"`` for a thermocouple).
    """
    return {
        "temperature": _get_sdm4065a().measure_temperature(probe, sensor_type)
    }


# --- configure / trigger / fetch ------------------------------------------


def sdm4065a_configure_resistance(
    range_ohm: Optional[float] = None, four_wire: bool = False
) -> dict:
    """Set up a resistance measurement without triggering a reading.

    Use this with ``sdm4065a_read`` when you need settings to persist
    across readings — a null, or a fixed range and NPLC. The one-shot
    ``sdm4065a_measure_resistance`` reconfigures every call and so cannot
    hold a null.

    Resets this function's parameters to defaults (NPLC 10, null off), so
    call ``sdm4065a_set_nplc`` and ``sdm4065a_null_now`` *after* this.
    """
    dmm = _get_sdm4065a()
    dmm.configure_resistance(range_ohm, four_wire=four_wire)
    function = "FRESistance" if four_wire else "RESistance"
    return {
        "function": dmm.get_function(),
        "range_ohm": dmm.get_range(function=function),
        "nplc": dmm.get_nplc(function=function),
    }


def sdm4065a_configure_dc_voltage(range_V: Optional[float] = None) -> dict:
    """Set up a DC voltage measurement without triggering a reading."""
    dmm = _get_sdm4065a()
    dmm.configure_dc_voltage(range_V)
    return {"function": dmm.get_function()}


def sdm4065a_get_configuration() -> dict:
    """``CONF?`` — the present function, range and resolution as a string."""
    return {"configuration": _get_sdm4065a().get_configuration()}


def sdm4065a_set_sample_count(count: int) -> dict:
    """Readings returned per trigger. ``sdm4065a_read`` returns them all.

    Raise the open timeout for large counts at high NPLC — the whole burst
    must complete inside one VISA timeout.
    """
    dmm = _get_sdm4065a()
    dmm.set_sample_count(count)
    return {"sample_count": dmm.get_sample_count()}


def sdm4065a_get_sample_count() -> dict:
    """The configured readings-per-trigger."""
    return {"sample_count": _get_sdm4065a().get_sample_count()}


def sdm4065a_read() -> dict:
    """``READ?`` — trigger and return the reading(s), preserving configuration.

    Always returns a list, one entry per ``sample_count``. Unlike the
    ``sdm4065a_measure_*`` tools this does *not* reconfigure, so range,
    NPLC and any null stay in effect. This is the tool to use after
    ``sdm4065a_null_now``.
    """
    return {"readings": _get_sdm4065a().read()}


def sdm4065a_read_nulled() -> dict:
    """One nulled reading. Errors if no null is active, rather than
    silently returning an un-nulled number."""
    return {"reading": _get_sdm4065a().read_nulled()}


def sdm4065a_initiate() -> dict:
    """``INIT`` — start an acquisition into instrument memory."""
    _get_sdm4065a().initiate()
    return {"initiated": True}


def sdm4065a_fetch() -> dict:
    """``FETCh?`` — retrieve readings from a previous ``sdm4065a_initiate``."""
    return {"readings": _get_sdm4065a().fetch()}


def sdm4065a_abort() -> dict:
    """``ABORt`` — stop an acquisition in progress."""
    _get_sdm4065a().abort()
    return {"aborted": True}


def sdm4065a_clear_device_buffers() -> dict:
    """Try to unstick a USB connection after a timed-out read.

    Sends the USB-TMC ``INITIATE_CLEAR`` request. Worth trying when a read has
    timed out and every command since then fails, which on this instrument
    means the aborted reply is still stranded in its USB endpoints.

    ``cleared: false`` means it did not work. So, unfortunately, can
    ``cleared: true`` — on firmware 0.0.0.20 the instrument reported the clear
    successful while remaining unresponsive, and only a **front-panel power
    cycle** recovered it. If commands still fail after this, ask the operator
    to power-cycle the meter; there is nothing further to try remotely.

    Returns ``cleared: false`` on a LAN session, where the request does not
    apply.
    """
    return {"cleared": _get_sdm4065a().clear_device_buffers()}


# --- accuracy-affecting settings -----------------------------------------


def sdm4065a_set_nplc(nplc: float, function: str = "RESistance") -> dict:
    """Integration time in power-line cycles: 100, 10, 1, 0.1, 0.01 or 0.001.

    (The SDM4055A accepts only 10/1/0.01; those are not this model.)

    Higher is slower and quieter — 100 PLC is ~2 s per reading at 50 Hz and
    gives the lowest noise, 0.001 PLC is fast and noisy. Off-list values are
    rejected rather than silently coerced.
    """
    dmm = _get_sdm4065a()
    dmm.set_nplc(nplc, function=function)
    return {"nplc": dmm.get_nplc(function=function)}


def sdm4065a_get_nplc(function: str = "RESistance") -> dict:
    """The configured integration time in power-line cycles."""
    return {"nplc": _get_sdm4065a().get_nplc(function=function)}


def sdm4065a_set_range(range_value: float, function: str = "RESistance") -> dict:
    """Pin a function to a fixed range, disabling autorange.

    Worth doing deliberately: the instrument's default resistance range is
    2 kΩ, so a 100 Ω measurement lands there unless told otherwise, and the
    "% of range" accuracy term is then computed on 2 kΩ instead of 200 Ω.
    """
    dmm = _get_sdm4065a()
    dmm.set_range(range_value, function=function)
    return {"range": dmm.get_range(function=function)}


def sdm4065a_get_range(function: str = "RESistance") -> dict:
    """The active range for a function, and whether autorange picked it.

    ``autorange: true`` means ``range`` is just whatever the instrument
    happens to be on right now, and it will move with the input. Only with
    ``autorange: false`` is the range a fixed property of the configuration —
    which is what accuracy figures are quoted against, since the error budget
    has a "% of range" term.
    """
    dmm = _get_sdm4065a()
    return {
        "range": dmm.get_range(function=function),
        "autorange": dmm.get_autorange(function=function),
    }


def sdm4065a_get_autorange(function: str = "RESistance") -> dict:
    """Whether autoranging is on for a function."""
    return {"autorange": _get_sdm4065a().get_autorange(function=function)}


def sdm4065a_set_autorange(enable: bool, function: str = "RESistance") -> dict:
    """Enable or disable autoranging for a function."""
    dmm = _get_sdm4065a()
    dmm.set_autorange(enable, function=function)
    return {"autorange": enable, "range": dmm.get_range(function=function)}


def sdm4065a_set_autozero(enable: bool, function: str = "RESistance") -> dict:
    """Enable or disable autozero for a function.

    Autozero removes the instrument's own offset drift at roughly half the
    reading rate. For resistance it is SDM4065A-only and defaults **off**,
    so a low-resistance measurement is not autozeroed unless you ask.
    """
    dmm = _get_sdm4065a()
    dmm.set_autozero(enable, function=function)
    return {"autozero": dmm.get_autozero(function=function)}


def sdm4065a_get_autozero(function: str = "RESistance") -> dict:
    """Whether autozero is on for a function, read back from the instrument."""
    return {"autozero": _get_sdm4065a().get_autozero(function=function)}


# --- null / "Ref" ---------------------------------------------------------


def sdm4065a_null_now(function: str = "RESistance", samples: int = 1) -> dict:
    """Measure the present input and install it as the null offset.

    The 2-wire low-resistance recipe: configure the range, short the test
    leads across the measurement point, call this, restore the DUT, then
    read with ``sdm4065a_read``. Without a null, 2-wire resistance carries
    ~0.2 Ω of lead and contact error.

    ``samples`` averages that many readings first. The offset becomes a
    constant subtracted from every later reading, so noise captured here
    turns into systematic error that no downstream averaging removes —
    3 to 10 samples is cheap insurance.

    Returns the offset read back from the instrument, not the value sent,
    so the number really being subtracted is visible.
    """
    dmm = _get_sdm4065a()
    offset = dmm.null_now(function=function, samples=samples)
    return {
        "null_offset": offset,
        "null_enabled": dmm.get_null(function=function),
        "null_auto": dmm.get_null_auto(function=function),
    }


def sdm4065a_set_null(enable: bool, function: str = "RESistance") -> dict:
    """Enable or disable null ("Ref") mode for a function.

    Enabling null also arms automatic null-value selection, which makes the
    instrument overwrite the offset with its own next reading. Prefer
    ``sdm4065a_null_now``, which sequences this correctly.
    """
    dmm = _get_sdm4065a()
    dmm.set_null(enable, function=function)
    return {
        "null_enabled": dmm.get_null(function=function),
        "null_auto": dmm.get_null_auto(function=function),
    }


def sdm4065a_get_null(function: str = "RESistance") -> dict:
    """Null state, stored offset, and whether auto-selection is armed."""
    dmm = _get_sdm4065a()
    return {
        "null_enabled": dmm.get_null(function=function),
        "null_offset": dmm.get_null_value(function=function),
        "null_auto": dmm.get_null_auto(function=function),
    }


def sdm4065a_set_null_value(value: float, function: str = "RESistance") -> dict:
    """Set the null offset explicitly (±110 MΩ for resistance).

    Also disables automatic null-value selection.
    """
    dmm = _get_sdm4065a()
    dmm.set_null_value(value, function=function)
    return {"null_offset": dmm.get_null_value(function=function)}


def sdm4065a_set_null_auto(enable: bool, function: str = "RESistance") -> dict:
    """Arm automatic null-value selection: the next reading becomes the offset.

    Convenient interactively, wrong for a repeatable measurement — whatever
    is connected at that instant silently becomes the zero. Prefer
    ``sdm4065a_null_now``.
    """
    dmm = _get_sdm4065a()
    dmm.set_null_auto(enable, function=function)
    return {"null_auto": dmm.get_null_auto(function=function)}


# --- units and helpers ---------------------------------------------------


def sdm4065a_set_temperature_unit(unit: str) -> dict:
    """Set the temperature unit: ``"C"``, ``"F"`` or ``"K"``."""
    dmm = _get_sdm4065a()
    dmm.set_temperature_unit(unit)
    return {"unit": dmm.get_temperature_unit()}


def sdm4065a_get_temperature_unit() -> dict:
    """The configured temperature unit."""
    return {"unit": _get_sdm4065a().get_temperature_unit()}


def sdm4065a_reading_timeout_ms(
    nplc: float, samples: int = 1, mains_hz: float = 50.0
) -> dict:
    """A VISA timeout (ms) that comfortably covers ``samples`` at ``nplc``.

    Use before ``sdm4065a_open`` when planning a slow burst: 100 PLC × 10
    samples needs well over the 10 s default.
    """
    from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A

    return {
        "timeout_ms": SiglentSDM4065A.reading_timeout_ms(nplc, samples, mains_hz)
    }


def sdm4065a_write(command: str) -> dict:
    """Send a raw SCPI command that expects no response. Escape hatch."""
    _get_sdm4065a().write(command)
    return {"written": command}


def sdm4065a_query(command: str) -> dict:
    """Send a raw SCPI query and return the response string. Escape hatch."""
    return {"response": _get_sdm4065a().query(command)}


_TOOLS = (
    sdm4065a_open,
    sdm4065a_close,
    sdm4065a_info,
    sdm4065a_reset,
    sdm4065a_clear_status,
    sdm4065a_drain_errors,
    sdm4065a_self_test,
    sdm4065a_last_error,
    sdm4065a_command_error,
    sdm4065a_standard_event_status,
    sdm4065a_raise_if_error,
    sdm4065a_set_function,
    sdm4065a_get_function,
    sdm4065a_measure_dc_voltage,
    sdm4065a_measure_ac_voltage,
    sdm4065a_measure_dc_current,
    sdm4065a_measure_ac_current,
    sdm4065a_measure_resistance,
    sdm4065a_measure_resistance_4wire,
    sdm4065a_measure_capacitance,
    sdm4065a_measure_frequency,
    sdm4065a_measure_period,
    sdm4065a_measure_continuity,
    sdm4065a_measure_diode,
    sdm4065a_measure_temperature,
    sdm4065a_configure_resistance,
    sdm4065a_configure_dc_voltage,
    sdm4065a_get_configuration,
    sdm4065a_set_sample_count,
    sdm4065a_get_sample_count,
    sdm4065a_read,
    sdm4065a_read_nulled,
    sdm4065a_initiate,
    sdm4065a_fetch,
    sdm4065a_abort,
    sdm4065a_clear_device_buffers,
    sdm4065a_set_nplc,
    sdm4065a_get_nplc,
    sdm4065a_set_range,
    sdm4065a_get_range,
    sdm4065a_get_autorange,
    sdm4065a_set_autorange,
    sdm4065a_set_autozero,
    sdm4065a_get_autozero,
    sdm4065a_null_now,
    sdm4065a_set_null,
    sdm4065a_get_null,
    sdm4065a_set_null_value,
    sdm4065a_set_null_auto,
    sdm4065a_set_temperature_unit,
    sdm4065a_get_temperature_unit,
    sdm4065a_reading_timeout_ms,
    sdm4065a_write,
    sdm4065a_query,
)


def register_mcp_tools(mcp) -> None:
    """Register every SDM4065A MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

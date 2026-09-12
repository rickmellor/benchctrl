"""MCP tool surface for the Siglent SDG1032X function / arbitrary waveform generator.

Per the v1.0 driver-symmetric architecture, each driver owns its own MCP
tools and exposes them via :py:func:`register_mcp_tools`. The top-level
:py:mod:`benchctrl.mcp` orchestrator calls this function at startup to
register every SDG1032X tool on the shared :py:class:`FastMCP` server.

Connection state (``_sdg1032x``) lives in this module. Tests can mutate the
singleton via this module to inject fakes.

Two things shape the docstrings, which are what a model reads before calling:

- **There is no error queue.** The SDG silently ignores or quantises a value
  it cannot represent, so every setter here returns what the instrument
  *reads back* and fails with a verify error when that differs from what was
  asked. A tool that returns normally has been confirmed; ``verify=false``
  turns the failure into a logged warning and returns whatever the instrument
  chose.
- **Two tools energise a BNC.** ``sdg1032x_set_output`` switches a channel's
  output on; ``sdg1032x_write_arb`` uploads a waveform with an amplitude. Both
  say so. ``allowed_channels`` and ``max_amplitude_vpp`` are fixed at
  ``sdg1032x_open`` and enforced by the driver regardless of what a model
  believes; ``sdg1032x_disable_outputs`` is never gated.

Binary results (``sdg1032x_read_screen``, ``sdg1032x_read_arb``) never return
the bytes — an image in a tool result lands in the transcript. They take
``save_to`` and return the path, size and SHA-256 instead.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, Union

log = logging.getLogger("benchctrl.drivers.siglent_sdg1032x.mcp_tools")

# Kept untyped to avoid eagerly importing pyvisa (the actual class is
# SiglentSDG1032X — pulled in lazily inside sdg1032x_open).
_sdg1032x = None
_sdg1032x_lock = threading.RLock()


def _get_sdg():
    from benchctrl.drivers.siglent_sdg1032x.driver import SDG1032XConnectionError

    # Take the lock: sdg1032x_open/sdg1032x_close mutate this global from
    # other threads, and reading it unguarded would be a race.
    with _sdg1032x_lock:
        if _sdg1032x is None:
            raise SDG1032XConnectionError("SDG1032X not open — call sdg1032x_open() first.")
        return _sdg1032x


def _bytes_result(data: bytes, save_to: Optional[str]) -> dict:
    out: dict = {"bytes": len(data), "sha256": hashlib.sha256(data).hexdigest(), "path": None}
    if save_to:
        path = Path(save_to).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        out["path"] = str(path)
    return out


# ---------------------------------------------------------------- lifecycle


def sdg1032x_open(
    resource: Optional[str] = None,
    allowed_channels: Sequence[int] = (1, 2),
    max_amplitude_vpp: Optional[float] = None,
    timeout_ms: int = 5000,
) -> dict:
    """Open a VISA session to a Siglent SDG1032X waveform generator. Energises
    nothing. ``resource`` unset auto-discovers the one SDG1000X on USB (VID/PID
    ``F4EC:1103``); pass a VISA resource string to target a specific unit.
    ``allowed_channels`` (default both) is the set of channels this session may
    *mutate* — reads are never gated — and is fixed for the life of the
    session: if a later call needs a channel not listed, ask the operator
    rather than reopening with a wider list. ``max_amplitude_vpp`` caps every
    amplitude this driver sends (the instrument's own ``MAX_OUTPUT_AMP`` is
    accepted but not enforced by the bench firmware, so the cap is the
    driver's check); it can be lowered later with
    ``sdg1032x_set_max_amplitude`` but never raised. Opening changes nothing
    on the instrument. Returns the identity, the resource string and the
    policy in force.
    """
    global _sdg1032x
    from benchctrl import session
    from benchctrl.drivers.siglent_sdg1032x import SiglentSDG1032X

    with _sdg1032x_lock:
        if _sdg1032x is not None:
            return {
                "error": "SDG1032X already open",
                "guidance": "Call sdg1032x_close() before reopening.",
                "current_resource": _sdg1032x.resource,
            }
        _sdg1032x = session.resolve(
            "siglent_sdg1032x",
            opener=SiglentSDG1032X.open,
            open_kwargs={
                "resource": resource,
                "allowed_channels": tuple(allowed_channels),
                "max_amplitude_vpp": max_amplitude_vpp,
                "timeout_ms": timeout_ms,
            },
        )
    gen = _sdg1032x
    info = gen.info()
    return {
        "resource": info.resource,
        "info": info.to_dict(),
        "allowed_channels": list(gen.allowed_channels),
        "max_amplitude_vpp": gen.max_amplitude_vpp,
    }


def sdg1032x_close() -> dict:
    """Close the SDG1032X session. Best-effort switches both outputs OFF first
    (``sdg1032x_disable_outputs``) so nothing is left driving a DUT after the
    session is gone. SAFETY: a failure to disarm during teardown is reported
    as ``outputs_off_failed`` in the result — verify the DUT side if you see
    it. Returns ``{"closed": true}``.
    """
    global _sdg1032x
    with _sdg1032x_lock:
        if _sdg1032x is None:
            return {"closed": False, "note": "no SDG1032X was open"}
        failure: Optional[str] = None
        try:
            _sdg1032x.disable_outputs()
        except Exception as e:  # noqa: BLE001 - best effort on the way out
            failure = f"{type(e).__name__}: {e}"
            log.warning("SDG1032X disable_outputs() failed during close: %s", e)
        _sdg1032x.close()
        _sdg1032x = None
    result: dict = {"closed": True}
    if failure:
        result["outputs_off_failed"] = failure
    return result


def sdg1032x_info() -> dict:
    """Identity from ``*IDN?`` — manufacturer, model, serial, firmware — plus
    the VISA resource string. Cached after the first call."""
    return _get_sdg().info().to_dict()


def sdg1032x_reset() -> dict:
    """``*RST`` — factory defaults: both outputs OFF, load HiZ, 1 kHz 4 Vpp
    sine on each channel. Nothing is read back; do any configuration *after*
    this, never before."""
    _get_sdg().reset()
    return {"ok": True, "reset": True}


def sdg1032x_operation_complete() -> dict:
    """``*OPC?`` — true once the previous command has been processed. Use it
    to wait out a slow command (a waveform upload) before the next step."""
    return {"complete": _get_sdg().operation_complete()}


# ---------------------------------------------------------------- transport


def sdg1032x_write(command: str) -> dict:
    """Send a raw SCPI command, e.g. ``C1:BSWV FRQ,1000``. Escape hatch: nothing
    comes back and **nothing is verified** — the SDG has no error queue, so a
    value it cannot take is silently ignored and this tool still returns ok.
    Prefer the typed ``sdg1032x_set_*`` tools, which read back and fail on a
    mismatch. SAFETY: the ``max_amplitude_vpp`` cap is a driver-side check on
    the typed setters and does **not** apply to a raw write."""
    _get_sdg().write(command)
    return {"ok": True, "command": command}


def sdg1032x_query(command: str) -> dict:
    """Send a raw SCPI query, e.g. ``C1:BSWV?``, and return the trimmed answer.
    Escape hatch. A header this firmware does not implement (``CURRPRT?``,
    ``VOLTSTAT?``) goes **unanswered** and the call fails with a timeout
    error rather than a reply; unlike the SDM4065A the instrument recovers on
    the next query."""
    return {"response": _get_sdg().query(command)}


# ---------------------------------------------------------------- output


def sdg1032x_get_output(channel: int) -> dict:
    """A channel's output state from ``Cn:OUTP?``: ``enabled``, ``load_ohm``
    (null = high impedance) and ``inverted``. Read-only; never gated."""
    return _get_sdg().get_output(channel).to_dict()


def sdg1032x_set_output(channel: int, on: bool, verify: bool = True) -> dict:
    """**Switch a channel's output on or off.** SAFETY: ``on=true`` energises
    the BNC with whatever waveform, amplitude and offset are configured — know
    what is attached first; the amplitude cap from ``sdg1032x_open`` and the
    load setting decide how much voltage appears. Only channels in
    ``allowed_channels`` can be switched. Returns the state the instrument
    reads back and fails with a verify error if it did not move."""
    return _get_sdg().set_output(channel, on, verify=verify).to_dict()


def sdg1032x_set_output_load(
    channel: int, load_ohm: Optional[float] = None, verify: bool = True
) -> dict:
    """Set the channel's expected load: ``null`` = high impedance, otherwise
    50–100000 Ω. Amplitude limits halve into 50 Ω, so set the load *before*
    the amplitude. Returns the read-back output state; fails with a verify
    error when the instrument kept a different load."""
    return _get_sdg().set_output_load(channel, load_ohm, verify=verify).to_dict()


def sdg1032x_set_output_polarity(channel: int, inverted: bool, verify: bool = True) -> dict:
    """Set the channel's output polarity (``PLRT``): ``inverted=true`` flips the
    waveform. Returns the read-back output state; fails with a verify error
    on a mismatch."""
    return _get_sdg().set_output_polarity(channel, inverted, verify=verify).to_dict()


def sdg1032x_disable_outputs() -> dict:
    """Switch **both** channels' outputs OFF and read each back — the safe-stop.
    Takes no arguments and ignores ``allowed_channels`` on purpose: disarming
    is never policy-gated. Every channel is attempted even if an earlier one
    failed; a channel still ON afterwards raises a verify error. Returns the
    read-back state per channel, keyed by channel number as a string."""
    states = _get_sdg().disable_outputs()
    return {str(ch): st.to_dict() for ch, st in sorted(states.items())}


# ---------------------------------------------------------------- basic wave


def sdg1032x_get_basic_wave(channel: int) -> dict:
    """The channel's basic-wave parameters from ``Cn:BSWV?``: wave type,
    frequency, period, amplitude (Vpp, Vrms, dBm), offset, high/low level,
    phase, duty, symmetry, pulse width/rise/fall/delay, noise stdev/mean and
    the instrument's amplitude cap. A field the current waveform does not
    report is null (NOISE has no frequency, DC no amplitude)."""
    return _get_sdg().get_basic_wave(channel).to_dict()


def sdg1032x_set_basic_wave(
    channel: int,
    wave_type: Optional[str] = None,
    frequency_hz: Optional[float] = None,
    period_s: Optional[float] = None,
    amplitude_vpp: Optional[float] = None,
    amplitude_vrms: Optional[float] = None,
    amplitude_dbm: Optional[float] = None,
    offset_v: Optional[float] = None,
    high_level_v: Optional[float] = None,
    low_level_v: Optional[float] = None,
    phase_deg: Optional[float] = None,
    duty_pct: Optional[float] = None,
    symmetry_pct: Optional[float] = None,
    pulse_width_s: Optional[float] = None,
    rise_s: Optional[float] = None,
    fall_s: Optional[float] = None,
    delay_s: Optional[float] = None,
    noise_stdev_v: Optional[float] = None,
    noise_mean_v: Optional[float] = None,
    verify: bool = True,
) -> dict:
    """Set any subset of the channel's basic-wave parameters in one command
    (``wave_type`` one of SINE, SQUARE, RAMP, PULSE, NOISE, ARB, DC; it is
    applied first so the other fields apply to it). Fields left null are
    untouched. Does not switch the output on. Returns the full read-back and
    fails with a verify error when any requested field differs beyond the
    instrument's resolution — a frequency above the waveform's ceiling, a
    duty the frequency cannot support, or an amplitude the load or the
    ``max_amplitude_vpp`` cap cannot deliver is refused silently by the
    instrument and surfaces only here."""
    return (
        _get_sdg()
        .set_basic_wave(
            channel,
            wave_type=wave_type,
            frequency_hz=frequency_hz,
            period_s=period_s,
            amplitude_vpp=amplitude_vpp,
            amplitude_vrms=amplitude_vrms,
            amplitude_dbm=amplitude_dbm,
            offset_v=offset_v,
            high_level_v=high_level_v,
            low_level_v=low_level_v,
            phase_deg=phase_deg,
            duty_pct=duty_pct,
            symmetry_pct=symmetry_pct,
            pulse_width_s=pulse_width_s,
            rise_s=rise_s,
            fall_s=fall_s,
            delay_s=delay_s,
            noise_stdev_v=noise_stdev_v,
            noise_mean_v=noise_mean_v,
            verify=verify,
        )
        .to_dict()
    )


def sdg1032x_set_wave_type(channel: int, wave_type: str, verify: bool = True) -> dict:
    """Select the channel's waveform: SINE, SQUARE, RAMP, PULSE, NOISE, ARB or
    DC. Other parameters keep their values where the new waveform supports
    them. Returns the read-back basic wave; fails with a verify error if the
    instrument refused (e.g. SQUARE while combining is on)."""
    return _get_sdg().set_wave_type(channel, wave_type, verify=verify).to_dict()


def sdg1032x_set_frequency(channel: int, frequency_hz: float, verify: bool = True) -> dict:
    """Set the channel's frequency in Hz (ceiling depends on the waveform:
    30 MHz sine/square, 12.5 MHz pulse, 6 MHz arb, 500 kHz ramp). Returns the
    read-back basic wave; fails with a verify error if the instrument
    quantised or refused the value."""
    return _get_sdg().set_frequency(channel, frequency_hz, verify=verify).to_dict()


def sdg1032x_set_amplitude(channel: int, amplitude_vpp: float, verify: bool = True) -> dict:
    """Set the channel's amplitude in Vpp. SAFETY: this is the voltage that
    appears on the BNC once the output is on; the session's
    ``max_amplitude_vpp`` cap is enforced here (driver-side) and a value
    above the load's limit (10 Vpp into 50 Ω, 20 Vpp HiZ) is refused. Returns the read-back basic wave;
    fails with a verify error on a mismatch."""
    return _get_sdg().set_amplitude(channel, amplitude_vpp, verify=verify).to_dict()


def sdg1032x_set_offset(channel: int, offset_v: float, verify: bool = True) -> dict:
    """Set the channel's DC offset in volts (±10 V; amplitude/2 + |offset|
    must fit the output range). Returns the read-back basic wave; fails with
    a verify error on a mismatch."""
    return _get_sdg().set_offset(channel, offset_v, verify=verify).to_dict()


def sdg1032x_set_phase(channel: int, phase_deg: float, verify: bool = True) -> dict:
    """Set the channel's phase in degrees (0–360). Returns the read-back basic
    wave; fails with a verify error on a mismatch."""
    return _get_sdg().set_phase(channel, phase_deg, verify=verify).to_dict()


def sdg1032x_set_max_amplitude(amplitude_vpp: float) -> dict:
    """Tighten this session's amplitude cap (``max_amplitude_vpp``, Vpp) at
    run time — every later ``sdg1032x_set_amplitude`` / ``set_basic_wave`` /
    ``write_arb`` above it is refused. The cap can only be *lowered* here;
    raising it is an operator decision made at ``sdg1032x_open`` (ask rather
    than reopening). Nothing is sent to the instrument: the bench firmware
    accepts its own ``MAX_OUTPUT_AMP`` but neither echoes nor enforces it, so
    the driver's check is the cap that holds. Returns the cap in force."""
    return {"max_amplitude_vpp": _get_sdg().set_max_amplitude(amplitude_vpp)}


# ---------------------------------------------------------------- arbitrary waveforms


def sdg1032x_list_arbs(kind: str = "builtin") -> dict:
    """List arbitrary waveforms: ``kind="builtin"`` returns index + name pairs
    (select by *index*), ``kind="user"`` the names stored on the instrument
    (select by *name*). Read-only."""
    arbs = _get_sdg().list_arbs(kind)
    return {"kind": kind, "count": len(arbs), "arbs": [a.to_dict() for a in arbs]}


def sdg1032x_get_arb(channel: int) -> dict:
    """Which arbitrary waveform the channel has selected (``Cn:ARWV?``): index
    and name. Read-only."""
    return _get_sdg().get_arb(channel).to_dict()


def sdg1032x_select_arb(
    channel: int,
    index: Optional[int] = None,
    name: Optional[str] = None,
    verify: bool = True,
) -> dict:
    """Pick the channel's arbitrary waveform: a built-in by ``index`` (2–198)
    or a user waveform by ``name`` — exactly one of the two. Does not change
    the wave type; use ``sdg1032x_set_wave_type(channel, "ARB")`` to play it.
    Returns the read-back selection; fails with a verify error if the
    instrument kept a different one (e.g. an unknown name)."""
    return _get_sdg().select_arb(channel, index=index, name=name, verify=verify).to_dict()


def sdg1032x_write_arb(
    name: str,
    samples: Union[list[int], list[float]],
    frequency_hz: float = 1000.0,
    amplitude_vpp: float = 1.0,
    offset_v: float = 0.0,
    phase_deg: float = 0.0,
    verify: bool = True,
) -> dict:
    """Upload a user waveform (``WVDT``) to the instrument's memory under
    ``name`` (letters, digits, underscore; up to 24 chars). ``samples`` are
    either all int16 codes (-32768..32767) or all floats in [-1, 1] scaled to
    full range; 2..16384 of them. SAFETY: ``amplitude_vpp`` (default 1.0) is
    the amplitude the waveform plays at when selected on a channel whose
    output is on; the session's ``max_amplitude_vpp`` cap is enforced. The
    upload is verified by reading the waveform back and comparing the codes
    (a verify error means the instrument resampled or rejected it). Returns
    the read-back metadata and sample count, never the samples."""
    got = _get_sdg().write_arb(
        name,
        samples,
        frequency_hz=frequency_hz,
        amplitude_vpp=amplitude_vpp,
        offset_v=offset_v,
        phase_deg=phase_deg,
        verify=verify,
    )
    out = got.to_dict()
    out["sha256"] = hashlib.sha256(got.codes).hexdigest()
    return out


def sdg1032x_read_arb(name: str, save_to: Optional[str] = None) -> dict:
    """Read a user waveform back from the instrument (``WVDT? USER,<name>``):
    its frequency, amplitude, offset, phase, sample count and byte count, plus
    the SHA-256 of the raw int16 little-endian codes. Pass ``save_to`` to
    write the raw codes host-side and get the ``path`` back; the result never
    carries the sample data. An unknown name goes unanswered (timeout)."""
    got = _get_sdg().read_arb(name)
    out = got.to_dict()
    out.update(_bytes_result(got.codes, save_to))
    return out


# ---------------------------------------------------------------- modulation


def sdg1032x_get_modulation(channel: int) -> dict:
    """The channel's modulation state from ``Cn:MDWV?``: enabled, type (AM,
    DSBAM, FM, PM, PWM, ASK, FSK, PSK), source, shape, modulating frequency,
    depth, deviation, key/hop frequency, polarity and the carrier's basic-wave
    parameters. Off, only ``enabled`` is populated."""
    return _get_sdg().get_modulation(channel).to_dict()


def sdg1032x_set_modulation(
    channel: int,
    enabled: Optional[bool] = None,
    type: Optional[str] = None,  # noqa: A002 - matches the driver/dataclass field
    source: Optional[str] = None,
    shape: Optional[str] = None,
    frequency_hz: Optional[float] = None,
    depth_pct: Optional[float] = None,
    deviation: Optional[float] = None,
    key_frequency_hz: Optional[float] = None,
    hop_frequency_hz: Optional[float] = None,
    polarity: Optional[str] = None,
    verify: bool = True,
) -> dict:
    """Configure modulation on the channel. ``enabled`` is sent first (the
    instrument only takes the other fields while ON), then ``type`` (AM,
    DSBAM, FM, PM, PWM, ASK, FSK, PSK), then that type's parameters:
    ``source`` INT/EXT/CH1/CH2, ``shape`` SINE/SQUARE/TRIANGLE/UPRAMP/DNRAMP/
    NOISE/ARB, ``frequency_hz`` of the modulator, ``depth_pct`` (AM),
    ``deviation`` (Hz for FM, degrees for PM, % for PWM), ``key_frequency_hz``
    and ``hop_frequency_hz`` (ASK/FSK/PSK), ``polarity``. Enabling modulation
    turns sweep and burst off. Returns the read-back; fails with a verify
    error when a requested field differs."""
    return (
        _get_sdg()
        .set_modulation(
            channel,
            enabled=enabled,
            type=type,
            source=source,
            shape=shape,
            frequency_hz=frequency_hz,
            depth_pct=depth_pct,
            deviation=deviation,
            key_frequency_hz=key_frequency_hz,
            hop_frequency_hz=hop_frequency_hz,
            polarity=polarity,
            verify=verify,
        )
        .to_dict()
    )


# ---------------------------------------------------------------- sweep


def sdg1032x_get_sweep(channel: int) -> dict:
    """The channel's frequency-sweep state from ``Cn:SWWV?``: enabled, sweep
    time, start/stop/center/span, mode (LINE/LOG/STEP), direction, trigger
    source, marker, and the carrier's basic-wave parameters. Off, only
    ``enabled`` is populated."""
    return _get_sdg().get_sweep(channel).to_dict()


def sdg1032x_set_sweep(
    channel: int,
    enabled: Optional[bool] = None,
    time_s: Optional[float] = None,
    start_hold_s: Optional[float] = None,
    end_hold_s: Optional[float] = None,
    return_s: Optional[float] = None,
    start_hz: Optional[float] = None,
    stop_hz: Optional[float] = None,
    center_hz: Optional[float] = None,
    span_hz: Optional[float] = None,
    mode: Optional[str] = None,
    direction: Optional[str] = None,
    symmetry_pct: Optional[float] = None,
    trigger_source: Optional[str] = None,
    trigger_out: Optional[bool] = None,
    trigger_edge: Optional[str] = None,
    mark_enabled: Optional[bool] = None,
    mark_hz: Optional[float] = None,
    verify: bool = True,
) -> dict:
    """Configure a frequency sweep on the channel. ``enabled`` is sent first
    (the instrument only takes the other fields while ON). Give either
    ``start_hz``/``stop_hz`` or ``center_hz``/``span_hz``; ``mode`` is LINE,
    LOG or STEP, ``direction`` UP, DOWN or UP_DOWN, ``trigger_source`` INT,
    EXT or MAN (then ``sdg1032x_trigger_sweep`` fires it), ``trigger_edge``
    RISE/FALL. Enabling a sweep turns modulation and burst off. Returns the
    read-back; fails with a verify error when a requested field differs."""
    return (
        _get_sdg()
        .set_sweep(
            channel,
            enabled=enabled,
            time_s=time_s,
            start_hold_s=start_hold_s,
            end_hold_s=end_hold_s,
            return_s=return_s,
            start_hz=start_hz,
            stop_hz=stop_hz,
            center_hz=center_hz,
            span_hz=span_hz,
            mode=mode,
            direction=direction,
            symmetry_pct=symmetry_pct,
            trigger_source=trigger_source,
            trigger_out=trigger_out,
            trigger_edge=trigger_edge,
            mark_enabled=mark_enabled,
            mark_hz=mark_hz,
            verify=verify,
        )
        .to_dict()
    )


def sdg1032x_trigger_sweep(channel: int) -> dict:
    """Fire one manual sweep trigger (``MTRIG``) on the channel; only
    meaningful with ``trigger_source="MAN"``. Nothing to read back."""
    _get_sdg().trigger_sweep(channel)
    return {"ok": True, "channel": channel, "triggered": "sweep"}


# ---------------------------------------------------------------- burst


def sdg1032x_get_burst(channel: int) -> dict:
    """The channel's burst state from ``Cn:BTWV?``: enabled, period, start
    phase, mode (GATE/NCYC), trigger source/out/edge, delay, polarity,
    ``cycles`` (null = infinite) and the carrier's basic-wave parameters.
    Off, only ``enabled`` is populated."""
    return _get_sdg().get_burst(channel).to_dict()


def sdg1032x_set_burst(
    channel: int,
    enabled: Optional[bool] = None,
    period_s: Optional[float] = None,
    start_phase_deg: Optional[float] = None,
    mode: Optional[str] = None,
    trigger_source: Optional[str] = None,
    trigger_out: Optional[str] = None,
    trigger_edge: Optional[str] = None,
    delay_s: Optional[float] = None,
    polarity: Optional[str] = None,
    cycles: Optional[Union[int, str]] = None,
    verify: bool = True,
) -> dict:
    """Configure burst on the channel. ``enabled`` is sent first (the
    instrument only takes the other fields while ON). ``mode`` is GATE or
    NCYC; ``cycles`` an integer ≥ 1 or ``"INF"`` (valid on its own); ``trigger_source`` INT, EXT
    or MAN (then ``sdg1032x_trigger_burst`` fires it); ``trigger_out`` RISE,
    FALL or OFF; ``trigger_edge`` RISE/FALL; ``polarity`` POS/NEG. Enabling
    burst turns modulation and sweep off. Returns the read-back; fails with a
    verify error when a requested field differs."""
    return (
        _get_sdg()
        .set_burst(
            channel,
            enabled=enabled,
            period_s=period_s,
            start_phase_deg=start_phase_deg,
            mode=mode,
            trigger_source=trigger_source,
            trigger_out=trigger_out,
            trigger_edge=trigger_edge,
            delay_s=delay_s,
            polarity=polarity,
            cycles=cycles,
            verify=verify,
        )
        .to_dict()
    )


def sdg1032x_trigger_burst(channel: int) -> dict:
    """Fire one manual burst trigger (``MTRIG``) on the channel; only
    meaningful with ``trigger_source="MAN"``. Nothing to read back."""
    _get_sdg().trigger_burst(channel)
    return {"ok": True, "channel": channel, "triggered": "burst"}


# ---------------------------------------------------------------- sync / clock / phase / channel


def sdg1032x_get_sync(channel: int) -> dict:
    """The channel's rear-panel sync output from ``Cn:SYNC?``: enabled and
    type (CH1, CH2, MOD_CH1, MOD_CH2). Read-only."""
    return _get_sdg().get_sync(channel).to_dict()


def sdg1032x_set_sync(
    channel: int,
    on: bool,
    type: Optional[str] = None,
    verify: bool = True,  # noqa: A002
) -> dict:
    """Switch the channel's sync output and optionally its ``type`` (CH1, CH2,
    MOD_CH1, MOD_CH2). Returns the read-back; ``enabled`` fails with a
    verify error on a mismatch, while ``type`` is verified only when the
    firmware echoes it (the bench unit answers ``C1:SYNC ON`` alone)."""
    return _get_sdg().set_sync(channel, on, type=type, verify=verify).to_dict()


def sdg1032x_get_clock() -> dict:
    """Reference clock configuration from ``ROSC?``: ``source`` INT or EXT and
    ``output_10m`` — null on the bench firmware, which omits the ``10MOUT``
    field from its read-back."""
    return _get_sdg().get_clock().to_dict()


def sdg1032x_set_clock(
    source: Optional[str] = None, output_10m: Optional[bool] = None, verify: bool = True
) -> dict:
    """Set the reference clock ``source`` (INT/EXT) and/or the rear 10 MHz
    ``output_10m``. Returns the read-back; the source fails with a verify
    error on a mismatch, but ``output_10m`` is verified only when the
    firmware echoes it (the bench unit does not)."""
    return _get_sdg().set_clock(source=source, output_10m=output_10m, verify=verify).to_dict()


def sdg1032x_get_phase_mode() -> dict:
    """The two-channel phase mode from ``MODE?``: ``PHASELOCKED`` or
    ``INDEPENDENT`` (the instrument spells the read-back ``PHASE-LOCKED``;
    normalised here)."""
    return {"phase_mode": _get_sdg().get_phase_mode()}


def sdg1032x_set_phase_mode(mode: str, verify: bool = True) -> dict:
    """Set the two-channel phase mode: ``PHASELOCKED`` (channels share a
    phase reference) or ``INDEPENDENT``. The driver sends the hyphenated
    ``PHASE-LOCKED`` the firmware actually takes and normalises the read-back.
    Returns the read-back; fails with a verify error on a mismatch."""
    return {"phase_mode": _get_sdg().set_phase_mode(mode, verify=verify)}


def sdg1032x_apply_equal_phase() -> dict:
    """``EQPHASE`` — align the two channels' phases now. Nothing to read
    back."""
    _get_sdg().apply_equal_phase()
    return {"ok": True, "applied": "equal_phase"}


def sdg1032x_get_invert(channel: int) -> dict:
    """Whether the channel's waveform is inverted (``Cn:INVT?``). Read-only."""
    return {"channel": channel, "invert": _get_sdg().get_invert(channel)}


def sdg1032x_set_invert(channel: int, on: bool, verify: bool = True) -> dict:
    """Invert (or un-invert) the channel's waveform (``Cn:INVT``). Returns the
    read-back; fails with a verify error on a mismatch."""
    return {"channel": channel, "invert": _get_sdg().set_invert(channel, on, verify=verify)}


def sdg1032x_apply_channel_copy(source: int, target: int, verify: bool = True) -> dict:
    """``PACP`` — copy the ``source`` channel's parameters onto ``target``
    (which must be in ``allowed_channels``). Verified by comparing the
    target's basic wave with the source's; returns the target's read-back and
    fails with a verify error if any field differs."""
    return _get_sdg().apply_channel_copy(source, target, verify=verify).to_dict()


# ---------------------------------------------------------------- coupling / harmonics / combine


def sdg1032x_get_coupling() -> dict:
    """Channel coupling/tracking from ``COUP?``: ``trace`` plus per-axis
    frequency/phase/amplitude coupled flags and their deviation or ratio.
    Read-only."""
    return _get_sdg().get_coupling().to_dict()


def sdg1032x_set_coupling(
    trace: Optional[bool] = None,
    freq_coupled: Optional[bool] = None,
    freq_deviation_hz: Optional[float] = None,
    freq_ratio: Optional[float] = None,
    phase_coupled: Optional[bool] = None,
    phase_deviation_deg: Optional[float] = None,
    phase_ratio: Optional[float] = None,
    amp_coupled: Optional[bool] = None,
    amp_ratio: Optional[float] = None,
    amp_deviation_v: Optional[float] = None,
    verify: bool = True,
) -> dict:
    """Configure channel coupling/tracking: ``trace`` mirrors CH1 onto CH2;
    the ``*_coupled`` flags couple one axis with either a deviation or a ratio
    (mutually exclusive per axis in the instrument — the read-back shows which
    one it kept). Returns the read-back; fails with a verify error when a
    requested field differs."""
    return (
        _get_sdg()
        .set_coupling(
            trace=trace,
            freq_coupled=freq_coupled,
            freq_deviation_hz=freq_deviation_hz,
            freq_ratio=freq_ratio,
            phase_coupled=phase_coupled,
            phase_deviation_deg=phase_deviation_deg,
            phase_ratio=phase_ratio,
            amp_coupled=amp_coupled,
            amp_ratio=amp_ratio,
            amp_deviation_v=amp_deviation_v,
            verify=verify,
        )
        .to_dict()
    )


def sdg1032x_get_harmonics(channel: int) -> dict:
    """The channel's harmonic generator from ``Cn:HARM?``: enabled, type
    (EVEN/ODD/ALL), the selected order and its amplitude (V and dBc) and
    phase. Read-only."""
    return _get_sdg().get_harmonics(channel).to_dict()


def sdg1032x_set_harmonics(
    channel: int,
    enabled: Optional[bool] = None,
    type: Optional[str] = None,  # noqa: A002 - matches the driver/dataclass field
    order: Optional[int] = None,
    amplitude_v: Optional[float] = None,
    amplitude_dbc: Optional[float] = None,
    phase_deg: Optional[float] = None,
    verify: bool = True,
) -> dict:
    """Configure the harmonic generator (sine only): ``type`` EVEN/ODD/ALL,
    ``order`` selects which harmonic ``amplitude_v`` *or* ``amplitude_dbc``
    (not both) and ``phase_deg`` apply to. Returns the read-back; fails with
    a verify error when a requested field differs."""
    return (
        _get_sdg()
        .set_harmonics(
            channel,
            enabled=enabled,
            type=type,
            order=order,
            amplitude_v=amplitude_v,
            amplitude_dbc=amplitude_dbc,
            phase_deg=phase_deg,
            verify=verify,
        )
        .to_dict()
    )


def sdg1032x_get_combine(channel: int) -> dict:
    """Whether the channel is combining its waveform with the other channel's
    (``Cn:CMBN?``). Read-only."""
    return {"channel": channel, "combine": _get_sdg().get_combine(channel)}


def sdg1032x_set_combine(channel: int, on: bool, verify: bool = True) -> dict:
    """Switch waveform combining (this channel + the other) on or off. With it
    on the SDG1000X refuses SQUARE as the wave type. Returns the read-back;
    fails with a verify error on a mismatch."""
    return {"channel": channel, "combine": _get_sdg().set_combine(channel, on, verify=verify)}


# ---------------------------------------------------------------- frequency counter


def sdg1032x_read_counter() -> dict:
    """The rear-panel frequency counter (``FCNT?``): enabled, measured
    frequency, duty, reference frequency, trigger level, positive/negative
    pulse width, deviation in ppm, coupling and HF-reject. Off, only
    ``enabled`` is populated."""
    return _get_sdg().read_counter().to_dict()


def sdg1032x_set_counter(
    enabled: Optional[bool] = None,
    reference_hz: Optional[float] = None,
    trigger_level_v: Optional[float] = None,
    coupling: Optional[str] = None,
    high_frequency_reject: Optional[bool] = None,
    verify: bool = True,
) -> dict:
    """Configure the frequency counter: ``enabled``, ``reference_hz``,
    ``trigger_level_v``, ``coupling`` AC/DC, ``high_frequency_reject``.
    Returns the read-back; fails with a verify error on a mismatch, except
    that fields other than ``enabled`` are verified only while the counter is
    ON (it reports nothing else while off)."""
    return (
        _get_sdg()
        .set_counter(
            enabled=enabled,
            reference_hz=reference_hz,
            trigger_level_v=trigger_level_v,
            coupling=coupling,
            high_frequency_reject=high_frequency_reject,
            verify=verify,
        )
        .to_dict()
    )


# ---------------------------------------------------------------- protection


def sdg1032x_get_protection() -> dict:
    """Over-voltage protection state (``VOLTPRT?``). Over-current protection
    is not implemented on this firmware (``CURRPRT?`` goes unanswered), so it
    is not exposed."""
    return _get_sdg().get_protection().to_dict()


def sdg1032x_set_protection(over_voltage: bool, verify: bool = True) -> dict:
    """Switch over-voltage protection on or off. Leave it on unless a test
    needs otherwise. Returns the read-back; fails with a verify error on a
    mismatch."""
    return _get_sdg().set_protection(over_voltage=over_voltage, verify=verify).to_dict()


# ---------------------------------------------------------------- system / screen / keys


def sdg1032x_read_screen(save_to: Optional[str] = None) -> dict:
    """Capture the instrument's own screen (``SCDP``) as a 480x272 24-bit BMP
    (~392 KB, ~450 ms). Pass ``save_to`` to write the file host-side and get
    the ``path`` back; the result carries only ``bytes``, ``sha256`` and the
    path, never the image. Use it as an independent read-back of what the
    instrument thinks it is doing."""
    return _bytes_result(_get_sdg().read_screen(), save_to)


def sdg1032x_get_buzzer() -> dict:
    """Whether the key-press buzzer is on (``BUZZ?``). Read-only."""
    return {"buzzer": _get_sdg().get_buzzer()}


def sdg1032x_set_buzzer(on: bool, verify: bool = True) -> dict:
    """Switch the key-press buzzer on or off. Returns the read-back; fails
    with a verify error on a mismatch."""
    return {"buzzer": _get_sdg().set_buzzer(on, verify=verify)}


def sdg1032x_get_screen_saver() -> dict:
    """Minutes until the screen saver kicks in, 0 = off (``SCSV?``)."""
    return {"minutes": _get_sdg().get_screen_saver()}


def sdg1032x_set_screen_saver(minutes: int, verify: bool = True) -> dict:
    """Set the screen-saver delay in minutes: one of 0 (off), 1, 5, 15, 30,
    60, 120, 300. Returns the read-back; fails with a verify error on a
    mismatch."""
    return {"minutes": _get_sdg().set_screen_saver(minutes, verify=verify)}


def sdg1032x_get_number_format() -> dict:
    """The display's number format (``NBFM?``): decimal ``point`` (DOT/COMMA)
    and thousands ``separator`` (SPACE/ON/OFF). Read-only."""
    return _get_sdg().get_number_format().to_dict()


def sdg1032x_set_number_format(
    point: Optional[str] = None, separator: Optional[str] = None, verify: bool = True
) -> dict:
    """Set the display's number format: ``point`` DOT or COMMA, ``separator``
    SPACE, ON or OFF. Returns the read-back; fails with a verify error on a
    mismatch."""
    return _get_sdg().set_number_format(point=point, separator=separator, verify=verify).to_dict()


def sdg1032x_get_language() -> dict:
    """The front-panel language (``LAGG?``): EN or CH. Read-only."""
    return {"language": _get_sdg().get_language()}


def sdg1032x_set_language(language: str, verify: bool = True) -> dict:
    """Set the front-panel language: EN or CH (the SDG1000X offers only these).
    Returns the read-back; fails with a verify error on a mismatch."""
    return {"language": _get_sdg().set_language(language, verify=verify)}


def sdg1032x_get_power_on_config() -> dict:
    """What the instrument restores at power-on (``SCFG?``): DEFAULT, LAST or
    USER. Read-only."""
    return {"power_on_config": _get_sdg().get_power_on_config()}


def sdg1032x_set_power_on_config(mode: str, verify: bool = True) -> dict:
    """Set what the instrument restores at power-on: DEFAULT, LAST or USER.
    Returns the read-back; fails with a verify error on a mismatch."""
    return {"power_on_config": _get_sdg().set_power_on_config(mode, verify=verify)}


def sdg1032x_get_lan_config() -> dict:
    """The LAN settings (IP, mask, gateway) as dotted quads — three queries,
    each answered with a quoted string. Read-only."""
    return _get_sdg().get_lan_config().to_dict()


def sdg1032x_set_lan_config(
    ip: Optional[str] = None,
    mask: Optional[str] = None,
    gateway: Optional[str] = None,
    verify: bool = True,
) -> dict:
    """Set any of the LAN ``ip``, ``mask`` and ``gateway`` (dotted quads).
    Changing these on a unit reached over LAN will drop that session; the
    bench unit is on USB. Returns the read-back; fails with a verify error on
    a mismatch."""
    return _get_sdg().set_lan_config(ip=ip, mask=mask, gateway=gateway, verify=verify).to_dict()


def sdg1032x_trigger_key(key: str) -> dict:
    """Press a front-panel key by name (``VKEY``), e.g. ``KB_UTILITY``,
    ``KB_NUMBER_1``, ``KB_ENTER``, ``KB_KNOB_RIGHT``. The two Output keys
    (``KB_OUTPUT1``/``KB_OUTPUT2``) are refused: they would energise a channel
    outside the safety governor's watch — use ``sdg1032x_set_output``.
    Nothing to read back; ``sdg1032x_read_screen`` shows the effect."""
    _get_sdg().trigger_key(key)
    return {"ok": True, "key": key.upper()}


_TOOLS = (
    sdg1032x_open,
    sdg1032x_close,
    sdg1032x_info,
    sdg1032x_reset,
    sdg1032x_operation_complete,
    sdg1032x_write,
    sdg1032x_query,
    sdg1032x_get_output,
    sdg1032x_set_output,
    sdg1032x_set_output_load,
    sdg1032x_set_output_polarity,
    sdg1032x_disable_outputs,
    sdg1032x_get_basic_wave,
    sdg1032x_set_basic_wave,
    sdg1032x_set_wave_type,
    sdg1032x_set_frequency,
    sdg1032x_set_amplitude,
    sdg1032x_set_offset,
    sdg1032x_set_phase,
    sdg1032x_set_max_amplitude,
    sdg1032x_list_arbs,
    sdg1032x_get_arb,
    sdg1032x_select_arb,
    sdg1032x_write_arb,
    sdg1032x_read_arb,
    sdg1032x_get_modulation,
    sdg1032x_set_modulation,
    sdg1032x_get_sweep,
    sdg1032x_set_sweep,
    sdg1032x_trigger_sweep,
    sdg1032x_get_burst,
    sdg1032x_set_burst,
    sdg1032x_trigger_burst,
    sdg1032x_get_sync,
    sdg1032x_set_sync,
    sdg1032x_get_clock,
    sdg1032x_set_clock,
    sdg1032x_get_phase_mode,
    sdg1032x_set_phase_mode,
    sdg1032x_apply_equal_phase,
    sdg1032x_get_invert,
    sdg1032x_set_invert,
    sdg1032x_apply_channel_copy,
    sdg1032x_get_coupling,
    sdg1032x_set_coupling,
    sdg1032x_get_harmonics,
    sdg1032x_set_harmonics,
    sdg1032x_get_combine,
    sdg1032x_set_combine,
    sdg1032x_read_counter,
    sdg1032x_set_counter,
    sdg1032x_get_protection,
    sdg1032x_set_protection,
    sdg1032x_read_screen,
    sdg1032x_get_buzzer,
    sdg1032x_set_buzzer,
    sdg1032x_get_screen_saver,
    sdg1032x_set_screen_saver,
    sdg1032x_get_number_format,
    sdg1032x_set_number_format,
    sdg1032x_get_language,
    sdg1032x_set_language,
    sdg1032x_get_power_on_config,
    sdg1032x_set_power_on_config,
    sdg1032x_get_lan_config,
    sdg1032x_set_lan_config,
    sdg1032x_trigger_key,
)


def register_mcp_tools(mcp) -> None:
    """Register every SDG1032X MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

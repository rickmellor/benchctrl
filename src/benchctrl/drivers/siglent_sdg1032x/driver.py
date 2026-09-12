"""Siglent SDG1032X function / arbitrary waveform generator over USB-TMC.

The instrument
--------------
Two channels, 30 MHz sine/square, 150 MSa/s, 16 kpts arbitrary memory, 14-bit
DAC. Siglent's own SCPI dialect rather than the ``:NODE:NODE value`` style
the Rigols speak: a command is ``C1:BSWV FRQ,1000,AMP,2`` (a header, then
``KEY,VALUE`` pairs) and a query returns the **whole** state of that header
with the instrument's units suffixed — ``C1:BSWV WVTP,SINE,FRQ,1000HZ,PERI,
0.001S,AMP,4V,AMPVRMS,1.414Vrms,OFST,0V,HLEV,2V,LLEV,-2V,PHSE,0``. The
``CHDR`` (header on/off) command is not supported on the SDG1000X, so the
short header is always present and the parser strips it.

The design constraint: there is no error queue
----------------------------------------------
The SDG has no ``SYST:ERR?``. A value the instrument cannot represent, or one
outside the range of the current waveform, is **silently ignored or
quantised** — the command returns nothing and the setting stays wherever it
was. So this driver treats read-back as the contract, not a courtesy: every
setter sends only the fields it was given, reads the header's state back,
returns that typed read-back, and raises :py:class:`SDG1032XVerifyError` when
a requested field differs beyond the instrument's own resolution. The same
idea as the PDU driver's ``set_outlet_state`` (whose device also reports
nothing), applied to every mutator. ``verify=False`` still returns the
read-back but never raises — the caller opting into quantisation.

Two further read-back channels exist outside this module and are what the
bench actually uses to *validate* the driver: the instrument's own screen
(:py:meth:`SiglentSDG1032X.read_screen`, a bitmap the FUI shows) and the
bench camera on the front panel (``bench_vision`` classifiers on the Output
key backlights — ``docs/vision.md`` § Classifiers).

Bench-measured deviations from the programming guide (firmware 1.01.01.33R1B6)
-------------------------------------------------------------------------------
``MODE?`` answers ``MODE PHASE-LOCKED`` (the guide's token is ``PHASELOCKED``
and that is what the set form takes); ``VOLTPRT?`` answers a bare ``ON`` with
no header; ``ROSC?`` omits the ``10MOUT`` field; ``CURRPRT?`` and ``VOLTSTAT?``
are **not implemented** — the query produces a USB pipe error rather than a
reply. Unlike the SDM4065A, an unanswered query does *not* wedge this
instrument: the next query works. ``SCDP`` (screen dump, documented only in a
Siglent operating tip) returns a 480x272 24-bit BMP of 391734 bytes followed
by a stray newline; pyvisa-py delivers it in 20 KB chunks. All recorded in
``KNOWN_LIMITATIONS.md`` § F-21 onward.

Safety
------
``open()`` energises nothing and changes nothing. ``max_amplitude_vpp`` caps
every amplitude the driver sends (the instrument's ``MAX_OUTPUT_AMP`` is
accepted but neither echoed nor enforced by the bench firmware, so the cap
is the driver's, and a raw ``write()`` bypasses it). ``allowed_channels`` gates
every channel mutator; reads are never gated. ``disable_outputs()`` takes no
arguments so the agent's safe-stop (``agent/safety.py``) can reach it, and
``__exit__`` calls it. ``trigger_key`` refuses the two Output keys: a virtual
key press would energise a channel without the governor's arming watch
seeing it — use :py:meth:`SiglentSDG1032X.set_output`.
"""

from __future__ import annotations

import contextlib
import logging
import math
import numbers
import re
import struct
from collections.abc import Sequence
from dataclasses import dataclass, fields
from typing import Any, Optional, Union

log = logging.getLogger("benchctrl.drivers.siglent_sdg1032x")

SIGLENT_USB_VID = 0xF4EC
SDG1032X_USB_PID = 0x1103

#: The two output channels. ``C1``/``C2`` on the wire.
CHANNELS: tuple[int, ...] = (1, 2)

#: Arbitrary waveform memory per channel on the SDG1032X (datasheet).
ARB_MAX_SAMPLES = 16384
ARB_MIN_SAMPLES = 2
ARB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,24}$")

#: ``WVTP`` values the SDG1000X accepts (PRBS/IQ are other models).
WAVE_TYPES: tuple[str, ...] = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC")
MOD_TYPES: tuple[str, ...] = ("AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK")
MOD_SOURCES: tuple[str, ...] = ("INT", "EXT", "CH1", "CH2")
MOD_SHAPES: tuple[str, ...] = ("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE", "ARB")
SWEEP_MODES: tuple[str, ...] = ("LINE", "LOG", "STEP")
SWEEP_DIRECTIONS: tuple[str, ...] = ("UP", "DOWN", "UP_DOWN")
TRIGGER_SOURCES: tuple[str, ...] = ("INT", "EXT", "MAN")
BURST_MODES: tuple[str, ...] = ("GATE", "NCYC")
SYNC_TYPES: tuple[str, ...] = ("CH1", "CH2", "MOD_CH1", "MOD_CH2")
PHASE_MODES: tuple[str, ...] = ("PHASELOCKED", "INDEPENDENT")
HARMONIC_TYPES: tuple[str, ...] = ("EVEN", "ODD", "ALL")
SCREEN_SAVER_MINUTES: tuple[int, ...] = (0, 1, 5, 15, 30, 60, 120, 300)
LANGUAGES: tuple[str, ...] = ("EN", "CH")
POWER_ON_CONFIGS: tuple[str, ...] = ("DEFAULT", "LAST", "USER")

#: Front-panel keys ``VKEY`` can press on an SDG1000X (guide § 3.36).
VIRTUAL_KEYS: dict[str, int] = {
    "KB_FUNC1": 28,
    "KB_FUNC2": 23,
    "KB_FUNC3": 18,
    "KB_FUNC4": 13,
    "KB_FUNC5": 8,
    "KB_FUNC6": 3,
    "KB_MOD": 15,
    "KB_SWEEP": 16,
    "KB_BURST": 17,
    "KB_WAVES": 4,
    "KB_UTILITY": 11,
    "KB_PARAMETER": 5,
    "KB_STORE_RECALL": 70,
    "KB_CHANNEL": 72,
    "KB_NUMBER_0": 48,
    "KB_NUMBER_1": 49,
    "KB_NUMBER_2": 50,
    "KB_NUMBER_3": 51,
    "KB_NUMBER_4": 52,
    "KB_NUMBER_5": 53,
    "KB_NUMBER_6": 54,
    "KB_NUMBER_7": 55,
    "KB_NUMBER_8": 56,
    "KB_NUMBER_9": 57,
    "KB_POINT": 46,
    "KB_NEGATIVE": 43,
    "KB_LEFT": 44,
    "KB_RIGHT": 40,
    "KB_ENTER": 58,
    "KB_K": 59,
    "KB_M": 60,
    "KB_G": 61,
    "KB_KNOB_RIGHT": 175,
    "KB_KNOB_LEFT": 177,
    "KB_KNOB_DOWN": 176,
    "KB_OUTPUT1": 153,
    "KB_OUTPUT2": 152,
}
#: Keys :py:meth:`SiglentSDG1032X.trigger_key` refuses — they arm a channel.
OUTPUT_KEYS: frozenset[str] = frozenset({"KB_OUTPUT1", "KB_OUTPUT2"})

DEFAULT_TIMEOUT_MS = 5000
#: Around a waveform transfer or a screen dump (hundreds of KB over USB-TMC).
TRANSFER_TIMEOUT_MS = 30000

#: Read-back tolerance. Relative 1e-5 absorbs the six significant digits the
#: instrument renders with (``1.15473Vrms``, ``2.4e-07S``); the absolute
#: floors are *half* the front-panel resolutions (1 µHz, 1 ns, 1 mV, 0.01°,
#: 0.01 %) — rounding to the nearest step moves a value by at most half a
#: step, while a full-step floor would let a one-step clamp through (bench:
#: ``AMP,0.001`` reads back ``0.002V``, the 2 mVpp minimum, and must fail).
REL_TOL = 1e-5
ABS_TOL: dict[str, float] = {"Hz": 5e-7, "s": 5e-10, "V": 5e-4, "deg": 0.005, "%": 0.005, "": 1e-9}

# Datasheet limits (the guide says "refer to the datasheet"); used client-side
# to refuse an obviously impossible request before it is sent.
FREQ_MAX_HZ: dict[str, float] = {
    "SINE": 30e6,
    "SQUARE": 30e6,
    "RAMP": 500e3,
    "PULSE": 12.5e6,
    "ARB": 6e6,
}
AMPLITUDE_MAX_VPP_HIZ = 20.0
AMPLITUDE_MAX_VPP_50 = 10.0
OFFSET_MAX_V = 10.0


# ---------------------------------------------------------------- errors


class SDG1032XError(RuntimeError):
    """Base class for every error this driver raises."""


class SDG1032XConnectionError(SDG1032XError):
    """No instrument, the session is closed, or VISA/USB failed."""


class SDG1032XTimeoutError(SDG1032XError, TimeoutError):
    """A query went unanswered. On this firmware that also means "header not
    implemented" (``CURRPRT?``, ``VOLTSTAT?``): the SDG replies with nothing."""


class SDG1032XValueError(SDG1032XError, ValueError):
    """The caller asked for something the instrument can never do."""


class SDG1032XProtocolError(SDG1032XError):
    """The instrument answered in a shape the parser does not know. There is no
    error code to carry (no queue); the remedy is a quirk entry, not a retry."""


class SDG1032XVerifyError(SDG1032XError):
    """The command was accepted and the read-back disagrees with what was asked.

    The instrument has no error queue, so this is the *only* signal that a
    value was refused or quantised. Remedy: ask for a value the instrument can
    represent for the current waveform/load, or pass ``verify=False`` to accept
    what it chose. Carries ``channel``, ``field``, ``wanted``, ``got``.
    """

    def __init__(self, message: str, *, channel: Optional[int], field: str, wanted: Any, got: Any):
        super().__init__(message)
        self.channel = channel
        self.field = field
        self.wanted = wanted
        self.got = got


class SDG1032XPolicyError(SDG1032XError):
    """Refused by this driver's own grant: a channel outside ``allowed_channels``,
    an amplitude above ``max_amplitude_vpp``, or an Output key through
    ``trigger_key``. A wiring decision for a human, never a retry."""


# ---------------------------------------------------------------- wire types


@dataclass(frozen=True)
class SDG1032XInfo:
    manufacturer: str
    model: str
    serial: str
    firmware: str
    resource: str

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class OutputState:
    """``Cn:OUTP?``: ``load_ohm`` is ``None`` for high impedance."""

    channel: int
    enabled: bool
    load_ohm: Optional[float]
    inverted: bool

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class BasicWave:
    """``Cn:BSWV?`` typed. A field the instrument did not report is ``None``
    (NOISE reports no frequency, DC no amplitude, and so on). Also the
    ``CARR`` block of a modulation/sweep/burst read, with ``channel=None``."""

    channel: Optional[int]
    wave_type: Optional[str] = None
    frequency_hz: Optional[float] = None
    period_s: Optional[float] = None
    amplitude_vpp: Optional[float] = None
    amplitude_vrms: Optional[float] = None
    amplitude_dbm: Optional[float] = None
    offset_v: Optional[float] = None
    high_level_v: Optional[float] = None
    low_level_v: Optional[float] = None
    phase_deg: Optional[float] = None
    duty_pct: Optional[float] = None
    symmetry_pct: Optional[float] = None
    pulse_width_s: Optional[float] = None
    rise_s: Optional[float] = None
    fall_s: Optional[float] = None
    delay_s: Optional[float] = None
    noise_stdev_v: Optional[float] = None
    noise_mean_v: Optional[float] = None
    max_amplitude_vpp: Optional[float] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Modulation:
    channel: int
    enabled: bool
    type: Optional[str] = None
    source: Optional[str] = None
    shape: Optional[str] = None
    frequency_hz: Optional[float] = None
    depth_pct: Optional[float] = None
    deviation: Optional[float] = None  # Hz for FM, degrees for PM, % for PWM
    key_frequency_hz: Optional[float] = None
    hop_frequency_hz: Optional[float] = None
    polarity: Optional[str] = None
    carrier: Optional[BasicWave] = None

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["carrier"] = self.carrier.to_dict() if self.carrier else None
        return out


@dataclass(frozen=True)
class Sweep:
    channel: int
    enabled: bool
    time_s: Optional[float] = None
    start_hold_s: Optional[float] = None
    end_hold_s: Optional[float] = None
    return_s: Optional[float] = None
    start_hz: Optional[float] = None
    stop_hz: Optional[float] = None
    center_hz: Optional[float] = None
    span_hz: Optional[float] = None
    mode: Optional[str] = None
    direction: Optional[str] = None
    symmetry_pct: Optional[float] = None
    trigger_source: Optional[str] = None
    trigger_out: Optional[bool] = None
    trigger_edge: Optional[str] = None
    mark_enabled: Optional[bool] = None
    mark_hz: Optional[float] = None
    carrier: Optional[BasicWave] = None

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["carrier"] = self.carrier.to_dict() if self.carrier else None
        return out


@dataclass(frozen=True)
class Burst:
    channel: int
    enabled: bool
    period_s: Optional[float] = None
    start_phase_deg: Optional[float] = None
    mode: Optional[str] = None
    trigger_source: Optional[str] = None
    trigger_out: Optional[str] = None
    trigger_edge: Optional[str] = None
    delay_s: Optional[float] = None
    polarity: Optional[str] = None
    cycles: Optional[int] = None  # None = infinite
    carrier: Optional[BasicWave] = None

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["carrier"] = self.carrier.to_dict() if self.carrier else None
        return out


@dataclass(frozen=True)
class ArbInfo:
    index: Optional[int]
    name: str
    builtin: bool

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class ArbSelection:
    channel: int
    index: Optional[int]
    name: str

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class ArbData:
    """A user waveform: metadata and the raw int16 little-endian codes."""

    name: str
    frequency_hz: Optional[float]
    amplitude_vpp: Optional[float]
    offset_v: Optional[float]
    phase_deg: Optional[float]
    codes: bytes

    @property
    def samples(self) -> tuple[int, ...]:
        n = len(self.codes) // 2
        return struct.unpack(f"<{n}h", self.codes[: 2 * n])

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "frequency_hz": self.frequency_hz,
            "amplitude_vpp": self.amplitude_vpp,
            "offset_v": self.offset_v,
            "phase_deg": self.phase_deg,
            "samples": len(self.codes) // 2,
            "bytes": len(self.codes),
        }


@dataclass(frozen=True)
class SyncConfig:
    channel: int
    enabled: bool
    type: Optional[str] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class ClockConfig:
    source: str
    output_10m: Optional[bool] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class CounterReading:
    enabled: bool
    frequency_hz: Optional[float] = None
    duty_pct: Optional[float] = None
    reference_hz: Optional[float] = None
    trigger_level_v: Optional[float] = None
    pulse_width_s: Optional[float] = None
    negative_width_s: Optional[float] = None
    deviation_ppm: Optional[float] = None
    coupling: Optional[str] = None
    high_frequency_reject: Optional[bool] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Coupling:
    trace: bool
    freq_coupled: Optional[bool] = None
    freq_deviation_hz: Optional[float] = None
    freq_ratio: Optional[float] = None
    phase_coupled: Optional[bool] = None
    phase_deviation_deg: Optional[float] = None
    phase_ratio: Optional[float] = None
    amp_coupled: Optional[bool] = None
    amp_ratio: Optional[float] = None
    amp_deviation_v: Optional[float] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Harmonic:
    channel: int
    enabled: bool
    type: Optional[str] = None
    order: Optional[int] = None
    amplitude_v: Optional[float] = None
    amplitude_dbc: Optional[float] = None
    phase_deg: Optional[float] = None

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class ProtectionState:
    over_voltage: Optional[bool]

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class LanConfig:
    ip: Optional[str]
    mask: Optional[str]
    gateway: Optional[str]

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class NumberFormat:
    point: str
    separator: str

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


# ---------------------------------------------------------------- parsing

_NUMBER_RE = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z%]*)$")
_UNITS: dict[str, str] = {
    "": "",
    "HZ": "Hz",
    "S": "s",
    "V": "V",
    "VRMS": "Vrms",
    "DBM": "dBm",
    "DBC": "dBc",
    "PPM": "ppm",
    "MIN": "min",
    "%": "%",
}


def split_header(raw: str, header: str) -> tuple[Optional[int], str]:
    """``'C1:BSWV WVTP,SINE,…'`` -> ``(1, 'WVTP,SINE,…')``.

    Tolerates the guide's ``C2:SWWVSTATE,…`` (no space after the header) and a
    headerless answer (``VOLTPRT?`` returns a bare ``ON``; the LAN queries a
    quoted string), in which case the whole line is the body.
    """
    text = raw.strip()
    m = re.match(rf"^(?:C(?P<ch>[12]):)?{re.escape(header)}(?![A-Z_]*[a-z])\s*", text)
    if m:
        ch = m.group("ch")
        return (int(ch) if ch else None), text[m.end() :].strip()
    return None, text


def parse_pairs(body: str) -> tuple[dict[str, str], Optional[dict[str, str]]]:
    """``'WVTP,SINE,FRQ,1000HZ,CARR,WVTP,SINE,…'`` -> (main, carrier).

    Tokens are stripped (``NBFM`` answers with spaces after its commas) and an
    empty leading token is dropped (``C1:HARM ,HARMSTATE,…`` — the stray comma
    the guide prints). Pairs after the bare ``CARR`` marker are the carrier.
    A key without a value (an odd trailing token) is kept with ``""``.
    """
    tokens = [t.strip() for t in body.split(",")]
    if tokens and tokens[0] == "":
        tokens = tokens[1:]
    main: dict[str, str] = {}
    carrier: Optional[dict[str, str]] = None
    target = main
    i = 0
    while i < len(tokens):
        key = tokens[i]
        if key == "CARR":
            carrier = {}
            target = carrier
            i += 1
            continue
        value = tokens[i + 1] if i + 1 < len(tokens) else ""
        target[key.upper()] = value
        i += 2
    return main, carrier


def parse_number(token: str) -> tuple[float, str]:
    """``'1000HZ'`` -> ``(1000.0, 'Hz')``; ``'2.4e-07S'`` -> ``(2.4e-7, 's')``;
    ``'0'`` -> ``(0.0, '')``. Raises :py:class:`SDG1032XProtocolError` on
    anything else (``HZ`` alone, ``INF``, ``OFF``… are not numbers)."""
    m = _NUMBER_RE.match(token.strip())
    if not m:
        raise SDG1032XProtocolError(f"not a number: {token!r}")
    unit = _UNITS.get(m.group(2).upper())
    if unit is None:
        raise SDG1032XProtocolError(f"unknown unit in {token!r}")
    return float(m.group(1)), unit


def _num(pairs: dict[str, str], key: str) -> Optional[float]:
    tok = pairs.get(key)
    if tok is None or tok == "":
        return None
    return parse_number(tok)[0]


def _int_or_none(pairs: dict[str, str], key: str) -> Optional[int]:
    v = _num(pairs, key)
    return int(v) if v is not None else None


def _onoff(pairs: dict[str, str], key: str) -> Optional[bool]:
    tok = pairs.get(key)
    if tok is None or tok == "":
        return None
    return _bool(tok)


def _bool(tok: str) -> bool:
    t = tok.strip().upper()
    if t in ("ON", "1", "TRUE"):
        return True
    if t in ("OFF", "0", "FALSE"):
        return False
    raise SDG1032XProtocolError(f"not ON/OFF: {tok!r}")


def _str(pairs: dict[str, str], key: str) -> Optional[str]:
    tok = pairs.get(key)
    return tok if tok not in (None, "") else None


def _load(tok: Optional[str]) -> Optional[float]:
    if tok is None:
        return None
    t = tok.strip().upper()
    if t in ("HZ", "HIZ"):
        return None
    return parse_number(t)[0]


def basic_wave_from_pairs(pairs: dict[str, str], channel: Optional[int]) -> BasicWave:
    return BasicWave(
        channel=channel,
        wave_type=_str(pairs, "WVTP"),
        frequency_hz=_num(pairs, "FRQ"),
        period_s=_num(pairs, "PERI"),
        amplitude_vpp=_num(pairs, "AMP"),
        amplitude_vrms=_num(pairs, "AMPVRMS"),
        amplitude_dbm=_num(pairs, "AMPDBM"),
        offset_v=_num(pairs, "OFST"),
        high_level_v=_num(pairs, "HLEV"),
        low_level_v=_num(pairs, "LLEV"),
        phase_deg=_num(pairs, "PHSE"),
        duty_pct=_num(pairs, "DUTY"),
        symmetry_pct=_num(pairs, "SYM"),
        pulse_width_s=_num(pairs, "WIDTH"),
        rise_s=_num(pairs, "RISE"),
        fall_s=_num(pairs, "FALL"),
        delay_s=_num(pairs, "DLY"),
        noise_stdev_v=_num(pairs, "STDEV"),
        noise_mean_v=_num(pairs, "MEAN"),
        max_amplitude_vpp=_num(pairs, "MAX_OUTPUT_AMP"),
    )


def _carrier(pairs: Optional[dict[str, str]]) -> Optional[BasicWave]:
    return basic_wave_from_pairs(pairs, None) if pairs else None


#: ``BasicWave`` field -> (``BSWV`` key, unit for the tolerance table).
_BSWV_FIELDS: dict[str, tuple[str, str]] = {
    "wave_type": ("WVTP", ""),
    "frequency_hz": ("FRQ", "Hz"),
    "period_s": ("PERI", "s"),
    "amplitude_vpp": ("AMP", "V"),
    "amplitude_vrms": ("AMPVRMS", "V"),
    "amplitude_dbm": ("AMPDBM", ""),
    "offset_v": ("OFST", "V"),
    "high_level_v": ("HLEV", "V"),
    "low_level_v": ("LLEV", "V"),
    "phase_deg": ("PHSE", "deg"),
    "duty_pct": ("DUTY", "%"),
    "symmetry_pct": ("SYM", "%"),
    "pulse_width_s": ("WIDTH", "s"),
    "rise_s": ("RISE", "s"),
    "fall_s": ("FALL", "s"),
    "delay_s": ("DLY", "s"),
    "noise_stdev_v": ("STDEV", "V"),
    "noise_mean_v": ("MEAN", "V"),
    "max_amplitude_vpp": ("MAX_OUTPUT_AMP", "V"),
}

_MDWV_FIELDS: dict[str, tuple[str, str]] = {
    "source": ("SRC", ""),
    "shape": ("MDSP", ""),
    "frequency_hz": ("FRQ", "Hz"),
    "depth_pct": ("DEPTH", "%"),
    "deviation": ("DEVI", ""),
    "key_frequency_hz": ("KFRQ", "Hz"),
    "hop_frequency_hz": ("HFRQ", "Hz"),
    "polarity": ("PLRT", ""),
}

_SWWV_FIELDS: dict[str, tuple[str, str]] = {
    "time_s": ("TIME", "s"),
    "start_hold_s": ("STARTTIME", "s"),
    "end_hold_s": ("ENDTIME", "s"),
    "return_s": ("BACKTIME", "s"),
    "start_hz": ("START", "Hz"),
    "stop_hz": ("STOP", "Hz"),
    "center_hz": ("CENTER", "Hz"),
    "span_hz": ("SPAN", "Hz"),
    "mode": ("SWMD", ""),
    "direction": ("DIR", ""),
    "symmetry_pct": ("SYM", "%"),
    "trigger_source": ("TRSR", ""),
    "trigger_out": ("TRMD", ""),
    "trigger_edge": ("EDGE", ""),
    "mark_enabled": ("MARK_STATE", ""),
    "mark_hz": ("MARK_FREQ", "Hz"),
}

_BTWV_FIELDS: dict[str, tuple[str, str]] = {
    "period_s": ("PRD", "s"),
    "start_phase_deg": ("STPS", "deg"),
    "mode": ("GATE_NCYC", ""),
    "trigger_source": ("TRSR", ""),
    "trigger_out": ("TRMD", ""),
    "trigger_edge": ("EDGE", ""),
    "delay_s": ("DLAY", "s"),
    "polarity": ("PLRT", ""),
    "cycles": ("TIME", ""),
}


def _fmt(value: Any) -> str:
    """Render a Python value for the wire: bools as ON/OFF, floats compactly."""
    if isinstance(value, bool):
        return "ON" if value else "OFF"
    if isinstance(value, float):
        return f"{value:.12g}"
    return str(value)


def _close(wanted: Any, got: Any, unit: str) -> bool:
    if isinstance(wanted, bool) or isinstance(got, bool):
        return bool(wanted) == bool(got)
    if isinstance(wanted, str) or isinstance(got, str):
        return str(wanted).strip().upper() == str(got).strip().upper()
    if got is None:
        return False
    w, g = float(wanted), float(got)
    return abs(g - w) <= max(REL_TOL * abs(w), ABS_TOL.get(unit, 1e-9))


# ---------------------------------------------------------------- the driver


ChannelLike = Union[int, str]


class SiglentSDG1032X:
    """Driver for the SDG1032X. See the module docstring for the read-back rule."""

    DEFAULT_TIMEOUT_MS = DEFAULT_TIMEOUT_MS

    def __init__(
        self,
        instrument,  # pyvisa Resource — typed loosely so pyvisa stays optional
        *,
        resource_string: str,
        owns_resource_manager: bool = False,
        resource_manager=None,
        allowed_channels: Sequence[int] = CHANNELS,
        max_amplitude_vpp: Optional[float] = None,
    ):
        self._inst = instrument
        self._resource = resource_string
        self._owns_rm = owns_resource_manager  # API compatibility; gates nothing
        self._rm = resource_manager
        self._info: Optional[SDG1032XInfo] = None
        self._closed = False
        self._allowed = tuple(sorted({self._coerce_channel(c) for c in allowed_channels}))
        if not self._allowed:
            raise SDG1032XValueError("allowed_channels must name at least one channel")
        if max_amplitude_vpp is not None:
            if isinstance(max_amplitude_vpp, bool) or not isinstance(
                max_amplitude_vpp, numbers.Real
            ):
                raise SDG1032XValueError("max_amplitude_vpp must be a number")
            if not 0 < float(max_amplitude_vpp) <= AMPLITUDE_MAX_VPP_HIZ:
                raise SDG1032XValueError(
                    f"max_amplitude_vpp must be in (0, {AMPLITUDE_MAX_VPP_HIZ:g}], got {max_amplitude_vpp!r}"
                )
        self._max_amp: Optional[float] = (
            float(max_amplitude_vpp) if max_amplitude_vpp is not None else None
        )
        self._base_timeout_ms = self.DEFAULT_TIMEOUT_MS

    # ------------------------------------------------------------ lifecycle

    @classmethod
    def open(
        cls,
        resource: Optional[str] = None,
        *,
        allowed_channels: Sequence[int] = CHANNELS,
        max_amplitude_vpp: Optional[float] = None,
        timeout_ms: int = DEFAULT_TIMEOUT_MS,
        read_termination: str = "\n",
        write_termination: str = "\n",
    ) -> SiglentSDG1032X:
        """Open the generator. Energises nothing.

        ``resource`` unset finds the one SDG1000X on the bus through
        :py:func:`benchctrl.discovery.visa_resource_for`. ``allowed_channels``
        gates every channel *mutator* (reads are never gated);
        ``max_amplitude_vpp`` caps every amplitude this driver sends (the
        instrument's own ``MAX_OUTPUT_AMP`` is not honoured by the bench
        firmware — see :py:meth:`set_max_amplitude`). ``open()`` makes no
        state change at all.
        """
        try:
            import pyvisa  # noqa: PLC0415
        except ImportError as e:
            raise SDG1032XConnectionError(
                "pyvisa is required for the SDG1032X — install with "
                "`pip install benchctrl[bench-visa]` (pyvisa-py speaks USB-TMC "
                "over libusb, so no kernel usbtmc module is needed)"
            ) from e
        try:
            rm = pyvisa.ResourceManager()
        except Exception as e:
            raise SDG1032XConnectionError(f"could not initialize VISA resource manager: {e}") from e
        if resource is None:
            from benchctrl import discovery  # noqa: PLC0415

            resource = discovery.visa_resource_for(
                "siglent_sdg1032x", error=SDG1032XConnectionError, resource_manager=rm
            )
        try:
            inst = rm.open_resource(resource)
        except Exception as e:
            raise SDG1032XConnectionError(f"could not open VISA resource {resource!r}: {e}") from e
        inst.timeout = timeout_ms
        inst.read_termination = read_termination  # type: ignore[attr-defined]
        inst.write_termination = write_termination  # type: ignore[attr-defined]
        gen = cls(
            inst,
            resource_string=resource,
            owns_resource_manager=True,
            resource_manager=rm,
            allowed_channels=allowed_channels,
            max_amplitude_vpp=max_amplitude_vpp,
        )
        gen._base_timeout_ms = timeout_ms
        return gen

    def close(self) -> None:
        """Release this instrument's VISA session (never the shared
        ResourceManager — see the SDM4065A driver for the bench history)."""
        if self._closed:
            return
        try:
            self._inst.close()
        except Exception:
            log.debug("error closing VISA instrument", exc_info=True)
        self._closed = True

    def __enter__(self) -> SiglentSDG1032X:
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        try:
            self.disable_outputs()
        except Exception as e:  # noqa: BLE001 - best effort on the way out
            log.warning("sdg1032x: could not disarm outputs on exit: %r", e)
        self.close()

    # ------------------------------------------------------------ properties (I/O-free)

    @property
    def is_connected(self) -> bool:
        return not self._closed

    @property
    def resource(self) -> str:
        return self._resource

    @property
    def channels(self) -> tuple[int, ...]:
        return CHANNELS

    @property
    def allowed_channels(self) -> tuple[int, ...]:
        return self._allowed

    @property
    def max_amplitude_vpp(self) -> Optional[float]:
        return self._max_amp

    # ------------------------------------------------------------ transport

    def write(self, command: str) -> None:
        """Send a raw command. Nothing comes back — and nothing is verified,
        which is why the typed setters exist."""
        if self._closed:
            raise SDG1032XConnectionError("instrument is closed")
        try:
            self._inst.write(command)
        except Exception as e:
            raise SDG1032XConnectionError(f"write({command!r}) failed: {e}") from e

    def query(self, command: str) -> str:
        """Send a raw query and return the trimmed answer. A header this
        firmware does not implement answers with nothing (a timeout or a USB
        pipe error, both reported as :py:class:`SDG1032XTimeoutError`); unlike
        the SDM4065A the instrument recovers on the next query."""
        if self._closed:
            raise SDG1032XConnectionError("instrument is closed")
        try:
            raw = self._inst.query(command)
        except Exception as e:
            text = str(e).lower()
            if "timeout" in text or "vi_error_tmo" in text or "pipe error" in text:
                raise SDG1032XTimeoutError(
                    f"query({command!r}) went unanswered — the SDG1032X replies with "
                    "nothing to a header its firmware does not implement"
                ) from e
            raise SDG1032XConnectionError(f"query({command!r}) failed: {e}") from e
        return raw.strip()

    def info(self) -> SDG1032XInfo:
        """``*IDN?`` (4 fields on the SDG1000X), cached after the first call."""
        if self._info is None:
            raw = self.query("*IDN?")
            parts = [p.strip() for p in raw.split(",")]
            if len(parts) < 4:
                raise SDG1032XProtocolError(f"unexpected *IDN? response: {raw!r}")
            self._info = SDG1032XInfo(
                manufacturer=parts[0],
                model=parts[1],
                serial=parts[2],
                firmware=parts[3],
                resource=self._resource,
            )
        return self._info

    def reset(self) -> None:
        """``*RST`` — factory defaults. Outputs end up OFF, load HiZ, 1 kHz 4 Vpp sine."""
        self.write("*RST")
        self._info = None

    def operation_complete(self) -> bool:
        """``*OPC?`` — always ``1`` once the previous command has been processed."""
        return self.query("*OPC?").strip() == "1"

    # ------------------------------------------------------------ output

    def get_output(self, channel: ChannelLike) -> OutputState:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:OUTP?")
        _, body = split_header(raw, "OUTP")
        tokens = [t.strip() for t in body.split(",")]
        if not tokens or tokens[0].upper() not in ("ON", "OFF"):
            raise SDG1032XProtocolError(f"unexpected OUTP response: {raw!r}")
        pairs, _ = parse_pairs(",".join(tokens[1:]))
        return OutputState(
            channel=ch,
            enabled=_bool(tokens[0]),
            load_ohm=_load(pairs.get("LOAD")),
            inverted=(pairs.get("PLRT", "NOR").upper() == "INVT"),
        )

    def set_output(self, channel: ChannelLike, on: bool, *, verify: bool = True) -> OutputState:
        """Switch a channel's output. Returns the read-back state.

        SAFETY: this energises the BNC. Know what is attached first; the
        amplitude cap (``max_amplitude_vpp``) and the load setting decide how
        much voltage appears. The agent's governor counts this as arming.
        """
        ch = self._check_channel(channel)
        if not isinstance(on, bool):
            raise SDG1032XValueError(f"on must be a bool, got {on!r}")
        self.write(f"C{ch}:OUTP {'ON' if on else 'OFF'}")
        st = self.get_output(ch)
        self._verify(verify, ch, "enabled", on, st.enabled, "")
        return st

    def set_output_load(
        self, channel: ChannelLike, load_ohm: Optional[float], *, verify: bool = True
    ) -> OutputState:
        """``None`` = high impedance; otherwise 50–100000 Ω. Amplitude limits
        halve into 50 Ω, so set the load before the amplitude."""
        ch = self._check_channel(channel)
        if load_ohm is not None:
            if isinstance(load_ohm, bool) or not isinstance(load_ohm, numbers.Real):
                raise SDG1032XValueError("load_ohm must be a number or None")
            if not 50 <= float(load_ohm) <= 100000:
                raise SDG1032XValueError(
                    f"load_ohm must be 50..100000 or None (HiZ), got {load_ohm!r}"
                )
        self.write(f"C{ch}:OUTP LOAD,{'HZ' if load_ohm is None else _fmt(float(load_ohm))}")
        st = self.get_output(ch)
        if load_ohm is None:
            self._verify(
                verify, ch, "load_ohm", "HZ", "HZ" if st.load_ohm is None else st.load_ohm, ""
            )
        else:
            self._verify(verify, ch, "load_ohm", float(load_ohm), st.load_ohm, "")
        return st

    def set_output_polarity(
        self, channel: ChannelLike, inverted: bool, *, verify: bool = True
    ) -> OutputState:
        ch = self._check_channel(channel)
        if not isinstance(inverted, bool):
            raise SDG1032XValueError("inverted must be a bool")
        self.write(f"C{ch}:OUTP PLRT,{'INVT' if inverted else 'NOR'}")
        st = self.get_output(ch)
        self._verify(verify, ch, "inverted", inverted, st.inverted, "")
        return st

    def disable_outputs(self) -> dict[int, OutputState]:
        """Switch every channel OFF, read back, return the states.

        Takes no arguments so the agent's safe-stop can call it, and ignores
        ``allowed_channels`` on purpose: disarming is never policy-gated.
        Each channel is attempted even if an earlier one failed.
        """
        out: dict[int, OutputState] = {}
        errors: list[str] = []
        for ch in CHANNELS:
            try:
                self.write(f"C{ch}:OUTP OFF")
                st = self.get_output(ch)
                out[ch] = st
                if st.enabled:
                    errors.append(f"C{ch} still ON after OUTP OFF")
            except Exception as e:  # noqa: BLE001 - keep going, this is the safety path
                errors.append(f"C{ch}: {e!r}")
        if errors:
            raise SDG1032XVerifyError(
                "disable_outputs: " + "; ".join(errors),
                channel=None,
                field="enabled",
                wanted=False,
                got=None,
            )
        return out

    # ------------------------------------------------------------ basic wave

    def get_basic_wave(self, channel: ChannelLike) -> BasicWave:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:BSWV?")
        _, body = split_header(raw, "BSWV")
        pairs, _ = parse_pairs(body)
        if "WVTP" not in pairs:
            raise SDG1032XProtocolError(f"unexpected BSWV response: {raw!r}")
        return basic_wave_from_pairs(pairs, ch)

    def set_basic_wave(
        self,
        channel: ChannelLike,
        *,
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
    ) -> BasicWave:
        """Set any subset of the basic-wave parameters in one command and
        return the read-back. Fields not given are untouched. The wave type,
        when given, is sent first so the other fields apply to it.

        Every field is checked against the read-back: a frequency above the
        waveform's ceiling, a duty the frequency cannot support, or an
        amplitude the load cannot deliver is refused silently by the
        instrument and surfaces here as :py:class:`SDG1032XVerifyError`.
        """
        ch = self._check_channel(channel)
        wanted: dict[str, Any] = {}
        if wave_type is not None:
            wt = str(wave_type).upper()
            if wt not in WAVE_TYPES:
                raise SDG1032XValueError(
                    f"wave_type must be one of {WAVE_TYPES}, got {wave_type!r}"
                )
            wanted["wave_type"] = wt
        for name, value in (
            ("frequency_hz", frequency_hz),
            ("period_s", period_s),
            ("amplitude_vpp", amplitude_vpp),
            ("amplitude_vrms", amplitude_vrms),
            ("amplitude_dbm", amplitude_dbm),
            ("offset_v", offset_v),
            ("high_level_v", high_level_v),
            ("low_level_v", low_level_v),
            ("phase_deg", phase_deg),
            ("duty_pct", duty_pct),
            ("symmetry_pct", symmetry_pct),
            ("pulse_width_s", pulse_width_s),
            ("rise_s", rise_s),
            ("fall_s", fall_s),
            ("delay_s", delay_s),
            ("noise_stdev_v", noise_stdev_v),
            ("noise_mean_v", noise_mean_v),
        ):
            if value is None:
                continue
            wanted[name] = self._number(value, name)
        if not wanted:
            raise SDG1032XValueError("set_basic_wave: nothing to set")
        self._check_limits(wanted)
        # Bench finding (firmware 1.01.01.33R1B6): a BSWV command that carries
        # WVTP *and* other fields makes the instrument swallow exactly one
        # following message — the read-back query times out and the next
        # one answers. WVTP alone, or fields without WVTP, answer at once. So
        # the wave type goes in its own command and the rest follow.
        if "wave_type" in wanted:
            self.write(f"C{ch}:BSWV WVTP,{wanted['wave_type']}")
        parts = [
            f"{_BSWV_FIELDS[name][0]},{_fmt(value)}"
            for name, value in wanted.items()
            if name != "wave_type"
        ]
        if parts:
            self.write(f"C{ch}:BSWV " + ",".join(parts))
        got = self.get_basic_wave(ch)
        for name, value in wanted.items():
            _, unit = _BSWV_FIELDS[name]
            self._verify(verify, ch, name, value, getattr(got, name), unit)
        return got

    def set_wave_type(
        self, channel: ChannelLike, wave_type: str, *, verify: bool = True
    ) -> BasicWave:
        return self.set_basic_wave(channel, wave_type=wave_type, verify=verify)

    def set_frequency(
        self, channel: ChannelLike, frequency_hz: float, *, verify: bool = True
    ) -> BasicWave:
        return self.set_basic_wave(channel, frequency_hz=frequency_hz, verify=verify)

    def set_amplitude(
        self, channel: ChannelLike, amplitude_vpp: float, *, verify: bool = True
    ) -> BasicWave:
        return self.set_basic_wave(channel, amplitude_vpp=amplitude_vpp, verify=verify)

    def set_offset(
        self, channel: ChannelLike, offset_v: float, *, verify: bool = True
    ) -> BasicWave:
        return self.set_basic_wave(channel, offset_v=offset_v, verify=verify)

    def set_phase(
        self, channel: ChannelLike, phase_deg: float, *, verify: bool = True
    ) -> BasicWave:
        return self.set_basic_wave(channel, phase_deg=phase_deg, verify=verify)

    def set_max_amplitude(self, amplitude_vpp: float) -> float:
        """Tighten this driver's amplitude cap (``max_amplitude_vpp``) at run time.

        The instrument has a ``MAX_OUTPUT_AMP`` parameter for this, but the
        bench unit's firmware (1.01.01.33R1B6) accepts it, never echoes it and
        does not enforce it — a 3 Vpp request after a 2 Vpp cap reads back
        3 Vpp — so the cap that holds is the driver's own check on every
        amplitude it sends. The cap can only be lowered here; raising it is
        an ``open()``/``agent.json`` decision. Returns the cap in force.
        """
        v = self._number(amplitude_vpp, "amplitude_vpp")
        if not 0 < v <= AMPLITUDE_MAX_VPP_HIZ:
            raise SDG1032XValueError(
                f"amplitude_vpp must be in (0, {AMPLITUDE_MAX_VPP_HIZ:g}], got {amplitude_vpp!r}"
            )
        if self._max_amp is not None and v > self._max_amp:
            raise SDG1032XPolicyError(
                f"the cap can only be lowered here: {v:g} Vpp > max_amplitude_vpp={self._max_amp:g} from open()"
            )
        self._max_amp = v
        return v

    # ------------------------------------------------------------ arbitrary waveforms

    def list_arbs(self, kind: str = "builtin") -> tuple[ArbInfo, ...]:
        """``STL? BUILDIN`` (index + name pairs) or ``STL? USER`` (names only).
        Built-ins select by *index*, user waves by *name* (``select_arb``)."""
        k = str(kind).lower()
        if k not in ("builtin", "user"):
            raise SDG1032XValueError("kind must be 'builtin' or 'user'")
        raw = self.query("STL? BUILDIN" if k == "builtin" else "STL? USER")
        _, body = split_header(raw, "STL")
        tokens = [t.strip() for t in body.split(",") if t.strip()]
        if k == "user":
            if tokens and tokens[0].upper() == "WVNM":
                tokens = tokens[1:]
            return tuple(ArbInfo(index=None, name=t, builtin=False) for t in tokens)
        out: list[ArbInfo] = []
        i = 0
        while i + 1 < len(tokens):
            m = re.match(r"^M(\d+)$", tokens[i])
            if not m:
                raise SDG1032XProtocolError(f"unexpected STL entry {tokens[i]!r} in {raw[:80]!r}")
            out.append(ArbInfo(index=int(m.group(1)), name=tokens[i + 1], builtin=True))
            i += 2
        return tuple(sorted(out, key=lambda a: a.index or 0))

    def get_arb(self, channel: ChannelLike) -> ArbSelection:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:ARWV?")
        _, body = split_header(raw, "ARWV")
        pairs, _ = parse_pairs(body)
        if "INDEX" not in pairs:
            raise SDG1032XProtocolError(f"unexpected ARWV response: {raw!r}")
        return ArbSelection(
            channel=ch, index=_int_or_none(pairs, "INDEX"), name=pairs.get("NAME", "")
        )

    def select_arb(
        self,
        channel: ChannelLike,
        *,
        index: Optional[int] = None,
        name: Optional[str] = None,
        verify: bool = True,
    ) -> ArbSelection:
        """Pick the channel's arbitrary waveform: a built-in by ``index``
        (2–198) or a user waveform by ``name`` — exactly one of the two."""
        ch = self._check_channel(channel)
        if (index is None) == (name is None):
            raise SDG1032XValueError("give exactly one of index (built-in) or name (user)")
        if index is not None:
            if (
                isinstance(index, bool)
                or not isinstance(index, numbers.Integral)
                or not 2 <= int(index) <= 198
            ):
                raise SDG1032XValueError(f"index must be an int in 2..198, got {index!r}")
            self.write(f"C{ch}:ARWV INDEX,{int(index)}")
            got = self.get_arb(ch)
            self._verify(verify, ch, "index", int(index), got.index, "")
            return got
        nm = self._arb_name(name)
        self.write(f'C{ch}:ARWV NAME,"{nm}"')
        got = self.get_arb(ch)
        self._verify(verify, ch, "name", nm, got.name, "")
        return got

    def write_arb(
        self,
        name: str,
        samples: Sequence[Union[int, float]],
        *,
        frequency_hz: float = 1000.0,
        amplitude_vpp: float = 1.0,
        offset_v: float = 0.0,
        phase_deg: float = 0.0,
        verify: bool = True,
    ) -> ArbData:
        """Upload a user waveform (``WVDT``) and read it back.

        ``samples`` are either int16 codes (-32768..32767) or floats in
        [-1, 1] scaled to full range; numpy arrays work (through ``numbers``),
        numpy is never imported. 2..16384 samples. The frame is sent with
        ``write_raw`` and **no terminator** — a ``\\n`` would become a stray
        sample byte — and carries ``LENGTH`` so the message is self-delimiting.
        Verified by reading the waveform back and comparing the codes.
        """
        nm = self._arb_name(name)
        codes = _codes(samples)
        n = len(codes) // 2
        if not ARB_MIN_SAMPLES <= n <= ARB_MAX_SAMPLES:
            raise SDG1032XValueError(
                f"samples must number {ARB_MIN_SAMPLES}..{ARB_MAX_SAMPLES}, got {n}"
            )
        f = self._number(frequency_hz, "frequency_hz")
        a = self._number(amplitude_vpp, "amplitude_vpp")
        o = self._number(offset_v, "offset_v")
        p = self._number(phase_deg, "phase_deg")
        if self._max_amp is not None and a > self._max_amp:
            raise SDG1032XPolicyError(
                f"amplitude {a:g} Vpp exceeds max_amplitude_vpp={self._max_amp:g}"
            )
        header = (
            f"C1:WVDT WVNM,{nm},FREQ,{_fmt(f)},AMPL,{_fmt(a)},OFST,{_fmt(o)},"
            f"PHASE,{_fmt(p)},LENGTH,{len(codes)}B,WAVEDATA,"
        ).encode("ascii")
        if self._closed:
            raise SDG1032XConnectionError("instrument is closed")
        with self._transfer_timeout():
            try:
                self._inst.write_raw(header + codes)
            except Exception as e:
                raise SDG1032XConnectionError(f"write_arb({nm!r}) failed: {e}") from e
        got = self.read_arb(nm)
        if verify and got.codes[: len(codes)] != codes:
            raise SDG1032XVerifyError(
                f"write_arb: {nm!r} read back {len(got.codes) // 2} samples that differ from the "
                f"{n} sent (the instrument may have resampled or rejected the upload)",
                channel=None,
                field="codes",
                wanted=n,
                got=len(got.codes) // 2,
            )
        return got

    def read_arb(self, name: str) -> ArbData:
        """``WVDT? USER,<name>``: the text header up to ``WAVEDATA,`` then the
        raw int16 little-endian codes."""
        nm = self._arb_name(name)
        if self._closed:
            raise SDG1032XConnectionError("instrument is closed")
        with self._transfer_timeout():
            try:
                self._inst.write(f"WVDT? USER,{nm}")
                head = bytearray()
                while not head.endswith(b"WAVEDATA,"):
                    b = self._inst.read_bytes(1)
                    if not b:
                        break
                    head += b
                    if len(head) > 512:
                        raise SDG1032XProtocolError(
                            f"WVDT? header did not end: {bytes(head[:80])!r}"
                        )
            except SDG1032XError:
                raise
            except Exception as e:
                text = str(e).lower()
                if "timeout" in text or "pipe error" in text:
                    raise SDG1032XTimeoutError(
                        f"read_arb({nm!r}): no such waveform, or no reply"
                    ) from e
                raise SDG1032XConnectionError(f"read_arb({nm!r}) failed: {e}") from e
            text = head.decode("ascii", errors="replace")
            _, body = split_header(text, "WVDT")
            pairs, _ = parse_pairs(body.rsplit("WAVEDATA", 1)[0])
            length = pairs.get("LENGTH", "")
            m = re.match(r"^(\d+)\s*(B|KB)?$", length.strip().upper())
            if not m:
                raise SDG1032XProtocolError(f"WVDT? without a LENGTH: {text!r}")
            nbytes = int(m.group(1)) * (1024 if m.group(2) == "KB" else 1)
            data = bytearray()
            try:
                while len(data) < nbytes:
                    chunk = self._inst.read_bytes(min(nbytes - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
            except Exception as e:
                raise SDG1032XConnectionError(f"read_arb({nm!r}): payload read failed: {e}") from e
            self._drain_terminator()
        return ArbData(
            name=pairs.get("WVNM", nm),
            frequency_hz=_num(pairs, "FREQ"),
            amplitude_vpp=_num(pairs, "AMPL"),
            offset_v=_num(pairs, "OFST"),
            phase_deg=_num(pairs, "PHASE"),
            codes=bytes(data),
        )

    # ------------------------------------------------------------ modulation

    def get_modulation(self, channel: ChannelLike) -> Modulation:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:MDWV?")
        _, body = split_header(raw, "MDWV")
        tokens = [t.strip() for t in body.split(",")]
        # The guide prints ``C1:MDWV AM,STATE,ON,…``; the bench unit answers
        # ``C1:MDWV STATE,ON,AM,MDSP,…`` — the type is a bare token after the
        # state pair. Accept both: pull the type out wherever it stands.
        mtype: Optional[str] = None
        if tokens and tokens[0].upper() in MOD_TYPES:
            mtype = tokens[0].upper()
            tokens = tokens[1:]
        elif len(tokens) >= 3 and tokens[0].upper() == "STATE" and tokens[2].upper() in MOD_TYPES:
            mtype = tokens[2].upper()
            tokens = tokens[:2] + tokens[3:]
        pairs, carr = parse_pairs(",".join(tokens))
        enabled = _onoff(pairs, "STATE")
        if enabled is None:
            raise SDG1032XProtocolError(f"unexpected MDWV response: {raw!r}")
        return Modulation(
            channel=ch,
            enabled=enabled,
            type=mtype,
            source=_str(pairs, "SRC"),
            shape=_str(pairs, "MDSP"),
            frequency_hz=_num(pairs, "FRQ"),
            depth_pct=_num(pairs, "DEPTH"),
            deviation=_num(pairs, "DEVI"),
            key_frequency_hz=_num(pairs, "KFRQ"),
            hop_frequency_hz=_num(pairs, "HFRQ"),
            polarity=_str(pairs, "PLRT"),
            carrier=_carrier(carr),
        )

    def set_modulation(
        self,
        channel: ChannelLike,
        *,
        enabled: Optional[bool] = None,
        type: Optional[str] = None,  # noqa: A002 - matches the dataclass field
        source: Optional[str] = None,
        shape: Optional[str] = None,
        frequency_hz: Optional[float] = None,
        depth_pct: Optional[float] = None,
        deviation: Optional[float] = None,
        key_frequency_hz: Optional[float] = None,
        hop_frequency_hz: Optional[float] = None,
        polarity: Optional[str] = None,
        verify: bool = True,
    ) -> Modulation:
        """Configure modulation. ``enabled`` is sent first (the instrument
        only accepts the other fields while STATE is ON), then ``type``, then
        the type's parameters as ``<TYPE>,<KEY>,<value>``. Enabling
        modulation turns sweep and burst off — read back reflects that."""
        ch = self._check_channel(channel)
        cmds: list[str] = []
        wanted: dict[str, Any] = {}
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise SDG1032XValueError("enabled must be a bool")
            cmds.append(f"STATE,{_fmt(enabled)}")
            wanted["enabled"] = enabled
        mtype = None
        if type is not None:
            mtype = str(type).upper()
            if mtype not in MOD_TYPES:
                raise SDG1032XValueError(f"type must be one of {MOD_TYPES}, got {type!r}")
            cmds.append(mtype)
            wanted["type"] = mtype
        for name, value in (
            ("source", source),
            ("shape", shape),
            ("frequency_hz", frequency_hz),
            ("depth_pct", depth_pct),
            ("deviation", deviation),
            ("key_frequency_hz", key_frequency_hz),
            ("hop_frequency_hz", hop_frequency_hz),
            ("polarity", polarity),
        ):
            if value is None:
                continue
            key, unit = _MDWV_FIELDS[name]
            v: Any = (
                str(value).upper()
                if unit == "" and isinstance(value, str)
                else self._number(value, name)
            )
            wanted[name] = v
            if mtype is None:
                mtype = self.get_modulation(ch).type
                if mtype is None:
                    raise SDG1032XValueError(f"{name}: no modulation type selected — pass type=")
            cmds.append(f"{mtype},{key},{_fmt(v)}")
        if not cmds:
            raise SDG1032XValueError("set_modulation: nothing to set")
        for c in cmds:
            self.write(f"C{ch}:MDWV {c}")
        got = self.get_modulation(ch)
        for name, value in wanted.items():
            unit = _MDWV_FIELDS.get(name, ("", ""))[1]
            self._verify(verify, ch, name, value, getattr(got, name), unit)
        return got

    # ------------------------------------------------------------ sweep

    def get_sweep(self, channel: ChannelLike) -> Sweep:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:SWWV?")
        _, body = split_header(raw, "SWWV")
        pairs, carr = parse_pairs(body)
        enabled = _onoff(pairs, "STATE")
        if enabled is None:
            raise SDG1032XProtocolError(f"unexpected SWWV response: {raw!r}")
        return Sweep(
            channel=ch,
            enabled=enabled,
            time_s=_num(pairs, "TIME"),
            start_hold_s=_num(pairs, "STARTTIME"),
            end_hold_s=_num(pairs, "ENDTIME"),
            return_s=_num(pairs, "BACKTIME"),
            start_hz=_num(pairs, "START"),
            stop_hz=_num(pairs, "STOP"),
            center_hz=_num(pairs, "CENTER"),
            span_hz=_num(pairs, "SPAN"),
            mode=_str(pairs, "SWMD"),
            direction=_str(pairs, "DIR"),
            symmetry_pct=_num(pairs, "SYM"),
            trigger_source=_str(pairs, "TRSR"),
            trigger_out=_onoff(pairs, "TRMD"),
            trigger_edge=_str(pairs, "EDGE"),
            mark_enabled=_onoff(pairs, "MARK_STATE"),
            mark_hz=_num(pairs, "MARK_FREQ"),
            carrier=_carrier(carr),
        )

    def set_sweep(
        self,
        channel: ChannelLike,
        *,
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
    ) -> Sweep:
        """Configure a frequency sweep (``enabled`` first: the instrument only
        takes the other fields while STATE is ON). Turns modulation and burst off."""
        ch = self._check_channel(channel)
        wanted = self._collect(
            _SWWV_FIELDS,
            enabled=enabled,
            time_s=time_s,
            start_hold_s=start_hold_s,
            end_hold_s=end_hold_s,
            return_s=return_s,
            start_hz=start_hz,
            stop_hz=stop_hz,
            center_hz=center_hz,
            span_hz=span_hz,
            mode=(mode, SWEEP_MODES),
            direction=(direction, SWEEP_DIRECTIONS),
            symmetry_pct=symmetry_pct,
            trigger_source=(trigger_source, TRIGGER_SOURCES),
            trigger_out=trigger_out,
            trigger_edge=(trigger_edge, ("RISE", "FALL")),
            mark_enabled=mark_enabled,
            mark_hz=mark_hz,
        )
        self._send_state_first(f"C{ch}:SWWV", _SWWV_FIELDS, wanted)
        got = self.get_sweep(ch)
        for name, value in wanted.items():
            unit = _SWWV_FIELDS.get(name, ("", ""))[1]
            self._verify(verify, ch, name, value, getattr(got, name), unit)
        return got

    def trigger_sweep(self, channel: ChannelLike) -> None:
        """Manual trigger (``MTRIG``); only meaningful with ``trigger_source="MAN"``.
        Nothing to read back."""
        ch = self._check_channel(channel)
        self.write(f"C{ch}:SWWV MTRIG")

    # ------------------------------------------------------------ burst

    def get_burst(self, channel: ChannelLike) -> Burst:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:BTWV?")
        _, body = split_header(raw, "BTWV")
        pairs, carr = parse_pairs(body)
        enabled = _onoff(pairs, "STATE")
        if enabled is None:
            raise SDG1032XProtocolError(f"unexpected BTWV response: {raw!r}")
        cycles_tok = pairs.get("TIME")
        cycles: Optional[int]
        if cycles_tok is None or cycles_tok == "" or cycles_tok.upper() == "INF":
            cycles = None
        else:
            cycles = int(parse_number(cycles_tok)[0])
        return Burst(
            channel=ch,
            enabled=enabled,
            period_s=_num(pairs, "PRD"),
            start_phase_deg=_num(pairs, "STPS"),
            mode=_str(pairs, "GATE_NCYC"),
            trigger_source=_str(pairs, "TRSR"),
            trigger_out=_str(pairs, "TRMD"),
            trigger_edge=_str(pairs, "EDGE"),
            delay_s=_num(pairs, "DLAY"),
            polarity=_str(pairs, "PLRT"),
            cycles=cycles,
            carrier=_carrier(carr),
        )

    def set_burst(
        self,
        channel: ChannelLike,
        *,
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
    ) -> Burst:
        """Configure burst (``enabled`` first). ``cycles`` is an int or ``"INF"``."""
        ch = self._check_channel(channel)
        cyc: Any = None
        if cycles is not None:
            if isinstance(cycles, str):
                if cycles.upper() != "INF":
                    raise SDG1032XValueError("cycles must be an int or 'INF'")
                cyc = "INF"
            elif (
                isinstance(cycles, bool)
                or not isinstance(cycles, numbers.Integral)
                or int(cycles) < 1
            ):
                raise SDG1032XValueError("cycles must be an int >= 1 or 'INF'")
            else:
                cyc = int(cycles)
        wanted = self._collect(
            _BTWV_FIELDS,
            _allow_empty=cyc is not None,
            enabled=enabled,
            period_s=period_s,
            start_phase_deg=start_phase_deg,
            mode=(mode, BURST_MODES),
            trigger_source=(trigger_source, TRIGGER_SOURCES),
            trigger_out=(trigger_out, ("RISE", "FALL", "OFF")),
            trigger_edge=(trigger_edge, ("RISE", "FALL")),
            delay_s=delay_s,
            polarity=(polarity, ("NEG", "POS")),
        )
        if cyc is not None:
            wanted["cycles"] = cyc
        self._send_state_first(f"C{ch}:BTWV", _BTWV_FIELDS, wanted)
        got = self.get_burst(ch)
        for name, value in wanted.items():
            if name == "cycles":
                want_c = None if value == "INF" else value
                self._verify(
                    verify,
                    ch,
                    name,
                    want_c if want_c is not None else "INF",
                    got.cycles if got.cycles is not None else "INF",
                    "",
                )
                continue
            unit = _BTWV_FIELDS.get(name, ("", ""))[1]
            self._verify(verify, ch, name, value, getattr(got, name), unit)
        return got

    def trigger_burst(self, channel: ChannelLike) -> None:
        """Manual burst trigger (``MTRIG``). Nothing to read back."""
        ch = self._check_channel(channel)
        self.write(f"C{ch}:BTWV MTRIG")

    # ------------------------------------------------------------ sync / clock / phase / channel

    def get_sync(self, channel: ChannelLike) -> SyncConfig:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:SYNC?")
        _, body = split_header(raw, "SYNC")
        tokens = [t.strip() for t in body.split(",")]
        if not tokens or tokens[0].upper() not in ("ON", "OFF"):
            raise SDG1032XProtocolError(f"unexpected SYNC response: {raw!r}")
        pairs, _ = parse_pairs(",".join(tokens[1:]))
        return SyncConfig(channel=ch, enabled=_bool(tokens[0]), type=_str(pairs, "TYPE"))

    def set_sync(
        self,
        channel: ChannelLike,
        on: bool,
        *,
        type: Optional[str] = None,
        verify: bool = True,  # noqa: A002
    ) -> SyncConfig:
        ch = self._check_channel(channel)
        if not isinstance(on, bool):
            raise SDG1032XValueError("on must be a bool")
        cmd = f"C{ch}:SYNC {_fmt(on)}"
        if type is not None:
            t = str(type).upper()
            if t not in SYNC_TYPES:
                raise SDG1032XValueError(f"type must be one of {SYNC_TYPES}")
            cmd += f",TYPE,{t}"
        self.write(cmd)
        got = self.get_sync(ch)
        self._verify(verify, ch, "enabled", on, got.enabled, "")
        # Bench finding: ``SYNC?`` answers ``C1:SYNC ON`` without the TYPE, so
        # the source can only be verified on firmware that echoes it.
        if type is not None and on and got.type is not None:
            self._verify(verify, ch, "type", str(type).upper(), got.type, "")
        return got

    def get_clock(self) -> ClockConfig:
        raw = self.query("ROSC?")
        _, body = split_header(raw, "ROSC")
        tokens = [t.strip() for t in body.split(",")]
        if not tokens or tokens[0].upper() not in ("INT", "EXT"):
            raise SDG1032XProtocolError(f"unexpected ROSC response: {raw!r}")
        pairs, _ = parse_pairs(",".join(tokens[1:]))
        return ClockConfig(source=tokens[0].upper(), output_10m=_onoff(pairs, "10MOUT"))

    def set_clock(
        self,
        *,
        source: Optional[str] = None,
        output_10m: Optional[bool] = None,
        verify: bool = True,
    ) -> ClockConfig:
        """Reference clock source (INT/EXT) and the 10 MHz output. The bench
        unit does not report ``10MOUT`` in its read-back, so that field is
        verified only when the instrument echoes it."""
        if source is None and output_10m is None:
            raise SDG1032XValueError("set_clock: nothing to set")
        if source is not None:
            s = str(source).upper()
            if s not in ("INT", "EXT"):
                raise SDG1032XValueError("source must be INT or EXT")
            self.write(f"ROSC {s}")
        if output_10m is not None:
            if not isinstance(output_10m, bool):
                raise SDG1032XValueError("output_10m must be a bool")
            self.write(f"ROSC 10MOUT,{_fmt(output_10m)}")
        got = self.get_clock()
        if source is not None:
            self._verify(verify, None, "source", str(source).upper(), got.source, "")
        if output_10m is not None and got.output_10m is not None:
            self._verify(verify, None, "output_10m", output_10m, got.output_10m, "")
        return got

    def get_phase_mode(self) -> str:
        """``PHASELOCKED`` or ``INDEPENDENT`` (the instrument spells the read-back
        ``PHASE-LOCKED``; normalised here)."""
        raw = self.query("MODE?")
        _, body = split_header(raw, "MODE")
        mode = body.strip().upper().replace("-", "")
        if mode not in PHASE_MODES:
            raise SDG1032XProtocolError(f"unexpected MODE response: {raw!r}")
        return mode

    def set_phase_mode(self, mode: str, *, verify: bool = True) -> str:
        m = str(mode).upper().replace("-", "")
        if m not in PHASE_MODES:
            raise SDG1032XValueError(f"mode must be one of {PHASE_MODES}")
        # Bench finding: the guide's ``PHASELOCKED`` is silently ignored; the
        # firmware takes (and reports) ``PHASE-LOCKED``. INDEPENDENT is as written.
        self.write(f"MODE {'PHASE-LOCKED' if m == 'PHASELOCKED' else m}")
        got = self.get_phase_mode()
        self._verify(verify, None, "phase_mode", m, got, "")
        return got

    def apply_equal_phase(self) -> None:
        """``EQPHASE`` — align the two channels' phases. Nothing to read back."""
        self.write("EQPHASE")

    def get_invert(self, channel: ChannelLike) -> bool:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:INVT?")
        _, body = split_header(raw, "INVT")
        return _bool(body)

    def set_invert(self, channel: ChannelLike, on: bool, *, verify: bool = True) -> bool:
        ch = self._check_channel(channel)
        if not isinstance(on, bool):
            raise SDG1032XValueError("on must be a bool")
        self.write(f"C{ch}:INVT {_fmt(on)}")
        got = self.get_invert(ch)
        self._verify(verify, ch, "invert", on, got, "")
        return got

    def apply_channel_copy(
        self, source: ChannelLike, target: ChannelLike, *, verify: bool = True
    ) -> BasicWave:
        """``PACP`` — copy the source channel's parameters onto the target.
        Verified by comparing the target's basic wave with the source's."""
        src = self._coerce_channel(source)
        dst = self._check_channel(target)
        if src == dst:
            raise SDG1032XValueError("source and target must differ")
        self.write(f"PACP C{dst},C{src}")
        want = self.get_basic_wave(src)
        got = self.get_basic_wave(dst)
        for name, (_, unit) in _BSWV_FIELDS.items():
            if name == "max_amplitude_vpp":
                continue
            w = getattr(want, name)
            if w is not None:
                self._verify(verify, dst, name, w, getattr(got, name), unit)
        return got

    # ------------------------------------------------------------ coupling / harmonics / combine

    def get_coupling(self) -> Coupling:
        raw = self.query("COUP?")
        _, body = split_header(raw, "COUP")
        pairs, _ = parse_pairs(body)
        trace = _onoff(pairs, "TRACE")
        if trace is None:
            raise SDG1032XProtocolError(f"unexpected COUP response: {raw!r}")
        return Coupling(
            trace=trace,
            freq_coupled=_onoff(pairs, "FCOUP"),
            freq_deviation_hz=_num(pairs, "FDEV"),
            freq_ratio=_num(pairs, "FRAT"),
            phase_coupled=_onoff(pairs, "PCOUP"),
            phase_deviation_deg=_num(pairs, "PDEV"),
            phase_ratio=_num(pairs, "PRAT"),
            amp_coupled=_onoff(pairs, "ACOUP"),
            amp_ratio=_num(pairs, "ARAT"),
            amp_deviation_v=_num(pairs, "ADEV"),
        )

    def set_coupling(
        self,
        *,
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
    ) -> Coupling:
        """Channel coupling/tracking. The SDG1000X has no ``STATE``/``BSCH``:
        use ``trace`` plus the per-axis ``*_coupled`` flags. A deviation and a
        ratio on the same axis are mutually exclusive in the instrument; the
        read-back shows which one it kept."""
        keys: dict[str, tuple[str, str]] = {
            "trace": ("TRACE", ""),
            "freq_coupled": ("FCOUP", ""),
            "freq_deviation_hz": ("FDEV", "Hz"),
            "freq_ratio": ("FRAT", ""),
            "phase_coupled": ("PCOUP", ""),
            "phase_deviation_deg": ("PDEV", "deg"),
            "phase_ratio": ("PRAT", ""),
            "amp_coupled": ("ACOUP", ""),
            "amp_ratio": ("ARAT", ""),
            "amp_deviation_v": ("ADEV", "V"),
        }
        given = {
            "trace": trace,
            "freq_coupled": freq_coupled,
            "freq_deviation_hz": freq_deviation_hz,
            "freq_ratio": freq_ratio,
            "phase_coupled": phase_coupled,
            "phase_deviation_deg": phase_deviation_deg,
            "phase_ratio": phase_ratio,
            "amp_coupled": amp_coupled,
            "amp_ratio": amp_ratio,
            "amp_deviation_v": amp_deviation_v,
        }
        wanted: dict[str, Any] = {}
        for name, value in given.items():
            if value is None:
                continue
            if keys[name][1] == "" and name.endswith(("coupled", "trace")):
                if not isinstance(value, bool):
                    raise SDG1032XValueError(f"{name} must be a bool")
                wanted[name] = value
            else:
                wanted[name] = self._number(value, name)
        if not wanted:
            raise SDG1032XValueError("set_coupling: nothing to set")
        for name, value in wanted.items():
            self.write(f"COUP {keys[name][0]},{_fmt(value)}")
        got = self.get_coupling()
        for name, value in wanted.items():
            self._verify(verify, None, name, value, getattr(got, name), keys[name][1])
        return got

    def get_harmonics(self, channel: ChannelLike) -> Harmonic:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:HARM?")
        _, body = split_header(raw, "HARM")
        pairs, _ = parse_pairs(body)
        enabled = _onoff(pairs, "HARMSTATE")
        if enabled is None:
            raise SDG1032XProtocolError(f"unexpected HARM response: {raw!r}")
        return Harmonic(
            channel=ch,
            enabled=enabled,
            type=_str(pairs, "HARMTYPE"),
            order=_int_or_none(pairs, "HARMORDER"),
            amplitude_v=_num(pairs, "HARMAMP"),
            amplitude_dbc=_num(pairs, "HARMDBC"),
            phase_deg=_num(pairs, "HARMPHASE"),
        )

    def set_harmonics(
        self,
        channel: ChannelLike,
        *,
        enabled: Optional[bool] = None,
        type: Optional[str] = None,  # noqa: A002
        order: Optional[int] = None,
        amplitude_v: Optional[float] = None,
        amplitude_dbc: Optional[float] = None,
        phase_deg: Optional[float] = None,
        verify: bool = True,
    ) -> Harmonic:
        """Harmonic generator (sine only). ``order`` selects which harmonic the
        amplitude/phase apply to; give ``amplitude_v`` *or* ``amplitude_dbc``."""
        ch = self._check_channel(channel)
        if amplitude_v is not None and amplitude_dbc is not None:
            raise SDG1032XValueError("give amplitude_v or amplitude_dbc, not both")
        parts: list[str] = []
        wanted: dict[str, tuple[Any, str]] = {}
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise SDG1032XValueError("enabled must be a bool")
            parts.append(f"HARMSTATE,{_fmt(enabled)}")
            wanted["enabled"] = (enabled, "")
        if type is not None:
            t = str(type).upper()
            if t not in HARMONIC_TYPES:
                raise SDG1032XValueError(f"type must be one of {HARMONIC_TYPES}")
            parts.append(f"HARMTYPE,{t}")
            wanted["type"] = (t, "")
        if order is not None:
            if isinstance(order, bool) or not isinstance(order, numbers.Integral) or int(order) < 1:
                raise SDG1032XValueError("order must be an int >= 1")
            parts.append(f"HARMORDER,{int(order)}")
            wanted["order"] = (int(order), "")
        if amplitude_v is not None:
            v = self._number(amplitude_v, "amplitude_v")
            parts.append(f"HARMAMP,{_fmt(v)}")
            wanted["amplitude_v"] = (v, "V")
        if amplitude_dbc is not None:
            v = self._number(amplitude_dbc, "amplitude_dbc")
            parts.append(f"HARMDBC,{_fmt(v)}")
            wanted["amplitude_dbc"] = (v, "")
        if phase_deg is not None:
            v = self._number(phase_deg, "phase_deg")
            parts.append(f"HARMPHASE,{_fmt(v)}")
            wanted["phase_deg"] = (v, "deg")
        if not parts:
            raise SDG1032XValueError("set_harmonics: nothing to set")
        self.write(f"C{ch}:HARM " + ",".join(parts))
        got = self.get_harmonics(ch)
        for name, (value, unit) in wanted.items():
            self._verify(verify, ch, name, value, getattr(got, name), unit)
        return got

    def get_combine(self, channel: ChannelLike) -> bool:
        ch = self._coerce_channel(channel)
        raw = self.query(f"C{ch}:CMBN?")
        _, body = split_header(raw, "CMBN")
        return _bool(body)

    def set_combine(self, channel: ChannelLike, on: bool, *, verify: bool = True) -> bool:
        """Waveform combining (this channel + the other). With it on, the
        SDG1000X refuses ``SQUARE`` as the wave type."""
        ch = self._check_channel(channel)
        if not isinstance(on, bool):
            raise SDG1032XValueError("on must be a bool")
        self.write(f"C{ch}:CMBN {_fmt(on)}")
        got = self.get_combine(ch)
        self._verify(verify, ch, "combine", on, got, "")
        return got

    # ------------------------------------------------------------ frequency counter

    def read_counter(self) -> CounterReading:
        """``FCNT?`` — the rear-panel counter. Off, it answers ``STATE,OFF`` only."""
        raw = self.query("FCNT?")
        _, body = split_header(raw, "FCNT")
        pairs, _ = parse_pairs(body)
        enabled = _onoff(pairs, "STATE")
        if enabled is None:
            raise SDG1032XProtocolError(f"unexpected FCNT response: {raw!r}")
        return CounterReading(
            enabled=enabled,
            frequency_hz=_num(pairs, "FRQ"),
            duty_pct=_num(pairs, "DUTY"),
            reference_hz=_num(pairs, "REFQ"),
            trigger_level_v=_num(pairs, "TRG"),
            pulse_width_s=_num(pairs, "PW"),
            negative_width_s=_num(pairs, "NW"),
            deviation_ppm=_num(pairs, "FRQDEV"),
            coupling=_str(pairs, "MODE"),
            high_frequency_reject=_onoff(pairs, "HFR"),
        )

    def set_counter(
        self,
        *,
        enabled: Optional[bool] = None,
        reference_hz: Optional[float] = None,
        trigger_level_v: Optional[float] = None,
        coupling: Optional[str] = None,
        high_frequency_reject: Optional[bool] = None,
        verify: bool = True,
    ) -> CounterReading:
        cmds: list[str] = []
        wanted: dict[str, tuple[Any, str]] = {}
        if enabled is not None:
            if not isinstance(enabled, bool):
                raise SDG1032XValueError("enabled must be a bool")
            cmds.append(f"STATE,{_fmt(enabled)}")
            wanted["enabled"] = (enabled, "")
        if reference_hz is not None:
            v = self._number(reference_hz, "reference_hz")
            cmds.append(f"REFQ,{_fmt(v)}")
            wanted["reference_hz"] = (v, "Hz")
        if trigger_level_v is not None:
            v = self._number(trigger_level_v, "trigger_level_v")
            cmds.append(f"TRG,{_fmt(v)}")
            wanted["trigger_level_v"] = (v, "V")
        if coupling is not None:
            c = str(coupling).upper()
            if c not in ("AC", "DC"):
                raise SDG1032XValueError("coupling must be AC or DC")
            cmds.append(f"MODE,{c}")
            wanted["coupling"] = (c, "")
        if high_frequency_reject is not None:
            if not isinstance(high_frequency_reject, bool):
                raise SDG1032XValueError("high_frequency_reject must be a bool")
            cmds.append(f"HFR,{_fmt(high_frequency_reject)}")
            wanted["high_frequency_reject"] = (high_frequency_reject, "")
        if not cmds:
            raise SDG1032XValueError("set_counter: nothing to set")
        for c in cmds:
            self.write(f"FCNT {c}")
        got = self.read_counter()
        for name, (value, unit) in wanted.items():
            if name != "enabled" and not got.enabled:
                continue  # the instrument reports nothing else while off
            self._verify(verify, None, name, value, getattr(got, name), unit)
        return got

    # ------------------------------------------------------------ protection

    def get_protection(self) -> ProtectionState:
        """Over-voltage protection state (``VOLTPRT?`` answers a bare ``ON``/``OFF``
        on the bench unit). Over-current protection is not implemented on this
        firmware (``CURRPRT?`` goes unanswered), so it is not exposed."""
        raw = self.query("VOLTPRT?")
        _, body = split_header(raw, "VOLTPRT")
        return ProtectionState(over_voltage=_bool(body))

    def set_protection(self, *, over_voltage: bool, verify: bool = True) -> ProtectionState:
        if not isinstance(over_voltage, bool):
            raise SDG1032XValueError("over_voltage must be a bool")
        self.write(f"VOLTPRT {_fmt(over_voltage)}")
        got = self.get_protection()
        self._verify(verify, None, "over_voltage", over_voltage, got.over_voltage, "")
        return got

    # ------------------------------------------------------------ system / screen / keys

    def read_screen(self) -> bytes:
        """``SCDP`` — the instrument's own screen as a BMP (480x272, 24-bit,
        391734 bytes on the bench unit, ~450 ms). The size is taken from the
        BMP header and the transfer read in chunks until complete; the stray
        newline the firmware appends is drained."""
        if self._closed:
            raise SDG1032XConnectionError("instrument is closed")
        with self._transfer_timeout():
            try:
                self._inst.write("SCDP")
                data = bytearray(self._inst.read_raw())
            except Exception as e:
                text = str(e).lower()
                if "timeout" in text or "pipe error" in text:
                    raise SDG1032XTimeoutError("SCDP went unanswered") from e
                raise SDG1032XConnectionError(f"read_screen failed: {e}") from e
            if len(data) < 14 or data[:2] != b"BM":
                raise SDG1032XProtocolError(f"SCDP did not return a BMP: {bytes(data[:16])!r}")
            declared = struct.unpack("<I", bytes(data[2:6]))[0]
            try:
                while len(data) < declared:
                    chunk = self._inst.read_bytes(min(declared - len(data), 65536))
                    if not chunk:
                        break
                    data += chunk
            except Exception as e:
                raise SDG1032XConnectionError(f"read_screen: image read failed: {e}") from e
            if len(data) < declared:
                raise SDG1032XProtocolError(f"SCDP short read: {len(data)} of {declared} bytes")
            self._drain_terminator()
        return bytes(data[:declared])

    def get_buzzer(self) -> bool:
        raw = self.query("BUZZ?")
        _, body = split_header(raw, "BUZZ")
        return _bool(body)

    def set_buzzer(self, on: bool, *, verify: bool = True) -> bool:
        if not isinstance(on, bool):
            raise SDG1032XValueError("on must be a bool")
        self.write(f"BUZZ {_fmt(on)}")
        got = self.get_buzzer()
        self._verify(verify, None, "buzzer", on, got, "")
        return got

    def get_screen_saver(self) -> int:
        """Minutes until the screen saver, 0 = off (the read-back is ``5MIN``/``OFF``)."""
        raw = self.query("SCSV?")
        _, body = split_header(raw, "SCSV")
        if body.strip().upper() == "OFF":
            return 0
        return int(parse_number(body)[0])

    def set_screen_saver(self, minutes: int, *, verify: bool = True) -> int:
        if (
            isinstance(minutes, bool)
            or not isinstance(minutes, numbers.Integral)
            or int(minutes) not in SCREEN_SAVER_MINUTES
        ):
            raise SDG1032XValueError(f"minutes must be one of {SCREEN_SAVER_MINUTES}")
        self.write(f"SCSV {'OFF' if int(minutes) == 0 else int(minutes)}")
        got = self.get_screen_saver()
        self._verify(verify, None, "screen_saver", int(minutes), got, "")
        return got

    def get_number_format(self) -> NumberFormat:
        raw = self.query("NBFM?")
        _, body = split_header(raw, "NBFM")
        pairs, _ = parse_pairs(body)
        if "PNT" not in pairs:
            raise SDG1032XProtocolError(f"unexpected NBFM response: {raw!r}")
        return NumberFormat(point=pairs["PNT"].upper(), separator=pairs.get("SEPT", "").upper())

    def set_number_format(
        self, *, point: Optional[str] = None, separator: Optional[str] = None, verify: bool = True
    ) -> NumberFormat:
        if point is None and separator is None:
            raise SDG1032XValueError("set_number_format: nothing to set")
        if point is not None:
            p = str(point).upper()
            if p not in ("DOT", "COMMA"):
                raise SDG1032XValueError("point must be DOT or COMMA")
            self.write(f"NBFM PNT,{p}")
        if separator is not None:
            s = str(separator).upper()
            if s not in ("SPACE", "OFF", "ON"):
                raise SDG1032XValueError("separator must be SPACE, OFF or ON")
            self.write(f"NBFM SEPT,{s}")
        got = self.get_number_format()
        if point is not None:
            self._verify(verify, None, "point", str(point).upper(), got.point, "")
        if separator is not None:
            self._verify(verify, None, "separator", str(separator).upper(), got.separator, "")
        return got

    def get_language(self) -> str:
        raw = self.query("LAGG?")
        _, body = split_header(raw, "LAGG")
        return body.strip().upper()

    def set_language(self, language: str, *, verify: bool = True) -> str:
        lang = str(language).upper()
        if lang not in LANGUAGES:
            raise SDG1032XValueError(f"language must be one of {LANGUAGES} on the SDG1000X")
        self.write(f"LAGG {lang}")
        got = self.get_language()
        self._verify(verify, None, "language", lang, got, "")
        return got

    def get_power_on_config(self) -> str:
        raw = self.query("SCFG?")
        _, body = split_header(raw, "SCFG")
        return body.strip().upper()

    def set_power_on_config(self, mode: str, *, verify: bool = True) -> str:
        """What the instrument restores at power-on: DEFAULT, LAST or USER."""
        m = str(mode).upper()
        if m not in POWER_ON_CONFIGS:
            raise SDG1032XValueError(f"mode must be one of {POWER_ON_CONFIGS}")
        self.write(f"SCFG {m}")
        got = self.get_power_on_config()
        self._verify(verify, None, "power_on_config", m, got, "")
        return got

    def get_lan_config(self) -> LanConfig:
        """The three LAN queries answer a quoted dotted quad each, no header."""
        vals = []
        for q in ("SYST:COMM:LAN:IPAD?", "SYST:COMM:LAN:SMAS?", "SYST:COMM:LAN:GAT?"):
            raw = self.query(q)
            vals.append(raw.strip().strip('"') or None)
        return LanConfig(ip=vals[0], mask=vals[1], gateway=vals[2])

    def set_lan_config(
        self,
        *,
        ip: Optional[str] = None,
        mask: Optional[str] = None,
        gateway: Optional[str] = None,
        verify: bool = True,
    ) -> LanConfig:
        if ip is None and mask is None and gateway is None:
            raise SDG1032XValueError("set_lan_config: nothing to set")
        for name, value, cmd in (
            ("ip", ip, "IPAD"),
            ("mask", mask, "SMAS"),
            ("gateway", gateway, "GAT"),
        ):
            if value is None:
                continue
            if not re.match(r"^\d{1,3}(\.\d{1,3}){3}$", str(value)):
                raise SDG1032XValueError(f"{name} must be a dotted quad, got {value!r}")
            self.write(f'SYST:COMM:LAN:{cmd} "{value}"')
        got = self.get_lan_config()
        for name, value in (("ip", ip), ("mask", mask), ("gateway", gateway)):
            if value is not None:
                self._verify(verify, None, name, str(value), getattr(got, name), "")
        return got

    def trigger_key(self, key: str) -> None:
        """Press a front-panel key (``VKEY``). The two Output keys are refused:
        they would energise a channel outside the governor's arming watch —
        use :py:meth:`set_output`. Nothing to read back."""
        k = str(key).upper()
        if k in OUTPUT_KEYS:
            raise SDG1032XPolicyError(f"{k} energises a channel — use set_output(channel, True)")
        if k not in VIRTUAL_KEYS:
            raise SDG1032XValueError(f"unknown key {key!r}; one of {sorted(VIRTUAL_KEYS)}")
        self.write(f"VKEY VALUE,{k},STATE,1")

    # ------------------------------------------------------------ internals

    @staticmethod
    def _coerce_channel(channel: ChannelLike) -> int:
        if isinstance(channel, bool):
            raise SDG1032XValueError(f"channel must be 1 or 2, got bool {channel!r}")
        if isinstance(channel, str):
            m = re.match(r"^(?:C|CH)?([12])$", channel.strip().upper())
            if not m:
                raise SDG1032XValueError(f"channel must be 1 or 2 (or 'C1'/'C2'), got {channel!r}")
            return int(m.group(1))
        if isinstance(channel, numbers.Integral) and int(channel) in CHANNELS:
            return int(channel)
        raise SDG1032XValueError(f"channel must be 1 or 2, got {channel!r}")

    def _check_channel(self, channel: ChannelLike) -> int:
        ch = self._coerce_channel(channel)
        if ch not in self._allowed:
            raise SDG1032XPolicyError(
                f"channel {ch} is outside allowed_channels={self._allowed} — a wiring decision, "
                "made at open()/in agent.json, not here"
            )
        return ch

    @staticmethod
    def _number(value: Any, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, numbers.Real):
            raise SDG1032XValueError(f"{name} must be a number, got {value!r}")
        v = float(value)
        if math.isnan(v) or math.isinf(v):
            raise SDG1032XValueError(f"{name} must be finite, got {value!r}")
        return v

    def _check_limits(self, wanted: dict[str, Any]) -> None:
        f = wanted.get("frequency_hz")
        if f is not None:
            ceiling = FREQ_MAX_HZ.get(wanted.get("wave_type", "SINE"), 30e6)
            if f <= 0 or f > ceiling:
                raise SDG1032XValueError(
                    f"frequency_hz {f:g} is outside (0, {ceiling:g}] for this waveform"
                )
        for key in ("amplitude_vpp", "high_level_v", "low_level_v"):
            v = wanted.get(key)
            if v is None:
                continue
            if key == "amplitude_vpp" and (v <= 0 or v > AMPLITUDE_MAX_VPP_HIZ):
                raise SDG1032XValueError(
                    f"amplitude_vpp must be in (0, {AMPLITUDE_MAX_VPP_HIZ:g}], got {v:g}"
                )
            if key == "amplitude_vpp" and self._max_amp is not None and v > self._max_amp:
                raise SDG1032XPolicyError(
                    f"amplitude {v:g} Vpp exceeds max_amplitude_vpp={self._max_amp:g} set at open()"
                )
            if key != "amplitude_vpp" and abs(v) > OFFSET_MAX_V + AMPLITUDE_MAX_VPP_HIZ / 2:
                raise SDG1032XValueError(f"{key} {v:g} V is beyond the output range")
        o = wanted.get("offset_v")
        if o is not None and abs(o) > OFFSET_MAX_V:
            raise SDG1032XValueError(f"offset_v must be within ±{OFFSET_MAX_V:g} V, got {o:g}")
        for key in ("phase_deg", "duty_pct", "symmetry_pct"):
            v = wanted.get(key)
            hi = 360.0 if key == "phase_deg" else 100.0
            if v is not None and not 0 <= v <= hi:
                raise SDG1032XValueError(f"{key} must be in [0, {hi:g}], got {v:g}")

    def _collect(
        self, table: dict[str, tuple[str, str]], *, _allow_empty: bool = False, **given: Any
    ) -> dict[str, Any]:
        """Validate a setter's keyword arguments against ``table``; enum
        arguments arrive as ``(value, choices)`` tuples."""
        wanted: dict[str, Any] = {}
        for name, value in given.items():
            choices: Optional[tuple[str, ...]] = None
            if isinstance(value, tuple):
                value, choices = value
            if value is None:
                continue
            if name == "enabled" or (choices is None and isinstance(value, bool)):
                if not isinstance(value, bool):
                    raise SDG1032XValueError(f"{name} must be a bool")
                wanted[name] = value
            elif choices is not None:
                v = str(value).upper()
                if v not in choices:
                    raise SDG1032XValueError(f"{name} must be one of {choices}, got {value!r}")
                wanted[name] = v
            else:
                wanted[name] = self._number(value, name)
        if not wanted and not _allow_empty:
            raise SDG1032XValueError("nothing to set")
        return wanted

    def _send_state_first(
        self, prefix: str, table: dict[str, tuple[str, str]], wanted: dict[str, Any]
    ) -> None:
        if "enabled" in wanted:
            self.write(f"{prefix} STATE,{_fmt(wanted['enabled'])}")
        rest = [f"{table[n][0]},{_fmt(v)}" for n, v in wanted.items() if n != "enabled"]
        if rest:
            self.write(f"{prefix} " + ",".join(rest))

    def _verify(
        self, verify: bool, channel: Optional[int], field: str, wanted: Any, got: Any, unit: str
    ) -> None:
        if _close(wanted, got, unit):
            return
        msg = (
            f"{f'C{channel} ' if channel else ''}{field}: asked for {wanted!r}, the instrument "
            f"reads back {got!r} — the SDG1032X has no error queue; a value outside the range "
            "for the current waveform/load is silently refused or quantised"
        )
        if verify:
            raise SDG1032XVerifyError(msg, channel=channel, field=field, wanted=wanted, got=got)
        log.warning("sdg1032x: %s (verify=False, accepted)", msg)

    @staticmethod
    def _arb_name(name: Any) -> str:
        if not isinstance(name, str) or not ARB_NAME_RE.match(name):
            raise SDG1032XValueError(
                f"waveform name must match {ARB_NAME_RE.pattern}, got {name!r}"
            )
        return name

    @contextlib.contextmanager
    def _transfer_timeout(self):
        old = getattr(self._inst, "timeout", self._base_timeout_ms)
        try:
            self._inst.timeout = max(int(old or 0), TRANSFER_TIMEOUT_MS)
            yield
        finally:
            self._inst.timeout = old

    def _drain_terminator(self) -> None:
        """Best effort: swallow the newline the firmware appends after a binary
        payload so it does not prefix the next reply. Short timeout, errors ignored."""
        old = getattr(self._inst, "timeout", self._base_timeout_ms)
        try:
            self._inst.timeout = 200
            self._inst.read_bytes(1)
        except Exception:  # noqa: BLE001 - nothing pending is the common case
            pass
        finally:
            self._inst.timeout = old


def _codes(samples: Sequence[Union[int, float]]) -> bytes:
    seq = list(samples)
    if not seq:
        raise SDG1032XValueError("samples is empty")
    if all(isinstance(s, numbers.Integral) and not isinstance(s, bool) for s in seq):
        ints = [int(s) for s in seq]
        if any(not -32768 <= v <= 32767 for v in ints):
            raise SDG1032XValueError("int16 codes must be in -32768..32767")
        return struct.pack(f"<{len(ints)}h", *ints)
    if all(isinstance(s, numbers.Real) and not isinstance(s, bool) for s in seq):
        vals = [float(s) for s in seq]
        if any(math.isnan(v) or abs(v) > 1.0 for v in vals):
            raise SDG1032XValueError("float samples must be in [-1, 1]")
        return struct.pack(f"<{len(vals)}h", *(int(round(v * 32767)) for v in vals))
    raise SDG1032XValueError("samples must be all ints (codes) or all floats in [-1, 1]")


def discover() -> list[str]:
    """VISA resource strings of every SDG1000X on the bus (no instrument opened)."""
    from benchctrl import discovery  # noqa: PLC0415

    return [d.path for d in discovery.find_for("siglent_sdg1032x") if d.path]

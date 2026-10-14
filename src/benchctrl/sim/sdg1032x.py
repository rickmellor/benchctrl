"""A simulated Siglent SDG1032X function / arbitrary waveform generator.

Speaks Siglent's SCPI dialect over the shared
:py:class:`~benchctrl.sim.scpi.ScpiDevice` pty loopback, so the real
:py:class:`SiglentSDG1032X` driver and the real pyvisa stack drive it
unmodified (``ASRL/dev/pts/N::INSTR``).

What this models, and why each part earns its keep
--------------------------------------------------
The SDG1032X has **no error queue**. A value it cannot represent — a
frequency above the waveform's ceiling, an amplitude the load cannot
deliver, a built-in index that does not exist — is dropped on the floor: the
command returns nothing and the setting stays where it was. The driver's
whole design answers that (every setter reads back and raises
:py:class:`SDG1032XVerifyError` when the read-back disagrees), and a
simulator that accepted everything would leave that path untested. So this
one models *silent rejection* as a first-class behaviour: a refused set is
appended to :py:attr:`rejections` and **not applied**, and the driver's
verify step is the only way a test can see it — exactly as on the bench.

* **Byte-exact read-backs.** Every query renders what firmware
  1.01.01.33R1B6 sent on the bench, including its irregularities: ``MODE?``
  answers ``PHASE-LOCKED`` (the set form takes ``PHASELOCKED``);
  ``VOLTPRT?`` answers a bare ``ON`` with no header; ``ROSC?`` omits the
  ``10MOUT`` field; a ``HARM?`` with harmonics on carries a stray comma
  after the header (``C1:HARM ,HARMSTATE,ON,…``); ``STL?`` lists built-ins
  in *lexicographic* order of the ``M<n>`` token (``M10, M100, M101, …,
  M11, M110, …``); ``SCDP`` and ``WVDT?`` append a newline after their
  binary payload. Each of those is a parser tolerance in the driver that
  would otherwise look like dead code.
* **Coupled parameters.** ``FRQ`` and ``PERI``, ``AMP``/``OFST`` and
  ``HLEV``/``LLEV``, ``DUTY`` and ``WIDTH``, sweep ``START``/``STOP`` and
  ``CENTER``/``SPAN``: setting one side recomputes the other, so a test
  can assert that a read-back reflects the instrument's arithmetic rather
  than an echo of the request.
* **Mode exclusivity.** Enabling modulation, sweep or burst turns the other
  two off, and their parameters are only accepted while ``STATE`` is ON
  (recorded as rejections otherwise) — the reason the driver sends
  ``STATE`` first.
* **Unanswered queries.** A header this firmware does not implement
  (``CURRPRT?``, ``VOLTSTAT?``) produces *no reply at all*, and so does
  ``Cn:HARM?`` while that channel's wave is not SINE; the sim records it in
  :py:attr:`unanswered_queries` and lets the driver's timeout path run.
  Unlike the SDM4065A the next query still works, and so it does here.
* **Two transports, as on the bench.** A ``WVDT`` upload over USB-TMC is
  *silently dropped* by this firmware in every framing (measured
  2026-09-12: with/without ``LENGTH``, with/without a terminator, chunked or
  not), so the pty path consumes the frame — :py:meth:`on_frame_bytes` stays
  length-aware so the binary payload does not choke the line splitter — and
  stores nothing, recording the drop in :py:attr:`rejections`. Uploads work
  over the instrument's **LAN SCPI socket** (TCP 5025), which the sim serves
  on loopback at :py:attr:`lan_port`. That socket is *line-oriented*: a
  message ends at the first ``0x0A`` byte, payload included, and a
  ``LENGTH`` field in the header is ignored — so an unescaped payload is
  truncated at its first newline byte exactly as the bench unit truncates
  it (recorded in :py:attr:`lan_truncated`). ``WVDT? USER,<name>`` reads the
  stored bytes back byte-exact behind the firmware's ``WVDT POS, /Local,
  WVNM, <name>, LENGTH, <n>B, TYPE, 6, WAVEDATA,`` header; every other line
  on the socket goes through the same command/query machinery as the pty.
  ``C1:ARWV NAME,<name>`` (bare name; quotes tolerated here, ignored by the
  firmware) selects a stored wave, which reads back as
  ``C1:ARWV NAME,<name>.bin``.
* **Clamp, don't ignore.** Where the bench unit was measured, an
  out-of-range number is clamped to the nearest limit (``AMP,0.001`` at HiZ
  reads back ``0.002V``; ``DUTY,99`` on a 20 MHz square reads back in the
  fifties), so the sim clamps too and records the clamp in
  :py:attr:`rejections`. ``MAX_OUTPUT_AMP`` is accepted but neither echoed
  nor enforced — the amplitude cap that holds is the driver's own.
* **Front-panel side effects.** ``VKEY`` presses are logged, and the two
  Output keys toggle the channel, so a test can show read-back catching a
  change the driver did not make.

What it does *not* model: the analogue output, timing of sweeps and bursts,
the counter measuring anything real (its reading is a fixed plausible
value), whether ``INVT`` and ``OUTP PLRT`` are the same switch on the
hardware (kept independent here), or the exact set of parameters the
firmware accepts while a mode is off. Those need the bench, and the driver's
hardware tests cover them.
"""

from __future__ import annotations

import contextlib
import logging
import math
import re
import socket
import struct
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from benchctrl.sim.loopback import SerialLoopback
from benchctrl.sim.scpi import ScpiDevice, normalise

log = logging.getLogger("benchctrl.sim.sdg1032x")

WAVE_TYPES = ("SINE", "SQUARE", "RAMP", "PULSE", "NOISE", "ARB", "DC")
MOD_TYPES = ("AM", "DSBAM", "FM", "PM", "PWM", "ASK", "FSK", "PSK")
MOD_SOURCES = ("INT", "EXT", "CH1", "CH2")
MOD_SHAPES = ("SINE", "SQUARE", "TRIANGLE", "UPRAMP", "DNRAMP", "NOISE", "ARB")
SWEEP_MODES = ("LINE", "LOG", "STEP")
SWEEP_DIRECTIONS = ("UP", "DOWN", "UP_DOWN")
TRIGGER_SOURCES = ("INT", "EXT", "MAN")
SYNC_TYPES = ("CH1", "CH2", "MOD_CH1", "MOD_CH2")
HARMONIC_TYPES = ("EVEN", "ODD", "ALL")
SCREEN_SAVER_TOKENS = ("OFF", "1", "5", "15", "30", "60", "120", "300")

#: Datasheet frequency ceilings per waveform (the driver has the same table).
FREQ_MAX_HZ = {"SINE": 30e6, "SQUARE": 30e6, "RAMP": 500e3, "PULSE": 12.5e6, "ARB": 6e6}
ARB_MAX_SAMPLES = 16384
ARB_NAME_RE = re.compile(r"^[A-Za-z0-9_]{1,24}$")

#: Built-in arbitrary waveforms, by index. 0 and 1 are the basic Sine/Noise
#: and are not listed by ``STL?``; 2..198 are selectable with ``ARWV INDEX``.
#: The first forty match the bench unit's list; the rest are plausible names
#: from the SDG family's catalogue so the table has the real shape.
# fmt: off
_BUILTIN_NAMES: tuple[str, ...] = (
    "Sine", "Noise", "StairUp", "StairDn", "StairUD", "Ppulse", "Npulse", "Trapezia", "Upramp",
    "Dnramp", "ExpFal", "ExpRise", "LogFall", "LogRise", "Sqrt", "Root3", "X^2", "X^3", "Sinc",
    "Gaussian", "Dlorentz", "Haversine", "Lorentz", "Gauspuls", "Gmonopuls", "Tripuls",
    "Cardiac", "Quake", "Chirp", "Twotone", "SNR", "Hamming", "Hanning", "Kaiser", "Blackman",
    "GaussiWin", "Triangle", "BlackmanH", "Bartlett-Hann", "Bartlett", "BarthannWin",
    "BohmanWin", "ChebWin", "FlattopWin", "NuttallWin", "ParzenWin", "TaylorWin", "TukeyWin",
    "SineTra", "SineVer", "AmpALT", "AttALT", "RoundHalf", "RoundsPM", "BlaseiWave",
    "DampedOsc", "SwingOsc", "Discharge", "Pahcur", "Combin", "SCR", "Butterworth",
    "Chebyshev1", "Chebyshev2", "TV", "Voice", "Surge", "Radar", "Ripple", "Gamma", "StepResp",
    "BandLimited", "CPulse", "CWPulse", "GateVibr", "LFMPulse", "MCNoise", "AM", "FM", "PFM",
    "PM", "PWM", "AbsSine", "AbsSineHalf", "Airy", "Besselj", "Bessely", "ECG1", "ECG2", "ECG3",
    "ECG4", "ECG5", "ECG6", "ECG7", "ECG8", "ECG9", "ECG10", "ECG11", "ECG12", "ECG13", "ECG14",
    "ECG15", "LFPulse", "Tens1", "Tens2", "Tens3", "Dirichlet", "Erf", "Erfc", "ErfcInv",
    "ErfInv", "Laguerre", "Legend", "Versiera", "Weibull", "LogNormal", "Laplace", "Maxwell",
    "Rayleigh", "Cauchy", "CosH", "CosInt", "Cot", "CotHCon", "CotHPro", "CscCon", "CscPro",
    "CscHCon", "CscHPro", "RecipCon", "RecipPro", "SecCon", "SecPro", "SecH", "SinH", "SinInt",
    "Tan", "TanH", "ACos", "ACosH", "ACot", "ACotCon", "ACotPro", "ACotHCon", "ACotHPro",
    "ACsc", "ACscCon", "ACscPro", "ACscHCon", "ACscHPro", "ASec", "ASecCon", "ASecPro", "ASecH",
    "ASin", "ASinH", "ATan", "ATanH", "Log", "Exp", "Cubic", "Cos", "Chirp2", "Gauss2",
    "Demo1_375pts", "Demo1_16kpts", "Demo2_3kpts", "Demo2_16kpts", "Bessel1", "Bessel2",
    "Bessel3", "Gam1", "Gam2", "Laguerre2", "Legend2", "Weibull2", "Bartlett2", "Hann2",
    "Kaiser2", "Nuttall2", "Parzen2", "Taylor2", "Tukey2", "Boxcar", "TriangWin", "Lanczos",
    "Welch", "Poisson", "HannPoisson", "Cosine", "Rife", "Blackman2", "Nuttall3", "Sine2",
    "Square2", "Bessel4", "Gam3", "Hann3", "Kaiser3",
)
# fmt: on
BUILTIN_ARBS: dict[int, str] = {
    i: (_BUILTIN_NAMES[i] if i < len(_BUILTIN_NAMES) else f"Wave{i}") for i in range(199)
}

#: Long-form headers the programming guide documents; the driver sends the
#: short forms, but a human at a terminal may not.
_HEADER_ALIASES: dict[str, str] = {
    "OUTPUT": "OUTP",
    "BASIC_WAVE": "BSWV",
    "MODULATEWAVE": "MDWV",
    "SWEEPWAVE": "SWWV",
    "BURSTWAVE": "BTWV",
    "PARACOPY": "PACP",
    "ARBWAVE": "ARWV",
    "STORELIST": "STL",
    "ROSCILLATOR": "ROSC",
    "INVERT": "INVT",
    "COUPLING": "COUP",
    "HARMONIC": "HARM",
    "COMBINE": "CMBN",
    "FREQCOUNTER": "FCNT",
    "BUZZER": "BUZZ",
    "SCREEN_SAVE": "SCSV",
    "NUMBER_FORMAT": "NBFM",
    "LANGUAGE": "LAGG",
    "SYSTEM_CONFIG": "SCFG",
    "VIRTUALKEY": "VKEY",
    "SYSTEM:COMMUNICATE:LAN:IPADDRESS": "SYST:COMM:LAN:IPAD",
    "SYSTEM:COMMUNICATE:LAN:SMASK": "SYST:COMM:LAN:SMAS",
    "SYSTEM:COMMUNICATE:LAN:GATEWAY": "SYST:COMM:LAN:GAT",
}

_CHANNEL_HEADERS = frozenset(
    {"OUTP", "BSWV", "MDWV", "SWWV", "BTWV", "ARWV", "SYNC", "INVT", "HARM", "CMBN"}
)

_CMD_RE = re.compile(
    r"^(?:C(?P<ch>[12]):)?(?P<head>\*?[A-Za-z_:]+)(?P<q>\??)(?:\s+(?P<args>.*))?$", re.S
)
_VALUE_RE = re.compile(r"^([-+]?(?:\d+\.?\d*|\.\d+)(?:[eE][-+]?\d+)?)\s*([A-Za-z%]*)$")
_UNIT_SCALE: dict[str, float] = {
    "": 1.0,
    "HZ": 1.0,
    "KHZ": 1e3,
    "MHZ": 1e6,
    "S": 1.0,
    "MS": 1e-3,
    "US": 1e-6,
    "NS": 1e-9,
    "V": 1.0,
    "MV": 1e-3,
    "VPP": 1.0,
    "MVPP": 1e-3,
    "VRMS": 1.0,
    "MVRMS": 1e-3,
    "%": 1.0,
    "DEG": 1.0,
    "DBC": 1.0,
    "PPM": 1.0,
}

#: Vpp -> Vrms for the shapes that report ``AMPVRMS``.
_VRMS_FACTOR: dict[str, float] = {
    "SINE": 1.0 / (2.0 * math.sqrt(2.0)),
    "SQUARE": 0.5,
    "PULSE": 0.5,
    "RAMP": 1.0 / (2.0 * math.sqrt(3.0)),
}


def _g(v: float) -> str:
    """The instrument's number rendering: six significant digits, compact."""
    return f"{v:g}"


def _parse_value(token: str) -> Optional[float]:
    """``'1000'``, ``'1kHz'``, ``'2.4e-07S'`` -> float; ``None`` when malformed."""
    m = _VALUE_RE.match(token.strip())
    if not m:
        return None
    scale = _UNIT_SCALE.get(m.group(2).upper())
    if scale is None:
        return None
    return float(m.group(1)) * scale


def _pairs(args: str) -> list[tuple[str, str]]:
    tokens = [t.strip() for t in args.split(",")]
    out: list[tuple[str, str]] = []
    for i in range(0, len(tokens), 2):
        out.append((tokens[i].upper(), tokens[i + 1] if i + 1 < len(tokens) else ""))
    return out


def _onoff(token: str) -> Optional[bool]:
    t = token.strip().upper()
    if t in ("ON", "1"):
        return True
    if t in ("OFF", "0"):
        return False
    return None


def _bmp(width: int, height: int, rgb: tuple[int, int, int] = (0x30, 0x60, 0x0A)) -> bytes:
    """A solid-colour 24-bit BMP. The default fill has a ``0x0A`` byte in every
    pixel on purpose: the real screen dump is full of them, and a transport
    that treats newline as a terminator would stop mid-image."""
    row = width * 3
    pad = (-row) % 4
    image = (row + pad) * height
    header = b"BM" + struct.pack("<IHHI", 54 + image, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, image, 2835, 2835, 0, 0)
    line = bytes((rgb[2], rgb[1], rgb[0])) * width + b"\0" * pad
    return header + dib + line * height


def _new_modulation() -> dict[str, Any]:
    return {
        "state": False,
        "type": "AM",
        "src": "INT",
        "shape": "SINE",
        "frq": 100.0,
        "depth": 100.0,
        "devi": 100.0,
        "kfrq": 100.0,
        "hfrq": 1e6,
        "plrt": "POS",
    }


def _new_sweep() -> dict[str, Any]:
    return {
        "state": False,
        "time": 1.0,
        "start": 500.0,
        "stop": 1500.0,
        "swmd": "LINE",
        "dir": "UP",
        "sym": 0.0,
        "trsr": "INT",
        "trmd": "OFF",
        "edge": "RISE",
        "mark_state": "OFF",
        "mark_freq": 1000.0,
        "starttime": None,
        "endtime": None,
        "backtime": None,
    }


def _new_burst() -> dict[str, Any]:
    return {
        "state": False,
        "prd": 0.01,
        "stps": 0.0,
        "gate_ncyc": "NCYC",
        "trsr": "INT",
        "trmd": "OFF",
        "edge": "RISE",
        "dlay": 2.4e-7,
        "plrt": "POS",
        "time": 1,  # int cycles, or the string "INF"
    }


def _new_harmonics() -> dict[str, Any]:
    return {"state": False, "type": "EVEN", "order": 2, "dbc": -6.0206, "phase": 0.0}


@dataclass
class ChannelState:
    """One output channel's whole state; ``BSWV``/``OUTP``/mode read-backs are
    rendered from it, never stored as text."""

    wave_type: str = "SINE"
    frequency: float = 1000.0
    amplitude: float = 4.0  # Vpp
    offset: float = 0.0
    phase: float = 0.0
    duty: float = 50.0
    symmetry: float = 50.0
    pulse_width: float = 5e-4
    rise: float = 1.68e-8
    fall: float = 1.68e-8
    delay: float = 0.0
    noise_stdev: float = 1.0
    noise_mean: float = 0.0
    max_output_amp: Optional[float] = None
    output: bool = False
    load: Optional[float] = None  # None = HiZ
    polarity: str = "NOR"
    invert: bool = False
    sync: bool = False
    sync_type: str = "CH1"
    modulation: dict[str, Any] = field(default_factory=_new_modulation)
    sweep: dict[str, Any] = field(default_factory=_new_sweep)
    burst: dict[str, Any] = field(default_factory=_new_burst)
    harmonics: dict[str, Any] = field(default_factory=_new_harmonics)
    combine: bool = False
    arb_index: int = 0
    arb_name: str = ""

    @property
    def period(self) -> float:
        return 1.0 / self.frequency

    @property
    def high_level(self) -> float:
        return self.offset + self.amplitude / 2.0

    @property
    def low_level(self) -> float:
        return self.offset - self.amplitude / 2.0

    def copy_wave_from(self, other: ChannelState) -> None:
        for name in (
            "wave_type",
            "frequency",
            "amplitude",
            "offset",
            "phase",
            "duty",
            "symmetry",
            "pulse_width",
            "rise",
            "fall",
            "delay",
            "noise_stdev",
            "noise_mean",
        ):
            setattr(self, name, getattr(other, name))


def _new_coupling() -> dict[str, Any]:
    return {
        "trace": False,
        "fcoup": False,
        "pcoup": False,
        "acoup": False,
        "fdev": None,
        "frat": None,
        "pdev": None,
        "prat": None,
        "arat": None,
        "adev": None,
    }


def _new_counter() -> dict[str, Any]:
    return {
        "state": False,
        "frq": 0.0,
        "duty": 0.0,
        "refq": 1000.0,
        "trg": 0.0,
        "pw": 0.0,
        "nw": 0.0,
        "frqdev": 0.0,
        "mode": "AC",
        "hfr": "OFF",
    }


class SimulatedSDG1032X(ScpiDevice):
    """A Siglent SDG1032X that answers its dialect from a pty.

    Args:
        screen_px: side of the square screen dump ``SCDP`` returns (24-bit
            BMP). 2 keeps tests fast; 200 makes a 120 KB blob.
        swwv_header_space: render ``C1:SWWV STATE,…`` (bench) or the
            guide's ``C1:SWWVSTATE,…`` (False) to exercise the parser's
            tolerance for a missing space.
        mdwv_guide_form: render ``MDWV?`` the way the guide prints it
            (``AM,STATE,ON,…``, type first) instead of the bench form
            (``STATE,ON,AM,…``); the driver accepts both.
        rosc_reports_10mout: append ``,10MOUT,OFF`` to ``ROSC?``; the bench
            unit does not, so the default is False.
        lan: serve the instrument's LAN SCPI socket (the bench unit's TCP
            port 5025) on ``127.0.0.1``; False models a unit with no
            Ethernet, so the driver's no-LAN error path can be tested.
        lan_port: the port to listen on; 0 (default) picks a free one —
            read :py:attr:`lan_port` after construction. The instrument's
            port is fixed and cannot be discovered over SCPI, so a caller
            passes the sim's port to the driver explicitly.
    """

    #: Shaped like a real unit's ``*IDN?``. The serial is deliberately
    #: synthetic — a sim claiming a real instrument's serial makes captured
    #: logs impossible to attribute.
    DEFAULT_IDN = "Siglent Technologies,SDG1032X,SIMSDG10320001,1.01.01.33R1B6"

    def __init__(
        self,
        *,
        idn: str = DEFAULT_IDN,
        screen_px: int = 2,
        swwv_header_space: bool = True,
        mdwv_guide_form: bool = False,
        rosc_reports_10mout: bool = False,
        lan: bool = True,
        lan_port: int = 0,
        loopback: Optional[SerialLoopback] = None,
        free_run: bool = True,
    ) -> None:
        super().__init__(idn=idn, loopback=loopback, free_run=free_run)
        # The generic handler table (incl. ``:SYSTem:ERRor?``) is for the
        # ``:NODE:NODE`` dialect. This instrument has no error queue and no
        # colon-rooted tree, so nothing here consults it.
        self.handlers.clear()
        self.handlers.pop(normalise(":SYSTem:ERRor"), None)

        self.screen_px = int(screen_px)
        self.swwv_header_space = swwv_header_space
        self.mdwv_guide_form = mdwv_guide_form
        self.rosc_reports_10mout = rosc_reports_10mout

        self.channels: dict[int, ChannelState] = {1: ChannelState(), 2: ChannelState()}
        self.rosc = "INT"
        self.rosc_10mout = False
        self.phase_mode = "PHASELOCKED"
        self.coupling: dict[str, Any] = _new_coupling()
        self.counter: dict[str, Any] = _new_counter()
        self.voltprt = True
        self.buzzer = True
        self.scsv = "OFF"
        self.nbfm: dict[str, str] = {"PNT": "DOT", "SEPT": "SPACE"}
        self.lagg = "EN"
        self.scfg = "DEFAULT"
        #: ``SYST:COMM:LAN:*`` read-backs. The address is the loopback one so
        #: the driver's auto-discovery (``IPAD?`` when ``lan_host`` is unset)
        #: lands on this sim's socket server; the factory-default
        #: ``10.11.13.230`` would be read by the driver as "no LAN".
        self.lan: dict[str, str] = {
            "IPAD": "127.0.0.1",
            "SMAS": "255.255.255.0",
            "GAT": "127.0.0.1",
        }
        #: Uploaded waveforms: name -> {freq, ampl, ofst, phase, codes}. Only
        #: the LAN socket fills this; ``codes`` is exactly the byte string that
        #: arrived after ``WAVEDATA,`` up to the terminating newline.
        self.user_arbs: dict[str, dict[str, Any]] = {}
        self.builtin_arbs: dict[int, str] = dict(BUILTIN_ARBS)

        #: What the driver cannot see and a test can: refused sets, queries
        #: that got no reply, writes nobody handled, key presses, triggers.
        self.rejections: list[str] = []
        self.unanswered_queries: list[str] = []
        #: Names of LAN uploads whose payload stopped short of the ``LENGTH``
        #: the client claimed — a 0x0A byte inside the samples ended the
        #: message early, as on the bench unit.
        self.lan_truncated: list[str] = []
        #: Every message received on the LAN socket, one entry per line
        #: (uploads as ``<header><n bytes>``). Kept apart from
        #: :py:attr:`command_log`, which stays the pty's alone.
        self.lan_log: list[str] = []
        #: Bench quirk (firmware 1.01.01.33R1B6): a BSWV command carrying WVTP
        #: together with other fields makes the instrument swallow exactly one
        #: following message. Set by ``_set_bswv``, consumed by ``_dispatch``.
        self._swallow_next = False
        self.swallowed: list[str] = []
        self.ignored_commands: list[str] = []
        self.key_presses: list[str] = []
        self.triggers: list[str] = []
        #: Where the reply of the command being dispatched goes: the pty by
        #: default, a socket connection while a LAN line is being handled.
        self._reply_to: Callable[[bytes], None] = self.send

        # The LAN SCPI socket. Bound here so ``lan_port`` is known before
        # ``start()``; the accept loop runs from ``start()`` to ``close()``.
        self.lan_enabled = bool(lan)
        self._lan_sock: Optional[socket.socket] = None
        self._lan_thread: Optional[threading.Thread] = None
        self._lan_stop = threading.Event()
        self._lan_conns: set[socket.socket] = set()
        self._lan_conns_lock = threading.Lock()
        if self.lan_enabled:
            srv = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            srv.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            srv.bind(("127.0.0.1", int(lan_port)))
            srv.listen(8)
            srv.settimeout(0.1)  # so the accept loop notices ``close()``
            self._lan_sock = srv

        self._queries: dict[str, Callable[[Optional[int], str], Optional[bytes]]] = {
            "*IDN": lambda ch, a: self._text(self.idn),
            "*OPC": lambda ch, a: self._text("1"),
            "OUTP": self._q_outp,
            "BSWV": self._q_bswv,
            "MDWV": self._q_mdwv,
            "SWWV": self._q_swwv,
            "BTWV": self._q_btwv,
            "ARWV": self._q_arwv,
            "STL": self._q_stl,
            "WVDT": self._q_wvdt,
            "SYNC": self._q_sync,
            "ROSC": self._q_rosc,
            "MODE": self._q_mode,
            "INVT": lambda ch, a: self._text(f"C{ch}:INVT {self._oo(self._ch(ch).invert)}"),
            "COUP": self._q_coup,
            "HARM": self._q_harm,
            "CMBN": lambda ch, a: self._text(f"C{ch}:CMBN {self._oo(self._ch(ch).combine)}"),
            "FCNT": self._q_fcnt,
            "VOLTPRT": lambda ch, a: self._text(self._oo(self.voltprt)),
            "BUZZ": lambda ch, a: self._text(f"BUZZ {self._oo(self.buzzer)}"),
            "SCSV": lambda ch, a: self._text(
                f"SCSV {'OFF' if self.scsv == 'OFF' else self.scsv + 'MIN'}"
            ),
            "NBFM": lambda ch, a: self._text(
                f"NBFM PNT,{self.nbfm['PNT']},SEPT,{self.nbfm['SEPT']}"
            ),
            "LAGG": lambda ch, a: self._text(f"LAGG {self.lagg}"),
            "SCFG": lambda ch, a: self._text(f"SCFG {self.scfg}"),
            "SYST:COMM:LAN:IPAD": lambda ch, a: self._text(f'"{self.lan["IPAD"]}"'),
            "SYST:COMM:LAN:SMAS": lambda ch, a: self._text(f'"{self.lan["SMAS"]}"'),
            "SYST:COMM:LAN:GAT": lambda ch, a: self._text(f'"{self.lan["GAT"]}"'),
        }
        self._sets: dict[str, Callable[[Optional[int], str], None]] = {
            "*RST": lambda ch, a: self.reset(),
            "*CLS": lambda ch, a: None,
            "*OPC": lambda ch, a: None,
            "OUTP": self._s_outp,
            "BSWV": lambda ch, a: self._set_bswv("BSWV", self._ch(ch), _pairs(a)),
            "MDWV": self._s_mdwv,
            "SWWV": self._s_swwv,
            "BTWV": self._s_btwv,
            "PACP": self._s_pacp,
            "ARWV": self._s_arwv,
            "SYNC": self._s_sync,
            "EQPHASE": lambda ch, a: None,
            "ROSC": self._s_rosc,
            "MODE": self._s_mode,
            "INVT": self._s_invt,
            "COUP": self._s_coup,
            "HARM": self._s_harm,
            "CMBN": self._s_cmbn,
            "FCNT": self._s_fcnt,
            "VOLTPRT": self._s_voltprt,
            "BUZZ": self._s_buzz,
            "SCSV": self._s_scsv,
            "NBFM": self._s_nbfm,
            "LAGG": self._s_lagg,
            "SCFG": self._s_scfg,
            "VKEY": self._s_vkey,
            "SCDP": self._s_scdp,
            "SYST:COMM:LAN:IPAD": lambda ch, a: self._s_lan("IPAD", a),
            "SYST:COMM:LAN:SMAS": lambda ch, a: self._s_lan("SMAS", a),
            "SYST:COMM:LAN:GAT": lambda ch, a: self._s_lan("GAT", a),
        }

    # ------------------------------------------------------------ framing

    USB_WVDT_DROPPED = "WVDT over USB-TMC is dropped by this firmware"

    def on_frame_bytes(self, data: bytes) -> None:
        """Split the pty byte stream into commands.

        Line-oriented, except for a ``WVDT`` upload, whose int16 payload may
        contain ``0x0A``: when the buffer holds ``WAVEDATA,`` the header
        before it is parsed for ``LENGTH,<n>B`` and the frame is consumed only
        once all ``n`` payload bytes have arrived, so the binary does not get
        chopped into bogus commands. Either way the upload is then **dropped
        without a trace**, which is what firmware 1.01.01.33R1B6 does with
        every ``WVDT`` framing over USB-TMC; the drop is recorded in
        :py:attr:`rejections`. Without a ``LENGTH`` the frame cannot be
        delimited, so whatever has arrived is discarded too.
        """
        with self._lock:
            self._rx.extend(data)
            while True:
                buf = bytes(self._rx)
                wd = buf.find(b"WAVEDATA,")
                nl = buf.find(b"\n")
                if nl != -1 and (wd == -1 or nl < wd):
                    self._rx = bytearray(buf[nl + 1 :])
                    text = buf[:nl].decode("ascii", errors="replace").strip()
                    if text:
                        self._handle_line(text)
                    continue
                if wd == -1:
                    break
                head_end = wd + len(b"WAVEDATA,")
                header = buf[:head_end].decode("ascii", errors="replace").strip()
                name = self._wvdt_name(header)
                m = re.search(r"LENGTH\s*,\s*(\d+)\s*(B|KB)?", header, re.I)
                if not m:
                    self.command_log.append(header)
                    self.rejections.append(
                        f"WVDT {name}: {self.USB_WVDT_DROPPED} (no LENGTH field, buffer discarded)"
                    )
                    self._rx = bytearray()
                    break
                nbytes = int(m.group(1)) * (1024 if (m.group(2) or "B").upper() == "KB" else 1)
                if len(buf) < head_end + nbytes:
                    break  # wait for the rest of the payload
                self._rx = bytearray(buf[head_end + nbytes :])
                self.command_log.append(f"{header}<{nbytes} bytes>")
                self.rejections.append(f"WVDT {name}: {self.USB_WVDT_DROPPED}")
                log.debug("sdg1032x sim: dropped USB WVDT upload of %r (%d bytes)", name, nbytes)

    @staticmethod
    def _wvdt_name(header: str) -> str:
        m = re.search(r"WVNM\s*,\s*\"?([^,\"]*)", header, re.I)
        return m.group(1).strip() if m else ""

    def _handle_line(self, line: str) -> None:
        self.command_log.append(line)
        self._dispatch(line)

    # ------------------------------------------------------------ LAN socket

    @property
    def lan_port(self) -> Optional[int]:
        """The loopback TCP port standing in for the instrument's port 5025;
        ``None`` when the sim was built with ``lan=False``."""
        if self._lan_sock is None:
            return None
        return int(self._lan_sock.getsockname()[1])

    def start(self) -> SimulatedSDG1032X:
        super().start()
        if self._lan_sock is not None and self._lan_thread is None:
            self._lan_stop.clear()
            self._lan_thread = threading.Thread(
                target=self._lan_accept_loop, name="SimulatedSDG1032X-lan", daemon=True
            )
            self._lan_thread.start()
        return self

    def close(self) -> None:
        self._lan_stop.set()
        with self._lan_conns_lock:
            conns = list(self._lan_conns)
        for c in conns:
            with contextlib.suppress(OSError):
                c.shutdown(socket.SHUT_RDWR)
            c.close()
        if self._lan_thread is not None:
            self._lan_thread.join(timeout=2.0)
            self._lan_thread = None
        if self._lan_sock is not None:
            self._lan_sock.close()
            self._lan_sock = None
        super().close()

    def _lan_accept_loop(self) -> None:
        srv = self._lan_sock
        assert srv is not None
        while not self._lan_stop.is_set():
            try:
                conn, _ = srv.accept()
            except socket.timeout:
                continue
            except OSError:
                break
            with self._lan_conns_lock:
                self._lan_conns.add(conn)
            threading.Thread(
                target=self._lan_serve, args=(conn,), name="SimulatedSDG1032X-lan-conn", daemon=True
            ).start()

    def _lan_serve(self, conn: socket.socket) -> None:
        """One client connection: bytes in, messages split at ``\\n`` **only**.

        A ``LENGTH`` field never extends a message past a newline — that is
        the bench unit's behaviour and the whole reason the driver escapes
        its payload."""
        buf = bytearray()

        def reply(data: bytes) -> None:
            with contextlib.suppress(OSError):
                conn.sendall(data)

        try:
            while not self._lan_stop.is_set():
                try:
                    data = conn.recv(65536)
                except OSError:
                    break
                if not data:
                    break
                buf += data
                while (nl := buf.find(b"\n")) != -1:
                    message = bytes(buf[:nl])
                    del buf[: nl + 1]
                    self._lan_message(message, reply)
        finally:
            with self._lan_conns_lock:
                self._lan_conns.discard(conn)
            conn.close()

    def _lan_message(self, message: bytes, reply: Callable[[bytes], None]) -> None:
        wd = message.find(b"WAVEDATA,")
        with self._lock:
            if wd != -1:
                head_end = wd + len(b"WAVEDATA,")
                header = message[:head_end].decode("ascii", errors="replace").strip()
                payload = message[head_end:]
                self.lan_log.append(f"{header}<{len(payload)} bytes>")
                self._lan_upload(header, payload)
                return
            text = message.decode("ascii", errors="replace").strip()
            if not text:
                return
            self.lan_log.append(text)
            m = re.match(r"^(?:C[12]:)?WVDT\?\s*(.*)$", text, re.I | re.S)
            if m:
                data = self._lan_wvdt_query(m.group(1))
                if data is None:
                    self.unanswered_queries.append(text)
                    log.debug("sdg1032x sim: unanswered LAN query %r", text)
                else:
                    reply(data)
                return
            self._dispatch(text, send=reply)

    def _lan_upload(self, header: str, payload: bytes) -> None:
        """Store a LAN ``WVDT`` upload exactly as received. ``payload`` is
        whatever followed ``WAVEDATA,`` up to the newline that ended the
        message; if the header claimed a longer ``LENGTH`` the client's data
        contained a 0x0A byte and was cut there, like on the bench."""
        body = header.split(" ", 1)[1] if " " in header else header
        fields = dict(_pairs(body.rsplit("WAVEDATA", 1)[0]))
        name = fields.get("WVNM", "").strip().strip('"')
        if not ARB_NAME_RE.match(name):
            self._reject("WVDT", "WVNM", name, "bad waveform name")
            return
        if len(payload) > 2 * ARB_MAX_SAMPLES:
            self._reject(
                "WVDT", "WAVEDATA", str(len(payload)), f"more than {ARB_MAX_SAMPLES} samples"
            )
            return
        nums: dict[str, float] = {}
        for key, default in (("FREQ", 1000.0), ("AMPL", 1.0), ("OFST", 0.0), ("PHASE", 0.0)):
            v = _parse_value(fields[key]) if key in fields else default
            if v is None:
                self._reject("WVDT", key, fields.get(key, ""), "malformed number")
                return
            nums[key] = v
        m = re.match(r"^\s*(\d+)\s*(B|KB)?\s*$", fields.get("LENGTH", ""), re.I)
        if m:
            claimed = int(m.group(1)) * (1024 if (m.group(2) or "B").upper() == "KB" else 1)
            if len(payload) < claimed:
                self.lan_truncated.append(name)
                log.debug(
                    "sdg1032x sim: LAN upload %r cut at a newline byte: %d of %d bytes stored",
                    name,
                    len(payload),
                    claimed,
                )
        self.user_arbs[name] = {
            "freq": nums["FREQ"],
            "ampl": nums["AMPL"],
            "ofst": nums["OFST"],
            "phase": nums["PHASE"],
            "codes": bytes(payload),
        }

    def _lan_wvdt_query(self, args: str) -> Optional[bytes]:
        """``WVDT? USER,<name>`` as the socket answers it (firmware
        1.01.01.33R1B6): ``WVDT POS, /Local, WVNM, <name>, LENGTH, <n>B, TYPE,
        6, WAVEDATA,`` — the fields comma-space separated — then immediately
        the ``n`` stored bytes and a newline. Unknown name: no reply at all."""
        parts = [p.strip() for p in args.split(",")]
        if len(parts) < 2 or parts[0].upper() != "USER":
            return None
        name = parts[1].strip('"')
        arb = self.user_arbs.get(name)
        if arb is None:
            return None
        codes: bytes = arb["codes"]
        head = f"WVDT POS, /Local, WVNM, {name}, LENGTH, {len(codes)}B, TYPE, 6, WAVEDATA,"
        return head.encode("ascii") + codes + b"\n"

    # ------------------------------------------------------------ dispatch

    def _dispatch(self, command: str, send: Optional[Callable[[bytes], None]] = None) -> None:
        """Run one command or query; its reply (if any) goes to ``send`` —
        the pty when None, a socket connection for a LAN line."""
        self._reply_to = send if send is not None else self.send
        m = _CMD_RE.match(command.strip())
        if not m:
            self.ignored_commands.append(command)
            return
        ch = int(m.group("ch")) if m.group("ch") else None
        head = self._canon_header(m.group("head"))
        is_query = m.group("q") == "?"
        args = (m.group("args") or "").strip()
        if head in _CHANNEL_HEADERS and ch is None:
            ch = 1
        if self._swallow_next:
            # The one message after a combined WVTP+fields BSWV is lost, query
            # or command alike, exactly as the bench unit does it.
            self._swallow_next = False
            self.swallowed.append(command)
            if is_query:
                self.unanswered_queries.append(command)
            log.debug("sdg1032x sim: swallowed %r after a combined WVTP command", command)
            return
        if is_query:
            fn = self._queries.get(head)
            reply = fn(ch, args) if fn is not None else None
            if reply is None:
                # No error queue, no reply: the driver's timeout is the signal.
                self.unanswered_queries.append(command)
                log.debug("sdg1032x sim: unanswered query %r", command)
                return
            self._reply_to(reply)
            return
        setter = self._sets.get(head)
        if setter is None:
            self.ignored_commands.append(command)
            log.debug("sdg1032x sim: ignored command %r", command)
            return
        setter(ch, args)

    @staticmethod
    def _canon_header(head: str) -> str:
        h = head.upper()
        if ":" in h:
            h = _HEADER_ALIASES.get(h, normalise(head))
        return _HEADER_ALIASES.get(h, h)

    def _text(self, text: str) -> bytes:
        return (text + self.terminator).encode("ascii", errors="replace")

    def _ch(self, ch: Optional[int]) -> ChannelState:
        return self.channels[ch or 1]

    @staticmethod
    def _oo(flag: bool) -> str:
        return "ON" if flag else "OFF"

    def _reject(self, header: str, key: str, val: str, reason: str) -> None:
        self.rejections.append(f"{header} {key},{val}: {reason}")
        log.debug("sdg1032x sim: silently rejected %s %s,%s (%s)", header, key, val, reason)

    # ------------------------------------------------------------ rendering

    def _carrier_fields(self, st: ChannelState, *, vrms_digits: Optional[int]) -> list[str]:
        """The ``CARR,`` block of a mode read-back (and the core of ``BSWV``)."""
        out = [f"WVTP,{st.wave_type}"]
        if st.wave_type in ("NOISE", "DC"):
            return out
        out.append(f"FRQ,{_g(st.frequency)}HZ")
        out.append(f"AMP,{_g(st.amplitude)}V")
        if vrms_digits is not None and st.wave_type in _VRMS_FACTOR:
            vrms = st.amplitude * _VRMS_FACTOR[st.wave_type]
            out.append(f"AMPVRMS,{vrms:.{vrms_digits}g}Vrms")
        out.append(f"OFST,{_g(st.offset)}V")
        if st.wave_type != "PULSE":
            out.append(f"PHSE,{_g(st.phase)}")
        if st.wave_type == "RAMP":
            out.append(f"SYM,{_g(st.symmetry)}")
        if st.wave_type in ("SQUARE", "PULSE"):
            out.append(f"DUTY,{_g(st.duty)}")
        return out

    def _q_bswv(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        fields: list[str] = [f"WVTP,{st.wave_type}"]
        if st.wave_type == "NOISE":
            fields += [f"STDEV,{_g(st.noise_stdev)}V", f"MEAN,{_g(st.noise_mean)}V"]
        elif st.wave_type == "DC":
            fields.append(f"OFST,{_g(st.offset)}V")
        else:
            fields += [
                f"FRQ,{_g(st.frequency)}HZ",
                f"PERI,{_g(st.period)}S",
                f"AMP,{_g(st.amplitude)}V",
            ]
            if st.wave_type in _VRMS_FACTOR:
                fields.append(f"AMPVRMS,{st.amplitude * _VRMS_FACTOR[st.wave_type]:.4g}Vrms")
            fields += [
                f"OFST,{_g(st.offset)}V",
                f"HLEV,{_g(st.high_level)}V",
                f"LLEV,{_g(st.low_level)}V",
            ]
            if st.wave_type != "PULSE":
                fields.append(f"PHSE,{_g(st.phase)}")
            if st.wave_type == "SQUARE":
                fields.append(f"DUTY,{_g(st.duty)}")
            elif st.wave_type == "RAMP":
                fields.append(f"SYM,{_g(st.symmetry)}")
            elif st.wave_type == "PULSE":
                fields += [
                    f"DUTY,{_g(st.duty)}",
                    f"WIDTH,{_g(st.pulse_width)}S",
                    f"RISE,{_g(st.rise)}S",
                    f"FALL,{_g(st.fall)}S",
                    f"DLY,{_g(st.delay)}S",
                ]
        # ``MAX_OUTPUT_AMP`` is accepted on set but never echoed (bench).
        return self._text(f"C{ch}:BSWV " + ",".join(fields))

    def _q_outp(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        load = "HZ" if st.load is None else _g(st.load)
        return self._text(f"C{ch}:OUTP {self._oo(st.output)},LOAD,{load},PLRT,{st.polarity}")

    def _q_mdwv(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        m = st.modulation
        if not m["state"]:
            return self._text(f"C{ch}:MDWV STATE,OFF")
        t = m["type"]
        # Bench form is ``STATE,ON,AM,…`` — the type is a bare token after the
        # state pair; the guide prints ``AM,STATE,ON,…``.
        fields = [t, "STATE,ON"] if self.mdwv_guide_form else ["STATE,ON", t]
        if t in ("AM", "DSBAM", "FM", "PM", "PWM"):
            fields += [f"MDSP,{m['shape']}", f"SRC,{m['src']}", f"FRQ,{_g(m['frq'])}HZ"]
            if t == "AM":
                fields.append(f"DEPTH,{_g(m['depth'])}")
            elif t == "FM":
                fields.append(f"DEVI,{_g(m['devi'])}HZ")
            elif t in ("PM", "PWM"):
                fields.append(f"DEVI,{_g(m['devi'])}")
        else:
            fields += [f"SRC,{m['src']}", f"KFRQ,{_g(m['kfrq'])}HZ"]
            if t == "FSK":
                fields.append(f"HFRQ,{_g(m['hfrq'])}HZ")
            elif t == "PSK":
                fields.append(f"PLRT,{m['plrt']}")
        fields.append("CARR")
        fields += self._carrier_fields(st, vrms_digits=4)
        return self._text(f"C{ch}:MDWV " + ",".join(fields))

    def _q_swwv(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        s = st.sweep
        head = f"C{ch}:SWWV" + (" " if self.swwv_header_space else "")
        if not s["state"]:
            return self._text(head + "STATE,OFF")
        fields = ["STATE,ON", f"TIME,{_g(s['time'])}S"]
        for key in ("starttime", "endtime", "backtime"):
            if s[key] is not None:
                fields.append(f"{key.upper()},{_g(s[key])}S")
        center = (s["start"] + s["stop"]) / 2.0
        span = s["stop"] - s["start"]
        fields += [
            f"STOP,{_g(s['stop'])}HZ",
            f"START,{_g(s['start'])}HZ",
            f"CENTER,{_g(center)}HZ",
            f"SPAN,{_g(span)}HZ",
            f"TRSR,{s['trsr']}",
            f"TRMD,{s['trmd']}",
        ]
        if s["trsr"] == "EXT":
            fields.append(f"EDGE,{s['edge']}")
        fields += [
            f"SWMD,{s['swmd']}",
            f"DIR,{s['dir']}",
            f"SYM,{_g(s['sym'])}",
            f"MARK_STATE,{s['mark_state']}",
            f"MARK_FREQ,{_g(s['mark_freq'])}HZ",
            "CARR",
        ]
        fields += self._carrier_fields(st, vrms_digits=6)
        return self._text(head + ",".join(fields))

    def _q_btwv(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        b = st.burst
        if not b["state"]:
            return self._text(f"C{ch}:BTWV STATE,OFF")
        fields = [
            "STATE,ON",
            f"PRD,{_g(b['prd'])}S",
            f"STPS,{_g(b['stps'])}",
            f"TRSR,{b['trsr']}",
            f"TRMD,{b['trmd']}",
        ]
        if b["trsr"] in ("EXT", "MAN"):
            fields.append(f"EDGE,{b['edge']}")
        fields += [f"TIME,{b['time']}", f"DLAY,{_g(b['dlay'])}S"]
        if b["gate_ncyc"] == "GATE":
            fields.append(f"PLRT,{b['plrt']}")
        fields += [f"GATE_NCYC,{b['gate_ncyc']}", "CARR"]
        fields += self._carrier_fields(st, vrms_digits=None)
        return self._text(f"C{ch}:BTWV " + ",".join(fields))

    def _q_arwv(self, ch: Optional[int], args: str) -> bytes:
        st = self._ch(ch)
        if st.arb_index == 0 and st.arb_name:
            # Bench: a selected user waveform answers with no INDEX and the
            # storage suffix appended.
            return self._text(f"C{ch}:ARWV NAME,{st.arb_name}.bin")
        return self._text(f"C{ch}:ARWV INDEX,{st.arb_index},NAME,{st.arb_name}")

    def _q_stl(self, ch: Optional[int], args: str) -> bytes:
        kind = args.strip().upper()
        if kind == "USER":
            names = list(self.user_arbs)
            return self._text("STL WVNM" + ("," + ",".join(names) if names else ""))
        items = sorted(
            ((f"M{i}", name) for i, name in self.builtin_arbs.items() if i >= 2),
            key=lambda kv: kv[0],
        )
        return self._text("STL " + ", ".join(f"{m}, {name}" for m, name in items))

    def _q_wvdt(self, ch: Optional[int], args: str) -> Optional[bytes]:
        parts = [p.strip() for p in args.split(",")]
        if len(parts) < 2 or parts[0].upper() != "USER":
            return None
        arb = self.user_arbs.get(parts[1].strip('"'))
        if arb is None:
            return None
        codes: bytes = arb["codes"]
        head = (
            f"WVDT WVNM,{parts[1]},LENGTH,{len(codes)}B,TYPE,10,FREQ,{_g(arb['freq'])},"
            f"AMPL,{_g(arb['ampl'])},OFST,{_g(arb['ofst'])},PHASE,{_g(arb['phase'])},WAVEDATA,"
        )
        return head.encode("ascii") + codes + self.terminator.encode("ascii")

    def _q_sync(self, ch: Optional[int], args: str) -> bytes:
        # Bench: ``C1:SYNC ON`` / ``C1:SYNC OFF`` — TYPE is stored but never echoed.
        return self._text(f"C{ch}:SYNC {self._oo(self._ch(ch).sync)}")

    def _q_rosc(self, ch: Optional[int], args: str) -> bytes:
        text = f"ROSC {self.rosc}"
        if self.rosc_reports_10mout:
            text += f",10MOUT,{self._oo(self.rosc_10mout)}"
        return self._text(text)

    def _q_mode(self, ch: Optional[int], args: str) -> bytes:
        return self._text(
            "MODE PHASE-LOCKED" if self.phase_mode == "PHASELOCKED" else "MODE INDEPENDENT"
        )

    def _q_coup(self, ch: Optional[int], args: str) -> bytes:
        c = self.coupling
        fields = [
            f"TRACE,{self._oo(c['trace'])}",
            f"FCOUP,{self._oo(c['fcoup'])}",
            f"PCOUP,{self._oo(c['pcoup'])}",
            f"ACOUP,{self._oo(c['acoup'])}",
        ]
        for key, unit in (
            ("fdev", "HZ"),
            ("frat", ""),
            ("pdev", ""),
            ("prat", ""),
            ("arat", ""),
            ("adev", "V"),
        ):
            if c[key] is not None:
                fields.append(f"{key.upper()},{_g(c[key])}{unit}")
        return self._text("COUP " + ",".join(fields))

    def _q_harm(self, ch: Optional[int], args: str) -> Optional[bytes]:
        st = self._ch(ch)
        h = st.harmonics
        if st.wave_type != "SINE":
            # Bench: ``HARM?`` off a sine wave gets no reply at all — harmonics
            # exist only for SINE and the firmware treats the query as unknown.
            return None
        if not h["state"]:
            return self._text(f"C{ch}:HARM HARMSTATE,OFF")
        amp = st.amplitude * 10 ** (h["dbc"] / 20.0)
        # The stray comma after the header is what the bench unit sends.
        return self._text(
            f"C{ch}:HARM ,HARMSTATE,ON,HARMTYPE,{h['type']},HARMORDER,{h['order']},"
            f"HARMAMP,{amp:.10g}V,HARMDBC,{_g(h['dbc'])}dBc,HARMPHASE,{_g(h['phase'])}"
        )

    def _q_fcnt(self, ch: Optional[int], args: str) -> bytes:
        c = self.counter
        if not c["state"]:
            return self._text("FCNT STATE,OFF")
        return self._text(
            f"FCNT STATE,ON,FRQ,{c['frq']:.10g}HZ,DUTY,{_g(c['duty'])},REFQ,{_g(c['refq'])}HZ,"
            f"TRG,{_g(c['trg'])}V,PW,{_g(c['pw'])}S,NW,{_g(c['nw'])}S,"
            f"FRQDEV,{_g(c['frqdev'])}ppm,MODE,{c['mode']},HFR,{c['hfr']}"
        )

    # ------------------------------------------------------------ basic wave sets

    @staticmethod
    def _amp_limits(st: ChannelState) -> tuple[float, float]:
        return (0.002, 20.0) if st.load is None else (0.001, 10.0)

    def _clamp(
        self, header: str, key: str, val: str, v: float, lo: float, hi: float, what: str
    ) -> float:
        """The bench unit *clamps* an out-of-range number to the nearest limit
        (``AMP,0.001`` at HiZ reads back ``0.002V``) rather than ignoring it.
        Recorded as a rejection either way: the request was not honoured."""
        if v < lo or v > hi:
            clamped = min(max(v, lo), hi)
            self._reject(
                header, key, val, f"{what} {v:g} outside {lo:g}..{hi:g}, clamped to {clamped:g}"
            )
            return clamped
        return v

    @staticmethod
    def _duty_limits(st: ChannelState) -> tuple[float, float]:
        """Square duty narrows at high frequency (bench: ``DUTY,99`` at 20 MHz
        reads back ``59``); modelled as 40..60 above 10 MHz."""
        if st.wave_type == "SQUARE" and st.frequency > 10e6:
            return 40.0, 60.0
        return 0.0, 100.0

    def _apply_amp_ofst(
        self, header: str, st: ChannelState, key: str, val: str, amp: float, ofst: float
    ) -> None:
        lo, hi = self._amp_limits(st)
        amp = round(amp * 1000.0) / 1000.0
        st.amplitude = self._clamp(header, key, val, amp, lo, hi, "amplitude Vpp")
        st.offset = self._clamp(header, key, val, ofst, -10.0, 10.0, "offset V")

    def _apply_frequency(self, header: str, st: ChannelState, key: str, val: str, f: float) -> None:
        if st.wave_type in ("NOISE", "DC"):
            self._reject(header, key, val, f"{st.wave_type} has no frequency")
            return
        ceiling = FREQ_MAX_HZ[st.wave_type]
        f = round(f * 1e6) / 1e6
        st.frequency = self._clamp(header, key, val, f, 1e-6, ceiling, "frequency Hz")
        st.pulse_width = st.duty / 100.0 * st.period
        lo, hi = self._duty_limits(st)
        if not lo <= st.duty <= hi:
            st.duty = min(max(st.duty, lo), hi)

    def _set_bswv(self, header: str, st: ChannelState, pairs: list[tuple[str, str]]) -> None:
        keys = [k for k, _ in pairs]
        if "WVTP" in keys and len(keys) > 1:
            self._swallow_next = True
        for key, val in pairs:
            if key == "WVTP":
                wt = val.strip().upper()
                if wt not in WAVE_TYPES:
                    self._reject(header, key, val, "unknown wave type")
                elif wt == "SQUARE" and st.combine:
                    self._reject(header, key, val, "SQUARE is unavailable while combine is on")
                else:
                    st.wave_type = wt
                    if wt in FREQ_MAX_HZ and st.frequency > FREQ_MAX_HZ[wt]:
                        st.frequency = FREQ_MAX_HZ[wt]
                continue
            v = _parse_value(val)
            if v is None:
                self._reject(header, key, val, "malformed number")
                continue
            if key in ("FRQ", "PERI"):
                if key == "PERI" and v <= 0:
                    self._reject(header, key, val, "period must be positive")
                    continue
                self._apply_frequency(header, st, key, val, v if key == "FRQ" else 1.0 / v)
            elif key in ("AMP", "AMPVRMS", "OFST", "HLEV", "LLEV"):
                if st.wave_type in ("NOISE", "DC") and key != "OFST":
                    self._reject(header, key, val, f"{st.wave_type} has no amplitude")
                    continue
                if key == "AMP":
                    self._apply_amp_ofst(header, st, key, val, v, st.offset)
                elif key == "AMPVRMS":
                    factor = _VRMS_FACTOR.get(st.wave_type)
                    if factor is None:
                        self._reject(header, key, val, f"{st.wave_type} has no Vrms")
                        continue
                    self._apply_amp_ofst(header, st, key, val, v / factor, st.offset)
                elif key == "OFST":
                    if st.wave_type == "DC":
                        st.offset = self._clamp(header, key, val, v, -10.0, 10.0, "offset V")
                    else:
                        self._apply_amp_ofst(header, st, key, val, st.amplitude, v)
                elif key == "HLEV":
                    low = st.low_level
                    self._apply_amp_ofst(header, st, key, val, v - low, (v + low) / 2.0)
                else:  # LLEV
                    high = st.high_level
                    self._apply_amp_ofst(header, st, key, val, high - v, (high + v) / 2.0)
            elif key == "PHSE":
                if st.wave_type in ("NOISE", "DC", "PULSE"):
                    self._reject(header, key, val, f"{st.wave_type} has no phase")
                else:
                    v = round(v * 100.0) / 100.0
                    st.phase = self._clamp(header, key, val, v, 0.0, 360.0, "phase")
            elif key == "DUTY":
                if st.wave_type not in ("SQUARE", "PULSE"):
                    self._reject(header, key, val, f"{st.wave_type} has no duty")
                else:
                    lo, hi = self._duty_limits(st)
                    v = round(v * 100.0) / 100.0
                    st.duty = self._clamp(header, key, val, v, lo, hi, "duty")
                    st.pulse_width = st.duty / 100.0 * st.period
            elif key == "SYM":
                if st.wave_type != "RAMP":
                    self._reject(header, key, val, f"{st.wave_type} has no symmetry")
                else:
                    v = round(v * 100.0) / 100.0
                    st.symmetry = self._clamp(header, key, val, v, 0.0, 100.0, "symmetry")
            elif key in ("WIDTH", "RISE", "FALL", "DLY"):
                if st.wave_type != "PULSE":
                    self._reject(header, key, val, f"{st.wave_type} has no {key}")
                elif v < 0 or (key == "WIDTH" and not 0 < v < st.period):
                    self._reject(header, key, val, "outside the pulse period")
                elif key == "WIDTH":
                    st.pulse_width = v
                    st.duty = round(v / st.period * 100.0 * 100.0) / 100.0
                else:
                    setattr(st, {"RISE": "rise", "FALL": "fall", "DLY": "delay"}[key], v)
            elif key in ("STDEV", "MEAN"):
                if st.wave_type != "NOISE":
                    self._reject(header, key, val, f"{st.wave_type} has no {key}")
                else:
                    setattr(st, "noise_stdev" if key == "STDEV" else "noise_mean", v)
            elif key == "MAX_OUTPUT_AMP":
                # Accepted, stored for inspection, neither echoed nor enforced
                # (bench: AMP 3 after a cap of 2 reads back 3 V).
                st.max_output_amp = v
            else:
                self._reject(header, key, val, "unknown parameter")

    # ------------------------------------------------------------ other sets

    def _s_outp(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        tokens = [t.strip().upper() for t in args.split(",") if t.strip()]
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in ("ON", "OFF"):
                st.output = t == "ON"
                i += 1
            elif t == "LOAD" and i + 1 < len(tokens):
                val = tokens[i + 1]
                if val in ("HZ", "HIZ"):
                    st.load = None
                else:
                    v = _parse_value(val)
                    if v is None or not 50 <= v <= 100000:
                        self._reject("OUTP", t, val, "load outside 50..100000 or HZ")
                    else:
                        st.load = v
                        _, hi = self._amp_limits(st)
                        st.amplitude = min(st.amplitude, hi)  # 20 Vpp HiZ -> 10 Vpp into 50 Ω
                i += 2
            elif t == "PLRT" and i + 1 < len(tokens):
                val = tokens[i + 1]
                if val in ("NOR", "INVT"):
                    st.polarity = val
                else:
                    self._reject("OUTP", t, val, "polarity must be NOR or INVT")
                i += 2
            else:
                self._reject("OUTP", t, "", "unknown parameter")
                i += 1

    def _exclusive_on(self, st: ChannelState, which: str) -> None:
        for name in ("modulation", "sweep", "burst"):
            getattr(st, name)["state"] = name == which

    def _s_mdwv(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        m = st.modulation
        tokens = [t.strip() for t in args.split(",")]
        target: Optional[str] = None
        i = 0
        while i < len(tokens):
            t = tokens[i].upper()
            if t == "STATE" and i + 1 < len(tokens):
                on = _onoff(tokens[i + 1])
                if on is None:
                    self._reject("MDWV", t, tokens[i + 1], "not ON/OFF")
                elif on:
                    self._exclusive_on(st, "modulation")
                else:
                    m["state"] = False
                i += 2
            elif t == "MTRIG":
                i += 1
            elif t in MOD_TYPES:
                if m["state"]:
                    m["type"] = t
                else:
                    self._reject("MDWV", t, "", "modulation is off")
                target = "type"
                i += 1
            elif t == "CARR":
                target = "carr"
                i += 1
            elif target == "carr" and i + 1 < len(tokens):
                self._set_bswv("MDWV CARR", st, [(t, tokens[i + 1])])
                i += 2
            elif target == "type" and i + 1 < len(tokens):
                if m["state"]:
                    self._set_mod_param(m, t, tokens[i + 1])
                else:
                    self._reject("MDWV", t, tokens[i + 1], "modulation is off")
                i += 2
            else:
                self._reject("MDWV", t, tokens[i + 1] if i + 1 < len(tokens) else "", "unknown")
                i += 2

    def _set_mod_param(self, m: dict[str, Any], key: str, val: str) -> None:
        enum: dict[str, tuple[str, tuple[str, ...]]] = {
            "SRC": ("src", MOD_SOURCES),
            "MDSP": ("shape", MOD_SHAPES),
            "PLRT": ("plrt", ("POS", "NEG")),
        }
        if key in enum:
            name, choices = enum[key]
            v = val.strip().upper()
            if v in choices:
                m[name] = v
            else:
                self._reject("MDWV", key, val, f"not one of {choices}")
            return
        numeric = {"FRQ": "frq", "DEPTH": "depth", "DEVI": "devi", "KFRQ": "kfrq", "HFRQ": "hfrq"}
        if key not in numeric:
            self._reject("MDWV", key, val, "unknown parameter")
            return
        v_num = _parse_value(val)
        if v_num is None:
            self._reject("MDWV", key, val, "malformed number")
        elif key == "DEPTH" and not 0 <= v_num <= 120:
            self._reject("MDWV", key, val, "depth outside 0..120")
        elif key != "DEPTH" and v_num < 0:
            self._reject("MDWV", key, val, "negative")
        else:
            m[numeric[key]] = v_num

    def _s_swwv(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        s = st.sweep
        tokens = [t.strip() for t in args.split(",")]
        carrier = False
        i = 0
        while i < len(tokens):
            t = tokens[i].upper()
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t == "STATE":
                on = _onoff(val)
                if on is None:
                    self._reject("SWWV", t, val, "not ON/OFF")
                elif on:
                    self._exclusive_on(st, "sweep")
                else:
                    s["state"] = False
                i += 2
            elif t == "MTRIG":
                self.triggers.append(f"C{ch}:SWWV")
                i += 1
            elif t == "CARR":
                carrier = True
                i += 1
            elif carrier:
                self._set_bswv("SWWV CARR", st, [(t, val)])
                i += 2
            elif not s["state"]:
                self._reject("SWWV", t, val, "sweep is off")
                i += 2
            else:
                self._set_sweep_param(s, t, val)
                i += 2

    def _set_sweep_param(self, s: dict[str, Any], key: str, val: str) -> None:
        enum: dict[str, tuple[str, tuple[str, ...]]] = {
            "SWMD": ("swmd", SWEEP_MODES),
            "DIR": ("dir", SWEEP_DIRECTIONS),
            "TRSR": ("trsr", TRIGGER_SOURCES),
            "TRMD": ("trmd", ("ON", "OFF")),
            "EDGE": ("edge", ("RISE", "FALL")),
            "MARK_STATE": ("mark_state", ("ON", "OFF")),
        }
        if key in enum:
            name, choices = enum[key]
            v = val.strip().upper()
            if v in choices:
                s[name] = v
            else:
                self._reject("SWWV", key, val, f"not one of {choices}")
            return
        numeric = {
            "TIME": "time",
            "STARTTIME": "starttime",
            "ENDTIME": "endtime",
            "BACKTIME": "backtime",
            "START": "start",
            "STOP": "stop",
            "SYM": "sym",
            "MARK_FREQ": "mark_freq",
        }
        v_num = _parse_value(val)
        if v_num is None:
            self._reject("SWWV", key, val, "malformed number")
            return
        if key in numeric:
            if key == "SYM" and not 0 <= v_num <= 100:
                self._reject("SWWV", key, val, "symmetry outside 0..100")
            elif key != "SYM" and v_num < 0:
                self._reject("SWWV", key, val, "negative")
            else:
                s[numeric[key]] = v_num
        elif key == "CENTER":
            span = s["stop"] - s["start"]
            s["start"], s["stop"] = v_num - span / 2.0, v_num + span / 2.0
        elif key == "SPAN":
            center = (s["start"] + s["stop"]) / 2.0
            s["start"], s["stop"] = center - v_num / 2.0, center + v_num / 2.0
        else:
            self._reject("SWWV", key, val, "unknown parameter")

    def _s_btwv(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        b = st.burst
        tokens = [t.strip() for t in args.split(",")]
        carrier = False
        i = 0
        while i < len(tokens):
            t = tokens[i].upper()
            val = tokens[i + 1] if i + 1 < len(tokens) else ""
            if t == "STATE":
                on = _onoff(val)
                if on is None:
                    self._reject("BTWV", t, val, "not ON/OFF")
                elif on:
                    self._exclusive_on(st, "burst")
                else:
                    b["state"] = False
                i += 2
            elif t == "MTRIG":
                self.triggers.append(f"C{ch}:BTWV")
                i += 1
            elif t == "CARR":
                carrier = True
                i += 1
            elif carrier:
                self._set_bswv("BTWV CARR", st, [(t, val)])
                i += 2
            elif not b["state"]:
                self._reject("BTWV", t, val, "burst is off")
                i += 2
            else:
                self._set_burst_param(b, t, val)
                i += 2

    def _set_burst_param(self, b: dict[str, Any], key: str, val: str) -> None:
        enum: dict[str, tuple[str, tuple[str, ...]]] = {
            "GATE_NCYC": ("gate_ncyc", ("GATE", "NCYC")),
            "TRSR": ("trsr", TRIGGER_SOURCES),
            "TRMD": ("trmd", ("RISE", "FALL", "OFF")),
            "EDGE": ("edge", ("RISE", "FALL")),
            "PLRT": ("plrt", ("POS", "NEG")),
        }
        if key in enum:
            name, choices = enum[key]
            v = val.strip().upper()
            if v in choices:
                b[name] = v
            else:
                self._reject("BTWV", key, val, f"not one of {choices}")
            return
        if key == "TIME":
            if val.strip().upper() == "INF":
                b["time"] = "INF"
                return
            v_num = _parse_value(val)
            if v_num is None or v_num < 1 or v_num != int(v_num) or v_num > 1_000_000:
                self._reject("BTWV", key, val, "cycles must be 1..1000000 or INF")
            else:
                b["time"] = int(v_num)
            return
        numeric = {"PRD": "prd", "STPS": "stps", "DLAY": "dlay"}
        if key not in numeric:
            self._reject("BTWV", key, val, "unknown parameter")
            return
        v_num = _parse_value(val)
        if v_num is None:
            self._reject("BTWV", key, val, "malformed number")
        elif key == "STPS" and not 0 <= v_num <= 360:
            self._reject("BTWV", key, val, "phase outside 0..360")
        elif key != "STPS" and v_num < 0:
            self._reject("BTWV", key, val, "negative")
        else:
            b[numeric[key]] = v_num

    def _s_pacp(self, ch: Optional[int], args: str) -> None:
        m = re.match(r"^\s*C([12])\s*,\s*C([12])\s*$", args.upper())
        if not m or m.group(1) == m.group(2):
            self._reject("PACP", args, "", "expects Cdst,Csrc with distinct channels")
            return
        dst, src = int(m.group(1)), int(m.group(2))
        self.channels[dst].copy_wave_from(self.channels[src])

    def _s_arwv(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        for key, val in _pairs(args):
            if key == "INDEX":
                v = _parse_value(val)
                if v is None or v != int(v) or not 2 <= int(v) <= 198:
                    self._reject("ARWV", key, val, "index outside 2..198")
                else:
                    st.arb_index = int(v)
                    st.arb_name = self.builtin_arbs[int(v)]
            elif key == "NAME":
                name = val.strip().strip('"')
                if name not in self.user_arbs and name.lower().endswith(".bin"):
                    name = name[:-4]  # the suffix ``ARWV?`` itself reports
                if name not in self.user_arbs:
                    self._reject("ARWV", key, val, "no such user waveform")
                else:
                    st.arb_index = 0
                    st.arb_name = name
            else:
                self._reject("ARWV", key, val, "unknown parameter")

    def _s_sync(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        tokens = [t.strip().upper() for t in args.split(",") if t.strip()]
        i = 0
        while i < len(tokens):
            t = tokens[i]
            if t in ("ON", "OFF"):
                st.sync = t == "ON"
                i += 1
            elif t == "TYPE" and i + 1 < len(tokens):
                if tokens[i + 1] in SYNC_TYPES:
                    st.sync_type = tokens[i + 1]
                else:
                    self._reject("SYNC", t, tokens[i + 1], f"not one of {SYNC_TYPES}")
                i += 2
            else:
                self._reject("SYNC", t, "", "unknown parameter")
                i += 1

    def _s_rosc(self, ch: Optional[int], args: str) -> None:
        tokens = [t.strip().upper() for t in args.split(",") if t.strip()]
        if len(tokens) == 1 and tokens[0] in ("INT", "EXT"):
            self.rosc = tokens[0]
        elif len(tokens) == 2 and tokens[0] == "10MOUT" and _onoff(tokens[1]) is not None:
            self.rosc_10mout = bool(_onoff(tokens[1]))
        else:
            self._reject("ROSC", args, "", "expects INT|EXT or 10MOUT,ON|OFF")

    def _s_mode(self, ch: Optional[int], args: str) -> None:
        # Bench: the guide's ``PHASELOCKED`` is silently ignored; the firmware
        # takes ``PHASE-LOCKED`` (and reports it that way) and ``INDEPENDENT``.
        v = args.strip().upper()
        if v == "PHASE-LOCKED":
            self.phase_mode = "PHASELOCKED"
        elif v == "INDEPENDENT":
            self.phase_mode = v
        else:
            self._reject("MODE", args, "", "expects PHASE-LOCKED or INDEPENDENT")

    def _s_invt(self, ch: Optional[int], args: str) -> None:
        on = _onoff(args)
        if on is None:
            self._reject("INVT", args, "", "not ON/OFF")
        else:
            self._ch(ch).invert = on

    def _s_cmbn(self, ch: Optional[int], args: str) -> None:
        on = _onoff(args)
        if on is None:
            self._reject("CMBN", args, "", "not ON/OFF")
        else:
            self._ch(ch).combine = on

    def _s_coup(self, ch: Optional[int], args: str) -> None:
        c = self.coupling
        exclusive = {
            "fdev": "frat",
            "frat": "fdev",
            "pdev": "prat",
            "prat": "pdev",
            "arat": "adev",
            "adev": "arat",
        }
        for key, val in _pairs(args):
            k = key.lower()
            if k in ("trace", "fcoup", "pcoup", "acoup"):
                on = _onoff(val)
                if on is None:
                    self._reject("COUP", key, val, "not ON/OFF")
                else:
                    c[k] = on
            elif k in exclusive:
                v = _parse_value(val)
                if v is None:
                    self._reject("COUP", key, val, "malformed number")
                else:
                    c[k] = v
                    c[exclusive[k]] = None
            else:
                self._reject("COUP", key, val, "unknown parameter")

    def _s_harm(self, ch: Optional[int], args: str) -> None:
        st = self._ch(ch)
        h = st.harmonics
        for key, val in _pairs(args):
            if key == "HARMSTATE":
                on = _onoff(val)
                if on is None:
                    self._reject("HARM", key, val, "not ON/OFF")
                else:
                    h["state"] = on
            elif key == "HARMTYPE":
                v = val.strip().upper()
                if v in HARMONIC_TYPES:
                    h["type"] = v
                else:
                    self._reject("HARM", key, val, f"not one of {HARMONIC_TYPES}")
            else:
                v_num = _parse_value(val)
                if v_num is None:
                    self._reject("HARM", key, val, "malformed number")
                elif key == "HARMORDER":
                    if v_num != int(v_num) or not 2 <= v_num <= 16:
                        self._reject("HARM", key, val, "order outside 2..16")
                    else:
                        h["order"] = int(v_num)
                elif key == "HARMAMP":
                    if not 0 < v_num <= st.amplitude:
                        self._reject("HARM", key, val, "amplitude outside (0, carrier]")
                    else:
                        h["dbc"] = 20.0 * math.log10(v_num / st.amplitude)
                elif key == "HARMDBC":
                    if v_num > 0:
                        self._reject("HARM", key, val, "dBc must be <= 0")
                    else:
                        h["dbc"] = v_num
                elif key == "HARMPHASE":
                    if not 0 <= v_num <= 360:
                        self._reject("HARM", key, val, "phase outside 0..360")
                    else:
                        h["phase"] = v_num
                else:
                    self._reject("HARM", key, val, "unknown parameter")

    def _s_fcnt(self, ch: Optional[int], args: str) -> None:
        c = self.counter
        for key, val in _pairs(args):
            if key == "STATE":
                on = _onoff(val)
                if on is None:
                    self._reject("FCNT", key, val, "not ON/OFF")
                else:
                    c["state"] = on
            elif key == "MODE":
                v = val.strip().upper()
                if v in ("AC", "DC"):
                    c["mode"] = v
                else:
                    self._reject("FCNT", key, val, "AC or DC")
            elif key == "HFR":
                on = _onoff(val)
                if on is None:
                    self._reject("FCNT", key, val, "not ON/OFF")
                else:
                    c["hfr"] = self._oo(on)
            elif key in ("REFQ", "TRG"):
                v_num = _parse_value(val)
                if v_num is None:
                    self._reject("FCNT", key, val, "malformed number")
                else:
                    c[key.lower()] = v_num
            else:
                self._reject("FCNT", key, val, "unknown parameter")

    def _s_voltprt(self, ch: Optional[int], args: str) -> None:
        on = _onoff(args)
        if on is None:
            self._reject("VOLTPRT", args, "", "not ON/OFF")
        else:
            self.voltprt = on

    def _s_buzz(self, ch: Optional[int], args: str) -> None:
        on = _onoff(args)
        if on is None:
            self._reject("BUZZ", args, "", "not ON/OFF")
        else:
            self.buzzer = on

    def _s_scsv(self, ch: Optional[int], args: str) -> None:
        v = args.strip().upper()
        if v == "0":
            v = "OFF"
        if v in SCREEN_SAVER_TOKENS:
            self.scsv = v
        else:
            self._reject("SCSV", args, "", f"not one of {SCREEN_SAVER_TOKENS}")

    def _s_nbfm(self, ch: Optional[int], args: str) -> None:
        for key, val in _pairs(args):
            v = val.strip().upper()
            if key == "PNT" and v in ("DOT", "COMMA"):
                self.nbfm["PNT"] = v
            elif key == "SEPT" and v in ("SPACE", "OFF", "ON"):
                self.nbfm["SEPT"] = v
            else:
                self._reject("NBFM", key, val, "unknown parameter or value")

    def _s_lagg(self, ch: Optional[int], args: str) -> None:
        v = args.strip().upper()
        if v in ("EN", "CH"):
            self.lagg = v
        else:
            self._reject("LAGG", args, "", "EN or CH")

    def _s_scfg(self, ch: Optional[int], args: str) -> None:
        v = args.strip().upper()
        if v in ("DEFAULT", "LAST", "USER"):
            self.scfg = v
        else:
            self._reject("SCFG", args, "", "DEFAULT, LAST or USER")

    def _s_lan(self, key: str, args: str) -> None:
        v = args.strip().strip('"')
        if re.match(r"^\d{1,3}(\.\d{1,3}){3}$", v):
            self.lan[key] = v
        else:
            self._reject(f"SYST:COMM:LAN:{key}", args, "", "not a dotted quad")

    def _s_vkey(self, ch: Optional[int], args: str) -> None:
        pairs = dict(_pairs(args))
        key = pairs.get("VALUE", "").strip().upper()
        if not key:
            self._reject("VKEY", args, "", "no VALUE")
            return
        self.key_presses.append(key)
        if pairs.get("STATE", "1").strip() != "1":
            return
        if key in ("KB_OUTPUT1", "153"):
            self.channels[1].output = not self.channels[1].output
        elif key in ("KB_OUTPUT2", "152"):
            self.channels[2].output = not self.channels[2].output

    def _s_scdp(self, ch: Optional[int], args: str) -> None:
        # A write-form command that answers with a binary blob, newline after.
        self._reply_to(_bmp(self.screen_px, self.screen_px) + self.terminator.encode("ascii"))

    # ------------------------------------------------------------ reset

    def reset(self) -> None:
        """``*RST`` — power-on defaults: outputs off, HiZ, 1 kHz 4 Vpp sine.
        Uploaded waveforms, the LAN settings and the sim's own logs survive,
        as they do on the instrument."""
        super().reset()
        self.channels = {1: ChannelState(), 2: ChannelState()}
        self.rosc = "INT"
        self.rosc_10mout = False
        self.phase_mode = "PHASELOCKED"
        self.coupling = _new_coupling()
        self.counter = _new_counter()
        self.voltprt = True
        self.buzzer = True
        self.scsv = "OFF"
        self.nbfm = {"PNT": "DOT", "SEPT": "SPACE"}
        self.lagg = "EN"
        self.scfg = "DEFAULT"


__all__ = ["BUILTIN_ARBS", "ChannelState", "SimulatedSDG1032X"]

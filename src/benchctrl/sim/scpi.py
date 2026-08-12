"""SCPI instrument simulators over a serial loopback.

The Rigol drivers talk SCPI through pyvisa. pyvisa-py's ASRL backend speaks
serial, so pointing a driver at ``ASRL/dev/pts/N::INSTR`` runs the real
driver, the real pyvisa stack, and real serial I/O against a simulator —
the same fidelity the Arc gets from its pty, over the transport the drivers
actually support (the DL3031A documents RS-232 alongside USB-TMC).

Coverage model
--------------
SCPI is overwhelmingly regular: ``:NODE:NODE value`` sets, ``:NODE:NODE?``
reads back. :py:class:`ScpiDevice` implements that generically over a
register dict, with mnemonics normalised to their short form so the long
forms the drivers emit and the short forms a human types land on the same
key. Between the two Rigol drivers there are ~254 distinct command strings
and nearly all of them are covered by that rule alone.

Commands needing behaviour rather than storage — ``*IDN?``, ``:MEASure:*``,
``:FETCh:*``, ``:SYSTem:ERRor?`` — are explicit handlers. Everything else
stores and reads back, which is exactly what the instrument does.

What this does *not* model: LIST sequencing timing, transient waveform
generation, the battery-test state machine, and the firmware quirks in
``bugs/``. Those need hardware. The simulator answers their commands and
tracks their parameters, so command-shape and parameter-validation logic is
testable; timing-dependent behaviour is not.
"""

from __future__ import annotations

import logging
import re
import threading
from typing import Callable, Optional

from benchctrl.sim.base import SimDevice
from benchctrl.sim.loopback import SerialLoopback
from benchctrl.sim.waveforms import Waveform

log = logging.getLogger("benchctrl.sim.scpi")

_MNEMONIC = re.compile(r"^([A-Z*]+)")


def normalise(command: str) -> str:
    """Reduce a SCPI header to canonical short form.

    ``:SOURce:CURRent:LEVel:IMMediate`` and ``:sour:curr:lev:imm`` both
    become ``:SOUR:CURR:LEV:IMM``. The short form is the leading run of
    uppercase characters in each mnemonic, which is how SCPI defines it.
    """
    head = command.strip()
    if head.endswith("?"):
        head = head[:-1]
    out = []
    for token in head.split(":"):
        if not token:
            out.append("")
            continue
        m = _MNEMONIC.match(token)
        out.append(m.group(1) if m else token.upper())
    return ":".join(out)


def split_command(line: str) -> tuple[str, Optional[str]]:
    """Split ``":SOUR:CURR 1.5"`` into ``(":SOUR:CURR", "1.5")``."""
    line = line.strip()
    if not line:
        return "", None
    if " " in line:
        head, _, arg = line.partition(" ")
        return head, arg.strip()
    return line, None


class ScpiDevice(SimDevice):
    """A line-oriented SCPI instrument behind a pty.

    Args:
        idn: the ``*IDN?`` response, comma-separated.
        defaults: initial register values, keyed by *normalised* header.
        terminator: response line ending. Rigol uses ``\\n``.
    """

    def __init__(
        self,
        *,
        idn: str,
        defaults: Optional[dict[str, str]] = None,
        loopback: Optional[SerialLoopback] = None,
        terminator: str = "\n",
        free_run: bool = True,
    ) -> None:
        super().__init__(loopback=loopback, tick_hz=100.0, free_run=free_run)
        self._lock = threading.RLock()
        self._rx = bytearray()
        self.idn = idn
        self.terminator = terminator
        self.registers: dict[str, str] = {}
        if defaults:
            self.registers.update({normalise(k): v for k, v in defaults.items()})
        self.command_log: list[str] = []
        self.error_queue: list[tuple[int, str]] = []
        self.handlers: dict[str, Callable[[Optional[str]], Optional[str]]] = {}
        #: When set, the next command pushes this onto the error queue.
        self.force_next_error: Optional[tuple[int, str]] = None
        self._install_common_handlers()

    # --- registration ---------------------------------------------------

    def handler(self, header: str) -> Callable:
        """Decorator registering a handler for a (normalised) header."""

        def wrap(fn: Callable[[Optional[str]], Optional[str]]):
            self.handlers[normalise(header)] = fn
            return fn

        return wrap

    def _install_common_handlers(self) -> None:
        self.handlers[normalise("*IDN")] = lambda arg: self.idn
        self.handlers[normalise("*RST")] = self._on_rst
        self.handlers[normalise("*CLS")] = self._on_cls
        self.handlers[normalise("*OPC")] = lambda arg: "1"
        self.handlers[normalise(":SYSTem:ERRor")] = self._on_error_query

    def _on_rst(self, arg: Optional[str]) -> None:
        self.reset()
        return None

    def _on_cls(self, arg: Optional[str]) -> None:
        self.error_queue.clear()
        return None

    def _on_error_query(self, arg: Optional[str]) -> str:
        if self.error_queue:
            code, msg = self.error_queue.pop(0)
            return f'{code},"{msg}"'
        return '0,"No error"'

    def reset(self) -> None:
        """``*RST`` — subclasses restore their power-on defaults."""
        self.error_queue.clear()

    # --- I/O ------------------------------------------------------------

    def on_frame_bytes(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)
            while b"\n" in self._rx:
                line, _, rest = bytes(self._rx).partition(b"\n")
                self._rx = bytearray(rest)
                text = line.decode("ascii", errors="replace").strip()
                if text:
                    self._handle_line(text)

    def _handle_line(self, line: str) -> None:
        self.command_log.append(line)

        if self.force_next_error is not None:
            self.error_queue.append(self.force_next_error)
            self.force_next_error = None

        # Compound commands are semicolon-separated; each is independent.
        for part in line.split(";"):
            part = part.strip()
            if part:
                self._dispatch(part)

    def _dispatch(self, command: str) -> None:
        head, arg = split_command(command)
        is_query = head.endswith("?")
        key = normalise(head)

        fn = self.handlers.get(key)
        if fn is not None:
            result = fn(arg)
            if is_query:
                self._reply("" if result is None else str(result))
            return

        if is_query:
            value = self.registers.get(key)
            if value is None:
                self.error_queue.append((-113, "Undefined header"))
                log.debug("scpi sim: unknown query %s", command)
                self._reply("0")
                return
            self._reply(value)
            return

        if arg is None:
            # A bare command with no argument and no handler: accept it as a
            # no-op action rather than erroring, matching how instruments
            # treat vendor extensions.
            self.registers.setdefault(key, "")
            return
        self.registers[key] = arg

    def _reply(self, text: str) -> None:
        self.send((text + self.terminator).encode("ascii", errors="replace"))

    # --- helpers for subclasses -----------------------------------------

    def get_float(self, header: str, default: float = 0.0) -> float:
        try:
            return float(self.registers.get(normalise(header), default))
        except (TypeError, ValueError):
            return default

    def get_int(self, header: str, default: int = 0) -> int:
        try:
            return int(float(self.registers.get(normalise(header), default)))
        except (TypeError, ValueError):
            return default

    def set_value(self, header: str, value) -> None:
        self.registers[normalise(header)] = str(value)

    def inject_error(self, code: int = -222, message: str = "Data out of range") -> None:
        """Queue a device error, surfaced by the driver's ``raise_if_error``."""
        self.force_next_error = (code, message)


class SimulatedRigolDL3031A(ScpiDevice):
    """A DL3000-series electronic load.

    Models the measurement relationship that matters for tests: with the
    input on in CC mode the load sinks its programmed current, and the
    measured voltage comes from whatever source is attached (settable, or
    driven by a waveform).
    """

    DEFAULT_IDN = "RIGOL TECHNOLOGIES,DL3031A,SIM0000000001,00.01.05.00.01"

    def __init__(
        self,
        *,
        idn: str = DEFAULT_IDN,
        supply_voltage: float = 3.3,
        source: Optional[Waveform] = None,
        loopback: Optional[SerialLoopback] = None,
        free_run: bool = True,
    ) -> None:
        super().__init__(
            idn=idn,
            loopback=loopback,
            free_run=free_run,
            defaults={
                ":SOURce:INPut:STATe": "0",
                ":SOURce:FUNCtion": "CC",
                ":SOURce:FUNCtion:MODE": "FIXed",
                ":SOURce:CURRent:LEVel:IMMediate": "0.000000",
                ":SOURce:CURRent:RANGe": "4.000000",
                ":SOURce:VOLTage:LEVel:IMMediate": "0.000000",
                ":SOURce:RESistance:LEVel:IMMediate": "1000.000000",
                ":SOURce:POWer:LEVel:IMMediate": "0.000000",
            },
        )
        self.supply_voltage = supply_voltage
        self.source = source
        self._install_measurement_handlers()

    #: The load is set with a long mnemonic but reads back as the two-letter
    #: regulation mode — ``:SOUR:FUNC CURRent`` answers ``CC``, not ``CURR``.
    #: Faithful to the hardware, and the driver's ``get_mode`` depends on it.
    _FUNC_READBACK = {
        "CURR": "CC",
        "VOLT": "CV",
        "RES": "CR",
        "POW": "CP",
        "CC": "CC",
        "CV": "CV",
        "CR": "CR",
        "CP": "CP",
    }

    @property
    def input_on(self) -> bool:
        return self.get_int(":SOURce:INPut:STATe") == 1

    @property
    def function(self) -> str:
        """Regulation mode as ``CC`` / ``CV`` / ``CR`` / ``CP``."""
        raw = self.registers.get(normalise(":SOURce:FUNCtion"), "CC")
        return self._FUNC_READBACK.get(normalise(raw), "CC")

    def _on_set_function(self, arg: Optional[str]) -> Optional[str]:
        if arg is None:
            return self.function
        self.registers[normalise(":SOURce:FUNCtion")] = self._FUNC_READBACK.get(
            normalise(arg), "CC"
        )
        return None

    def measured_voltage(self) -> float:
        if self.source is not None:
            return self.source.value(self.elapsed_s)
        return self.supply_voltage

    def measured_current(self) -> float:
        if not self.input_on:
            return 0.0
        fn = self.function
        v = self.measured_voltage()
        if fn == "CC":
            return self.get_float(":SOURce:CURRent:LEVel:IMMediate")
        if fn == "CR":
            r = self.get_float(":SOURce:RESistance:LEVel:IMMediate", 1000.0)
            return v / r if r > 0 else 0.0
        if fn == "CP":
            p = self.get_float(":SOURce:POWer:LEVel:IMMediate")
            return p / v if v > 0 else 0.0
        if fn == "CV":
            # In CV mode the load sinks whatever holds the rail at setpoint;
            # without a source model, report a plausible constant.
            return 0.1
        return 0.0

    def _install_measurement_handlers(self) -> None:
        def volt(arg):
            return f"{self.measured_voltage():.6f}"

        def curr(arg):
            return f"{self.measured_current():.6f}"

        def power(arg):
            return f"{self.measured_voltage() * self.measured_current():.6f}"

        def res(arg):
            i = self.measured_current()
            return f"{(self.measured_voltage() / i) if i > 1e-9 else 9.9e37:.6f}"

        for prefix in (":MEASure", ":FETCh"):
            self.handlers[normalise(f"{prefix}:VOLTage:DC")] = volt
            self.handlers[normalise(f"{prefix}:CURRent:DC")] = curr
            self.handlers[normalise(f"{prefix}:POWer:DC")] = power
            self.handlers[normalise(f"{prefix}:RESistance:DC")] = res

        self.handlers[normalise(":FETCh:CAPability")] = lambda a: "0.000000"
        self.handlers[normalise(":FETCh:WATThours")] = lambda a: "0.000000"
        self.handlers[normalise(":FETCh:DISChargingTime")] = lambda a: "0"
        self.handlers[normalise(":SOURce:FUNCtion")] = self._on_set_function

    def reset(self) -> None:
        super().reset()
        self.set_value(":SOURce:INPut:STATe", 0)
        self.set_value(":SOURce:CURRent:LEVel:IMMediate", "0.000000")
        self.registers[normalise(":SOURce:FUNCtion")] = "CC"


class SimulatedRigolDP2031(ScpiDevice):
    """A DP2000-series three-channel programmable power supply.

    Each channel is an independent V/I setpoint with an output switch;
    measurements reflect the setpoint into a configurable per-channel load.
    """

    DEFAULT_IDN = "RIGOL TECHNOLOGIES,DP2031,SIM0000000002,01.00.01.00.16"
    CHANNELS = (1, 2, 3)

    def __init__(
        self,
        *,
        idn: str = DEFAULT_IDN,
        load_ohm: float = 100.0,
        loopback: Optional[SerialLoopback] = None,
        free_run: bool = True,
    ) -> None:
        super().__init__(idn=idn, loopback=loopback, free_run=free_run)
        self.load_ohm = {ch: load_ohm for ch in self.CHANNELS}
        self.output_on = {ch: False for ch in self.CHANNELS}
        self.voltage = {ch: 0.0 for ch in self.CHANNELS}
        self.current_limit = {ch: 1.0 for ch in self.CHANNELS}
        self._install_channel_handlers()

    def measured(self, ch: int) -> tuple[float, float]:
        """Return ``(volts, amps)`` for channel ``ch``."""
        if not self.output_on.get(ch):
            return 0.0, 0.0
        v = self.voltage.get(ch, 0.0)
        r = self.load_ohm.get(ch, 100.0)
        i = v / r if r > 0 else 0.0
        limit = self.current_limit.get(ch, 1.0)
        if i > limit:  # constant-current fold-back
            i = limit
            v = i * r
        return v, i

    def _install_channel_handlers(self) -> None:
        def _ch_from(arg: Optional[str], default: int = 1) -> int:
            if not arg:
                return default
            m = re.search(r"CH(\d)", arg.upper())
            return int(m.group(1)) if m else default

        def apply(arg):
            # :APPLy CH1,3.3,0.5
            if not arg:
                return None
            parts = [p.strip() for p in arg.split(",")]
            ch = _ch_from(parts[0])
            if len(parts) > 1:
                self.voltage[ch] = float(parts[1])
            if len(parts) > 2:
                self.current_limit[ch] = float(parts[2])
            return None

        self.handlers[normalise(":APPLy")] = apply

        def make_output(ch: int):
            def fn(arg):
                if arg is None:
                    return "ON" if self.output_on[ch] else "OFF"
                self.output_on[ch] = arg.strip().upper() in ("ON", "1", "TRUE")
                return None

            return fn

        def make_volt(ch: int):
            def fn(arg):
                if arg is None:
                    return f"{self.voltage[ch]:.6f}"
                self.voltage[ch] = float(arg)
                return None

            return fn

        def make_curr(ch: int):
            def fn(arg):
                if arg is None:
                    return f"{self.current_limit[ch]:.6f}"
                self.current_limit[ch] = float(arg)
                return None

            return fn

        def make_meas(ch: int, index: int):
            def fn(arg):
                return f"{self.measured(ch)[index]:.6f}"

            return fn

        for ch in self.CHANNELS:
            self.handlers[normalise(f":OUTPut:STATe CH{ch}")] = make_output(ch)
            self.handlers[normalise(f":SOURce{ch}:VOLTage:LEVel:IMMediate:AMPLitude")] = make_volt(ch)
            self.handlers[normalise(f":SOURce{ch}:CURRent:LEVel:IMMediate:AMPLitude")] = make_curr(ch)
            self.handlers[normalise(f":MEASure:VOLTage:DC CH{ch}")] = make_meas(ch, 0)
            self.handlers[normalise(f":MEASure:CURRent:DC CH{ch}")] = make_meas(ch, 1)

    def reset(self) -> None:
        super().reset()
        for ch in self.CHANNELS:
            self.output_on[ch] = False
            self.voltage[ch] = 0.0
            self.current_limit[ch] = 1.0

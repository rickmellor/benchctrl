"""A simulated Siglent SDM4065A 6½-digit bench DMM.

Speaks SCPI over the shared :py:class:`~benchctrl.sim.scpi.ScpiDevice` pty
loopback, so the real :py:class:`SiglentSDM4065A` driver and the real pyvisa
stack drive it unmodified (``ASRL/dev/pts/N::INSTR``).

What this models, and why each part earns its keep
--------------------------------------------------
A DMM's whole job is to return a number, so a simulator that returns a
*stored* number tests almost nothing. This one models the small signal chain
that the driver's correctness actually depends on:

* **A DUT resistance** (:py:attr:`dut_ohm`) that readings are derived from,
  so a test can assert the driver reports what the instrument measured
  rather than what it was told.
* **Lead resistance** (:py:attr:`lead_ohm`), added on 2-wire (``RES``) and
  *not* on 4-wire (``FRES``). This is the physical difference between the
  two functions; without it, a test could not tell that
  ``measure_resistance_4wire`` sends a different command at all, and the
  0.2 Ω that datasheet note [6] warns about would be invisible.
* **Null with automatic-value selection**, including the trap in remote
  manual §7.4.2/§7.4.4: enabling ``NULL:STATe`` *also* arms
  ``NULL:VALue:AUTO``, so the next reading overwrites the stored offset.
  A driver that sets the value before the state gets a different null than
  it asked for. That is a silent wrong-answer bug, so the sim reproduces it.
* **Range selection and the 9.9E37 overload sentinel** (§7.4.5), so
  overload handling is testable without abusing real hardware.
* **``CONFigure`` resetting NPLC, null and range to defaults** (§7.4.1
  onward), which is why configuration order matters in the driver.

What it does *not* model: absolute accuracy, integration-time noise,
autozero's effect on drift, or self-heating. Those are the properties the
hardware cross-validation exists to check, and a simulator asserting them
would only be asserting its own arithmetic.
"""

from __future__ import annotations

import logging
from typing import Optional

from benchctrl.sim.loopback import SerialLoopback
from benchctrl.sim.scpi import ScpiDevice, normalise

log = logging.getLogger("benchctrl.sim.sdm4065a")

#: Ranges the SDM4065A offers, ascending (remote manual §7.4.5).
#: 1 MΩ, not the SDM4055A's 2 MΩ.
RESISTANCE_RANGES = (200.0, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6)
DC_VOLTAGE_RANGES = (0.2, 2.0, 20.0, 200.0, 1000.0)

#: The instrument's own power-on / ``CONFigure`` default range is 2 kΩ —
#: *not* autorange, and not the lowest range (§7.4.5).
DEFAULT_RESISTANCE_RANGE = 2e3

#: Returned instead of a reading when the input exceeds the manual range.
OVERLOAD = 9.9e37

#: Autoranging steps up above 120% of range (§7.4.6); a fixed range that is
#: exceeded by the same margin reads as Overload.
_OVERRANGE_FACTOR = 1.2


class SimulatedSDM4065A(ScpiDevice):
    """A Siglent SDM4065A that answers SCPI from a pty.

    Args:
        dut_ohm: the resistance connected to the terminals.
        lead_ohm: series lead + contact resistance, seen by 2-wire only.
            Defaults to the 0.2 Ω the datasheet warns about in note [6],
            so the un-nulled 2-wire error is present by default rather than
            something a test has to opt into.
        dut_volts: DC voltage present at the terminals.
    """

    #: Shaped like a real unit's ``*IDN?``. The serial is deliberately
    #: synthetic — a sim claiming a real instrument's serial makes captured
    #: logs impossible to attribute.
    DEFAULT_IDN = "Siglent Technologies,SDM4065A,SIM4065A0001,1.01.01.15"

    def __init__(
        self,
        *,
        idn: str = DEFAULT_IDN,
        dut_ohm: float = 100.0,
        lead_ohm: float = 0.2,
        dut_volts: float = 0.0,
        loopback: Optional[SerialLoopback] = None,
        free_run: bool = True,
    ) -> None:
        super().__init__(idn=idn, loopback=loopback, free_run=free_run)

        self.dut_ohm = dut_ohm
        self.lead_ohm = lead_ohm
        self.dut_volts = dut_volts

        self.function = "VOLT"  # *RST default is DC voltage (§7.1)
        self.sample_count = 1
        #: Readings taken by INITiate, waiting for FETCh?.
        self.reading_buffer: list[float] = []

        # Per-function measurement parameters. Keyed by the *canonical* short
        # function name so RES and FRES keep independent settings, which is
        # what the instrument does (§7.1: switching back restores them).
        self.nplc = {"RES": 10.0, "FRES": 10.0, "VOLT": 10.0, "CURR": 10.0}
        self.range = {
            "RES": DEFAULT_RESISTANCE_RANGE,
            "FRES": DEFAULT_RESISTANCE_RANGE,
            "VOLT": 20.0,
            "CURR": 1.0,
        }
        self.autorange = {"RES": True, "FRES": True, "VOLT": True, "CURR": True}
        self.autozero = {"RES": False, "FRES": False, "VOLT": True, "CURR": True}
        self.null_state = {"RES": False, "FRES": False, "VOLT": False, "CURR": False}
        self.null_value = {"RES": 0.0, "FRES": 0.0, "VOLT": 0.0, "CURR": 0.0}
        self.null_auto = {"RES": False, "FRES": False, "VOLT": False, "CURR": False}
        self.temperature_unit = "C"

        self._install_handlers()

    # --- the modelled signal chain --------------------------------------

    def raw_value(self, function: str) -> float:
        """What the terminals present for ``function``, before range/null.

        2-wire resistance sees the leads in series; 4-wire does not. That is
        the entire physical distinction between the two, so it is the one
        thing this method has to get right.
        """
        if function == "RES":
            return self.dut_ohm + self.lead_ohm
        if function == "FRES":
            return self.dut_ohm
        if function == "VOLT":
            return self.dut_volts
        return 0.0

    def _ranges_for(self, function: str) -> tuple[float, ...]:
        if function in ("RES", "FRES"):
            return RESISTANCE_RANGES
        if function == "VOLT":
            return DC_VOLTAGE_RANGES
        return ()

    def _effective_range(self, function: str, value: float) -> Optional[float]:
        """The range in use, autoranging if enabled. None when unranged."""
        ranges = self._ranges_for(function)
        if not ranges:
            return None
        if not self.autorange.get(function, True):
            return self.range.get(function, ranges[-1])
        for r in ranges:
            if abs(value) <= r * _OVERRANGE_FACTOR:
                return r
        return ranges[-1]

    def take_reading(self, function: Optional[str] = None) -> float:
        """One reading, with range limiting and null applied.

        Null is subtracted *after* the overload check: an input that overloads
        the range produces the sentinel regardless of any offset, because the
        ADC saturated before arithmetic could help.
        """
        fn = function or self.function
        key = self._param_key(fn)
        value = self.raw_value(fn)

        rng = self._effective_range(key, value)
        if rng is not None:
            # Record the range actually used, so RANGe? reports what the
            # instrument settled on rather than what was last requested. Real
            # hardware does this, and without it a test could autorange and
            # then read back a range the reading was never taken on.
            self.range[key] = rng
            if abs(value) > rng * _OVERRANGE_FACTOR:
                return OVERLOAD

        if self.null_state.get(key):
            # AUTO means "the first reading becomes the offset, then AUTO
            # switches itself off" (§7.4.4). The reading that *sets* the
            # offset is returned nulled by it, so it reads as ~0.
            if self.null_auto.get(key):
                self.null_value[key] = value
                self.null_auto[key] = False
                log.debug("sdm4065a sim: auto-null captured %g for %s", value, key)
            value -= self.null_value.get(key, 0.0)

        return value

    # --- formatting -----------------------------------------------------

    @staticmethod
    def _fmt(value: float) -> str:
        """Siglent's response format, e.g. ``+1.00000000E+01`` (§7.4.1)."""
        return f"{value:+.8E}"

    def _readings_reply(self) -> str:
        values = [self.take_reading() for _ in range(max(1, self.sample_count))]
        return ", ".join(self._fmt(v) for v in values)

    # --- function naming ------------------------------------------------

    #: Maps whatever arrives in ``FUNCtion "..."`` to the canonical short
    #: form the instrument answers with. ``FUNC?`` returns the short form in
    #: quotation marks (§7.1), so the driver has to strip them.
    _FUNCTION_ALIASES = {
        "VOLT": "VOLT",
        "VOLT:DC": "VOLT",
        "VOLTAGE:DC": "VOLT",
        "VOLT:AC": "VOLT:AC",
        "CURR": "CURR",
        "CURR:DC": "CURR",
        "CURR:AC": "CURR:AC",
        "RES": "RES",
        "RESISTANCE": "RES",
        "FRES": "FRES",
        "FRESISTANCE": "FRES",
        "CAP": "CAP",
        "FREQ": "FREQ",
        "PER": "PER",
        "CONT": "CONT",
        "DIOD": "DIOD",
        "TEMP": "TEMP",
    }

    def _canon(self, raw: str) -> str:
        token = raw.strip().strip('"').strip("'").upper()
        return self._FUNCTION_ALIASES.get(token, token)

    @staticmethod
    def _param_key(function: str) -> str:
        """Which per-function parameter set a function shares.

        AC and DC voltage share the ``VOLT`` sense subsystem here, which is a
        simplification: the instrument keeps them separate. Tests exercise DC.
        """
        return function.split(":")[0]

    # --- handlers -------------------------------------------------------

    def _install_handlers(self) -> None:
        self._install_rootless_aliases()
        self._install_function_handlers()
        self._install_measure_handlers()
        self._install_acquisition_handlers()
        self._install_sense_handlers()

    def _install_rootless_aliases(self) -> None:
        """Accept headers without a leading colon, as Siglent documents them.

        A leading colon is the optional SCPI *root* specifier: ``:SYST:ERR?``
        and ``SYST:ERR?`` mean the same thing to an instrument. The Rigol
        manuals write it, Siglent's does not, and this driver follows its own
        manual — so the base class's ``:SYSTem:ERRor`` registration would not
        match, the query would fall through to the register lookup, and
        ``last_error()`` would answer ``0`` ("no error") no matter what had
        gone wrong. A silently clean error queue is worse than no error queue,
        so every common handler gets both spellings.

        Aliasing here rather than changing :py:func:`normalise` keeps the
        shared helper's behaviour (and the Rigol sims) untouched.
        """
        for key, fn in list(self.handlers.items()):
            if key.startswith(":"):
                self.handlers.setdefault(key.lstrip(":"), fn)

    def _install_function_handlers(self) -> None:
        def func(arg):
            if arg is None:
                return f'"{self.function}"'
            self.function = self._canon(arg)
            return None

        self.handlers[normalise("FUNCtion")] = func

        def conf_query(arg):
            key = self._param_key(self.function)
            rng = self.range.get(key, 0.0)
            # CONF? answers a quoted "FUNC <range>,<resolution>" string (§3.1).
            resolution = rng / 1e6 if rng else 0.0
            return f'"{self.function} {self._fmt(rng)},{self._fmt(resolution)}"'

        self.handlers[normalise("CONFigure")] = conf_query

        def unit_temp(arg):
            if arg is None:
                return self.temperature_unit
            self.temperature_unit = arg.strip().upper()
            return None

        self.handlers[normalise("UNIT:TEMPerature")] = unit_temp
        self.handlers[normalise("*TST")] = lambda arg: "0"

    def _configure(self, function: str, arg: Optional[str]) -> None:
        """Shared ``CONFigure:<fn> [range]`` behaviour.

        ``CONFigure`` resets that function's measurement parameters to their
        defaults — NPLC to 10, null off, range to 2 kΩ for resistance (the
        "set to its default value after a Factory Reset or CONFigure function"
        note repeated throughout §7.4). The driver documents that you must
        configure *then* set NPLC; this is what makes the wrong order fail.
        """
        self.function = function
        key = self._param_key(function)
        self.nplc[key] = 10.0
        self.null_state[key] = False
        self.null_value[key] = 0.0
        self.null_auto[key] = False

        if arg:
            token = arg.split(",")[0].strip().upper()
            if token in ("AUTO", "DEF"):
                self.autorange[key] = True
                self.range[key] = self._default_range(key)
            else:
                try:
                    requested = float(token)
                except ValueError:
                    self.error_queue.append((-224, "Illegal parameter value"))
                    return
                self.autorange[key] = False
                self.range[key] = self._quantise_range(key, requested)
        else:
            self.autorange[key] = True
            self.range[key] = self._default_range(key)

    def _default_range(self, key: str) -> float:
        if key in ("RES", "FRES"):
            return DEFAULT_RESISTANCE_RANGE
        return self.range.get(key, 0.0)

    def _quantise_range(self, key: str, requested: float) -> float:
        """Snap a requested range up to the nearest one the hardware has.

        Real instruments do this silently, which is exactly why the driver
        validates client-side instead of trusting the request to survive.
        """
        ranges = self._ranges_for(key)
        if not ranges:
            return requested
        for r in ranges:
            if requested <= r * (1 + 1e-9):
                return r
        self.error_queue.append((-222, "Data out of range"))
        return ranges[-1]

    def _install_measure_handlers(self) -> None:
        def make_measure(function: str):
            def fn(arg):
                # MEASure:<fn>? [range] is CONFigure + READ? in one step.
                self._configure(function, arg)
                return self._readings_reply()

            return fn

        for header, function in (
            ("MEASure:RESistance", "RES"),
            ("MEASure:FRESistance", "FRES"),
            ("MEASure:VOLTage:DC", "VOLT"),
            ("MEASure:VOLTage:AC", "VOLT:AC"),
            ("MEASure:CURRent:DC", "CURR"),
            ("MEASure:CURRent:AC", "CURR:AC"),
            ("MEASure:CAPacitance", "CAP"),
            ("MEASure:FREQuency", "FREQ"),
            ("MEASure:PERiod", "PER"),
            ("MEASure:CONTinuity", "CONT"),
            ("MEASure:DIODe", "DIOD"),
            ("MEASure:TEMPerature", "TEMP"),
        ):
            self.handlers[normalise(header)] = make_measure(function)

        def make_configure(function: str):
            def fn(arg):
                self._configure(function, arg)
                return None

            return fn

        for header, function in (
            ("CONFigure:RESistance", "RES"),
            ("CONFigure:FRESistance", "FRES"),
            ("CONFigure:VOLTage:DC", "VOLT"),
            ("CONFigure:VOLTage:AC", "VOLT:AC"),
            ("CONFigure:CURRent:DC", "CURR"),
            ("CONFigure:CURRent:AC", "CURR:AC"),
        ):
            self.handlers[normalise(header)] = make_configure(function)

    def _install_acquisition_handlers(self) -> None:
        def samp_count(arg):
            if arg is None:
                return self._fmt(self.sample_count)
            try:
                self.sample_count = max(1, int(float(arg)))
            except ValueError:
                self.error_queue.append((-224, "Illegal parameter value"))
            return None

        self.handlers[normalise("SAMPle:COUNt")] = samp_count
        self.handlers[normalise("READ")] = lambda arg: self._readings_reply()

        def initiate(arg):
            self.reading_buffer = [
                self.take_reading() for _ in range(max(1, self.sample_count))
            ]
            return None

        self.handlers[normalise("INITiate")] = initiate

        def fetch(arg):
            if not self.reading_buffer:
                # FETCh? with nothing acquired is an error, not an empty line.
                self.error_queue.append((-230, "Data corrupt or stale"))
                return self._fmt(0.0)
            return ", ".join(self._fmt(v) for v in self.reading_buffer)

        self.handlers[normalise("FETCh")] = fetch

        def abort(arg):
            self.reading_buffer = []
            return None

        self.handlers[normalise("ABORt")] = abort

    def _install_sense_handlers(self) -> None:
        """Register the per-function SENSe parameters.

        Registered explicitly for each function rather than pattern-matched,
        because ``RES`` and ``FRES`` must not share state — a test that nulls
        2-wire and then reads 4-wire has to see an un-nulled 4-wire reading.
        """
        for function in ("RESistance", "FRESistance", "VOLTage:DC", "CURRent:DC"):
            key = self._param_key(self._canon(function))
            self._install_sense_for(function, key)

    def _install_sense_for(self, function: str, key: str) -> None:
        def nplc(arg):
            if arg is None:
                return self._fmt(self.nplc.get(key, 10.0))
            try:
                self.nplc[key] = float(arg)
            except ValueError:
                self.error_queue.append((-224, "Illegal parameter value"))
            return None

        def rng(arg):
            if arg is None:
                return self._fmt(self.range.get(key, 0.0))
            token = arg.strip().upper()
            ranges = self._ranges_for(key)
            if token == "MIN" and ranges:
                self.range[key] = ranges[0]
            elif token == "MAX" and ranges:
                self.range[key] = ranges[-1]
            elif token == "DEF":
                self.range[key] = self._default_range(key)
            else:
                try:
                    self.range[key] = self._quantise_range(key, float(token))
                except ValueError:
                    self.error_queue.append((-224, "Illegal parameter value"))
                    return None
            # Selecting a fixed range disables autoranging (§7.4.5).
            self.autorange[key] = False
            return None

        def rng_auto(arg):
            if arg is None:
                return "1" if self.autorange.get(key, True) else "0"
            token = arg.strip().upper()
            if token == "ONCE":
                # Autorange immediately, then leave autoranging off (§7.4.6).
                self.range[key] = self._effective_range(
                    key, self.raw_value(key)
                ) or self.range.get(key, 0.0)
                self.autorange[key] = False
            else:
                self.autorange[key] = token in ("ON", "1")
            return None

        def az(arg):
            if arg is None:
                return "1" if self.autozero.get(key, False) else "0"
            self.autozero[key] = arg.strip().upper() in ("ON", "1")
            return None

        def null_state(arg):
            if arg is None:
                return "1" if self.null_state.get(key, False) else "0"
            on = arg.strip().upper() in ("ON", "1")
            self.null_state[key] = on
            if on:
                # The documented side effect (§7.4.2): enabling null also
                # arms automatic null-value selection, so the next reading
                # replaces whatever offset was stored.
                self.null_auto[key] = True
            return None

        def null_value(arg):
            if arg is None:
                return self._fmt(self.null_value.get(key, 0.0))
            try:
                value = float(arg)
            except ValueError:
                self.error_queue.append((-224, "Illegal parameter value"))
                return None
            if key in ("RES", "FRES") and abs(value) > 110e6:
                self.error_queue.append((-222, "Data out of range"))
                return None
            self.null_value[key] = value
            # Specifying a value disables automatic selection (§7.4.3).
            self.null_auto[key] = False
            return None

        def null_auto(arg):
            if arg is None:
                return "1" if self.null_auto.get(key, False) else "0"
            self.null_auto[key] = arg.strip().upper() in ("ON", "1")
            return None

        for suffix, fn in (
            ("NPLC", nplc),
            ("RANGe", rng),
            ("RANGe:AUTO", rng_auto),
            ("AZ", az),
            ("AZ:STATe", az),
            ("NULL", null_state),
            ("NULL:STATe", null_state),
            ("NULL:VALue", null_value),
            ("NULL:VALue:AUTO", null_auto),
        ):
            self.handlers[normalise(f"{function}:{suffix}")] = fn

    # --- reset ----------------------------------------------------------

    def reset(self) -> None:
        """``*RST`` — DC voltage, NPLC 10, null off, autorange on (§7.1)."""
        super().reset()
        self.function = "VOLT"
        self.sample_count = 1
        self.reading_buffer = []
        for key in self.nplc:
            self.nplc[key] = 10.0
            self.null_state[key] = False
            self.null_value[key] = 0.0
            self.null_auto[key] = False
            self.autorange[key] = True
        self.range["RES"] = DEFAULT_RESISTANCE_RANGE
        self.range["FRES"] = DEFAULT_RESISTANCE_RANGE
        self.autozero["RES"] = False
        self.autozero["FRES"] = False

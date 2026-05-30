"""Battery emulator — host-side control loop.

Makes the SMU behave like a battery for DUT testing: dynamically adjusts
output voltage based on instantaneous current draw and a profile's OCV
+ ESR curves. Replaces Qoitech's licensed Battery Emulator entirely on
top of benchctrl's existing wire vocabulary.

How it works
------------
A background thread runs at ``update_interval_s`` (default 10 ms ≈ 100 Hz):

1. Read main current ``I`` from the SMU.
2. Integrate ``I × dt`` to update used capacity → state of charge.
3. Look up ``OCV(SoC)`` and ``ESR(SoC)`` from the profile.
4. Apply series / parallel multipliers.
5. Compute target output ``V_out = OCV_total − I × ESR_total``.
6. Clamp to ``safety_max_voltage_V``.
7. Write ``set_voltage(V_out)``.

The control loop bandwidth is bounded by USB latency (~ms), so this
emulator handles steady-state and slow transients well. Sub-ms ESR
tracking would need the device's own regulator — Otii's licensed
device-side emulator covers that range, but ~100 Hz is good enough for
most IoT loads (TX bursts, sleep/wake cycles, sensor sampling).

Safety
------
The emulator drives voltage onto the output terminals. Connect a DUT
before calling :py:meth:`start`. The constructor accepts a
``safety_max_voltage_V`` parameter (default 5.0 V) that caps the
output voltage absolutely — set this to match your DUT's tolerance.
On any exception or :py:meth:`stop` call, the output is disabled in
a ``finally`` block.

Modes
-----
- ``soc_tracking=True`` (default) — SoC drops as the DUT draws charge.
  Simulates a real discharging cell.
- ``soc_tracking=False`` — SoC is pinned at its initial value. The
  emulator still applies ESR sag in response to load, but the cell
  doesn't appear to "run down". Useful for steady-state DUT
  characterisation at a known operating point.

Series / parallel
-----------------
- ``series`` (default 1) — multiplies OCV and ESR ("two cells in series
  doubles voltage and ESR").
- ``parallel`` (default 1) — divides ESR and multiplies effective
  capacity ("two cells in parallel halves ESR and doubles capacity").

Example::

    from benchctrl.drivers.otii_arc.device import OtiiArc as SMU
    from benchctrl.battery import BatteryProfile
    from benchctrl.battery.emulator import Emulator, EmulatorConfig

    profile = BatteryProfile.load("CR2032-Energizer-(25).json")
    config = EmulatorConfig(
        profile=profile,
        initial_soc=1.0,
        series=1, parallel=1,
        safety_max_voltage_V=3.5,
    )
    with SMU.open() as smu:
        emu = Emulator(smu, config)
        emu.start()
        try:
            # ... run your DUT against the emulated battery ...
            time.sleep(60.0)
            print(emu.state())
        finally:
            emu.stop()
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import TYPE_CHECKING, Optional

from benchctrl.battery.profile import BatteryProfile
from benchctrl.channels import StandardChannel
from benchctrl.exceptions import BenchValueError

if TYPE_CHECKING:
    from benchctrl.interfaces import SourceMeasurementUnit
    # Backward-compatible name in this module so the existing
    # `smu: SMU` parameter annotations don't need to change.
    SMU = SourceMeasurementUnit

log = logging.getLogger("benchctrl.battery.emulator")


@dataclass
class EmulatorConfig:
    """Inputs for the emulator.

    Attributes:
        profile: the battery to emulate.
        initial_soc: initial state of charge, 0.0..1.0 (1.0 = fresh cell).
        series: number of cells in series (multiplies OCV + ESR).
        parallel: number of cells in parallel (divides ESR, scales capacity).
        temperature: which discharge table to use from the profile.
        soc_tracking: if True (default), SoC drops as the DUT discharges
            the emulated cell. If False, SoC is pinned to its initial
            value.
        update_interval_s: control-loop tick (default 0.01 = 100 Hz).
        safety_max_voltage_V: absolute cap on the output. Must be set
            to a value your DUT can tolerate.
        safety_max_used_mAh: optional hard cap on used capacity. If
            specified, the emulator stops the loop and disables the
            output once the cap is reached.
        soc_floor: optional minimum SoC. Below this, the loop stops
            and the output is disabled — useful to model an emergency
            cutoff in your test rig.
        current_read_timeout_s: how long ``read_value(mc)`` may block.
    """

    profile: BatteryProfile
    initial_soc: float = 1.0
    series: int = 1
    parallel: int = 1
    temperature: Optional[float] = None
    soc_tracking: bool = True
    update_interval_s: float = 0.01
    safety_max_voltage_V: float = 5.0
    safety_max_used_mAh: Optional[float] = None
    soc_floor: float = 0.0
    current_read_timeout_s: float = 0.5
    # OC protection during emulation. The SMU acts as a voltage source up
    # to this current; beyond it the device protects itself. Should comfortably
    # exceed the highest current you expect to draw from the emulated cell.
    current_limit_A: float = 0.5
    # SMU voltage range. ``None`` (default) auto-selects ``"low"`` for cells
    # whose fresh OCV (× series multiplier) is ≤ 3.4 V, ``"high"`` otherwise
    # (LiPo, multi-cell packs above 3.5 V, etc.). Override only if you have a
    # specific reason to force a range.
    voltage_range: Optional[str] = None


@dataclass(frozen=True)
class EmulatorState:
    """Snapshot of the emulator's runtime state."""

    soc: float
    used_capacity_mAh: float
    ocv_V: float
    esr_ohm: float
    output_voltage_V: float
    measured_current_A: float
    runtime_s: float
    iteration: int
    running: bool
    stop_reason: Optional[str]


# ---------------------------------------------------------------------------
# Emulator
# ---------------------------------------------------------------------------


class Emulator:
    """Drives an SMU as a battery with OCV + ESR sag.

    Not thread-safe for construction or start/stop; the control thread
    is internal and well-isolated. Safe to call :py:meth:`state` from
    any thread.
    """

    def __init__(self, smu: "SMU", config: EmulatorConfig) -> None:
        self.smu = smu
        self.config = config
        self._validate()

        # Runtime state
        self._used_mAh = self._initial_used_mAh()
        self._iteration = 0
        self._measured_current_A = 0.0
        self._last_ocv = self._cell_ocv(self._per_cell_used_mAh())
        self._last_esr = self._cell_esr(self._per_cell_used_mAh())
        self._last_v_out = self._total_ocv()
        self._t_start: Optional[float] = None
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._lock = threading.RLock()
        self._stop_reason: Optional[str] = None

    # --- public lifecycle -----------------------------------------

    def start(self) -> None:
        """Configure the SMU for CV-mode operation and start the control loop.

        The emulator drives the SMU as a voltage source: ``V = OCV − I·ESR``.
        Before enabling output we explicitly select **constant-voltage**
        regulation so the device sources current into the DUT load rather
        than enforcing a residual CC setpoint left over from a previous
        session. Current limit is armed at ``config.current_limit_A``
        to bound fault current.
        """
        if self._thread is not None:
            raise BenchValueError("emulator is already running")
        # Configure the SMU for voltage-source operation. **No
        # try/except here**: if any configuration call fails the
        # device is in an unknown state — arming the output anyway
        # would let the emulator run with the wrong range, missing
        # current limit, or no CV regulation, silently producing
        # garbage. Let the exception propagate; the caller can decide
        # whether to retry or abort.
        rng = self._select_voltage_range()
        self.smu.set_range(rng)
        self.smu.set_current_limit(self.config.current_limit_A)
        self.smu.set_current_limit_enabled(True)
        self.smu.set_power_regulation("voltage")

        # Seed the output at the open-circuit voltage (no current → no sag).
        # Clamp to safety_max_voltage_V so a profile whose fresh OCV exceeds
        # what the SMU can physically deliver (e.g. LiPo on Arc Pro high
        # range, max ~4.2 V under load) doesn't get rejected at startup,
        # which would otherwise queue a -101 error and stall the loop.
        initial_v = self._total_ocv()
        if initial_v > self.config.safety_max_voltage_V:
            log.warning(
                "initial OCV %.4f V exceeds safety_max_voltage_V %.4f V; clamping",
                initial_v, self.config.safety_max_voltage_V,
            )
            initial_v = self.config.safety_max_voltage_V
        log.debug("seed output to %.4f V", initial_v)
        self.smu.set_voltage(initial_v)
        self.smu.set_output(True)
        self._t_start = time.monotonic()
        self._stop_event.clear()
        self._stop_reason = None
        self._thread = threading.Thread(
            target=self._loop, name="benchctrl-emulator", daemon=True
        )
        self._thread.start()

    def stop(self) -> None:
        """Stop the control loop and disable the output.

        Always safe to call; idempotent.
        """
        if self._thread is None:
            return
        self._stop_event.set()
        self._thread.join(timeout=2.0)
        self._thread = None
        try:
            self.smu.set_output(False)
        except Exception:
            log.warning("failed to disable output on emulator stop", exc_info=True)
        if self._stop_reason is None:
            self._stop_reason = "stopped by caller"

    def run_for(self, seconds: float) -> EmulatorState:
        """Convenience: ``start()``, ``sleep(seconds)``, ``stop()``.

        Returns the final :class:`EmulatorState`.
        """
        self.start()
        try:
            time.sleep(seconds)
        finally:
            self.stop()
        return self.state()

    def state(self) -> EmulatorState:
        """Return a thread-safe snapshot of the emulator's state."""
        with self._lock:
            runtime = (
                time.monotonic() - self._t_start
                if self._t_start is not None
                else 0.0
            )
            return EmulatorState(
                soc=self._compute_soc(),
                used_capacity_mAh=self._used_mAh,
                ocv_V=self._last_ocv * self.config.series,
                esr_ohm=self._last_esr * self.config.series / self.config.parallel,
                output_voltage_V=self._last_v_out,
                measured_current_A=self._measured_current_A,
                runtime_s=runtime,
                iteration=self._iteration,
                running=(self._thread is not None and not self._stop_event.is_set()),
                stop_reason=self._stop_reason,
            )

    # --- helpers --------------------------------------------------

    def _select_voltage_range(self) -> str:
        """Auto-select 'low' or 'high' from the profile's fresh OCV.

        Cells above ~3.4 V fresh OCV (LiPo, Li-ion, multi-cell packs)
        need ``set_range('high')`` so the Arc can drive past the
        low-range cap of ~3.5 V. The user can override via
        ``EmulatorConfig.voltage_range``.
        """
        if self.config.voltage_range is not None:
            return self.config.voltage_range
        fresh_ocv = self.config.profile.ocv_at(0.0)
        pack_v = fresh_ocv * self.config.series
        return "high" if pack_v > 3.4 else "low"

    def _validate(self) -> None:
        if not 0.0 <= self.config.initial_soc <= 1.0:
            raise BenchValueError(
                f"initial_soc must be in [0, 1], got {self.config.initial_soc}"
            )
        if self.config.series < 1:
            raise BenchValueError(f"series must be >= 1, got {self.config.series}")
        if self.config.parallel < 1:
            raise BenchValueError(f"parallel must be >= 1, got {self.config.parallel}")
        if self.config.update_interval_s <= 0:
            raise BenchValueError(
                f"update_interval_s must be > 0, got {self.config.update_interval_s}"
            )
        if not 0.0 <= self.config.soc_floor <= 1.0:
            raise BenchValueError(
                f"soc_floor must be in [0, 1], got {self.config.soc_floor}"
            )
        if self.config.safety_max_voltage_V <= 0:
            raise BenchValueError("safety_max_voltage_V must be > 0")
        if self.config.profile.nominal_capacity_mAh <= 0:
            raise BenchValueError("profile.nominal_capacity_mAh must be > 0")

    def _bank_capacity_mAh(self) -> float:
        return (
            self.config.profile.nominal_capacity_mAh * self.config.parallel
        )

    def _initial_used_mAh(self) -> float:
        return self._bank_capacity_mAh() * (1.0 - self.config.initial_soc)

    def _per_cell_used_mAh(self) -> float:
        # The profile is per-cell. With ``parallel`` cells, used charge
        # divides across them so each cell sees ``used_mAh / parallel``.
        return self._used_mAh / self.config.parallel

    def _cell_ocv(self, per_cell_used: float) -> float:
        return self.config.profile.ocv_at(per_cell_used, temperature=self.config.temperature)

    def _cell_esr(self, per_cell_used: float) -> float:
        return self.config.profile.esr_at(per_cell_used, temperature=self.config.temperature)

    def _total_ocv(self) -> float:
        return self._cell_ocv(self._per_cell_used_mAh()) * self.config.series

    def _compute_soc(self) -> float:
        cap = self._bank_capacity_mAh()
        if cap <= 0:
            return 0.0
        return max(0.0, 1.0 - self._used_mAh / cap)

    # --- control loop ---------------------------------------------

    def _loop(self) -> None:
        last_tick = time.monotonic()
        while not self._stop_event.is_set():
            now = time.monotonic()
            dt = now - last_tick
            last_tick = now

            try:
                I = self._read_current()
            except Exception:
                # Connection might have flickered — back off briefly and retry
                log.warning("read_current failed", exc_info=True)
                time.sleep(self.config.update_interval_s)
                continue

            with self._lock:
                self._measured_current_A = I

                # SoC integration
                if self.config.soc_tracking and dt > 0:
                    drawn_mAh = I * dt / 3.6  # A * s = C; C/3.6 = mAh
                    self._used_mAh += drawn_mAh
                    self._used_mAh = max(0.0, self._used_mAh)

                per_cell_used = self._per_cell_used_mAh()
                ocv = self._cell_ocv(per_cell_used)
                esr = self._cell_esr(per_cell_used)
                self._last_ocv = ocv
                self._last_esr = esr

                # Series multiplies V + ESR; parallel divides ESR
                total_ocv = ocv * self.config.series
                total_esr = esr * self.config.series / self.config.parallel
                v_out = total_ocv - I * total_esr
                v_out = max(0.0, min(v_out, self.config.safety_max_voltage_V))
                self._last_v_out = v_out

                # Check stop conditions
                if (
                    self.config.safety_max_used_mAh is not None
                    and self._used_mAh >= self.config.safety_max_used_mAh
                ):
                    self._stop_reason = "safety_max_used_mAh reached"
                    break
                if self._compute_soc() <= self.config.soc_floor:
                    self._stop_reason = "soc_floor reached"
                    break

                self._iteration += 1

            # Drive the SMU outside the lock so state() doesn't block
            # on slow USB writes. Trade-off: state() can observe
            # _last_v_out (set inside the lock) updated before the
            # SMU has actually received the new voltage. Acceptable
            # for monitoring; not acceptable as a control signal —
            # callers who need "SMU is definitely at v_out now" must
            # use the SMU's own readback after start() settles.
            #
            # Also: if set_voltage is slower than update_interval_s
            # (e.g. USB hub buffering delays), consecutive iterations
            # can queue writes faster than the device drains them.
            # Currently acceptable; if it becomes a problem, add a
            # "in-flight" flag and skip the iteration.
            try:
                self.smu.set_voltage(v_out)
            except Exception:
                log.warning("set_voltage failed", exc_info=True)

            # Sleep until next tick
            elapsed = time.monotonic() - now
            remaining = self.config.update_interval_s - elapsed
            if remaining > 0:
                self._stop_event.wait(remaining)

        # Loop exit — caller will disable output
        try:
            self.smu.set_voltage(0.0)
        except Exception:
            pass

    def _read_current(self) -> float:
        """Read the main current. Raises on read failure.

        Earlier versions caught all exceptions and returned 0.0 here,
        which converted hard failures (queued SCPI errors, transport
        disconnects, etc.) into "I = 0 forever, V = OCV" — the loop
        kept running and the saved ``state()`` looked plausible. The
        outer loop already has a try/except around this call that
        sleeps and continues, so retries on transient failures still
        work — but real errors now surface in the warning log instead
        of being silently swallowed."""
        return self.smu.read_value(
            StandardChannel.MAIN_CURRENT,
            timeout=self.config.current_read_timeout_s,
        )

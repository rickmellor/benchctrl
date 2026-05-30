"""Hardware-free tests for benchctrl.battery.profiler.

These verify the orchestration logic against a mock SMU. Real-hardware
validation is a separate task (needs a battery connected to the output
terminals).
"""

from __future__ import annotations

import time
from collections import deque
from dataclasses import dataclass, field

import pytest

from benchctrl import Channel
from benchctrl.battery import (
    Battery,
    DischargeProfile,
    DischargeStep,
    ExitConditions,
)
from benchctrl.battery.profiler import (
    Profiler,
    ProfilerConfig,
    ProfilerSample,
)
from benchctrl.exceptions import SMUValueError


# ---------------------------------------------------------------------------
# Mock SMU
# ---------------------------------------------------------------------------


@dataclass
class _MockSMU:
    """Pretends to be an benchctrl SMU for the profiler's purposes.

    Models a simple battery: starts at ``ocv_initial`` V, OCV drops
    linearly to ``ocv_final`` V over ``capacity_mAh`` of consumed charge.
    Loaded voltage = OCV - I * esr.
    """

    ocv_initial: float = 4.2
    ocv_final: float = 3.0
    capacity_mAh: float = 1000.0
    esr: float = 0.1
    # Internal state
    capacity_consumed_mAh: float = 0.0
    current_setpoint_A: float = 0.0
    output_enabled: bool = False
    _last_apply_time: float = 0.0
    # History for assertions
    set_main_current_calls: list[float] = field(default_factory=list)
    set_output_calls: list[bool] = field(default_factory=list)
    # Track time so the mock "consumes" capacity proportional to current * elapsed
    _t_zero: float = field(default_factory=time.monotonic)
    _last_t: float = 0.0

    def __post_init__(self) -> None:
        self._last_t = time.monotonic()

    # --- methods the profiler calls --------------------------------

    def set_main_current(self, amps: float) -> None:
        # Advance state to account for charge drawn since last set
        self._accrue()
        self.current_setpoint_A = amps
        self.set_main_current_calls.append(amps)

    def set_output(self, enable: bool) -> None:
        self.output_enabled = enable
        self.set_output_calls.append(enable)

    # No-op mode setters the profiler calls during setup
    def set_range(self, range_: str) -> None: pass
    def set_current_limit_enabled(self, enable: bool) -> None: pass
    def set_power_regulation(self, mode: str) -> None: pass

    def read_value(self, channel, timeout: float = 2.0) -> float:
        self._accrue()
        # Linearly interpolate OCV
        frac = min(1.0, self.capacity_consumed_mAh / self.capacity_mAh)
        ocv = self.ocv_initial + frac * (self.ocv_final - self.ocv_initial)
        if channel is Channel.MAIN_VOLTAGE or channel == "mv":
            # Sink convention: setpoint is negative when drawing from a cell.
            # V_loaded = OCV - |I_sink| * ESR
            return ocv - abs(self.current_setpoint_A) * self.esr
        if channel is Channel.MAIN_CURRENT or channel == "mc":
            return self.current_setpoint_A
        return float("nan")

    def read_window(self, channels, duration_s):
        """Mock the new SMU.read_window: return one current-state sample per channel."""
        self._accrue()
        if duration_s > 0:
            time.sleep(duration_s)
        out = {}
        for ch in channels:
            ch_obj = Channel.coerce(ch) if not isinstance(ch, Channel) else ch
            out[ch_obj] = [self.read_value(ch_obj)]
        return out

    def _accrue(self) -> None:
        """Consume capacity proportional to (|current| * elapsed). Sink convention."""
        now = time.monotonic()
        elapsed = now - self._last_t
        self._last_t = now
        if self.output_enabled:
            self.capacity_consumed_mAh += (
                abs(self.current_setpoint_A) * elapsed / 3.6
            )

    def get_fw_version(self) -> str:
        return "mock-3.1.3"

    def get_device_id(self) -> str:
        return "MOCK0123456789ABCDEF0123456789AB"

    # (the sink-convention _accrue lives above, near read_value)


def _fast_config(
    *,
    high_amps: float = 0.10,
    low_amps: float = 0.001,
    high_time: float = 0.10,
    low_time: float = 0.20,
    cutoff_v: float = 3.2,
    iterations: int = 0,
    capacity_mAh: float = 100.0,
) -> ProfilerConfig:
    """A test config with short step times for fast unit tests."""
    return ProfilerConfig(
        discharge_profile=DischargeProfile(
            low=DischargeStep("current", low_amps, low_time),
            high=DischargeStep("current", high_amps, high_time),
            exit_conditions=ExitConditions(
                iterations=iterations, ocv=cutoff_v, voltage=cutoff_v - 0.1
            ),
        ),
        battery=Battery(
            capacity=capacity_mAh, capacity_unit="mAh",
            voltage=3.7, voltage_unit="V",
            manufacturer="MockCo", model="Test-100",
        ),
        relaxation_time_s=0.02,
        measurement_window_s=0.01,
        initial_settle_time_s=0.02,
        progress_throttle_s=0.0,
    )


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


def test_profiler_rejects_too_short_step():
    config = ProfilerConfig(
        discharge_profile=DischargeProfile(
            low=DischargeStep("current", 0.001, 60.0),
            high=DischargeStep("current", 0.01, 0.002),  # 2 ms — too short
            exit_conditions=ExitConditions(),
        ),
        battery=Battery(capacity=200.0),
    )
    with pytest.raises(SMUValueError, match="below the profiler's minimum"):
        Profiler(_MockSMU(), config)


def test_profiler_rejects_negative_current():
    config = ProfilerConfig(
        discharge_profile=DischargeProfile(
            low=DischargeStep("current", -0.001, 1.0),
            high=DischargeStep("current", 0.01, 1.0),
            exit_conditions=ExitConditions(),
        ),
        battery=Battery(capacity=200.0),
    )
    with pytest.raises(SMUValueError, match="must be >= 0"):
        Profiler(_MockSMU(), config)


def test_profiler_rejects_unsupported_mode():
    config = ProfilerConfig(
        discharge_profile=DischargeProfile(
            low=DischargeStep("power", 0.001, 1.0),
            high=DischargeStep("power", 0.01, 1.0),
            exit_conditions=ExitConditions(),
        ),
        battery=Battery(capacity=200.0),
    )
    with pytest.raises(SMUValueError, match="phase 3"):
        Profiler(_MockSMU(), config)


def test_profiler_rejects_zero_capacity():
    config = _fast_config(capacity_mAh=0.0)
    with pytest.raises(SMUValueError):
        Profiler(_MockSMU(), config)


# ---------------------------------------------------------------------------
# Run behaviour against the mock
# ---------------------------------------------------------------------------


def test_profiler_runs_and_produces_samples():
    config = _fast_config(iterations=3)
    smu = _MockSMU(ocv_initial=4.2, ocv_final=3.0, capacity_mAh=200.0)
    profiler = Profiler(smu, config)
    result = profiler.run()
    assert len(result.samples) == 3
    assert result.stop_reason.startswith("iteration limit")
    assert not result.aborted
    assert result.profile.discharge_tables[0].table


def test_profiler_stops_on_ocv_cutoff():
    # With a fast-decaying mock and a high cutoff, the run should stop early
    config = _fast_config(cutoff_v=4.0, iterations=0)
    smu = _MockSMU(ocv_initial=4.2, ocv_final=3.0, capacity_mAh=10.0)  # tiny capacity
    profiler = Profiler(smu, config)
    result = profiler.run()
    assert result.stop_reason.startswith("OCV cutoff") or result.stop_reason.startswith(
        "loaded-voltage cutoff"
    )


def test_profiler_disables_output_at_end():
    config = _fast_config(iterations=2)
    smu = _MockSMU()
    profiler = Profiler(smu, config)
    profiler.run()
    # Last set_output call must be False
    assert smu.set_output_calls[-1] is False
    # Last set_main_current call must be 0
    assert smu.set_main_current_calls[-1] == 0.0


def test_profiler_alternates_high_and_low_steps():
    config = _fast_config(high_amps=0.05, low_amps=0.001, iterations=2)
    smu = _MockSMU()
    profiler = Profiler(smu, config)
    profiler.run()
    # The wire convention is: positive = source, negative = sink. Battery
    # profiling sinks from the cell, so the profiler negates the user-supplied
    # positive load magnitudes internally. We expect:
    # 0 (settle), [-0.05, -0.001] x 2, 0 (cleanup)
    non_zero = [c for c in smu.set_main_current_calls if c not in (0.0,)]
    assert non_zero[0] == -0.05
    assert non_zero[1] == -0.001
    assert non_zero[2] == -0.05
    assert non_zero[3] == -0.001


def test_profiler_computes_nonnegative_esr():
    # The mock's read_value returns current_setpoint as mc. With the
    # profiler now negating internally for sink, current_loaded will be
    # negative; ESR = (OCV - V_loaded) / |I_loaded| must still be
    # non-negative for a cell with non-decreasing terminal voltage under
    # discharge. We bump initial OCV high enough for the slope to read
    # positive ESR throughout.
    config = _fast_config(iterations=2)
    smu = _MockSMU(esr=0.5, ocv_initial=4.2, ocv_final=3.0, capacity_mAh=1000.0)
    profiler = Profiler(smu, config)
    result = profiler.run()
    for s in result.samples:
        assert s.resistance >= 0.0


def test_profiler_progress_callback_fires():
    config = _fast_config(iterations=4)
    smu = _MockSMU()
    profiler = Profiler(smu, config)
    seen: list[ProfilerSample] = []
    profiler.run(progress=lambda s: seen.append(s))
    # progress_throttle_s=0 so all 4 should fire
    assert len(seen) == 4


def test_profiler_progress_callback_exception_does_not_crash():
    config = _fast_config(iterations=2)
    smu = _MockSMU()
    profiler = Profiler(smu, config)

    def bad_cb(s):
        raise RuntimeError("BOOM")

    # Must not raise
    result = profiler.run(progress=bad_cb)
    assert len(result.samples) == 2


def test_profiler_abort_stops_after_current_cycle():
    config = _fast_config(iterations=100, low_time=0.05, high_time=0.05)
    smu = _MockSMU()
    profiler = Profiler(smu, config)

    def cb_abort(s):
        if s.iteration >= 2:
            profiler.abort()

    result = profiler.run(progress=cb_abort)
    assert result.aborted is True
    assert result.stop_reason == "aborted"
    assert len(result.samples) >= 2
    assert len(result.samples) <= 4


def test_profiler_built_profile_roundtrips_through_json(tmp_path):
    config = _fast_config(iterations=3)
    smu = _MockSMU()
    profiler = Profiler(smu, config)
    result = profiler.run()
    out = tmp_path / "mock.json"
    result.profile.save(out)
    # Load + assert it matches structurally
    from benchctrl.battery import BatteryProfile

    loaded = BatteryProfile.load(out)
    assert loaded.battery.manufacturer == "MockCo"
    assert loaded.battery.model == "Test-100"
    assert len(loaded.discharge_tables) == 1
    assert len(loaded.discharge_tables[0].table) == 3
    # Device info populated from mock GET calls
    assert loaded.discharge_tables[0].device.firmware_version == "mock-3.1.3"
    assert loaded.discharge_tables[0].device.id.startswith("MOCK")


def test_profiler_runtime_recorded_in_result():
    config = _fast_config(iterations=2)
    smu = _MockSMU()
    profiler = Profiler(smu, config)
    result = profiler.run()
    assert result.runtime_s > 0

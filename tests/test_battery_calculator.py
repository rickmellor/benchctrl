"""Hardware-free tests for benchctrl.battery.calculator."""

from __future__ import annotations

import math

import pytest

from benchctrl.battery import (
    Battery,
    BatteryProfile,
    DischargeProfile,
    DischargeSample,
    DischargeStep,
    DischargeTable,
    DutyCycle,
    ExitConditions,
    duty_cycle_from_recording,
    estimate_life_constant_current,
    estimate_life_from_profile,
)
from benchctrl.exceptions import BenchValueError


# ---------------------------------------------------------------------------
# DutyCycle
# ---------------------------------------------------------------------------


def test_dutycycle_cycle_time():
    d = DutyCycle(active_current_A=0.020, active_time_s=0.1,
                  sleep_current_A=5e-6, sleep_time_s=60.0)
    assert d.cycle_time_s == pytest.approx(60.1)


def test_dutycycle_average_current():
    """avg I = (I_a*T_a + I_s*T_s) / (T_a+T_s)"""
    d = DutyCycle(active_current_A=0.020, active_time_s=0.1,
                  sleep_current_A=5e-6, sleep_time_s=60.0)
    expected = (0.020 * 0.1 + 5e-6 * 60.0) / 60.1
    assert d.average_current_A == pytest.approx(expected)


def test_dutycycle_rejects_negative_durations():
    with pytest.raises(BenchValueError):
        DutyCycle(active_current_A=0.001, active_time_s=-1,
                  sleep_current_A=0, sleep_time_s=1)


def test_dutycycle_rejects_zero_total_time():
    with pytest.raises(BenchValueError):
        DutyCycle(active_current_A=0.001, active_time_s=0,
                  sleep_current_A=0, sleep_time_s=0)


# ---------------------------------------------------------------------------
# Constant-current estimator
# ---------------------------------------------------------------------------


def test_cc_estimator_simple_math():
    """1000 mAh / 1 mA average current → 1000 hours = 3.6 million seconds."""
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0.001, sleep_time_s=0.0)
    est = estimate_life_constant_current(capacity_mAh=1000.0, duty_cycle=duty)
    assert est.runtime_s == pytest.approx(1000.0 * 3600.0)
    assert est.average_current_A == pytest.approx(0.001)
    assert est.average_current_mA == pytest.approx(1.0)
    assert est.method == "constant_current"
    assert est.stop_reason == "usable capacity exhausted"


def test_cc_estimator_safety_margin():
    """10% safety margin should reduce runtime by 10%."""
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0.001, sleep_time_s=0.0)
    full = estimate_life_constant_current(capacity_mAh=1000.0, duty_cycle=duty)
    reserved = estimate_life_constant_current(
        capacity_mAh=1000.0, duty_cycle=duty, safety_margin_pct=10.0
    )
    assert reserved.runtime_s == pytest.approx(full.runtime_s * 0.9)
    assert reserved.safety_margin_loss_mAh == pytest.approx(100.0)


def test_cc_estimator_self_discharge():
    """1% self-discharge per month on 1000 mAh = ~13.9 µA constant drain."""
    duty = DutyCycle(active_current_A=0.0, active_time_s=0,
                     sleep_current_A=0.0, sleep_time_s=1.0)
    # No active drain — only self-discharge dictates life
    est = estimate_life_constant_current(
        capacity_mAh=1000.0,
        duty_cycle=duty,
        self_discharge_per_month_pct=1.0,
    )
    # 1% per month = 100 months to deplete = 100 * 30 * 24 * 3600 s
    expected_s = 100 * 30 * 24 * 3600
    assert est.runtime_s == pytest.approx(expected_s, rel=0.01)
    assert est.self_discharge_loss_mAh == pytest.approx(1000.0, rel=0.01)


def test_cc_estimator_zero_drain_returns_infinite():
    """No active or self-discharge → infinite life."""
    duty = DutyCycle(active_current_A=0.0, active_time_s=1.0,
                     sleep_current_A=0.0, sleep_time_s=1.0)
    est = estimate_life_constant_current(capacity_mAh=100.0, duty_cycle=duty)
    assert est.runtime_s == float("inf")
    assert est.runtime_human == "infinite"


def test_cc_estimator_rejects_bad_inputs():
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0, sleep_time_s=0.0)
    with pytest.raises(BenchValueError):
        estimate_life_constant_current(capacity_mAh=0, duty_cycle=duty)
    with pytest.raises(BenchValueError):
        estimate_life_constant_current(
            capacity_mAh=100, duty_cycle=duty, safety_margin_pct=100
        )
    with pytest.raises(BenchValueError):
        estimate_life_constant_current(
            capacity_mAh=100, duty_cycle=duty, self_discharge_per_month_pct=-1
        )


# ---------------------------------------------------------------------------
# Profile-based estimator
# ---------------------------------------------------------------------------


def _flat_profile_for_test(capacity_mAh=1000.0, ocv=3.7, cutoff=3.0):
    """Build a profile with a flat voltage that drops to cutoff at end."""
    return BatteryProfile(
        battery=Battery(capacity=capacity_mAh, capacity_unit="mAh",
                        voltage=ocv, voltage_unit="V"),
        discharge_tables=[
            DischargeTable(
                table=[
                    DischargeSample(voltage=ocv, resistance=0.1, capacity=0.0),
                    DischargeSample(voltage=ocv, resistance=0.1, capacity=capacity_mAh * 0.9),
                    DischargeSample(voltage=cutoff, resistance=0.5, capacity=capacity_mAh),
                ],
                discharge_profile=DischargeProfile(
                    low=DischargeStep("current", 0.001, 60),
                    high=DischargeStep("current", 0.5, 1),
                    exit_conditions=ExitConditions(iterations=0, ocv=cutoff, voltage=cutoff),
                ),
            )
        ],
    )


def test_profile_estimator_close_to_cc_for_flat_battery():
    """A flat-voltage profile should produce results close to the CC estimator."""
    profile = _flat_profile_for_test(capacity_mAh=1000.0)
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0.001, sleep_time_s=0.0)
    cc = estimate_life_constant_current(capacity_mAh=1000.0, duty_cycle=duty)
    pf = estimate_life_from_profile(profile=profile, duty_cycle=duty)
    # Should agree to ~1 cycle (= 1 second here)
    assert abs(pf.runtime_s - cc.runtime_s) < duty.cycle_time_s * 2


def test_profile_estimator_stops_on_voltage_cutoff():
    """If we set a cutoff above the cell's curve, runtime is zero-ish."""
    profile = _flat_profile_for_test(capacity_mAh=1000.0, ocv=3.7, cutoff=3.0)
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0.001, sleep_time_s=0.0)
    est = estimate_life_from_profile(
        profile=profile, duty_cycle=duty, cutoff_voltage=3.6
    )
    # Profile voltage is 3.7 until ~900 mAh; first sample where V < 3.6 is past 90%.
    # So we should run for >= 90% of cc-estimator life.
    cc = estimate_life_constant_current(capacity_mAh=1000.0, duty_cycle=duty)
    assert est.runtime_s >= 0.9 * cc.runtime_s


def test_profile_estimator_iterates_safety_margin():
    profile = _flat_profile_for_test(capacity_mAh=1000.0)
    duty = DutyCycle(active_current_A=0.001, active_time_s=1.0,
                     sleep_current_A=0.001, sleep_time_s=0.0)
    full = estimate_life_from_profile(profile=profile, duty_cycle=duty)
    reserved = estimate_life_from_profile(
        profile=profile, duty_cycle=duty, safety_margin_pct=20.0
    )
    # 20% reserved should reduce runtime by ~20%
    assert reserved.runtime_s == pytest.approx(full.runtime_s * 0.8, rel=0.01)


def test_profile_estimator_rejects_zero_capacity():
    profile = BatteryProfile(battery=Battery(capacity=0.0))
    profile.discharge_tables.append(
        DischargeTable(
            table=[DischargeSample(3.0, 0.1, 0.0)],
            discharge_profile=DischargeProfile(
                DischargeStep("current", 0.001, 1),
                DischargeStep("current", 0.001, 1),
                ExitConditions(),
            ),
        )
    )
    duty = DutyCycle(active_current_A=0.001, active_time_s=1,
                     sleep_current_A=0, sleep_time_s=0)
    with pytest.raises(BenchValueError):
        estimate_life_from_profile(profile=profile, duty_cycle=duty)


def test_profile_estimator_runs_against_bundled_cr2032():
    """Smoke test against the real CR2032 bundled profile if Otii is installed."""
    import os
    from pathlib import Path

    base = Path(os.environ.get("LOCALAPPDATA", "")) / "otii3"
    profiles = None
    for sub in base.glob("app-*/resources/batteryprofiles"):
        profiles = sub
        break
    if profiles is None:
        pytest.skip("Otii bundled profiles not found")

    profile = BatteryProfile.load(profiles / "CR2032-Energizer-(25).json")
    # IoT sensor: 20 mA / 100 ms / 60 s
    duty = DutyCycle(
        active_current_A=0.020, active_time_s=0.1,
        sleep_current_A=5e-6, sleep_time_s=60.0,
    )
    est = estimate_life_from_profile(profile=profile, duty_cycle=duty)
    # Should be roughly 230 mAh / 38 µA ~ 6,050 hours ~ 252 days (give or take 10%)
    days = est.runtime_s / (24 * 3600)
    assert 200 <= days <= 300, f"unrealistic CR2032 IoT life: {days:.1f} days"


# ---------------------------------------------------------------------------
# DutyCycle from Recording
# ---------------------------------------------------------------------------


def test_duty_cycle_from_recording():
    """Given a synthetic recording with a step in mc, extract the duty cycle."""
    from benchctrl import Channel, Recording

    rec = Recording(name="dc-test")
    mc = rec._ensure_buffer(Channel.MAIN_CURRENT, 1000)
    # 0.0-1.0s: 5 µA (sleep)
    mc.extend([5e-6] * 1000)
    # 1.0-2.0s: 20 mA (active)
    mc.extend([0.020] * 1000)
    # 2.0-3.0s: 5 µA (sleep)
    mc.extend([5e-6] * 1000)

    duty = duty_cycle_from_recording(
        rec,
        active_window=(1.0, 2.0),
        sleep_window=(0.0, 1.0),
    )
    assert duty.active_current_A == pytest.approx(0.020)
    assert duty.sleep_current_A == pytest.approx(5e-6)
    assert duty.active_time_s == 1.0
    assert duty.sleep_time_s == 1.0


def test_duty_cycle_from_recording_rejects_missing_channel():
    from benchctrl import Channel, Recording

    rec = Recording(name="missing")
    rec._ensure_buffer(Channel.MAIN_VOLTAGE, 1000)
    with pytest.raises(BenchValueError):
        duty_cycle_from_recording(
            rec, active_window=(0, 1), sleep_window=(1, 2), channel="mc"
        )


def test_duty_cycle_from_recording_rejects_empty_window():
    from benchctrl import Channel, Recording

    rec = Recording(name="empty")
    mc = rec._ensure_buffer(Channel.MAIN_CURRENT, 1000)
    mc.extend([0.001] * 10)  # 10 ms of data
    with pytest.raises(BenchValueError):
        # Window outside the data range
        duty_cycle_from_recording(
            rec, active_window=(0, 0.005), sleep_window=(100, 101)
        )


# ---------------------------------------------------------------------------
# Humanize formatting (private but worth a smoke test)
# ---------------------------------------------------------------------------


def test_humanize_renders_long_durations():
    from benchctrl.battery.calculator import _humanize_seconds

    assert _humanize_seconds(0) == "0 seconds"
    assert _humanize_seconds(1) == "1 second"
    assert _humanize_seconds(60) == "1 minute"
    assert _humanize_seconds(3600) == "1 hour"
    assert _humanize_seconds(86400) == "1 day"
    h = _humanize_seconds(3600 * 24 * 5 + 3600 * 3 + 60 * 22 + 14)
    assert h == "5 days 3 hours 22 minutes 14 seconds"

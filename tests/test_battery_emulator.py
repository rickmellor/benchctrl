"""Hardware-free tests for opensmu.battery.emulator against a mock SMU.

Real-hardware validation (DUT with known current draw connected to the
SMU's output terminals) is a separate task.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field

import pytest

from opensmu import Channel
from opensmu.battery import (
    Battery,
    BatteryProfile,
    DischargeProfile,
    DischargeSample,
    DischargeStep,
    DischargeTable,
    ExitConditions,
)
from opensmu.battery.emulator import Emulator, EmulatorConfig
from opensmu.exceptions import SMUValueError


# ---------------------------------------------------------------------------
# Mock SMU (different from the profiler mock — this one models a DUT load)
# ---------------------------------------------------------------------------


@dataclass
class _MockSMU:
    """Mock SMU for the emulator. Simulates a DUT drawing constant current."""

    dut_current_A: float = 0.010   # 10 mA DUT
    set_voltage_calls: list[float] = field(default_factory=list)
    set_output_calls: list[bool] = field(default_factory=list)
    voltage_setpoint_V: float = 0.0
    output_enabled: bool = False

    def set_main_voltage(self, volts: float) -> None:
        self.set_voltage_calls.append(volts)
        self.voltage_setpoint_V = volts

    def set_output(self, enable: bool) -> None:
        self.output_enabled = enable
        self.set_output_calls.append(enable)

    def read_value(self, channel, timeout: float = 0.5) -> float:
        if channel is Channel.MAIN_CURRENT or channel == "mc":
            return self.dut_current_A if self.output_enabled else 0.0
        if channel is Channel.MAIN_VOLTAGE or channel == "mv":
            return self.voltage_setpoint_V if self.output_enabled else 0.0
        return float("nan")


def _flat_profile(capacity_mAh: float = 1000.0, ocv: float = 3.7,
                  esr: float = 0.1, cutoff: float = 3.0) -> BatteryProfile:
    """A simple linear-decay profile."""
    return BatteryProfile(
        battery=Battery(capacity=capacity_mAh, capacity_unit="mAh",
                        voltage=ocv, voltage_unit="V"),
        discharge_tables=[
            DischargeTable(
                table=[
                    DischargeSample(voltage=ocv, resistance=esr, capacity=0.0),
                    DischargeSample(voltage=cutoff, resistance=esr * 5,
                                    capacity=capacity_mAh),
                ],
                discharge_profile=DischargeProfile(
                    low=DischargeStep("current", 0.001, 60),
                    high=DischargeStep("current", 0.01, 1),
                    exit_conditions=ExitConditions(iterations=0, ocv=cutoff,
                                                   voltage=cutoff),
                ),
            )
        ],
    )


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def test_emulator_rejects_bad_soc():
    with pytest.raises(SMUValueError):
        EmulatorConfig(profile=_flat_profile(), initial_soc=1.5)


def test_emulator_rejects_zero_capacity_profile():
    p = BatteryProfile(battery=Battery(capacity=0.0))
    p.discharge_tables.append(
        DischargeTable(
            table=[DischargeSample(3.0, 0.1, 0.0)],
            discharge_profile=DischargeProfile(
                DischargeStep("current", 0.001, 1),
                DischargeStep("current", 0.001, 1),
                ExitConditions(),
            ),
        )
    )
    config = EmulatorConfig(profile=p)
    with pytest.raises(SMUValueError):
        Emulator(_MockSMU(), config)


def test_emulator_rejects_zero_series_parallel():
    config = EmulatorConfig(profile=_flat_profile(), series=0)
    with pytest.raises(SMUValueError):
        Emulator(_MockSMU(), config)
    config = EmulatorConfig(profile=_flat_profile(), parallel=0)
    with pytest.raises(SMUValueError):
        Emulator(_MockSMU(), config)


def test_emulator_rejects_zero_update_interval():
    config = EmulatorConfig(profile=_flat_profile(), update_interval_s=0)
    with pytest.raises(SMUValueError):
        Emulator(_MockSMU(), config)


# ---------------------------------------------------------------------------
# Run behaviour
# ---------------------------------------------------------------------------


def test_emulator_seeds_output_at_total_ocv_on_start():
    smu = _MockSMU(dut_current_A=0.0)
    config = EmulatorConfig(
        profile=_flat_profile(ocv=3.7),
        initial_soc=1.0,
        series=2,        # two in series doubles OCV
        update_interval_s=0.1,
        safety_max_voltage_V=10.0,
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        # First write is the seed at total OCV = 7.4 V (3.7 × 2)
        assert smu.set_voltage_calls[0] == pytest.approx(7.4, abs=0.001)
        assert smu.set_output_calls[0] is True
    finally:
        emu.stop()


def test_emulator_stop_disables_output():
    smu = _MockSMU()
    emu = Emulator(smu, EmulatorConfig(profile=_flat_profile(), update_interval_s=0.05))
    emu.start()
    time.sleep(0.15)
    emu.stop()
    assert smu.set_output_calls[-1] is False


def test_emulator_double_start_raises():
    smu = _MockSMU()
    emu = Emulator(smu, EmulatorConfig(profile=_flat_profile(), update_interval_s=0.05))
    emu.start()
    try:
        with pytest.raises(SMUValueError):
            emu.start()
    finally:
        emu.stop()


def test_emulator_stop_is_idempotent():
    smu = _MockSMU()
    emu = Emulator(smu, EmulatorConfig(profile=_flat_profile(), update_interval_s=0.05))
    emu.start()
    emu.stop()
    emu.stop()  # second call must not raise


def test_emulator_applies_esr_sag_under_load():
    smu = _MockSMU(dut_current_A=0.500)   # 500 mA — significant sag
    config = EmulatorConfig(
        profile=_flat_profile(ocv=3.7, esr=0.1),
        initial_soc=1.0,
        update_interval_s=0.05,
        safety_max_voltage_V=4.0,
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(0.3)
        st = emu.state()
        # Expected V = OCV - I*ESR = 3.7 - 0.5*0.1 = 3.65 V
        assert st.output_voltage_V == pytest.approx(3.65, abs=0.05)
        assert st.measured_current_A == pytest.approx(0.500)
    finally:
        emu.stop()


def test_emulator_tracks_soc_over_time():
    """100 mA DUT for ~300 ms should consume ~8.3 µAh — visible in SoC."""
    smu = _MockSMU(dut_current_A=0.100)
    config = EmulatorConfig(
        profile=_flat_profile(capacity_mAh=1.0),  # 1 mAh cell — drains fast
        initial_soc=1.0,
        update_interval_s=0.02,
        safety_max_voltage_V=4.0,
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(0.5)
        st1 = emu.state()
    finally:
        emu.stop()
    assert st1.used_capacity_mAh > 0
    assert st1.soc < 1.0


def test_emulator_soc_tracking_off_keeps_soc_constant():
    smu = _MockSMU(dut_current_A=0.100)
    config = EmulatorConfig(
        profile=_flat_profile(capacity_mAh=1.0),
        initial_soc=0.8,
        soc_tracking=False,
        update_interval_s=0.02,
        safety_max_voltage_V=4.0,
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(0.3)
        st = emu.state()
    finally:
        emu.stop()
    # SoC pinned at 0.8 even though current was being drawn
    assert st.soc == pytest.approx(0.8, abs=0.01)


def test_emulator_safety_max_voltage_clamps_output():
    """If profile + series want > safety cap, output stays clamped."""
    smu = _MockSMU(dut_current_A=0.0)
    config = EmulatorConfig(
        profile=_flat_profile(ocv=3.7),
        series=3,        # 3.7 × 3 = 11.1 V wanted
        safety_max_voltage_V=5.0,
        update_interval_s=0.05,
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        time.sleep(0.15)
        st = emu.state()
    finally:
        emu.stop()
    assert st.output_voltage_V == pytest.approx(5.0, abs=0.05)


def test_emulator_stops_when_safety_max_used_mAh_reached():
    smu = _MockSMU(dut_current_A=0.500)   # 500 mA
    config = EmulatorConfig(
        profile=_flat_profile(capacity_mAh=100.0),
        update_interval_s=0.02,
        safety_max_voltage_V=4.0,
        safety_max_used_mAh=0.01,   # tiny cap → stops fast
    )
    emu = Emulator(smu, config)
    emu.start()
    try:
        # Wait until the cap fires
        for _ in range(100):
            st = emu.state()
            if not st.running:
                break
            time.sleep(0.05)
    finally:
        emu.stop()
    assert smu.set_output_calls[-1] is False
    assert "safety_max_used_mAh" in (emu.state().stop_reason or "")


def test_emulator_run_for_returns_final_state():
    smu = _MockSMU(dut_current_A=0.0)
    config = EmulatorConfig(
        profile=_flat_profile(),
        update_interval_s=0.05,
        safety_max_voltage_V=4.0,
    )
    emu = Emulator(smu, config)
    final = emu.run_for(0.2)
    assert isinstance(final.output_voltage_V, float)
    assert smu.set_output_calls[-1] is False


def test_emulator_parallel_scales_capacity():
    """parallel=2 means twice the bank capacity for the same per-cell profile."""
    smu = _MockSMU(dut_current_A=0.500)
    p = _flat_profile(capacity_mAh=10.0)
    cfg1 = EmulatorConfig(profile=p, parallel=1, update_interval_s=0.02,
                          safety_max_voltage_V=4.0)
    cfg2 = EmulatorConfig(profile=p, parallel=2, update_interval_s=0.02,
                          safety_max_voltage_V=4.0)
    smu1 = _MockSMU(dut_current_A=0.500)
    smu2 = _MockSMU(dut_current_A=0.500)
    e1 = Emulator(smu1, cfg1)
    e2 = Emulator(smu2, cfg2)
    e1.start(); e2.start()
    try:
        time.sleep(0.3)
        s1 = e1.state()
        s2 = e2.state()
    finally:
        e1.stop(); e2.stop()
    # parallel=2 has used roughly the same total mAh, but SoC drops half as fast
    # because bank capacity doubled
    assert s2.soc > s1.soc


def test_emulator_state_threadsafe_during_run():
    """state() called from a separate thread while loop runs must not deadlock."""
    smu = _MockSMU(dut_current_A=0.01)
    emu = Emulator(smu, EmulatorConfig(profile=_flat_profile(),
                                       update_interval_s=0.02,
                                       safety_max_voltage_V=4.0))
    seen: list = []

    def poller():
        for _ in range(20):
            seen.append(emu.state())
            time.sleep(0.01)

    emu.start()
    t = threading.Thread(target=poller)
    t.start()
    t.join(timeout=2.0)
    emu.stop()
    assert len(seen) == 20
    assert all(s is not None for s in seen)

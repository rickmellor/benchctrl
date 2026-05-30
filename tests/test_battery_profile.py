"""Hardware-free tests for benchctrl.battery.profile.

Tests against the JSON profiles bundled with the Otii desktop app at
``C:\\Users\\<user>\\AppData\\Local\\otii3\\app-*\\resources\\batteryprofiles``.
If that directory is not present, those tests skip — but the synthetic-
profile tests still cover the data model.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchctrl.battery import (
    Battery,
    BatteryProfile,
    DischargeProfile,
    DischargeSample,
    DischargeStep,
    DischargeTable,
    ExitConditions,
)
from benchctrl.exceptions import BenchValueError


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _otii_profile_dir() -> Path | None:
    """Find the Otii desktop app's bundled profiles, if installed."""
    base = Path(os.environ.get("LOCALAPPDATA", "")) / "otii3"
    if not base.exists():
        # Other OSes / installs
        candidates = [
            Path.home() / "AppData/Local/otii3",
            Path("/Applications/Otii.app/Contents/Resources/batteryprofiles"),
            Path("/opt/otii3/resources/batteryprofiles"),
        ]
        for c in candidates:
            if c.exists():
                if c.name == "batteryprofiles":
                    return c
                for sub in c.rglob("batteryprofiles"):
                    return sub
        return None
    # Windows otii3 typical layout
    for sub in base.glob("app-*/resources/batteryprofiles"):
        return sub
    return None


def _synthetic_profile() -> BatteryProfile:
    """A tiny synthetic profile with a known interpolation behaviour."""
    samples = [
        DischargeSample(voltage=4.2, resistance=0.05, capacity=0.0),
        DischargeSample(voltage=3.8, resistance=0.10, capacity=500.0),
        DischargeSample(voltage=3.3, resistance=0.20, capacity=1000.0),
    ]
    table = DischargeTable(
        table=samples,
        discharge_profile=DischargeProfile(
            low=DischargeStep(mode="current", value=0.001, time=60.0),
            high=DischargeStep(mode="current", value=0.5, time=1.0),
            exit_conditions=ExitConditions(iterations=0, ocv=3.0, voltage=3.0),
        ),
        temperature=25.0,
    )
    return BatteryProfile(
        battery=Battery(
            capacity=1000.0,
            capacity_unit="mAh",
            voltage=3.7,
            voltage_unit="V",
            manufacturer="Synthetic",
            model="Test cell",
        ),
        discharge_tables=[table],
    )


# ---------------------------------------------------------------------------
# Synthetic-data tests (always run)
# ---------------------------------------------------------------------------


def test_summary_fields_present():
    p = _synthetic_profile()
    s = p.summary()
    assert s["nominal_voltage_V"] == 3.7
    assert s["nominal_capacity_mAh"] == 1000.0
    assert s["cutoff_voltage_V"] == 3.0
    assert s["temperatures"] == [25.0]
    assert s["n_discharge_tables"] == 1
    table_info = s["discharge_tables"][0]
    assert table_info["samples"] == 3
    assert table_info["capacity_extent_mAh"] == [0.0, 1000.0]


def test_ocv_interpolation_midpoint():
    p = _synthetic_profile()
    # Midpoint between (500 mAh, 3.8 V) and (1000 mAh, 3.3 V) is 750 mAh, 3.55 V
    assert p.ocv_at(750.0) == pytest.approx(3.55)


def test_esr_interpolation_midpoint():
    p = _synthetic_profile()
    # Midpoint between (500 mAh, 0.10 Ω) and (1000 mAh, 0.20 Ω) is 0.15 Ω
    assert p.esr_at(750.0) == pytest.approx(0.15)


def test_interpolation_clamps_outside_range():
    p = _synthetic_profile()
    # Below first sample
    assert p.ocv_at(-100.0) == 4.2
    # Above last sample
    assert p.ocv_at(2000.0) == 3.3


def test_select_table_with_single_table_no_temperature():
    p = _synthetic_profile()
    t = p.select_table()
    assert t is p.discharge_tables[0]


def test_select_table_ambiguous_without_temperature():
    p = _synthetic_profile()
    # Add a second table
    p.discharge_tables.append(
        DischargeTable(
            table=p.discharge_tables[0].table[:],
            discharge_profile=p.discharge_tables[0].discharge_profile,
            temperature=-10.0,
        )
    )
    with pytest.raises(BenchValueError):
        p.select_table()


def test_select_table_picks_nearest_temperature():
    p = _synthetic_profile()
    p.discharge_tables.append(
        DischargeTable(
            table=p.discharge_tables[0].table[:],
            discharge_profile=p.discharge_tables[0].discharge_profile,
            temperature=-10.0,
        )
    )
    assert p.select_table(temperature=0).temperature == -10.0
    assert p.select_table(temperature=22).temperature == 25.0


def test_json_round_trip_synthetic():
    p = _synthetic_profile()
    text = p.to_json()
    loaded = BatteryProfile.from_json(text)
    assert loaded.to_dict() == p.to_dict()


def test_save_load_round_trip(tmp_path):
    p = _synthetic_profile()
    out = tmp_path / "synthetic.json"
    p.save(out)
    loaded = BatteryProfile.load(out)
    assert loaded.battery.manufacturer == "Synthetic"
    assert loaded.ocv_at(750.0) == pytest.approx(3.55)


def test_empty_profile_summary():
    p = BatteryProfile()
    s = p.summary()
    assert s["n_discharge_tables"] == 0
    assert s["cutoff_voltage_V"] is None
    assert s["temperatures"] == []


def test_empty_table_interpolation_raises():
    p = BatteryProfile(
        discharge_tables=[
            DischargeTable(
                table=[],
                discharge_profile=DischargeProfile(
                    low=DischargeStep("current", 0.001, 60),
                    high=DischargeStep("current", 0.1, 1),
                    exit_conditions=ExitConditions(),
                ),
            )
        ]
    )
    with pytest.raises(BenchValueError):
        p.ocv_at(0.0)


def test_capacity_unit_normalization():
    """Profile in Ah is reported in mAh by nominal_capacity_mAh."""
    p = BatteryProfile(battery=Battery(capacity=2.5, capacity_unit="Ah"))
    assert p.nominal_capacity_mAh == 2500.0


def test_select_table_no_tables_raises():
    p = BatteryProfile()
    with pytest.raises(BenchValueError):
        p.select_table()


# ---------------------------------------------------------------------------
# Real-profile tests (only run if the Otii install is present)
# ---------------------------------------------------------------------------


otii_dir = _otii_profile_dir()
needs_otii = pytest.mark.skipif(
    otii_dir is None,
    reason="Otii desktop app's bundled profiles not found on this system",
)


@needs_otii
def test_loads_every_bundled_profile():
    assert otii_dir is not None
    files = sorted(otii_dir.glob("*.json"))
    assert len(files) >= 1, f"no bundled profiles found in {otii_dir}"
    for f in files:
        profile = BatteryProfile.load(f)
        assert profile.battery.manufacturer != ""
        assert profile.battery.model != ""
        assert profile.nominal_capacity_mAh > 0
        assert len(profile.discharge_tables) >= 1
        assert all(len(t.table) > 0 for t in profile.discharge_tables)


@needs_otii
def test_semantic_round_trip_against_every_bundled_profile(tmp_path):
    """Loading + re-saving every Otii-bundled profile produces semantically
    identical JSON (key-for-key)."""
    assert otii_dir is not None
    for f in sorted(otii_dir.glob("*.json")):
        original = json.loads(f.read_text(encoding="utf-8"))
        profile = BatteryProfile.load(f)
        out = tmp_path / f.name
        profile.save(out)
        round_tripped = json.loads(out.read_text(encoding="utf-8"))
        assert original == round_tripped, f"mismatch round-tripping {f.name}"


@needs_otii
def test_cr2032_known_ocv_at_first_sample():
    """The first sample of CR2032-Energizer-(25) has known V=3.224 V and
    ESR=8.904 Ω at capacity~0.004 mAh."""
    assert otii_dir is not None
    p = BatteryProfile.load(otii_dir / "CR2032-Energizer-(25).json")
    # First sample's capacity is approximately zero
    v = p.ocv_at(0.0)
    r = p.esr_at(0.0)
    assert v == pytest.approx(3.224, abs=0.001)
    assert r == pytest.approx(8.904, abs=0.001)


@needs_otii
def test_lipo_profiles_use_power_mode():
    """The Renata LiPo profiles use mode='power' in the discharge profile."""
    assert otii_dir is not None
    for f in otii_dir.glob("LiPo_*.json"):
        p = BatteryProfile.load(f)
        t = p.discharge_tables[0]
        assert t.discharge_profile.high.mode == "power"
        assert t.discharge_profile.low.mode == "power"


@needs_otii
def test_temperatures_present_across_lipo_profiles():
    """The three LiPo files cover -10, 5, 20 °C — each as a separate file."""
    assert otii_dir is not None
    temps = []
    for f in sorted(otii_dir.glob("LiPo_*.json")):
        p = BatteryProfile.load(f)
        temps.extend(p.temperatures)
    assert set(temps) == {-10.0, 5.0, 20.0}

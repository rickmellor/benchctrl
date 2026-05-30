"""Battery profile data model — Otii-compatible JSON format.

A battery profile describes a real battery's discharge characteristics:
- Static metadata (manufacturer, model, capacity, nominal voltage, size)
- One or more **discharge tables** at different temperatures, each carrying
  a measured curve of voltage and internal resistance vs. capacity consumed.

The on-disk JSON schema matches the format Qoitech's Otii application
ships with (`C:\\Users\\<user>\\AppData\\Local\\otii3\\app-*\\resources\\batteryprofiles`).
Profiles produced by benchctrl round-trip bit-identically through Otii, so
existing measurement data is interchangeable in both directions.

This module is pure data + I/O — no SMU connection or hardware required.

Example:

    >>> from benchctrl.battery import BatteryProfile
    >>> p = BatteryProfile.load("CR2032-Energizer-(25).json")
    >>> p.nominal_voltage, p.nominal_capacity_mAh
    (3.0, 230.24)
    >>> p.ocv_at(used_capacity_mAh=50.0, temperature=25)   # OCV at 50 mAh consumed
    3.10...
    >>> p.esr_at(used_capacity_mAh=50.0, temperature=25)   # ESR at same point (Ω)
    9.15...
"""

from __future__ import annotations

import bisect
import json
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, ClassVar, Literal, Optional, Union

from benchctrl.exceptions import SMUValueError

# Discharge step "mode" — Otii supports current-mode (A) and power-mode (W) loads.
DischargeMode = Literal["current", "power"]


def _new_uuid() -> str:
    return str(uuid.uuid4())


# ---------------------------------------------------------------------------
# Leaf dataclasses
# ---------------------------------------------------------------------------


@dataclass
class DischargeStep:
    """One half of a discharge cycle — either the high-load or low-load step.

    Attributes:
        mode: ``"current"`` (value in amps) or ``"power"`` (value in watts).
        value: load magnitude in the chosen units.
        time: duration of the step in seconds.
    """

    mode: DischargeMode
    value: float
    time: float

    def to_dict(self) -> dict:
        return {"mode": self.mode, "value": self.value, "time": self.time}

    @classmethod
    def from_dict(cls, d: dict) -> DischargeStep:
        return cls(mode=d["mode"], value=float(d["value"]), time=float(d["time"]))


@dataclass
class ExitConditions:
    """When to stop the profiler discharge cycle.

    Attributes:
        iterations: maximum number of high/low cycles (0 = unlimited).
        ocv: stop when the post-relaxation OCV falls to or below this (V).
        voltage: stop when the on-load voltage falls to or below this (V).
    """

    iterations: int = 0
    ocv: float = 0.0
    voltage: float = 0.0

    def to_dict(self) -> dict:
        return {"iterations": int(self.iterations), "ocv": self.ocv, "voltage": self.voltage}

    @classmethod
    def from_dict(cls, d: dict) -> ExitConditions:
        return cls(
            iterations=int(d.get("iterations", 0)),
            ocv=float(d.get("ocv", 0.0)),
            voltage=float(d.get("voltage", 0.0)),
        )


@dataclass
class DischargeProfile:
    """Pulse-load configuration used to characterise the battery.

    The profiler alternates between ``high`` and ``low`` load steps until one
    of the ``exitConditions`` is met. The ``high`` step does most of the
    discharge; the ``low`` step lets the cell partially recover so the
    profiler can measure both on-load voltage AND open-circuit-ish voltage.
    """

    low: DischargeStep
    high: DischargeStep
    exit_conditions: ExitConditions

    def to_dict(self) -> dict:
        return {
            "low": self.low.to_dict(),
            "high": self.high.to_dict(),
            "exitConditions": self.exit_conditions.to_dict(),
        }

    @classmethod
    def from_dict(cls, d: dict) -> DischargeProfile:
        return cls(
            low=DischargeStep.from_dict(d["low"]),
            high=DischargeStep.from_dict(d["high"]),
            exit_conditions=ExitConditions.from_dict(d["exitConditions"]),
        )


@dataclass
class DeviceInfo:
    """Which Otii device was used to capture the profile.

    Preserved verbatim for round-trip compatibility with the Otii app.
    """

    type: str = "Arc"
    id: str = ""
    hardware_id: str = ""
    firmware_version: str = ""

    def to_dict(self) -> dict:
        return {
            "type": self.type,
            "id": self.id,
            "hardwareId": self.hardware_id,
            "firmwareVersion": self.firmware_version,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DeviceInfo:
        return cls(
            type=d.get("type", "Arc"),
            id=d.get("id", ""),
            hardware_id=d.get("hardwareId", ""),
            firmware_version=d.get("firmwareVersion", ""),
        )


@dataclass(frozen=True)
class DischargeSample:
    """One row of a discharge curve: voltage and ESR at a given used-capacity.

    Attributes:
        voltage: open-circuit-ish cell voltage at this point (V).
        resistance: equivalent series resistance at this point (Ohm).
        capacity: total capacity consumed up to this point (mAh).
    """

    voltage: float
    resistance: float
    capacity: float

    def to_dict(self) -> dict:
        return {
            "voltage": self.voltage,
            "resistance": self.resistance,
            "capacity": self.capacity,
        }

    @classmethod
    def from_dict(cls, d: dict) -> DischargeSample:
        return cls(
            voltage=float(d["voltage"]),
            resistance=float(d["resistance"]),
            capacity=float(d["capacity"]),
        )


# ---------------------------------------------------------------------------
# Battery metadata
# ---------------------------------------------------------------------------


@dataclass
class Battery:
    """Static physical metadata about the battery cell.

    All fields preserve Otii's units verbatim so profiles round-trip.
    """

    capacity: float = 0.0  # in capacityunit (typically mAh)
    capacity_unit: str = "mAh"
    voltage: float = 0.0  # nominal (in voltage_unit, typically V)
    voltage_unit: str = "V"
    manufacturer: str = ""
    model: str = ""
    size: str = ""
    size_unit: str = "mm"

    def to_dict(self) -> dict:
        return {
            "capacity": self.capacity,
            "capacityunit": self.capacity_unit,
            "voltage": self.voltage,
            "voltageunit": self.voltage_unit,
            "manufacturer": self.manufacturer,
            "model": self.model,
            "size": self.size,
            "sizeunit": self.size_unit,
        }

    @classmethod
    def from_dict(cls, d: dict) -> Battery:
        return cls(
            capacity=float(d.get("capacity", 0.0)),
            capacity_unit=d.get("capacityunit", "mAh"),
            voltage=float(d.get("voltage", 0.0)),
            voltage_unit=d.get("voltageunit", "V"),
            manufacturer=d.get("manufacturer", ""),
            model=d.get("model", ""),
            size=d.get("size", ""),
            size_unit=d.get("sizeunit", "mm"),
        )


# ---------------------------------------------------------------------------
# Discharge table — the actual measured curve
# ---------------------------------------------------------------------------


@dataclass
class DischargeTable:
    """One temperature point's discharge data.

    A profile can hold multiple tables, one per temperature, but Otii's
    shipped profiles each carry exactly one table; multi-temperature
    measurements (e.g. the LiPo at -10/5/20 °C) ship as three separate
    profile files. benchctrl preserves both possibilities.

    Attributes:
        table: list of (voltage, resistance, capacity) samples ordered by
            increasing ``capacity`` (used-capacity in mAh).
        temperature: ambient temperature during measurement (in
            ``temperature_unit``, typically °C).
        discharge_profile: the pulse load used during profiling.
        device: which Arc/Ace device captured the data (preserved for
            round-trip).
        software_version: Otii (or benchctrl) version that produced the
            table.
        id: stable UUID for this discharge table.
    """

    table: list[DischargeSample]
    discharge_profile: DischargeProfile
    temperature: float = 25.0
    temperature_unit: str = "°C"
    device: DeviceInfo = field(default_factory=DeviceInfo)
    software_version: str = ""
    id: str = field(default_factory=_new_uuid)

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "dischargeprofile": self.discharge_profile.to_dict(),
            "temperature": self.temperature,
            "temperatureunit": self.temperature_unit,
            "device": self.device.to_dict(),
            "softwareVersion": self.software_version,
            "table": [s.to_dict() for s in self.table],
        }

    @classmethod
    def from_dict(cls, d: dict) -> DischargeTable:
        return cls(
            id=d.get("id", _new_uuid()),
            discharge_profile=DischargeProfile.from_dict(d["dischargeprofile"]),
            temperature=float(d.get("temperature", 25.0)),
            temperature_unit=d.get("temperatureunit", "°C"),
            device=DeviceInfo.from_dict(d.get("device", {})),
            software_version=d.get("softwareVersion", ""),
            table=[DischargeSample.from_dict(s) for s in d.get("table", [])],
        )

    # --- interpolation helpers ---------------------------------------

    def ocv_at(self, used_capacity_mAh: float) -> float:
        """Interpolate the open-circuit voltage at ``used_capacity_mAh``.

        Below the first sample, returns the first sample's voltage.
        Above the last sample, returns the last sample's voltage.
        """
        return self._interpolate(used_capacity_mAh, key="voltage")

    def esr_at(self, used_capacity_mAh: float) -> float:
        """Interpolate the ESR at ``used_capacity_mAh`` (Ω)."""
        return self._interpolate(used_capacity_mAh, key="resistance")

    @property
    def capacity_extent(self) -> tuple[float, float]:
        """``(first_capacity, last_capacity)`` in mAh."""
        if not self.table:
            return (0.0, 0.0)
        return (self.table[0].capacity, self.table[-1].capacity)

    def _interpolate(self, capacity: float, *, key: str) -> float:
        if not self.table:
            raise SMUValueError("discharge table is empty")
        if len(self.table) == 1:
            return getattr(self.table[0], key)

        caps = [s.capacity for s in self.table]
        # Clamp to the table's range
        if capacity <= caps[0]:
            return getattr(self.table[0], key)
        if capacity >= caps[-1]:
            return getattr(self.table[-1], key)

        # Linear interpolation between bracketing samples
        i = bisect.bisect_left(caps, capacity)
        lo = self.table[i - 1]
        hi = self.table[i]
        span = hi.capacity - lo.capacity
        if span <= 0:
            return getattr(lo, key)
        t = (capacity - lo.capacity) / span
        return getattr(lo, key) + t * (getattr(hi, key) - getattr(lo, key))


# ---------------------------------------------------------------------------
# Top-level profile
# ---------------------------------------------------------------------------


@dataclass
class BatteryProfile:
    """A complete battery profile: metadata + one or more discharge tables.

    Load with :py:meth:`load`, save with :py:meth:`save`. Profiles
    produced by benchctrl round-trip bit-identically through Otii.
    """

    battery: Battery = field(default_factory=Battery)
    discharge_tables: list[DischargeTable] = field(default_factory=list)
    id: str = field(default_factory=_new_uuid)

    # Optional schema-version marker we emit on save. Otii's bundled
    # profiles don't carry one, so by default we preserve their shape.
    BENCHCTRL_SCHEMA_VERSION: ClassVar[int] = 1

    # --- I/O ---------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "battery": self.battery.to_dict(),
            "dischargetables": [t.to_dict() for t in self.discharge_tables],
        }

    @classmethod
    def from_dict(cls, d: dict) -> BatteryProfile:
        return cls(
            id=d.get("id", _new_uuid()),
            battery=Battery.from_dict(d.get("battery", {})),
            discharge_tables=[
                DischargeTable.from_dict(t) for t in d.get("dischargetables", [])
            ],
        )

    def to_json(self, indent: Optional[int] = 2) -> str:
        return json.dumps(self.to_dict(), indent=indent, ensure_ascii=False)

    @classmethod
    def from_json(cls, text: Union[str, bytes]) -> BatteryProfile:
        return cls.from_dict(json.loads(text))

    @classmethod
    def load(cls, path: Union[str, Path]) -> BatteryProfile:
        """Load a JSON profile from disk."""
        p = Path(path)
        return cls.from_json(p.read_text(encoding="utf-8"))

    def save(self, path: Union[str, Path], *, indent: Optional[int] = 2) -> Path:
        """Save the profile to disk as JSON. Returns the written path."""
        p = Path(path)
        p.write_text(self.to_json(indent=indent), encoding="utf-8")
        return p

    # --- summary properties -----------------------------------------

    @property
    def nominal_voltage(self) -> float:
        """Nominal cell voltage from the battery metadata (V)."""
        return self.battery.voltage

    @property
    def nominal_capacity_mAh(self) -> float:
        """Nominal capacity from the battery metadata, normalised to mAh."""
        c = self.battery.capacity
        unit = (self.battery.capacity_unit or "mAh").lower()
        if unit == "mah":
            return c
        if unit == "ah":
            return c * 1000.0
        # Unrecognised — return raw value with a soft fallback
        return c

    @property
    def temperatures(self) -> list[float]:
        """All distinct temperatures (in their stored units) across the
        discharge tables."""
        return sorted({t.temperature for t in self.discharge_tables})

    @property
    def cutoff_voltage(self) -> Optional[float]:
        """Lowest ``exitConditions.voltage`` across all discharge tables, or
        ``None`` if no tables are present."""
        if not self.discharge_tables:
            return None
        return min(
            t.discharge_profile.exit_conditions.voltage for t in self.discharge_tables
        )

    # --- table selection + interpolation ----------------------------

    def select_table(
        self, temperature: Optional[float] = None
    ) -> DischargeTable:
        """Return the discharge table closest to ``temperature``.

        If ``temperature`` is None and only one table exists, returns it.
        Raises if there are zero tables, or the request is ambiguous.
        """
        if not self.discharge_tables:
            raise SMUValueError("profile has no discharge tables")
        if temperature is None:
            if len(self.discharge_tables) == 1:
                return self.discharge_tables[0]
            raise SMUValueError(
                f"profile has {len(self.discharge_tables)} tables; pass temperature="
            )
        return min(
            self.discharge_tables, key=lambda t: abs(t.temperature - temperature)
        )

    def ocv_at(
        self, used_capacity_mAh: float, temperature: Optional[float] = None
    ) -> float:
        """Interpolated OCV (V) at the given used-capacity and temperature."""
        return self.select_table(temperature).ocv_at(used_capacity_mAh)

    def esr_at(
        self, used_capacity_mAh: float, temperature: Optional[float] = None
    ) -> float:
        """Interpolated ESR (Ω) at the given used-capacity and temperature."""
        return self.select_table(temperature).esr_at(used_capacity_mAh)

    # --- summary for humans / MCP ----------------------------------

    def summary(self) -> dict[str, Any]:
        """JSON-friendly summary suitable for MCP tool responses."""
        tables_info = []
        for t in self.discharge_tables:
            lo, hi = t.capacity_extent
            tables_info.append(
                {
                    "id": t.id,
                    "temperature": t.temperature,
                    "temperature_unit": t.temperature_unit,
                    "samples": len(t.table),
                    "capacity_extent_mAh": [lo, hi],
                    "discharge_high": t.discharge_profile.high.to_dict(),
                    "discharge_low": t.discharge_profile.low.to_dict(),
                    "exit_conditions": t.discharge_profile.exit_conditions.to_dict(),
                    "device_type": t.device.type,
                    "software_version": t.software_version,
                }
            )
        return {
            "id": self.id,
            "battery": self.battery.to_dict(),
            "nominal_voltage_V": self.nominal_voltage,
            "nominal_capacity_mAh": self.nominal_capacity_mAh,
            "cutoff_voltage_V": self.cutoff_voltage,
            "temperatures": self.temperatures,
            "n_discharge_tables": len(self.discharge_tables),
            "discharge_tables": tables_info,
        }

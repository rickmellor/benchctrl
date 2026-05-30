"""Bench-validation harness for the OpenSMU battery emulator.

Runs reproducible "scenarios" that drive the Otii Arc Pro emulator
against a programmable load and saves the captured response to disk
as a JSON + CSV pair (optionally a PNG plot) under ``scenarios/``.

Two scenario types:

* ``static``  — step through a fixed list of load resistances, let each
  step settle, record the emulator's V/I/SoC/ESR snapshot. Use this to
  verify the emulator's ESR sag matches what the profile predicts.

* ``dynamic`` — drive the load through a time-varying pattern (e.g. an
  IoT sleep / wake / TX cycle) while polling the emulator's state at a
  fixed rate. Use this to verify transient response.

The load can be either:

* ``qr10x`` — Eastwood Tech QR10x programmable resistor (relay-based,
  30–95 ms switching, no transients below ~50 ms)
* ``dl3031a`` — Rigol DL3031A electronic load in CR mode (electronic,
  sub-ms switching, much wider current envelope)

Each saved scenario is self-contained: it embeds a copy of the battery
profile JSON, the emulator config, the bench config, and the raw
captured data.

CLI (live capture)::

    # QR10x (default)
    python validation/run_validation.py --profile CR2032-Energizer-(25) \\
        --scenario static

    # DL3031A
    python validation/run_validation.py --profile CR2032-Energizer-(25) \\
        --scenario static --load dl3031a

Use ``--all`` to run the full static matrix across every bundled profile.
"""
from __future__ import annotations

import argparse
import csv
import json
import logging
import re
import shutil
import sys
import time

log = logging.getLogger("opensmu.validation")
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from opensmu import SMU, Channel
from opensmu._version import __version__ as OPENSMU_VERSION
from opensmu.battery import BatteryProfile, Emulator, EmulatorConfig
from opensmu.bench import QR10x

REPO_ROOT = Path(__file__).resolve().parent.parent
PROFILE_DIR = Path(r"C:\Users\rickm\AppData\Local\otii3\app-3.7.2\resources\batteryprofiles")
SCENARIO_DIR = REPO_ROOT / "validation" / "scenarios"

# Default static-sweep load resistances (Ω). High → low forces a clear
# sag curve; the final value back at the start is a recovery point.
DEFAULT_STATIC_STEPS = [100_000, 10_000, 1_000, 100, 50, 25, 12, 100_000]

# Default dynamic pattern — one full LoRaWAN-style end-device cycle.
# Phases are picked so the relay switching time (~30-95 ms) is small
# compared to the dwell of each phase. Use with --pattern standard.
DEFAULT_IOT_PATTERN = [
    ("sleep", 320_000, 5.0),  # ~10 µA at 3.2 V
    ("wake",    3_200, 0.6),  # ~1 mA sensor read
    ("sleep", 320_000, 1.5),
    ("tx",        100, 0.4),  # ~32 mA burst
    ("sleep", 320_000, 5.0),
]

# High-resolution pattern — captured via Arc Pro native streaming
# (~4 kHz on MAIN_CURRENT). TX phases are 300 ms — at 100 ms we
# observed the DL3031A's input-toggle latency (~700 ms relay/regulator
# settling) putting the actual current spike outside the labeled TX
# window. Faster-than-300 ms work needs CC-mode-input-always-on (TBD)
# or a different load. Cycle = 3.6 s; with --cycles 3 → ~11 s capture.
DEFAULT_HIRES_IOT_PATTERN = [
    ("sleep", 320_000, 1.0),
    ("tx",        100, 0.3),
    ("sleep", 320_000, 0.7),
    ("tx",        100, 0.3),
    ("sleep", 320_000, 1.3),
]

PATTERNS: dict[str, list[tuple[str, float, float]]] = {
    "standard": DEFAULT_IOT_PATTERN,
    "hires":    DEFAULT_HIRES_IOT_PATTERN,
}

PROFILE_OVERRIDES: dict[str, dict[str, Any]] = {
    # Keyed by the profile filename stem. Lets us tune safety caps per
    # chemistry: LiPo needs the Arc's high range; AA needs a lower
    # safety voltage so we don't accidentally damage anything.
    "AA-Varta-(25)":               {"safety_max_V": 2.0, "qr_safety_R": 6.0},
    "AAA-Duracell-(25)":           {"safety_max_V": 2.0, "qr_safety_R": 6.0},
    "CR2032-Energizer-(25)":       {"safety_max_V": 3.5, "qr_safety_R": 12.0},
    "CR2-Panasonic-(25)":          {"safety_max_V": 3.5, "qr_safety_R": 12.0},
    "CR123A-GP-(25)":              {"safety_max_V": 3.5, "qr_safety_R": 12.0},
    # NOTE: Arc Pro high-range tops out at ~4.2 V under load. LiPo profiles
    # have fresh OCV > 4.3 V, so we cap to 4.2 V — the emulator will clamp
    # high-SoC samples and reproduce normal sag behavior at lower SoC.
    "LiPo_ICP632136HPST-Renata-(20)":  {"safety_max_V": 4.2, "qr_safety_R": 20.0},
    "LiPo_ICP632136HPST-Renata-(5)":   {"safety_max_V": 4.2, "qr_safety_R": 20.0},
    "LiPo_ICP632136HPST-Renata-(-10)": {"safety_max_V": 4.2, "qr_safety_R": 20.0},
}


# ---------------------------------------------------------------------------
# Load adapters — thin wrappers that hide the per-instrument quirks so the
# scenario logic stays load-agnostic. Each adapter owns the lifecycle of one
# instrument: open → setup → set_resistance loop → teardown → close.
# ---------------------------------------------------------------------------


class _LoadAdapter:
    """Common interface; subclasses implement the wire ops."""

    kind: str
    bench_dict: dict[str, Any]

    def open(self) -> None:
        raise NotImplementedError

    def setup(self, *, safety_R: float, voltage_V: float) -> None:
        """Apply safety params and put the load into its baseline state."""
        raise NotImplementedError

    def set_resistance(self, ohms: float) -> float:
        """Command R, return the actual achieved R (or NaN if unmeasurable)."""
        raise NotImplementedError

    def teardown(self, idle_R: float) -> None:
        """Return to a safe idle state before close()."""
        raise NotImplementedError

    def close(self) -> None:
        raise NotImplementedError

    def measure(self) -> Optional[dict[str, float]]:
        """Optional: read the load's own V/I measurement.

        Returns ``{"voltage_V": float, "current_A": float}`` for loads
        that expose direct measurement (DL3031A), or ``None`` for
        loads that don't (QR10x is passive — it doesn't measure).
        """
        return None


class _QR10xAdapter(_LoadAdapter):
    kind = "qr10x"

    def __init__(self, port: str):
        self.port = port
        self.qr: Optional[QR10x] = None
        self.bench_dict = {}
        self.safety_R = 12.0

    def open(self) -> None:
        self.qr = QR10x.open(self.port)

    def setup(self, *, safety_R: float, voltage_V: float) -> None:
        assert self.qr is not None
        self.safety_R = safety_R
        info = self.qr.info()
        self.bench_dict = {
            "load_kind": "Eastwood QR10x programmable resistor",
            "device_type": info.device_type,
            "serial": info.serial,
            "hardware_version": info.hardware_version,
            "firmware_version": info.firmware_version,
            "port": self.port,
            "safety_limit_ohm_applied": safety_R,
        }
        self.qr.set_safety_limit(safety_R)

    def set_resistance(self, ohms: float) -> float:
        assert self.qr is not None
        self.qr.set_resistance(ohms)
        return self.qr.actual_resistance()

    def teardown(self, idle_R: float) -> None:
        if self.qr is None:
            return
        try:
            self.qr.set_resistance(idle_R)
        except Exception:
            pass

    def close(self) -> None:
        if self.qr is not None:
            self.qr.close()
            self.qr = None


class _DL3031AAdapter(_LoadAdapter):
    """Drives the DL3031A in CR mode with input ON.

    The DL3031A's CR range is **~2 Ω to 16 kΩ** (queried at runtime).
    Setpoints outside that window get translated to ``input = OFF``,
    which presents high impedance at the terminals — a more honest
    representation of an IoT device's deep-sleep state than e.g. a
    100 kΩ programmable resistor anyway.
    """

    kind = "dl3031a"

    def __init__(self, resource: Optional[str] = None):
        self.resource = resource
        self.dl = None
        self.bench_dict = {}
        self.r_min = 2.0
        self.r_max = 16_000.0
        self._input_on = False

    def open(self) -> None:
        from opensmu.bench import RigolDL3031A
        self.dl = RigolDL3031A.open(self.resource)

    def setup(self, *, safety_R: float, voltage_V: float) -> None:
        assert self.dl is not None
        self.dl.reset()
        self.dl.clear_status()
        time.sleep(0.2)  # *RST takes a beat to settle
        self.dl.set_voltage_range(36.0)
        self.dl.set_current_range(6.0)
        self.dl.set_mode("CR")
        # Discover the actual CR bounds the firmware reports — these
        # vary slightly between DL3000 models.
        try:
            self.r_min = float(self.dl.query(":SOURce:RESistance:LEVel:IMMediate? MIN"))
            self.r_max = float(self.dl.query(":SOURce:RESistance:LEVel:IMMediate? MAX"))
        except Exception:
            log.warning("could not query DL3031A R bounds; using defaults", exc_info=True)
        info = self.dl.info()
        self.bench_dict = {
            "load_kind": "Rigol DL3031A electronic load (CR mode)",
            "model": info.model,
            "serial": info.serial,
            "firmware": info.firmware,
            "resource": info.resource,
            "current_range_A": self.dl.get_current_range(),
            "voltage_range_V": self.dl.get_voltage_range(),
            "cr_min_ohm": self.r_min,
            "cr_max_ohm": self.r_max,
            "note": (
                "no fixed safety_limit_ohm; protected via current/voltage "
                "ranges. R > cr_max_ohm is realized as input=OFF (open)."
            ),
        }
        self._input_on = False

    def set_resistance(self, ohms: float) -> float:
        assert self.dl is not None
        if ohms > self.r_max:
            # Out of CR range — present open circuit instead.
            if self._input_on:
                self.dl.set_input(False)
                self._input_on = False
            return float("inf")
        # Clamp to the device's regulation floor; the device would
        # error out (-220) on a raw < r_min anyway.
        cmd_r = max(ohms, self.r_min)
        self.dl.set_resistance(cmd_r)
        if not self._input_on:
            self.dl.set_input(True)
            self._input_on = True
        return cmd_r

    def teardown(self, idle_R: float) -> None:
        if self.dl is None:
            return
        try:
            self.dl.set_input(False)
            self._input_on = False
        except Exception:
            pass

    def close(self) -> None:
        if self.dl is not None:
            self.dl.close()
            self.dl = None

    def measure(self) -> Optional[dict[str, float]]:
        if self.dl is None:
            return None
        # :FETCh: rather than :MEASure: — the latter triggers a fresh
        # 200 ms integration per call and would dominate the sample
        # loop. :FETCh: reads the device's continuously-updated
        # measurement register: ~10-20 ms USB-TMC round-trip per call.
        try:
            v = self.dl.fetch_voltage()
        except Exception:
            v = float("nan")
        try:
            i = self.dl.fetch_current()
        except Exception:
            i = float("nan")
        return {"voltage_V": v, "current_A": i}


def _make_load(load_kind: str, qr_port: str, dl_resource: Optional[str]) -> _LoadAdapter:
    if load_kind == "qr10x":
        return _QR10xAdapter(qr_port)
    if load_kind == "dl3031a":
        return _DL3031AAdapter(dl_resource)
    raise ValueError(f"unknown load {load_kind!r}; expected qr10x or dl3031a")


def _slugify(name: str) -> str:
    return re.sub(r"[^A-Za-z0-9_.-]+", "_", name).strip("_")


def _timestamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _resolve_profile(name: str) -> Path:
    """Accept ``CR2032-Energizer-(25)`` or a full path."""
    p = Path(name)
    if p.exists():
        return p
    candidate = PROFILE_DIR / f"{name}.json"
    if candidate.exists():
        return candidate
    raise FileNotFoundError(f"profile not found: {name} (looked in {PROFILE_DIR})")


def _make_emulator_config(profile: BatteryProfile, overrides: dict[str, Any]) -> EmulatorConfig:
    safety = overrides.get("safety_max_V", 3.5)
    return EmulatorConfig(
        profile=profile,
        initial_soc=1.0,
        series=1,
        parallel=1,
        soc_tracking=True,
        safety_max_voltage_V=safety,
        current_limit_A=overrides.get("current_limit_A", 0.5),
        update_interval_s=0.01,
    )


def _step_record(emu: Emulator, *, r_setpoint: float, r_actual: float,
                 phase: Optional[str] = None) -> dict[str, Any]:
    st = emu.state()
    v_pred = st.ocv_V - st.measured_current_A * st.esr_ohm
    i_abs = abs(st.measured_current_A)
    rec = {
        "r_setpoint_ohm": r_setpoint,
        "r_actual_ohm": r_actual,
        "v_out_V": st.output_voltage_V,
        "i_measured_A": st.measured_current_A,
        "ocv_V": st.ocv_V,
        "esr_ohm": st.esr_ohm,
        "v_predicted_V": v_pred,
        "voltage_error_mV": (st.output_voltage_V - v_pred) * 1000.0,
        "r_inferred_ohm": (st.output_voltage_V / i_abs) if i_abs > 1e-9 else None,
        "soc_pct": st.soc * 100.0,
        "used_capacity_mAh": st.used_capacity_mAh,
        "iteration": st.iteration,
    }
    if phase is not None:
        rec["phase"] = phase
    return rec


def _save_csv(path: Path, rows: list[dict[str, Any]]) -> None:
    if not rows:
        return
    cols = list(rows[0].keys())
    with path.open("w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if v is None else v) for k, v in r.items()})


def _save_static_plot(path: Path, rows: list[dict[str, Any]], title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    rs = [r["r_actual_ohm"] for r in rows]
    vs = [r["v_out_V"] for r in rows]
    is_mA = [r["i_measured_A"] * 1000.0 for r in rows]
    fig, (ax1, ax2) = plt.subplots(2, 1, figsize=(10, 6), sharex=True)
    ax1.semilogx(rs, vs, "o-", color="#1f77b4")
    ax1.set_ylabel("V_out (V)")
    ax1.grid(True, which="both", alpha=0.3)
    ax2.semilogx(rs, is_mA, "s-", color="#d62728")
    ax2.set_ylabel("I (mA)")
    ax2.set_xlabel("Load R (Ω)")
    ax2.grid(True, which="both", alpha=0.3)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


def _save_dynamic_plot(path: Path, samples: list[dict[str, Any]], title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    ts = [s["t_s"] for s in samples]
    vs = [s["v_out_V"] for s in samples]
    is_mA = [s["i_measured_A"] * 1000.0 for s in samples]
    socs = [s["soc_pct"] for s in samples]
    has_load_meas = "dl_voltage_V" in samples[0]
    fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(ts, vs, color="#1f77b4", label="emulator V_set", linewidth=1.2)
    if has_load_meas:
        vs_dl = [s["dl_voltage_V"] for s in samples]
        axes[0].plot(ts, vs_dl, color="#9467bd",
                     label="DL3031A V_measured", linewidth=0.8, alpha=0.8)
        axes[0].legend(loc="lower right", fontsize=8)
    axes[0].set_ylabel("V (V)")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(ts, is_mA, color="#d62728",
                 label="emulator I (lagged)", linewidth=1.2)
    if has_load_meas:
        is_dl_mA = [s["dl_current_A"] * 1000.0 for s in samples]
        axes[1].plot(ts, is_dl_mA, color="#ff7f0e",
                     label="DL3031A I (direct)", linewidth=0.8, alpha=0.8)
        axes[1].legend(loc="upper right", fontsize=8)
    axes[1].set_ylabel("I (mA)")
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(ts, socs, color="#2ca02c")
    axes[2].set_ylabel("SoC (%)")
    axes[2].set_xlabel("t (s)")
    axes[2].grid(True, alpha=0.3)
    # Shade phase regions
    phases = []
    cur_phase = samples[0].get("phase")
    cur_start = samples[0]["t_s"]
    for s in samples[1:]:
        if s.get("phase") != cur_phase:
            phases.append((cur_phase, cur_start, s["t_s"]))
            cur_phase = s.get("phase")
            cur_start = s["t_s"]
    phases.append((cur_phase, cur_start, samples[-1]["t_s"]))
    colors = {"sleep": "#eef7ee", "wake": "#fff4e6", "tx": "#ffeaea"}
    for ax in axes:
        for label, t0, t1 in phases:
            ax.axvspan(t0, t1, color=colors.get(label, "#ffffff"), alpha=0.5)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=120)
    plt.close(fig)
    return True


def run_static_sweep(*, profile_path: Path, load: _LoadAdapter,
                     smu_port: Optional[str] = None,
                     r_steps: Optional[list[float]] = None, settle_s: float = 1.2,
                     output_dir: Path = SCENARIO_DIR) -> dict[str, Any]:
    profile = BatteryProfile.load(profile_path)
    stem = profile_path.stem
    overrides = PROFILE_OVERRIDES.get(stem, {})
    r_steps = list(r_steps if r_steps is not None else DEFAULT_STATIC_STEPS)
    safety_R = overrides.get("qr_safety_R", 12.0)
    safety_V = overrides.get("safety_max_V", 3.5)

    scenario_name = f"{_slugify(stem)}_static_{load.kind}_{_timestamp()}"
    out_base = output_dir / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[static/{load.kind}] {stem}: {len(r_steps)} steps, "
          f"settle={settle_s}s, V_safe={safety_V}, load R_min={safety_R}Ω")

    cfg = _make_emulator_config(profile, overrides)
    steps_out: list[dict[str, Any]] = []

    smu_kwargs = {"port": smu_port} if smu_port else {}
    with SMU.open(**smu_kwargs) as smu:
        load.open()
        try:
            load.setup(safety_R=safety_R, voltage_V=safety_V)
            load.set_resistance(r_steps[0])
            time.sleep(0.3)
            emu = Emulator(smu, cfg)
            emu.start()
            try:
                time.sleep(2.0)  # let emulator settle on its first OCV setpoint
                for r in r_steps:
                    r_actual = load.set_resistance(r)
                    time.sleep(settle_s)
                    rec = _step_record(emu, r_setpoint=r, r_actual=r_actual)
                    steps_out.append(rec)
                    print(f"  R={r:>8.1f}Ω  Vout={rec['v_out_V']:.4f}V  "
                          f"I={rec['i_measured_A']*1000:+.3f}mA  "
                          f"ΔV={rec['voltage_error_mV']:+.2f}mV  "
                          f"SoC={rec['soc_pct']:.3f}%")
            finally:
                load.teardown(r_steps[0])
                emu.stop()
        finally:
            load.close()

    scenario = {
        "scenario": "static_load_sweep",
        "schema_version": 2,
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "opensmu_version": OPENSMU_VERSION,
        "profile": {
            "path": str(profile_path),
            "stem": stem,
            "battery": {
                "manufacturer": profile.battery.manufacturer,
                "model": profile.battery.model,
                "nominal_voltage_V": profile.nominal_voltage,
                "nominal_capacity_mAh": profile.nominal_capacity_mAh,
                "cutoff_voltage_V": profile.cutoff_voltage,
            },
        },
        "emulator_config": {
            "initial_soc": cfg.initial_soc,
            "series": cfg.series,
            "parallel": cfg.parallel,
            "soc_tracking": cfg.soc_tracking,
            "safety_max_voltage_V": cfg.safety_max_voltage_V,
            "current_limit_A": cfg.current_limit_A,
            "update_interval_s": cfg.update_interval_s,
            "voltage_range_resolved": ("high" if profile.ocv_at(0.0) > 3.4 else "low"),
        },
        "bench": {
            **load.bench_dict,
            "settle_time_per_step_s": settle_s,
        },
        "r_steps_ohm": r_steps,
        "steps": steps_out,
    }

    out_base.with_suffix(".json").write_text(
        json.dumps(scenario, indent=2), encoding="utf-8")
    _save_csv(out_base.with_suffix(".csv"), steps_out)
    profile_copy = out_base.parent / f"{scenario_name}_profile.json"
    shutil.copyfile(profile_path, profile_copy)
    title = (f"{profile.battery.manufacturer} {profile.battery.model} — "
             f"emulator static sweep")
    has_plot = _save_static_plot(out_base.with_suffix(".png"), steps_out, title)
    print(f"  → wrote {out_base.name}.json / .csv"
          f"{' / .png' if has_plot else ''} and profile snapshot")
    return scenario


def run_dynamic_pattern(*, profile_path: Path, load: _LoadAdapter,
                        smu_port: Optional[str] = None,
                        pattern: Optional[list[tuple[str, float, float]]] = None,
                        pattern_name: str = "standard",
                        cycles: int = 1, sample_hz: float = 20.0,
                        output_dir: Path = SCENARIO_DIR) -> dict[str, Any]:
    profile = BatteryProfile.load(profile_path)
    stem = profile_path.stem
    overrides = PROFILE_OVERRIDES.get(stem, {})
    pattern = list(pattern if pattern is not None else DEFAULT_IOT_PATTERN)
    safety_R = overrides.get("qr_safety_R", 12.0)
    safety_V = overrides.get("safety_max_V", 3.5)
    sample_dt = 1.0 / sample_hz

    suffix = "dynamic" if pattern_name == "standard" else f"dynamic-{pattern_name}"
    scenario_name = f"{_slugify(stem)}_{suffix}_{load.kind}_{_timestamp()}"
    out_base = output_dir / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)

    cycle_total = sum(d for _, _, d in pattern)
    total_s = cycle_total * cycles
    print(f"[dynamic/{load.kind}] {stem}: {cycles} cycles × "
          f"{len(pattern)} phases = {total_s:.1f}s @ {sample_hz:.0f}Hz")

    cfg = _make_emulator_config(profile, overrides)
    samples_out: list[dict[str, Any]] = []
    phase_events: list[dict[str, Any]] = []

    smu_kwargs = {"port": smu_port} if smu_port else {}
    sleep_idle_R = pattern[0][1]
    with SMU.open(**smu_kwargs) as smu:
        load.open()
        try:
            load.setup(safety_R=safety_R, voltage_V=safety_V)
            load.set_resistance(sleep_idle_R)
            time.sleep(0.3)
            emu = Emulator(smu, cfg)
            emu.start()
            try:
                time.sleep(2.0)
                t0 = time.monotonic()
                for cycle_idx in range(cycles):
                    for label, r_ohm, dur in pattern:
                        load.set_resistance(r_ohm)
                        phase_start = time.monotonic()
                        phase_events.append({
                            "cycle": cycle_idx,
                            "phase": label,
                            "t_start_s": phase_start - t0,
                            "r_setpoint_ohm": r_ohm,
                            "duration_s": dur,
                        })
                        next_sample = phase_start
                        end = phase_start + dur
                        while time.monotonic() < end:
                            now = time.monotonic()
                            if now >= next_sample:
                                rec = _step_record(
                                    emu, r_setpoint=r_ohm,
                                    r_actual=float("nan"), phase=label,
                                )
                                rec["t_s"] = now - t0
                                rec["cycle"] = cycle_idx
                                # Direct load measurement (DL3031A only)
                                load_m = load.measure()
                                if load_m is not None:
                                    rec["dl_voltage_V"] = load_m["voltage_V"]
                                    rec["dl_current_A"] = load_m["current_A"]
                                samples_out.append(rec)
                                next_sample += sample_dt
                            else:
                                time.sleep(min(0.005, end - now))
                load.set_resistance(sleep_idle_R)
            finally:
                load.teardown(sleep_idle_R)
                emu.stop()
        finally:
            load.close()

    # Per-phase summary (V min/max and mean I — both emulator and, if
    # available, the load's own direct measurements)
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for s in samples_out:
        by_phase.setdefault(s["phase"], []).append(s)
    has_load_meas = (
        len(samples_out) > 0 and "dl_voltage_V" in samples_out[0]
    )
    phase_summary: list[dict[str, Any]] = []
    for label, rows in by_phase.items():
        vs = [r["v_out_V"] for r in rows]
        is_ = [r["i_measured_A"] for r in rows]
        entry = {
            "phase": label,
            "n_samples": len(rows),
            "v_min_V": min(vs),
            "v_max_V": max(vs),
            "v_mean_V": sum(vs) / len(vs),
            "i_mean_A": sum(is_) / len(is_),
            "i_min_A": min(is_),
            "i_max_A": max(is_),
        }
        if has_load_meas:
            vs_dl = [r["dl_voltage_V"] for r in rows]
            is_dl = [r["dl_current_A"] for r in rows]
            entry.update({
                "dl_v_min_V": min(vs_dl),
                "dl_v_max_V": max(vs_dl),
                "dl_v_mean_V": sum(vs_dl) / len(vs_dl),
                "dl_i_min_A": min(is_dl),
                "dl_i_max_A": max(is_dl),
                "dl_i_mean_A": sum(is_dl) / len(is_dl),
            })
        phase_summary.append(entry)
    for ps in phase_summary:
        line = (f"  phase {ps['phase']:>6}: n={ps['n_samples']:<4}  "
                f"emu V[{ps['v_min_V']:.4f}..{ps['v_max_V']:.4f}]  "
                f"emu I_mean={ps['i_mean_A']*1000:+.3f}mA")
        if has_load_meas:
            line += (f"  dl V[{ps['dl_v_min_V']:.4f}..{ps['dl_v_max_V']:.4f}]"
                     f"  dl I_mean={ps['dl_i_mean_A']*1000:+.3f}mA")
        print(line)

    scenario = {
        "scenario": "dynamic_load_pattern",
        "schema_version": 2,
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "opensmu_version": OPENSMU_VERSION,
        "profile": {
            "path": str(profile_path),
            "stem": stem,
            "battery": {
                "manufacturer": profile.battery.manufacturer,
                "model": profile.battery.model,
                "nominal_voltage_V": profile.nominal_voltage,
                "nominal_capacity_mAh": profile.nominal_capacity_mAh,
                "cutoff_voltage_V": profile.cutoff_voltage,
            },
        },
        "emulator_config": {
            "initial_soc": cfg.initial_soc, "series": cfg.series,
            "parallel": cfg.parallel, "soc_tracking": cfg.soc_tracking,
            "safety_max_voltage_V": cfg.safety_max_voltage_V,
            "current_limit_A": cfg.current_limit_A,
            "update_interval_s": cfg.update_interval_s,
            "voltage_range_resolved": ("high" if profile.ocv_at(0.0) > 3.4 else "low"),
        },
        "bench": {**load.bench_dict},
        "pattern_name": pattern_name,
        "pattern": [{"phase": p[0], "r_ohm": p[1], "duration_s": p[2]} for p in pattern],
        "cycles": cycles,
        "sample_hz": sample_hz,
        "phase_events": phase_events,
        "phase_summary": phase_summary,
        "samples": samples_out,
    }

    out_base.with_suffix(".json").write_text(
        json.dumps(scenario, indent=2), encoding="utf-8")
    _save_csv(out_base.with_suffix(".csv"), samples_out)
    profile_copy = out_base.parent / f"{scenario_name}_profile.json"
    shutil.copyfile(profile_path, profile_copy)
    pattern_tag = "" if pattern_name == "standard" else f" ({pattern_name})"
    title = (f"{profile.battery.manufacturer} {profile.battery.model} — "
             f"emulator dynamic IoT pattern{pattern_tag} / {load.kind}")
    has_plot = _save_dynamic_plot(out_base.with_suffix(".png"), samples_out, title)
    print(f"  → wrote {out_base.name}.json / .csv"
          f"{' / .png' if has_plot else ''} and profile snapshot")
    return scenario


def _save_hires_plot(path: Path, samples: list[dict[str, Any]],
                     phase_events: list[dict[str, Any]], title: str) -> bool:
    try:
        import matplotlib
        matplotlib.use("Agg")
        import matplotlib.pyplot as plt
    except Exception:
        return False
    ts = [s["t_s"] for s in samples]
    vs = [s["voltage_V"] for s in samples]
    is_mA = [s["current_A"] * 1000.0 for s in samples]
    fig, axes = plt.subplots(2, 1, figsize=(11, 6), sharex=True)
    axes[0].plot(ts, vs, color="#1f77b4", linewidth=0.6)
    axes[0].set_ylabel("V_arc (V)")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(ts, is_mA, color="#d62728", linewidth=0.6)
    axes[1].set_ylabel("I_load (mA)")
    axes[1].set_xlabel("t (s)")
    axes[1].grid(True, alpha=0.3)
    colors = {"sleep": "#eef7ee", "wake": "#fff4e6", "tx": "#ffeaea"}
    for ax in axes:
        for ev in phase_events:
            t0 = ev["t_start_s"]
            t1 = t0 + ev["duration_s"]
            ax.axvspan(t0, t1, color=colors.get(ev["phase"], "#ffffff"), alpha=0.35)
    fig.suptitle(title)
    fig.tight_layout()
    fig.savefig(path, dpi=140)
    plt.close(fig)
    return True


def run_dynamic_hires(*, profile_path: Path, load: _LoadAdapter,
                      smu_port: Optional[str] = None,
                      pattern: Optional[list[tuple[str, float, float]]] = None,
                      cycles: int = 1,
                      output_dir: Path = SCENARIO_DIR) -> dict[str, Any]:
    """High-resolution dynamic capture.

    Uses an :py:meth:`SMU.record` session for the V/I time series — the
    Arc Pro's native streaming rate is much higher (kHz-class) than the
    state-polling path in :py:func:`run_dynamic_pattern`. The harness
    only timestamps phase transitions; the actual data comes from the
    recording.

    The DL3031A is driven but **not polled** during the run — its 10
    PLC (200 ms) integration is the wrong tool for sub-100 ms
    transients anyway.
    """
    profile = BatteryProfile.load(profile_path)
    stem = profile_path.stem
    overrides = PROFILE_OVERRIDES.get(stem, {})
    pattern = list(pattern if pattern is not None else DEFAULT_HIRES_IOT_PATTERN)
    safety_R = overrides.get("qr_safety_R", 12.0)
    safety_V = overrides.get("safety_max_V", 3.5)

    scenario_name = f"{_slugify(stem)}_dynamic-hires_{load.kind}_{_timestamp()}"
    out_base = output_dir / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)

    cycle_total = sum(d for _, _, d in pattern)
    total_s = cycle_total * cycles
    print(f"[hires/{load.kind}] {stem}: {cycles} cycles × "
          f"{len(pattern)} phases = {total_s:.2f}s, "
          f"streaming MAIN_V + MAIN_C from Arc")

    cfg = _make_emulator_config(profile, overrides)
    phase_events: list[dict[str, Any]] = []
    samples_out: list[dict[str, Any]] = []
    record_rate_v: Optional[int] = None
    record_rate_i: Optional[int] = None

    smu_kwargs = {"port": smu_port} if smu_port else {}
    sleep_idle_R = pattern[0][1]
    pinned_V: Optional[float] = None
    with SMU.open(**smu_kwargs) as smu:
        load.open()
        try:
            load.setup(safety_R=safety_R, voltage_V=safety_V)
            load.set_resistance(sleep_idle_R)
            time.sleep(0.3)
            # Use the emulator briefly to land V at fresh OCV under the
            # right range / current-limit setup, then stop it before
            # recording: emulator loop + recording reader contend on
            # the Arc's transport and deadlock under load.
            emu = Emulator(smu, cfg)
            emu.start()
            try:
                time.sleep(2.0)
                pinned_V = emu.state().output_voltage_V
            finally:
                emu.stop()
            time.sleep(0.3)
            # Re-pin V manually so the Arc still sources during the
            # recording — emulator is off, so OCV is frozen.
            smu.set_voltage(pinned_V)
            smu.set_output(True)
            time.sleep(0.3)
            try:
                with smu.record(Channel.MAIN_VOLTAGE, Channel.MAIN_CURRENT,
                                name=scenario_name) as rec:
                    t0 = time.monotonic()
                    for cycle_idx in range(cycles):
                        for label, r_ohm, dur in pattern:
                            load.set_resistance(r_ohm)
                            t_change = time.monotonic() - t0
                            phase_events.append({
                                "cycle": cycle_idx,
                                "phase": label,
                                "t_start_s": t_change,
                                "r_setpoint_ohm": r_ohm,
                                "duration_s": dur,
                            })
                            time.sleep(dur)
                # Extract recording — values + timestamps per channel
                vs = rec.data(Channel.MAIN_VOLTAGE)
                ts_v = rec.timestamps(Channel.MAIN_VOLTAGE)
                is_ = rec.data(Channel.MAIN_CURRENT)
                ts_i = rec.timestamps(Channel.MAIN_CURRENT)
                record_rate_v = rec.buffer(Channel.MAIN_VOLTAGE).sample_rate
                record_rate_i = rec.buffer(Channel.MAIN_CURRENT).sample_rate
                # Merge to a single time axis by interpolation on the I rate
                # (current is the higher-rate channel typically).
                merged_ts = ts_i
                merged_v: list[float] = []
                vi = 0
                for t_i in ts_i:
                    while vi + 1 < len(ts_v) and ts_v[vi + 1] <= t_i:
                        vi += 1
                    merged_v.append(vs[vi] if vs else float("nan"))
                for t_s, v, i in zip(merged_ts, merged_v, is_):
                    # Tag phase based on phase_events
                    phase = None
                    for ev in phase_events:
                        if ev["t_start_s"] <= t_s < ev["t_start_s"] + ev["duration_s"]:
                            phase = ev["phase"]
                            break
                    samples_out.append({
                        "t_s": float(t_s),
                        "voltage_V": float(v),
                        "current_A": float(i),
                        "phase": phase,
                    })
            finally:
                load.teardown(sleep_idle_R)
                try:
                    smu.set_output(False)
                except Exception:
                    pass
        finally:
            load.close()

    # Per-phase summary
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for s in samples_out:
        if s["phase"] is None:
            continue
        by_phase.setdefault(s["phase"], []).append(s)
    phase_summary: list[dict[str, Any]] = []
    for label, rows in by_phase.items():
        vs = [r["voltage_V"] for r in rows]
        is_ = [r["current_A"] for r in rows]
        phase_summary.append({
            "phase": label,
            "n_samples": len(rows),
            "v_min_V": min(vs), "v_max_V": max(vs),
            "v_mean_V": sum(vs) / len(vs),
            "i_min_A": min(is_), "i_max_A": max(is_),
            "i_mean_A": sum(is_) / len(is_),
        })
    for ps in phase_summary:
        print(f"  phase {ps['phase']:>6}: n={ps['n_samples']:<5}  "
              f"V[{ps['v_min_V']:.4f}..{ps['v_max_V']:.4f}]  "
              f"I[{ps['i_min_A']*1000:+.2f}..{ps['i_max_A']*1000:+.2f}] mA")

    scenario = {
        "scenario": "dynamic_load_pattern_hires",
        "schema_version": 2,
        "captured_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "opensmu_version": OPENSMU_VERSION,
        "profile": {
            "path": str(profile_path), "stem": stem,
            "battery": {
                "manufacturer": profile.battery.manufacturer,
                "model": profile.battery.model,
                "nominal_voltage_V": profile.nominal_voltage,
                "nominal_capacity_mAh": profile.nominal_capacity_mAh,
                "cutoff_voltage_V": profile.cutoff_voltage,
            },
        },
        "emulator_config": {
            "initial_soc": cfg.initial_soc, "series": cfg.series,
            "parallel": cfg.parallel, "soc_tracking": cfg.soc_tracking,
            "safety_max_voltage_V": cfg.safety_max_voltage_V,
            "current_limit_A": cfg.current_limit_A,
            "update_interval_s": cfg.update_interval_s,
            "voltage_range_resolved": ("high" if profile.ocv_at(0.0) > 3.4 else "low"),
        },
        "bench": {**load.bench_dict},
        "pattern_name": "hires",
        "pattern": [{"phase": p[0], "r_ohm": p[1], "duration_s": p[2]} for p in pattern],
        "cycles": cycles,
        "recording": {
            "main_voltage_sample_rate_Hz": record_rate_v,
            "main_current_sample_rate_Hz": record_rate_i,
            "n_samples": len(samples_out),
            "duration_s": (samples_out[-1]["t_s"] - samples_out[0]["t_s"])
                          if len(samples_out) >= 2 else 0.0,
            "pinned_voltage_V": pinned_V,
            "note": (
                "Emulator settled to OCV first, then stopped before "
                "recording. SMU was pinned at that V for the duration. "
                "SoC tracking off during recording — short window."
            ),
        },
        "phase_events": phase_events,
        "phase_summary": phase_summary,
        "samples": samples_out,
    }

    out_base.with_suffix(".json").write_text(
        json.dumps(scenario, indent=2), encoding="utf-8")
    _save_csv(out_base.with_suffix(".csv"), samples_out)
    profile_copy = out_base.parent / f"{scenario_name}_profile.json"
    shutil.copyfile(profile_path, profile_copy)
    title = (f"{profile.battery.manufacturer} {profile.battery.model} — "
             f"emulator hires IoT pattern / {load.kind} "
             f"(Arc native streaming, {record_rate_i} Hz I)")
    has_plot = _save_hires_plot(out_base.with_suffix(".png"), samples_out,
                                phase_events, title)
    print(f"  → wrote {out_base.name}.json / .csv"
          f"{' / .png' if has_plot else ''} and profile snapshot")
    return scenario


def _all_profile_stems() -> list[str]:
    return sorted(p.stem for p in PROFILE_DIR.glob("*.json"))


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--scenario", choices=["static", "dynamic"], default="static")
    parser.add_argument("--profile", help="profile stem or full JSON path")
    parser.add_argument("--all", action="store_true",
                        help="run scenario across every bundled profile")
    parser.add_argument("--load", choices=["qr10x", "dl3031a"], default="qr10x",
                        help="which programmable load to drive (default: qr10x)")
    parser.add_argument("--qr-port", default="COM7",
                        help="QR10x COM port (--load qr10x only)")
    parser.add_argument("--dl-resource", default=None,
                        help="DL3031A VISA resource string (--load dl3031a only); "
                             "auto-discovers Rigol VID/PID if omitted")
    parser.add_argument("--smu-port", default=None,
                        help="explicit Arc Pro COM port; auto-discover if omitted")
    parser.add_argument("--cycles", type=int, default=1,
                        help="dynamic scenario: pattern repeats")
    parser.add_argument("--pattern", choices=list(PATTERNS.keys()), default="standard",
                        help="dynamic scenario pattern (default: standard)")
    parser.add_argument("--sample-hz", type=float, default=None,
                        help="dynamic sample rate; default 20 Hz for standard, "
                             "50 Hz for hires")
    parser.add_argument("--settle-s", type=float, default=1.2)
    parser.add_argument("--output-dir", default=str(SCENARIO_DIR))
    args = parser.parse_args(argv)

    out_dir = Path(args.output_dir)

    targets: list[Path]
    if args.all:
        targets = [_resolve_profile(s) for s in _all_profile_stems()]
    elif args.profile:
        targets = [_resolve_profile(args.profile)]
    else:
        parser.error("--profile or --all is required")

    sample_hz = args.sample_hz
    if sample_hz is None:
        sample_hz = 50.0 if args.pattern == "hires" else 20.0
    pattern = PATTERNS[args.pattern]

    for prof in targets:
        load = _make_load(args.load, args.qr_port, args.dl_resource)
        try:
            if args.scenario == "static":
                run_static_sweep(profile_path=prof, load=load,
                                 smu_port=args.smu_port, settle_s=args.settle_s,
                                 output_dir=out_dir)
            elif args.pattern == "hires":
                run_dynamic_hires(profile_path=prof, load=load,
                                  smu_port=args.smu_port, cycles=args.cycles,
                                  pattern=pattern, output_dir=out_dir)
            else:
                run_dynamic_pattern(profile_path=prof, load=load,
                                    smu_port=args.smu_port, cycles=args.cycles,
                                    pattern=pattern, pattern_name=args.pattern,
                                    sample_hz=sample_hz, output_dir=out_dir)
        except Exception as e:
            print(f"[error] {prof.stem}: {e}", file=sys.stderr)
            if not args.all:
                raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

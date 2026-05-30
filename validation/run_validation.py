"""Bench-validation harness for the OpenSMU battery emulator.

Runs reproducible "scenarios" that drive the Otii Arc Pro emulator against
a programmable load (Eastwood QR10x) and saves the captured response to
disk as a JSON + CSV pair (optionally a PNG plot) under ``scenarios/``.

Two scenario types:

* ``static``  — step through a fixed list of load resistances, let each
  step settle, record the emulator's V/I/SoC/ESR snapshot. Use this to
  verify the emulator's ESR sag matches what the profile predicts.

* ``dynamic`` — drive the QR through a time-varying pattern (e.g. an
  IoT sleep / wake / TX cycle) while polling the emulator's state at a
  fixed rate. Use this to verify transient response.

Each saved scenario is self-contained: it embeds a copy of the battery
profile JSON, the emulator config, the bench config, and the raw
captured data. Re-running ``run_validation.py --replay <scenario.json>``
is a future regression-test entry point.

CLI (live capture)::

    python validation/run_validation.py --profile CR2032-Energizer-(25) \\
        --scenario static --qr-port COM7

    python validation/run_validation.py --scenario dynamic \\
        --profile CR2032-Energizer-(25) --cycles 3

Use ``--all`` to run the full static matrix across every bundled profile.
"""
from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from opensmu import SMU
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
# compared to the dwell of each phase.
DEFAULT_IOT_PATTERN = [
    ("sleep", 320_000, 5.0),  # ~10 µA at 3.2 V
    ("wake",    3_200, 0.6),  # ~1 mA sensor read
    ("sleep", 320_000, 1.5),
    ("tx",        100, 0.4),  # ~32 mA burst
    ("sleep", 320_000, 5.0),
]

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
    fig, axes = plt.subplots(3, 1, figsize=(11, 7), sharex=True)
    axes[0].plot(ts, vs, color="#1f77b4")
    axes[0].set_ylabel("V_out (V)")
    axes[0].grid(True, alpha=0.3)
    axes[1].plot(ts, is_mA, color="#d62728")
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


def run_static_sweep(*, profile_path: Path, qr_port: str, smu_port: Optional[str] = None,
                     r_steps: Optional[list[float]] = None, settle_s: float = 1.2,
                     output_dir: Path = SCENARIO_DIR) -> dict[str, Any]:
    profile = BatteryProfile.load(profile_path)
    stem = profile_path.stem
    overrides = PROFILE_OVERRIDES.get(stem, {})
    r_steps = list(r_steps if r_steps is not None else DEFAULT_STATIC_STEPS)
    qr_safety = overrides.get("qr_safety_R", 12.0)

    scenario_name = f"{_slugify(stem)}_static_{_timestamp()}"
    out_base = output_dir / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)

    print(f"[static] {stem}: {len(r_steps)} steps, settle={settle_s}s, "
          f"V_safe={overrides.get('safety_max_V', 3.5)}, QR R_min={qr_safety}Ω")

    cfg = _make_emulator_config(profile, overrides)
    steps_out: list[dict[str, Any]] = []
    qr_info: dict[str, Any] = {}

    smu_kwargs = {"port": smu_port} if smu_port else {}
    with SMU.open(**smu_kwargs) as smu, QR10x.open(qr_port) as qr:
        info = qr.info()
        qr_info = {
            "device_type": info.device_type, "serial": info.serial,
            "hardware_version": info.hardware_version,
            "firmware_version": info.firmware_version,
            "port": qr_port,
            "safety_limit_ohm_applied": qr_safety,
        }
        qr.set_safety_limit(qr_safety)
        qr.set_resistance(r_steps[0])
        time.sleep(0.3)
        emu = Emulator(smu, cfg)
        emu.start()
        try:
            time.sleep(2.0)  # let emulator settle on its first OCV setpoint
            for r in r_steps:
                qr.set_resistance(r)
                r_actual = qr.actual_resistance()
                time.sleep(settle_s)
                rec = _step_record(emu, r_setpoint=r, r_actual=r_actual)
                steps_out.append(rec)
                print(f"  R={r:>8.1f}Ω  Vout={rec['v_out_V']:.4f}V  "
                      f"I={rec['i_measured_A']*1000:+.3f}mA  "
                      f"ΔV={rec['voltage_error_mV']:+.2f}mV  "
                      f"SoC={rec['soc_pct']:.3f}%")
        finally:
            try:
                qr.set_resistance(r_steps[0])
            except Exception:
                pass
            emu.stop()

    scenario = {
        "scenario": "static_load_sweep",
        "schema_version": 1,
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
            "load_kind": "Eastwood QR10x programmable resistor",
            **qr_info,
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


def run_dynamic_pattern(*, profile_path: Path, qr_port: str,
                        smu_port: Optional[str] = None,
                        pattern: Optional[list[tuple[str, float, float]]] = None,
                        cycles: int = 1, sample_hz: float = 20.0,
                        output_dir: Path = SCENARIO_DIR) -> dict[str, Any]:
    profile = BatteryProfile.load(profile_path)
    stem = profile_path.stem
    overrides = PROFILE_OVERRIDES.get(stem, {})
    pattern = list(pattern if pattern is not None else DEFAULT_IOT_PATTERN)
    qr_safety = overrides.get("qr_safety_R", 12.0)
    sample_dt = 1.0 / sample_hz

    scenario_name = f"{_slugify(stem)}_dynamic_{_timestamp()}"
    out_base = output_dir / scenario_name
    output_dir.mkdir(parents=True, exist_ok=True)

    cycle_total = sum(d for _, _, d in pattern)
    total_s = cycle_total * cycles
    print(f"[dynamic] {stem}: {cycles} cycles × "
          f"{len(pattern)} phases = {total_s:.1f}s @ {sample_hz:.0f}Hz")

    cfg = _make_emulator_config(profile, overrides)
    samples_out: list[dict[str, Any]] = []
    phase_events: list[dict[str, Any]] = []
    qr_info: dict[str, Any] = {}

    smu_kwargs = {"port": smu_port} if smu_port else {}
    sleep_idle_R = pattern[0][1]
    with SMU.open(**smu_kwargs) as smu, QR10x.open(qr_port) as qr:
        info = qr.info()
        qr_info = {"device_type": info.device_type, "serial": info.serial,
                   "hardware_version": info.hardware_version,
                   "firmware_version": info.firmware_version,
                   "port": qr_port,
                   "safety_limit_ohm_applied": qr_safety}
        qr.set_safety_limit(qr_safety)
        qr.set_resistance(sleep_idle_R)
        time.sleep(0.3)
        emu = Emulator(smu, cfg)
        emu.start()
        try:
            time.sleep(2.0)
            t0 = time.monotonic()
            for cycle_idx in range(cycles):
                for label, r_ohm, dur in pattern:
                    qr.set_resistance(r_ohm)
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
                            rec = _step_record(emu, r_setpoint=r_ohm,
                                               r_actual=float("nan"),
                                               phase=label)
                            rec["t_s"] = now - t0
                            rec["cycle"] = cycle_idx
                            samples_out.append(rec)
                            next_sample += sample_dt
                        else:
                            time.sleep(min(0.005, end - now))
            qr.set_resistance(sleep_idle_R)
        finally:
            try:
                qr.set_resistance(sleep_idle_R)
            except Exception:
                pass
            emu.stop()

    # Per-phase summary (V min/max and mean I)
    by_phase: dict[str, list[dict[str, Any]]] = {}
    for s in samples_out:
        by_phase.setdefault(s["phase"], []).append(s)
    phase_summary: list[dict[str, Any]] = []
    for label, rows in by_phase.items():
        vs = [r["v_out_V"] for r in rows]
        is_ = [r["i_measured_A"] for r in rows]
        phase_summary.append({
            "phase": label,
            "n_samples": len(rows),
            "v_min_V": min(vs),
            "v_max_V": max(vs),
            "v_mean_V": sum(vs) / len(vs),
            "i_mean_A": sum(is_) / len(is_),
            "i_min_A": min(is_),
            "i_max_A": max(is_),
        })
    for ps in phase_summary:
        print(f"  phase {ps['phase']:>6}: n={ps['n_samples']:<4}  "
              f"V[{ps['v_min_V']:.4f}..{ps['v_max_V']:.4f}]  "
              f"I_mean={ps['i_mean_A']*1000:+.3f}mA")

    scenario = {
        "scenario": "dynamic_load_pattern",
        "schema_version": 1,
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
        "bench": {
            "load_kind": "Eastwood QR10x programmable resistor",
            **qr_info,
        },
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
    title = (f"{profile.battery.manufacturer} {profile.battery.model} — "
             f"emulator dynamic IoT pattern")
    has_plot = _save_dynamic_plot(out_base.with_suffix(".png"), samples_out, title)
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
    parser.add_argument("--qr-port", default="COM7")
    parser.add_argument("--smu-port", default=None,
                        help="explicit Arc Pro COM port; auto-discover if omitted")
    parser.add_argument("--cycles", type=int, default=1,
                        help="dynamic scenario: pattern repeats")
    parser.add_argument("--sample-hz", type=float, default=20.0)
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

    for prof in targets:
        try:
            if args.scenario == "static":
                run_static_sweep(profile_path=prof, qr_port=args.qr_port,
                                 smu_port=args.smu_port, settle_s=args.settle_s,
                                 output_dir=out_dir)
            else:
                run_dynamic_pattern(profile_path=prof, qr_port=args.qr_port,
                                    smu_port=args.smu_port, cycles=args.cycles,
                                    sample_hz=args.sample_hz, output_dir=out_dir)
        except Exception as e:
            print(f"[error] {prof.stem}: {e}", file=sys.stderr)
            if not args.all:
                raise
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

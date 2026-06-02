"""Power-profile analysis library + CLI for sensor_profiler runs.

Loads every ``data/sensor_*.opensmu`` chunk inside a run folder,
detects radio TX bursts, computes per-chunk and overall power metrics,
and writes ``report/summary.md``, CSVs, and PNG plots.

CLI::

    python analyze.py --run runs/<run_id>
    python analyze.py --run runs/<run_id> --peek 0
    python analyze.py --runs-dir runs/  --all  # batch-process every run

This file is also used as a library by ``app.py``; the Streamlit UI
calls :py:func:`analyse_run` / :py:func:`load_run_summary` directly.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable, Optional

import numpy as np

from benchctrl import Recording
from benchctrl.drivers.otii_arc import OtiiArcChannel

APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / "runs"

# ---------------------------------------------------------------------------
# Configuration — battery models per chemistry (mAh / V_nominal)
# ---------------------------------------------------------------------------

BATTERY_MODELS = {
    "CR2477":  {"capacity_mAh": 1000.0, "voltage_V": 3.0},
    "CR2032":  {"capacity_mAh": 225.0,  "voltage_V": 3.0},
    "CR123A":  {"capacity_mAh": 1500.0, "voltage_V": 3.0},
    "AA":      {"capacity_mAh": 2000.0, "voltage_V": 1.5},
    "AAA":     {"capacity_mAh": 1000.0, "voltage_V": 1.5},
    "18650":   {"capacity_mAh": 3000.0, "voltage_V": 3.7},
}
DEFAULT_BATTERY = "CR2477"

# TX burst detector — see README for tuning notes
BURST_ENTER_MA = 1.0
BURST_EXIT_MA = 0.5
BURST_MIN_GAP_MS = 5.0
BURST_MIN_DURATION_MS = 0.5


# ---------------------------------------------------------------------------
# Data classes
# ---------------------------------------------------------------------------


@dataclass
class BurstEvent:
    chunk_idx: int
    start_s: float
    duration_ms: float
    peak_mA: float
    mean_mA: float
    charge_uC: float
    wall_start: Optional[_dt.datetime] = None


@dataclass
class ChunkAnalysis:
    chunk_idx: int
    path: Path
    start_time: Optional[_dt.datetime]
    duration_s: float
    sample_count: int
    avg_current_mA: float
    peak_current_mA: float
    min_current_mA: float
    median_current_mA: float
    avg_voltage_V: float
    min_voltage_V: float
    max_voltage_V: float
    charge_mAh: float
    energy_mWh: float
    estimated_sleep_current_mA: float
    bursts: list[BurstEvent] = field(default_factory=list)


@dataclass
class OverallSummary:
    run_id: str
    dut: str
    label: str
    chunks: list[ChunkAnalysis]
    metadata: dict
    n_chunks: int
    total_duration_s: float
    total_samples: int
    total_charge_mAh: float
    total_energy_mWh: float
    avg_current_mA: float
    sleep_current_mA: float
    n_bursts: int
    burst_rate_per_hour: float
    avg_burst_peak_mA: float
    avg_burst_duration_ms: float
    avg_charge_per_burst_uC: float
    battery_model: str
    projected_life_hours: float
    projected_life_days: float
    projected_life_years: float


# ---------------------------------------------------------------------------
# Loaders
# ---------------------------------------------------------------------------


def list_runs(runs_dir: Path = RUNS_DIR) -> list[Path]:
    """Return every run folder under ``runs_dir`` in chronological order.

    A run folder is anything with a ``metadata.json``.
    """
    if not runs_dir.exists():
        return []
    runs = sorted(
        p for p in runs_dir.iterdir()
        if p.is_dir() and (p / "metadata.json").exists()
    )
    return runs


def load_run_metadata(run_dir: Path) -> dict:
    p = run_dir / "metadata.json"
    if not p.exists():
        return {}
    return json.loads(p.read_text(encoding="utf-8"))


def discover_chunks(run_dir: Path) -> list[Path]:
    data_dir = run_dir / "data"
    if not data_dir.exists():
        return []
    return sorted(data_dir.glob("sensor_*.opensmu"))


def channel_arrays(
    rec: Recording, channel: OtiiArcChannel,
) -> tuple[np.ndarray, np.ndarray]:
    if channel not in rec.channels:
        return np.array([], dtype=np.float64), np.array([], dtype=np.float64)
    ts = np.asarray(rec.timestamps(channel), dtype=np.float64)
    vals = np.asarray(rec.data(channel), dtype=np.float64)
    return ts, vals


# ---------------------------------------------------------------------------
# Burst detection
# ---------------------------------------------------------------------------


def detect_bursts(
    timestamps: np.ndarray,
    current_A: np.ndarray,
    chunk_idx: int = 0,
    wall_start: Optional[_dt.datetime] = None,
) -> list[BurstEvent]:
    if current_A.size == 0:
        return []
    current_mA = current_A * 1000.0
    active = current_mA > BURST_ENTER_MA
    if not active.any():
        return []

    padded = np.concatenate([[False], active, [False]])
    diff = np.diff(padded.astype(np.int8))
    starts = np.where(diff == 1)[0]
    ends = np.where(diff == -1)[0]

    # Refine: hysteresis exit at BURST_EXIT_MA
    below_exit = current_mA <= BURST_EXIT_MA
    refined_ends = []
    for s, e in zip(starts, ends):
        tail = e
        while tail < len(current_mA) and not below_exit[tail]:
            tail += 1
        refined_ends.append(tail)
    ends = np.asarray(refined_ends, dtype=np.int64)

    # Merge events whose gap is < MIN_GAP_MS
    merged_starts = [starts[0]] if len(starts) > 0 else []
    merged_ends = [ends[0]] if len(ends) > 0 else []
    for s, e in zip(starts[1:], ends[1:]):
        prev_end_idx = max(merged_ends[-1] - 1, 0)
        gap_ms = (timestamps[s] - timestamps[prev_end_idx]) * 1000.0
        if gap_ms < BURST_MIN_GAP_MS:
            merged_ends[-1] = e
        else:
            merged_starts.append(s)
            merged_ends.append(e)

    out: list[BurstEvent] = []
    for s, e in zip(merged_starts, merged_ends):
        seg = current_mA[s:e]
        seg_t = timestamps[s:e]
        if seg.size < 1:
            continue
        duration_ms = (seg_t[-1] - seg_t[0]) * 1000.0
        if duration_ms < BURST_MIN_DURATION_MS:
            continue
        charge_C = np.trapezoid(seg / 1000.0, seg_t)
        ev = BurstEvent(
            chunk_idx=chunk_idx,
            start_s=float(seg_t[0]),
            duration_ms=float(duration_ms),
            peak_mA=float(seg.max()),
            mean_mA=float(seg.mean()),
            charge_uC=float(charge_C * 1e6),
            wall_start=(
                wall_start + _dt.timedelta(seconds=float(seg_t[0]))
                if wall_start is not None else None
            ),
        )
        out.append(ev)
    return out


# ---------------------------------------------------------------------------
# Per-chunk metrics
# ---------------------------------------------------------------------------


def analyse_chunk(path: Path, chunk_idx: int) -> ChunkAnalysis:
    rec = Recording.load(path)
    ts_i, i_A = channel_arrays(rec, OtiiArcChannel.MAIN_CURRENT)
    ts_v, v_V = channel_arrays(rec, OtiiArcChannel.MAIN_VOLTAGE)

    duration_s = float(ts_i[-1] - ts_i[0]) if ts_i.size > 1 else 0.0
    bursts = detect_bursts(ts_i, i_A, chunk_idx=chunk_idx,
                           wall_start=rec.start_time)

    if i_A.size:
        i_mA = i_A * 1000.0
        avg_mA = float(np.mean(i_mA))
        peak_mA = float(np.max(i_mA))
        min_mA = float(np.min(i_mA))
        median_mA = float(np.median(i_mA))
        sleep_mA = float(np.percentile(i_mA, 10))
        charge_C = float(np.trapezoid(i_A, ts_i))
        if v_V.size > 1:
            v_at_i = np.interp(ts_i, ts_v, v_V)
            energy_J = float(np.trapezoid(i_A * v_at_i, ts_i))
        else:
            energy_J = charge_C * 3.0  # rough fallback if no V channel
    else:
        avg_mA = peak_mA = min_mA = median_mA = sleep_mA = 0.0
        charge_C = energy_J = 0.0

    if v_V.size:
        avg_voltage_V = float(np.mean(v_V))
        min_voltage_V = float(np.min(v_V))
        max_voltage_V = float(np.max(v_V))
    else:
        avg_voltage_V = min_voltage_V = max_voltage_V = float("nan")

    return ChunkAnalysis(
        chunk_idx=chunk_idx,
        path=path,
        start_time=rec.start_time,
        duration_s=duration_s,
        sample_count=int(i_A.size),
        avg_current_mA=avg_mA,
        peak_current_mA=peak_mA,
        min_current_mA=min_mA,
        median_current_mA=median_mA,
        avg_voltage_V=avg_voltage_V,
        min_voltage_V=min_voltage_V,
        max_voltage_V=max_voltage_V,
        charge_mAh=charge_C / 3.6,
        energy_mWh=energy_J / 3.6,
        estimated_sleep_current_mA=sleep_mA,
        bursts=bursts,
    )


def summarise(
    chunks: list[ChunkAnalysis],
    metadata: dict,
    battery_model: str = DEFAULT_BATTERY,
) -> OverallSummary:
    capacity_mAh = BATTERY_MODELS.get(battery_model, {}).get(
        "capacity_mAh", BATTERY_MODELS[DEFAULT_BATTERY]["capacity_mAh"],
    )

    if not chunks:
        return OverallSummary(
            run_id=metadata.get("run_id", ""),
            dut=metadata.get("dut", ""),
            label=metadata.get("label", ""),
            chunks=[], metadata=metadata,
            n_chunks=0, total_duration_s=0, total_samples=0,
            total_charge_mAh=0, total_energy_mWh=0,
            avg_current_mA=0, sleep_current_mA=0, n_bursts=0,
            burst_rate_per_hour=0, avg_burst_peak_mA=0,
            avg_burst_duration_ms=0, avg_charge_per_burst_uC=0,
            battery_model=battery_model,
            projected_life_hours=0, projected_life_days=0,
            projected_life_years=0,
        )

    total_duration_s = sum(c.duration_s for c in chunks)
    total_samples = sum(c.sample_count for c in chunks)
    total_charge_mAh = sum(c.charge_mAh for c in chunks)
    total_energy_mWh = sum(c.energy_mWh for c in chunks)
    avg_current_mA = (
        total_charge_mAh / (total_duration_s / 3600.0)
        if total_duration_s > 0 else 0.0
    )
    sleep_current_mA = float(np.median(
        [c.estimated_sleep_current_mA for c in chunks]
    ))

    all_bursts = [b for c in chunks for b in c.bursts]
    n_bursts = len(all_bursts)
    if n_bursts:
        peaks = np.array([b.peak_mA for b in all_bursts])
        durations = np.array([b.duration_ms for b in all_bursts])
        charges = np.array([b.charge_uC for b in all_bursts])
        avg_burst_peak_mA = float(peaks.mean())
        avg_burst_duration_ms = float(durations.mean())
        avg_charge_per_burst_uC = float(charges.mean())
        burst_rate_per_hour = n_bursts / (total_duration_s / 3600.0) \
            if total_duration_s > 0 else 0.0
    else:
        avg_burst_peak_mA = 0.0
        avg_burst_duration_ms = 0.0
        avg_charge_per_burst_uC = 0.0
        burst_rate_per_hour = 0.0

    life_hours = capacity_mAh / avg_current_mA if avg_current_mA > 0 \
        else float("inf")

    return OverallSummary(
        run_id=metadata.get("run_id", ""),
        dut=metadata.get("dut", ""),
        label=metadata.get("label", ""),
        chunks=chunks, metadata=metadata,
        n_chunks=len(chunks),
        total_duration_s=total_duration_s,
        total_samples=total_samples,
        total_charge_mAh=total_charge_mAh,
        total_energy_mWh=total_energy_mWh,
        avg_current_mA=avg_current_mA,
        sleep_current_mA=sleep_current_mA,
        n_bursts=n_bursts,
        burst_rate_per_hour=burst_rate_per_hour,
        avg_burst_peak_mA=avg_burst_peak_mA,
        avg_burst_duration_ms=avg_burst_duration_ms,
        avg_charge_per_burst_uC=avg_charge_per_burst_uC,
        battery_model=battery_model,
        projected_life_hours=life_hours,
        projected_life_days=life_hours / 24.0,
        projected_life_years=life_hours / 24.0 / 365.25,
    )


# ---------------------------------------------------------------------------
# Plotting (matplotlib — static archival PNGs)
# ---------------------------------------------------------------------------


def _decimate(x: np.ndarray, y: np.ndarray, max_points: int = 50_000):
    if x.size <= max_points:
        return x, y
    stride = max(1, x.size // max_points)
    return x[::stride], y[::stride]


def _plot_overview(summary: OverallSummary, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt

    if not summary.chunks:
        return

    fig, axes = plt.subplots(
        2, 1, figsize=(14, 7), sharex=True,
        gridspec_kw={"height_ratios": [2, 1]},
    )
    ax_i, ax_v = axes
    cumulative_offset = 0.0
    for c in summary.chunks:
        rec = Recording.load(c.path)
        ts_i, i_A = channel_arrays(rec, OtiiArcChannel.MAIN_CURRENT)
        ts_v, v_V = channel_arrays(rec, OtiiArcChannel.MAIN_VOLTAGE)
        if ts_i.size:
            x, y = _decimate((ts_i + cumulative_offset) / 3600.0,
                             i_A * 1000.0)
            ax_i.plot(x, y, linewidth=0.4, color="C0")
        if ts_v.size:
            x, y = _decimate((ts_v + cumulative_offset) / 3600.0, v_V)
            ax_v.plot(x, y, linewidth=0.6, color="C2")
        cumulative_offset += c.duration_s

    ax_i.set_ylabel("Current (mA)")
    ax_i.set_title(
        f"{summary.dut} — {summary.label} — full window"
    )
    ax_i.grid(True, alpha=0.3)
    ax_v.set_ylabel("Voltage (V)")
    ax_v.set_xlabel("Time since capture start (hours)")
    ax_v.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def _plot_cumulative_charge(summary: OverallSummary, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if not summary.chunks:
        return
    cumulative_mAh = 0.0
    cumulative_offset = 0.0
    times_h, charge_mAh = [], []
    for c in summary.chunks:
        rec = Recording.load(c.path)
        ts_i, i_A = channel_arrays(rec, OtiiArcChannel.MAIN_CURRENT)
        if ts_i.size > 1:
            running = np.cumsum(np.diff(ts_i) * (i_A[:-1] / 3.6))
            x = (ts_i[:-1] + cumulative_offset) / 3600.0
            y = cumulative_mAh + running
            x_d, y_d = _decimate(x, y)
            times_h.append(x_d)
            charge_mAh.append(y_d)
            cumulative_mAh = float(y[-1]) if y.size else cumulative_mAh
        cumulative_offset += c.duration_s

    fig, ax = plt.subplots(figsize=(12, 5))
    for x, y in zip(times_h, charge_mAh):
        ax.plot(x, y, color="C3", linewidth=1.0)
    ax.set_xlabel("Time since capture start (hours)")
    ax.set_ylabel("Cumulative charge (mAh)")
    ax.set_title(
        f"Cumulative draw — {cumulative_mAh:.4f} mAh over "
        f"{summary.total_duration_s / 3600:.2f} h"
    )
    ax.grid(True, alpha=0.3)
    cap = BATTERY_MODELS.get(summary.battery_model, {}).get(
        "capacity_mAh", 0.0,
    )
    if cap > 0 and summary.avg_current_mA > 0:
        deplete_h = cap / summary.avg_current_mA
        ax.axhline(
            cap, color="C7", linestyle="--", linewidth=0.8,
            label=f"{summary.battery_model} {cap:.0f} mAh — "
                  f"deplete at ~{deplete_h:.0f} h "
                  f"({deplete_h/24:.1f} d)",
        )
        ax.legend()
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


def _plot_burst_histograms(summary: OverallSummary, out_dir: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    all_bursts = [b for c in summary.chunks for b in c.bursts]
    if not all_bursts:
        return
    peaks = np.array([b.peak_mA for b in all_bursts])
    durations = np.array([b.duration_ms for b in all_bursts])

    cumulative_offset = 0.0
    abs_starts = []
    for c in summary.chunks:
        for b in c.bursts:
            abs_starts.append(b.start_s + cumulative_offset)
        cumulative_offset += c.duration_s
    abs_starts = np.array(abs_starts, dtype=np.float64)
    inter_arrival_s = np.diff(abs_starts) if abs_starts.size > 1 \
        else np.array([])

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(peaks, bins=40, color="C1", edgecolor="black", linewidth=0.4)
    ax.set_xlabel("Peak current (mA)")
    ax.set_ylabel("Count")
    ax.set_title(
        f"TX burst peak distribution (n={len(peaks)}, "
        f"mean {peaks.mean():.2f} mA)"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "burst_amplitude_hist.png",
                dpi=120, bbox_inches="tight")
    plt.close(fig)

    fig, ax = plt.subplots(figsize=(10, 4))
    ax.hist(durations, bins=40, color="C4", edgecolor="black", linewidth=0.4)
    ax.set_xlabel("Burst duration (ms)")
    ax.set_ylabel("Count")
    ax.set_title(
        f"TX burst duration distribution (mean {durations.mean():.2f} ms)"
    )
    ax.grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_dir / "burst_duration_hist.png",
                dpi=120, bbox_inches="tight")
    plt.close(fig)

    if inter_arrival_s.size:
        fig, ax = plt.subplots(figsize=(10, 4))
        positive = inter_arrival_s[inter_arrival_s > 0]
        if positive.size:
            bins = np.logspace(
                np.log10(max(positive.min(), 1e-3)),
                np.log10(positive.max()), 40,
            )
            ax.hist(positive, bins=bins, color="C0",
                    edgecolor="black", linewidth=0.4)
            ax.set_xscale("log")
        ax.set_xlabel("Inter-arrival time (s, log scale)")
        ax.set_ylabel("Count")
        ax.set_title(
            f"TX inter-arrival distribution "
            f"(median {np.median(inter_arrival_s):.2f} s)"
        )
        ax.grid(True, alpha=0.3, which="both")
        fig.tight_layout()
        fig.savefig(out_dir / "inter_arrival_hist.png",
                    dpi=120, bbox_inches="tight")
        plt.close(fig)


def _plot_per_chunk_trends(summary: OverallSummary, out_path: Path) -> None:
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    if not summary.chunks:
        return
    idxs = np.arange(len(summary.chunks))
    avg_mA = np.array([c.avg_current_mA for c in summary.chunks])
    sleep_mA = np.array(
        [c.estimated_sleep_current_mA for c in summary.chunks]
    )
    n_bursts = np.array([len(c.bursts) for c in summary.chunks])
    avg_voltage = np.array([c.avg_voltage_V for c in summary.chunks])

    fig, axes = plt.subplots(4, 1, figsize=(11, 9), sharex=True)
    axes[0].plot(idxs, avg_mA, marker="o", color="C0", linewidth=1.0)
    axes[0].set_ylabel("Avg I (mA)")
    axes[0].grid(True, alpha=0.3)
    axes[0].set_title("Per-chunk trends")
    axes[1].plot(idxs, sleep_mA * 1000.0, marker="s", color="C2",
                 linewidth=1.0)
    axes[1].set_ylabel("Sleep I (µA)")
    axes[1].grid(True, alpha=0.3)
    axes[2].plot(idxs, n_bursts, marker="^", color="C1", linewidth=1.0)
    axes[2].set_ylabel("Burst count")
    axes[2].grid(True, alpha=0.3)
    axes[3].plot(idxs, avg_voltage, marker="d", color="C3", linewidth=1.0)
    axes[3].set_ylabel("Avg V (V)")
    axes[3].set_xlabel("Chunk index")
    axes[3].grid(True, alpha=0.3)
    fig.tight_layout()
    fig.savefig(out_path, dpi=120, bbox_inches="tight")
    plt.close(fig)


# ---------------------------------------------------------------------------
# CSV + Markdown writers
# ---------------------------------------------------------------------------


def _write_per_chunk_csv(summary: OverallSummary, out_path: Path) -> None:
    import csv
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "chunk_idx", "path", "start_time_utc", "duration_s",
            "sample_count", "avg_current_mA", "peak_current_mA",
            "min_current_mA", "median_current_mA",
            "estimated_sleep_current_mA",
            "avg_voltage_V", "min_voltage_V", "max_voltage_V",
            "charge_mAh", "energy_mWh", "burst_count",
        ])
        for c in summary.chunks:
            w.writerow([
                c.chunk_idx, c.path.name,
                c.start_time.isoformat() if c.start_time else "",
                f"{c.duration_s:.3f}", c.sample_count,
                f"{c.avg_current_mA:.6f}", f"{c.peak_current_mA:.6f}",
                f"{c.min_current_mA:.6f}", f"{c.median_current_mA:.6f}",
                f"{c.estimated_sleep_current_mA:.6f}",
                f"{c.avg_voltage_V:.4f}",
                f"{c.min_voltage_V:.4f}", f"{c.max_voltage_V:.4f}",
                f"{c.charge_mAh:.6f}", f"{c.energy_mWh:.6f}",
                len(c.bursts),
            ])


def _write_burst_csv(summary: OverallSummary, out_path: Path) -> None:
    import csv
    with out_path.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow([
            "chunk_idx", "wall_start_utc", "start_s_in_chunk",
            "duration_ms", "peak_mA", "mean_mA", "charge_uC",
        ])
        for c in summary.chunks:
            for b in c.bursts:
                w.writerow([
                    b.chunk_idx,
                    b.wall_start.isoformat() if b.wall_start else "",
                    f"{b.start_s:.6f}",
                    f"{b.duration_ms:.4f}",
                    f"{b.peak_mA:.4f}",
                    f"{b.mean_mA:.4f}",
                    f"{b.charge_uC:.4f}",
                ])


def _format_duration(seconds: float) -> str:
    if not np.isfinite(seconds):
        return "∞"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _write_summary_md(summary: OverallSummary, out_path: Path) -> None:
    lines: list[str] = []
    lines.append(f"# {summary.dut} — {summary.label or 'run'}")
    lines.append("")
    lines.append(
        f"Generated: {_dt.datetime.now(_dt.timezone.utc).isoformat()}"
    )
    lines.append("")
    lines.append(f"- run_id: `{summary.run_id}`")
    lines.append(f"- status: `{summary.metadata.get('status', '?')}`")
    lines.append(f"- battery model: **{summary.battery_model}**")
    lines.append("")

    if not summary.chunks:
        lines.append("No chunks loaded.")
        out_path.write_text("\n".join(lines), encoding="utf-8")
        return

    lines.append("## Headline metrics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Chunks | {summary.n_chunks} |")
    lines.append(f"| Duration | {_format_duration(summary.total_duration_s)} |")
    lines.append(f"| Samples | {summary.total_samples:,} |")
    lines.append(
        f"| **Average current** | **{summary.avg_current_mA:.4f} mA** |"
    )
    lines.append(
        f"| Sleep / quiescent current | "
        f"{summary.sleep_current_mA*1000:.2f} µA |"
    )
    lines.append(f"| Charge consumed | {summary.total_charge_mAh:.4f} mAh |")
    lines.append(f"| Energy consumed | {summary.total_energy_mWh:.4f} mWh |")
    lines.append("")
    lines.append("## TX burst statistics")
    lines.append("")
    lines.append("| Metric | Value |")
    lines.append("|---|---|")
    lines.append(f"| Detected bursts | {summary.n_bursts} |")
    lines.append(f"| Burst rate | {summary.burst_rate_per_hour:.2f} /h |")
    lines.append(
        f"| Avg peak current | {summary.avg_burst_peak_mA:.2f} mA |"
    )
    lines.append(
        f"| Avg duration | {summary.avg_burst_duration_ms:.2f} ms |"
    )
    lines.append(
        f"| Avg charge per burst | "
        f"{summary.avg_charge_per_burst_uC:.2f} µC |"
    )
    lines.append("")
    lines.append(f"## {summary.battery_model} life projection")
    lines.append("")
    lines.append("| Horizon | Projected |")
    lines.append("|---|---|")
    lines.append(f"| Hours | {summary.projected_life_hours:.1f} |")
    lines.append(f"| Days  | {summary.projected_life_days:.1f} |")
    lines.append(f"| Years | {summary.projected_life_years:.2f} |")
    lines.append("")
    lines.append("_Best-case ceiling at nominal capacity / room "
                 "temperature. Real life is reduced by cutoff voltage, "
                 "self-discharge (~1 %/y), and temperature effects._")
    lines.append("")
    lines.append("## Plots")
    lines.append("")
    for fname in (
        "overview.png", "cumulative_charge.png", "per_chunk_trends.png",
        "burst_amplitude_hist.png", "burst_duration_hist.png",
        "inter_arrival_hist.png",
    ):
        if (out_path.parent / fname).exists():
            lines.append(f"- ![{fname}]({fname})")
    lines.append("")
    out_path.write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------------
# Run-level orchestration
# ---------------------------------------------------------------------------


def analyse_run(
    run_dir: Path,
    battery_model: str = DEFAULT_BATTERY,
    write_report: bool = True,
) -> OverallSummary:
    """Load every chunk in ``run_dir/data/``, compute metrics, write the
    report bundle to ``run_dir/report/`` if ``write_report``."""
    metadata = load_run_metadata(run_dir)
    chunks = discover_chunks(run_dir)
    analyses: list[ChunkAnalysis] = []
    for i, p in enumerate(chunks):
        analyses.append(analyse_chunk(p, i))
    summary = summarise(analyses, metadata, battery_model=battery_model)
    if write_report and summary.chunks:
        report_dir = run_dir / "report"
        report_dir.mkdir(parents=True, exist_ok=True)
        _write_per_chunk_csv(summary, report_dir / "per_chunk.csv")
        _write_burst_csv(summary, report_dir / "burst_events.csv")
        _plot_overview(summary, report_dir / "overview.png")
        _plot_cumulative_charge(summary, report_dir / "cumulative_charge.png")
        _plot_burst_histograms(summary, report_dir)
        _plot_per_chunk_trends(summary, report_dir / "per_chunk_trends.png")
        _write_summary_md(summary, report_dir / "summary.md")
    return summary


def load_run_summary(
    run_dir: Path, battery_model: str = DEFAULT_BATTERY,
) -> OverallSummary:
    """Convenience used by the Streamlit app — never writes to disk."""
    return analyse_run(run_dir, battery_model=battery_model,
                       write_report=False)


def peek_chunk(run_dir: Path, idx: int) -> None:
    chunks = discover_chunks(run_dir)
    if not chunks:
        print(f"[peek] no chunks in {run_dir}")
        return
    if idx < 0 or idx >= len(chunks):
        print(f"[peek] chunk index out of range (have {len(chunks)})")
        return
    path = chunks[idx]
    c = analyse_chunk(path, idx)
    print(f"[peek] {path.name}")
    print(f"  start:    {c.start_time}")
    print(f"  duration: {c.duration_s:.1f} s")
    print(f"  samples:  {c.sample_count:,}")
    print(f"  avg I:    {c.avg_current_mA:.4f} mA")
    print(f"  peak I:   {c.peak_current_mA:.2f} mA")
    print(f"  sleep I:  {c.estimated_sleep_current_mA*1000:.2f} µA")
    print(f"  voltage:  {c.avg_voltage_V:.4f} V "
          f"({c.min_voltage_V:.4f}–{c.max_voltage_V:.4f})")
    print(f"  charge:   {c.charge_mAh:.6f} mAh")
    print(f"  bursts:   {len(c.bursts)}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------


def main(argv: Iterable[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--run", help="path to a single run folder")
    p.add_argument("--runs-dir", default=str(RUNS_DIR),
                   help="root runs directory (for --all)")
    p.add_argument("--all", action="store_true",
                   help="analyse every run under --runs-dir")
    p.add_argument("--peek", type=int, default=None,
                   help="print one chunk's metrics and exit")
    p.add_argument("--battery", default=DEFAULT_BATTERY,
                   choices=list(BATTERY_MODELS.keys()),
                   help="battery model for life projection")
    args = p.parse_args(list(argv) if argv is not None else None)

    if args.all:
        runs_dir = Path(args.runs_dir)
        for run in list_runs(runs_dir):
            print(f"[analyse] {run.name}")
            analyse_run(run, battery_model=args.battery)
        return 0

    if not args.run:
        print("error: --run or --all required", file=sys.stderr)
        return 2

    run_dir = Path(args.run)
    if not run_dir.exists():
        print(f"error: {run_dir} not found", file=sys.stderr)
        return 2

    if args.peek is not None:
        peek_chunk(run_dir, args.peek)
        return 0

    summary = analyse_run(run_dir, battery_model=args.battery)
    print(
        f"[analyse] {summary.n_chunks} chunks, "
        f"{summary.total_samples:,} samples, "
        f"avg {summary.avg_current_mA:.4f} mA, "
        f"{summary.n_bursts} bursts → "
        f"{run_dir / 'report'}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

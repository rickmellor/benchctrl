"""Streamlit browser for sensor_profiler runs.

Run with::

    streamlit run applications/sensor_profiler/app.py

Sidebar lists every run under ``runs/`` with DUT name, start time,
duration, and completion status. Pick one and the main panel renders
five tabs: Overview / Bursts / Per-chunk / Raw chunks / Metadata.

Plots are Plotly (interactive zoom/pan) rendered from the same
analysis library that ``analyze.py`` uses, so what you see here
matches the static ``report/`` bundle. The app never writes to disk —
it loads ``data/`` chunks via :py:mod:`analyze` and displays them.

Decimation is applied to large traces (above ~50 K points per chunk)
so the browser stays responsive at 24-hour x ~4 kHz scale; use the
"Raw chunks" tab to drill into one chunk at full resolution.
"""
from __future__ import annotations

import datetime as _dt
from pathlib import Path

import numpy as np
import pandas as pd
import plotly.graph_objects as go
import streamlit as st
from plotly.subplots import make_subplots

import analyze
from benchctrl import Recording
from benchctrl.drivers.otii_arc import OtiiArcChannel

APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / "runs"

st.set_page_config(
    page_title="sensor_profiler",
    page_icon="⚡",
    layout="wide",
)


# ---------------------------------------------------------------------------
# Cached loaders — keep the app responsive when switching between runs
# ---------------------------------------------------------------------------


@st.cache_data(show_spinner=False)
def cached_metadata(run_dir_str: str) -> dict:
    return analyze.load_run_metadata(Path(run_dir_str))


@st.cache_data(show_spinner="Analysing run…")
def cached_summary(run_dir_str: str, battery_model: str) -> analyze.OverallSummary:
    return analyze.load_run_summary(
        Path(run_dir_str), battery_model=battery_model,
    )


@st.cache_data(show_spinner="Loading chunk…")
def cached_chunk_arrays(
    chunk_path_str: str,
) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
    rec = Recording.load(Path(chunk_path_str))
    ts_i, i_A = analyze.channel_arrays(rec, OtiiArcChannel.MAIN_CURRENT)
    ts_v, v_V = analyze.channel_arrays(rec, OtiiArcChannel.MAIN_VOLTAGE)
    return ts_i, i_A, ts_v, v_V


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _decimate(x: np.ndarray, y: np.ndarray, max_points: int = 30_000):
    if x.size <= max_points:
        return x, y
    stride = max(1, x.size // max_points)
    return x[::stride], y[::stride]


def _format_duration(seconds: float) -> str:
    if seconds is None or not np.isfinite(seconds):
        return "—"
    s = int(seconds)
    h, rem = divmod(s, 3600)
    m, s = divmod(rem, 60)
    return f"{h}h {m:02d}m {s:02d}s"


def _format_run_label(run: Path, meta: dict) -> str:
    dut = meta.get("dut", "?")
    started = meta.get("started_utc", "")[:16].replace("T", " ")
    status = meta.get("status", "?")
    return f"{dut} · {started} · {status}"


# ---------------------------------------------------------------------------
# Sidebar — run browser
# ---------------------------------------------------------------------------


def render_sidebar() -> tuple[Path | None, str]:
    st.sidebar.title("⚡ sensor_profiler")
    st.sidebar.caption("Power-profile runs")

    runs = analyze.list_runs(RUNS_DIR)
    if not runs:
        st.sidebar.info(
            "No runs yet.\n\n"
            "Run a capture with:\n\n"
            "`python capture.py --dut <name>`"
        )
        return None, analyze.DEFAULT_BATTERY

    rows = []
    for r in runs:
        m = cached_metadata(str(r))
        rows.append({
            "path": r,
            "label": _format_run_label(r, m),
            "started_utc": m.get("started_utc", ""),
            "dut": m.get("dut", ""),
            "status": m.get("status", ""),
            "chunks": m.get("chunks_written", 0),
            "duration_h": m.get("config", {}).get("total_hours", 0),
        })

    df = pd.DataFrame(rows)

    # Filter widgets
    duts = sorted(df["dut"].dropna().unique().tolist())
    statuses = sorted(df["status"].dropna().unique().tolist())
    dut_filter = st.sidebar.multiselect("Filter DUT", duts, default=duts)
    status_filter = st.sidebar.multiselect(
        "Filter status", statuses, default=statuses,
    )

    filtered = df[
        df["dut"].isin(dut_filter) & df["status"].isin(status_filter)
    ].sort_values("started_utc", ascending=False)

    st.sidebar.caption(f"{len(filtered)} of {len(df)} runs")
    selected_label = st.sidebar.radio(
        "Pick a run",
        options=filtered["label"].tolist(),
        index=0 if not filtered.empty else None,
        key="run_picker",
    )
    if selected_label is None:
        return None, analyze.DEFAULT_BATTERY

    selected_path = filtered.loc[
        filtered["label"] == selected_label, "path"
    ].iloc[0]

    st.sidebar.divider()
    st.sidebar.subheader("Battery model")
    battery = st.sidebar.selectbox(
        "for life projection",
        options=list(analyze.BATTERY_MODELS.keys()),
        index=list(analyze.BATTERY_MODELS.keys()).index(
            analyze.DEFAULT_BATTERY,
        ),
        key="battery_picker",
    )
    cap = analyze.BATTERY_MODELS[battery]
    st.sidebar.caption(
        f"{cap['capacity_mAh']:.0f} mAh @ {cap['voltage_V']:.1f} V "
        "(nominal)"
    )

    return Path(selected_path), battery


# ---------------------------------------------------------------------------
# Tab renderers
# ---------------------------------------------------------------------------


def render_overview(summary: analyze.OverallSummary) -> None:
    st.header(f"{summary.dut} — {summary.label or 'run'}")
    st.caption(f"`{summary.run_id}`")

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Avg current", f"{summary.avg_current_mA:.4f} mA")
    c2.metric("Sleep current",
              f"{summary.sleep_current_mA * 1000:.2f} µA")
    c3.metric("Total charge", f"{summary.total_charge_mAh:.4f} mAh")
    c4.metric("Duration", _format_duration(summary.total_duration_s))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Chunks", summary.n_chunks)
    c2.metric("Bursts", summary.n_bursts)
    c3.metric("Burst rate", f"{summary.burst_rate_per_hour:.2f}/h")
    c4.metric("Energy", f"{summary.total_energy_mWh:.4f} mWh")

    st.subheader("Battery-life projection")
    c1, c2, c3 = st.columns(3)
    c1.metric("Hours",
              f"{summary.projected_life_hours:.1f}",
              help=f"@ {summary.battery_model} nominal")
    c2.metric("Days", f"{summary.projected_life_days:.1f}")
    c3.metric("Years", f"{summary.projected_life_years:.2f}")

    if not summary.chunks:
        st.info("No chunks captured yet.")
        return

    st.subheader("Full-window current + voltage trace")
    st.caption(
        "Decimated for browser performance. Zoom / pan to drill in; "
        "use the *Raw chunks* tab for one-chunk full-resolution views."
    )

    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.04,
        subplot_titles=("Current (mA)", "Voltage (V)"),
    )

    cumulative_offset = 0.0
    for c in summary.chunks:
        ts_i, i_A, ts_v, v_V = cached_chunk_arrays(str(c.path))
        if ts_i.size:
            x, y = _decimate(
                (ts_i + cumulative_offset) / 3600.0, i_A * 1000.0,
            )
            fig.add_trace(
                go.Scattergl(
                    x=x, y=y, mode="lines",
                    line=dict(width=0.8, color="#1f77b4"),
                    name=f"chunk {c.chunk_idx}",
                    showlegend=False,
                ),
                row=1, col=1,
            )
        if ts_v.size:
            x, y = _decimate(
                (ts_v + cumulative_offset) / 3600.0, v_V,
            )
            fig.add_trace(
                go.Scattergl(
                    x=x, y=y, mode="lines",
                    line=dict(width=1.0, color="#2ca02c"),
                    showlegend=False,
                ),
                row=2, col=1,
            )
        cumulative_offset += c.duration_s

    fig.update_xaxes(title_text="Time since capture start (hours)",
                     row=2, col=1)
    fig.update_yaxes(title_text="Current (mA)", row=1, col=1)
    fig.update_yaxes(title_text="Voltage (V)", row=2, col=1)
    fig.update_layout(height=600, margin=dict(t=40, l=50, r=20, b=40))
    st.plotly_chart(fig, use_container_width=True)


def render_bursts(summary: analyze.OverallSummary) -> None:
    all_bursts = [b for c in summary.chunks for b in c.bursts]
    if not all_bursts:
        st.info("No TX bursts detected yet.")
        return

    burst_df = pd.DataFrame([
        {
            "chunk": b.chunk_idx,
            "wall_start_utc": b.wall_start.isoformat() if b.wall_start else "",
            "start_s_in_chunk": round(b.start_s, 4),
            "duration_ms": round(b.duration_ms, 3),
            "peak_mA": round(b.peak_mA, 3),
            "mean_mA": round(b.mean_mA, 3),
            "charge_uC": round(b.charge_uC, 3),
        }
        for b in all_bursts
    ])

    st.subheader(f"All {len(burst_df)} detected bursts")
    st.dataframe(
        burst_df, use_container_width=True, hide_index=True,
        column_config={
            "peak_mA": st.column_config.NumberColumn(
                "Peak (mA)", format="%.2f",
            ),
            "duration_ms": st.column_config.NumberColumn(
                "Duration (ms)", format="%.2f",
            ),
            "charge_uC": st.column_config.NumberColumn(
                "Charge (µC)", format="%.2f",
            ),
        },
    )

    st.subheader("Distributions")
    c1, c2 = st.columns(2)

    with c1:
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=burst_df["peak_mA"], nbinsx=40,
            marker_color="#ff7f0e",
        ))
        fig.update_layout(
            xaxis_title="Peak current (mA)",
            yaxis_title="Count",
            title=f"Burst amplitude (mean {burst_df['peak_mA'].mean():.2f} mA)",
            margin=dict(t=40, l=50, r=20, b=40), height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

    with c2:
        fig = go.Figure()
        fig.add_trace(go.Histogram(
            x=burst_df["duration_ms"], nbinsx=40,
            marker_color="#9467bd",
        ))
        fig.update_layout(
            xaxis_title="Duration (ms)", yaxis_title="Count",
            title=(
                f"Burst duration (mean "
                f"{burst_df['duration_ms'].mean():.2f} ms)"
            ),
            margin=dict(t=40, l=50, r=20, b=40), height=350,
        )
        st.plotly_chart(fig, use_container_width=True)

    # Inter-arrival times — compute on absolute timeline
    cum_offset = 0.0
    abs_starts: list[float] = []
    for c in summary.chunks:
        for b in c.bursts:
            abs_starts.append(b.start_s + cum_offset)
        cum_offset += c.duration_s
    inter_arrival = np.diff(np.array(abs_starts)) if len(abs_starts) > 1 \
        else np.array([])
    if inter_arrival.size:
        st.subheader("Inter-arrival times (log scale)")
        positive = inter_arrival[inter_arrival > 0]
        fig = go.Figure()
        if positive.size:
            fig.add_trace(go.Histogram(
                x=positive,
                xbins=dict(
                    start=np.log10(max(positive.min(), 1e-3)),
                    end=np.log10(positive.max()),
                ),
                nbinsx=40,
                marker_color="#1f77b4",
            ))
        fig.update_xaxes(type="log", title_text="Inter-arrival (s, log)")
        fig.update_layout(
            yaxis_title="Count",
            title=(
                f"median {np.median(inter_arrival):.2f} s, "
                f"n={inter_arrival.size}"
            ),
            margin=dict(t=40, l=50, r=20, b=40), height=350,
        )
        st.plotly_chart(fig, use_container_width=True)


def render_per_chunk(summary: analyze.OverallSummary) -> None:
    if not summary.chunks:
        st.info("No chunks loaded.")
        return

    df = pd.DataFrame([
        {
            "chunk": c.chunk_idx,
            "start_utc": c.start_time.isoformat() if c.start_time else "",
            "duration_s": round(c.duration_s, 1),
            "samples": c.sample_count,
            "avg_I_mA": round(c.avg_current_mA, 5),
            "peak_I_mA": round(c.peak_current_mA, 3),
            "sleep_I_uA": round(c.estimated_sleep_current_mA * 1000, 3),
            "median_I_mA": round(c.median_current_mA, 5),
            "avg_V": round(c.avg_voltage_V, 4),
            "min_V": round(c.min_voltage_V, 4),
            "max_V": round(c.max_voltage_V, 4),
            "charge_mAh": round(c.charge_mAh, 5),
            "energy_mWh": round(c.energy_mWh, 5),
            "bursts": len(c.bursts),
        }
        for c in summary.chunks
    ])

    st.subheader("Per-chunk table")
    st.dataframe(df, use_container_width=True, hide_index=True)

    st.subheader("Trend lines")
    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=True, vertical_spacing=0.04,
        subplot_titles=(
            "Avg current (mA)",
            "Sleep current (µA)",
            "Burst count per chunk",
            "Avg voltage (V)",
        ),
    )
    fig.add_trace(go.Scatter(
        x=df["chunk"], y=df["avg_I_mA"],
        mode="lines+markers", line=dict(color="#1f77b4"),
        showlegend=False,
    ), row=1, col=1)
    fig.add_trace(go.Scatter(
        x=df["chunk"], y=df["sleep_I_uA"],
        mode="lines+markers", line=dict(color="#2ca02c"),
        showlegend=False,
    ), row=2, col=1)
    fig.add_trace(go.Scatter(
        x=df["chunk"], y=df["bursts"],
        mode="lines+markers", line=dict(color="#ff7f0e"),
        showlegend=False,
    ), row=3, col=1)
    fig.add_trace(go.Scatter(
        x=df["chunk"], y=df["avg_V"],
        mode="lines+markers", line=dict(color="#d62728"),
        showlegend=False,
    ), row=4, col=1)
    fig.update_xaxes(title_text="Chunk index", row=4, col=1)
    fig.update_layout(height=700, margin=dict(t=40, l=50, r=20, b=40))
    st.plotly_chart(fig, use_container_width=True)


def render_raw_chunks(summary: analyze.OverallSummary) -> None:
    if not summary.chunks:
        st.info("No chunks loaded.")
        return

    labels = [
        f"chunk {c.chunk_idx}: {c.path.name}"
        for c in summary.chunks
    ]
    selected = st.selectbox(
        "Chunk to inspect at full resolution", labels,
    )
    idx = labels.index(selected)
    c = summary.chunks[idx]
    ts_i, i_A, ts_v, v_V = cached_chunk_arrays(str(c.path))

    c1, c2, c3, c4 = st.columns(4)
    c1.metric("Samples", f"{c.sample_count:,}")
    c2.metric("Duration", f"{c.duration_s:.1f} s")
    c3.metric("Avg I", f"{c.avg_current_mA:.4f} mA")
    c4.metric("Bursts", len(c.bursts))

    st.caption("Full-resolution chunk view (no decimation).")
    fig = make_subplots(
        rows=2, cols=1, shared_xaxes=True,
        row_heights=[0.65, 0.35],
        vertical_spacing=0.04,
        subplot_titles=("Current (mA)", "Voltage (V)"),
    )
    if ts_i.size:
        fig.add_trace(go.Scattergl(
            x=ts_i, y=i_A * 1000.0,
            mode="lines", line=dict(width=0.6, color="#1f77b4"),
            showlegend=False,
        ), row=1, col=1)
    if ts_v.size:
        fig.add_trace(go.Scattergl(
            x=ts_v, y=v_V,
            mode="lines", line=dict(width=0.8, color="#2ca02c"),
            showlegend=False,
        ), row=2, col=1)
    fig.update_xaxes(title_text="Time within chunk (s)", row=2, col=1)
    fig.update_yaxes(title_text="Current (mA)", row=1, col=1)
    fig.update_yaxes(title_text="Voltage (V)", row=2, col=1)
    fig.update_layout(height=600, margin=dict(t=40, l=50, r=20, b=40))
    st.plotly_chart(fig, use_container_width=True)

    if c.bursts:
        st.subheader(f"{len(c.bursts)} bursts in this chunk")
        st.dataframe(
            pd.DataFrame([
                {
                    "start_s": round(b.start_s, 4),
                    "duration_ms": round(b.duration_ms, 3),
                    "peak_mA": round(b.peak_mA, 3),
                    "mean_mA": round(b.mean_mA, 3),
                    "charge_uC": round(b.charge_uC, 3),
                }
                for b in c.bursts
            ]),
            use_container_width=True, hide_index=True,
        )


def render_metadata(summary: analyze.OverallSummary) -> None:
    md = summary.metadata
    st.subheader("Run metadata")
    if not md:
        st.info("metadata.json missing or empty.")
        return

    started = md.get("started_utc", "")
    finished = md.get("finished_utc", "")
    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**DUT**: `{md.get('dut', '?')}`")
    c2.markdown(f"**Label**: `{md.get('label', '?') or '—'}`")
    c3.markdown(f"**Status**: `{md.get('status', '?')}`")

    c1, c2, c3 = st.columns(3)
    c1.markdown(f"**Started**: {started}")
    c2.markdown(f"**Finished**: {finished or '—'}")
    c3.markdown(f"**Chunks**: {md.get('chunks_written', 0)}")

    config = md.get("config", {})
    if config:
        st.subheader("Capture config")
        st.json(config)

    arc = md.get("arc_info", {})
    if arc:
        st.subheader("Arc info")
        st.json(arc)

    notes = md.get("notes", "")
    if notes:
        st.subheader("Notes")
        st.text(notes)

    st.subheader("Chunk files")
    chunks = md.get("chunk_files", [])
    if chunks:
        for ch in chunks:
            st.markdown(f"- `{ch}`")
    else:
        st.caption("none yet")


# ---------------------------------------------------------------------------
# Main app
# ---------------------------------------------------------------------------


def main() -> None:
    run_path, battery_model = render_sidebar()

    if run_path is None:
        st.title("sensor_profiler")
        st.write(
            "Start a capture with `python capture.py --dut <name>`, "
            "then refresh this page."
        )
        return

    summary = cached_summary(str(run_path), battery_model)

    tabs = st.tabs([
        "Overview", "Bursts", "Per-chunk", "Raw chunks", "Metadata",
    ])
    with tabs[0]:
        render_overview(summary)
    with tabs[1]:
        render_bursts(summary)
    with tabs[2]:
        render_per_chunk(summary)
    with tabs[3]:
        render_raw_chunks(summary)
    with tabs[4]:
        render_metadata(summary)


if __name__ == "__main__":
    main()

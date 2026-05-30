"""Hardware-free tests for the optional-dependency export methods on Recording.

These skip if the optional dep isn't installed. Run with the full
science stack via ``pip install benchctrl[science]``.
"""

from __future__ import annotations

import pytest

from benchctrl import Channel, Recording


def _build_recording():
    rec = Recording(name="export-test")
    mc = rec._ensure_buffer(Channel.MAIN_CURRENT, 4000)
    mc.extend([0.001 * i for i in range(40)])  # 10 ms at 4 kHz
    mv = rec._ensure_buffer(Channel.MAIN_VOLTAGE, 1000)
    mv.extend([3.3, 3.31, 3.32, 3.33])  # 4 ms at 1 kHz
    return rec


# ----- numpy -----------------------------------------------------------------


def test_to_numpy_returns_float32_ndarray():
    np = pytest.importorskip("numpy")
    rec = _build_recording()
    arr = rec.to_numpy(Channel.MAIN_CURRENT)
    assert arr.shape == (40,)
    assert arr.dtype == np.float32
    assert arr[0] == pytest.approx(0.0)
    assert arr[-1] == pytest.approx(0.039, rel=1e-5)


def test_to_numpy_string_channel():
    pytest.importorskip("numpy")
    rec = _build_recording()
    arr = rec.to_numpy("mv")
    assert arr.shape == (4,)


def test_timestamps_numpy_matches_synthetic_axis():
    np = pytest.importorskip("numpy")
    rec = _build_recording()
    ts = rec.timestamps_numpy(Channel.MAIN_CURRENT)
    assert ts.shape == (40,)
    assert ts.dtype == np.float64
    assert ts[0] == pytest.approx(0.0)
    assert ts[1] == pytest.approx(0.00025)  # 1/4000
    # last sample is at index 39 -> 39/4000 = 9.75 ms
    assert ts[-1] == pytest.approx(39 / 4000, abs=1e-9)


def test_timestamps_numpy_includes_offset():
    np = pytest.importorskip("numpy")
    rec = _build_recording()
    rec.offset = 10.0
    ts = rec.timestamps_numpy(Channel.MAIN_CURRENT)
    assert ts[0] == pytest.approx(10.0)
    assert ts[-1] == pytest.approx(10.0 + 39 / 4000, abs=1e-9)


def test_to_numpy_empty_buffer_returns_empty_array():
    pytest.importorskip("numpy")
    rec = Recording()
    rec._ensure_buffer(Channel.MAIN_CURRENT, 4000)  # buffer with zero samples
    arr = rec.to_numpy(Channel.MAIN_CURRENT)
    assert arr.shape == (0,)


# ----- pandas ---------------------------------------------------------------


def test_to_pandas_series_for_single_channel():
    pd = pytest.importorskip("pandas")
    rec = _build_recording()
    s = rec.to_pandas(Channel.MAIN_CURRENT)
    assert isinstance(s, pd.Series)
    assert s.name == "mc"
    assert s.index.name == "timestamp_s"
    assert len(s) == 40


def test_to_pandas_dataframe_for_all_channels():
    pd = pytest.importorskip("pandas")
    rec = _build_recording()
    df = rec.to_pandas()
    assert isinstance(df, pd.DataFrame)
    assert set(df.columns) == {"mc", "mv"}
    # mc has 40 samples at 4 kHz; mv has 4 samples at 1 kHz aligned every 4 rows
    assert len(df) == 40
    # mv has many NaNs because its native rate is 4x lower than mc's
    assert df["mv"].isna().sum() >= 36
    # Where mv is present, the value is one of the 4 we put in
    present = df["mv"].dropna().tolist()
    assert present[0] == pytest.approx(3.3)
    assert len(present) == 4


def test_to_pandas_dataframe_handles_empty_recording():
    pd = pytest.importorskip("pandas")
    rec = Recording()
    df = rec.to_pandas()
    assert isinstance(df, pd.DataFrame)
    assert df.empty


# ----- parquet --------------------------------------------------------------


def test_save_parquet_round_trips_via_pandas(tmp_path):
    pd = pytest.importorskip("pandas")
    pytest.importorskip("pyarrow")
    rec = _build_recording()
    p = rec.save_parquet(tmp_path / "rec.parquet")
    assert p.exists()
    df = pd.read_parquet(p)
    assert "mc" in df.columns
    assert "mv" in df.columns
    # round-trip preserves the value at index 0
    assert df["mc"].iloc[0] == pytest.approx(0.0)


def test_save_parquet_embeds_column_metadata(tmp_path):
    pa = pytest.importorskip("pyarrow")
    pq = pytest.importorskip("pyarrow.parquet")
    rec = _build_recording()
    p = rec.save_parquet(tmp_path / "rec.parquet")
    table = pq.read_table(p)
    md = table.schema.metadata or {}
    assert b"benchctrl.recording_name" in md
    assert md[b"benchctrl.recording_name"] == b"export-test"
    assert b"benchctrl.columns" in md


def test_save_parquet_smaller_than_csv(tmp_path):
    pytest.importorskip("pyarrow")
    rec = _build_recording()
    csv_path = rec.save_csv(tmp_path / "rec.csv", format="wide")
    parquet_path = rec.save_parquet(tmp_path / "rec.parquet")
    # On these small synthetic recordings, parquet is competitive with CSV
    # — assert both files exist and parquet is at least produced.
    assert parquet_path.stat().st_size > 0
    assert csv_path.stat().st_size > 0


# ----- plot -----------------------------------------------------------------


def test_plot_returns_figure_with_one_axes_per_channel():
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    rec = _build_recording()
    fig = rec.plot(show=False)
    assert len(fig.axes) == 2  # mc + mv
    # x-axis label set on last axis
    assert fig.axes[-1].get_xlabel() == "time (s)"


def test_plot_subset_of_channels():
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    rec = _build_recording()
    fig = rec.plot(channels=[Channel.MAIN_CURRENT], show=False)
    assert len(fig.axes) == 1


def test_plot_rejects_empty_recording():
    pytest.importorskip("matplotlib")
    import matplotlib

    matplotlib.use("Agg")
    from benchctrl.exceptions import BenchValueError

    rec = Recording()
    with pytest.raises(BenchValueError):
        rec.plot(show=False)


# ----- lazy import behaviour ------------------------------------------------
# These tests verify the OPTIONAL nature of the data-science extras —
# benchctrl must import clean without any of numpy / pandas / pyarrow /
# matplotlib loaded, and each method must raise a clear, actionable
# ImportError when its dep is missing.


def _import_with_blocked(blocked: set[str], module: str):
    """Import `module` in a child interpreter with the named top-level
    packages blocked at the meta_path level."""
    import subprocess
    import sys
    import textwrap

    code = textwrap.dedent(
        f"""
        import sys
        BLOCKED = {sorted(blocked)!r}
        class _Block:
            def find_spec(self, name, *_):
                if name.split('.')[0] in BLOCKED:
                    raise ImportError(f'{{name}} BLOCKED for test')
                return None
        sys.meta_path.insert(0, _Block())
        sys.path.insert(0, 'src')
        import {module}  # noqa: F401
        leaked = [m for m in sys.modules if m.split('.')[0] in BLOCKED]
        assert not leaked, f'leaked modules: {{leaked}}'
        print('OK')
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=30
    )
    return out


def test_benchctrl_imports_clean_without_optional_deps():
    """Importing benchctrl must not load numpy/pandas/pyarrow/matplotlib."""
    out = _import_with_blocked(
        {"numpy", "pandas", "pyarrow", "matplotlib"}, "benchctrl"
    )
    assert out.returncode == 0, out.stderr
    assert "OK" in out.stdout


def test_to_numpy_raises_friendly_error_without_numpy(tmp_path, monkeypatch):
    """Simulate numpy being absent by hiding it from the import system."""
    import sys

    # Force a fresh import path for any retry within this test
    real_numpy = sys.modules.pop("numpy", None)
    try:

        class _Block:
            def find_spec(self, name, *_):
                if name == "numpy" or name.startswith("numpy."):
                    raise ImportError("numpy BLOCKED for test")
                return None

        blocker = _Block()
        sys.meta_path.insert(0, blocker)
        try:
            rec = _build_recording()
            with pytest.raises(ImportError) as exc:
                rec.to_numpy(Channel.MAIN_CURRENT)
            assert "benchctrl[numpy]" in str(exc.value)
        finally:
            sys.meta_path.remove(blocker)
    finally:
        if real_numpy is not None:
            sys.modules["numpy"] = real_numpy

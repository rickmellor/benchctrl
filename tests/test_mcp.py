"""Hardware-free tests for the MCP server tool functions.

These exercise the tool functions directly (FastMCP preserves them as
callable Python functions). Hardware-required tests live in
``test_mcp_hw.py``.
"""

from __future__ import annotations

import pytest


def test_mcp_module_importable():
    from benchctrl import mcp as m

    assert m.mcp.name == "benchctrl"


def test_list_channels_returns_all_channels():
    from benchctrl.mcp import list_channels

    result = list_channels()
    codes = [c["code"] for c in result["channels"]]
    for required in ("mc", "mv", "mp", "ac", "av", "ap", "vb", "vj", "tp", "rx", "i1", "i2"):
        assert required in codes, f"missing channel {required}"


def test_list_channels_includes_metadata():
    from benchctrl.mcp import list_channels

    result = list_channels()
    mc = next(c for c in result["channels"] if c["code"] == "mc")
    assert mc["wire_id"] == 0x00
    assert mc["unit"] == "A"
    assert mc["sample_rate_hz"] == 4000
    assert mc["subtype"] == 4
    assert mc["toggleable"] is True


def test_list_channels_temperature_is_not_toggleable():
    from benchctrl.mcp import list_channels

    result = list_channels()
    tp = next(c for c in result["channels"] if c["code"] == "tp")
    assert tp["toggleable"] is False


def test_tools_have_docstrings():
    """Every public tool function must have a docstring — that's what the
    LLM sees as the tool description."""
    from benchctrl import mcp as m

    expected_tools = [
        "info", "state", "versions", "list_channels",
        "set_voltage", "set_current_limit", "set_exp_voltage", "set_exp_5v",
        "set_range", "set_4wire", "set_current_limit_enabled",
        "set_uart", "set_gpo", "set_power_regulation",
        "enable_output", "disable_output",
        "live", "take_snapshot", "record",
        "write_uart_tx", "get_gpi",
        "reconnect", "disconnect",
        # v0.4.0 sync — recording I/O tools (work on saved files):
        "plot_recording", "recording_summary", "export_recording",
    ]
    for name in expected_tools:
        fn = getattr(m, name, None)
        assert fn is not None, f"missing tool: {name}"
        assert callable(fn), f"{name} not callable"
        assert fn.__doc__ and fn.__doc__.strip(), f"{name} missing docstring"


# ----- v0.4.0 sync — tools that operate on saved recordings -----------------
#
# These work on saved files; no SMU connection required, so they're
# hardware-free.


def _make_saved_recording(tmp_path):
    """Build + save a small synthetic recording. Returns the .opensmu path."""
    from benchctrl import Recording
    from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel

    rec = Recording(name="hw-free-sync-test")
    mc = rec._ensure_buffer(Channel.MAIN_CURRENT, 4000)
    mc.extend([0.001 * i for i in range(40)])
    mv = rec._ensure_buffer(Channel.MAIN_VOLTAGE, 1000)
    mv.extend([3.3, 3.31, 3.32, 3.33])
    p = tmp_path / "rec.opensmu"
    rec.save(p)
    return p


def test_recording_summary_returns_stats(tmp_path):
    from benchctrl.mcp import recording_summary

    p = _make_saved_recording(tmp_path)
    r = recording_summary(str(p))
    assert r["name"] == "hw-free-sync-test"
    assert set(r["channels"].keys()) == {"mc", "mv"}
    assert r["channels"]["mc"]["samples"] == 40
    assert r["channels"]["mv"]["samples"] == 4
    # charge is populated for current channels
    assert r["channels"]["mc"]["charge_C"] is not None
    # not for voltage channels
    assert r["channels"]["mv"]["charge_C"] is None


def test_export_recording_to_csv(tmp_path):
    from benchctrl.mcp import export_recording

    p = _make_saved_recording(tmp_path)
    out = tmp_path / "rec.csv"
    r = export_recording(str(p), str(out))
    assert r["format"] == "csv"
    assert out.exists()
    assert out.read_text().startswith("timestamp_s,")


def test_export_recording_to_parquet(tmp_path):
    pytest.importorskip("pyarrow")
    from benchctrl.mcp import export_recording

    p = _make_saved_recording(tmp_path)
    out = tmp_path / "rec.parquet"
    r = export_recording(str(p), str(out))
    assert r["format"] == "parquet"
    assert out.exists()


def test_export_recording_unknown_extension_normalises_to_benchctrl(tmp_path):
    from benchctrl.mcp import export_recording

    p = _make_saved_recording(tmp_path)
    out = tmp_path / "rec.bin"
    r = export_recording(str(p), str(out))
    assert r["output"].endswith(".opensmu")


def test_plot_recording_writes_png(tmp_path):
    pytest.importorskip("matplotlib")
    from benchctrl.mcp import plot_recording

    p = _make_saved_recording(tmp_path)
    out = tmp_path / "rec.png"
    r = plot_recording(str(p), str(out))
    assert out.exists()
    assert out.stat().st_size > 1000  # some real PNG data
    assert set(r["channels"]) == {"mc", "mv"}


def test_plot_recording_subset_of_channels(tmp_path):
    pytest.importorskip("matplotlib")
    from benchctrl.mcp import plot_recording

    p = _make_saved_recording(tmp_path)
    out = tmp_path / "rec.png"
    r = plot_recording(str(p), str(out), channels=["mc"])
    assert out.exists()


def test_smu_state_snapshot_shape():
    """The internal state-snapshot helper produces a well-formed dict for any
    SMU. Use a transport-less SMU stub to avoid hardware."""
    from benchctrl.drivers.otii_arc.device import OtiiArc as SMU
    from benchctrl.mcp import _smu_state
    from benchctrl.drivers.otii_arc.transport import Transport

    smu = SMU(Transport("__dummy__"))  # never opened
    snap = _smu_state(smu)
    for required in (
        "is_connected", "port", "voltage_V", "current_limit_A",
        "output_enabled", "range", "enabled_channels", "gpo",
    ):
        assert required in snap
    # nothing has been set yet
    assert snap["voltage_V"] is None
    assert snap["enabled_channels"] == []


# ---------------------------------------------------------------------------
# DL3031A MCP layer — arg coercion & connection-state checks
# ---------------------------------------------------------------------------


class _FakeDL:
    """Stub that mimics the RigolDL3031A surface the MCP tools touch."""
    def __init__(self):
        self.program_list_calls = []
        self.set_input_calls = []
        self._mode = "CC"
        self._func_mode = "FIX"
        self._closed = False

    def program_list(self, *, steps, mode, count, range_value,
                     slew_A_per_us, end_behavior, trigger_source):
        # Record what the MCP layer coerced our args into
        self.program_list_calls.append({
            "steps": list(steps), "mode": mode, "count": count,
            "range_value": range_value, "slew_A_per_us": slew_A_per_us,
            "end_behavior": end_behavior, "trigger_source": trigger_source,
        })

    def get_function_mode(self): return self._func_mode
    def get_mode(self): return self._mode
    def get_input(self): return False
    def set_input(self, on): self.set_input_calls.append(on)
    def close(self): self._closed = True


def test_dl3031a_program_list_coerces_int_and_float_step_pairs():
    from benchctrl import mcp as m
    fake = _FakeDL()
    m._dl3031a = fake
    try:
        result = m.dl3031a_program_list(
            steps=[[1, 1], [0.030, 0.05], (1e-4, 1.0)],  # mixed int / float / tuple
            mode="CC", count=2, range_value=6.0,
            slew_A_per_us=0.5, end_behavior="LAST",
            trigger_source="BUS",
        )
        assert fake.program_list_calls, "program_list was not called"
        steps = fake.program_list_calls[0]["steps"]
        # All steps coerced to (float, float) tuples
        for level, width in steps:
            assert isinstance(level, float)
            assert isinstance(width, float)
        assert steps[0] == pytest.approx((1.0, 1.0))
        assert steps[1] == pytest.approx((0.030, 0.05))
        assert steps[2] == pytest.approx((0.0001, 1.0))
        assert result["function_mode"] == "FIX"
        assert result["n_steps"] == 3
        assert result["count"] == 2
        assert result["trigger_source"] == "BUS"
    finally:
        m._dl3031a = None


def test_dl3031a_program_list_rejects_malformed_steps():
    from benchctrl import mcp as m
    fake = _FakeDL()
    m._dl3031a = fake
    try:
        # Each step must be (level, width); a bare scalar is malformed
        with pytest.raises((TypeError, IndexError, ValueError)):
            m.dl3031a_program_list(steps=[0.030, 0.05], mode="CC")
        # Non-numeric also rejected
        with pytest.raises((TypeError, ValueError)):
            m.dl3031a_program_list(steps=[["a", "b"], [0.0, 0.1]], mode="CC")
    finally:
        m._dl3031a = None


def test_dl3031a_tools_raise_driver_connection_error_when_not_open():
    """L2 fix: _get_dl3031a now raises RigolDLConnectionError, not
    bare RuntimeError, when the singleton is None."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dl3031a.driver import RigolDLConnectionError
    m._dl3031a = None
    with pytest.raises(RigolDLConnectionError):
        m.dl3031a_info()


def test_qr10x_tools_raise_driver_connection_error_when_not_open():
    """Same as above for QR10x."""
    from benchctrl import mcp as m
    from benchctrl.drivers.eastwood_qr10x.driver import QR10xConnectionError
    m._qr10x = None
    with pytest.raises(QR10xConnectionError):
        m.qr10x_info()


def test_dl3031a_close_surfaces_input_off_failure():
    """H5 fix: dl3031a_close returns input_off_failed when set_input(False)
    raises. Previously, errors were silently swallowed and the operator
    got {"closed": True} even though the load could still be sinking."""
    from benchctrl import mcp as m
    fake = _FakeDL()

    def boom(_on):
        raise RuntimeError("simulated set_input failure")
    fake.set_input = boom  # type: ignore[method-assign]

    m._dl3031a = fake
    result = m.dl3031a_close()
    assert result["closed"] is True
    assert "input_off_failed" in result
    assert "simulated set_input failure" in result["input_off_failed"]
    assert "warning" in result

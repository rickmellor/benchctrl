"""Hardware-required tests for the MCP server tool functions."""

from __future__ import annotations

import time

import pytest

pytestmark = pytest.mark.hardware


@pytest.fixture(autouse=True)
def _disconnect_after_each_test():
    """Each MCP test owns the singleton SMU; clean up at the end."""
    yield
    from benchctrl import mcp as m

    m._close_smu()


def _ensure_hardware_available():
    """Skip if no Arc is connected."""
    from benchctrl.drivers.otii_arc.device import OtiiArc as SMU

    if not SMU.discover():
        pytest.skip("no Arc Pro found")


# ---- information -----------------------------------------------------------


def test_info_returns_device_metadata():
    _ensure_hardware_available()
    from benchctrl.mcp import info

    r = info()
    assert r["is_connected"] is True
    assert r["name"] == "Arc"
    assert "." in (r["fw_version"] or "")


def test_state_returns_full_snapshot():
    _ensure_hardware_available()
    from benchctrl.mcp import state

    r = state()
    for k in ("voltage_V", "current_limit_A", "output_enabled",
              "enabled_channels", "range", "gpo"):
        assert k in r


def test_versions_returns_strings():
    _ensure_hardware_available()
    from benchctrl.mcp import versions

    r = versions()
    assert r["name"] == "Arc"
    assert isinstance(r["fw_version"], str)
    assert isinstance(r["device_id"], str)
    assert len(r["device_id"]) == 32


# ---- setpoint round-trips --------------------------------------------------


def test_set_voltage_updates_state():
    _ensure_hardware_available()
    from benchctrl.mcp import set_voltage, state

    r = set_voltage(3.0)
    assert r["set_voltage_V"] == 3.0
    assert state()["voltage_V"] == 3.0
    set_voltage(3.25)  # restore


def test_set_current_limit_updates_state():
    _ensure_hardware_available()
    from benchctrl.mcp import set_current_limit, state

    set_current_limit(1.0)
    assert state()["current_limit_A"] == 1.0
    set_current_limit(2.5)


def test_set_range_round_trip():
    _ensure_hardware_available()
    from benchctrl.mcp import set_range, state

    set_range("low")
    assert state()["range"] == "low"
    set_range("high")
    assert state()["range"] == "high"
    set_range("low")


def test_set_4wire_toggle():
    _ensure_hardware_available()
    from benchctrl.mcp import set_4wire, state

    set_4wire(True)
    assert state()["four_wire_enabled"] is True
    set_4wire(False)
    assert state()["four_wire_enabled"] is False


# ---- safety: enable_output refuses without confirmation --------------------


def test_enable_output_refuses_without_current_limit():
    _ensure_hardware_available()
    from benchctrl.mcp import disconnect, enable_output

    # Fresh connection: no current_limit cached
    disconnect()
    r = enable_output(confirm_dut_attached=True)
    assert "error" in r
    assert "current_limit" in r["error"].lower()


def test_enable_output_refuses_without_confirmation():
    _ensure_hardware_available()
    from benchctrl.mcp import enable_output, set_current_limit, set_voltage

    set_voltage(3.0)
    set_current_limit(1.0)
    r = enable_output(confirm_dut_attached=False)
    assert "error" in r
    assert "confirm_dut_attached" in r["error"]
    # guidance must mention the configured voltage and limit
    assert "3.0" in r["guidance"] or "3.0 V" in r["guidance"]


def test_enable_output_accepts_when_all_guards_pass():
    _ensure_hardware_available()
    from benchctrl.mcp import (
        disable_output,
        enable_output,
        set_current_limit,
        set_voltage,
    )

    set_voltage(3.25)
    set_current_limit(2.0)
    r = enable_output(confirm_dut_attached=True)
    assert r.get("output_enabled") is True
    disable_output()


# ---- measurement -----------------------------------------------------------


def test_take_snapshot_returns_channel_values():
    _ensure_hardware_available()
    from benchctrl.mcp import take_snapshot

    r = take_snapshot(duration_s=0.5)
    # baseline streaming returns ~12 channels in a 0.5 s window
    assert len(r["channels"]) > 5
    # main voltage should be present
    assert "mv" in r["channels"]
    assert "value" in r["channels"]["mv"]


def test_live_returns_single_value():
    _ensure_hardware_available()
    from benchctrl.mcp import live

    r = live("mv", timeout_s=2.0)
    assert r["channel"] == "mv"
    assert r["unit"] == "V"
    assert isinstance(r["value"], float)


def test_record_returns_statistics():
    _ensure_hardware_available()
    from benchctrl.mcp import record

    r = record(seconds=1.0, channels=["mc", "mv"])
    assert "mc" in r["channels"]
    assert "mv" in r["channels"]
    assert r["channels"]["mc"]["samples"] > 1000  # 4 kHz native
    assert r["channels"]["mv"]["samples"] > 500   # 1 kHz native
    assert r["channels"]["mc"]["charge_C"] is not None
    assert r["channels"]["mv"]["charge_C"] is None  # voltage channel has no charge


def test_record_saves_csv_when_requested(tmp_path):
    _ensure_hardware_available()
    from benchctrl.mcp import record

    out = tmp_path / "run.csv"
    r = record(seconds=0.6, channels=["mv"], save_path=str(out))
    assert r["saved_to"] == str(out)
    assert out.exists()
    assert out.read_text().startswith("timestamp_s,")


def test_record_saves_native_when_extension_unknown(tmp_path):
    _ensure_hardware_available()
    from benchctrl.mcp import record

    out = tmp_path / "run.bin"
    r = record(seconds=0.6, channels=["mv"], save_path=str(out))
    assert r["saved_to"].endswith(".opensmu")


def test_record_saves_parquet_extension(tmp_path):
    """v0.4.0 sync — `record` now handles `.parquet` save_path natively."""
    _ensure_hardware_available()
    pytest.importorskip("pyarrow")
    from benchctrl.mcp import record

    out = tmp_path / "run.parquet"
    r = record(seconds=0.6, channels=["mv"], save_path=str(out))
    assert r["saved_to"] == str(out)
    assert out.exists()


def test_record_plot_png_produces_image(tmp_path):
    """v0.4.0 sync — `record` can also render a matplotlib PNG in one call."""
    _ensure_hardware_available()
    pytest.importorskip("matplotlib")
    from benchctrl.mcp import record

    out = tmp_path / "run.png"
    r = record(seconds=0.6, channels=["mc", "mv"], plot_png=str(out))
    assert r["plotted_to"] == str(out)
    assert out.exists()
    assert out.stat().st_size > 1000


def test_record_combined_save_and_plot(tmp_path):
    """One call: capture, save as parquet, render PNG."""
    _ensure_hardware_available()
    pytest.importorskip("pyarrow")
    pytest.importorskip("matplotlib")
    from benchctrl.mcp import record

    parquet_out = tmp_path / "run.parquet"
    png_out = tmp_path / "run.png"
    r = record(
        seconds=0.6,
        channels=["mc", "mv"],
        save_path=str(parquet_out),
        plot_png=str(png_out),
    )
    assert r["saved_to"] == str(parquet_out)
    assert r["plotted_to"] == str(png_out)
    assert parquet_out.exists()
    assert png_out.exists()


# ---- GPIO ------------------------------------------------------------------


def test_set_gpo_pin_3_works_via_mcp():
    _ensure_hardware_available()
    from benchctrl.mcp import set_gpo, state

    set_gpo(3, True)
    set_gpo(3, False)
    # state.gpo should reflect both writes
    assert 3 in state()["gpo"]


# ---- connection management -------------------------------------------------


def test_disconnect_then_reconnect():
    _ensure_hardware_available()
    from benchctrl.mcp import disconnect, reconnect, state

    state()  # ensure connection
    disconnect()
    r = reconnect()
    assert r["is_connected"] is True

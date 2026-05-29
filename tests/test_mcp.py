"""Hardware-free tests for the MCP server tool functions.

These exercise the tool functions directly (FastMCP preserves them as
callable Python functions). Hardware-required tests live in
``test_mcp_hw.py``.
"""

from __future__ import annotations

import pytest


def test_mcp_module_importable():
    from opensmu import mcp as m

    assert m.mcp.name == "opensmu"


def test_list_channels_returns_all_channels():
    from opensmu.mcp import list_channels

    result = list_channels()
    codes = [c["code"] for c in result["channels"]]
    for required in ("mc", "mv", "mp", "ac", "av", "ap", "vb", "vj", "tp", "rx", "i1", "i2"):
        assert required in codes, f"missing channel {required}"


def test_list_channels_includes_metadata():
    from opensmu.mcp import list_channels

    result = list_channels()
    mc = next(c for c in result["channels"] if c["code"] == "mc")
    assert mc["wire_id"] == 0x00
    assert mc["unit"] == "A"
    assert mc["sample_rate_hz"] == 4000
    assert mc["subtype"] == 4
    assert mc["toggleable"] is True


def test_list_channels_temperature_is_not_toggleable():
    from opensmu.mcp import list_channels

    result = list_channels()
    tp = next(c for c in result["channels"] if c["code"] == "tp")
    assert tp["toggleable"] is False


def test_tools_have_docstrings():
    """Every public tool function must have a docstring — that's what the
    LLM sees as the tool description."""
    from opensmu import mcp as m

    expected_tools = [
        "info", "state", "versions", "list_channels",
        "set_voltage", "set_current_limit", "set_exp_voltage", "set_exp_5v",
        "set_range", "set_4wire", "set_current_limit_enabled",
        "set_uart", "set_gpo", "set_power_regulation",
        "enable_output", "disable_output",
        "live", "take_snapshot", "record",
        "write_uart_tx", "get_gpi",
        "reconnect", "disconnect",
    ]
    for name in expected_tools:
        fn = getattr(m, name, None)
        assert fn is not None, f"missing tool: {name}"
        assert callable(fn), f"{name} not callable"
        assert fn.__doc__ and fn.__doc__.strip(), f"{name} missing docstring"


def test_smu_state_snapshot_shape():
    """The internal state-snapshot helper produces a well-formed dict for any
    SMU. Use a transport-less SMU stub to avoid hardware."""
    from opensmu.device import SMU
    from opensmu.mcp import _smu_state
    from opensmu.transport import Transport

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

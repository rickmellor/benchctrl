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
    from benchctrl.drivers.rigol_dl3031a import mcp_tools as dl_tools
    fake = _FakeDL()
    dl_tools._dl3031a = fake
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
        dl_tools._dl3031a = None


def test_dl3031a_program_list_rejects_malformed_steps():
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dl3031a import mcp_tools as dl_tools
    fake = _FakeDL()
    dl_tools._dl3031a = fake
    try:
        # Each step must be (level, width); a bare scalar is malformed
        with pytest.raises((TypeError, IndexError, ValueError)):
            m.dl3031a_program_list(steps=[0.030, 0.05], mode="CC")
        # Non-numeric also rejected
        with pytest.raises((TypeError, ValueError)):
            m.dl3031a_program_list(steps=[["a", "b"], [0.0, 0.1]], mode="CC")
    finally:
        dl_tools._dl3031a = None


def test_dl3031a_tools_raise_driver_connection_error_when_not_open():
    """L2 fix: _get_dl3031a now raises RigolDLConnectionError, not
    bare RuntimeError, when the singleton is None."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dl3031a import mcp_tools as dl_tools
    from benchctrl.drivers.rigol_dl3031a.driver import RigolDLConnectionError
    dl_tools._dl3031a = None
    with pytest.raises(RigolDLConnectionError):
        m.dl3031a_info()


def test_qr10x_tools_raise_driver_connection_error_when_not_open():
    """Same as above for QR10x."""
    from benchctrl import mcp as m
    from benchctrl.drivers.eastwood_qr10x import mcp_tools as qr_tools
    from benchctrl.drivers.eastwood_qr10x.driver import QR10xConnectionError
    qr_tools._qr10x = None
    with pytest.raises(QR10xConnectionError):
        m.qr10x_info()


def test_dl3031a_close_surfaces_input_off_failure():
    """H5 fix: dl3031a_close returns input_off_failed when set_input(False)
    raises. Previously, errors were silently swallowed and the operator
    got {"closed": True} even though the load could still be sinking."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dl3031a import mcp_tools as dl_tools
    fake = _FakeDL()

    def boom(_on):
        raise RuntimeError("simulated set_input failure")
    fake.set_input = boom  # type: ignore[method-assign]

    dl_tools._dl3031a = fake
    result = m.dl3031a_close()
    assert result["closed"] is True
    assert "input_off_failed" in result
    assert "simulated set_input failure" in result["input_off_failed"]
    assert "warning" in result


# ---------------------------------------------------------------------------
# DP2031 MCP layer — connection-state checks and singleton injection
# ---------------------------------------------------------------------------


class _FakeDP2031:
    """Stub that mimics the RigolDP2031 surface the MCP tools touch."""
    def __init__(self):
        self.set_output_calls: list[tuple[int, bool]] = []
        self.set_voltage_calls: list[tuple[int, float]] = []
        self._closed = False

    def set_voltage(self, ch, volts):
        self.set_voltage_calls.append((int(ch), float(volts)))

    def get_voltage(self, ch):
        return 3.3

    def set_output(self, ch, on):
        self.set_output_calls.append((int(ch), bool(on)))

    def close(self):
        self._closed = True


def test_dp2031_tools_raise_driver_connection_error_when_not_open():
    """Same pattern as DL3031A/QR10x — clear singleton, expect a
    driver-specific connection error from any tool that needs the device."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dp2031 import mcp_tools as dp_tools
    from benchctrl.drivers.rigol_dp2031.driver import RigolDP2031ConnectionError
    dp_tools._dp2031 = None
    with pytest.raises(RigolDP2031ConnectionError):
        m.dp2031_info()
    with pytest.raises(RigolDP2031ConnectionError):
        m.dp2031_set_voltage(1, 3.3)


def test_dp2031_set_voltage_calls_driver():
    """Singleton-injection sanity: dp2031_set_voltage routes channel + V
    correctly to the underlying driver."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dp2031 import mcp_tools as dp_tools
    fake = _FakeDP2031()
    dp_tools._dp2031 = fake
    try:
        result = m.dp2031_set_voltage(2, 5.0)
        assert result == {"channel": 2, "voltage_V": 5.0}
        assert fake.set_voltage_calls == [(2, 5.0)]
    finally:
        dp_tools._dp2031 = None


def test_dp2031_close_disables_all_three_channels():
    """dp2031_close should call set_output(ch, False) for ch in 1/2/3
    before close()."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dp2031 import mcp_tools as dp_tools
    fake = _FakeDP2031()
    dp_tools._dp2031 = fake
    result = m.dp2031_close()
    assert result == {"closed": True}
    assert fake.set_output_calls == [(1, False), (2, False), (3, False)]
    assert fake._closed is True
    assert dp_tools._dp2031 is None


def test_dp2031_close_surfaces_per_channel_failure():
    """If one channel's set_output(False) fails, the response dict
    surfaces it under outputs_off_failed (parallels dl3031a_close H5
    behaviour). Other channels are still attempted."""
    from benchctrl import mcp as m
    from benchctrl.drivers.rigol_dp2031 import mcp_tools as dp_tools
    fake = _FakeDP2031()

    original = fake.set_output

    def flaky(ch, on):
        if int(ch) == 2:
            raise RuntimeError(f"simulated CH{ch} failure")
        original(ch, on)
    fake.set_output = flaky  # type: ignore[method-assign]

    dp_tools._dp2031 = fake
    result = m.dp2031_close()
    assert result["closed"] is True
    assert "outputs_off_failed" in result
    assert any("CH2" in f for f in result["outputs_off_failed"])
    assert "warning" in result
    # CH1 and CH3 should still have been attempted via the original
    assert (1, False) in fake.set_output_calls
    assert (3, False) in fake.set_output_calls


# ---------------------------------------------------------------------------
# SDM4065A MCP layer — connection state, parity with the driver, tool shapes
# ---------------------------------------------------------------------------


class _FakeSDM4065A:
    """Stub with the SDM4065A surface the MCP tools touch.

    Deliberately records the *order* of null calls: the driver's contract is
    state-then-value (enabling NULL:STATe arms NULL:VALue:AUTO, so the
    reverse order nulls by the wrong number), and a tool that reordered them
    would still return a plausible dict.
    """

    def __init__(self):
        self.calls: list[tuple] = []
        self._null = False
        self._null_auto = False
        self._null_value = 0.0
        self._closed = False

    # identity ------------------------------------------------------------
    def info(self):
        from benchctrl.drivers.siglent_sdm4065a.driver import SDM4065AInfo

        return SDM4065AInfo(
            manufacturer="Siglent Technologies",
            model="SDM4065A",
            serial="FAKE0001",
            firmware="1.01.01.15",
            resource="ASRL/dev/null::INSTR",
        )

    # measurement ---------------------------------------------------------
    def measure_resistance(self, range_=None):
        self.calls.append(("measure_resistance", range_))
        return 100.2

    def measure_resistance_4wire(self, range_=None):
        self.calls.append(("measure_resistance_4wire", range_))
        return 100.0

    def read(self):
        self.calls.append(("read",))
        return [100.0, 100.0]

    def read_nulled(self):
        self.calls.append(("read_nulled",))
        return 100.0

    def get_function(self):
        return "RES"

    # null ----------------------------------------------------------------
    def null_now(self, *, function="RESistance", samples=1):
        self.calls.append(("null_now", function, samples))
        self._null = True
        self._null_auto = False
        self._null_value = 0.2
        return self._null_value

    def set_null(self, enable, *, function="RESistance"):
        self.calls.append(("set_null", enable))
        self._null = bool(enable)
        if enable:
            self._null_auto = True  # the real instrument's side effect

    def get_null(self, *, function="RESistance"):
        return self._null

    def set_null_value(self, value, *, function="RESistance"):
        self.calls.append(("set_null_value", value))
        self._null_value = float(value)
        self._null_auto = False

    def get_null_value(self, *, function="RESistance"):
        return self._null_value

    def set_null_auto(self, enable, *, function="RESistance"):
        self._null_auto = bool(enable)

    def get_null_auto(self, *, function="RESistance"):
        return self._null_auto

    def close(self):
        self._closed = True


def test_sdm4065a_tools_raise_driver_connection_error_when_not_open():
    """Same pattern as DL3031A/QR10x/DP2031 — a driver-specific connection
    error, not a bare RuntimeError, when the singleton is None."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools
    from benchctrl.drivers.siglent_sdm4065a.driver import SDM4065AConnectionError

    sdm_tools._sdm4065a = None
    with pytest.raises(SDM4065AConnectionError):
        m.sdm4065a_info()
    with pytest.raises(SDM4065AConnectionError):
        m.sdm4065a_measure_resistance()


def test_sdm4065a_close_needs_no_disarm():
    """A DMM sources nothing, so unlike dl3031a_close/dp2031_close there is
    no output to switch off first. Pinned so a future edit that added a
    disarm step to a *meter* would look deliberate rather than copied."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    fake = _FakeSDM4065A()
    sdm_tools._sdm4065a = fake
    try:
        assert m.sdm4065a_close() == {"closed": True}
        assert fake._closed is True
        assert sdm_tools._sdm4065a is None
        assert fake.calls == []  # nothing was written on the way out
    finally:
        sdm_tools._sdm4065a = None


def test_sdm4065a_close_when_nothing_is_open_is_not_an_error():
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    sdm_tools._sdm4065a = None
    result = m.sdm4065a_close()
    assert result["closed"] is False


def test_sdm4065a_measure_tools_pass_the_range_through():
    """Singleton-injection sanity: the range argument must reach the driver,
    because on this meter the default is 2 kΩ rather than autorange, and a
    dropped range silently costs a factor of ten in accuracy."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    fake = _FakeSDM4065A()
    sdm_tools._sdm4065a = fake
    try:
        assert m.sdm4065a_measure_resistance(200)["resistance_ohm"] == 100.2
        assert m.sdm4065a_measure_resistance_4wire(200)["resistance_ohm"] == 100.0
        assert ("measure_resistance", 200.0) in [
            (c[0], float(c[1])) for c in fake.calls if len(c) == 2
        ]
    finally:
        sdm_tools._sdm4065a = None


def test_sdm4065a_read_tool_returns_every_sample():
    """``read()`` is ``list[float]``; a tool that unwrapped it to a scalar
    would silently discard every sample after the first."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    fake = _FakeSDM4065A()
    sdm_tools._sdm4065a = fake
    try:
        assert m.sdm4065a_read()["readings"] == [100.0, 100.0]
    finally:
        sdm_tools._sdm4065a = None


def test_sdm4065a_null_now_tool_reports_auto_disarmed():
    """The tool's response is what the model reads back, so it must show
    ``null_auto`` false — if auto were still armed the instrument would
    overwrite the offset with its own next reading."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    fake = _FakeSDM4065A()
    sdm_tools._sdm4065a = fake
    try:
        result = m.sdm4065a_null_now(samples=3)
        assert result["null_offset"] == pytest.approx(0.2)
        assert result["null_enabled"] is True
        assert result["null_auto"] is False
        assert ("null_now", "RESistance", 3) in fake.calls
    finally:
        sdm_tools._sdm4065a = None


def test_sdm4065a_set_null_tool_surfaces_the_armed_auto_flag():
    """``sdm4065a_set_null(True)`` alone leaves auto armed. The tool reports
    that rather than hiding it, so a model that used the low-level tool can
    see it needs to set a value next."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    fake = _FakeSDM4065A()
    sdm_tools._sdm4065a = fake
    try:
        result = m.sdm4065a_set_null(True)
        assert result["null_enabled"] is True
        assert result["null_auto"] is True
    finally:
        sdm_tools._sdm4065a = None


def test_sdm4065a_reading_timeout_ms_needs_no_open_device():
    """A pure-arithmetic helper: it has to be callable *before* open, since
    its whole purpose is choosing the timeout to open with."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    sdm_tools._sdm4065a = None
    slow = m.sdm4065a_reading_timeout_ms(100, samples=10)["timeout_ms"]
    fast = m.sdm4065a_reading_timeout_ms(0.001, samples=1)["timeout_ms"]
    assert slow > 10_000  # the point of the helper: exceeds the open default
    assert slow > fast


# ----- parity: every driver capability is reachable over MCP ---------------


def test_sdm4065a_mcp_tools_cover_the_driver_surface():
    """Every public driver method has a tool, except a documented few.

    This is the test that fails when a method is added to the driver and the
    tool is forgotten — the failure mode that leaves a capability working
    locally and invisible to an agent.
    """
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools
    from benchctrl.drivers.siglent_sdm4065a.driver import SiglentSDM4065A

    # Deliberately not exposed:
    #   open        — sdm4065a_open is the tool; the classmethod is internal
    #   query_float/query_floats — typed sugar over query(); sdm4065a_query
    #                 is the single raw escape hatch, as for every other driver
    #   get_null_value/get_null_auto — folded into sdm4065a_get_null's dict,
    #                 which returns state, offset and auto together
    exempt = {
        "open",
        "query_float",
        "query_floats",
        "get_null_value",
        "get_null_auto",
    }
    methods = {
        name
        for name in vars(SiglentSDM4065A)
        if not name.startswith("_") and callable(getattr(SiglentSDM4065A, name))
    } - exempt
    tools = {fn.__name__[len("sdm4065a_"):] for fn in sdm_tools._TOOLS}

    assert not (methods - tools), f"driver methods with no MCP tool: {sorted(methods - tools)}"
    assert not (tools - methods - {"open"}), (
        f"MCP tools with no driver method: {sorted(tools - methods - {'open'})}"
    )


def test_sdm4065a_tools_are_registered_on_the_shared_server():
    """Owning the tools is not the same as registering them. This fails if
    ``benchctrl.mcp`` forgot the ``register_mcp_tools`` call."""
    import asyncio

    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools
    from benchctrl.mcp import mcp

    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    for fn in sdm_tools._TOOLS:
        assert fn.__name__ in registered, f"{fn.__name__} not registered on the server"


def test_sdm4065a_tools_are_importable_from_the_orchestrator():
    """The re-export block in ``benchctrl.mcp`` is how tests and callers
    reach these; a missing name there breaks them without breaking MCP."""
    from benchctrl import mcp as m
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    missing = [fn.__name__ for fn in sdm_tools._TOOLS if not hasattr(m, fn.__name__)]
    assert missing == []


def test_sdm4065a_tools_have_docstrings():
    """The docstring is the tool description the model sees. For this driver
    it is also where the accuracy traps live, so an empty one is a real
    defect and not a style issue."""
    from benchctrl.drivers.siglent_sdm4065a import mcp_tools as sdm_tools

    for fn in sdm_tools._TOOLS:
        assert fn.__doc__ and fn.__doc__.strip(), f"{fn.__name__} missing docstring"

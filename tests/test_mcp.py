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
    #   get_autozero_cached — SDK-only by design: it returns the last
    #                 commanded value without a round trip, which is useful
    #                 inside a timed loop but would be a trap as a tool, since
    #                 an agent cannot tell a cache from a measurement.
    #                 sdm4065a_get_autozero reads the instrument instead.
    exempt = {
        "open",
        "query_float",
        "query_floats",
        "get_null_value",
        "get_null_auto",
        "get_autozero_cached",
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


# ----- CyberPower PDU41002: the first MCP surface that could cut mains ------


def test_pdu41002_mcp_tools_cover_the_driver_surface():
    """Every public driver method is reachable through a tool.

    Unlike the other drivers, the tool names here are **not** the method names:
    the MCP surface is phrased for a model (``pdu41002_info``,
    ``pdu41002_status``) while the driver is phrased for the SDK
    (``read_identity``, ``read_device_status``). So parity needs an explicit
    map rather than a prefix strip — and the map is the useful artifact,
    because it is what fails when a method is added and the tool is forgotten.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools
    from benchctrl.drivers.cyberpower_pdu41002.driver import CyberPowerPDU41002

    #: tool suffix -> the driver method it exposes
    TOOL_TO_METHOD = {
        "info": "read_identity",
        "status": "read_device_status",
        "measure_load": "measure_load_A",
        "measure_voltage": "measure_voltage_V",
        "measure_frequency": "measure_frequency_Hz",
        "outlet_states": "outlet_states",
        "outlet_state": "outlet_state",
        "outlet_config": "read_outlet_config",
        "set_outlet_state": "set_outlet_state",
        "reset_outlet": "reset_outlet",
        "clear_outlet_command": "clear_outlet_command",
    }
    #: tools with no single method behind them — they compose properties
    COMPOSED = {"open", "close", "allowed_outlets", "transport"}

    # Deliberately not exposed:
    #   open/close — the tools exist, but they wrap lifecycle rather than a
    #                capability, and pdu41002_open takes no password parameter
    #                where the classmethod does
    #   outlet_name — folded into pdu41002_outlet_state's dict, which returns
    #                index, state and name together; a separate tool would be
    #                an extra round trip on a 9600 baud console for a field the
    #                model already has
    exempt = {"open", "close", "outlet_name"}

    methods = {
        name
        for name in vars(CyberPowerPDU41002)
        if not name.startswith("_") and callable(getattr(CyberPowerPDU41002, name))
    } - exempt
    tools = {fn.__name__[len("pdu41002_"):] for fn in pdu_tools._TOOLS}

    assert tools == set(TOOL_TO_METHOD) | COMPOSED, (
        "the tool list changed — update TOOL_TO_METHOD so parity is still "
        f"checked. Unmapped: {sorted(tools - set(TOOL_TO_METHOD) - COMPOSED)}"
    )
    covered = set(TOOL_TO_METHOD.values())
    assert not (methods - covered), (
        f"driver methods with no MCP tool: {sorted(methods - covered)}"
    )
    # Every mapped method must actually exist, or the map rots into a
    # comfortable fiction that passes while covering nothing.
    for tool, method in TOOL_TO_METHOD.items():
        assert hasattr(CyberPowerPDU41002, method), (
            f"pdu41002_{tool} maps to missing method {method}"
        )


def test_pdu41002_tools_are_registered_on_the_shared_server():
    """Owning the tools is not the same as registering them."""
    import asyncio

    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools
    from benchctrl.mcp import mcp

    registered = {t.name for t in asyncio.run(mcp.list_tools())}
    for fn in pdu_tools._TOOLS:
        assert fn.__name__ in registered, f"{fn.__name__} not registered on the server"


def test_pdu41002_tools_are_importable_from_the_orchestrator():
    from benchctrl import mcp as m
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    missing = [fn.__name__ for fn in pdu_tools._TOOLS if not hasattr(m, fn.__name__)]
    assert missing == []


def test_pdu41002_tools_have_docstrings():
    """For this driver the docstrings *are* the safety interface — they are what
    a model reads before calling something on a device that switches mains — so
    an empty one is a real defect."""
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    for fn in pdu_tools._TOOLS:
        assert fn.__doc__ and fn.__doc__.strip(), f"{fn.__name__} missing docstring"


def test_pdu41002_open_takes_no_password_parameter():
    """Absent, not defaulted.

    A credential passed as a tool argument is logged in the conversation
    transcript, so the parameter must not exist for a model to fill in. The
    password comes from ``BENCHCTRL_PDU_PASSWORD`` in the server's environment.

    Checks every tool, not just ``open``: a future ``pdu41002_reconnect`` would
    reintroduce the hole.
    """
    import inspect

    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    for fn in pdu_tools._TOOLS:
        params = set(inspect.signature(fn).parameters)
        leaky = params & {"password", "passphrase", "secret", "token", "env"}
        assert not leaky, f"{fn.__name__} accepts credential material: {sorted(leaky)}"


def test_pdu41002_exposes_exactly_the_reviewed_switching_tools():
    """Which tools can move a contactor is pinned in both directions.

    A *new* mains-switching tool arriving without review is exactly the thing to
    catch mechanically — that was this test's job when the surface was read-only,
    and it still is now that three switching tools exist. Adding a fourth means
    updating this list on purpose.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    switching = {
        "pdu41002_set_outlet_state",
        "pdu41002_reset_outlet",
        "pdu41002_clear_outlet_command",
    }
    names = {fn.__name__ for fn in pdu_tools._TOOLS}
    assert names & switching == switching, "a reviewed switching tool went missing"

    suspicious = {
        n
        for n in names - switching
        if any(w in n for w in ("set_outlet", "reset", "reboot", "toggle", "switch"))
    }
    assert not suspicious, (
        f"unreviewed tool(s) that look like they switch mains: {sorted(suspicious)}"
    )


def test_pdu41002_switching_tools_warn_about_mains_in_their_docstrings():
    """The docstring *is* the safety interface for an MCP tool — it is what a
    model reads before deciding to call it.

    So each switching tool must say plainly that it cuts real power. A tool
    documented as neutrally as a read is one a model will treat as neutral.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    for name in ("set_outlet_state", "reset_outlet"):
        doc = getattr(pdu_tools, f"pdu41002_{name}").__doc__ or ""
        assert "mains" in doc.lower(), f"{name} does not mention mains"
        assert "allowed_outlets" in doc, f"{name} does not point at the allowlist"


def test_pdu41002_read_tools_report_a_clear_error_before_open():
    """A model that calls a read first must get "call open" back, not a
    ``NoneType`` traceback — the message is what tells it what to do next."""
    from benchctrl import mcp as m
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002ConnectionError

    pdu_tools._pdu = None
    with pytest.raises(PDU41002ConnectionError, match="pdu41002_open"):
        m.pdu41002_status()


def test_pdu41002_close_is_safe_to_call_when_not_open():
    """Teardown must not raise. A close that failed because nothing was open
    would make cleanup code unreliable on the one device where a *missed*
    close leaves the PDU unreachable from the other transport."""
    from benchctrl import mcp as m
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools

    pdu_tools._pdu = None
    assert m.pdu41002_close()["closed"] is False


def test_pdu41002_tools_work_against_the_simulator():
    """The tools end to end, through the module singleton.

    Bypasses ``pdu41002_open`` (which would need a real port) by injecting the
    simulator-backed driver, so the tools' own logic is what is under test.
    """
    from benchctrl import mcp as m
    from benchctrl.drivers.cyberpower_pdu41002 import PDU41002PolicyError
    from benchctrl.drivers.cyberpower_pdu41002 import mcp_tools as pdu_tools
    from benchctrl.sim.factories import make_pdu41002

    driver = make_pdu41002(allowed_outlets=(2, 3))
    pdu_tools._pdu = driver
    try:
        assert m.pdu41002_info()["model"] == "PDU41002"
        assert m.pdu41002_status()["frequency_Hz"] == pytest.approx(60.0)
        assert m.pdu41002_measure_voltage()["voltage_V"] == pytest.approx(121.7)
        assert m.pdu41002_transport()["transport"] == "serial"

        # JSON object keys are strings, so the tool must key by str(index) —
        # a model that got integer keys back would see them stringified
        # anyway, and an inconsistency here is a silent KeyError in its code.
        states = m.pdu41002_outlet_states()
        assert set(states["outlets"]) == {str(i) for i in range(1, 9)}
        assert states["on_count"] == 8

        one = m.pdu41002_outlet_state(1)
        assert one["on"] is True and one["index"] == 1

        cfg = m.pdu41002_outlet_config()
        assert cfg["outlets"]["1"]["on_delay_s"] == 3

        policy = m.pdu41002_allowed_outlets()
        assert policy["allowed_outlets"] == [2, 3]
        assert policy["switching_available"] is True

        # Switching through the tool layer, on an allowed outlet. `state` is the
        # verified read-back, so asserting it proves the tool reports what the
        # device did rather than what was asked of it.
        result = m.pdu41002_set_outlet_state(2, False)
        assert result["state"] is False
        assert result["verified"] is True
        assert m.pdu41002_outlet_state(2)["on"] is False

        # ...and the allowlist is enforced through the tool layer too, not just
        # in the SDK. A model calling the tool directly must hit the same wall.
        with pytest.raises(PDU41002PolicyError):
            m.pdu41002_set_outlet_state(5, False)
        assert m.pdu41002_outlet_state(5)["on"] is True
    finally:
        pdu_tools._pdu = None
        driver.close()


# ----- ADU218: parity, and the naming that keeps the docstrings honest -----


def test_adu218_mcp_tools_cover_the_driver_surface():
    """Every public driver method is reachable through a tool.

    The tool names are **not** the method names here: the MCP surface is phrased
    for a model (``adu218_info``, ``adu218_counters``) while the driver is
    phrased for the SDK (``read_identity``, ``read_counters``). So parity needs
    an explicit map rather than a prefix strip — and the map is the useful
    artifact, because it is what fails when a method is added and the tool is
    forgotten, the failure mode that leaves a capability working locally and
    invisible to an agent.
    """
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools
    from benchctrl.drivers.ontrak_adu218.driver import OntrakADU218

    #: tool suffix -> the driver method it exposes
    TOOL_TO_METHOD = {
        "info": "read_identity",
        "relay_states": "relay_states",
        "relay_state": "relay_state",
        "set_relay_state": "set_relay_state",
        "set_relay_port": "set_relay_port",
        "reset_relays": "reset_relays",
        "input_states": "input_states",
        "input_state": "input_state",
        "input_port_mask": "input_port_mask",
        "counters": "read_counters",
        "counter": "read_counter",
        "clear_counter": "clear_counter",
        "debounce": "read_debounce",
        "set_debounce": "set_debounce",
        "watchdog": "read_watchdog",
        "set_watchdog": "set_watchdog",
    }
    #: tools with no single method behind them — they compose properties
    COMPOSED = {"open", "close", "allowed_relays"}

    # Deliberately not exposed:
    #   open/close   — the tools exist, but wrap lifecycle rather than a
    #                  capability, and their signatures differ from the
    #                  classmethod's
    #   relay_mask   — folded into adu218_relay_states' dict, which returns the
    #                  per-relay map, the energised list and the mask together;
    #                  a separate tool would be a second round trip for a value
    #                  the model already has
    #   input_mask   — same, folded into adu218_input_states. Note this is *not*
    #                  the same read as adu218_input_port_mask, which does get a
    #                  tool of its own: input_mask is ``PI``, both ports packed
    #                  into one byte, and the folded dict already carries it.
    #                  input_port_mask is ``Py``, one port, and is the only input
    #                  read whose bits need no reordering — a model asking about
    #                  one port should not have to mask a two-port value and get
    #                  the nibble order right to do it
    #   read_debounce_ms — same, folded into adu218_debounce, which returns both
    #                  ``debounce`` and ``debounce_ms``. Returning them together
    #                  is the point: the setting number runs *backwards* to the
    #                  filter width (0 = 10 ms, 2 = 100 us), so a tool handing a
    #                  model the bare number invites "2 filters hardest"
    #   read_watchdog_tripped — folded into adu218_watchdog, and deliberately:
    #                  it *clears* the driver-held expectation when it detects a
    #                  trip, so a standalone tool would let a model consume the
    #                  only trace of a trip without seeing the setting it must
    #                  be compared against
    #   watchdog_setting — the cached expectation, not a measurement. As a tool
    #                  a model could not tell a cache from a device read; it is
    #                  returned as ``expected`` inside adu218_watchdog instead
    exempt = {
        "open",
        "close",
        "relay_mask",
        "input_mask",
        "read_debounce_ms",
        "read_watchdog_tripped",
        "watchdog_setting",
    }

    methods = {
        name
        for name in vars(OntrakADU218)
        if not name.startswith("_") and callable(getattr(OntrakADU218, name, None))
    } - exempt
    tools = {fn.__name__[len("adu218_"):] for fn in adu_tools._TOOLS}

    assert tools == set(TOOL_TO_METHOD) | COMPOSED, (
        "the tool list changed — update TOOL_TO_METHOD so parity is still "
        f"checked. Unmapped: {sorted(tools - set(TOOL_TO_METHOD) - COMPOSED)}"
    )
    covered = set(TOOL_TO_METHOD.values())
    assert not (methods - covered), (
        f"driver methods with no MCP tool: {sorted(methods - covered)}"
    )
    # Every mapped method must actually exist, or the map rots into a
    # comfortable fiction that passes while covering nothing.
    for tool, method in TOOL_TO_METHOD.items():
        assert hasattr(OntrakADU218, method), (
            f"adu218_{tool} maps to missing method {method}"
        )


def test_adu218_tools_are_registered_on_the_shared_server():
    from benchctrl import mcp as m
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    registered = {fn.__name__ for fn in adu_tools._TOOLS}
    for name in registered:
        assert hasattr(m, name), f"{name} is not re-exported from benchctrl.mcp"


def test_adu218_tools_have_docstrings():
    """The docstring *is* the interface for an MCP tool — it is what a model
    reads before deciding to call it."""
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    for fn in adu_tools._TOOLS:
        assert (fn.__doc__ or "").strip(), f"{fn.__name__} has no docstring"


def test_adu218_exposes_exactly_the_reviewed_switching_tools():
    """Which tools can move a contact is pinned in both directions.

    A new switching tool arriving without review is the thing to catch
    mechanically. Adding a fourth means updating this list on purpose.
    """
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    switching = {
        "adu218_set_relay_state",
        "adu218_set_relay_port",
        "adu218_reset_relays",
    }
    names = {fn.__name__ for fn in adu_tools._TOOLS}
    assert names & switching == switching, "a reviewed switching tool went missing"

    suspicious = {
        n
        for n in names - switching
        if any(w in n for w in ("set_relay", "toggle", "switch", "energis", "close_"))
    }
    assert not suspicious, (
        f"unreviewed tool(s) that look like they switch a relay: {sorted(suspicious)}"
    )


def test_adu218_switching_tools_say_what_they_physically_do():
    """A tool documented as neutrally as a read is one a model treats as
    neutral. These make and break real circuits — on this bench including
    instrument sense leads, so a switch mid-measurement changes what another
    instrument is reading."""
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    for name in ("set_relay_state", "set_relay_port"):
        doc = getattr(adu_tools, f"adu218_{name}").__doc__ or ""
        assert "allowed_relays" in doc, f"{name} does not point at the allowlist"
        assert any(
            word in doc.lower() for word in ("circuit", "conduct", "connected")
        ), f"{name} does not say it moves a physical contact"


def test_the_watchdog_tool_warns_that_reading_it_refeeds_the_timer():
    """The property that makes a monitoring loop dangerous, and the one a model
    is most likely to get wrong: polling ``adu218_watchdog`` in a loop keeps an
    armed watchdog alive and guarantees ``tripped`` stays false."""
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    doc = adu_tools.adu218_watchdog.__doc__ or ""
    assert "refeed" in doc.lower()
    arm_doc = adu_tools.adu218_set_watchdog.__doc__ or ""
    assert "de-energise" in arm_doc.lower()
    # RST emphasis markers land *inside* the phrase — "**Any** command" is how
    # this sentence is written, and rightly so, since that word is the whole
    # hazard. Strip the markup before matching rather than forbidding emphasis
    # on the one clause that most needs it.
    plain = arm_doc.lower().replace("*", "")
    assert "any command" in plain
    # Also assert the consequence, not just the word: a docstring can say "any
    # command" while describing something harmless. The refeed only matters
    # because a polling loop is the thing that silently neuters the watchdog.
    assert "poll" in plain


def test_adu218_read_tools_report_a_clear_error_before_open():
    """A model that calls a read first must get "call open" back, not a
    ``NoneType`` traceback — the message is what tells it what to do next."""
    from benchctrl import mcp as m
    from benchctrl.drivers.ontrak_adu218 import ADU218ConnectionError
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    adu_tools._adu218 = None
    with pytest.raises(ADU218ConnectionError, match="adu218_open"):
        m.adu218_relay_states()


def test_adu218_close_is_safe_to_call_when_not_open():
    """Teardown must not raise, or cleanup code becomes unreliable."""
    from benchctrl import mcp as m
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools

    adu_tools._adu218 = None
    assert m.adu218_close()["closed"] is False


def test_adu218_tools_work_against_the_simulator():
    """The tools end to end, through the module singleton.

    Bypasses ``adu218_open`` (which would need real USB) by injecting the
    simulator-backed driver, so the tools' own logic is what is under test.
    """
    from benchctrl import mcp as m
    from benchctrl.drivers.ontrak_adu218 import ADU218PolicyError
    from benchctrl.drivers.ontrak_adu218 import mcp_tools as adu_tools
    from benchctrl.sim.factories import make_adu218

    driver = make_adu218(allowed_relays=(0, 1))
    adu_tools._adu218 = driver
    try:
        assert m.adu218_info()["model"] == "ADU218"

        # JSON object keys are strings, so the tool must key by str(index) — and
        # relay 0 makes that sharper than the PDU's 1-indexed equivalent.
        states = m.adu218_relay_states()
        assert set(states["relays"]) == {str(i) for i in range(8)}
        assert states["energised"] == []
        assert states["mask"] == 0

        assert m.adu218_relay_state(0)["on"] is False
        assert m.adu218_allowed_relays()["allowed_relays"] == [0, 1]
        assert m.adu218_counters()["counters"]["0"] == 0

        # The de-bounce tool must carry the *width* as well as the setting,
        # because the two run in opposite directions: setting 1 is 1 ms, but
        # setting 0 is the longest filter (10 ms) and 2 the shortest (100 us).
        # This is what licenses read_debounce_ms's parity exemption above.
        debounce = m.adu218_debounce()
        assert debounce["debounce"] == 1
        assert debounce["debounce_ms"] == 1.0
        assert m.adu218_set_debounce(0) == {"debounce": 0, "debounce_ms": 10.0}
        assert m.adu218_set_debounce(2) == {"debounce": 2, "debounce_ms": 0.1}
        m.adu218_set_debounce(1)

        # Switching through the tool layer. `state` is the verified read-back, so
        # asserting it proves the tool reports what the device did rather than
        # what was asked of it.
        result = m.adu218_set_relay_state(0, True)
        assert result["state"] is True
        assert result["verified"] is True
        assert m.adu218_relay_states()["energised"] == [0]

        # The allowlist is enforced through the tool layer too, not just in the
        # SDK — a model calling the tool directly must hit the same wall.
        with pytest.raises(ADU218PolicyError):
            m.adu218_set_relay_state(5, True)
        # ...and de-energising an unlisted relay is still allowed, which is the
        # asymmetry the tool docstrings promise.
        assert m.adu218_set_relay_state(5, False)["state"] is False

        # Ports A and B are four lines each, and the input map must arrive
        # already reversed out of the device's MSB-first reply.
        driver._benchctrl_sim.device_model.set_input("B", 3, True)
        ports = m.adu218_input_states()["ports"]
        assert ports["B"] == [False, False, False, True]
        assert m.adu218_input_state("b", 3)["asserted"] is True

        # Arming reports the measured timeout, not a manual-derived one.
        armed = m.adu218_set_watchdog(3)
        assert armed["timeout_s"] == 60.0 and armed["armed"] is True
        assert m.adu218_watchdog()["setting"] == 3
        assert m.adu218_set_watchdog(0)["armed"] is False

        assert m.adu218_reset_relays()["mask"] == 0
    finally:
        adu_tools._adu218 = None
        driver.close()


# --------------------------------------------------------------------------
# The generated CLI's contract: ``_TOOLS`` is the whole surface
# --------------------------------------------------------------------------
#
# ``benchctrl`` (the CLI) builds its subcommands by walking each module's
# ``_TOOLS`` tuple rather than asking a live FastMCP server, because importing
# ``benchctrl.mcp`` costs ~0.6 s and drags in the optional ``[mcp]`` extra —
# which needs Python >=3.10 while the package supports 3.9. That is only sound
# while the two routes describe the same surface. The tests below are what make
# it sound: a tool added with a bare ``@mcp.tool()`` against the module-level
# server would be reachable over MCP and *silently absent from the CLI*, which
# is the exact failure mode that motivated moving the framework tools out of
# ``benchctrl.mcp`` into ``benchctrl.framework_tools``.


#: Every module that contributes tools. The CLI reads this same list, so a new
#: driver missing here is missing from both — a visible failure, not a silent
#: one.
TOOL_MODULES = (
    "benchctrl.drivers.otii_arc.mcp_tools",
    "benchctrl.drivers.eastwood_qr10x.mcp_tools",
    "benchctrl.drivers.rigol_dl3031a.mcp_tools",
    "benchctrl.drivers.rigol_dp2031.mcp_tools",
    "benchctrl.drivers.siglent_sdm4065a.mcp_tools",
    "benchctrl.drivers.cyberpower_pdu41002.mcp_tools",
    "benchctrl.drivers.silabs_cp2112.mcp_tools",
    "benchctrl.drivers.ontrak_adu218.mcp_tools",
    "benchctrl.framework_tools",
)


def _tools_route_names() -> list[str]:
    """The surface as the CLI sees it: no FastMCP import anywhere."""
    import importlib

    names = []
    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        names += [fn.__name__ for fn in mod._TOOLS]
    return names


def test_every_tool_module_exposes_the_registration_contract():
    """``_TOOLS`` plus ``register_mcp_tools`` — the shape the CLI relies on."""
    import importlib

    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        assert isinstance(mod._TOOLS, tuple), f"{mod_name}._TOOLS must be a tuple"
        assert mod._TOOLS, f"{mod_name}._TOOLS is empty"
        assert callable(mod.register_mcp_tools), f"{mod_name} has no register_mcp_tools"
        for fn in mod._TOOLS:
            assert callable(fn), f"{mod_name}._TOOLS holds a non-callable: {fn!r}"


def test_tools_route_has_no_duplicate_names():
    """Two modules exporting one name would collide into a single subcommand,
    and argparse takes the last one silently. Checked separately from parity
    because a duplicate makes the *set* comparison pass while the CLI loses a
    tool."""
    names = _tools_route_names()
    dupes = sorted({n for n in names if names.count(n) > 1})
    assert dupes == [], f"tool names exported by more than one module: {dupes}"


def test_tools_route_matches_the_live_mcp_server_exactly():
    """The parity guarantee, asserted as set equality in both directions.

    A tool present only on the server is missing from the CLI; a tool present
    only in ``_TOOLS`` is a CLI subcommand that no MCP client can reach. Both
    are defects, so neither ``<=`` nor ``>=`` is the right assertion.
    """
    import asyncio

    pytest.importorskip("mcp.server.fastmcp")
    from benchctrl.mcp import mcp

    served = {t.name for t in asyncio.run(mcp.list_tools())}
    generated = set(_tools_route_names())

    assert sorted(served - generated) == [], (
        "on the MCP server but not in any _TOOLS tuple — the CLI cannot see "
        "these. A bare @mcp.tool() in benchctrl.mcp is the usual cause."
    )
    assert sorted(generated - served) == [], (
        "in a _TOOLS tuple but never registered on the server — a "
        "register_mcp_tools call is missing from benchctrl.mcp."
    )


def test_framework_tools_are_reachable_without_importing_fastmcp():
    """The whole point of the ``_TOOLS`` route.

    Imports the framework module into a fresh interpreter with ``mcp`` poisoned,
    proving the CLI's enumeration path does not reach FastMCP even transitively.
    A subprocess is needed because ``benchctrl.mcp`` is almost certainly already
    in ``sys.modules`` by the time this test runs, which would make an in-process
    check pass vacuously.
    """
    import subprocess
    import sys
    import textwrap

    program = textwrap.dedent(
        """
        import sys

        class _Poison:
            def __getattr__(self, name):
                raise AssertionError("FastMCP was imported by the _TOOLS route")

        sys.modules["mcp"] = _Poison()
        sys.modules["mcp.server"] = _Poison()
        sys.modules["mcp.server.fastmcp"] = _Poison()

        from benchctrl import framework_tools

        assert len(framework_tools._TOOLS) == 13, len(framework_tools._TOOLS)
        assert "benchctrl.mcp" not in sys.modules
        print("ok")
        """
    )
    out = subprocess.run(
        [sys.executable, "-c", program],
        capture_output=True,
        text=True,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout.strip() == "ok"


def test_framework_tools_are_importable_from_the_orchestrator():
    """The re-export block: ``from benchctrl.mcp import plot_recording`` is a
    documented import path and predates the move, so it must keep working."""
    from benchctrl import framework_tools
    from benchctrl import mcp as m

    missing = [fn.__name__ for fn in framework_tools._TOOLS if not hasattr(m, fn.__name__)]
    assert missing == []
    # Same object, not a copy — a re-export that shadowed the original would
    # mean the emulator's module-global state had two homes.
    assert m.plot_recording is framework_tools.plot_recording
    assert m.battery_emulator_stop is framework_tools.battery_emulator_stop


def test_framework_tools_have_docstrings():
    """The docstring is the tool description a model sees and the CLI's
    ``--help`` text, so an empty one degrades two interfaces at once."""
    from benchctrl import framework_tools

    for fn in framework_tools._TOOLS:
        assert fn.__doc__ and fn.__doc__.strip(), f"{fn.__name__} missing docstring"


def test_every_public_function_in_framework_tools_is_in_the_tuple():
    """Closes a blind spot the parity test structurally cannot see.

    A function defined here but left out of ``_TOOLS`` is never registered
    *and* never generated, so both routes agree it does not exist and parity
    passes. Every driver catches this with a "tools cover the driver surface"
    test; framework tools have no driver to compare against, so the module
    itself is the reference.

    Underscore-prefixed helpers and imported symbols are excluded — only
    functions actually defined in this module.
    """
    import inspect

    from benchctrl import framework_tools

    declared = {fn.__name__ for fn in framework_tools._TOOLS}
    public = {
        name
        for name, obj in vars(framework_tools).items()
        if inspect.isfunction(obj)
        and not name.startswith("_")
        and obj.__module__ == framework_tools.__name__
        and name != "register_mcp_tools"
    }
    assert public - declared == set(), (
        f"defined in framework_tools but absent from _TOOLS, so invisible to "
        f"both the MCP server and the CLI: {sorted(public - declared)}"
    )
    assert declared - public == set(), (
        f"in _TOOLS but not a function defined in this module: {sorted(declared - public)}"
    )

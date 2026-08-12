"""The local/remote/sim seam, and the config layering behind it.

The load-bearing assertion in this file is that *nothing changes* when
nothing is configured. Everything else is opt-in.
"""

from __future__ import annotations

import json

import pytest

from benchctrl import config as cfgmod
from benchctrl import session
from benchctrl.config import Config, DeviceConfig, EndpointConfig
from benchctrl.exceptions import BenchValueError


@pytest.fixture(autouse=True)
def _clean_session():
    session.reset_for_tests()
    yield
    session.reset_for_tests()


# --------------------------------------------------------------------------
# The default must be untouched behaviour
# --------------------------------------------------------------------------


def test_unconfigured_resolve_is_exactly_the_opener():
    sentinel = object()
    calls = []

    def opener(**kwargs):
        calls.append(kwargs)
        return sentinel

    assert session.resolve("otii_arc", opener=opener) is sentinel
    assert calls == [{}]


def test_unconfigured_mode_is_local_for_every_device():
    for key in cfgmod.DEVICE_KEYS:
        assert session.mode_for(key) == "local"
        assert not session.is_remote(key)


def test_empty_config_is_all_local():
    assert Config().is_all_local


def test_resolve_forwards_open_kwargs():
    seen = {}

    def opener(**kwargs):
        seen.update(kwargs)
        return "device"

    session.resolve(
        "eastwood_qr10x", opener=opener, open_kwargs={"port": "/dev/ttyUSB9"}
    )
    assert seen == {"port": "/dev/ttyUSB9"}


# --------------------------------------------------------------------------
# Address parsing
# --------------------------------------------------------------------------


@pytest.mark.parametrize(
    "text,expected",
    [
        ("bench.local", ("bench.local", 9737)),
        ("bench.local:1234", ("bench.local", 1234)),
        ("10.0.0.4:9999", ("10.0.0.4", 9999)),
        ("[::1]:8080", ("::1", 8080)),
        ("[fe80::1]", ("fe80::1", 9737)),
    ],
)
def test_parse_address(text, expected):
    assert cfgmod.parse_address(text) == expected


@pytest.mark.parametrize("bad", ["", ":9737", "host:abc", "host:0", "host:70000"])
def test_parse_address_rejects_garbage(bad):
    with pytest.raises(BenchValueError):
        cfgmod.parse_address(bad)


# --------------------------------------------------------------------------
# Endpoint validation
# --------------------------------------------------------------------------


def test_deadman_must_exceed_heartbeat():
    """Otherwise a perfectly healthy link trips the safety governor."""
    with pytest.raises(BenchValueError, match="deadman"):
        EndpointConfig(host="h", heartbeat_s=10.0, deadman_s=5.0)


def test_endpoint_dict_never_leaks_the_token():
    ep = EndpointConfig(host="h", token="super-secret")
    assert "super-secret" not in json.dumps(ep.to_dict())


def test_unknown_device_mode_rejected():
    with pytest.raises(BenchValueError):
        DeviceConfig(mode="teleport")


# --------------------------------------------------------------------------
# build() — the flag-shaped entry point
# --------------------------------------------------------------------------


def test_remote_binds_every_device():
    cfg = cfgmod.build(remote="bench.local:9000", token="t")
    for key in cfgmod.DEVICE_KEYS:
        assert cfg.mode_for(key) == "remote"
    assert cfg.endpoint_for("otii_arc").port == 9000
    assert not cfg.is_all_local


def test_local_devices_carve_exceptions_out_of_remote():
    """The headline deployment: Arc on the bench, Rigols on this laptop."""
    cfg = cfgmod.build(
        remote="bench.local",
        local_devices=["rigol_dl3031a", "rigol_dp2031"],
    )
    assert cfg.mode_for("otii_arc") == "remote"
    assert cfg.mode_for("eastwood_qr10x") == "remote"
    assert cfg.mode_for("rigol_dl3031a") == "local"
    assert cfg.mode_for("rigol_dp2031") == "local"


def test_sim_devices_carve_exceptions_too():
    cfg = cfgmod.build(remote="bench.local", sim_devices=["otii_arc"])
    assert cfg.mode_for("otii_arc") == "sim"
    assert cfg.mode_for("eastwood_qr10x") == "remote"


def test_build_rejects_unknown_device_key():
    with pytest.raises(BenchValueError, match="unknown device key"):
        cfgmod.build(remote="h", local_devices=["keithley_2400"])


def test_remote_device_without_endpoint_is_a_loud_error():
    """Silently falling back to local would drive the wrong hardware."""
    cfg = Config(
        endpoints={"a": EndpointConfig(host="a"), "b": EndpointConfig(host="b")},
        devices={"otii_arc": DeviceConfig(mode="remote")},
    )
    with pytest.raises(BenchValueError, match="names no endpoint"):
        cfg.endpoint_for("otii_arc")


def test_single_endpoint_is_inferred():
    cfg = Config(
        endpoints={"only": EndpointConfig(host="bench")},
        devices={"otii_arc": DeviceConfig(mode="remote")},
    )
    assert cfg.endpoint_for("otii_arc").host == "bench"


def test_undefined_endpoint_reference_is_an_error():
    cfg = Config(
        endpoints={"a": EndpointConfig(host="a")},
        devices={"otii_arc": DeviceConfig(mode="remote", endpoint="nope")},
    )
    with pytest.raises(BenchValueError, match="undefined endpoint"):
        cfg.endpoint_for("otii_arc")


# --------------------------------------------------------------------------
# Layering
# --------------------------------------------------------------------------


def test_env_is_ignored_when_unset():
    assert cfgmod.load_env({}) is None


def test_env_builds_a_config():
    cfg = cfgmod.load_env(
        {
            "BENCHCTRL_REMOTE": "bench.local:9100",
            "BENCHCTRL_TOKEN": "tok",
            "BENCHCTRL_LOCAL_DEVICES": "rigol_dp2031",
        }
    )
    assert cfg.mode_for("otii_arc") == "remote"
    assert cfg.mode_for("rigol_dp2031") == "local"
    assert cfg.endpoint_for("otii_arc").token == "tok"


def test_missing_config_file_is_all_local(tmp_path):
    assert cfgmod.load_file(tmp_path / "nope.json").is_all_local


def test_config_file_round_trip(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": {"bench": {"host": "uno.local", "port": 9737}},
                "devices": {
                    "otii_arc": {"mode": "remote", "endpoint": "bench"},
                    "rigol_dl3031a": {"mode": "local"},
                },
            }
        )
    )
    cfg = cfgmod.load_file(path)
    assert cfg.mode_for("otii_arc") == "remote"
    assert cfg.mode_for("rigol_dl3031a") == "local"
    assert cfg.endpoint_for("otii_arc").host == "uno.local"


def test_config_file_ignores_unknown_device_keys(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(json.dumps({"devices": {"nonesuch": {"mode": "remote"}}}))
    assert cfgmod.load_file(path).is_all_local


def test_cli_overrides_env_overrides_file(tmp_path):
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps(
            {
                "endpoints": {"bench": {"host": "from-file"}},
                "devices": {"otii_arc": {"mode": "remote", "endpoint": "bench"}},
            }
        )
    )
    env = {"BENCHCTRL_REMOTE": "from-env", "BENCHCTRL_CONFIG": str(path)}
    cli = cfgmod.build(remote="from-cli")

    from_file = cfgmod.resolve(env={"BENCHCTRL_CONFIG": str(path)})
    assert from_file.endpoint_for("otii_arc").host == "from-file"

    from_env = cfgmod.resolve(env=env)
    assert from_env.endpoint_for("otii_arc").host == "from-env"

    from_cli = cfgmod.resolve(cli=cli, env=env)
    assert from_cli.endpoint_for("otii_arc").host == "from-cli"


def test_flag_endpoint_does_not_erase_a_file_token(tmp_path):
    """A --remote flag shouldn't silently drop the token from the config."""
    path = tmp_path / "config.json"
    path.write_text(
        json.dumps({"endpoints": {"bench": {"host": "h", "token": "kept"}}})
    )
    merged = cfgmod.resolve(
        cli=cfgmod.build(remote="other.local"),
        env={"BENCHCTRL_CONFIG": str(path)},
    )
    assert merged.endpoints["bench"].token == "kept"


# --------------------------------------------------------------------------
# Sim mode through the seam
# --------------------------------------------------------------------------


def test_sim_mode_uses_a_registered_factory():
    session.configure(cfgmod.build(sim_devices=["otii_arc"]))
    session.register_sim_factory("otii_arc", lambda **kw: "simulated!")
    assert session.resolve("otii_arc", opener=lambda **kw: "real") == "simulated!"


def test_register_sim_factory_rejects_unknown_key():
    with pytest.raises(BenchValueError):
        session.register_sim_factory("nope", lambda **kw: None)


def test_sim_mode_default_factory_returns_a_real_driver():
    """``mode="sim"`` must exercise the production driver, not a mock."""
    from benchctrl.drivers.otii_arc import OtiiArc
    from benchctrl.interfaces import SourceMeasurementUnit

    session.configure(cfgmod.build(sim_devices=["otii_arc"]))
    smu = session.resolve("otii_arc", opener=OtiiArc.open)
    try:
        assert isinstance(smu, OtiiArc)
        assert isinstance(smu, SourceMeasurementUnit)
        assert smu.is_connected
        smu.set_voltage(2.5)
        assert smu.voltage == pytest.approx(2.5)
    finally:
        smu.close()


def test_closing_a_sim_driver_releases_the_simulator():
    from benchctrl.drivers.otii_arc import OtiiArc

    session.configure(cfgmod.build(sim_devices=["otii_arc"]))
    smu = session.resolve("otii_arc", opener=OtiiArc.open)
    sim = smu._benchctrl_sim
    smu.close()
    assert not sim.link.is_open


def test_mcp_get_smu_honours_sim_mode():
    """The seam works through the real MCP accessor, not just session."""
    from benchctrl.drivers.otii_arc import mcp_tools as arc_tools

    session.configure(cfgmod.build(sim_devices=["otii_arc"]))
    try:
        smu = arc_tools._get_smu()
        assert smu.is_connected
        assert arc_tools._get_smu() is smu  # cached in the module global
    finally:
        arc_tools._close_smu()


def test_mcp_globals_remain_injectable():
    """Existing tests inject fakes by assigning the module global."""
    from benchctrl.drivers.rigol_dl3031a import mcp_tools as dl_tools

    sentinel = object()
    dl_tools._dl3031a = sentinel
    try:
        assert dl_tools._get_dl3031a() is sentinel
    finally:
        dl_tools._dl3031a = None

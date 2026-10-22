"""``DeviceRegistry.describe()`` names each instrument: serial and binding.

Inventory is keyed by (manufacturer, model, serial), so a device key alone
("rigol_dp2031") cannot tell two benches' supplies apart, or notice that the
supply on this bench was swapped overnight. The serial is read from the
instrument at open; the binding (how it is wired) comes from agent.json,
because only the person who wired the bench knows it.
"""

from __future__ import annotations

import json

import pytest

from benchctrl.agent import main as agent_main
from benchctrl.agent.registry import (
    BINDINGS,
    DeviceRegistry,
    build_default_registry,
    read_serial,
)
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.exceptions import BenchValueError
from benchctrl.net.client import RemoteClient

pytestmark = pytest.mark.plan("CORE-P0.11")

ARC_SIM_ID = "SIM0000000000000000000000000ARC1"


def _row(registry: DeviceRegistry, key: str) -> dict:
    return next(d for d in registry.describe() if d["key"] == key)


# ---------------------------------------------------------------- serial


def test_the_serial_is_read_from_the_instrument_at_open():
    # Three drivers, three places the serial lives: the Arc's device-id query
    # (opened by port, so no USB descriptor), the QR10x's info() method, and
    # the CP2112's serial property.
    registry = build_default_registry(
        ["otii_arc", "eastwood_qr10x", "silabs_cp2112"], simulate=True
    )
    try:
        for key in registry.keys:
            row = _row(registry, key)
            assert ("serial", None) in row.items(), "unopened: unknown, but the key is there"
            registry.open(key)
        assert _row(registry, "otii_arc")["serial"] == ARC_SIM_ID
        assert _row(registry, "eastwood_qr10x")["serial"] == "SIM-QR10X-0001"
        assert _row(registry, "silabs_cp2112")["serial"] == "SIM0CP2112"
        # Kept across a close: it names the instrument last seen under the key.
        registry.close("otii_arc")
        row = _row(registry, "otii_arc")
        assert (row["open"], row["serial"]) == (False, ARC_SIM_ID)
    finally:
        registry.close_all()


class _Probe:
    """Answers one serial probe and records which ones were asked."""

    def __init__(self, **answers):
        self.asked: list[str] = []
        self._answers = answers

    def __getattr__(self, name):
        if name.startswith("_") or name == "asked":
            raise AttributeError(name)
        if name not in self._answers:
            raise AttributeError(name)
        self.asked.append(name)
        answer = self._answers[name]
        if isinstance(answer, BaseException):
            raise answer
        return answer


class _Info:
    def __init__(self, serial):
        self.serial = serial


def test_a_serial_is_found_wherever_the_driver_keeps_it():
    assert read_serial(_Probe(serial=" SN1 ")) == "SN1"
    assert read_serial(_Probe(info=_Info("SN2"))) == "SN2"
    assert read_serial(_Probe(info=lambda: _Info("SN3"))) == "SN3"
    assert read_serial(_Probe(read_identity=lambda: _Info("SN4"))) == "SN4"
    assert read_serial(_Probe(get_device_id=lambda: "SN5")) == "SN5"


def test_the_first_probe_that_answers_wins_and_no_more_are_asked():
    probe = _Probe(info=_Info("USB-SN"), get_device_id=lambda: "WIRE-ID")
    assert read_serial(probe) == "USB-SN"
    assert "get_device_id" not in probe.asked


def test_a_probe_that_raises_or_says_nothing_falls_through_and_never_raises():
    probe = _Probe(
        serial="",
        info=RuntimeError("identity unavailable"),
        read_identity=lambda: _Info(None),
        get_device_id=lambda: "LAST",
    )
    assert read_serial(probe) == "LAST"
    assert probe.asked == ["serial", "info", "read_identity", "get_device_id"]

    def broken():
        raise OSError("port gone")

    assert read_serial(_Probe(info=broken, get_device_id=broken)) is None
    assert read_serial(object()) is None


@pytest.mark.parametrize("key", ["cyberpower_pdu41002", "bench_vision"])
def test_a_device_with_no_serial_in_its_identity_is_never_asked_for_one(key):
    probe = _Probe(read_identity=lambda: _Info("would-be"), get_device_id=lambda: "x")
    assert read_serial(probe, key) is None
    assert probe.asked == []


# ---------------------------------------------------------------- binding


def test_binding_comes_from_config_and_unset_reads_as_none():
    registry = build_default_registry(
        ["otii_arc", "siglent_sdm4065a"],
        simulate=True,
        bindings={"siglent_sdm4065a": "dut_bound"},
    )
    assert _row(registry, "siglent_sdm4065a")["binding"] == "dut_bound"
    assert _row(registry, "otii_arc")["binding"] is None
    # The hardware openers take the same path; registering opens nothing.
    real = build_default_registry(["otii_arc"], bindings={"otii_arc": "routable_source"})
    assert _row(real, "otii_arc") == {
        "key": "otii_arc",
        "open": False,
        "serial": None,
        "binding": "routable_source",
    }


@pytest.mark.parametrize("binding", BINDINGS)
def test_every_documented_binding_is_accepted(binding):
    registry = DeviceRegistry()
    registry.register("otii_arc", lambda **kw: None, binding=binding)
    assert _row(registry, "otii_arc")["binding"] == binding


def test_an_unknown_binding_is_refused_at_registration():
    registry = DeviceRegistry()
    with pytest.raises(BenchValueError, match="dut-bound"):
        registry.register("otii_arc", lambda **kw: None, binding="dut-bound")
    with pytest.raises(BenchValueError):
        registry.register_open("otii_arc", object(), binding="routable")
    assert registry.keys == []


# ---------------------------------------------------------------- agent.json → wire


class _AgentBuiltError(Exception):
    """Stops main() once it has built the agent, with what it built it from."""


def _main_with(tmp_path, monkeypatch, cfg: dict):
    path = tmp_path / "agent.json"
    path.write_text(json.dumps(cfg))
    seen: dict = {}

    def fake_agent(registry, **kwargs):
        seen.update(kwargs, registry=registry)
        raise _AgentBuiltError

    monkeypatch.setattr(agent_main, "BenchAgent", fake_agent)
    try:
        rc = agent_main.main(["--config", str(path), "--simulate", "--no-beacon"])
    except _AgentBuiltError:
        return None, seen
    return rc, seen


def test_agent_json_binding_and_agent_id_reach_the_agent(tmp_path, monkeypatch):
    rc, seen = _main_with(
        tmp_path,
        monkeypatch,
        {
            "devices": ["otii_arc", "siglent_sdm4065a"],
            "binding": {"siglent_sdm4065a": "dut_bound", "otii_arc": "routable_source"},
            "agent_id": "bench-07",
        },
    )
    assert rc is None, "main() returned before building the agent"
    assert seen["agent_id"] == "bench-07"
    registry = seen["registry"]
    assert _row(registry, "siglent_sdm4065a")["binding"] == "dut_bound"
    assert _row(registry, "otii_arc")["binding"] == "routable_source"


@pytest.mark.parametrize(
    "binding",
    [{"siglent_sdm4065a": "dut-bound"}, {"no_such_device": "dut_bound"}],
)
def test_a_bad_binding_in_agent_json_stops_the_agent(tmp_path, monkeypatch, capsys, binding):
    rc, seen = _main_with(tmp_path, monkeypatch, {"binding": binding})
    assert rc == 2
    assert seen == {}, "the agent was built from a config it should have refused"
    assert "binding" in capsys.readouterr().err


def test_serial_and_binding_cross_the_wire_in_welcome():
    from benchctrl.sim.factories import make_otii_arc

    registry = DeviceRegistry()
    registry.register_open("otii_arc", make_otii_arc(), binding="routable_source")
    server = AgentServer(BenchAgent(registry, token="t"), host="127.0.0.1", port=0).start()
    try:
        with RemoteClient(EndpointConfig(host="127.0.0.1", port=server.port, token="t")) as c:
            (row,) = c.welcome["devices"]
            assert (row["serial"], row["binding"]) == (ARC_SIM_ID, "routable_source")
    finally:
        server.stop()
        registry.close_all()

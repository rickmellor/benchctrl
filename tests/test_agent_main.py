"""The agent entrypoint: config file -> BenchAgent wiring.

These are deployment tests. A key documented in ``agent.json`` that
``main()`` never forwards is invisible in development — the default happens
to be sane when you launch by hand from a checkout — and only shows up under
systemd, where there is no meaningful cwd.
"""

from __future__ import annotations

import json

import pytest

from benchctrl.agent import blobs as blobs_mod
from benchctrl.agent import main as main_mod


@pytest.fixture
def captured_agent(tmp_path, monkeypatch):
    """Run ``main()`` far enough to construct BenchAgent, then stop.

    Patches the server rather than the agent so the real BenchAgent is built
    with the real kwargs — a mock of BenchAgent would happily accept a
    ``runs_dir`` the production class ignored.
    """
    seen = {}

    class _StopHereError(Exception):
        pass

    def _fake_server(agent, **kwargs):
        seen["agent"] = agent
        seen["server_kwargs"] = kwargs
        raise _StopHereError

    monkeypatch.setattr(main_mod, "AgentServer", _fake_server)

    def run(cfg: dict):
        path = tmp_path / "agent.json"
        path.write_text(json.dumps(cfg))
        path.chmod(0o640)
        with pytest.raises(_StopHereError):
            main_mod.main(["--config", str(path), "--no-beacon"])
        return seen["agent"]

    return run


def test_runs_dir_from_config_reaches_the_run_manager(captured_agent, tmp_path):
    """``runs_dir`` in agent.json is honoured.

    Regression: main() built BenchAgent without runs_dir=, so every run
    bundle landed in ``$CWD/benchctrl-runs`` no matter what the config said.
    """
    want = tmp_path / "board" / "runs"
    agent = captured_agent(
        {"devices": ["otii_arc"], "simulate": True, "runs_dir": str(want)}
    )
    assert agent.runs.runs_dir == want
    assert want.is_dir()


def test_runs_dir_absent_falls_back_to_the_default(captured_agent):
    agent = captured_agent({"devices": ["otii_arc"], "simulate": True})
    from benchctrl.agent.runs import store as store_mod

    assert agent.runs.runs_dir == store_mod.default_runs_dir()


def test_blob_dir_from_config_is_honoured(captured_agent, tmp_path):
    want = tmp_path / "board" / "blobs"
    agent = captured_agent(
        {"devices": ["otii_arc"], "simulate": True, "blob_dir": str(want)}
    )
    # Blobs under the spill threshold stay in memory and never touch the
    # directory, so a small payload would pass whatever blob_dir said.
    big = b"\0" * (blobs_mod.DEFAULT_SPILL_THRESHOLD + 1)
    info = agent.blobs.put(big)
    assert info.path is not None, "payload should have spilled to disk"
    assert info.path.parent == want


def test_llm_base_url_from_config_is_honoured(captured_agent):
    agent = captured_agent(
        {
            "devices": ["otii_arc"],
            "simulate": True,
            "llm_base_url": "http://192.0.2.1:8000/v1",
        }
    )
    assert agent.llm_base_url == "http://192.0.2.1:8000/v1"


def test_safe_stop_exits_without_starting_a_server(tmp_path, monkeypatch):
    """``--safe-stop`` is the ExecStopPost path: disarm and exit.

    It must never bind a port — the replacement process is already coming up
    on it.
    """

    def _boom(*a, **k):  # pragma: no cover - the point is that it isn't called
        raise AssertionError("--safe-stop must not start a server")

    monkeypatch.setattr(main_mod, "AgentServer", _boom)
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"devices": ["otii_arc"], "simulate": True}))
    assert main_mod.main(["--config", str(path), "--safe-stop"]) == 0


def test_world_readable_token_file_warns(tmp_path, caplog):
    """The token is a bench-wide credential; a 0644 config file is a finding."""
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"token": "s3cret"}))
    path.chmod(0o644)
    with caplog.at_level("WARNING"):
        main_mod.load_agent_config(path)
    assert "chmod 640" in caplog.text


def test_documented_0640_token_file_does_not_warn(tmp_path, caplog):
    """0640 root:<service user> is the deployed mode, so it must be quiet.

    Regression: the check masked with 0o077, so group-read tripped it and the
    warning recommended `chmod 640` — the exact mode that had just fired it.
    The service user needs group read.
    """
    path = tmp_path / "agent.json"
    path.write_text(json.dumps({"token": "s3cret"}))
    path.chmod(0o640)
    with caplog.at_level("WARNING"):
        main_mod.load_agent_config(path)
    assert caplog.text == ""

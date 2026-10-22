"""CORE-P0.7: the two measurements ``tools/p07_measure.py`` makes.

The plan item is ``verify: evidence`` — the numbers themselves come off benchpi
and off a rig, which CI has neither of. What CI can hold the tool to is its
contract: one JSON document on stdout, a ``pass`` that agrees with the numbers
beside it, and a verdict that is right for a bench that behaved *and* for one
that did not. So every test here runs the tool in-process against an agent it
owns and a simulated Arc, on loopback only.

The timing is the real thing shrunk: ``wedge`` at a 2 s deadman rather than the
rig's 15 s, a 3 s run rather than 60. The path being walked — the engine stops
touching, the governor notices, the device is driven safe, the run ends
``safe_stopped`` — is the one the rig will walk.
"""

from __future__ import annotations

import contextlib
import importlib.util
import io
import json
import sys
import time
from contextlib import redirect_stdout
from pathlib import Path

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient
from benchctrl.sim.factories import make_otii_arc

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("p07_measure", ROOT / "tools" / "p07_measure.py")
assert _spec and _spec.loader
p07 = importlib.util.module_from_spec(_spec)
sys.modules["p07_measure"] = p07
_spec.loader.exec_module(p07)

pytestmark = pytest.mark.plan("CORE-P0.7")

TOKEN = "p07-measure-token"
DEVICE = "otii_arc"

#: The tool's own contract, checked on every document it prints.
COMMON = ("kind", "pass", "host", "python", "benchctrl_commit")


def _tool(argv: list) -> tuple[int, dict]:
    """Run the tool in-process; return its exit code and the JSON it printed."""
    out = io.StringIO()
    with redirect_stdout(out):
        code = p07.main(argv)
    text = out.getvalue()
    assert text.lstrip().startswith("{"), f"stdout is not one JSON document: {text!r}"
    report = json.loads(text)
    assert all(field in report for field in COMMON), report
    return code, report


def _endpoint(port: int) -> EndpointConfig:
    return EndpointConfig(
        host="127.0.0.1", port=port, token=TOKEN, heartbeat_s=5.0, deadman_s=15.0
    )


#: Seconds past the phase's own clock to wait before reading a run back. The
#: rig default is 20, and this is deliberately far below it: the read-back here
#: lands while the run is still flushing its last chunk, so the tool's own
#: bounded wait for a terminal state is what makes the verdict right.
SETTLE_S = 0.5


def _disconnected_argv(port: int, **overrides) -> list:
    argv = [
        "disconnected-run", "--host", "127.0.0.1", "--port", str(port), "--token", TOKEN,
        "--duration-s", "3", "--settle-s", str(SETTLE_S),
    ]
    for flag, value in overrides.items():
        argv += [f"--{flag.replace('_', '-')}", str(value)]
    return argv


@pytest.fixture()
def bench(tmp_path):
    """An agent on loopback serving one simulated Arc: the stand-in benchpi."""
    registry = DeviceRegistry()
    registry.register_open(DEVICE, make_otii_arc())
    agent = BenchAgent(
        registry, token=TOKEN, deadman_s=15.0, heartbeat_s=5.0, runs_dir=tmp_path / "runs"
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    try:
        yield agent, server.port
    finally:
        with contextlib.suppress(Exception):
            agent.runs.abort_all("test teardown")
        server.stop()
        registry.close_all()


# ---------------------------------------------------------------- wedge


@pytest.mark.plan("CORE-P0.7")
def test_the_wedge_measurement_measures_a_run_watchdog_trip():
    code, report = _tool(["wedge", "--trials", "1", "--deadman-s", "2",
                          "--heartbeat-s", "0.5", "--duration-s", "30"])
    assert report["kind"] == "wedge"
    assert (report["deadman_s"], report["heartbeat_s"]) == (2.0, 0.5)
    trial = report["trials"][0]
    # The three facts the plan asks for, not just a verdict: that the trip was
    # the watchdog's, that the engine ended safe-stopped, and where it landed.
    assert trial["reason"] == "run_watchdog", trial
    assert trial["final_state"] == "safe_stopped", trial
    assert 2.0 <= trial["latency_s"] <= 3.0, trial
    assert report["pass"] and code == 0, report


def test_a_wedge_trial_restores_the_engine_it_patched(monkeypatch):
    """A hook left behind would wedge every run in the process from then on.

    The wedge is a monkeypatch of ``RunEngine._check_rules``, so a trial that
    raises on its way through the wait must restore it — otherwise the tool
    hangs the *next* file in the suite instead of failing in its own. Raised
    out of the wait rather than waited past: the ``finally`` is the same one a
    timeout takes, and this way it costs a second rather than a deadman window.
    """
    from benchctrl.agent.runs.engine import RunEngine

    before = RunEngine._check_rules

    def refuse(client, timeout_s):
        raise OSError("the observer could not get through")

    monkeypatch.setattr(p07, "_await_trip", refuse)
    with pytest.raises(OSError, match="could not get through"):
        _tool(["wedge", "--trials", "1", "--deadman-s", "2", "--heartbeat-s", "0.5",
               "--duration-s", "30"])
    assert RunEngine._check_rules is before


# ------------------------------------------------------- disconnected-run


def test_a_run_completes_with_its_client_gone(bench):
    _, port = bench
    started = time.monotonic()
    code, report = _tool(_disconnected_argv(port))
    assert report["kind"] == "disconnected-run"
    assert report["final_state"] == "complete", report
    assert report["trips_after_submit"] == [], report
    assert report["output_on_after"] is False, report
    assert report["ended_at"], report
    assert code == 0 and report["pass"], report
    # The tool came back only once the run had actually settled. At this
    # settle the read-back lands while the last chunk is still being flushed,
    # so it is the tool's own bounded wait for a terminal state that turns a
    # still-running run into the right answer — a tool that read once and
    # gave up would report a failure here and look exactly like this passing.
    assert time.monotonic() - started >= 3 + SETTLE_S - 0.5


def test_the_report_says_no_when_the_run_is_aborted_while_unattended(bench):
    """Negative control 1: someone stops the run while nobody is watching.

    Without a failing case the ``pass`` field is untested — a measurement that
    always reports success is indistinguishable from one that works. The abort
    is ordinary bench traffic, not a rigged agent: when the submitting client's
    socket closed, its claim went with it, so the client that claims the device
    next is its holder, and a holder may abort without ``force``.
    """
    _, port = bench
    code, report = _tool(_disconnected_argv(port, interrupt_after=1))
    assert report["final_state"] == "aborted", report
    assert not report["trips_after_submit"], report
    assert report["output_on_after"] is False, report
    assert not report["pass"] and code == 1, report


def test_the_report_says_no_when_the_bench_trips_under_the_run(bench):
    """Negative control 2: the run is cut short by a safety trip.

    Closing the device is how one session takes one off another: the agent
    aborts the run driving it and trips it with ``operator``, so the run ends
    ``safe_stopped`` with a trip on the record after submit. The output state
    reads ``None`` here and not ``False`` — a closed device answers no reads,
    and the tool reports what an observer could not see as unknown rather than
    as off. The trip list is what fails this measurement.
    """
    _, port = bench
    code, report = _tool(_disconnected_argv(port, interrupt_after=1, interrupt="close"))
    assert report["final_state"] == "safe_stopped", report
    assert [t["reason"] for t in report["trips_after_submit"]] == ["operator"], report
    assert report["output_on_after"] is None, report
    assert not report["pass"] and code == 1, report


def test_an_observer_cannot_submit_the_run_it_reads_back(bench):
    """Why the read-back is an observer and the submit was not.

    If an observer could submit or claim, "the client was disconnected" would be
    a claim about one socket rather than about the bench: the script could be
    keeping its own run alive.
    """
    port = bench[1]
    observer = RemoteClient(_endpoint(port), observer=True).connect()
    try:
        with pytest.raises(Exception, match="not available to an observer"):
            observer.call("agent.claim", {"device": DEVICE})
        assert observer.call("agent.time", {})["utc"] == pytest.approx(
            time.time(), abs=5.0
        )
    finally:
        observer.close()

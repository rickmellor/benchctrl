"""The LLM supervisor, against a stub inference server.

Built against a stdlib stub rather than real inference, so the guardrails
test in milliseconds instead of two minutes per turn. One on-board smoke
test against real ollama is a separate, manual exercise; these are the tests
that have to pass on every commit.

The through-line: **the deterministic rules are the safety system and the
model is commentary.** Most of this file asserts things the model cannot do.
"""

from __future__ import annotations

import json
import threading
import time
from http.server import BaseHTTPRequestHandler, HTTPServer

import pytest

from benchctrl.agent.llm.client import LLMClient, LLMUnavailable, estimate_tokens
from benchctrl.agent.llm.supervisor import LLMSupervisor
from benchctrl.agent.llm.tools import TOOL_NAMES, ToolExecutor, tool_schemas
from benchctrl.agent.runs.engine import RunEngine
from benchctrl.agent.runs.spec import LLMConfig, Phase, RunSpec, Safety, Sampling
from benchctrl.sim import SimulatedOtiiArc


# --------------------------------------------------------------------------
# Stub inference server
# --------------------------------------------------------------------------


class StubLLM:
    """A minimal OpenAI-compatible server with scripted responses."""

    def __init__(self) -> None:
        self.reply_text = "Phase looked normal."
        self.tool_calls: list[dict] = []
        self.delay_s = 0.0
        self.requests: list[dict] = []
        self.fail = False

        stub = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):  # silence
                pass

            def do_GET(self):
                if stub.fail:
                    self.send_error(500)
                    return
                self._json({"data": [{"id": "stub-model"}]})

            def do_POST(self):
                if stub.fail:
                    self.send_error(500)
                    return
                length = int(self.headers.get("Content-Length", 0))
                body = json.loads(self.rfile.read(length) or b"{}")
                stub.requests.append(body)
                if stub.delay_s:
                    time.sleep(stub.delay_s)
                message = {"role": "assistant", "content": stub.reply_text}
                if stub.tool_calls:
                    message["tool_calls"] = [
                        {
                            "id": f"c{i}",
                            "type": "function",
                            "function": {
                                "name": c["name"],
                                "arguments": json.dumps(c.get("arguments", {})),
                            },
                        }
                        for i, c in enumerate(stub.tool_calls)
                    ]
                self._json(
                    {
                        "choices": [{"message": message, "finish_reason": "stop"}],
                        "usage": {"prompt_tokens": 120, "completion_tokens": 25},
                    }
                )

            def _json(self, payload):
                data = json.dumps(payload).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)

        self.server = HTTPServer(("127.0.0.1", 0), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    @property
    def base_url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}/v1"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()


@pytest.fixture()
def stub():
    s = StubLLM()
    yield s
    s.close()


@pytest.fixture()
def engine(tmp_path):
    from benchctrl.drivers.otii_arc import OtiiArc

    sim = SimulatedOtiiArc()
    sim.start()
    smu = OtiiArc.open(sim.port)
    spec = RunSpec(
        name="llm-test",
        safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
        sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.1,
                          record=False),
        phases=(
            Phase(name="a", mode="cv", setpoints={"voltage_V": 3.0}, duration_s=1.0),
            Phase(name="b", mode="cv", setpoints={"voltage_V": 2.0}, duration_s=1.0),
        ),
        llm=LLMConfig(enabled=True, min_interval_s=30, call_on=("phase_end", "severity")),
    )
    eng = RunEngine(spec, smu, runs_dir=tmp_path, clock_scale=0.05)
    eng._phase_idx = 0
    eng.store.start_phase(0, spec.phases[0])
    yield eng
    smu.close()
    sim.close()


def _supervisor(engine, stub, **overrides) -> LLMSupervisor:
    config = engine.spec.llm
    if overrides:
        from dataclasses import replace

        config = replace(config, **overrides)
    client = LLMClient(base_url=stub.base_url, model="stub-model", timeout_s=10)
    return LLMSupervisor(engine, client, config=config)


# --------------------------------------------------------------------------
# Client
# --------------------------------------------------------------------------


def test_client_reports_availability(stub):
    client = LLMClient(base_url=stub.base_url, model="stub-model")
    assert client.available()
    assert "stub-model" in client.models()


def test_client_reports_unavailable_without_raising():
    client = LLMClient(base_url="http://127.0.0.1:1/v1", model="x", timeout_s=1)
    assert client.available(timeout_s=0.5) is False


def test_unreachable_backend_raises_llm_unavailable():
    client = LLMClient(base_url="http://127.0.0.1:1/v1", model="x", timeout_s=1)
    with pytest.raises(LLMUnavailable):
        client.chat([{"role": "user", "content": "hi"}])


def test_client_parses_tool_calls(stub):
    stub.tool_calls = [{"name": "annotate", "arguments": {"text": "hello"}}]
    client = LLMClient(base_url=stub.base_url, model="stub-model")
    completion = client.chat([{"role": "user", "content": "x"}])
    assert len(completion.tool_calls) == 1
    assert completion.tool_calls[0].name == "annotate"
    assert completion.tool_calls[0].arguments["text"] == "hello"


def test_token_estimate_is_roughly_right():
    assert estimate_tokens("a" * 400) == pytest.approx(100, rel=0.2)


# --------------------------------------------------------------------------
# The tool allowlist
# --------------------------------------------------------------------------


def test_only_eight_tools_are_offered():
    names = {t["function"]["name"] for t in tool_schemas()}
    assert names == set(TOOL_NAMES)
    assert len(names) == 8


def test_no_driver_method_is_reachable():
    """The model must never be able to energise anything."""
    names = {t["function"]["name"] for t in tool_schemas()}
    for forbidden in (
        "set_voltage", "set_output", "set_current_limit", "enable_output",
        "device_call", "set_main_current", "close",
    ):
        assert forbidden not in names
        assert forbidden not in TOOL_NAMES


def test_unknown_tool_is_a_policy_violation(engine):
    executor = ToolExecutor(engine)
    result = executor.execute("set_voltage", {"volts": 5.0})
    assert "error" in result
    assert executor.violations == 1
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert "policy_violation" in kinds


def test_read_tools_work(engine):
    executor = ToolExecutor(engine)
    assert "status" in executor.execute("run_status", {})
    assert "phase" in executor.execute("phase_summary", {"phase_idx": 0})
    assert "events" in executor.execute("recent_events", {})


def test_recent_events_is_clamped(engine):
    for i in range(50):
        engine.store.append_event("noise", payload={"i": i})
    executor = ToolExecutor(engine)
    assert len(executor.execute("recent_events", {"n": 10_000})["events"]) <= 20


def test_metric_window_is_clamped(engine):
    engine.store.append_metric(0, "mv", {"n": 1, "min": 3.3, "max": 3.3,
                                         "mean": 3.3, "last": 3.3})
    executor = ToolExecutor(engine)
    result = executor.execute("metric_window", {"channel": "mv", "seconds": 999_999})
    assert result["seconds"] <= 600.0


def test_annotate_writes_to_the_run_log(engine):
    executor = ToolExecutor(engine)
    assert executor.execute("annotate", {"text": "looks fine"})["ok"]
    assert "looks fine" in engine.store.notes_path.read_text()
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert "llm_note" in kinds


def test_raise_alert_clamps_severity(engine):
    """The model does not get to declare something critical."""
    executor = ToolExecutor(engine)
    result = executor.execute("raise_alert", {"severity": "critical", "text": "!"})
    assert result["severity"] == "warn"


def test_advance_phase_is_forward_only(engine):
    executor = ToolExecutor(engine)
    assert executor.execute("advance_phase", {"reason": "done early"})["ok"]
    assert engine._advance_request

    engine._phase_idx = len(engine.spec.phases) - 1
    result = executor.execute("advance_phase", {"reason": "again"})
    assert "error" in result
    assert "last phase" in result["error"]


def test_abort_run_is_monotone_toward_safety(engine):
    executor = ToolExecutor(engine)
    assert executor.execute("abort_run", {"reason": "pointless"})["ok"]
    assert engine._abort_reason.startswith("llm:")


# --------------------------------------------------------------------------
# The supervisor
# --------------------------------------------------------------------------


def test_turn_annotates_the_run(engine, stub):
    stub.reply_text = "Voltage held steady at 3.3 V throughout."
    sup = _supervisor(engine, stub)
    sup._turn("phase_end", 0)
    notes = [
        e for e in engine.store.events_since(0)
        if e["kind"] == "llm_note"
    ]
    assert notes
    assert "3.3 V" in notes[-1]["data"]["text"]


def test_prompt_never_contains_raw_samples(engine, stub):
    """Raw samples would cost minutes of prompt processing."""
    for i in range(200):
        engine.store.append_metric(0, "mv", {"n": 1, "min": 3.3, "max": 3.3,
                                             "mean": 3.3, "last": 3.3})
    sup = _supervisor(engine, stub)
    prompt = sup._build_prompt("phase_end", 0)
    assert len(prompt) < 4000
    assert prompt.count("3.3") < 20


def test_prompt_is_truncated_to_budget(engine, stub):
    sup = _supervisor(engine, stub, max_prompt_tokens=50)
    sup._turn("phase_end", 0)
    sent = stub.requests[-1]["messages"][-1]["content"]
    assert estimate_tokens(sent) <= 60


def test_rate_limit_skips_rapid_triggers(engine, stub):
    sup = _supervisor(engine, stub, min_interval_s=300)
    sup._maybe_turn("phase_end", 0)
    sup._maybe_turn("phase_end", 0)
    sup._maybe_turn("phase_end", 0)
    assert sup.stats.turns == 1
    assert sup.stats.skipped_rate_limit == 2


def test_backend_failure_does_not_break_the_run(engine, stub):
    stub.fail = True
    sup = _supervisor(engine, stub)
    sup._maybe_turn("phase_end", 0)  # must not raise
    assert sup.stats.failures == 1


def test_three_violations_disable_the_model(engine, stub):
    stub.tool_calls = [{"name": "set_voltage", "arguments": {"v": 5}}]
    sup = _supervisor(engine, stub, min_interval_s=30)
    for _ in range(3):
        sup._last_turn_at = 0.0
        sup._turn("phase_end", 0)
    assert sup.enabled is False
    assert "violations" in sup.stats.disabled_reason
    kinds = [e["kind"] for e in engine.store.events_since(0)]
    assert "llm_disabled" in kinds


def test_notify_never_blocks_the_tick_loop(engine, stub):
    """A stalled model must not delay a measurement, let alone an abort."""
    stub.delay_s = 5.0
    sup = _supervisor(engine, stub).start()
    try:
        started = time.perf_counter()
        for _ in range(20):
            sup.notify("phase_end", 0)
        elapsed = time.perf_counter() - started
        assert elapsed < 0.1, f"notify() blocked for {elapsed:.3f}s"
    finally:
        sup.stop(timeout=1.0)


def test_a_stalled_model_does_not_delay_a_run(tmp_path, stub):
    """The headline safety property of the whole layer."""
    from benchctrl.drivers.otii_arc import OtiiArc

    stub.delay_s = 30.0  # far longer than the run itself
    sim = SimulatedOtiiArc()
    sim.start()
    smu = OtiiArc.open(sim.port)
    try:
        spec = RunSpec(
            name="stall-test",
            safety=Safety(max_voltage_V=4.0, max_current_A=0.5, max_duration_s=600),
            sampling=Sampling(channels=("mv",), chunk_s=60, metric_period_s=0.1,
                              record=False),
            phases=tuple(
                Phase(name=f"p{i}", mode="cv", setpoints={"voltage_V": 2.0},
                      duration_s=1.0)
                for i in range(3)
            ),
            llm=LLMConfig(enabled=True, min_interval_s=30, call_on=("phase_end",)),
        )
        eng = RunEngine(spec, smu, runs_dir=tmp_path, clock_scale=0.05)
        sup = _supervisor(eng, stub)
        eng.attach_llm(sup.start())

        started = time.monotonic()
        eng.start()
        assert eng.join(timeout=30), "run blocked on the model"
        elapsed = time.monotonic() - started
        sup.stop(timeout=1.0)

        assert elapsed < 15.0, f"run took {elapsed:.1f}s with a 30 s model stall"
        assert eng.status == "complete"
    finally:
        smu.close()
        sim.close()


def test_disabled_llm_config_never_starts(engine, stub):
    sup = _supervisor(engine, stub, enabled=False)
    assert sup.start()._thread is None
    sup.notify("phase_end", 0)
    assert sup.stats.turns == 0

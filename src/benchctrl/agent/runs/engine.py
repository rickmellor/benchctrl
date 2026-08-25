"""The run engine: executes a spec unattended, durably, safely.

Runs on the bench, in its own thread, and keeps going when the host
disconnects. That is the point of the whole feature — hand it a long
experiment, go away, come back to a complete artifact bundle.

Ordering inside a tick is a safety property, not an implementation detail:

1. sample metrics
2. evaluate the **safety envelope** — an abort here pre-empts everything
3. evaluate rules, emitting events
4. evaluate phase-exit conditions
5. roll a chunk if one is due

The LLM is not in that list. It runs on its own thread, reads what the tick
loop already computed, and may only annotate or request bounded actions. At
~2 minutes per turn it could not participate in step 2 even if it were
allowed to, and pretending otherwise would be the dangerous kind of clever.
"""

from __future__ import annotations

import logging
import threading
import time
from pathlib import Path
from typing import Any, Callable, Optional

from benchctrl.agent.runs import store as store_mod
from benchctrl.agent.runs.rules import (
    EXIT_OFFSET,
    RULE_OFFSET,
    SAFETY_OFFSET,
    RuleEngine,
)
from benchctrl.agent.runs.spec import Phase, RunSpec
from benchctrl.agent.runs.store import RunStore, new_run_id
from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.agent.runs.engine")

TICK_S = 0.25

#: How long one verified outlet switch may take on the PDU's worker.
#: The driver derives its own read-back budget from the outlet's configured
#: ``td_on``/``td_off`` (3 s as shipped) plus a 3 s margin, so a worker timeout
#: below that would pre-empt the driver's own verification and report a
#: *timeout* where the truthful answer is "the contactor did not move" — two
#: failures with different remedies. Sized well clear of it.
PDU_SWITCH_TIMEOUT_S = 30.0


class RunEngine:
    """Executes one :py:class:`RunSpec` against one device."""

    def __init__(
        self,
        spec: RunSpec,
        device: Any,
        *,
        runs_dir: Optional[Path] = None,
        worker=None,
        governor=None,
        on_event: Optional[Callable[[dict], None]] = None,
        agent_version: str = "",
        clock_scale: float = 1.0,
        pdu=None,
        pdu_worker=None,
    ) -> None:
        self.spec = spec
        self.device = device
        self.worker = worker
        #: The switched PDU, if this run's phases switch mains. A *second*
        #: device, on its own key and its own worker — the run's `device` is the
        #: instrument being driven. Absent unless the spec asks for outlets, so
        #: an ordinary run never touches mains even if a PDU is on the bench.
        self.pdu = pdu
        self.pdu_worker = pdu_worker
        self.governor = governor
        self.on_event = on_event
        #: Compresses phase durations for tests. Never touches the safety
        #: envelope, which is evaluated against real measurements.
        self.clock_scale = max(clock_scale, 1e-6)

        # Refused here rather than at the phase that switches, and before the
        # run directory exists: a spec that power-cycles a DUT and silently
        # skipped every switch would run to "complete" having measured a DUT
        # that was never rebooted. Wrong data reported as good data.
        if spec.switched_outlets and pdu is None:
            raise BenchValueError(
                f"spec switches mains outlets {sorted(spec.switched_outlets)} "
                f"but no PDU was supplied to the run engine. A run that cannot "
                f"switch is not a run with switching skipped."
            )

        self.run_id = new_run_id(spec.name)
        base = Path(runs_dir) if runs_dir else store_mod.default_runs_dir()
        self.store = RunStore(base / self.run_id, self.run_id)
        self.store.create(spec, agent_version=agent_version)

        self.rules = RuleEngine()
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._abort_reason = ""
        self._advance_request = ""
        self._phase_idx = -1
        self._recording = None
        self._chunk_started = 0.0
        self._llm = None
        self.status = store_mod.STATUS_PENDING

    # --- lifecycle ------------------------------------------------------

    def start(self) -> "RunEngine":
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name=f"run-{self.run_id}", daemon=True
        )
        self._thread.start()
        return self

    def abort(self, reason: str = "operator") -> None:
        self._abort_reason = reason
        self._stop.set()

    def request_advance(self, reason: str = "requested") -> None:
        """End the current phase early and continue to the next.

        Forward-only, within the phase list declared before the run started.
        This is the only control the LLM supervisor has over sequencing, and
        it cannot repeat a phase, skip backwards, or extend the run.
        """
        self._advance_request = reason or "requested"

    def join(self, timeout: Optional[float] = None) -> bool:
        if self._thread is None:
            return True
        self._thread.join(timeout=timeout)
        return not self._thread.is_alive()

    @property
    def is_running(self) -> bool:
        return self._thread is not None and self._thread.is_alive()

    def attach_llm(self, supervisor) -> None:
        """Attach an advisory supervisor. Never on the tick path."""
        self._llm = supervisor

    # --- the run --------------------------------------------------------

    def _run(self) -> None:
        self.status = store_mod.STATUS_RUNNING
        self.store.set_status(store_mod.STATUS_RUNNING)
        self._emit("run_start", payload={"spec_sha256": self.spec.sha256})
        started = time.monotonic()

        try:
            for idx, phase in enumerate(self.spec.phases):
                if self._stop.is_set():
                    break
                if (time.monotonic() - started) > self.spec.safety.max_duration_s:
                    self._finish(
                        store_mod.STATUS_SAFE_STOPPED, "safety.max_duration_s exceeded"
                    )
                    return
                self._run_phase(idx, phase)
                if self._abort_reason:
                    break
            if self._abort_reason:
                terminal = (
                    store_mod.STATUS_SAFE_STOPPED
                    if self._abort_reason.startswith("safety")
                    else store_mod.STATUS_ABORTED
                )
                self._finish(terminal, self._abort_reason)
            else:
                self._finish(store_mod.STATUS_COMPLETE, "all phases complete")
        except Exception as exc:  # noqa: BLE001
            log.exception("run %s failed", self.run_id)
            self._emit("run_error", severity="critical", payload={"error": repr(exc)})
            self._finish(store_mod.STATUS_ERRORED, repr(exc))

    def _run_phase(self, idx: int, phase: Phase) -> None:
        self._phase_idx = idx
        self.store.start_phase(idx, phase)
        self._emit(
            "phase_start",
            phase_idx=idx,
            payload={"name": phase.name, "mode": phase.mode, "setpoints": phase.setpoints},
        )
        self.rules.reset_range(EXIT_OFFSET, len(phase.exit))
        self._advance_request = ""

        self._apply_setpoints(phase)
        if phase.records and self.spec.sampling.record:
            self._start_chunk()

        deadline = (
            time.monotonic() + phase.duration_s * self.clock_scale
            if phase.duration_s > 0
            else float("inf")
        )
        next_metric = 0.0
        exit_reason = ""

        while not self._stop.is_set():
            now = time.monotonic()
            if now >= deadline:
                exit_reason = "duration elapsed"
                break

            # Checked on the tick loop, not applied from the LLM thread, so
            # a phase transition never races a measurement.
            if self._advance_request:
                exit_reason = f"advanced early: {self._advance_request}"
                self._advance_request = ""
                break

            if now >= next_metric:
                next_metric = now + self.spec.sampling.metric_period_s * self.clock_scale
                values = self._sample_metrics(idx)

                # 1. Safety first. An abort here pre-empts everything else.
                breach = self._check_safety(values)
                if breach:
                    exit_reason = breach
                    self._abort_reason = f"safety: {breach}"
                    break

                # 2. Rules — observation, never control.
                self._check_rules(idx, values)

                # 3. Phase exit.
                hit = self._check_exit(phase, values)
                if hit:
                    exit_reason = hit
                    break

            if self._chunk_due():
                self._roll_chunk(idx)

            self._stop.wait(TICK_S * self.clock_scale)

        if self._stop.is_set() and not exit_reason:
            exit_reason = self._abort_reason or "stopped"

        self._end_chunk(idx)
        self._idle_device()
        self.store.end_phase(
            idx,
            status=store_mod.STATUS_COMPLETE if not self._abort_reason else store_mod.STATUS_ABORTED,
            exit_reason=exit_reason,
        )
        self._emit(
            "phase_end", phase_idx=idx, payload={"name": phase.name, "reason": exit_reason}
        )
        self._notify_llm("phase_end", idx)

    # --- device interaction ---------------------------------------------

    def _submit(self, fn, label: str):
        if self.worker is None:
            return fn()
        return self.worker.submit(fn, label=label)

    def _apply_setpoints(self, phase: Phase) -> None:
        # Re-check against the envelope even though the spec was validated:
        # this is the last gate before the output is energised.
        self.spec.safety.check_setpoints(phase.setpoints)
        sp = phase.setpoints

        # Mains **first**, and it is not a preference. An instrument setpoint
        # applied while its DUT is de-powered lands nowhere; energising mains
        # under an already-live output is how an inductive kick reaches a DUT.
        # The order is the same one the governor uses in reverse on a trip.
        self._apply_outlets(phase)

        if phase.mode == "idle":
            self._submit(lambda: self._safe_set("set_output", False), "idle")
            return

        limit = sp.get("current_limit_A")
        if limit is not None:
            self._submit(lambda: self._safe_set("set_current_limit", limit), "limit")
            self._submit(
                lambda: self._safe_set("set_current_limit_enabled", True), "limit_on"
            )
        if sp.get("range"):
            self._submit(lambda: self._safe_set("set_range", sp["range"]), "range")

        if phase.mode == "cv":
            volts = sp.get("voltage_V", 0.0)
            self._submit(lambda: self._safe_set("set_voltage", volts), "voltage")
            self._submit(
                lambda: self._safe_set("set_power_regulation", "voltage"), "regmode"
            )
        elif phase.mode == "cc":
            amps = sp.get("current_A", 0.0)
            self._submit(lambda: self._safe_set("set_main_current", amps), "current")
            self._submit(
                lambda: self._safe_set("set_power_regulation", "current"), "regmode"
            )

        self._submit(lambda: self._safe_set("set_output", True), "output_on")
        if self.governor is not None:
            self.governor.observe_call(
                self.spec.device, "set_output", (True,), {}, session_id="run-engine"
            )

    def _apply_outlets(self, phase: Phase) -> None:
        """Switch this phase's mains outlets, then wait for the DUT to settle.

        Runs on the PDU's own worker, not the instrument's: they are two
        devices, and serialising a 3-second contactor delay behind the
        instrument's queue would stall the measurement path for no reason.

        A failed switch **fails the phase**. It is tempting to log it and carry
        on — the run is unattended and nobody is watching — but a power-cycle
        test whose power cycle did not happen produces data that looks fine and
        describes nothing. `set_outlet_state(verify=True)` raises when the
        contactor never moved, and that exception is allowed to propagate.
        """
        outlets = phase.setpoints.get("outlets") or {}
        if not outlets:
            return
        if self.pdu is None:  # pragma: no cover - refused in __init__
            raise BenchValueError("phase switches outlets but no PDU is attached")

        for idx in sorted(outlets):
            want = outlets[idx]
            state = self._submit_pdu(
                lambda i=idx, w=want: self.pdu.set_outlet_state(i, w, verify=True),
                f"outlet_{idx}",
            )
            # Emitted per outlet, and *after* the verified read-back, so the run
            # record says what the contactor did rather than what was asked. A
            # mains transition missing from the timeline is a hole in the audit
            # trail of a run that power-cycled a DUT.
            self._emit(
                "run_outlet",
                payload={"outlet": idx, "requested": want, "state": bool(state)},
            )

        settle = phase.effective_settle_s * self.clock_scale
        if settle > 0:
            self._emit("run_outlet_settle", payload={"settle_s": settle})
            # `wait`, not `sleep`: an abort arriving during a settle window must
            # not have to outlast it. On a 30 s DUT boot that is the difference
            # between stopping now and stopping in half a minute.
            self._stop.wait(settle)

    def _submit_pdu(self, fn, label: str):
        """Queue work on the PDU's worker, falling back to inline.

        Inline is the local/SDK case and the test case; the worker exists so the
        agent serialises access to the device's single CLI session.
        """
        if self.pdu_worker is None:
            return fn()
        return self.pdu_worker.submit(fn, label=label, timeout=PDU_SWITCH_TIMEOUT_S)

    def _safe_set(self, method: str, *args) -> None:
        fn = getattr(self.device, method, None)
        if fn is None:
            log.debug("device has no %s; skipping", method)
            return
        fn(*args)

    def _idle_device(self) -> None:
        # Idles the *instrument*, never the mains. A phase end is not a reason
        # to de-power a DUT: the next phase almost certainly wants it up, and a
        # run whose every phase boundary power-cycled the DUT would measure
        # boot behaviour and nothing else. Outlets are left exactly where the
        # last phase put them — including at `_finish`, so an aborted run does
        # not silently cut mains. Cutting on a *trip* is the governor's opt-in
        # `panic_outlets` path, which is a different decision by a different
        # component; see `agent/safety.py`.
        try:
            self._submit(lambda: self._safe_set("set_output", False), "output_off")
            if self.governor is not None:
                self.governor.observe_call(
                    self.spec.device, "set_output", (False,), {}, session_id="run-engine"
                )
        except Exception as exc:  # noqa: BLE001
            log.error("could not idle device at phase end: %r", exc)

    # --- sampling -------------------------------------------------------

    def _sample_metrics(self, phase_idx: int) -> dict[str, Optional[float]]:
        """Read one value per channel and persist it as a metric row."""
        values: dict[str, Optional[float]] = {}
        for code in self.spec.sampling.channels:
            value = self._read_channel(code)
            values[code] = value
            if value is not None:
                self.store.append_metric(
                    phase_idx,
                    code,
                    {"n": 1, "min": value, "max": value, "mean": value, "last": value},
                )
        temp = self._board_temperature()
        if temp is not None:
            values["board_temp_C"] = temp
            self.store.append_metric(
                phase_idx,
                "board_temp_C",
                {"n": 1, "min": temp, "max": temp, "mean": temp, "last": temp},
            )
        return values

    def _read_channel(self, code: str) -> Optional[float]:
        try:
            if self._recording is not None:
                buf = self._recording.buffer(code)
                return buf.values[-1] if buf.values else None
            return self._submit(
                lambda: self.device.read_value(code, 1.0), f"read_{code}"
            )
        except Exception as exc:  # noqa: BLE001
            log.debug("could not read %s: %r", code, exc)
            return None

    @staticmethod
    def _board_temperature() -> Optional[float]:
        """Board temperature in Celsius, if the kernel exposes it.

        Sustained inference on four A53 cores throttles, and throttling
        shows up as control-loop jitter. Worth recording alongside the
        measurements rather than discovering later.
        """
        for path in (
            "/sys/class/thermal/thermal_zone0/temp",
            "/sys/class/thermal/thermal_zone1/temp",
        ):
            try:
                raw = Path(path).read_text().strip()
                value = float(raw)
                return value / 1000.0 if value > 200 else value
            except (OSError, ValueError):
                continue
        return None

    # --- evaluation -----------------------------------------------------

    def _check_safety(self, values: dict) -> str:
        envelope = self.spec.safety
        for code, value in values.items():
            if value is None:
                continue
            if code == "mv" and value > envelope.max_voltage_V:
                return f"{code}={value:.4g} exceeds max_voltage_V={envelope.max_voltage_V:g}"
            if code == "mc" and value > envelope.max_current_A:
                return f"{code}={value:.4g} exceeds max_current_A={envelope.max_current_A:g}"
            if code == "board_temp_C" and value > envelope.max_board_temp_C:
                return f"board temperature {value:.1f}C exceeds {envelope.max_board_temp_C:g}C"

        for condition, value in self.rules.evaluate(
            list(envelope.abort_if), values, offset=SAFETY_OFFSET
        ):
            return condition.reason or f"abort_if: {condition.describe()} (={value:.4g})"
        return ""

    def _check_rules(self, phase_idx: int, values: dict) -> None:
        conditions = [r.when for r in self.spec.rules]
        for condition, value in self.rules.evaluate(
            conditions, values, offset=RULE_OFFSET
        ):
            rule = next(r for r in self.spec.rules if r.when is condition)
            self._emit(
                rule.kind,
                severity=rule.severity,
                source="rules",
                phase_idx=phase_idx,
                payload={
                    "text": rule.text or condition.describe(),
                    "condition": condition.describe(),
                    "value": value,
                },
            )
            if rule.severity in ("warn", "alarm", "critical"):
                self._notify_llm("severity", phase_idx)

    def _check_exit(self, phase: Phase, values: dict) -> str:
        for condition, value in self.rules.evaluate(
            list(phase.exit), values, offset=EXIT_OFFSET
        ):
            return condition.reason or f"{condition.describe()} (={value:.4g})"
        return ""

    # --- chunking -------------------------------------------------------

    def _start_chunk(self) -> None:
        try:
            self._recording = self._submit(
                lambda: self.device.start_recording(
                    name=f"{self.spec.name}-p{self._phase_idx}",
                    channels=tuple(self.spec.sampling.channels),
                ),
                "start_recording",
            )
            self._chunk_started = time.monotonic()
        except Exception as exc:  # noqa: BLE001
            log.warning("could not start a recording chunk: %r", exc)
            self._recording = None

    def _chunk_due(self) -> bool:
        if self._recording is None:
            return False
        return (time.monotonic() - self._chunk_started) >= (
            self.spec.sampling.chunk_s * self.clock_scale
        )

    def _roll_chunk(self, phase_idx: int) -> None:
        self._end_chunk(phase_idx)
        self._start_chunk()

    def _end_chunk(self, phase_idx: int) -> None:
        if self._recording is None:
            return
        recording, self._recording = self._recording, None
        try:
            self._submit(self.device.stop_recording, "stop_recording")
        except Exception as exc:  # noqa: BLE001
            log.warning("stop_recording raised: %r", exc)
        try:
            written = self.store.write_chunk(recording, phase_idx)
            self._emit("chunk_written", phase_idx=phase_idx, payload=written)
        except Exception as exc:  # noqa: BLE001
            log.error("could not persist chunk: %r", exc)

    # --- events / finish -------------------------------------------------

    def _emit(
        self,
        kind: str,
        *,
        severity: str = "info",
        source: str = "engine",
        phase_idx: Optional[int] = None,
        payload: Optional[dict] = None,
    ) -> None:
        event = self.store.append_event(
            kind,
            severity=severity,
            source=source,
            phase_idx=self._phase_idx if phase_idx is None else phase_idx,
            payload=payload,
        )
        if self.on_event is not None:
            try:
                self.on_event({"run_id": self.run_id, **event.to_dict()})
            except Exception:  # noqa: BLE001
                log.debug("run event sink raised", exc_info=True)

    def _notify_llm(self, trigger: str, phase_idx: int) -> None:
        if self._llm is None:
            return
        try:
            self._llm.notify(trigger, phase_idx)
        except Exception:  # noqa: BLE001
            log.debug("llm notify raised", exc_info=True)

    def _finish(self, status: str, reason: str) -> None:
        self._end_chunk(self._phase_idx)
        self._idle_device()
        self.status = status
        self.store.set_status(status, stop_reason=reason)
        severity = "critical" if status == store_mod.STATUS_SAFE_STOPPED else "info"
        self._emit("run_end", severity=severity, payload={"status": status, "reason": reason})
        self.store.write_manifest()
        log.info("run %s finished: %s (%s)", self.run_id, status, reason)

    # --- reporting ------------------------------------------------------

    def status_dict(self) -> dict:
        info = self.store.info()
        info.update(
            {
                "running": self.is_running,
                "phase_idx": self._phase_idx,
                "phase_count": len(self.spec.phases),
                "phase_name": (
                    self.spec.phases[self._phase_idx].name
                    if 0 <= self._phase_idx < len(self.spec.phases)
                    else None
                ),
            }
        )
        return info


class RunManager:
    """Tracks the runs an agent has executed."""

    def __init__(self, runs_dir: Optional[Path] = None) -> None:
        self.runs_dir = Path(runs_dir) if runs_dir else store_mod.default_runs_dir()
        self.runs_dir.mkdir(parents=True, exist_ok=True)
        self._engines: dict[str, RunEngine] = {}
        self._lock = threading.RLock()
        self.interrupted = store_mod.reconcile_interrupted(self.runs_dir)

    def submit(self, spec: RunSpec, device, **kwargs) -> RunEngine:
        with self._lock:
            for engine in self._engines.values():
                if engine.is_running and engine.spec.device == spec.device:
                    raise BenchValueError(
                        f"{spec.device} already has run {engine.run_id} in "
                        f"progress; abort it first"
                    )
            engine = RunEngine(spec, device, runs_dir=self.runs_dir, **kwargs)
            self._engines[engine.run_id] = engine
        return engine

    def get(self, run_id: str) -> RunEngine:
        with self._lock:
            engine = self._engines.get(run_id)
        if engine is None:
            raise BenchValueError(f"unknown run {run_id!r}")
        return engine

    def store_for(self, run_id: str) -> RunStore:
        """Open a store for any run on disk, live or finished."""
        with self._lock:
            engine = self._engines.get(run_id)
        if engine is not None:
            return engine.store
        path = self.runs_dir / run_id
        if not (path / "run.db").is_file():
            raise BenchValueError(f"unknown run {run_id!r}")
        return RunStore(path, run_id)

    def list(self) -> list[dict]:
        live = {}
        with self._lock:
            for run_id, engine in self._engines.items():
                live[run_id] = engine.status_dict()
        for info in store_mod.list_runs(self.runs_dir):
            live.setdefault(info["run_id"], info)
        return sorted(live.values(), key=lambda r: r.get("created_utc", ""), reverse=True)

    def abort_all(self, reason: str = "agent shutdown") -> None:
        with self._lock:
            engines = list(self._engines.values())
        for engine in engines:
            if engine.is_running:
                engine.abort(reason)
                engine.join(timeout=10.0)

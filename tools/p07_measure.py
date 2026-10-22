#!/usr/bin/env python3
"""CORE-P0.7 bench measurements: the disconnected run, and the wedged engine.

Two questions the plan asks of the bench, one subcommand each. Each prints
**one JSON document on stdout** (everything else — benchctrl's own logging,
progress — goes to stderr) and exits 0 when the measurement passed, 1 when it
did not, so a rig run is one shell line whose output is an evidence file.

``disconnected-run``
    Submits a run, closes the socket for good, comes back as an observer after
    the clock the run needed, and asks whether the run finished on its own and
    left the bench safe. This one talks to whatever agent ``--host``/``--port``
    name, because the thing being proved is that a bench keeps working when its
    client is gone. On the rig that is benchpi's own agent; the script has no
    bench address built into it and reads no config file.

``wedge``
    Starts an agent **in this process** on 127.0.0.1, wedges its run engine by
    blocking the tick loop, and measures how long the safety governor takes to
    notice. Nothing here can reach the bench: the only socket is a loopback one
    this process opens and closes. The plan's bound is ``deadman_s`` plus one
    second, and the deadman thread polls at 0.5 s, so a healthy trip lands in
    ``[deadman_s, deadman_s + 1.0]``.

A third path exists for tests: call :py:func:`main` in-process with an argv
list and parse what it printed on stdout.

Usage::

    python tools/p07_measure.py disconnected-run --host benchpi --port 9737 --token ...
    python tools/p07_measure.py wedge --trials 5 --deadman-s 15 --heartbeat-s 5
"""

from __future__ import annotations

import argparse
import contextlib
import json
import logging
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import time
from typing import Any, Optional

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.engine import RunEngine
from benchctrl.agent.runs.spec import Phase, RunSpec, Safety, Sampling
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.exceptions import BenchError
from benchctrl.net.client import RemoteClient
from benchctrl.sim.factories import make_otii_arc

#: The device both subcommands drive. On the rig this is the real Arc.
DEVICE = "otii_arc"

#: The token every wedge trial's in-process agent answers to. Nothing outside
#: this process can reach one: the agents bind 127.0.0.1 on a port the OS hands
#: out and close with their trial.
WEDGE_TOKEN = "p07-wedge-in-process"

#: How long past ``deadman_s`` a wedge trial waits before giving up. The pass
#: bound is tighter (``WEDGE_TOLERANCE_S``); this only bounds the wait, so a
#: bench that never trips costs a few extra seconds instead of hanging.
WEDGE_WAIT_MARGIN_S = 10.0

#: The plan's bound, as a number: the trip must land no later than a second
#: past the deadman window.
WEDGE_TOLERANCE_S = 1.0


def log(msg: str, *args: Any) -> None:
    """Progress, to stderr. Stdout carries exactly one JSON document."""
    print(f"p07: {msg % args}" if args else f"p07: {msg}", file=sys.stderr, flush=True)


# --------------------------------------------------------------------- report


def _commit() -> Optional[str]:
    """``git rev-parse --short HEAD``, or None without git or outside a repo."""
    try:
        out = subprocess.run(
            ["git", "rev-parse", "--short", "HEAD"],
            capture_output=True,
            text=True,
            timeout=5,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return out.stdout.strip() or None


def _report(kind: str, fields: dict) -> dict:
    """Stamp one measurement, print it, and say whether it passed.

    ``pass`` is computed here from the fields the subcommand just measured —
    never passed in — so a subcommand cannot report a verdict its own numbers
    do not support.
    """
    report: dict = {"kind": kind}
    report.update(fields)
    report["pass"] = bool(_verdict(kind, report))
    report["host"] = socket.gethostname()
    report["python"] = sys.version.split()[0]
    report["benchctrl_commit"] = _commit()
    print(json.dumps(report, indent=2))
    return report


def _verdict(kind: str, report: dict) -> bool:
    if kind == "disconnected-run":
        return (
            report["final_state"] == "complete"
            and not report["trips_after_submit"]
            # ``None`` means "could not read it", not "it is off". An output
            # state the bench would not confirm is not a pass.
            and report["output_on_after"] is False
        )
    if kind == "wedge":
        trials = report["trials"]
        return bool(trials) and all(
            isinstance(t["latency_s"], (int, float))
            and t["reason"] == "run_watchdog"
            and t["final_state"] == "safe_stopped"
            and report["deadman_s"] <= t["latency_s"] <= report["deadman_s"] + WEDGE_TOLERANCE_S
            for t in trials
        )
    raise ValueError(f"unknown measurement {kind!r}")


def _endpoint(host: str, port: int, token: str, *, heartbeat_s: float, deadman_s: float):
    return EndpointConfig(
        host=host, port=port, token=token, heartbeat_s=heartbeat_s, deadman_s=deadman_s
    )


def _status(client: RemoteClient, run_id: str) -> dict:
    """``run.status``, tolerating an agent whose live run list has moved on."""
    try:
        return client.call("run.status", {"run_id": run_id}) or {}
    except BenchError as exc:
        log("run.status for %s failed: %s", run_id, exc)
        return {}


def _safety(client: RemoteClient) -> dict:
    return ((client.call("agent.status", {}) or {}).get("safety") or {})


def _output_state(client: RemoteClient) -> Optional[bool]:
    """Whether the device is still sourcing, as an observer can see it.

    ``device.read`` is the only route an observer has to a device, and it
    serves properties and non-mutators only. An agent that will not answer is
    reported as ``None`` — unknown — rather than as a reassuring ``False``.
    """
    try:
        value = client.call("device.read", {"device": DEVICE, "method": "output_enabled"})
    except BenchError as exc:
        log("device.read output_enabled failed: %s", exc)
        return None
    return bool(value.get("output_enabled") if isinstance(value, dict) else value)


def _spec(device_key: str, duration_s: float, voltage: float, current_limit: float,
          *, envelope_current_A: float, record: bool, metric_period_s: float) -> RunSpec:
    """The one-phase ``cv`` run both measurements submit.

    The envelope sits deliberately outside the setpoints: ``max_voltage_V`` a
    half-volt clear of what is asked for, and the time limit a minute clear of
    the phase, so nothing in the run can trip it for a reason unrelated to
    whatever is being measured.
    """
    return RunSpec(
        name="p07-measure",
        device=device_key,
        safety=Safety(
            max_voltage_V=voltage + 0.5,
            max_current_A=envelope_current_A,
            max_duration_s=duration_s + 60,
        ),
        sampling=Sampling(
            channels=("mc", "mv"), chunk_s=300.0, metric_period_s=metric_period_s, record=record
        ),
        phases=(
            Phase(
                name="hold",
                mode="cv",
                setpoints={"voltage_V": voltage, "current_limit_A": current_limit},
                duration_s=duration_s,
            ),
        ),
    )


# ------------------------------------------------------- disconnected-run


def disconnected_run(args: argparse.Namespace) -> dict:
    """Run to completion with no client attached, then read back what it did."""
    endpoint = _endpoint(args.host, args.port, args.token, heartbeat_s=5.0, deadman_s=15.0)
    spec = _spec(
        args.device,
        args.duration_s,
        args.voltage,
        args.current_limit,
        envelope_current_A=args.envelope_current_A,
        record=True,
        metric_period_s=1.0,
    )
    log(
        "submitting a %.0f s %s run at %.3f V / %.4f A",
        args.duration_s, args.device, args.voltage, args.current_limit,
    )
    client = RemoteClient(endpoint).connect()
    try:
        client.call("agent.claim", {"device": args.device})
        submitted_at = time.time()
        run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
        safety_at_submit = _safety(client)
        log(
            "run %s submitted at %.3f, %d trip(s) already on the bench's record, armed %s",
            run_id, submitted_at, len(safety_at_submit.get("trips") or []),
            safety_at_submit.get("armed"),
        )
    finally:
        # Everything below depends on this being a real disconnect: socket
        # shut down, heartbeats stopped, the agent watching its own deadman
        # clock with nobody left to answer it.
        client.close()
    readback_at = time.monotonic() + args.duration_s + args.settle_s
    log(
        "client disconnected; the run now has %.0f s of unattended clock",
        args.duration_s + args.settle_s,
    )
    if args.interrupt_after is not None:
        # The negative control, for proving the measurement can fail: something
        # happens to the run while nobody is watching. Either way the second
        # client's traffic is legitimate bench traffic, not a rigged agent —
        # when the submitting client's socket closed, its claim went with it,
        # so whoever claims the device next is its holder.
        time.sleep(max(0.0, args.interrupt_after))
        other = RemoteClient(endpoint).connect()
        try:
            other.call("agent.claim", {"device": args.device})
            if args.interrupt == "abort":
                other.call("run.abort", {"run_id": run_id, "reason": "p07 negative control"})
            else:
                # Closing a device trips it — an ``operator`` trip under the
                # run, which is the other way this measurement must report fail.
                other.call("agent.close", {"device": args.device})
            log("%s of run %s performed while disconnected", args.interrupt, run_id)
        finally:
            other.close()
    remaining = readback_at - time.monotonic()
    log("waiting out the rest of the unattended clock: %.0f s", max(0.0, remaining))
    if remaining > 0:
        time.sleep(remaining)

    observer = RemoteClient(endpoint, observer=True).connect()
    try:
        status = _await_terminal(observer, run_id, args.grace_s)
        trips_now = _safety(observer).get("trips") or []
        output_on = _output_state(observer)
    finally:
        observer.close()

    # "after submit" is a claim about *this* run, so it is measured against the
    # bench's trip ledger as it stood at submit: a trip from some earlier
    # session is not this run's failure. The ledger only ever grows.
    trips_after = trips_now[len(safety_at_submit.get("trips") or []) :]  # noqa: E203
    final_state = status.get("status")
    log(
        "final state %s, %d trip(s) after submit, output on afterwards: %s",
        final_state, len(trips_after), output_on,
    )
    return _report(
        "disconnected-run",
        {
            "run_id": run_id,
            "submitted_at": submitted_at,
            "ended_at": status.get("finished_utc"),
            "final_state": final_state,
            "stop_reason": status.get("stop_reason"),
            "duration_s": args.duration_s,
            "safety_at_submit": safety_at_submit,
            "trips_after_submit": trips_after,
            "output_on_after": output_on,
        },
    )


def _await_terminal(client: RemoteClient, run_id: str, grace_s: float) -> dict:
    """Read the run back, waiting out a grace for one that has not settled.

    A run ends a moment after its phase's own clock — INIT, the phase's device
    setup, the last chunk flushed — so a read-back landing inside that window
    would see ``running`` and call a healthy bench a failure. Bounded by
    ``grace_s`` either way: a run still going past that is the finding, not
    something to wait out.
    """
    deadline = time.monotonic() + grace_s
    while True:
        status = _status(client, run_id)
        if status.get("status") != "running" or time.monotonic() >= deadline:
            return status
        time.sleep(0.25)


# ------------------------------------------------------------- wedge


def _await_trip(client: RemoteClient, timeout_s: float) -> Optional[dict]:
    """Poll the agent's safety ledger for a trip, bounded.

    Polling ``agent.status`` rather than subscribing to events: the trip lives
    in the governor's ledger either way, and a poll cannot miss the record by
    being connected a moment too late — which matters when the thing being
    timed happened while no client was attached at all.
    """
    deadline = time.monotonic() + timeout_s
    while time.monotonic() < deadline:
        trips = _safety(client).get("trips") or []
        if trips:
            return trips[-1]
        time.sleep(0.05)
    return None


def _final_state(agent: BenchAgent, run_id: Optional[str], timeout_s: float) -> str:
    """The run's terminal state, read from the engine this process still holds."""
    if run_id is None:
        return "unknown"
    try:
        engine = agent.runs.get(run_id)
    except BenchError:
        return "unknown"
    engine.join(timeout=timeout_s)
    return str(engine.status)


def _wedge_trial(deadman_s: float, heartbeat_s: float, duration_s: float, n: int) -> dict:
    """One wedged-engine trial, against an agent this function owns and closes.

    The wedge is a monkeypatch of ``RunEngine._check_rules`` — a step on the
    tick loop that sits outside every bounded wait, so blocking it stops the
    loop dead and the engine stops touching its own ``run:<id>`` owner. That is
    precisely the failure the watchdog exists for: an engine that is alive as a
    thread and gone as a heartbeat.
    """
    registry = DeviceRegistry()
    registry.register_open(DEVICE, make_otii_arc())
    # A directory of its own rather than the agent's default runs dir, which on
    # a bench is the real ``~/benchctrl-runs``: a measurement trial has no
    # business appearing in the bench's own run list. Removed with the trial.
    trial_dir = tempfile.mkdtemp(prefix=f"p07-wedge-{n}-")
    agent = BenchAgent(
        registry,
        token=WEDGE_TOKEN,
        deadman_s=deadman_s,
        heartbeat_s=heartbeat_s,
        runs_dir=trial_dir,
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = _endpoint(
        "127.0.0.1", server.port, WEDGE_TOKEN, heartbeat_s=heartbeat_s, deadman_s=deadman_s
    )
    spec = _spec(
        DEVICE, duration_s, 1.0, 0.01, envelope_current_A=0.05, record=False,
        metric_period_s=0.5,
    )
    wedged_at: list[float] = []
    unwedge = threading.Event()
    original = RunEngine._check_rules
    run_id: Optional[str] = None
    trip: Optional[dict] = None

    def wedge(self, phase_idx: int, values: dict) -> None:  # noqa: ANN001 - patched method
        if not wedged_at:
            wedged_at.append(time.time())
        unwedge.wait(60)

    try:
        RunEngine._check_rules = wedge
        try:
            client = RemoteClient(endpoint).connect()
            try:
                client.call("agent.claim", {"device": DEVICE})
                run_id = client.call("run.submit", {"spec": spec.to_dict()})["run_id"]
            finally:
                # The run outlives its client, as it must. From here the only
                # thing that can keep the Arc armed is the engine's own touch —
                # and the wedge stops that too, which is the point.
                client.close()
            observer = RemoteClient(endpoint, observer=True).connect()
            try:
                trip = _await_trip(observer, deadman_s + WEDGE_WAIT_MARGIN_S)
            finally:
                observer.close()
        finally:
            # Restore before unblocking, so a thread that wakes mid-patch
            # resumes into the class it was written against, never into a
            # stale hook. In a finally because a trial that raised would
            # otherwise leave every later run in this process wedged.
            RunEngine._check_rules = original
            unwedge.set()

        latency: Optional[float] = None
        reason: Optional[str] = None
        seen = trip or {}
        if wedged_at and isinstance(seen.get("at"), (int, float)):
            latency = seen["at"] - wedged_at[0]
            reason = seen.get("reason")
        final_state = _final_state(agent, run_id, timeout_s=10.0)
        log("trial %d: latency %s s, reason %s, final state %s", n,
            "none" if latency is None else round(latency, 3), reason, final_state)
        return {"latency_s": latency, "reason": reason, "final_state": final_state}
    finally:
        with contextlib.suppress(Exception):
            agent.runs.abort_all("p07 wedge teardown")
        server.stop()
        registry.close_all()
        shutil.rmtree(trial_dir, ignore_errors=True)


def wedge(args: argparse.Namespace) -> dict:
    trials = [
        _wedge_trial(args.deadman_s, args.heartbeat_s, args.duration_s, n)
        for n in range(1, args.trials + 1)
    ]
    latencies = [t["latency_s"] for t in trials if isinstance(t["latency_s"], (int, float))]
    return _report(
        "wedge",
        {
            "deadman_s": args.deadman_s,
            "heartbeat_s": args.heartbeat_s,
            "duration_s": args.duration_s,
            "trials": trials,
            "latency_min_s": min(latencies) if latencies else None,
            "latency_max_s": max(latencies) if latencies else None,
            "latency_mean_s": sum(latencies) / len(latencies) if latencies else None,
        },
    )


# --------------------------------------------------------------------- cli


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="p07_measure", description="CORE-P0.7 bench measurements",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    dr = sub.add_parser(
        "disconnected-run",
        help="submit a run, disconnect, and read back what the bench did alone",
    )
    dr.add_argument("--host", required=True, help="agent host, e.g. benchpi")
    dr.add_argument("--port", type=int, required=True)
    dr.add_argument("--token", required=True)
    dr.add_argument("--device", default=DEVICE)
    dr.add_argument("--duration-s", dest="duration_s", type=float, default=60.0)
    dr.add_argument("--voltage", type=float, default=1.0)
    dr.add_argument("--current-limit", dest="current_limit", type=float, default=0.01)
    dr.add_argument("--max-current-A", dest="envelope_current_A", type=float, default=0.05)
    dr.add_argument(
        "--interrupt-after",
        dest="interrupt_after",
        type=float,
        default=None,
        metavar="S",
        help="S seconds into the disconnected stretch, do --interrupt to the run from "
        "a second client: the negative control, which must report pass=false",
    )
    dr.add_argument("--interrupt", choices=("abort", "close"), default="abort",
                    help="what the negative control does: run.abort, or close the "
                    "device — which trips it under the run")
    dr.add_argument("--settle-s", dest="settle_s", type=float, default=20.0,
                    help="extra seconds to wait past the run before reading it back")
    dr.add_argument("--grace-s", dest="grace_s", type=float, default=20.0,
                    help="if the run is still going at the read-back, wait this long more")
    dr.set_defaults(fn=disconnected_run)

    w = sub.add_parser("wedge", help="measure the wedged-engine trip time, in this process")
    w.add_argument("--trials", type=int, default=5)
    w.add_argument("--deadman-s", dest="deadman_s", type=float, default=15.0)
    w.add_argument("--heartbeat-s", dest="heartbeat_s", type=float, default=5.0)
    w.add_argument("--duration-s", dest="duration_s", type=float, default=120.0)
    w.set_defaults(fn=wedge)
    return parser


def _quiet_logging() -> None:
    """Send benchctrl's logging to stderr without touching anyone else's.

    The script's contract is one JSON document on stdout, so the library's
    warnings must not land there. Attaching a handler to this package's own
    logger rather than calling ``basicConfig`` keeps the tool honest as a
    library entry point: a test that imports and runs ``main`` still owns the
    root logger's handlers, and a rig run still sees the trip warnings — on
    stderr, beside the tool's own progress lines.
    """
    own = logging.getLogger("benchctrl")
    if not logging.getLogger().handlers and not own.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(levelname)s %(name)s: %(message)s"))
        own.addHandler(handler)
        own.setLevel(logging.WARNING)
        own.propagate = False


def main(argv: Optional[list] = None) -> int:
    _quiet_logging()
    args = _parser().parse_args(argv)
    if getattr(args, "device", DEVICE) != DEVICE:
        # The observer read-back addresses DEVICE by name; refuse a different
        # key rather than quietly reporting another device's output state.
        raise SystemExit(f"p07_measure: only --device {DEVICE} is supported")
    if args.command == "wedge" and args.trials < 1:
        raise SystemExit("p07_measure: --trials must be at least 1")
    if args.command == "wedge" and args.deadman_s <= args.heartbeat_s:
        raise SystemExit("p07_measure: --deadman-s must exceed --heartbeat-s")
    return 0 if args.fn(args)["pass"] else 1


if __name__ == "__main__":
    sys.exit(main())

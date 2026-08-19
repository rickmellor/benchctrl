"""``benchctrl-agent`` — run the bench agent on the machine holding the USB.

Typical board deployment::

    benchctrl-agent --config /etc/benchctrl/agent.json

and for development anywhere, with no instruments attached::

    benchctrl-agent --simulate

``--simulate`` binds every device to a simulator behind a pty, with the
production driver still in the path. That is what makes the whole remote
stack developable and demonstrable without hardware.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import signal
import socket
import sys
import threading
from pathlib import Path
from typing import Optional

from benchctrl._version import __version__
from benchctrl.agent.blobs import BlobStore, default_spill_dir
from benchctrl.agent.registry import build_default_registry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import DEFAULT_PORT, DEVICE_KEYS
from benchctrl.net import auth as authmod
from benchctrl.net.beacon import BeaconTransmitter, avahi_service_xml

log = logging.getLogger("benchctrl.agent")

DEFAULT_CONFIG_PATH = Path("/etc/benchctrl/agent.json")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="benchctrl-agent",
        description="Serve locally-attached instruments to a benchctrl host.",
    )
    p.add_argument("--version", action="version", version=f"benchctrl {__version__}")
    p.add_argument("--config", type=Path, help="agent config JSON")
    p.add_argument("--host", default=None, help="bind address (default 0.0.0.0)")
    p.add_argument("--port", type=int, default=None, help=f"TCP port (default {DEFAULT_PORT})")
    p.add_argument("--token", default=None, help="shared secret (prefer the config file)")
    p.add_argument(
        "--devices",
        default=None,
        help=f"comma-separated device keys to serve (default: all of {','.join(DEVICE_KEYS)})",
    )
    p.add_argument(
        "--simulate",
        action="store_true",
        help="back every device with a simulator behind a pty (no hardware needed)",
    )
    p.add_argument("--no-beacon", action="store_true", help="do not broadcast a discovery beacon")
    p.add_argument("--deadman-s", type=float, default=None)
    p.add_argument("--heartbeat-s", type=float, default=None)
    p.add_argument("--max-recording-s", type=float, default=None)
    p.add_argument(
        "--safe-stop",
        action="store_true",
        help="drive every device to its safe state and exit (for ExecStopPost)",
    )
    p.add_argument(
        "--print-avahi-service",
        action="store_true",
        help="print an Avahi service file for /etc/avahi/services/ and exit",
    )
    p.add_argument("--generate-token", action="store_true", help="print a fresh token and exit")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return p


def load_agent_config(path: Optional[Path]) -> dict:
    candidate = path or (DEFAULT_CONFIG_PATH if DEFAULT_CONFIG_PATH.is_file() else None)
    if candidate is None:
        return {}
    try:
        raw = json.loads(Path(candidate).read_text())
    except (OSError, json.JSONDecodeError) as exc:
        log.error("could not read %s: %s", candidate, exc)
        return {}
    # Only "other" bits are a finding. The documented deployment mode is 0640
    # root:<service user>, so masking group bits too made the warning fire on
    # the very mode it recommends — and the service user needs group read.
    mode = Path(candidate).stat().st_mode & 0o007
    if raw.get("token") and mode:
        log.warning(
            "%s holds a token but is world-accessible (mode %o) — run: chmod 640 %s",
            candidate,
            Path(candidate).stat().st_mode & 0o777,
            candidate,
        )
    return raw


def main(argv: Optional[list[str]] = None) -> int:  # noqa: C901
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    if args.generate_token:
        print(authmod.new_token())
        return 0

    cfg = load_agent_config(args.config)

    host = args.host or cfg.get("host", "0.0.0.0")
    port = args.port or int(cfg.get("port", DEFAULT_PORT))
    token = args.token or cfg.get("token") or os.environ.get("BENCHCTRL_TOKEN")
    deadman_s = args.deadman_s or float(cfg.get("deadman_s", 15.0))
    heartbeat_s = args.heartbeat_s or float(cfg.get("heartbeat_s", 5.0))
    max_recording_s = args.max_recording_s or float(cfg.get("max_recording_s", 300.0))
    simulate = args.simulate or bool(cfg.get("simulate", False))

    if args.print_avahi_service:
        print(
            avahi_service_xml(
                port=port,
                agent_version=__version__,
                fingerprint=authmod.token_fingerprint(token) if token else "",
            )
        )
        return 0

    keys = (
        [k.strip() for k in args.devices.split(",") if k.strip()]
        if args.devices
        else cfg.get("devices") or list(DEVICE_KEYS)
    )
    unknown = [k for k in keys if k not in DEVICE_KEYS]
    if unknown:
        print(f"unknown device key(s): {unknown}; valid: {list(DEVICE_KEYS)}", file=sys.stderr)
        return 2

    registry = build_default_registry(
        keys, simulate=simulate, open_kwargs=cfg.get("open") or {}
    )

    if args.safe_stop:
        return _safe_stop(registry)

    if not token:
        log.warning(
            "no token configured — ANY host on this network can drive these "
            "instruments. Generate one with: benchctrl-agent --generate-token"
        )

    agent = BenchAgent(
        registry,
        token=token,
        deadman_s=deadman_s,
        heartbeat_s=heartbeat_s,
        max_recording_s=max_recording_s,
        blob_store=BlobStore(spill_dir=cfg.get("blob_dir") or default_spill_dir()),
        # Under systemd there is no meaningful cwd, so the fallback
        # ``$CWD/benchctrl-runs`` would scatter run bundles wherever the unit
        # happened to start. The config key has to reach the RunManager.
        runs_dir=Path(cfg["runs_dir"]) if cfg.get("runs_dir") else None,
        llm_base_url=cfg.get("llm_base_url", ""),
    )
    server = AgentServer(agent, host=host, port=port).start()

    beacon = None
    if not args.no_beacon and not cfg.get("no_beacon"):
        beacon = BeaconTransmitter(
            host=socket.gethostname(),
            port=port,
            token=token,
            agent_version=__version__,
            device_count_fn=lambda: len(registry.open_devices()),
        ).start()

    log.info(
        "benchctrl-agent %s serving %s on %s:%d%s",
        __version__,
        ",".join(keys),
        *server.address,
        " (SIMULATED)" if simulate else "",
    )

    stop = threading.Event()

    def _handle_signal(signum, _frame):
        log.info("agent: received %s — shutting down", signal.Signals(signum).name)
        stop.set()

    for sig in (signal.SIGINT, signal.SIGTERM):
        signal.signal(sig, _handle_signal)

    try:
        while not stop.wait(1.0):
            pass
    finally:
        if beacon is not None:
            beacon.stop()
        server.stop()
    return 0


def _safe_stop(registry) -> int:
    """Drive every device to its safe state and exit.

    Wired to ``ExecStopPost=`` so a service restart disarms the bench rather
    than leaving an output live across the gap.
    """
    from benchctrl.agent.safety import default_safe_state

    failures = 0
    for key in registry.keys:
        try:
            obj = registry.get(key)
        except Exception as exc:  # noqa: BLE001
            log.info("safe-stop: %s not open (%s)", key, exc)
            continue
        try:
            default_safe_state(obj)
            log.info("safe-stop: %s disarmed", key)
        except Exception as exc:  # noqa: BLE001
            failures += 1
            log.error("safe-stop: %s FAILED: %r", key, exc)
        finally:
            registry.close(key)
    return 1 if failures else 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

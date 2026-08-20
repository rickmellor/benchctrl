"""Serves the FUI: one page, one JSON endpoint, nothing else.

Deliberately small. ``http.server`` from the stdlib, because the board is at 83%
on ``/`` and this needs to add nothing to it — and because the entire attack
surface is a handler that can only read.

What it will not do
-------------------

- **No writes.** There is no route that changes anything. The feed underneath
  holds an *observer* session, so even a bug here cannot arm an instrument; the
  agent would refuse it (:py:data:`benchctrl.agent.server.OBSERVER_METHODS`).
  The e-stop, when the touchscreen arrives, will be a separate deliberate
  mechanism — see ``docs/dashboard.md``.
- **No binding beyond loopback.** Default ``127.0.0.1``. The bench view names
  instruments and arm state; publishing it on the LAN is a decision, not a
  default.
- **No blocking the bench.** Every request reads a snapshot the feed thread
  already assembled. A wedged browser costs the panel its freshness and the
  bench nothing.
"""

from __future__ import annotations

import json
import logging
import mimetypes
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.dashboards.fui.view import build_view

log = logging.getLogger("benchctrl.dashboards.fui")

STATIC_DIR = Path(__file__).parent / "static"

#: How often the browser re-fetches the view. The feed pushes events into the
#: model as they arrive, so this only paces how quickly the *paint* catches up;
#: it is not a poll of the agent.
POLL_MS = 500


class _Handler(BaseHTTPRequestHandler):
    server_version = "benchctrl-fui"

    # Silence per-request logging: on a kiosk this is two requests a second
    # forever, and it would bury anything worth reading in the journal.
    def log_message(self, fmt, *args):  # noqa: A003
        pass

    def do_GET(self) -> None:  # noqa: N802
        path = self.path.split("?", 1)[0]
        if path == "/api/view":
            self._send_view()
        elif path in ("/", "/index.html"):
            self._send_static("index.html")
        else:
            self._send_static(path.lstrip("/"))

    def _send_view(self) -> None:
        feed: AgentFeed = self.server.feed  # type: ignore[attr-defined]
        try:
            view = build_view(feed.snapshot(), feed.bench)
        except Exception:  # noqa: BLE001
            # A renderer that gets no answer shows its own stale banner, which
            # is the honest outcome; inventing a view here would not be.
            log.exception("fui: could not build the view")
            self.send_error(500, "view unavailable")
            return
        body = json.dumps(view).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        # A cached bench status is a lying bench status.
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _send_static(self, name: str) -> None:
        # resolve() then check containment: the only defence that survives
        # "..%2f" and symlinks, unlike stripping "..".
        #
        # is_relative_to rather than a string startswith, which would treat a
        # sibling directory as contained because ".../static-evil" is prefixed by
        # ".../static". Not reachable through today's fixed route table, but the
        # path-shaped check is the one that stays true if a route ever isn't.
        target = (STATIC_DIR / name).resolve()
        if not target.is_relative_to(STATIC_DIR.resolve()) or not target.is_file():
            self.send_error(404, "not found")
            return
        body = target.read_bytes()
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)


class FuiServer:
    """The FUI's HTTP front end, with its feed."""

    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        host: str = "127.0.0.1",
        port: int = 8600,
        feed: Optional[AgentFeed] = None,
    ) -> None:
        self.feed = feed or AgentFeed(endpoint)
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.feed = self.feed  # type: ignore[attr-defined]
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self._httpd.server_address[:2]  # type: ignore[return-value]

    def start(self) -> FuiServer:
        self.feed.start()
        self._thread = threading.Thread(
            target=self._httpd.serve_forever, name="benchctrl-fui-http", daemon=True
        )
        self._thread.start()
        host, port = self.address
        log.info("fui: serving on http://%s:%d", host, port)
        return self

    def stop(self) -> None:
        self._httpd.shutdown()
        self._httpd.server_close()
        if self._thread is not None:
            self._thread.join(timeout=3.0)
        self._thread = None
        self.feed.stop()

    def __enter__(self) -> FuiServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


def main(argv: Optional[list[str]] = None) -> int:  # pragma: no cover - entry point
    import argparse
    import os

    from benchctrl.config import DEFAULT_PORT

    parser = argparse.ArgumentParser(description="benchctrl FUI status display")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8600)
    parser.add_argument(
        "--agent-host",
        default=os.environ.get("BENCHCTRL_DASHBOARD_HOST", "127.0.0.1"),
    )
    parser.add_argument(
        "--agent-port",
        type=int,
        default=int(os.environ.get("BENCHCTRL_DASHBOARD_PORT", DEFAULT_PORT)),
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    endpoint = EndpointConfig(
        host=args.agent_host,
        port=args.agent_port,
        token=os.environ.get("BENCHCTRL_TOKEN", ""),
    )
    server = FuiServer(endpoint, host=args.host, port=args.port).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

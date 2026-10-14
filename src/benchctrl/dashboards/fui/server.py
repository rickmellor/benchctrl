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

The one exception to "one JSON endpoint" is the camera. ``/vision/stream`` and
``/vision/frame.jpg`` relay the vision sidecar's MJPEG stream and still frame
from the bench box's loopback (``BENCHCTRL_VISION_URL``, default
``http://127.0.0.1:8095``), so the page gets video on its own origin — in the
kiosk and through an ssh tunnel alike — without the sidecar's unauthenticated
control port ever being published. Only those two paths are relayed; they can
fire nothing and configure nothing.

The other picture is the function generator's own screen. ``/sdg/screen``
serves the last ``SCDP`` bitmap the feed grabbed from the SDG1032X (a 480x272
BMP) with its age in ``X-Screen-Age``, or 404 when there is no current one. Each
hit also arms the feed's screen poll (:py:meth:`AgentFeed.want_screen`), so the
instrument is only read while a page is showing it: the first fetch answers 404
and the picture is there by the next. Nothing here reaches the instrument
directly — the handler reads a snapshot the feed thread already holds.
"""

from __future__ import annotations

import http.client
import json
import logging
import mimetypes
import os
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Optional
from urllib.parse import urlsplit

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.dashboards.fui.view import build_view

log = logging.getLogger("benchctrl.dashboards.fui")

#: Where the vision sidecar listens on the bench box. The FUI runs beside it.
DEFAULT_VISION_URL = "http://127.0.0.1:8095"

#: The only sidecar paths the FUI relays. Both are reads a human watches.
VISION_RELAY: dict[str, str] = {
    "/vision/stream": "/stream",
    "/vision/frame.jpg": "/frame.jpg",
}

#: The generator's screen grab, served from the feed's memory — no relay, no
#: upstream, and a hit is also what keeps the feed grabbing it.
SCREEN_PATH = "/sdg/screen"

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
        elif path in VISION_RELAY:
            self._relay_vision(VISION_RELAY[path])
        elif path == SCREEN_PATH:
            self._send_screen()
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

    def _send_screen(self) -> None:
        """The generator's last screen grab, or 404 while there is none.

        ``want_screen`` first, unconditionally: a 404 is how the page learns it
        has to ask again, and the ask is what starts the grabs.
        """
        feed: AgentFeed = self.server.feed  # type: ignore[attr-defined]
        feed.want_screen()
        latest = feed.latest_screen
        if latest is None:
            body = json.dumps({"error": "no screen"}).encode("utf-8")
            self.send_response(404)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)
            return
        data, taken = latest
        self.send_response(200)
        self.send_header("Content-Type", "image/bmp")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Screen-Age", f"{max(time.monotonic() - taken, 0.0):.2f}")
        self.end_headers()
        self.wfile.write(data)

    def _relay_vision(self, sidecar_path: str) -> None:
        """Copy one sidecar response through, chunk by chunk, until an end.

        For ``/stream`` that end is the browser leaving; for the still it is the
        sidecar's ``Content-Length``. A sidecar that is down answers 503 with a
        reason, which the page renders as NO FEED rather than a broken image.
        """
        base = urlsplit(self.server.vision_url)  # type: ignore[attr-defined]
        conn = http.client.HTTPConnection(base.hostname or "127.0.0.1", base.port or 80, timeout=5)
        try:
            conn.request("GET", sidecar_path, headers={"Accept": "*/*"})
            upstream = conn.getresponse()
        except OSError as exc:
            conn.close()
            self.send_error(503, f"no vision feed ({exc.__class__.__name__})")
            return
        try:
            if upstream.status != 200:
                self.send_error(503, f"vision sidecar answered {upstream.status}")
                return
            self.send_response(200)
            self.send_header(
                "Content-Type", upstream.getheader("Content-Type") or "application/octet-stream"
            )
            length = upstream.getheader("Content-Length")
            if length:
                self.send_header("Content-Length", length)
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            while True:
                # read1, not read: the stream has no length and never ends, so a
                # full-buffer read would wait for 16 KB of frames before forwarding
                # the first. read1 hands over whatever has arrived.
                chunk = upstream.read1(16384)
                if not chunk:
                    break
                self.wfile.write(chunk)
                if not length:
                    self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            return  # the browser left; the sidecar keeps running
        finally:
            conn.close()

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
        vision_url: Optional[str] = None,
    ) -> None:
        self.feed = feed or AgentFeed(endpoint)
        self.vision_url = vision_url or os.environ.get("BENCHCTRL_VISION_URL") or DEFAULT_VISION_URL
        self._httpd = ThreadingHTTPServer((host, port), _Handler)
        self._httpd.daemon_threads = True
        self._httpd.feed = self.feed  # type: ignore[attr-defined]
        self._httpd.vision_url = self.vision_url  # type: ignore[attr-defined]
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
    parser.add_argument(
        "--vision-url",
        default=os.environ.get("BENCHCTRL_VISION_URL", DEFAULT_VISION_URL),
        help="the vision sidecar whose stream the page shows (loopback on the bench box)",
    )
    args = parser.parse_args(argv)

    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s")
    endpoint = EndpointConfig(
        host=args.agent_host,
        port=args.agent_port,
        token=os.environ.get("BENCHCTRL_TOKEN", ""),
    )
    server = FuiServer(endpoint, host=args.host, port=args.port, vision_url=args.vision_url).start()
    try:
        threading.Event().wait()
    except KeyboardInterrupt:
        pass
    finally:
        server.stop()
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

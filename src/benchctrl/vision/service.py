"""The vision sidecar's HTTP surface — stdlib only, shared by sidecar and sim.

Why this module exists on its own
---------------------------------
The driver (:py:mod:`benchctrl.drivers.bench_vision`) is written against an
HTTP API. If the simulator that CI tests the driver against were a *separate*
implementation of that API, the two could agree with each other and both be
wrong about the real sidecar — the failure ``AGENTS.md`` warns about, where sim
and driver share an author's assumptions. So there is one router, this one,
and it is what both the production sidecar and :py:mod:`benchctrl.sim.vision`
serve. Only the *collaborators* differ: a pylon camera and a Metis detector in
production, a synthetic camera and a canned detector in the simulator.

The collaborators are duck-typed rather than Protocols, per ``CONTRIBUTING.md``
convention 3 (a Protocol arrives with the second concrete instance — there is
exactly one production camera and one production detector). What the router
needs from each is listed on :py:class:`VisionService`.

Wire format
-----------
JSON everywhere except the two browser endpoints (``/frame.jpg``, ``/stream``),
which exist for a human focusing the camera and are never used by the driver.
Errors are ``{"error": {"type": <str>, "message": <str>}}`` with an HTTP status
per type (:py:data:`ERROR_STATUS`); the driver maps ``type`` to an exception
class, so a *type* added here needs a class there.

``seq`` correlation
-------------------
A trigger carries a caller-chosen ``seq``, stamped onto the *next* frame the
camera produces. ``/capture`` triggers, waits for the frame, and refuses to
return a frame whose ``seq`` is not the one requested: a frame from an earlier
trigger, or a free-run frame, is a capture error rather than a plausible-looking
success. That refusal is the property everything downstream (labelled datasets,
LED-state reads against a commanded state) rests on.
"""

from __future__ import annotations

import base64
import json
import logging
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Any, Callable, Optional
from urllib.parse import parse_qs, urlsplit

log = logging.getLogger("benchctrl.vision.service")

#: Bumped when a route's shape changes. Reported in ``/health`` and ``/status``.
API_VERSION = "1"

#: The longest a single request may block waiting for a frame. The agent's
#: worker clamps a device call at 20 s; leaving headroom under that keeps a
#: remote ``read_frame`` from tripping the worker timeout instead of returning
#: a clean timeout error.
MAX_WAIT_S = 10.0

#: Error type -> HTTP status. The driver reads the type, not the status; the
#: status is for humans with curl.
ERROR_STATUS: dict[str, int] = {
    "value": 400,
    "forbidden": 403,
    "not_found": 404,
    "capability": 409,
    "capture": 503,
    "timeout": 504,
    "internal": 500,
}


#: What the LAN-facing *view* listener may serve: a human watching the camera,
#: nothing that fires, configures or reads back structured state. Everything
#: else on that listener is refused with a ``forbidden`` error. The control
#: surface stays on loopback, where the benchctrl agent is the only client.
VIEW_ROUTES: frozenset[tuple[str, str]] = frozenset(
    {("GET", "/stream"), ("GET", "/frame.jpg"), ("GET", "/health")}
)


class ServiceError(Exception):
    """A request the service refuses, with a wire-visible ``type``."""

    type: str = "internal"

    def __init__(self, message: str, *, type: Optional[str] = None) -> None:
        super().__init__(message)
        if type is not None:
            self.type = type

    @property
    def status(self) -> int:
        return ERROR_STATUS.get(self.type, 500)


class ServiceValueError(ServiceError):
    type = "value"


class ServiceCapabilityError(ServiceError):
    """No AIPU / no model here. Distinct so a caller can fall back rather than retry."""

    type = "capability"


class ServiceCaptureError(ServiceError):
    """The camera did not produce the frame that was asked for."""

    type = "capture"


class ServiceTimeoutError(ServiceError):
    type = "timeout"


@dataclass(frozen=True)
class FrameRecord:
    """One frame as the camera hands it to the service.

    ``jpeg`` is what crosses HTTP. ``image`` is an opaque decoded form (a numpy
    array in production, ``None`` in the simulator) that a detector may prefer
    to re-decoding the JPEG; the service never looks inside it.
    """

    frame_id: int
    seq: int
    ts: float
    width: int
    height: int
    crop: Optional[tuple[int, int, int, int]]
    jpeg: bytes
    image: Any = None


class VisionService:
    """Routes HTTP requests to a camera and a detector.

    The camera must provide::

        info() -> dict            model, serial, width, height (sensor)
        status() -> dict          exposure_us, gain_db, fps, triggered, crop,
                                  frame_id, trigger_seq, dropped, width, height
        configure(**kw) -> dict   any of exposure_us, gain_db, fps; returns the
                                  values read back from the camera
        set_crop(x, y, w, h) -> tuple | clear_crop() -> None
        trigger(seq: int) -> None            raises ServiceCaptureError
        latest() -> FrameRecord | None
        wait_for(after_frame_id: int, timeout_s: float) -> FrameRecord | None
        close()

    The detector, if any, must provide::

        present: bool             an AIPU and a model are loaded
        model_name: str | None
        infer(record: FrameRecord, min_conf: float) -> (list[dict], float)
                                  detections as dicts (class_id, label, score,
                                  x1, y1, x2, y2) and the inference time in ms
        status() -> dict          firmware, temp_c, cores, infer_ms_last

    A detector that is absent (``None``) or ``present=False`` makes ``/detect``
    and ``infer=true`` answer with a *capability* error — the documented gap on
    hosts without PCIe.
    """

    def __init__(
        self,
        camera: Any,
        detector: Any = None,
        *,
        version: str = "0",
    ) -> None:
        self.camera = camera
        self.detector = detector
        self.version = version
        self._started = time.monotonic()
        self._lock = threading.RLock()

    # ------------------------------------------------------------ routing

    def handle(self, method: str, target: str, body: bytes = b"") -> tuple[int, str, bytes]:
        """One request -> ``(status, content_type, payload)``.

        Kept free of ``http.server`` types so tests can drive it directly and
        the sidecar/simulator handler is a thin adapter.
        """
        parts = urlsplit(target)
        path = parts.path
        query = {k: v[-1] for k, v in parse_qs(parts.query, keep_blank_values=True).items()}
        try:
            if method == "GET" and path == "/health":
                return _json(200, self._health())
            if method == "GET" and path == "/status":
                return _json(200, self.status())
            if method == "GET" and path == "/config":
                return _json(200, self._config())
            if method == "PUT" and path == "/config":
                return _json(200, self._put_config(_body_json(body)))
            if method in ("GET", "POST") and path == "/trigger":
                params = query if method == "GET" else _body_json(body)
                return _json(200, self._trigger(params))
            if method == "POST" and path == "/capture":
                return _json(200, self._capture(_body_json(body)))
            if method == "GET" and path == "/frame.json":
                return _json(200, self._frame_json(query))
            if method == "GET" and path == "/frame.jpg":
                return self._frame_jpg(query)
            if method == "POST" and path == "/detect":
                return _json(200, self._detect(_body_json(body)))
            if method == "GET" and path == "/crop":
                return _json(200, self._crop(query))
            if method == "GET" and path == "/stream":
                # Streamed by the handler, not here: it never ends.
                return 200, "multipart/x-mixed-replace; boundary=frame", b""
            raise ServiceError(f"no route for {method} {path}", type="not_found")
        except ServiceError as exc:
            return _json(exc.status, {"error": {"type": exc.type, "message": str(exc)}})
        except ValueError as exc:
            # A collaborator refusing a parameter (a crop past the sensor, an
            # exposure the camera cannot do) is the caller's mistake, not ours.
            return _json(ERROR_STATUS["value"], {"error": {"type": "value", "message": str(exc)}})
        except Exception as exc:  # noqa: BLE001 - the wire must always get JSON
            log.exception("vision service: %s %s failed", method, path)
            return _json(500, {"error": {"type": "internal", "message": repr(exc)}})

    # ------------------------------------------------------------ handlers

    def _health(self) -> dict:
        return {
            "ok": True,
            "camera": self.camera is not None,
            "aipu": bool(self.detector is not None and self.detector.present),
            "version": self.version,
            "api": API_VERSION,
        }

    def status(self) -> dict:
        cam = dict(self.camera.status())
        cam.update(self.camera.info())
        det = self.detector
        aipu: dict[str, Any] = {"present": False, "model_name": None}
        if det is not None:
            aipu["present"] = bool(det.present)
            aipu["model_name"] = det.model_name
            try:
                aipu.update(det.status())
            except Exception as exc:  # noqa: BLE001 - status is best effort
                log.debug("detector status failed: %r", exc)
        return {
            "camera": cam,
            "aipu": aipu,
            "sidecar": {
                "version": self.version,
                "api": API_VERSION,
                "uptime_s": round(time.monotonic() - self._started, 3),
            },
        }

    def _config(self) -> dict:
        st = self.camera.status()
        return {k: st.get(k) for k in ("exposure_us", "gain_db", "fps", "crop", "triggered")}

    def _put_config(self, body: dict) -> dict:
        allowed = {"exposure_us", "gain_db", "fps"}
        unknown = set(body) - allowed - {"crop"}
        if unknown:
            raise ServiceValueError(
                f"unknown config keys {sorted(unknown)}; allowed {sorted(allowed)}"
            )
        kw = {}
        for key in allowed:
            if key in body:
                kw[key] = _number(body[key], key)
                if kw[key] <= 0:
                    raise ServiceValueError(f"{key} must be > 0, got {kw[key]}")
        with self._lock:
            if kw:
                self.camera.configure(**kw)
            if "crop" in body:
                crop = body["crop"]
                if crop is None:
                    self.camera.clear_crop()
                else:
                    self.camera.set_crop(*_crop_tuple(crop))
        return self._config()

    def _trigger(self, params: dict) -> dict:
        seq = _int(params.get("seq", -1), "seq")
        with self._lock:
            self.camera.trigger(seq)
        return {"triggered": True, "seq": seq}

    def _capture(self, body: dict) -> dict:
        seq = _int(body.get("seq", -1), "seq")
        wait_s = _wait(body.get("wait_s", 2.0))
        infer = bool(body.get("infer", False))
        min_conf = _conf(body.get("min_conf", 0.4))
        if infer:
            self._require_detector()
        with self._lock:
            before = self.camera.latest()
            after_id = before.frame_id if before is not None else -1
            self.camera.trigger(seq)
            rec = self.camera.wait_for(after_id, wait_s)
        if rec is None:
            raise ServiceTimeoutError(f"no frame within {wait_s:g} s of trigger seq={seq}")
        if rec.seq != seq:
            # A frame arrived, but not ours: an earlier trigger's, or free-run.
            # Returning it as if it were seq would poison every label built on it.
            raise ServiceCaptureError(
                f"frame {rec.frame_id} carries seq={rec.seq}, not the requested {seq}"
            )
        return self._frame_dict(rec, infer=infer, min_conf=min_conf)

    def _frame_json(self, query: dict) -> dict:
        rec = self._wait_query(query)
        infer = query.get("infer", "0") in ("1", "true", "yes")
        if infer:
            self._require_detector()
        return self._frame_dict(rec, infer=infer, min_conf=_conf(query.get("min_conf", 0.4)))

    def _frame_jpg(self, query: dict) -> tuple[int, str, bytes]:
        rec = self._wait_query(query)
        return 200, "image/jpeg", rec.jpeg

    def _wait_query(self, query: dict) -> FrameRecord:
        if "wait" in query:
            after = _int(query["wait"], "wait")
            wait_s = _wait(query.get("wait_s", 5.0))
            rec = self.camera.wait_for(after, wait_s)
            if rec is None:
                raise ServiceTimeoutError(f"no frame after {after} within {wait_s:g} s")
            return rec
        rec = self.camera.latest()
        if rec is None:
            raise ServiceCaptureError("no frame yet — trigger one, or wait for free-run")
        return rec

    def _detect(self, body: dict) -> dict:
        self._require_detector()
        rec = self.camera.latest()
        if rec is None:
            raise ServiceCaptureError("no frame to run detection on")
        min_conf = _conf(body.get("min_conf", 0.4))
        items, infer_ms = self.detector.infer(rec, min_conf)
        return {
            "frame_id": rec.frame_id,
            "seq": rec.seq,
            "model_name": self.detector.model_name,
            "infer_ms": infer_ms,
            "items": list(items),
        }

    def _crop(self, query: dict) -> dict:
        with self._lock:
            if "off" in query:
                self.camera.clear_crop()
            elif all(k in query for k in ("x", "y", "w", "h")):
                self.camera.set_crop(*(_int(query[k], k) for k in ("x", "y", "w", "h")))
        return {"crop": self.camera.status().get("crop")}

    # ------------------------------------------------------------ helpers

    def _require_detector(self) -> None:
        if self.detector is None or not self.detector.present:
            raise ServiceCapabilityError(
                "no AIPU/model on this host — detection is unavailable here "
                "(the Metis needs PCIe: Raspberry Pi 5 or desktop, not an Uno Q)"
            )

    def _frame_dict(self, rec: FrameRecord, *, infer: bool, min_conf: float) -> dict:
        out: dict[str, Any] = {
            "frame_id": rec.frame_id,
            "seq": rec.seq,
            "ts": rec.ts,
            "width": rec.width,
            "height": rec.height,
            "crop": list(rec.crop) if rec.crop else None,
            "jpeg_b64": base64.b64encode(rec.jpeg).decode("ascii"),
            "detections": None,
        }
        if infer:
            items, infer_ms = self.detector.infer(rec, min_conf)
            out["detections"] = {
                "frame_id": rec.frame_id,
                "seq": rec.seq,
                "model_name": self.detector.model_name,
                "infer_ms": infer_ms,
                "items": list(items),
            }
        return out

    def close(self) -> None:
        for obj in (self.camera, self.detector):
            close = getattr(obj, "close", None)
            if callable(close):
                try:
                    close()
                except Exception as exc:  # noqa: BLE001
                    log.debug("vision service: close raised %r", exc)


# ---------------------------------------------------------------- HTTP glue


def make_handler(
    service: VisionService,
    *,
    on_request: Optional[Callable[[str, str], None]] = None,
    allow: Optional[frozenset[tuple[str, str]]] = None,
) -> type:
    """A ``BaseHTTPRequestHandler`` bound to ``service``.

    ``on_request`` is the simulator's hook for its request log; production
    passes nothing. ``allow`` restricts the handler to those ``(method, path)``
    pairs — the view listener passes :py:data:`VIEW_ROUTES` — and everything
    else is refused before it reaches the service.
    """

    class Handler(BaseHTTPRequestHandler):
        server_version = "benchctrl-vision"
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt: str, *args: Any) -> None:  # quiet by default
            log.debug("vision http: " + fmt, *args)

        def _dispatch(self, method: str) -> None:
            if on_request is not None:
                on_request(method, self.path)
            route = urlsplit(self.path).path
            if allow is not None and (method, route) not in allow:
                payload = json.dumps(
                    {
                        "error": {
                            "type": "forbidden",
                            "message": f"{method} {route} is not served on the view port",
                        }
                    }
                ).encode("utf-8")
                self.send_response(ERROR_STATUS["forbidden"])
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)
                return
            if method == "GET" and route == "/stream":
                return self._stream()
            length = int(self.headers.get("Content-Length") or 0)
            body = self.rfile.read(length) if length else b""
            status, ctype, payload = service.handle(method, self.path, body)
            self.send_response(status)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self) -> None:  # noqa: N802 - http.server API
            self._dispatch("GET")

        def do_POST(self) -> None:  # noqa: N802
            self._dispatch("POST")

        def do_PUT(self) -> None:  # noqa: N802
            self._dispatch("PUT")

        def _stream(self) -> None:
            """MJPEG for a human. Ends when the client goes away."""
            self.send_response(200)
            self.send_header("Content-Type", "multipart/x-mixed-replace; boundary=frame")
            self.end_headers()
            last = -1
            try:
                while True:
                    rec = service.camera.wait_for(last, 1.0)
                    if rec is None:
                        continue
                    last = rec.frame_id
                    self.wfile.write(b"--frame\r\nContent-Type: image/jpeg\r\n")
                    self.wfile.write(f"Content-Length: {len(rec.jpeg)}\r\n\r\n".encode())
                    self.wfile.write(rec.jpeg + b"\r\n")
                    self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                return

    return Handler


def serve(
    service: VisionService,
    *,
    bind: str = "127.0.0.1",
    port: int = 8095,
    on_request: Optional[Callable[[str, str], None]] = None,
    allow: Optional[frozenset[tuple[str, str]]] = None,
) -> ThreadingHTTPServer:
    """Bind and return a server; the caller runs ``serve_forever`` (or a thread).

    Two are normally run against one service: the control listener on loopback
    (``allow=None``) and, optionally, a view listener on the LAN with
    ``allow=VIEW_ROUTES``.
    """
    server = ThreadingHTTPServer(
        (bind, port), make_handler(service, on_request=on_request, allow=allow)
    )
    server.daemon_threads = True
    return server


# ---------------------------------------------------------------- parsing


def _json(status: int, payload: dict) -> tuple[int, str, bytes]:
    return status, "application/json", json.dumps(payload).encode("utf-8")


def _body_json(body: bytes) -> dict:
    if not body:
        return {}
    try:
        value = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ServiceValueError(f"body is not JSON: {exc}") from None
    if not isinstance(value, dict):
        raise ServiceValueError("body must be a JSON object")
    return value


def _number(value: Any, name: str) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        raise ServiceValueError(f"{name} must be a number, got {type(value).__name__}")
    try:
        return float(value)
    except ValueError:
        raise ServiceValueError(f"{name} must be a number, got {value!r}") from None


def _int(value: Any, name: str) -> int:
    if isinstance(value, bool):
        raise ServiceValueError(f"{name} must be an integer, got bool")
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ServiceValueError(f"{name} must be an integer, got {value!r}") from None


def _wait(value: Any) -> float:
    wait_s = _number(value, "wait_s")
    if not 0 < wait_s <= MAX_WAIT_S:
        raise ServiceValueError(f"wait_s must be in (0, {MAX_WAIT_S:g}], got {wait_s:g}")
    return wait_s


def _conf(value: Any) -> float:
    conf = _number(value, "min_conf")
    if not 0.0 <= conf <= 1.0:
        raise ServiceValueError(f"min_conf must be in [0, 1], got {conf:g}")
    return conf


def _crop_tuple(value: Any) -> tuple[int, int, int, int]:
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        raise ServiceValueError("crop must be [x, y, w, h] or null")
    x, y, w, h = (_int(v, "crop") for v in value)
    if w <= 0 or h <= 0 or x < 0 or y < 0:
        raise ServiceValueError(f"crop must have x,y >= 0 and w,h > 0, got {value}")
    return x, y, w, h

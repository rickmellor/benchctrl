"""Bench vision driver — a camera and, where the host has one, a Metis NPU.

What this is for
----------------
Reading the bench with a camera: is the DUT's LED on, what colour, what blink
pattern; is the display showing what the run expects. The R&D that proved the
stack (a Basler a2A1920-160uc USB3 Vision camera and an Axelera Metis M.2 AIPU,
YOLOv8n at ~500 FPS on-device) lives in the ``metis`` repository; this driver is
the production seam benchctrl drives it through.

Why the driver is an HTTP client
--------------------------------
The camera SDK (pypylon) and the Axelera runtime are heavy, native, and — in the
runtime's case — only supported inside its own Ubuntu 24.04 container, because it
maps PCIe BARs directly. benchctrl's agent is stdlib + pyserial by design, and
runs on boards that can install nothing else. So the heavy half runs as a
**sidecar** (``benchctrl-vision``, see :py:mod:`benchctrl.vision`) on loopback,
and this driver is a stdlib ``urllib`` client to it. That split is exactly what
makes the device portable across benchctrl's three modes:

* **local** — the driver talks to a sidecar on this host;
* **remote** — the agent on the bench box opens this same driver against *its*
  loopback sidecar, and the host talks to the agent as for any device;
* **sim** — :py:mod:`benchctrl.sim.vision` serves the same HTTP routes with a
  synthetic camera.

The sidecar has no authentication and binds loopback by default; the agent is
the network face, as for every other instrument.

What crosses the wire
---------------------
Frames come back as :py:class:`Frame` with the JPEG bytes inline. Over the agent
link the codec turns anything above 64 KB into a blob reference fetched on
demand and verified by SHA-256, so a 1920x1200 frame (~150-300 KB at q80)
rides the existing blob store unchanged.

``seq`` — the property everything rests on
------------------------------------------
:py:meth:`BenchVision.trigger_capture` fires a software trigger tagged with a
caller-chosen ``seq`` and returns **only** the frame that carries that tag. A
frame from an earlier trigger, or a free-run frame, is a
:py:class:`VisionCaptureError`, never a plausible-looking success. That is what
lets a labelled dataset say "this frame was taken while outlet 3 was commanded
on" and mean it. The hardware trigger cable, when it lands, hits the same
``TriggerSource`` seam in the sidecar and needs nothing here.

``trigger_capture`` is a **mutator** by name (``agent/dispatch.py`` derives the
writer-claim gate from the ``trigger`` prefix): a trigger advances the frame
counter and the pending ``seq``, and two writers interleaving triggers would make
the correlation above undecidable. Reads (``read_frame``, ``detect``,
``read_status``) need no claim, so a dashboard can watch the bench without
taking the camera.

Properties and the snapshot
---------------------------
The agent piggybacks **every** property onto every response. A property that
cost an HTTP round trip each would make one remote call cost fifteen, so all
properties here read from one ``/status`` document cached for
:py:data:`STATUS_TTL_S`; a getter never raises — it logs and returns ``None``,
which is what the snapshot reports for a property it cannot read anyway.
"""

from __future__ import annotations

import base64
import json
import logging
import os
import socket
import threading
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, fields
from typing import Any, Optional

log = logging.getLogger("benchctrl.drivers.bench_vision")

#: Where a sidecar listens when nothing says otherwise. Loopback on purpose.
DEFAULT_URL = "http://127.0.0.1:8095"

#: Environment override for the sidecar URL, read by :py:meth:`BenchVision.open`
#: when no ``url`` is passed. On an agent this is the *agent's* environment.
URL_ENV = "BENCHCTRL_VISION_URL"

#: How long one ``/status`` read serves every property. Short enough that a
#: change made by another client shows within a blink; long enough that the
#: agent's per-response snapshot of all properties costs one request, not
#: fifteen.
STATUS_TTL_S = 0.2

#: Upper bound on any wait this driver will ask the sidecar for. The agent's
#: worker times a device call out at 20 s; this leaves room for the HTTP round
#: trip on top of the wait.
MAX_WAIT_S = 10.0


# ---------------------------------------------------------------- exceptions


class VisionError(RuntimeError):
    """Base for every vision failure."""


class VisionConnectionError(VisionError, ConnectionError):
    """The sidecar could not be reached."""


class VisionProtocolError(VisionError):
    """The sidecar answered, but not in a shape this driver understands."""


class VisionTimeoutError(VisionError, TimeoutError):
    """A wait for a frame, or the HTTP request itself, ran out of time.

    Real here, unlike the CP2112: ``urllib`` has a socket timeout and a
    long-poll has a deadline, so there is a genuine condition to distinguish.
    """


class VisionValueError(VisionError, ValueError):
    """A caller argument is out of range or the wrong type."""


class VisionCapabilityError(VisionError):
    """This host has no AIPU or no model, so detection is unavailable.

    Its own type because the remedy is not a retry: it is the documented gap
    on hosts without PCIe (an Arduino Uno Q), and a caller that gets it should
    fall back to a classical-CV or human channel, not loop.
    """


class VisionCaptureError(VisionError):
    """The camera did not produce the frame that was asked for.

    Not trigger-ready, a failed grab, or — the one that matters — a frame that
    arrived carrying a different ``seq`` than the trigger that was fired.
    """


#: Wire error type -> exception class. A type the sidecar can emit that is not
#: here degrades to :py:class:`VisionProtocolError`, deliberately loud.
_ERROR_TYPES: dict[str, type[VisionError]] = {
    "value": VisionValueError,
    "capability": VisionCapabilityError,
    "capture": VisionCaptureError,
    "timeout": VisionTimeoutError,
    "not_found": VisionProtocolError,
    "internal": VisionError,
}


# ---------------------------------------------------------------- dataclasses


@dataclass(frozen=True)
class Crop:
    """A region of interest in sensor pixels."""

    x: int
    y: int
    w: int
    h: int

    def to_dict(self) -> dict:
        return {"x": self.x, "y": self.y, "w": self.w, "h": self.h}

    @classmethod
    def from_wire(cls, value: Any) -> Optional[Crop]:
        if value is None:
            return None
        if isinstance(value, dict):
            return cls(int(value["x"]), int(value["y"]), int(value["w"]), int(value["h"]))
        x, y, w, h = value
        return cls(int(x), int(y), int(w), int(h))


@dataclass(frozen=True)
class Detection:
    """One box, in frame coordinates (after any crop)."""

    class_id: int
    label: str
    score: float
    x1: int
    y1: int
    x2: int
    y2: int

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class Detections:
    """Every box the model returned for one frame, plus what it cost."""

    frame_id: int
    seq: int
    model_name: Optional[str]
    infer_ms: float
    items: tuple[Detection, ...] = ()

    def __post_init__(self) -> None:
        # The codec delivers tuples as lists; keep the declared type honest.
        object.__setattr__(self, "items", tuple(self.items))

    def __len__(self) -> int:
        return len(self.items)

    def to_dict(self) -> dict:
        return {
            "frame_id": self.frame_id,
            "seq": self.seq,
            "model_name": self.model_name,
            "infer_ms": self.infer_ms,
            "items": [d.to_dict() for d in self.items],
        }


@dataclass(frozen=True)
class Frame:
    """One captured frame: the JPEG, where it came from, and any detections."""

    frame_id: int
    seq: int
    ts: float
    width: int
    height: int
    jpeg: bytes = field(repr=False)
    crop: Optional[Crop] = None
    detections: Optional[Detections] = None

    @property
    def size(self) -> int:
        return len(self.jpeg)

    def to_dict(self) -> dict:
        """Metadata only — the JPEG is never put in a dict that may be logged."""
        return {
            "frame_id": self.frame_id,
            "seq": self.seq,
            "ts": self.ts,
            "width": self.width,
            "height": self.height,
            "bytes": len(self.jpeg),
            "crop": self.crop.to_dict() if self.crop else None,
            "detections": self.detections.to_dict() if self.detections else None,
        }


@dataclass(frozen=True)
class VisionInfo:
    """Identity: which camera, which sidecar, whether an AIPU is behind it."""

    url: str
    sidecar_version: str
    camera_model: Optional[str]
    camera_serial: Optional[str]
    sensor_width: Optional[int]
    sensor_height: Optional[int]
    aipu_present: bool
    aipu_firmware: Optional[str]
    model_name: Optional[str]

    def to_dict(self) -> dict:
        return {f.name: getattr(self, f.name) for f in fields(self)}


@dataclass(frozen=True)
class VisionStatus:
    """The ``/status`` document, typed. What every property reads from."""

    camera_model: Optional[str]
    camera_serial: Optional[str]
    width: Optional[int]
    height: Optional[int]
    exposure_us: Optional[float]
    gain_db: Optional[float]
    fps: Optional[float]
    triggered: Optional[bool]
    crop: Optional[Crop]
    frame_id: Optional[int]
    trigger_seq: Optional[int]
    dropped: Optional[int]
    aipu_present: bool
    aipu_firmware: Optional[str]
    aipu_temp_c: Optional[float]
    aipu_cores: Optional[int]
    model_name: Optional[str]
    infer_ms_last: Optional[float]
    sidecar_version: Optional[str]
    uptime_s: Optional[float]

    def to_dict(self) -> dict:
        out = {f.name: getattr(self, f.name) for f in fields(self)}
        out["crop"] = self.crop.to_dict() if self.crop else None
        return out

    @classmethod
    def from_wire(cls, doc: dict) -> VisionStatus:
        cam = doc.get("camera") or {}
        aipu = doc.get("aipu") or {}
        side = doc.get("sidecar") or {}
        return cls(
            camera_model=cam.get("model"),
            camera_serial=cam.get("serial"),
            width=cam.get("width"),
            height=cam.get("height"),
            exposure_us=cam.get("exposure_us"),
            gain_db=cam.get("gain_db"),
            fps=cam.get("fps"),
            triggered=cam.get("triggered"),
            crop=Crop.from_wire(cam.get("crop")),
            frame_id=cam.get("frame_id"),
            trigger_seq=cam.get("trigger_seq"),
            dropped=cam.get("dropped"),
            aipu_present=bool(aipu.get("present", False)),
            aipu_firmware=aipu.get("firmware"),
            aipu_temp_c=aipu.get("temp_c"),
            aipu_cores=aipu.get("cores"),
            model_name=aipu.get("model_name"),
            infer_ms_last=aipu.get("infer_ms_last"),
            sidecar_version=side.get("version"),
            uptime_s=side.get("uptime_s"),
        )


# ---------------------------------------------------------------- the driver


class BenchVision:
    """A camera (and optional NPU) behind a ``benchctrl-vision`` sidecar.

    Construct with :py:meth:`open`. Implements no Protocol from
    :py:mod:`benchctrl.interfaces` — see the package docstring for why.
    """

    def __init__(self, url: str, *, timeout_s: float = 5.0) -> None:
        self._url = url.rstrip("/")
        self._timeout_s = float(timeout_s)
        self._open = True
        self._status_cache: Optional[VisionStatus] = None
        self._status_at = 0.0
        self._lock = threading.RLock()

    # --- lifecycle -------------------------------------------------------

    @classmethod
    def open(cls, url: Optional[str] = None, *, timeout_s: float = 5.0) -> BenchVision:
        """Connect to a sidecar and confirm it answers.

        ``url`` defaults to ``$BENCHCTRL_VISION_URL``, then
        :py:data:`DEFAULT_URL`. Raises :py:class:`VisionConnectionError` with
        the thing to check when nothing answers, because "connection refused"
        on loopback nearly always means the sidecar service is not running.
        """
        resolved = url or os.environ.get(URL_ENV) or DEFAULT_URL
        if not resolved.startswith(("http://", "https://")):
            raise VisionValueError(f"url must start with http:// or https://, got {resolved!r}")
        self = cls(resolved, timeout_s=timeout_s)
        health = self._request("GET", "/health")
        if not health.get("ok"):
            raise VisionConnectionError(f"sidecar at {resolved} reports not ok: {health}")
        log.info(
            "bench_vision: connected to %s (sidecar %s, aipu=%s)",
            resolved,
            health.get("version"),
            health.get("aipu"),
        )
        return self

    def close(self) -> None:
        """Forget the sidecar. Changes nothing on the camera.

        Nothing here can strand a DUT: the camera holds no output, and the
        sidecar keeps running for the next client. ``safety.default_safe_state``
        is inert on this device for the same reason.
        """
        with self._lock:
            self._open = False
            self._status_cache = None

    def __enter__(self) -> BenchVision:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<BenchVision {self._url} {'open' if self._open else 'closed'}>"

    # --- reads (no writer claim) -------------------------------------------

    def read_identity(self) -> VisionInfo:
        """Camera model/serial, sidecar version, and whether an AIPU is present."""
        st = self._status(max_age_s=0.0)
        doc = self._last_status_doc or {}
        cam = doc.get("camera") or {}
        return VisionInfo(
            url=self._url,
            sidecar_version=str(st.sidecar_version),
            camera_model=st.camera_model,
            camera_serial=st.camera_serial,
            sensor_width=cam.get("sensor_width"),
            sensor_height=cam.get("sensor_height"),
            aipu_present=st.aipu_present,
            aipu_firmware=st.aipu_firmware,
            model_name=st.model_name,
        )

    def read_status(self) -> VisionStatus:
        """Everything the sidecar knows right now, in one round trip."""
        return self._status(max_age_s=0.0)

    def read_frame(self, *, wait_for: Optional[int] = None, wait_s: float = 5.0) -> Frame:
        """The latest frame, or the next one after ``wait_for``.

        With ``wait_for`` (a frame id) this long-polls until a newer frame
        exists, up to ``wait_s``; in triggered mode that wait ends only when
        someone triggers. Without it, the latest frame is returned at once and
        a camera that has never produced one raises
        :py:class:`VisionCaptureError`.
        """
        wait_s = self._check_wait(wait_s)
        query = {"wait_s": f"{wait_s:g}"}
        if wait_for is not None:
            query["wait"] = str(int(wait_for))
        doc = self._request("GET", "/frame.json", query=query, timeout=wait_s + self._timeout_s)
        return self._frame_from(doc)

    def detect(self, *, min_conf: float = 0.4) -> Detections:
        """Run the model on the latest frame. No trigger, no new frame.

        Raises :py:class:`VisionCapabilityError` where there is no AIPU.
        """
        doc = self._request("POST", "/detect", body={"min_conf": self._check_conf(min_conf)})
        return self._detections_from(doc)

    # --- mutators (writer claim) --------------------------------------------

    def trigger_capture(
        self,
        *,
        seq: Optional[int] = None,
        infer: bool = False,
        min_conf: float = 0.4,
        wait_s: float = 2.0,
    ) -> Frame:
        """Fire a software trigger tagged ``seq`` and return *that* frame.

        A frame that arrives carrying any other ``seq`` is a
        :py:class:`VisionCaptureError` — see the module docstring. ``seq``
        defaults to a millisecond timestamp, which is unique enough for an
        interactive call; a dataset loop should choose its own.

        ``infer=True`` also runs the model on the frame (one round trip, no
        second frame) and raises :py:class:`VisionCapabilityError` on a host
        without an AIPU **before** triggering, so the camera is not fired for a
        result that cannot be produced.
        """
        wait_s = self._check_wait(wait_s)
        if seq is None:
            seq = int(time.time() * 1000) & 0x7FFFFFFF
        if not isinstance(seq, int) or isinstance(seq, bool) or seq < 0:
            raise VisionValueError(f"seq must be a non-negative int, got {seq!r}")
        doc = self._request(
            "POST",
            "/capture",
            body={
                "seq": seq,
                "infer": bool(infer),
                "min_conf": self._check_conf(min_conf),
                "wait_s": wait_s,
            },
            timeout=wait_s + self._timeout_s,
        )
        frame = self._frame_from(doc)
        if frame.seq != seq:  # the sidecar promises this; hold it to the promise
            raise VisionCaptureError(f"sidecar returned seq={frame.seq} for a trigger tagged {seq}")
        self._invalidate()
        return frame

    def set_exposure_us(self, exposure_us: float) -> float:
        """Exposure in microseconds. Returns what the camera read back
        (clamped to its range), which may differ from what was asked."""
        return float(self._put_config("exposure_us", self._positive(exposure_us, "exposure_us")))

    def set_gain_db(self, gain_db: float) -> float:
        """Analog gain in dB. Returns the read-back value."""
        if isinstance(gain_db, bool) or not isinstance(gain_db, (int, float)) or gain_db < 0:
            raise VisionValueError(f"gain_db must be a number >= 0, got {gain_db!r}")
        return float(self._put_config("gain_db", float(gain_db)))

    def set_fps(self, fps: float) -> float:
        """Free-run frame rate. Ignored by the camera while triggered."""
        return float(self._put_config("fps", self._positive(fps, "fps")))

    def set_crop(self, x: int, y: int, w: int, h: int) -> Crop:
        """Restrict frames to a region of interest, in sensor pixels."""
        for name, v in (("x", x), ("y", y), ("w", w), ("h", h)):
            if isinstance(v, bool) or not isinstance(v, int):
                raise VisionValueError(f"{name} must be an int, got {v!r}")
        if x < 0 or y < 0 or w <= 0 or h <= 0:
            raise VisionValueError(f"crop needs x,y >= 0 and w,h > 0, got {(x, y, w, h)}")
        doc = self._request("PUT", "/config", body={"crop": [x, y, w, h]})
        self._invalidate()
        crop = Crop.from_wire(doc.get("crop"))
        if crop is None:
            raise VisionProtocolError("sidecar accepted a crop but reports none")
        return crop

    def clear_crop(self) -> None:
        """Back to the full sensor."""
        self._request("PUT", "/config", body={"crop": None})
        self._invalidate()

    # --- properties (all from one cached /status) --------------------------

    @property
    def url(self) -> str:
        return self._url

    @property
    def is_open(self) -> bool:
        return self._open

    @property
    def stream_url(self) -> str:
        """Where a browser can watch the camera live (MJPEG). Humans only."""
        return f"{self._url}/stream"

    @property
    def camera_model(self) -> Optional[str]:
        return self._prop("camera_model")

    @property
    def camera_serial(self) -> Optional[str]:
        return self._prop("camera_serial")

    @property
    def frame_id(self) -> Optional[int]:
        return self._prop("frame_id")

    @property
    def trigger_seq(self) -> Optional[int]:
        return self._prop("trigger_seq")

    @property
    def crop(self) -> Optional[Crop]:
        return self._prop("crop")

    @property
    def exposure_us(self) -> Optional[float]:
        return self._prop("exposure_us")

    @property
    def gain_db(self) -> Optional[float]:
        return self._prop("gain_db")

    @property
    def fps(self) -> Optional[float]:
        return self._prop("fps")

    @property
    def triggered(self) -> Optional[bool]:
        return self._prop("triggered")

    @property
    def aipu_present(self) -> bool:
        return bool(self._prop("aipu_present"))

    @property
    def aipu_temp_c(self) -> Optional[float]:
        """Whatever the runtime exposes; ``None`` where it exposes nothing.
        Never estimated."""
        return self._prop("aipu_temp_c")

    @property
    def model_name(self) -> Optional[str]:
        return self._prop("model_name")

    # --- internals -----------------------------------------------------------

    _last_status_doc: Optional[dict] = None

    def _prop(self, name: str) -> Any:
        try:
            return getattr(self._status(max_age_s=STATUS_TTL_S), name)
        except VisionError as exc:
            log.debug("bench_vision: property %s unavailable: %r", name, exc)
            return None

    def _status(self, *, max_age_s: float) -> VisionStatus:
        with self._lock:
            fresh = (time.monotonic() - self._status_at) < max_age_s
            if fresh and self._status_cache is not None:
                return self._status_cache
        doc = self._request("GET", "/status")
        st = VisionStatus.from_wire(doc)
        with self._lock:
            self._status_cache = st
            self._status_at = time.monotonic()
            self._last_status_doc = doc
        return st

    def _invalidate(self) -> None:
        with self._lock:
            self._status_at = 0.0

    def _put_config(self, key: str, value: float) -> Any:
        doc = self._request("PUT", "/config", body={key: value})
        self._invalidate()
        if key not in doc:
            raise VisionProtocolError(f"sidecar did not echo {key} back from /config")
        return doc[key]

    def _frame_from(self, doc: dict) -> Frame:
        try:
            jpeg = base64.b64decode(doc["jpeg_b64"])
            dets = doc.get("detections")
            return Frame(
                frame_id=int(doc["frame_id"]),
                seq=int(doc["seq"]),
                ts=float(doc["ts"]),
                width=int(doc["width"]),
                height=int(doc["height"]),
                jpeg=jpeg,
                crop=Crop.from_wire(doc.get("crop")),
                detections=self._detections_from(dets) if dets else None,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VisionProtocolError(f"malformed frame document: {exc!r}") from exc

    @staticmethod
    def _detections_from(doc: dict) -> Detections:
        try:
            items = tuple(
                Detection(
                    class_id=int(d["class_id"]),
                    label=str(d.get("label", "")),
                    score=float(d["score"]),
                    x1=int(d["x1"]),
                    y1=int(d["y1"]),
                    x2=int(d["x2"]),
                    y2=int(d["y2"]),
                )
                for d in doc.get("items", [])
            )
            return Detections(
                frame_id=int(doc["frame_id"]),
                seq=int(doc["seq"]),
                model_name=doc.get("model_name"),
                infer_ms=float(doc.get("infer_ms", 0.0)),
                items=items,
            )
        except (KeyError, TypeError, ValueError) as exc:
            raise VisionProtocolError(f"malformed detections document: {exc!r}") from exc

    @staticmethod
    def _check_wait(wait_s: float) -> float:
        if isinstance(wait_s, bool) or not isinstance(wait_s, (int, float)):
            raise VisionValueError(f"wait_s must be a number, got {wait_s!r}")
        if not 0 < wait_s <= MAX_WAIT_S:
            raise VisionValueError(f"wait_s must be in (0, {MAX_WAIT_S:g}], got {wait_s!r}")
        return float(wait_s)

    @staticmethod
    def _check_conf(min_conf: float) -> float:
        if isinstance(min_conf, bool) or not isinstance(min_conf, (int, float)):
            raise VisionValueError(f"min_conf must be a number, got {min_conf!r}")
        if not 0.0 <= min_conf <= 1.0:
            raise VisionValueError(f"min_conf must be in [0, 1], got {min_conf!r}")
        return float(min_conf)

    @staticmethod
    def _positive(value: float, name: str) -> float:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or value <= 0:
            raise VisionValueError(f"{name} must be a number > 0, got {value!r}")
        return float(value)

    def _request(
        self,
        method: str,
        path: str,
        *,
        query: Optional[dict[str, str]] = None,
        body: Optional[dict] = None,
        timeout: Optional[float] = None,
    ) -> dict:
        if not self._open:
            raise VisionConnectionError("BenchVision is closed")
        url = self._url + path
        if query:
            url += "?" + "&".join(f"{k}={v}" for k, v in query.items())
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=timeout or self._timeout_s) as resp:
                raw = resp.read()
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise self._error_from(exc.code, raw, method, path) from None
        except urllib.error.URLError as exc:
            reason = exc.reason
            if isinstance(reason, (socket.timeout, TimeoutError)):
                raise VisionTimeoutError(f"{method} {path} timed out") from None
            raise VisionConnectionError(
                f"cannot reach the vision sidecar at {self._url} ({reason}) — "
                f"is benchctrl-vision.service running on this host?"
            ) from None
        except (socket.timeout, TimeoutError):
            raise VisionTimeoutError(f"{method} {path} timed out") from None
        except OSError as exc:
            raise VisionConnectionError(f"{method} {path}: {exc}") from None
        try:
            doc = json.loads(raw.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise VisionProtocolError(f"{method} {path}: non-JSON reply ({exc})") from None
        if not isinstance(doc, dict):
            raise VisionProtocolError(
                f"{method} {path}: expected an object, got {type(doc).__name__}"
            )
        return doc

    @staticmethod
    def _error_from(status: int, raw: bytes, method: str, path: str) -> VisionError:
        try:
            doc = json.loads(raw.decode("utf-8"))
            err = doc["error"]
            etype, message = str(err["type"]), str(err.get("message", ""))
        except (UnicodeDecodeError, json.JSONDecodeError, KeyError, TypeError):
            return VisionProtocolError(f"{method} {path}: HTTP {status} with no error document")
        cls = _ERROR_TYPES.get(etype)
        if cls is None:
            return VisionProtocolError(f"{method} {path}: unknown error type {etype!r}: {message}")
        return cls(message)

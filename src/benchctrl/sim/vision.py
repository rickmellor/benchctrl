"""A simulated vision sidecar: the real service, a synthetic camera, canned boxes.

What this proves and what it does not
-------------------------------------
:py:class:`SimulatedVisionSidecar` runs :py:class:`benchctrl.vision.service.VisionService`
— the same router the production sidecar runs — on a real ``ThreadingHTTPServer``
bound to ``127.0.0.1:0``. The production driver talks to it over a real socket
with its real ``urllib`` code. So a green suite proves the driver, the HTTP
contract, the codec, the agent's dispatch and the remote proxy. It proves
nothing about pylon or the Axelera runtime, which live behind the two
collaborators substituted here.

The substitutions are deliberate about the *behaviours the driver could ship
wrong*, because those are what the tests need to be able to see:

* In triggered mode (the default) **no frame appears without a trigger**, so a
  ``read_frame(wait_for=...)`` with nothing triggered really times out.
* A trigger's ``seq`` lands on the **next** frame only. A frame is never
  retro-stamped, and a second trigger before the first frame is produced counts
  as ``dropped``.
* A crop changes the reported ``width``/``height``.
* Exposure and gain **read back clamped** to the camera's range, the way pylon
  clamps a value outside ``ExposureTime``'s limits, so a driver that trusted
  what it *sent* rather than what came back would be caught.
* ``fail_next_trigger`` makes one trigger raise, so the label loop's "discard,
  never label" path can be exercised.

The JPEG is real. :py:data:`TINY_JPEG` is a 16x16 baseline JPEG minted once
with Pillow, and :py:func:`padded_jpeg` grows it to any requested size by
inserting a ``COM`` (0xFFFE) segment before ``EOI`` — still a valid JPEG that
any decoder accepts, which is how the >64 KB blob path across the agent wire is
exercised with no image library in CI. The *reported* ``width``/``height``
are the nominal sensor dimensions, not the 16x16 of the bytes; the simulator
is honest about that in ``info()`` (``model`` ends in ``-SIM``).
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

from benchctrl.vision.service import (
    FrameRecord,
    ServiceCaptureError,
    VisionService,
    serve,
)

log = logging.getLogger("benchctrl.sim.vision")

#: A valid 16x16 8-bit grey baseline JPEG (checkerboard), 200 bytes.
TINY_JPEG = bytes.fromhex(
    "ffd8ffe000104a46494600010100000100010000ffdb004300100b0c0e0c0a100e0d0e1211"
    "101318281a181616183123251d283a333d3c3933383740485c4e404457453738506d51575f"
    "626768673e4d71797064785c656763ffc0000b080010001001011100ffc400150001010000"
    "0000000000000000000000000004ffc4001b1000010403000000000000000000000000111223"
    "3261428191ffda0008010100003f009e34340704682714b48d0d01c11a09c52d234340704682"
    "714b48d0d01c11a09c52d7ffd9"
)

#: Largest single COM segment: the length field is 16-bit and counts itself.
_COM_MAX = 0xFFFF - 2


def padded_jpeg(size: int, base: bytes = TINY_JPEG) -> bytes:
    """``base`` grown to exactly ``size`` bytes with COM segments before EOI.

    A COM segment is ``FF FE <len:2> <payload>``; decoders skip it. Sizes below
    ``len(base) + 4`` return ``base`` unchanged (a JPEG cannot be made smaller
    than it is).
    """
    assert base[-2:] == b"\xff\xd9", "base must end in EOI"
    need = size - len(base)
    if need < 4:
        return base
    body, eoi = base[:-2], base[-2:]
    out = bytearray(body)
    while need >= 4:
        payload = min(need - 4, _COM_MAX)
        # A segment must be at least 4 bytes (marker + length); if the remainder
        # would leave a 1..3 byte gap, fold it into this segment's payload.
        if 0 < need - (payload + 4) < 4:
            payload = need - 4
        out += b"\xff\xfe" + (payload + 2).to_bytes(2, "big") + b"\x00" * payload
        need -= payload + 4
    return bytes(out) + eoi


class SyntheticCamera:
    """The camera half of the sidecar, with no sensor behind it."""

    EXPOSURE_RANGE_US = (20.0, 10_000_000.0)
    GAIN_RANGE_DB = (0.0, 48.0)
    FPS_RANGE = (0.1, 160.0)

    def __init__(
        self,
        *,
        width: int = 1920,
        height: int = 1200,
        model: str = "a2A1920-160uc-SIM",
        serial: str = "SIM0VISION",
        triggered: bool = True,
        exposure_us: float = 8000.0,
        gain_db: float = 0.0,
        fps: float = 60.0,
        frame_bytes: Optional[int] = None,
        latency_s: float = 0.0,
    ) -> None:
        self.width, self.height = int(width), int(height)
        self.model, self.serial = model, serial
        self.triggered = bool(triggered)
        self.exposure_us = self._clamp(exposure_us, self.EXPOSURE_RANGE_US)
        self.gain_db = self._clamp(gain_db, self.GAIN_RANGE_DB)
        self.fps = self._clamp(fps, self.FPS_RANGE)
        self.frame_bytes = frame_bytes
        self.latency_s = float(latency_s)
        self.crop: Optional[tuple[int, int, int, int]] = None
        self.frame_id = 0
        self.trigger_seq = -1
        self.dropped = 0
        self.fail_next_trigger = False
        self._pending: Optional[int] = None
        self._latest: Optional[FrameRecord] = None
        self._cv = threading.Condition()
        self._closed = False
        self._thread: Optional[threading.Thread] = None
        if not self.triggered:
            self._thread = threading.Thread(target=self._free_run, daemon=True)
            self._thread.start()

    # --- the duck type the service needs ------------------------------

    def info(self) -> dict:
        return {
            "model": self.model,
            "serial": self.serial,
            "sensor_width": self.width,
            "sensor_height": self.height,
        }

    def status(self) -> dict:
        w, h = self._dims()
        return {
            "exposure_us": self.exposure_us,
            "gain_db": self.gain_db,
            "fps": self.fps,
            "triggered": self.triggered,
            "crop": list(self.crop) if self.crop else None,
            "frame_id": self.frame_id,
            "trigger_seq": self.trigger_seq,
            "dropped": self.dropped,
            "width": w,
            "height": h,
        }

    def configure(self, **kw: float) -> dict:
        if "exposure_us" in kw:
            self.exposure_us = self._clamp(kw["exposure_us"], self.EXPOSURE_RANGE_US)
        if "gain_db" in kw:
            self.gain_db = self._clamp(kw["gain_db"], self.GAIN_RANGE_DB)
        if "fps" in kw:
            self.fps = self._clamp(kw["fps"], self.FPS_RANGE)
        return self.status()

    def set_crop(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        if x + w > self.width or y + h > self.height:
            raise ValueError(f"crop {(x, y, w, h)} exceeds the {self.width}x{self.height} sensor")
        self.crop = (x, y, w, h)
        return self.crop

    def clear_crop(self) -> None:
        self.crop = None

    def trigger(self, seq: int) -> None:
        if self.fail_next_trigger:
            self.fail_next_trigger = False
            raise ServiceCaptureError("camera not trigger-ready (simulated)")
        with self._cv:
            if self._pending is not None:
                # The previous trigger's frame has not been produced yet.
                self.dropped += 1
            self._pending = seq
        if self.triggered:
            # Produce the frame "after the exposure": synchronously here, with
            # an optional delay so long-poll paths see a real wait.
            if self.latency_s:
                time.sleep(self.latency_s)
            self._produce()

    def latest(self) -> Optional[FrameRecord]:
        with self._cv:
            return self._latest

    def wait_for(self, after_frame_id: int, timeout_s: float) -> Optional[FrameRecord]:
        deadline = time.monotonic() + timeout_s
        with self._cv:
            while self._latest is None or self._latest.frame_id <= after_frame_id:
                remaining = deadline - time.monotonic()
                if remaining <= 0 or self._closed:
                    return None
                self._cv.wait(remaining)
            return self._latest

    def close(self) -> None:
        with self._cv:
            self._closed = True
            self._cv.notify_all()

    # --- internals -------------------------------------------------------

    def _dims(self) -> tuple[int, int]:
        return (self.crop[2], self.crop[3]) if self.crop else (self.width, self.height)

    def _produce(self) -> None:
        with self._cv:
            seq = self._pending if self._pending is not None else -1
            self._pending = None
            self.frame_id += 1
            self.trigger_seq = seq
            w, h = self._dims()
            jpeg = padded_jpeg(self.frame_bytes) if self.frame_bytes else TINY_JPEG
            self._latest = FrameRecord(
                frame_id=self.frame_id,
                seq=seq,
                ts=time.time(),
                width=w,
                height=h,
                crop=self.crop,
                jpeg=jpeg,
            )
            self._cv.notify_all()

    def _free_run(self) -> None:
        while not self._closed:
            time.sleep(1.0 / self.fps)
            if not self._closed:
                self._produce()

    @staticmethod
    def _clamp(value: float, bounds: tuple[float, float]) -> float:
        lo, hi = bounds
        return float(min(max(float(value), lo), hi))


class CannedDetector:
    """A detector that returns what it was told to, at a fixed cost."""

    def __init__(
        self,
        detections: Optional[list[dict[str, Any]]] = None,
        *,
        present: bool = True,
        model_name: Optional[str] = "yolov8n-coco-sim",
        infer_ms: float = 22.0,
        temp_c: Optional[float] = 41.5,
        firmware: str = "1.8.0-sim",
        cores: int = 4,
    ) -> None:
        self.detections: list[dict[str, Any]] = (
            list(detections)
            if detections is not None
            else [
                {
                    "class_id": 0,
                    "label": "person",
                    "score": 0.91,
                    "x1": 100,
                    "y1": 80,
                    "x2": 420,
                    "y2": 900,
                },
                {
                    "class_id": 41,
                    "label": "cup",
                    "score": 0.63,
                    "x1": 1200,
                    "y1": 600,
                    "x2": 1320,
                    "y2": 760,
                },
            ]
        )
        self.present = present
        self.model_name = model_name if present else None
        self.infer_ms = infer_ms
        self.temp_c = temp_c
        self.firmware = firmware
        self.cores = cores
        self.calls = 0

    def infer(self, record: FrameRecord, min_conf: float) -> tuple[list[dict], float]:
        self.calls += 1
        items = [dict(d) for d in self.detections if d["score"] >= min_conf]
        return items, self.infer_ms

    def status(self) -> dict:
        return {
            "firmware": self.firmware,
            "temp_c": self.temp_c,
            "cores": self.cores,
            "infer_ms_last": self.infer_ms if self.calls else None,
        }


class SimulatedVisionSidecar:
    """The service on a loopback socket, with a request log for tests.

    ``request_log`` records ``(method, path)`` for every request, which is how
    the driver's "one ``/status`` per property snapshot" promise is pinned.
    """

    def __init__(
        self,
        camera: Optional[SyntheticCamera] = None,
        detector: Optional[Any] = None,
        *,
        aipu: bool = True,
        **camera_kwargs: Any,
    ) -> None:
        self.camera = camera if camera is not None else SyntheticCamera(**camera_kwargs)
        if detector is None and aipu:
            detector = CannedDetector()
        self.detector = detector
        self.request_log: list[tuple[str, str]] = []
        self.service = VisionService(self.camera, self.detector, version="sim")
        self._server = serve(self.service, bind="127.0.0.1", port=0, on_request=self._log)
        self._thread = threading.Thread(
            target=self._server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True
        )
        self._thread.start()

    def _log(self, method: str, path: str) -> None:
        self.request_log.append((method, path))

    @property
    def url(self) -> str:
        host, port = self._server.server_address[:2]
        return f"http://{str(host)}:{int(port)}"

    def requests_to(self, path: str) -> int:
        return sum(1 for _, p in self.request_log if p.split("?")[0] == path)

    def close(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        self.service.close()

    def __enter__(self) -> SimulatedVisionSidecar:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

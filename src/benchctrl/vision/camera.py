"""The Basler camera behind the vision sidecar, through pypylon.

Heavy half — imports pypylon, numpy and OpenCV at module load. Imported only by
:py:mod:`benchctrl.vision.server`; the router (:py:mod:`benchctrl.vision.service`)
and the driver never see this module.

Adapted from the ``metis`` repository's ``camera_server.py``, which proved the
Basler a2A1920-160uc (USB3 Vision, GenICam — no ``/dev/video*`` node, so pylon
is the only path) at 119.6 FPS free-run and a 15 ms software-triggered round
trip on the Raspberry Pi 5.

Two things the router relies on that this class must get right:

* **A trigger's ``seq`` lands on the next frame only.** ``pending_seq`` is
  consumed by the grab loop when the frame arrives; a second trigger before
  that counts as ``dropped`` and *replaces* the pending tag, so a frame never
  carries a stale one.
* **Configuration reads back from the camera.** Exposure and gain are
  clamped to the camera's own limits before being written and then read
  back, because pylon raises rather than clamps on an out-of-range write and
  a driver that trusted what it sent would be wrong on the boundary.

The hardware trigger cable, when it lands, is a change to ``TriggerSource``
here (``Line1`` instead of ``Software``) and nothing else.
"""

from __future__ import annotations

import logging
import threading
import time
from typing import Any, Optional

import cv2
import numpy as np
from pypylon import pylon

from benchctrl.vision.service import FrameRecord, ServiceCaptureError

log = logging.getLogger("benchctrl.vision.camera")


class PylonCamera:
    """One Basler camera, grabbing on a background thread."""

    def __init__(
        self,
        *,
        serial: Optional[str] = None,
        triggered: bool = True,
        exposure_us: float = 8000.0,
        gain_db: float = 0.0,
        fps: float = 60.0,
        jpeg_quality: int = 80,
    ) -> None:
        tlf = pylon.TlFactory.GetInstance()
        devices = tlf.EnumerateDevices()
        if not devices:
            raise RuntimeError(
                "no pylon camera found — check the USB cable and /dev/bus/usb access"
            )
        if serial is not None:
            devices = [d for d in devices if d.GetSerialNumber() == serial]
            if not devices:
                raise RuntimeError(f"no pylon camera with serial {serial!r}")
        self.cam = pylon.InstantCamera(tlf.CreateDevice(devices[0]))
        self.cam.Open()
        info = self.cam.GetDeviceInfo()
        self.model = info.GetModelName()
        self.serial = info.GetSerialNumber()
        self.sensor_width = int(self.cam.WidthMax.Value)
        self.sensor_height = int(self.cam.HeightMax.Value)
        self.triggered = bool(triggered)
        self.jpeg_quality = int(jpeg_quality)
        # State first: configure() below reads status(), which reads the crop.
        self.crop: Optional[tuple[int, int, int, int]] = None
        self.frame_id = 0
        self.trigger_seq = -1
        self.dropped = 0
        self._pending: Optional[int] = None
        self._latest: Optional[FrameRecord] = None
        self._cv = threading.Condition()
        self._tlock = threading.Lock()
        self._closed = False

        if self.triggered:
            self.cam.TriggerSelector.Value = "FrameStart"
            self.cam.TriggerMode.Value = "On"
            self.cam.TriggerSource.Value = "Software"
        else:
            self.cam.TriggerMode.Value = "Off"
            self.cam.AcquisitionFrameRateEnable.Value = True
            self.cam.AcquisitionFrameRate.Value = float(fps)
        self.cam.ExposureAuto.Value = "Off"
        self.cam.GainAuto.Value = "Off"
        self.cam.BalanceWhiteAuto.Value = "Continuous"
        self.configure(exposure_us=exposure_us, gain_db=gain_db)

        self.conv = pylon.ImageFormatConverter()
        self.conv.OutputPixelFormat = pylon.PixelType_BGR8packed

        self.cam.StartGrabbing(pylon.GrabStrategy_LatestImageOnly)
        self._thread = threading.Thread(target=self._loop, name="pylon-grab", daemon=True)
        self._thread.start()
        log.info(
            "camera: %s serial %s, %dx%d, %s",
            self.model,
            self.serial,
            self.sensor_width,
            self.sensor_height,
            "triggered" if self.triggered else f"free-run {fps:g} fps",
        )

    # --- the duck type the service needs ------------------------------

    def info(self) -> dict:
        return {
            "model": self.model,
            "serial": self.serial,
            "sensor_width": self.sensor_width,
            "sensor_height": self.sensor_height,
        }

    def status(self) -> dict:
        w, h = self._dims()
        return {
            "exposure_us": float(self.cam.ExposureTime.Value),
            "gain_db": float(self.cam.Gain.Value),
            "fps": float(self.cam.AcquisitionFrameRate.Value),
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
            node = self.cam.ExposureTime
            node.Value = float(min(max(float(kw["exposure_us"]), node.Min), node.Max))
        if "gain_db" in kw:
            node = self.cam.Gain
            node.Value = float(min(max(float(kw["gain_db"]), node.Min), node.Max))
        if "fps" in kw:
            node = self.cam.AcquisitionFrameRate
            self.cam.AcquisitionFrameRateEnable.Value = True
            node.Value = float(min(max(float(kw["fps"]), node.Min), node.Max))
        return self.status()

    def set_crop(self, x: int, y: int, w: int, h: int) -> tuple[int, int, int, int]:
        if x + w > self.sensor_width or y + h > self.sensor_height:
            raise ValueError(
                f"crop {(x, y, w, h)} exceeds the {self.sensor_width}x{self.sensor_height} sensor"
            )
        self.crop = (x, y, w, h)
        return self.crop

    def clear_crop(self) -> None:
        self.crop = None

    def trigger(self, seq: int) -> None:
        if not self.triggered:
            # Free-run: nothing to fire, but the tag still lands on the next frame.
            with self._cv:
                if self._pending is not None:
                    self.dropped += 1
                self._pending = seq
            return
        with self._tlock:
            with self._cv:
                if self._pending is not None:
                    self.dropped += 1
                self._pending = seq
            if not self.cam.WaitForFrameTriggerReady(1000, pylon.TimeoutHandling_Return):
                with self._cv:
                    self._pending = None
                raise ServiceCaptureError("camera not trigger-ready")
            self.cam.ExecuteSoftwareTrigger()

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
        try:
            self.cam.StopGrabbing()
            self.cam.Close()
        except Exception as exc:  # noqa: BLE001
            log.debug("camera close: %r", exc)

    # --- internals -------------------------------------------------------

    def _dims(self) -> tuple[int, int]:
        return (
            (self.crop[2], self.crop[3]) if self.crop else (self.sensor_width, self.sensor_height)
        )

    def _loop(self) -> None:
        while not self._closed:
            try:
                res = self.cam.RetrieveResult(5000, pylon.TimeoutHandling_Return)
            except Exception as exc:  # noqa: BLE001
                log.warning("grab error: %r", exc)
                time.sleep(0.5)
                continue
            if not res.IsValid():  # timeout: nothing waiting (normal in triggered mode)
                continue
            if not res.GrabSucceeded():
                log.warning("grab failed: %s %s", res.ErrorCode, res.ErrorDescription)
                res.Release()
                continue
            img: Any = self.conv.Convert(res).GetArray()
            res.Release()
            crop = self.crop
            if crop:
                x, y, w, h = crop
                img = np.ascontiguousarray(img[y : y + h, x : x + w])
            ok, buf = cv2.imencode(".jpg", img, [cv2.IMWRITE_JPEG_QUALITY, self.jpeg_quality])
            if not ok:
                log.warning("jpeg encode failed")
                continue
            with self._cv:
                seq = self._pending if self._pending is not None else -1
                self._pending = None
                self.frame_id += 1
                self.trigger_seq = seq
                self._latest = FrameRecord(
                    frame_id=self.frame_id,
                    seq=seq,
                    ts=time.time(),
                    width=int(img.shape[1]),
                    height=int(img.shape[0]),
                    crop=crop,
                    jpeg=buf.tobytes(),
                    image=img,
                )
                self._cv.notify_all()

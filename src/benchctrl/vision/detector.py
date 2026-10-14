"""YOLOv8n on the Axelera Metis, through ``axelera.runtime``.

Heavy half — imports numpy, OpenCV, onnxruntime and the Axelera runtime at
module load, and the runtime maps PCIe BARs, so this only works inside the
sidecar container (``deploy/vision/``). Imported only by
:py:mod:`benchctrl.vision.server`.

Lifted from the ``metis`` repository's ``yolo_metis.py`` (510 FPS on-device,
~46 FPS end to end on scrub): letterbox → INT8 quantize with the model's
zero-point and NHWC padding → device run → dequantize/unpad → the
``postprocess_graph.onnx`` bundled inside the ``.axm`` (DFL decode to
``[1, 84, 8400]``) → confidence filter → NMS. The only additions are the
duck type the router expects (``present``, ``model_name``, ``infer(record,
min_conf)``, ``status()``) and a best-effort board temperature.

The AIPU temperature is read by shelling out to ``axcmd --board-temp`` (the
runtime's Python API exposes no thermal call we know of) on a background
thread every few seconds, because the call takes seconds on a Pi and a status
request — which the agent piggybacks onto every response — must never wait on
it. Reported as ``None`` until the first read lands or on any failure; never
estimated. Firmware
1.8.0 restored the chip's own thermal management (HW throttle at 105 °C);
on the 1.3.0 firmware the card hard-hung under load, so ``firmware`` in
``status()`` is worth a glance before trusting a soak.
"""

from __future__ import annotations

import contextlib
import logging
import re
import subprocess
import threading
import time
import zipfile
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
import onnxruntime as ort
from axelera import runtime as rt

from benchctrl.vision.coco_classes import COCO_CLASSES
from benchctrl.vision.service import FrameRecord

log = logging.getLogger("benchctrl.vision.detector")

#: Model input edge, pixels. yolov8n-coco is compiled for 640x640.
SIZE = 640

#: How often the background thread re-reads ``axcmd --board-temp``. The read
#: takes seconds (it attaches to the runtime), so it never runs on a request.
TEMP_REFRESH_S = 10.0


class YoloMetisDetector:
    """COCO-80 object detection on the AIPU, from a :py:class:`FrameRecord`."""

    def __init__(self, axm_path: str, *, aipu_cores: int = 4, iou: float = 0.5) -> None:
        path = Path(axm_path)
        if not path.is_file():
            raise FileNotFoundError(f"model not found: {path}")
        self.model_name = path.stem
        self.iou = float(iou)
        self.cores = int(aipu_cores)
        self._lock = threading.Lock()
        self.ctx = rt.Context()
        self.model = self.ctx.load_model(str(path))
        self.conn = self.ctx.device_connect(None, num_sub_devices=1)
        self.inst = self.conn.load_model_instance(
            self.model, num_sub_devices=1, aipu_cores=self.cores
        )
        self.iinfo = self.model.inputs()[0]
        self.oinfo = self.model.outputs()
        self.ibuf = np.zeros(self.iinfo.shape, dtype=np.int8)
        self.obufs = [np.zeros(o.shape, dtype=np.int8) for o in self.oinfo]
        pp = zipfile.ZipFile(str(path)).read("postprocess_graph.onnx")
        self.pp = ort.InferenceSession(pp, providers=["CPUExecutionProvider"])
        self.pp_in = [i.name for i in self.pp.get_inputs()]
        self.present = True
        self.infer_ms_last: Optional[float] = None
        self.firmware = _firmware_version()
        self._temp_c: Optional[float] = None
        self._stop = threading.Event()
        self._temp_thread = threading.Thread(target=self._temp_loop, name="metis-temp", daemon=True)
        self._temp_thread.start()
        log.info(
            "detector: %s on %d AIPU cores (firmware %s)",
            self.model_name,
            self.cores,
            self.firmware,
        )

    # --- the duck type the service needs ------------------------------

    def infer(self, record: FrameRecord, min_conf: float) -> tuple[list[dict], float]:
        bgr = record.image
        if bgr is None:
            bgr = cv2.imdecode(np.frombuffer(record.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("frame JPEG could not be decoded")
        t0 = time.perf_counter()
        with self._lock:
            dets = self._infer(bgr, float(min_conf))
        ms = (time.perf_counter() - t0) * 1000.0
        self.infer_ms_last = ms
        items = [
            {
                "class_id": cid,
                "label": COCO_CLASSES[cid] if 0 <= cid < len(COCO_CLASSES) else str(cid),
                "score": score,
                "x1": x1,
                "y1": y1,
                "x2": x2,
                "y2": y2,
            }
            for cid, score, x1, y1, x2, y2 in dets
        ]
        return items, round(ms, 2)

    def status(self) -> dict:
        return {
            "firmware": self.firmware,
            "temp_c": self.temp_c,
            "cores": self.cores,
            "infer_ms_last": self.infer_ms_last,
        }

    @property
    def temp_c(self) -> Optional[float]:
        """The last temperature the background thread read; None until it has."""
        return self._temp_c

    def _temp_loop(self) -> None:
        while not self._stop.is_set():
            self._temp_c = _board_temp_c()
            self._stop.wait(TEMP_REFRESH_S)

    def close(self) -> None:
        self.present = False
        self._stop.set()
        for obj in (self.inst, self.conn, self.model, self.ctx):
            with contextlib.suppress(Exception):
                obj.release()

    # --- the pipeline ------------------------------------------------------

    def _letterbox(self, bgr: np.ndarray) -> tuple[np.ndarray, float, int, int]:
        h, w = bgr.shape[:2]
        r = min(SIZE / w, SIZE / h)
        nw, nh = round(w * r), round(h * r)
        dx, dy = (SIZE - nw) // 2, (SIZE - nh) // 2
        img = np.full((SIZE, SIZE, 3), 114, dtype=np.uint8)
        img[dy : dy + nh, dx : dx + nw] = cv2.resize(bgr, (nw, nh))
        return img, r, dx, dy

    def _infer(self, bgr: np.ndarray, conf: float) -> list[tuple[int, float, int, int, int, int]]:
        lb, r, dx, dy = self._letterbox(bgr)
        rgb = cv2.cvtColor(lb, cv2.COLOR_BGR2RGB)
        q = (rgb.astype(np.int16) + int(round(self.iinfo.zero_point))).astype(np.int8)
        (_pn0, _pn1), (pt, _pb), (pl, _pr), (pc0, _pc1) = self.iinfo.padding
        self.ibuf[:] = 0
        self.ibuf[0, pt : pt + SIZE, pl : pl + SIZE, pc0 : pc0 + 3] = q

        self.inst.run([self.ibuf], self.obufs)

        feats = []
        for buf, info in zip(self.obufs, self.oinfo):
            f = (buf.astype(np.float32) - np.float32(info.zero_point)) * np.float32(info.scale)
            cpad = info.padding[3][1]
            if cpad:
                f = f[..., :-cpad]
            feats.append(np.ascontiguousarray(f.transpose(0, 3, 1, 2)))

        (out,) = self.pp.run(None, dict(zip(self.pp_in, feats)))
        pred = out[0]
        cls = pred[4:]
        ids = cls.argmax(0)
        scores = cls[ids, np.arange(cls.shape[1])]
        keep = scores >= conf
        if not keep.any():
            return []
        boxes_xywh = pred[:4, keep].T
        ids, scores = ids[keep], scores[keep]
        xy = boxes_xywh[:, :2]
        wh = boxes_xywh[:, 2:]
        rects = np.hstack([xy - wh / 2, wh])
        idx = cv2.dnn.NMSBoxes(rects.tolist(), scores.tolist(), conf, self.iou)
        dets: list[tuple[int, float, int, int, int, int]] = []
        h, w = bgr.shape[:2]
        for i in np.array(idx).flatten():
            x, y, bw, bh = rects[i]
            x1 = int(np.clip((x - dx) / r, 0, w - 1))
            y1 = int(np.clip((y - dy) / r, 0, h - 1))
            x2 = int(np.clip((x + bw - dx) / r, 0, w - 1))
            y2 = int(np.clip((y + bh - dy) / r, 0, h - 1))
            dets.append((int(ids[i]), float(scores[i]), x1, y1, x2, y2))
        return dets


# ---------------------------------------------------------------- helpers


def _run(args: list[str], timeout: float = 20.0) -> Optional[str]:
    try:
        out = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
    except (OSError, subprocess.TimeoutExpired):
        return None
    return out.stdout if out.returncode == 0 else None


def _firmware_version() -> Optional[str]:
    """``flver=1.8.0`` from ``axdevice``, or None."""
    out = _run(["axdevice"])
    m = re.search(r"flver=([0-9.]+)", out or "")
    return m.group(1) if m else None


def _board_temp_c() -> Optional[float]:
    """The first sensor ``axcmd --board-temp`` prints, or None.

    The line looks like ``0: temp_sensor0@48 = 50.25`` — a value in degrees C
    with no unit suffix (verified on scrub, firmware 1.8.0).
    """
    out = _run(["axcmd", "--board-temp"])
    m = re.search(r"temp_sensor\d+@\d+\s*=\s*(-?\d+(?:\.\d+)?)", out or "")
    return float(m.group(1)) if m else None


def load_detector(axm_path: Optional[str], *, aipu_cores: int = 4) -> Optional[Any]:
    """A detector if a model path is given and the AIPU answers, else None.

    Failing to load is logged and *not* fatal: a sidecar with a camera and no
    NPU is a valid host (an Uno Q, a desktop with the card out), and the
    router turns ``None`` into a capability error per request.
    """
    if not axm_path:
        return None
    try:
        return YoloMetisDetector(axm_path, aipu_cores=aipu_cores)
    except Exception as exc:  # noqa: BLE001
        log.warning("detector: unavailable (%r) — serving the camera only", exc)
        return None

"""Indicator classifiers on the Axelera Metis, through ``axelera.runtime``.

Heavy half — numpy, OpenCV and the Axelera runtime at module load; only the
sidecar container imports it (:py:mod:`benchctrl.vision.server`). The router
sees a duck type: ``present``, ``name``, ``classes``, ``crop``,
``classify(record, region)``, ``status()``.

What a classifier is here
-------------------------
A small CNN that reads **one indicator from one fixed sensor region** and
answers with a label from a closed set: the model the label loop's dataset
trains (``docs/vision.md`` § Label loop, § Classifiers). It is compiled on
scrub with the Axelera devkit — the ``metis`` repo's
``experiments/vision/bench_led/`` pipeline: ``prep.py`` (dataset -> npz),
``train.py`` (-> ONNX + calibration set + ``classes.json``), ``compile.py``
(per-tensor min-max PTQ -> ``compiled_<name>/model.json``) — and shipped to
the bench box as a directory, never trained on the Pi.

The directory carries ``model.json`` (+ its blobs) for the runtime and a
``classes.json`` that this module needs: the class list in output order, the
``input`` shape ``[C, H, W]``, the sensor ``crop`` the frames were taken with,
and provenance (dataset name, spec digest, held-out accuracy). The crop is
what makes the model portable across camera settings: the router translates
it into the current frame (or refuses a frame that does not contain it) and
passes the region in, so the model always sees the patch it was trained on.

Inference follows the metis repo's board reader (``player.py`` MetisReader):
resize the region to the input size with an area filter, scale to ``0..1``,
quantize with the model's own input scale/zero-point, place into the padded
NHWC input buffer, run, dequantize, strip the channel padding. Logits are
returned as-is; the margin gate is the router's (one place, one default).
"""

from __future__ import annotations

import contextlib
import json
import logging
import threading
import time
from pathlib import Path
from typing import Any, Optional

import cv2
import numpy as np
from axelera import runtime as rt

from benchctrl.vision.service import FrameRecord

log = logging.getLogger("benchctrl.vision.classifier")

#: The file beside ``model.json`` that names the classes (written by train.py).
CLASSES_FILE = "classes.json"


class MetisClassifier:
    """One compiled indicator classifier, resident on the AIPU."""

    def __init__(self, model_dir: str, *, aipu_cores: int = 1) -> None:
        path = Path(model_dir)
        model_json = path / "model.json"
        if not model_json.is_file():
            raise FileNotFoundError(f"no model.json in {path}")
        meta = json.loads((path / CLASSES_FILE).read_text(encoding="utf-8"))
        classes = meta.get("classes")
        if not isinstance(classes, list) or not classes:
            raise ValueError(f"{path / CLASSES_FILE} has no 'classes' list")
        self.name = str(meta.get("name") or path.name)
        self.classes = tuple(str(c) for c in classes)
        crop = meta.get("crop")  # the label loop's manifest spells it {x, y, w, h}
        if isinstance(crop, dict):
            crop = [crop["x"], crop["y"], crop["w"], crop["h"]]
        self.crop = (int(crop[0]), int(crop[1]), int(crop[2]), int(crop[3])) if crop else None
        cin, hin, win = (int(v) for v in meta.get("input", [3, 96, 96]))
        if cin not in (1, 3):
            raise ValueError(f"{self.name}: input channels must be 1 or 3, got {cin}")
        self.channels, self.height, self.width = cin, hin, win
        self.meta = meta
        self.cores = int(aipu_cores)
        self.present = True
        self.infer_ms_last: Optional[float] = None
        self._lock = threading.Lock()

        self.ctx = rt.Context()
        self.model = self.ctx.load_model(str(model_json))
        self.conn = self.ctx.device_connect(None, num_sub_devices=1)
        self.inst = self.conn.load_model_instance(
            self.model, num_sub_devices=1, aipu_cores=self.cores
        )
        self.iinfo = self.model.inputs()[0]
        self.oinfo = self.model.outputs()[0]
        self.ibuf = np.zeros(self.iinfo.shape, dtype=np.int8)
        self.obuf = np.zeros(self.oinfo.shape, dtype=np.int8)
        true_c = int(self.oinfo.shape[3] - self.oinfo.padding[3][1])
        if true_c != len(self.classes):
            raise ValueError(
                f"{self.name}: model has {true_c} outputs but classes.json lists "
                f"{len(self.classes)} classes"
            )

    # --- the duck type -------------------------------------------------------

    def classify(
        self, record: FrameRecord, region: Optional[tuple[int, int, int, int]]
    ) -> tuple[dict[str, float], float]:
        bgr = record.image
        if bgr is None:
            bgr = cv2.imdecode(np.frombuffer(record.jpeg, dtype=np.uint8), cv2.IMREAD_COLOR)
            if bgr is None:
                raise ValueError("frame JPEG could not be decoded")
        if region is not None:
            x, y, w, h = region
            bgr = bgr[y : y + h, x : x + w]
        t0 = time.perf_counter()
        with self._lock:
            logits = self._infer(bgr)
        ms = (time.perf_counter() - t0) * 1000.0
        self.infer_ms_last = round(ms, 3)
        return {c: float(v) for c, v in zip(self.classes, logits)}, self.infer_ms_last

    def status(self) -> dict:
        return {
            "input": [self.channels, self.height, self.width],
            "cores": self.cores,
            "infer_ms_last": self.infer_ms_last,
            "source_dataset": self.meta.get("source_dataset"),
            "spec_digest": self.meta.get("spec_digest"),
            "holdout_acc": self.meta.get("holdout_acc"),
        }

    def close(self) -> None:
        self.present = False
        for obj in (self.inst, self.conn, self.model, self.ctx):
            with contextlib.suppress(Exception):
                obj.release()

    # --- the pipeline ------------------------------------------------------

    def _infer(self, bgr: np.ndarray) -> np.ndarray:
        if self.channels == 3:
            img = cv2.cvtColor(bgr, cv2.COLOR_BGR2RGB)
        else:
            img = cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)
        img = cv2.resize(img, (self.width, self.height), interpolation=cv2.INTER_AREA)
        v = img.astype(np.float32) / 255.0  # train.py: RGB/255, NCHW
        q = np.clip(
            np.rint(v / float(self.iinfo.scale) + float(self.iinfo.zero_point)), -128, 127
        ).astype(np.int8)
        if q.ndim == 2:
            q = q[:, :, None]
        (_pn0, _pn1), (pt, _pb), (pl, _pr), (pc0, _pc1) = self.iinfo.padding
        self.ibuf[:] = 0
        self.ibuf[0, pt : pt + self.height, pl : pl + self.width, pc0 : pc0 + self.channels] = q
        self.inst.run([self.ibuf], [self.obuf])
        f = (self.obuf.astype(np.float32) - np.float32(self.oinfo.zero_point)) * np.float32(
            self.oinfo.scale
        )
        cpad = self.oinfo.padding[3][1]
        if cpad:
            f = f[..., :-cpad]
        return f.reshape(-1)


def load_classifiers(model_dirs: list[str], *, aipu_cores: int = 1) -> dict[str, Any]:
    """Every classifier that loads, by name. A failure is logged, not fatal:
    the sidecar still serves the camera and whatever else loaded, and the
    router answers ``/classify`` for the missing one with a capability error."""
    out: dict[str, Any] = {}
    for model_dir in model_dirs or []:
        try:
            clf = MetisClassifier(model_dir, aipu_cores=aipu_cores)
        except Exception as exc:  # noqa: BLE001
            log.warning("classifier %s: unavailable (%r)", model_dir, exc)
            continue
        if clf.name in out:
            log.warning("classifier %s: duplicate name %r, keeping the first", model_dir, clf.name)
            clf.close()
            continue
        out[clf.name] = clf
        log.info(
            "classifier %s: %s -> %s, region %s",
            clf.name,
            model_dir,
            "/".join(clf.classes),
            list(clf.crop) if clf.crop else "whole frame",
        )
    return out

"""MCP tool surface for the bench vision device (camera + optional Metis NPU).

Per the driver-symmetric architecture, each driver owns its MCP tools and
exposes them via :py:func:`register_mcp_tools`; :py:mod:`benchctrl.mcp` calls it
at startup. Connection state (``_vision``) lives here so tests can inject fakes.

Two choices shape this surface:

- **No tool returns image bytes.** A JPEG in a tool result would land in the
  conversation transcript, hundreds of kilobytes at a time. ``vision_frame`` and
  ``vision_trigger_capture`` take ``save_to`` and write the JPEG host-side,
  returning the path, size and SHA-256 alongside the metadata; without it they
  return metadata only. A tool that returns the frame as an *image* the model can
  look at is on the ROADMAP — there is no image-return precedent here yet.
- **``vision_open`` goes through ``session.resolve``**, so the tools work in
  local, remote and sim mode unchanged. The URL is the sidecar's address on the
  host that opens the driver: on an agent that is the agent's loopback, and the
  host's config never needs to know it.
"""

from __future__ import annotations

import hashlib
import logging
import threading
from pathlib import Path
from typing import Any, Optional, Union

log = logging.getLogger("benchctrl.drivers.bench_vision.mcp_tools")

_vision = None
_vision_lock = threading.RLock()


def _get_vision():
    from benchctrl.drivers.bench_vision.driver import VisionConnectionError

    with _vision_lock:
        if _vision is None:
            raise VisionConnectionError("vision not open — call vision_open() first.")
        return _vision


def _frame_result(frame: Any, save_to: Optional[str]) -> dict:
    out = frame.to_dict()
    out["sha256"] = hashlib.sha256(frame.jpeg).hexdigest()
    if save_to:
        path = Path(save_to).expanduser()
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(frame.jpeg)
        out["path"] = str(path)
    return out


def vision_open(url: Optional[str] = None, timeout_s: float = 5.0) -> dict:
    """Connect to the bench vision sidecar (camera, and a Metis NPU where present).

    ``url`` is where the ``benchctrl-vision`` sidecar listens **on the host that
    opens it** — leave it unset and the driver uses ``BENCHCTRL_VISION_URL`` or
    ``http://127.0.0.1:8095``. In remote mode the bench agent opens the driver
    against its own loopback sidecar, so a URL passed here is only meaningful in
    local mode.

    Returns the identity: camera model and serial, sidecar version, and
    ``aipu_present``. When that is ``False`` every detection tool fails with a
    capability error — the Metis needs PCIe, which an Arduino Uno Q lacks.
    """
    global _vision
    from benchctrl import session
    from benchctrl.drivers.bench_vision import BenchVision

    with _vision_lock:
        if _vision is not None:
            return {
                "error": "vision already open",
                "guidance": "Call vision_close() before reopening.",
                "url": _vision.url,
            }
        _vision = session.resolve(
            "bench_vision",
            opener=BenchVision.open,
            open_kwargs={"url": url, "timeout_s": timeout_s},
        )
    return _vision.read_identity().to_dict()


def vision_close() -> dict:
    """Forget the sidecar. Changes nothing on the camera; the sidecar keeps running."""
    global _vision
    with _vision_lock:
        if _vision is None:
            return {"closed": False, "note": "vision was not open"}
        _vision.close()
        _vision = None
    return {"closed": True}


def vision_info() -> dict:
    """Camera model/serial, sidecar version, and whether an AIPU + model are loaded."""
    return _get_vision().read_identity().to_dict()


def vision_status() -> dict:
    """Everything the sidecar knows: exposure, gain, fps, crop, frame counter,
    last trigger seq, dropped triggers, AIPU temperature and last inference time."""
    return _get_vision().read_status().to_dict()


def vision_frame(
    wait_for: Optional[int] = None,
    wait_s: float = 5.0,
    save_to: Optional[str] = None,
) -> dict:
    """The latest frame's metadata, optionally saving the JPEG to ``save_to``.

    With ``wait_for`` (a frame id from an earlier result) this waits up to
    ``wait_s`` for a *newer* frame; in triggered mode that only ends when a
    trigger fires. Read-only: fires no trigger, needs no writer claim.
    Returns ``frame_id``, ``seq``, size, dimensions, crop, ``sha256`` and — with
    ``save_to`` — the ``path`` written.
    """
    frame = _get_vision().read_frame(wait_for=wait_for, wait_s=wait_s)
    return _frame_result(frame, save_to)


def vision_trigger_capture(
    seq: Optional[int] = None,
    infer: bool = False,
    min_conf: float = 0.4,
    wait_s: float = 2.0,
    save_to: Optional[str] = None,
    classify: Optional[str] = None,
    min_margin: float = 3.0,
) -> dict:
    """Fire a software trigger tagged ``seq`` and return that exact frame.

    The frame returned **carries the requested ``seq``** or the call fails with
    a capture error — a frame from an earlier trigger is never passed off as
    this one. Choose ``seq`` yourself when correlating captures with commanded
    bench state; unset, it defaults to a millisecond timestamp.

    ``infer=True`` also runs the detector on the frame (boxes with class,
    label, score and coordinates in the result under ``detections``). On a
    host with no AIPU that fails *before* the trigger fires.

    ``classify`` names an indicator classifier the sidecar serves (or ``"*"``
    for the only one loaded) and reads it off this same frame — the result's
    ``classification`` carries label, logit margin and ``confident`` (margin
    >= ``min_margin``). This is the seq-correlated way to read an LED against
    a state benchctrl just commanded.

    Needs the writer claim in remote mode: a trigger changes camera state.
    Pass ``save_to`` to write the JPEG host-side; the result never carries
    image bytes.
    """
    want: Union[bool, str, None] = True if classify == "*" else classify
    frame = _get_vision().trigger_capture(
        seq=seq,
        infer=infer,
        min_conf=min_conf,
        wait_s=wait_s,
        classify=want,
        min_margin=min_margin,
    )
    return _frame_result(frame, save_to)


def vision_detect(min_conf: float = 0.4) -> dict:
    """Run the detector on the latest frame without triggering a new one.

    Returns the model name, inference time and a list of boxes. Fails with a
    capability error on a host without an AIPU.
    """
    return _get_vision().detect(min_conf=min_conf).to_dict()


def vision_classify(name: Optional[str] = None, min_margin: float = 3.0) -> dict:
    """Read an indicator (an LED, a lamp) off the latest frame with a trained
    classifier, without triggering a new frame.

    ``name`` picks one of the classifiers listed by ``vision_status`` under
    ``classifiers``; unset, the only one loaded is used. The result carries the
    winning ``label``, the raw per-class ``scores`` (logits), the ``margin``
    between the top two and ``confident`` (margin >= ``min_margin``) — a
    hesitant read is returned, not hidden, so the caller can capture again.
    Fails with a capability error where no classifier is loaded and a value
    error when the camera's crop does not contain the region the model was
    trained on (``vision_status`` shows that region as ``crop`` per classifier).
    """
    return _get_vision().classify(name=name, min_margin=min_margin).to_dict()


def vision_set_exposure_us(exposure_us: float) -> dict:
    """Set the exposure in microseconds. Returns what the camera read back,
    which is clamped to its range and may differ from what was asked.

    Tune exposure before anything else: signal-to-noise beats frame rate for
    LED and indicator reading."""
    return {"exposure_us": _get_vision().set_exposure_us(exposure_us)}


def vision_set_gain_db(gain_db: float) -> dict:
    """Set the analog gain in dB. Returns the read-back value."""
    return {"gain_db": _get_vision().set_gain_db(gain_db)}


def vision_set_fps(fps: float) -> dict:
    """Set the free-run frame rate. Has no effect while the camera is triggered."""
    return {"fps": _get_vision().set_fps(fps)}


def vision_set_crop(x: int, y: int, w: int, h: int) -> dict:
    """Restrict frames to a region of interest in sensor pixels. Detections and
    frame dimensions are then relative to the crop. Returns the applied crop."""
    return {"crop": _get_vision().set_crop(x, y, w, h).to_dict()}


def vision_clear_crop() -> dict:
    """Back to the full sensor."""
    _get_vision().clear_crop()
    return {"crop": None}


_TOOLS = (
    vision_open,
    vision_close,
    vision_info,
    vision_status,
    vision_frame,
    vision_trigger_capture,
    vision_detect,
    vision_classify,
    vision_set_exposure_us,
    vision_set_gain_db,
    vision_set_fps,
    vision_set_crop,
    vision_clear_crop,
)


def register_mcp_tools(mcp) -> None:
    """Register every vision MCP tool on the shared FastMCP server."""
    for fn in _TOOLS:
        mcp.tool()(fn)

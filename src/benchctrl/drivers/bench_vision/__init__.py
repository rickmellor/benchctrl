"""Bench vision — a camera and, where the host has one, an Axelera Metis NPU.

Implements **no Protocol** from :py:mod:`benchctrl.interfaces`, deliberately.
``CONTRIBUTING.md`` convention 3 defines a Protocol when the *second* instance
of a shape lands; this is the bench's first camera, and a ``Camera`` Protocol
generalised from one sample would bake in this sidecar's particulars (software
trigger with ``seq`` tagging, a JPEG per frame, an optional detector behind the
same endpoint). ``rigol_dp2031/__init__.py`` and ``silabs_cp2112/__init__.py``
are the precedents for saying so in the docstring rather than implying it.

The device key is ``bench_vision`` rather than ``<vendor>_<model>`` — the first
key that is not — because what the agent serves is one HTTP surface fronting a
camera and *optionally* an NPU, and which of those a host has is a fact carried
as data (``aipu_present``, ``model_name``), not in the key. Naming it after
Axelera would be false on a camera-only host; naming it after Basler would
invite a second key when the NPU is present. Discovery still identifies the
camera by its exact USB id.

The Metis needs PCIe, so on an Arduino Uno Q this device is camera-only at best
and ``detect`` raises :py:class:`VisionCapabilityError`. See
``KNOWN_LIMITATIONS.md`` § V-1.
"""

from benchctrl.drivers.bench_vision.driver import (
    DEFAULT_URL,
    MAX_WAIT_S,
    STATUS_TTL_S,
    URL_ENV,
    BenchVision,
    Crop,
    Detection,
    Detections,
    Frame,
    VisionCapabilityError,
    VisionCaptureError,
    VisionConnectionError,
    VisionError,
    VisionInfo,
    VisionProtocolError,
    VisionStatus,
    VisionTimeoutError,
    VisionValueError,
)

__all__ = [
    "DEFAULT_URL",
    "MAX_WAIT_S",
    "STATUS_TTL_S",
    "URL_ENV",
    "BenchVision",
    "Crop",
    "Detection",
    "Detections",
    "Frame",
    "VisionCapabilityError",
    "VisionCaptureError",
    "VisionConnectionError",
    "VisionError",
    "VisionInfo",
    "VisionProtocolError",
    "VisionStatus",
    "VisionTimeoutError",
    "VisionValueError",
]

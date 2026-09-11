"""``benchctrl-vision`` — the vision sidecar's entry point.

Runs the stdlib router (:py:mod:`benchctrl.vision.service`) with the real
camera (:py:mod:`benchctrl.vision.camera`) and, when a model is given and the
AIPU answers, the real detector (:py:mod:`benchctrl.vision.detector`).
Binds loopback by default: the sidecar has no authentication, and the
benchctrl agent is the network face.

Normally started by ``benchctrl-vision.service`` inside the container from
``deploy/vision/``; runnable by hand for bring-up::

    benchctrl-vision --triggered --exposure-us 6000 --model /models/yolov8n-coco.axm
    curl -s localhost:8095/health
"""

from __future__ import annotations

import argparse
import logging
import signal
import sys
import threading
from typing import Optional

from benchctrl._version import __version__

log = logging.getLogger("benchctrl.vision.server")


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="benchctrl-vision",
        description="Serve a Basler camera (and an Axelera Metis NPU, where present) to benchctrl.",
    )
    p.add_argument("--bind", default="127.0.0.1", help="address to listen on (default loopback)")
    p.add_argument("--port", type=int, default=8095)
    p.add_argument("--camera-serial", default=None, help="pick one camera by serial number")
    p.add_argument(
        "--view-port",
        type=int,
        default=0,
        help="also serve /stream and /frame.jpg (read-only) on this port, on --view-bind; 0 = off",
    )
    p.add_argument(
        "--view-bind",
        default="0.0.0.0",
        help="address for the read-only view listener (default all interfaces)",
    )
    mode = p.add_mutually_exclusive_group()
    mode.add_argument(
        "--triggered",
        dest="triggered",
        action="store_true",
        default=True,
        help="frames only on trigger (default)",
    )
    mode.add_argument(
        "--free-run", dest="triggered", action="store_false", help="continuous acquisition at --fps"
    )
    p.add_argument("--exposure-us", type=float, default=8000.0)
    p.add_argument("--gain-db", type=float, default=0.0)
    p.add_argument("--fps", type=float, default=60.0)
    p.add_argument("--jpeg-quality", type=int, default=80)
    p.add_argument("--model", default=None, help="path to a compiled .axm (YOLOv8n-COCO)")
    p.add_argument("--aipu-cores", type=int, default=4)
    p.add_argument("--no-aipu", action="store_true", help="serve the camera only")
    p.add_argument("--log-level", default="info", choices=["debug", "info", "warning", "error"])
    return p


def main(argv: Optional[list[str]] = None) -> int:
    args = build_parser().parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )
    from benchctrl.vision.camera import PylonCamera
    from benchctrl.vision.service import VIEW_ROUTES, VisionService, serve

    camera = PylonCamera(
        serial=args.camera_serial,
        triggered=args.triggered,
        exposure_us=args.exposure_us,
        gain_db=args.gain_db,
        fps=args.fps,
        jpeg_quality=args.jpeg_quality,
    )
    detector = None
    if not args.no_aipu:
        from benchctrl.vision.detector import load_detector

        detector = load_detector(args.model, aipu_cores=args.aipu_cores)
    service = VisionService(camera, detector, version=__version__)
    server = serve(service, bind=args.bind, port=args.port)
    view = None
    if args.view_port:
        view = serve(service, bind=args.view_bind, port=args.view_port, allow=VIEW_ROUTES)
    stop = threading.Event()

    def _stop(signum, _frame):
        log.info("signal %d — stopping", signum)
        stop.set()

    signal.signal(signal.SIGTERM, _stop)
    signal.signal(signal.SIGINT, _stop)
    thread = threading.Thread(
        target=server.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
    )
    thread.start()
    if view is not None:
        threading.Thread(
            target=view.serve_forever, kwargs={"poll_interval": 0.2}, daemon=True
        ).start()
        log.info(
            "view listener (stream + still only) on http://%s:%d", args.view_bind, args.view_port
        )
    log.info(
        "benchctrl-vision %s serving %s (%s) on http://%s:%d — aipu=%s",
        __version__,
        camera.model,
        camera.serial,
        args.bind,
        args.port,
        "yes" if detector is not None else "no",
    )
    if args.bind not in ("127.0.0.1", "localhost", "::1"):
        log.warning(
            "bound to %s: the sidecar has NO authentication; keep it on loopback", args.bind
        )
    try:
        while not stop.is_set():
            stop.wait(1.0)
    finally:
        server.shutdown()
        server.server_close()
        if view is not None:
            view.shutdown()
            view.server_close()
        service.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

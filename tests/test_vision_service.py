"""The vision sidecar's HTTP surface, driven directly and over a socket.

:py:mod:`benchctrl.vision.service` is the one router both the production
sidecar and the simulator run, so its behaviour *is* the contract the driver
is written against. These tests pin the parts of that contract a driver could
otherwise silently get wrong: the error document shape and status per type,
``seq`` landing on the next frame only, a wrong-``seq`` frame being refused
rather than returned, and the long-poll timing out when nothing triggers.

The last test is the import rule: the router and the label loop must import
with no image library, no camera SDK and no NPU runtime present, because the
simulator (and therefore CI) runs them and the agent's core never pays for
them.
"""

from __future__ import annotations

import base64
import http.client
import json
import sys
import threading
import time

import pytest

from benchctrl.sim.vision import (
    TINY_JPEG,
    CannedDetector,
    SimulatedVisionSidecar,
    SyntheticCamera,
    padded_jpeg,
)
from benchctrl.vision.service import (
    ERROR_STATUS,
    MAX_WAIT_S,
    VisionService,
)


def call(service: VisionService, method: str, target: str, body: dict | None = None):
    payload = json.dumps(body).encode() if body is not None else b""
    status, ctype, raw = service.handle(method, target, payload)
    doc = json.loads(raw) if ctype == "application/json" else raw
    return status, doc


@pytest.fixture
def service():
    cam = SyntheticCamera()
    svc = VisionService(cam, CannedDetector(), version="test")
    yield svc
    svc.close()


# ------------------------------------------------------------- the contract


def test_health_reports_camera_and_aipu(service):
    status, doc = call(service, "GET", "/health")
    assert status == 200
    assert doc["ok"] is True and doc["camera"] is True and doc["aipu"] is True


def test_health_reports_no_aipu_when_the_detector_is_absent():
    svc = VisionService(SyntheticCamera(), None, version="t")
    _, doc = call(svc, "GET", "/health")
    assert doc["aipu"] is False


def test_an_unknown_route_is_a_json_error_not_html(service):
    status, doc = call(service, "GET", "/nope")
    assert status == 404
    assert doc["error"]["type"] == "not_found"


@pytest.mark.parametrize("etype,status", sorted(ERROR_STATUS.items()))
def test_every_error_type_has_a_distinct_client_facing_status(etype, status):
    """The driver keys on ``type``; curl users key on status. Both must exist."""
    assert 400 <= status < 600


def test_a_frame_needs_a_trigger_in_triggered_mode(service):
    status, doc = call(service, "GET", "/frame.json")
    assert status == ERROR_STATUS["capture"]
    assert doc["error"]["type"] == "capture"


def test_a_long_poll_times_out_when_nothing_triggers(service):
    t0 = time.monotonic()
    status, doc = call(service, "GET", "/frame.json?wait=0&wait_s=0.2")
    assert status == ERROR_STATUS["timeout"] and doc["error"]["type"] == "timeout"
    assert time.monotonic() - t0 >= 0.2


def test_wait_s_is_bounded(service):
    status, doc = call(service, "GET", f"/frame.json?wait=0&wait_s={MAX_WAIT_S + 1}")
    assert status == 400 and doc["error"]["type"] == "value"


def test_capture_returns_the_frame_with_the_requested_seq(service):
    status, doc = call(service, "POST", "/capture", {"seq": 7})
    assert status == 200
    assert doc["seq"] == 7 and doc["frame_id"] == 1
    assert base64.b64decode(doc["jpeg_b64"]) == TINY_JPEG
    assert doc["detections"] is None


def test_seq_lands_on_the_next_frame_only(service):
    call(service, "POST", "/capture", {"seq": 1})
    call(service, "POST", "/capture", {"seq": 2})
    _, doc = call(service, "GET", "/frame.json")
    assert doc["seq"] == 2 and doc["frame_id"] == 2
    _, st = call(service, "GET", "/status")
    assert st["camera"]["trigger_seq"] == 2 and st["camera"]["dropped"] == 0


def test_a_frame_carrying_the_wrong_seq_is_refused(service):
    """The property everything downstream rests on.

    Make the camera produce a frame with another seq between the trigger and
    the wait: the service must refuse it, not hand it back as seq=9.
    """
    cam = service.camera
    real_trigger = cam.trigger

    def trigger_then_stale(seq):
        real_trigger(seq)  # produces the frame for seq…
        cam._pending = 999  # …and a stale one lands on top before we look
        cam._produce()

    cam.trigger = trigger_then_stale
    status, doc = call(service, "POST", "/capture", {"seq": 9})
    assert status == ERROR_STATUS["capture"]
    assert doc["error"]["type"] == "capture"
    assert "999" in doc["error"]["message"]


def test_a_camera_that_will_not_fire_is_a_capture_error(service):
    service.camera.fail_next_trigger = True
    status, doc = call(service, "POST", "/capture", {"seq": 3})
    assert status == ERROR_STATUS["capture"] and doc["error"]["type"] == "capture"


def test_infer_without_a_detector_is_a_capability_error_before_the_trigger():
    cam = SyntheticCamera()
    svc = VisionService(cam, None, version="t")
    status, doc = call(svc, "POST", "/capture", {"seq": 1, "infer": True})
    assert status == ERROR_STATUS["capability"] and doc["error"]["type"] == "capability"
    assert cam.frame_id == 0, "the camera was fired for a result that could not be produced"


def test_detect_runs_on_the_latest_frame_without_a_new_one(service):
    call(service, "POST", "/capture", {"seq": 5})
    status, doc = call(service, "POST", "/detect", {"min_conf": 0.8})
    assert status == 200
    assert doc["frame_id"] == 1 and doc["seq"] == 5
    assert [d["label"] for d in doc["items"]] == ["person"], "min_conf was not applied"
    assert service.camera.frame_id == 1


def test_config_put_returns_read_back_values_not_the_request(service):
    _, doc = call(service, "PUT", "/config", {"exposure_us": 0.5, "gain_db": 99})
    assert doc["exposure_us"] == SyntheticCamera.EXPOSURE_RANGE_US[0]
    assert doc["gain_db"] == SyntheticCamera.GAIN_RANGE_DB[1]


def test_config_refuses_unknown_keys(service):
    status, doc = call(service, "PUT", "/config", {"exposure": 100})
    assert status == 400 and "exposure" in doc["error"]["message"]


def test_a_crop_changes_the_reported_dimensions(service):
    call(service, "PUT", "/config", {"crop": [10, 20, 300, 200]})
    _, doc = call(service, "POST", "/capture", {"seq": 1})
    assert (doc["width"], doc["height"]) == (300, 200) and doc["crop"] == [10, 20, 300, 200]
    call(service, "PUT", "/config", {"crop": None})
    _, doc = call(service, "POST", "/capture", {"seq": 2})
    assert (doc["width"], doc["height"]) == (1920, 1200) and doc["crop"] is None


def test_the_browser_crop_endpoint_still_works(service):
    """``/crop?x=&y=&w=&h=`` and ``/crop?off`` are kept for the focus workflow."""
    _, doc = call(service, "GET", "/crop?x=1&y=2&w=3&h=4")
    assert doc["crop"] == [1, 2, 3, 4]
    _, doc = call(service, "GET", "/crop?off")
    assert doc["crop"] is None


def test_frame_jpg_returns_the_bytes(service):
    call(service, "GET", "/trigger?seq=4")
    status, ctype, raw = service.handle("GET", "/frame.jpg", b"")
    assert status == 200 and ctype == "image/jpeg" and raw == TINY_JPEG


def test_a_non_json_body_is_a_value_error(service):
    status, ctype, raw = service.handle("POST", "/capture", b"not json")
    assert status == 400 and json.loads(raw)["error"]["type"] == "value"


# ----------------------------------------------------------- over a socket


def test_the_simulated_sidecar_serves_the_same_router_over_http():
    with SimulatedVisionSidecar() as sim:
        host, port = sim.url[len("http://") :].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.request(
            "POST",
            "/capture",
            body=json.dumps({"seq": 11}),
            headers={"Content-Type": "application/json"},
        )
        resp = conn.getresponse()
        doc = json.loads(resp.read())
        assert resp.status == 200 and doc["seq"] == 11
        conn.request("GET", "/status")
        assert json.loads(conn.getresponse().read())["camera"]["frame_id"] == 1
        conn.close()
        assert ("POST", "/capture") in sim.request_log


def test_the_stream_endpoint_emits_mjpeg_parts_until_the_client_leaves():
    with SimulatedVisionSidecar() as sim:
        host, port = sim.url[len("http://") :].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.request("GET", "/stream")
        resp = conn.getresponse()
        assert resp.status == 200
        assert resp.getheader("Content-Type").startswith("multipart/x-mixed-replace")
        threading.Thread(target=lambda: sim.camera.trigger(1), daemon=True).start()
        chunk = resp.fp.read(len(b"--frame\r\nContent-Type: image/jpeg\r\n"))
        assert chunk.startswith(b"--frame")
        conn.close()


# ----------------------------------------------------------- the JPEG trick


@pytest.mark.parametrize("size", [0, 200, 300, 70_000, 200_000, 70_003])
def test_a_padded_jpeg_is_still_a_jpeg(size):
    b = padded_jpeg(size)
    assert b[:2] == b"\xff\xd8" and b[-2:] == b"\xff\xd9"
    assert len(b) == max(size, len(TINY_JPEG))
    pil = pytest.importorskip("PIL.Image")
    import io

    im = pil.open(io.BytesIO(b))
    im.load()
    assert im.size == (16, 16)


# ----------------------------------------------------------- the import rule


def test_the_router_and_label_loop_import_with_no_heavy_dependency(monkeypatch):
    """Poison every heavy import, then import the stdlib half fresh.

    The simulator, the agent's sim mode and CI all run these modules; if one
    of them grew an ``import numpy`` at module level, the failure would appear
    as an ImportError on the Uno Q, not here — unless this test exists.
    """
    for name in (
        "numpy",
        "cv2",
        "pypylon",
        "pypylon.pylon",
        "axelera",
        "axelera.runtime",
        "onnxruntime",
        "PIL",
    ):
        monkeypatch.setitem(sys.modules, name, None)
    for mod in (
        "benchctrl.vision.service",
        "benchctrl.sim.vision",
        "benchctrl.drivers.bench_vision.driver",
    ):
        monkeypatch.delitem(sys.modules, mod, raising=False)
        __import__(mod)


# ----------------------------------------------------------- the view listener


def _view_server():
    import threading

    from benchctrl.vision.service import VIEW_ROUTES, serve

    cam = SyntheticCamera()
    svc = VisionService(cam, CannedDetector(), version="t")
    srv = serve(svc, bind="127.0.0.1", port=0, allow=VIEW_ROUTES)
    threading.Thread(target=srv.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    return cam, svc, srv


def _get(srv, method, path, body=None):
    host, port = srv.server_address[:2]
    conn = http.client.HTTPConnection(str(host), int(port), timeout=5)
    conn.request(
        method, path, body=body, headers={"Content-Type": "application/json"} if body else {}
    )
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, resp.getheader("Content-Type"), data


def test_the_view_listener_serves_only_the_stream_and_the_still():
    """A LAN-facing port must be able to fire nothing and configure nothing.

    ``VIEW_ROUTES`` is the whole allowlist; everything else — including reads
    of structured state — is refused before the service sees it, so a widened
    ``allow`` set would show up here as a passing request that must fail.
    """
    cam, svc, srv = _view_server()
    try:
        cam.trigger(1)
        status, ctype, data = _get(srv, "GET", "/frame.jpg")
        assert status == 200 and ctype == "image/jpeg" and data == TINY_JPEG
        assert _get(srv, "GET", "/health")[0] == 200
        before = cam.frame_id
        for method, path, body in (
            ("POST", "/capture", json.dumps({"seq": 9})),
            ("GET", "/trigger?seq=9", None),
            ("PUT", "/config", json.dumps({"exposure_us": 1})),
            ("GET", "/crop?off", None),
            ("POST", "/detect", json.dumps({})),
            ("GET", "/status", None),
            ("GET", "/frame.json", None),
        ):
            status, ctype, data = _get(srv, method, path, body)
            assert status == 403, f"{method} {path} was served on the view port"
            assert json.loads(data)["error"]["type"] == "forbidden"
        assert cam.frame_id == before, "a refused request still fired the camera"
    finally:
        srv.shutdown()
        srv.server_close()
        svc.close()


def test_the_control_listener_is_unrestricted_by_default():
    with SimulatedVisionSidecar() as sim:
        host, port = sim.url[len("http://") :].split(":")
        conn = http.client.HTTPConnection(host, int(port), timeout=5)
        conn.request("GET", "/status")
        assert conn.getresponse().status == 200
        conn.close()


# ------------------------------------------------------------- classifiers


@pytest.fixture
def clf_service():
    from benchctrl.sim.vision import CannedClassifier

    cam = SyntheticCamera()
    clf = CannedClassifier()
    svc = VisionService(cam, CannedDetector(), {clf.name: clf}, version="test")
    svc.clf = clf
    yield svc
    svc.close()


def test_status_and_health_list_the_classifiers(clf_service):
    _, health = call(clf_service, "GET", "/health")
    assert health["classifiers"] == ["act-led-sim"]
    _, status = call(clf_service, "GET", "/status")
    (doc,) = status["classifiers"]
    assert doc["name"] == "act-led-sim"
    assert doc["classes"] == ["dark", "lit"]
    assert doc["crop"] == [1040, 620, 160, 160]
    assert doc["input"] == [3, 96, 96], "status() extras are merged in"


def test_classify_reads_the_latest_frame_and_reports_the_margin(clf_service):
    call(clf_service, "GET", "/trigger?seq=7")
    status, doc = call(clf_service, "POST", "/classify", {})
    assert status == 200
    assert doc["seq"] == 7 and doc["model_name"] == "act-led-sim"
    assert doc["label"] == "lit"
    assert doc["scores"] == {"dark": -5.4, "lit": 5.9}
    assert doc["margin"] == pytest.approx(11.3)
    assert doc["confident"] is True
    # The camera is uncropped: the model's sensor region is used as-is.
    assert doc["region"] == [1040, 620, 160, 160]
    assert clf_service.clf.region_seen == (1040, 620, 160, 160)


def test_classify_is_hesitant_below_the_margin_but_still_answers(clf_service):
    call(clf_service, "GET", "/trigger?seq=1")
    _, doc = call(clf_service, "POST", "/classify", {"min_margin": 20})
    assert doc["label"] == "lit" and doc["confident"] is False
    status, doc = call(clf_service, "POST", "/classify", {"min_margin": -1})
    assert status == ERROR_STATUS["value"]


def test_classify_translates_the_region_into_a_cropped_frame(clf_service):
    """A camera crop that contains the model's region shifts it; the frame's own
    crop is the region when they coincide."""
    call(clf_service, "GET", "/crop?x=1000&y=600&w=300&h=300")
    call(clf_service, "GET", "/trigger?seq=1")
    _, doc = call(clf_service, "POST", "/classify", {})
    assert doc["region"] == [40, 20, 160, 160]
    call(clf_service, "GET", "/crop?x=1040&y=620&w=160&h=160")
    call(clf_service, "GET", "/trigger?seq=2")
    _, doc = call(clf_service, "POST", "/classify", {})
    assert doc["region"] == [0, 0, 160, 160]


def test_classify_refuses_a_frame_that_does_not_contain_the_region(clf_service):
    call(clf_service, "GET", "/crop?x=0&y=0&w=640&h=480")
    call(clf_service, "GET", "/trigger?seq=1")
    status, doc = call(clf_service, "POST", "/classify", {})
    assert status == ERROR_STATUS["value"]
    assert doc["error"]["type"] == "value"
    assert "does not contain" in doc["error"]["message"]
    assert clf_service.clf.calls == 0, "the model must not be asked about the wrong patch"


def test_classify_without_a_classifier_is_a_capability_error(service):
    call(service, "GET", "/trigger?seq=1")
    status, doc = call(service, "POST", "/classify", {})
    assert status == ERROR_STATUS["capability"]
    assert doc["error"]["type"] == "capability"
    _, health = call(service, "GET", "/health")
    assert health["classifiers"] == []


def test_classify_by_name_and_the_ambiguity_of_several():
    from benchctrl.sim.vision import CannedClassifier

    a = CannedClassifier(name="a", crop=None)
    b = CannedClassifier({"dark": 2.0, "lit": -2.0}, name="b", crop=None)
    svc = VisionService(SyntheticCamera(), None, {"a": a, "b": b}, version="test")
    try:
        call(svc, "GET", "/trigger?seq=1")
        status, doc = call(svc, "POST", "/classify", {})
        assert status == ERROR_STATUS["value"] and "name one" in doc["error"]["message"]
        _, doc = call(svc, "POST", "/classify", {"name": "b"})
        assert doc["label"] == "dark" and doc["region"] is None
        status, doc = call(svc, "POST", "/classify", {"name": "zz"})
        assert status == ERROR_STATUS["value"] and "zz" in doc["error"]["message"]
        _, health = call(svc, "GET", "/health")
        assert health["aipu"] is True, "classifiers alone are an AIPU present"
    finally:
        svc.close()


def test_capture_can_classify_the_frame_it_returns(clf_service):
    status, doc = call(
        clf_service, "POST", "/capture", {"seq": 9, "classify": True, "min_margin": 1.0}
    )
    assert status == 200 and doc["seq"] == 9
    assert doc["classification"]["seq"] == 9
    assert doc["classification"]["label"] == "lit"
    assert doc["detections"] is None
    _, doc = call(clf_service, "POST", "/capture", {"seq": 10})
    assert doc["classification"] is None
    _, doc = call(clf_service, "GET", "/frame.json?classify=act-led-sim")
    assert doc["classification"]["seq"] == 10


def test_capture_with_an_unknown_classifier_fails_before_the_trigger(clf_service):
    before = clf_service.camera.frame_id
    status, doc = call(clf_service, "POST", "/capture", {"seq": 1, "classify": "nope"})
    assert status == ERROR_STATUS["value"]
    assert clf_service.camera.frame_id == before, "the camera fired for a result it cannot give"


def test_classifier_region_is_pure_and_precise():
    from benchctrl.vision.service import ServiceValueError, classifier_region

    assert classifier_region(None, None, 1920, 1200) is None
    assert classifier_region((10, 20, 30, 40), None, 1920, 1200) == (10, 20, 30, 40)
    assert classifier_region((10, 20, 30, 40), (10, 20, 30, 40), 30, 40) == (0, 0, 30, 40)
    assert classifier_region((10, 20, 30, 40), (5, 5, 100, 100), 100, 100) == (5, 15, 30, 40)
    with pytest.raises(ServiceValueError):
        classifier_region((10, 20, 30, 40), (5, 5, 30, 40), 30, 40)  # one pixel short
    with pytest.raises(ServiceValueError):
        classifier_region((10, 20, 30, 40), (20, 20, 100, 100), 100, 100)  # starts past it

"""The bench vision driver against the simulated sidecar, in one process.

The driver is a stdlib HTTP client; what can go wrong in it is the mapping —
of documents to dataclasses, of error types to exception classes, of "what I
asked for" to "what the camera read back" — and the one performance promise
it makes to the agent: every property is served from a single cached
``/status``. Each of those has a test here that reads the simulator's request
log or state directly, so a driver that cached the request instead of the
reply, or fetched per property, is caught rather than merely slow.

The MCP parity test at the end is the review gate from ``CONTRIBUTING.md``:
every public driver method has a tool, with the naming aliases made explicit
rather than exempted.
"""

from __future__ import annotations

import hashlib

import pytest

from benchctrl.drivers.bench_vision import (
    MAX_WAIT_S,
    STATUS_TTL_S,
    BenchVision,
    Crop,
    Detections,
    Frame,
    VisionCapabilityError,
    VisionCaptureError,
    VisionConnectionError,
    VisionInfo,
    VisionProtocolError,
    VisionStatus,
    VisionTimeoutError,
    VisionValueError,
)
from benchctrl.sim.vision import TINY_JPEG, SimulatedVisionSidecar, SyntheticCamera


@pytest.fixture
def sim():
    with SimulatedVisionSidecar() as s:
        yield s


@pytest.fixture
def vision(sim):
    v = BenchVision.open(sim.url)
    yield v
    v.close()


# ------------------------------------------------------------------ opening


def test_open_checks_health_and_reports_identity(sim, vision):
    assert ("GET", "/health") in sim.request_log
    info = vision.read_identity()
    assert isinstance(info, VisionInfo)
    assert info.camera_model.endswith("-SIM") and info.camera_serial == "SIM0VISION"
    assert info.aipu_present is True and info.model_name == "yolov8n-coco-sim"
    assert info.sensor_width == 1920 and info.sensor_height == 1200


def test_open_names_the_service_when_nothing_answers():
    with pytest.raises(VisionConnectionError, match="benchctrl-vision.service"):
        BenchVision.open("http://127.0.0.1:1")  # nothing listens on port 1


def test_open_reads_the_url_from_the_environment(sim, monkeypatch):
    monkeypatch.setenv("BENCHCTRL_VISION_URL", sim.url)
    v = BenchVision.open()
    assert v.url == sim.url
    v.close()


def test_a_non_http_url_is_refused_before_any_request():
    with pytest.raises(VisionValueError):
        BenchVision.open("127.0.0.1:8095")


def test_close_is_local_only(sim, vision):
    before = len(sim.request_log)
    vision.close()
    assert vision.is_open is False
    assert len(sim.request_log) == before, "close() must not touch the sidecar"
    with pytest.raises(VisionConnectionError):
        vision.read_status()


# ------------------------------------------------------------------ frames


def test_trigger_capture_returns_the_frame_with_that_seq(vision):
    frame = vision.trigger_capture(seq=42)
    assert isinstance(frame, Frame)
    assert frame.seq == 42 and frame.frame_id == 1
    assert frame.jpeg == TINY_JPEG and frame.size == len(TINY_JPEG)
    assert frame.crop is None and frame.detections is None


def test_trigger_capture_defaults_seq_to_something_non_negative(vision):
    frame = vision.trigger_capture()
    assert frame.seq >= 0 and vision.trigger_seq == frame.seq


def test_no_frame_without_a_trigger(vision):
    with pytest.raises(VisionTimeoutError):
        vision.read_frame(wait_for=0, wait_s=0.2)
    with pytest.raises(VisionCaptureError):
        vision.read_frame()


def test_read_frame_returns_the_latest_without_triggering(sim, vision):
    vision.trigger_capture(seq=1)
    n = sim.camera.frame_id
    frame = vision.read_frame()
    assert frame.seq == 1 and sim.camera.frame_id == n


def test_read_frame_wait_for_sees_the_next_trigger(sim, vision):
    first = vision.trigger_capture(seq=1)
    import threading

    threading.Timer(0.1, lambda: sim.camera.trigger(2)).start()
    nxt = vision.read_frame(wait_for=first.frame_id, wait_s=2.0)
    assert nxt.frame_id == first.frame_id + 1 and nxt.seq == 2


def test_a_wrong_seq_frame_is_a_capture_error(sim, vision):
    cam = sim.camera
    real = cam.trigger

    def stale_after(seq):
        real(seq)
        cam._pending = 999
        cam._produce()

    cam.trigger = stale_after
    with pytest.raises(VisionCaptureError, match="999"):
        vision.trigger_capture(seq=9)


def test_a_camera_that_will_not_fire_is_a_capture_error(sim, vision):
    sim.camera.fail_next_trigger = True
    with pytest.raises(VisionCaptureError):
        vision.trigger_capture(seq=3)
    assert vision.trigger_capture(seq=4).seq == 4, "the next trigger must work"


def test_a_dropped_trigger_is_counted(sim, vision):
    """Two triggers before a frame is produced: the first is dropped."""
    cam = sim.camera
    cam._pending = 1  # a trigger whose frame never came
    vision.trigger_capture(seq=2)
    assert vision.read_status().dropped == 1


@pytest.mark.parametrize("wait_s", [0, -1, MAX_WAIT_S + 0.1, "5", True])
def test_wait_s_is_validated_in_the_driver(vision, wait_s):
    with pytest.raises(VisionValueError):
        vision.trigger_capture(seq=1, wait_s=wait_s)
    with pytest.raises(VisionValueError):
        vision.read_frame(wait_for=0, wait_s=wait_s)


@pytest.mark.parametrize("seq", [-1, 1.5, "7", True])
def test_seq_must_be_a_non_negative_int(vision, seq):
    with pytest.raises(VisionValueError):
        vision.trigger_capture(seq=seq)


def test_a_large_frame_round_trips_intact():
    with SimulatedVisionSidecar(frame_bytes=200_000) as sim:
        v = BenchVision.open(sim.url)
        frame = v.trigger_capture(seq=1)
        assert frame.size == 200_000
        assert (
            hashlib.sha256(frame.jpeg).hexdigest()
            == hashlib.sha256(sim.camera.latest().jpeg).hexdigest()
        )
        v.close()


# ------------------------------------------------------------------ detection


def test_infer_on_capture_returns_typed_detections(vision):
    frame = vision.trigger_capture(seq=5, infer=True)
    assert isinstance(frame.detections, Detections)
    assert frame.detections.seq == 5 and frame.detections.frame_id == frame.frame_id
    assert len(frame.detections) == 2
    box = frame.detections.items[0]
    assert (box.label, box.class_id) == ("person", 0) and box.x2 > box.x1


def test_detect_applies_min_conf_and_fires_nothing(sim, vision):
    vision.trigger_capture(seq=1)
    n = sim.camera.frame_id
    dets = vision.detect(min_conf=0.8)
    assert [d.label for d in dets.items] == ["person"]
    assert sim.camera.frame_id == n


def test_min_conf_is_validated(vision):
    with pytest.raises(VisionValueError):
        vision.detect(min_conf=1.5)


def test_no_aipu_is_a_capability_error_and_the_camera_still_works():
    with SimulatedVisionSidecar(aipu=False) as sim:
        v = BenchVision.open(sim.url)
        assert v.aipu_present is False and v.model_name is None
        frame = v.trigger_capture(seq=1)
        assert frame.seq == 1
        with pytest.raises(VisionCapabilityError):
            v.detect()
        with pytest.raises(VisionCapabilityError):
            v.trigger_capture(seq=2, infer=True)
        assert sim.camera.frame_id == 1, "infer=True must fail before the trigger fires"
        v.close()


# ------------------------------------------------------------------ config


def test_exposure_and_gain_report_what_the_camera_read_back(vision):
    assert vision.set_exposure_us(0.5) == SyntheticCamera.EXPOSURE_RANGE_US[0]
    assert vision.set_gain_db(99) == SyntheticCamera.GAIN_RANGE_DB[1]
    assert vision.exposure_us == SyntheticCamera.EXPOSURE_RANGE_US[0]


@pytest.mark.parametrize("value", [0, -5, "8000", True])
def test_bad_exposure_is_refused_locally(vision, value):
    with pytest.raises(VisionValueError):
        vision.set_exposure_us(value)


def test_set_fps_reads_back(vision):
    assert vision.set_fps(30) == 30.0 and vision.fps == 30.0


def test_crop_round_trips_and_changes_frame_dimensions(vision):
    crop = vision.set_crop(10, 20, 300, 200)
    assert crop == Crop(10, 20, 300, 200) and vision.crop == crop
    frame = vision.trigger_capture(seq=1)
    assert (frame.width, frame.height) == (300, 200) and frame.crop == crop
    vision.clear_crop()
    assert vision.crop is None
    assert vision.trigger_capture(seq=2).width == 1920


@pytest.mark.parametrize("args", [(-1, 0, 10, 10), (0, 0, 0, 10), (0, 0, 10, 0), (1.5, 0, 1, 1)])
def test_bad_crops_are_refused_locally(vision, args):
    with pytest.raises(VisionValueError):
        vision.set_crop(*args)


def test_a_crop_beyond_the_sensor_is_refused_by_the_sidecar(vision):
    """The driver cannot know the sensor size; the sidecar can, and its
    refusal must arrive as a value error, not a generic failure."""
    with pytest.raises(VisionValueError, match="sensor"):
        vision.set_crop(1900, 1100, 300, 300)


# ------------------------------------------------------------------ properties


def test_all_properties_come_from_one_status_read(sim, vision):
    """The promise the agent's per-response snapshot depends on."""
    vision.trigger_capture(seq=1)
    before = sim.requests_to("/status")
    values = (
        vision.camera_model,
        vision.camera_serial,
        vision.frame_id,
        vision.trigger_seq,
        vision.crop,
        vision.exposure_us,
        vision.gain_db,
        vision.fps,
        vision.triggered,
        vision.aipu_present,
        vision.aipu_temp_c,
        vision.model_name,
    )
    assert sim.requests_to("/status") - before <= 1
    assert values[2] == 1 and values[3] == 1 and values[9] is True and values[10] == 41.5


def test_the_status_cache_expires(sim, vision):
    import time

    vision.read_status()
    n = sim.requests_to("/status")
    time.sleep(STATUS_TTL_S * 1.5)
    _ = vision.frame_id
    assert sim.requests_to("/status") == n + 1


def test_a_write_invalidates_the_cache(sim, vision):
    _ = vision.exposure_us
    n = sim.requests_to("/status")
    vision.set_exposure_us(5000)
    _ = vision.exposure_us
    assert sim.requests_to("/status") == n + 1


def test_properties_never_raise(sim, vision):
    sim.close()
    assert vision.camera_model is None
    assert vision.aipu_present is False
    assert vision.frame_id is None


def test_read_status_is_typed(vision):
    st = vision.read_status()
    assert isinstance(st, VisionStatus)
    assert st.sidecar_version == "sim" and st.aipu_cores == 4
    assert st.to_dict()["crop"] is None


def test_stream_url_is_derived_locally(sim, vision):
    n = len(sim.request_log)
    assert vision.stream_url == sim.url + "/stream"
    assert len(sim.request_log) == n


# ------------------------------------------------------------------ errors


def test_an_unknown_error_type_is_a_protocol_error(sim, vision, monkeypatch):
    from benchctrl.vision import service as svc

    monkeypatch.setitem(svc.ERROR_STATUS, "weird", 418)
    real = sim.service.handle

    def weird(method, target, body):
        if target.startswith("/detect"):
            return real("GET", "/no-such-route", b"")[0] and (
                418,
                "application/json",
                b'{"error": {"type": "weird", "message": "?"}}',
            )
        return real(method, target, body)

    monkeypatch.setattr(sim.service, "handle", weird)
    with pytest.raises(VisionProtocolError, match="weird"):
        vision.detect()


def test_frame_to_dict_never_carries_the_jpeg(vision):
    frame = vision.trigger_capture(seq=1, infer=True)
    d = frame.to_dict()
    assert "jpeg" not in d and "jpeg_b64" not in d
    assert d["bytes"] == frame.size and d["detections"]["items"][0]["label"] == "person"


# ------------------------------------------------------------------ MCP parity


def test_vision_mcp_tools_cover_the_driver_surface():
    """Every public driver method has a tool, except a documented few.

    Fails when a method is added to the driver and the tool is forgotten —
    the failure mode that leaves a capability working locally and invisible
    to an agent.
    """
    from benchctrl.drivers.bench_vision import mcp_tools as tools
    from benchctrl.drivers.bench_vision.driver import BenchVision

    # open is the classmethod behind vision_open; the tool exists under the
    # driver-symmetric name and takes the same arguments.
    exempt = {"open"}
    # Tool names drop the read_ prefix, matching the other drivers (sdm4065a_info,
    # not sdm4065a_read_identity). Normalise rather than exempt.
    aliases = {
        "read_identity": "info",
        "read_status": "status",
        "read_frame": "frame",
    }
    methods = {
        aliases.get(name, name)
        for name in vars(BenchVision)
        if not name.startswith("_") and callable(getattr(BenchVision, name))
    } - exempt
    tool_names = {fn.__name__[len("vision_") :] for fn in tools._TOOLS}
    missing = methods - tool_names
    assert not missing, f"driver methods with no MCP tool: {sorted(missing)}"


def test_every_vision_tool_is_registered_and_re_exported():
    """A tool absent from mcp.py is invisible to an agent even though it exists."""
    m = pytest.importorskip("benchctrl.mcp")
    from benchctrl.drivers.bench_vision import mcp_tools as tools

    for fn in tools._TOOLS:
        assert hasattr(m, fn.__name__), f"{fn.__name__} not re-exported from mcp.py"


def test_no_vision_tool_returns_raw_bytes(sim, tmp_path):
    """A JPEG in a tool result lands in a transcript. ``save_to`` is the way."""
    from benchctrl import session
    from benchctrl.config import Config, DeviceConfig
    from benchctrl.drivers.bench_vision import mcp_tools as tools

    session.configure(Config(devices={"bench_vision": DeviceConfig(mode="sim")}))
    try:
        tools._vision = None
        out = tools.vision_open()
        assert out["camera_model"].endswith("-SIM")
        cap = tools.vision_trigger_capture(seq=3, infer=True, save_to=str(tmp_path / "f.jpg"))
        assert (tmp_path / "f.jpg").read_bytes()[:2] == b"\xff\xd8"
        assert cap["sha256"] == hashlib.sha256((tmp_path / "f.jpg").read_bytes()).hexdigest()
        for result in (
            cap,
            tools.vision_frame(),
            tools.vision_status(),
            tools.vision_info(),
            tools.vision_detect(),
        ):
            for v in _walk(result):
                assert not isinstance(v, (bytes, bytearray)), "a tool returned raw bytes"
                if isinstance(v, str):
                    assert len(v) < 4096, "a tool returned something JPEG-sized as text"
        assert tools.vision_close() == {"closed": True}
    finally:
        tools._vision = None
        session.configure(None)


def _walk(value):
    if isinstance(value, dict):
        for v in value.values():
            yield from _walk(v)
    elif isinstance(value, (list, tuple)):
        for v in value:
            yield from _walk(v)
    else:
        yield value


# ------------------------------------------------------------- classifiers


def test_classify_returns_a_typed_read_with_the_margin(sim, vision):
    from benchctrl.drivers.bench_vision import Classification

    vision.trigger_capture(seq=4)
    read = vision.classify()
    assert isinstance(read, Classification)
    assert read.seq == 4 and read.model_name == "act-led-sim"
    assert read.label == "lit" and read.confident is True
    assert read.margin == pytest.approx(11.3)
    assert read.scores == {"dark": -5.4, "lit": 5.9}
    assert read.region == Crop(1040, 620, 160, 160)
    assert sim.camera.frame_id == 1, "classify must not fire the camera"
    assert read.to_dict()["region"] == {"x": 1040, "y": 620, "w": 160, "h": 160}


def test_classify_margin_gate_and_argument_checks(sim, vision):
    vision.trigger_capture(seq=1)
    assert vision.classify(min_margin=20).confident is False
    assert vision.classify(name="act-led-sim").label == "lit"
    with pytest.raises(VisionValueError):
        vision.classify(name="nope")
    with pytest.raises(VisionValueError):
        vision.classify(name="")
    with pytest.raises(VisionValueError):
        vision.classify(min_margin=-0.1)
    with pytest.raises(VisionValueError):
        vision.classify(min_margin=True)  # type: ignore[arg-type]


def test_classify_without_a_classifier_is_a_capability_error():
    with SimulatedVisionSidecar(aipu=False) as s:
        v = BenchVision.open(s.url)
        try:
            v.trigger_capture(seq=1)
            assert v.classifiers == []
            with pytest.raises(VisionCapabilityError):
                v.classify()
        finally:
            v.close()


def test_classify_refuses_a_crop_that_misses_the_region(sim, vision):
    vision.set_crop(0, 0, 640, 480)
    vision.trigger_capture(seq=1)
    with pytest.raises(VisionValueError, match="does not contain"):
        vision.classify()


def test_trigger_capture_can_classify_the_frame_it_returns(sim, vision):
    from benchctrl.drivers.bench_vision import Classification

    frame = vision.trigger_capture(seq=12, classify=True)
    assert isinstance(frame.classification, Classification)
    assert frame.classification.seq == 12 and frame.classification.label == "lit"
    assert frame.to_dict()["classification"]["label"] == "lit"
    frame = vision.trigger_capture(seq=13, classify="act-led-sim", min_margin=50)
    assert frame.classification.confident is False
    assert vision.trigger_capture(seq=14).classification is None
    before = sim.camera.frame_id
    with pytest.raises(VisionValueError):
        vision.trigger_capture(seq=15, classify="nope")
    assert sim.camera.frame_id == before, "the camera fired for a result it cannot give"


def test_classifiers_property_comes_from_the_cached_status(sim, vision):
    assert vision.classifiers == ["act-led-sim"]
    assert vision.read_identity().classifiers == ("act-led-sim",)
    assert vision.read_status().classifiers == ("act-led-sim",)
    n = sim.requests_to("/status")
    _ = (vision.classifiers, vision.model_name, vision.aipu_present)
    assert sim.requests_to("/status") - n <= 1


def test_vision_classify_tool_returns_json_only(tmp_path):
    from benchctrl import session
    from benchctrl.config import Config, DeviceConfig
    from benchctrl.drivers.bench_vision import mcp_tools as tools

    session.configure(Config(devices={"bench_vision": DeviceConfig(mode="sim")}))
    try:
        tools._vision = None
        tools.vision_open()
        cap = tools.vision_trigger_capture(seq=3, classify="*", save_to=str(tmp_path / "f.jpg"))
        assert cap["classification"]["label"] == "lit"
        read = tools.vision_classify(min_margin=1.0)
        assert read["label"] == "lit" and read["confident"] is True
        for v in _walk(read):
            assert not isinstance(v, (bytes, bytearray))
    finally:
        tools.vision_close()
        tools._vision = None
        session.configure(None)

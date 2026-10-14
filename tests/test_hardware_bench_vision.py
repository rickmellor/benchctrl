"""The vision device against a real sidecar — camera, and the Metis if present.

Skips cleanly when no sidecar answers, so the suite is safe to run anywhere;
on the bench (``benchpi``) run it with::

    pytest -m hardware tests/test_hardware_bench_vision.py -q

Point ``BENCHCTRL_VISION_URL`` at the sidecar if it is not on loopback :8095.
These are the checks the simulator cannot make: that pylon really produces a
frame per trigger, that ``seq`` really lands on it, that the padded JPEG the
simulator ships is not a fiction OpenCV would reject, and that the NPU answers.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.hardware

URL = os.environ.get("BENCHCTRL_VISION_URL", "http://127.0.0.1:8095")


@pytest.fixture(scope="module")
def cam():
    from benchctrl.drivers.bench_vision import BenchVision, VisionConnectionError

    try:
        v = BenchVision.open(URL, timeout_s=3.0)
    except VisionConnectionError as exc:
        pytest.skip(f"no vision sidecar at {URL}: {exc}")
    yield v
    v.close()


def test_identity_names_a_real_camera(cam):
    info = cam.read_identity()
    assert info.camera_serial and not info.camera_model.endswith("-SIM")
    assert info.sensor_width >= 640 and info.sensor_height >= 480


def test_a_trigger_produces_exactly_one_frame_with_that_seq(cam):
    before = cam.read_status().frame_id
    frame = cam.trigger_capture(seq=4242, wait_s=3.0)
    assert frame.seq == 4242
    assert frame.frame_id == before + 1
    assert frame.jpeg[:2] == b"\xff\xd8" and frame.jpeg[-2:] == b"\xff\xd9"
    assert cam.read_status().frame_id == before + 1, "a trigger produced more than one frame"


def test_seq_correlation_holds_over_ten_triggers(cam):
    for seq in range(100, 110):
        assert cam.trigger_capture(seq=seq, wait_s=3.0).seq == seq


def test_the_frame_decodes(cam):
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    frame = cam.trigger_capture(seq=7, wait_s=3.0)
    img = cv2.imdecode(np.frombuffer(frame.jpeg, np.uint8), cv2.IMREAD_COLOR)
    assert img is not None and img.shape[1] == frame.width and img.shape[0] == frame.height


def test_the_simulators_padded_jpeg_is_a_real_jpeg_to_opencv():
    """The sim grows its JPEG with COM segments. Prove a real decoder agrees."""
    cv2 = pytest.importorskip("cv2")
    import numpy as np

    from benchctrl.sim.vision import padded_jpeg

    img = cv2.imdecode(np.frombuffer(padded_jpeg(200_000), np.uint8), cv2.IMREAD_GRAYSCALE)
    assert img is not None and img.shape == (16, 16)


def test_exposure_reads_back_from_the_camera(cam):
    original = cam.read_status().exposure_us
    try:
        got = cam.set_exposure_us(5000)
        assert abs(got - 5000) < 50, "pylon quantises exposure; 5000 µs must land close"
        assert abs(cam.read_status().exposure_us - got) < 1e-6
        floor = cam.set_exposure_us(0.001)
        assert floor > 0, "an impossible exposure must clamp to the camera's minimum, not fail"
    finally:
        cam.set_exposure_us(original)


def test_a_crop_changes_the_frame_the_camera_returns(cam):
    try:
        crop = cam.set_crop(100, 100, 320, 240)
        frame = cam.trigger_capture(seq=9, wait_s=3.0)
        assert (frame.width, frame.height) == (320, 240) and frame.crop == crop
    finally:
        cam.clear_crop()
    assert cam.trigger_capture(seq=10, wait_s=3.0).width > 320


def test_reopen_is_clean(cam):
    from benchctrl.drivers.bench_vision import BenchVision

    again = BenchVision.open(URL)
    try:
        assert again.trigger_capture(seq=11, wait_s=3.0).seq == 11
    finally:
        again.close()


def test_detection_runs_where_there_is_an_aipu(cam):
    from benchctrl.drivers.bench_vision import VisionCapabilityError

    if not cam.aipu_present:
        with pytest.raises(VisionCapabilityError):
            cam.detect()
        pytest.skip("no AIPU on this host — the capability error above is the expected answer")
    frame = cam.trigger_capture(seq=12, infer=True, wait_s=5.0)
    assert frame.detections is not None
    assert frame.detections.model_name and frame.detections.infer_ms > 0
    assert cam.read_status().infer_ms_last == pytest.approx(frame.detections.infer_ms, rel=0.5)


def test_aipu_status_is_reported_or_honestly_none(cam):
    st = cam.read_status()
    if st.aipu_present:
        assert st.aipu_firmware is None or st.aipu_firmware[0].isdigit()
        assert st.aipu_temp_c is None or 0 < st.aipu_temp_c < 120

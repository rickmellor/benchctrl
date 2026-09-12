"""The camera confirms what the generator read back: the Output keys' backlights.

Needs the generator (``BENCHCTRL_SDG1032X``), the vision sidecar
(``BENCHCTRL_VISION_URL``) and the two classifiers ``sdg-out1``/``sdg-out2``
trained by the label loop (``examples/vision/sdg-out*.json``). The driver
never imports vision; this test is the one place the two meet, and it is the
optical half of the read-back rule: ``set_output`` says the channel is on,
the key light agrees, or the test fails.
"""

from __future__ import annotations

import os

import pytest

pytestmark = pytest.mark.hardware

RESOURCE = os.environ.get("BENCHCTRL_SDG1032X")
URL = os.environ.get("BENCHCTRL_VISION_URL", "http://127.0.0.1:8095")


@pytest.fixture(scope="module")
def bench():
    if not RESOURCE:
        pytest.skip("BENCHCTRL_SDG1032X not set")
    from benchctrl.drivers.bench_vision import BenchVision, VisionConnectionError
    from benchctrl.drivers.siglent_sdg1032x import SDG1032XConnectionError, SiglentSDG1032X

    try:
        cam = BenchVision.open(URL)
    except VisionConnectionError as exc:
        pytest.skip(f"no vision sidecar at {URL}: {exc}")
    missing = {"sdg-out1", "sdg-out2"} - set(cam.classifiers)
    if missing:
        cam.close()
        pytest.skip(f"classifiers not loaded: {sorted(missing)}")
    try:
        gen = SiglentSDG1032X.open(None if RESOURCE == "auto" else RESOURCE, max_amplitude_vpp=5.0)
    except SDG1032XConnectionError as exc:
        cam.close()
        pytest.skip(f"no SDG1032X: {exc}")
    try:
        yield gen, cam
    finally:
        try:
            gen.disable_outputs()
        finally:
            gen.close()
            cam.close()


@pytest.mark.parametrize("channel", [1, 2])
def test_the_key_light_agrees_with_the_read_back(bench, channel):
    import time

    gen, cam = bench
    name = f"sdg-out{channel}"
    gen.set_basic_wave(
        channel, wave_type="SINE", frequency_hz=1000, amplitude_vpp=1.0, offset_v=0.0
    )
    for want in (False, True, False):
        st = gen.set_output(channel, want)
        assert st.enabled is want
        time.sleep(1.0)
        seq = 30000 + channel * 100 + int(want)
        read = cam.trigger_capture(seq=seq, classify=name).classification
        assert read.seq == seq
        assert read.confident, f"hesitant read: {read}"
        assert read.label == ("on" if want else "off"), (
            f"camera saw {read.label!r} after set_output({want})"
        )

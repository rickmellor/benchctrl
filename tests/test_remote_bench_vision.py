"""The vision device in remote mode, end to end.

A driver can pass every local test and still be unreachable through the agent:
a missing entry in one of the registries, a return type the codec drops, an
exception the wire cannot reconstruct. None of those failures are visible to
:py:mod:`tests.test_bench_vision`, which never leaves the process.

So the stack under test here is complete — proxy, wire protocol, agent dispatch,
device worker, production driver, the simulated sidecar over a real socket. Only
pylon and the NPU are fake.

Three things carry extra weight for this device:

- ``Frame.jpeg`` above 64 KB must ride the **blob** path and arrive byte-exact.
  That is the existing blob store doing its job with no vision-specific code in
  ``net/``; if it ever regresses, this is where it shows.
- ``VisionCapabilityError`` and ``VisionCaptureError`` must keep their types,
  because "no NPU here" (fall back), "not the frame you asked for" (discard)
  and "timed out" (retry) call for three different responses.
- ``trigger_capture`` must be a mutator and ``read_frame``/``detect`` must not,
  or either a dashboard cannot watch the bench or any observer can fire the
  camera.
"""

from __future__ import annotations

import hashlib

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"

#: Over the 64 KB inline limit, so frames cross as blob references.
FRAME_BYTES = 200_000


def _bench(driver):
    registry = DeviceRegistry()
    registry.register_open("bench_vision", driver)
    agent = BenchAgent(registry, token=TOKEN, deadman_s=5.0, heartbeat_s=1.0)
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = EndpointConfig(
        host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=1.0, deadman_s=5.0
    )
    client = RemoteClient(endpoint).connect()
    return server, client, agent


@pytest.fixture()
def remote():
    """An agent serving a simulated vision sidecar, and an attached remote proxy.

    Built through :py:func:`make_bench_vision` so the agent holds the
    *production* driver, exactly as it would on the Pi.
    """
    from benchctrl.sim.factories import make_bench_vision

    driver = make_bench_vision(sim={"frame_bytes": FRAME_BYTES})
    server, client, agent = _bench(driver)
    try:
        proxy = client.attach("bench_vision")
        yield type(
            "RemoteBench",
            (),
            {
                "proxy": proxy,
                "driver": driver,
                "sim": driver._benchctrl_sim,
                "client": client,
                "agent": agent,
            },
        )
    finally:
        try:
            client.close()
        finally:
            server.stop()
            driver.close()


# --------------------------------------------------------------------------
# Reachability
# --------------------------------------------------------------------------


def test_the_device_key_is_servable(remote):
    assert remote.proxy is not None


def test_the_key_is_in_the_canonical_device_list():
    from benchctrl.config import DEVICE_KEYS

    assert "bench_vision" in DEVICE_KEYS


def test_the_agent_can_build_the_key_in_simulate_mode():
    from benchctrl.agent.registry import build_default_registry

    assert "bench_vision" in build_default_registry(["bench_vision"], simulate=True).keys


def test_the_agent_can_build_the_key_for_hardware():
    from benchctrl.agent.registry import build_default_registry

    assert "bench_vision" in build_default_registry(["bench_vision"]).keys


def test_a_sim_factory_exists_for_the_key():
    from benchctrl.sim.factories import factory_for

    assert factory_for("bench_vision") is not None


def test_the_sim_factory_ignores_a_url_and_picks_its_own_socket():
    from benchctrl.sim.factories import make_bench_vision

    driver = make_bench_vision(url="http://example.invalid:1")
    try:
        assert driver.url.startswith("http://127.0.0.1:")
    finally:
        driver.close()


def test_the_surface_is_exposed_over_the_wire(remote):
    for name in (
        "read_identity",
        "read_status",
        "read_frame",
        "detect",
        "trigger_capture",
        "set_exposure_us",
        "set_gain_db",
        "set_fps",
        "set_crop",
        "clear_crop",
    ):
        assert hasattr(remote.proxy, name), f"{name} not reachable remotely"
    for prop in (
        "camera_model",
        "frame_id",
        "trigger_seq",
        "aipu_present",
        "aipu_temp_c",
        "model_name",
        "crop",
        "exposure_us",
        "stream_url",
    ):
        assert prop in remote.proxy._properties, f"{prop} is not a remote property"


# --------------------------------------------------------------------------
# The dispatch gate
# --------------------------------------------------------------------------


def _surface():
    from benchctrl.agent.dispatch import introspect
    from benchctrl.sim.factories import make_bench_vision

    driver = make_bench_vision()
    try:
        return introspect(driver, "bench_vision")
    finally:
        driver.close()


def test_every_frame_producing_or_configuring_method_is_a_mutator():
    """The guard on the naming decision.

    ``dispatch.is_mutator`` is prefix-only. ``trigger_capture`` advances the
    frame counter and the pending seq; renamed to ``capture`` it would be
    callable by any observer and only this test would notice.
    """
    surface = _surface()
    for name in (
        "trigger_capture",
        "set_exposure_us",
        "set_gain_db",
        "set_fps",
        "set_crop",
        "clear_crop",
    ):
        assert name in surface.mutators, f"{name} changes camera state but is not a mutator"


def test_reads_are_not_mutators():
    surface = _surface()
    for name in ("read_identity", "read_status", "read_frame", "detect"):
        assert name not in surface.mutators, f"{name} only reads but needs a claim"


def test_no_method_is_named_stream():
    """``stream`` is a SPECIAL verb in dispatch with its own protocol handling;
    a driver method by that name would be hijacked rather than forwarded."""
    assert "stream" not in _surface().methods
    assert "stream" not in _surface().special


def test_the_live_agent_surface_gates_exactly_the_state_changing_methods(remote):
    """Asserted against the agent rather than local introspection — this is the
    surface a remote caller actually sees. Pinned in both directions: a missing
    entry means the camera can be fired without a claim, an extra one means a
    read grew a mutator prefix and a dashboard lost it."""
    assert remote.agent.registry.surface_of("bench_vision").mutators == frozenset(
        {"trigger_capture", "set_exposure_us", "set_gain_db", "set_fps", "set_crop", "clear_crop"}
    )


@pytest.mark.parametrize(
    "call",
    [
        pytest.param(lambda p: p.trigger_capture(seq=2), id="trigger_capture"),
        pytest.param(lambda p: p.set_exposure_us(5000), id="set_exposure_us"),
        pytest.param(lambda p: p.set_crop(0, 0, 10, 10), id="set_crop"),
        pytest.param(lambda p: p.clear_crop(), id="clear_crop"),
    ],
)
def test_state_changes_are_refused_without_the_writer_claim(remote, call):
    """``attach()`` claims automatically, so the claim is released first. A
    method named ``capture()`` would sail through here — that is the defect
    the ``trigger`` prefix prevents, and the reason to test the gate rather
    than trust the prefix table."""
    from benchctrl.net.errors import PolicyError

    remote.proxy.trigger_capture(seq=1)
    before = remote.sim.camera.frame_id
    remote.client.call("agent.release", {"device": "bench_vision"})
    try:
        with pytest.raises(PolicyError) as excinfo:
            call(remote.proxy)
        assert "claim" in str(excinfo.value).lower()
        assert remote.sim.camera.frame_id == before, "the camera fired despite the refusal"
    finally:
        remote.client.call("agent.claim", {"device": "bench_vision"})


def test_reads_still_work_without_the_writer_claim(remote):
    """The control for the test above, and the property a dashboard needs:
    watching the bench must not require taking the camera."""
    remote.proxy.trigger_capture(seq=1)
    remote.client.call("agent.release", {"device": "bench_vision"})
    try:
        assert remote.proxy.read_frame().seq == 1
        assert remote.proxy.detect().model_name == "yolov8n-coco-sim"
        assert remote.proxy.read_status().frame_id == 1
        assert remote.proxy.camera_model.endswith("-SIM")
    finally:
        remote.client.call("agent.claim", {"device": "bench_vision"})


# --------------------------------------------------------------------------
# Return types across the wire
# --------------------------------------------------------------------------


def test_identity_survives_the_codec(remote):
    info = remote.proxy.read_identity()
    assert info.camera_serial == "SIM0VISION" and info.aipu_present is True
    assert info.to_dict()["model_name"] == "yolov8n-coco-sim"


def test_status_survives_the_codec_with_a_nested_crop(remote):
    remote.proxy.set_crop(1, 2, 30, 40)
    st = remote.proxy.read_status()
    assert st.crop.w == 30 and st.to_dict()["crop"] == {"x": 1, "y": 2, "w": 30, "h": 40}


def test_a_large_frame_rides_the_blob_path_and_arrives_intact(remote):
    """The reason the codec needs no vision-specific code: bytes over 64 KB
    become a blob reference, fetched in chunks and SHA-256 verified."""
    frame = remote.proxy.trigger_capture(seq=42)
    assert frame.seq == 42 and frame.size == FRAME_BYTES
    local = remote.sim.camera.latest().jpeg
    assert hashlib.sha256(frame.jpeg).hexdigest() == hashlib.sha256(local).hexdigest()


def test_a_small_frame_rides_inline(remote):
    from benchctrl.sim.vision import TINY_JPEG

    remote.sim.camera.frame_bytes = None
    frame = remote.proxy.trigger_capture(seq=1)
    assert frame.jpeg == TINY_JPEG


def test_nested_detections_keep_their_types(remote):
    from benchctrl.drivers.bench_vision import Detection, Detections

    frame = remote.proxy.trigger_capture(seq=5, infer=True)
    assert isinstance(frame.detections, Detections)
    assert isinstance(frame.detections.items, tuple), "the codec's list must be re-tupled"
    assert isinstance(frame.detections.items[0], Detection)
    assert frame.detections.items[0].label == "person"


def test_detect_survives_the_wire(remote):
    remote.proxy.trigger_capture(seq=1)
    dets = remote.proxy.detect(min_conf=0.8)
    assert len(dets) == 1 and dets.items[0].score >= 0.8


def test_properties_are_piggybacked(remote):
    remote.proxy.trigger_capture(seq=77)
    assert remote.proxy.trigger_seq == 77
    assert remote.proxy.aipu_temp_c == 41.5
    assert remote.proxy.crop is None


def test_a_property_the_sidecar_cannot_answer_is_none_not_an_error(remote):
    remote.sim.close()
    assert remote.proxy.set_fps is not None  # the proxy still exists…
    assert remote.proxy.camera_model is None  # …and a dead sidecar reads as None


# --------------------------------------------------------------------------
# Exceptions across the wire
# --------------------------------------------------------------------------


def test_a_capability_refusal_keeps_its_type_remotely():
    """The one that matters most: "no NPU on this bench" must not look like a
    fault. The remedy is to fall back, not to retry."""
    from benchctrl.drivers.bench_vision import VisionCapabilityError
    from benchctrl.sim.factories import make_bench_vision

    driver = make_bench_vision(sim={"aipu": False})
    server, client, _agent = _bench(driver)
    try:
        proxy = client.attach("bench_vision")
        assert proxy.aipu_present is False
        assert proxy.trigger_capture(seq=1).seq == 1
        with pytest.raises(VisionCapabilityError):
            proxy.detect()
    finally:
        client.close()
        server.stop()
        driver.close()


def test_a_wrong_seq_frame_is_a_capture_error_remotely(remote):
    from benchctrl.drivers.bench_vision import VisionCaptureError

    cam = remote.sim.camera
    real = cam.trigger

    def stale_after(seq):
        real(seq)
        cam._pending = 999
        cam._produce()

    cam.trigger = stale_after
    with pytest.raises(VisionCaptureError):
        remote.proxy.trigger_capture(seq=9)


def test_a_timeout_keeps_its_type_remotely(remote):
    from benchctrl.drivers.bench_vision import VisionTimeoutError

    with pytest.raises(VisionTimeoutError):
        remote.proxy.read_frame(wait_for=0, wait_s=0.2)


def test_a_bad_argument_is_a_value_error_remotely(remote):
    from benchctrl.drivers.bench_vision import VisionValueError

    with pytest.raises(VisionValueError):
        remote.proxy.set_exposure_us(-1)


def test_every_vision_exception_is_wire_registered():
    from benchctrl.net.errors import known_class_names

    names = set(known_class_names())
    for name in (
        "VisionError",
        "VisionConnectionError",
        "VisionProtocolError",
        "VisionTimeoutError",
        "VisionValueError",
        "VisionCapabilityError",
        "VisionCaptureError",
    ):
        assert name in names, f"{name} missing from the error registry"


def test_every_vision_dataclass_is_a_wire_type():
    from benchctrl.net.codec import wire_type_names

    names = set(wire_type_names())
    for name in ("Crop", "Detection", "Detections", "Frame", "VisionInfo", "VisionStatus"):
        assert name in names, f"{name} missing from the codec allowlist"


# --------------------------------------------------------------------------
# Stateful sequences across the wire
# --------------------------------------------------------------------------


def test_seq_correlation_holds_across_the_wire(remote):
    for seq in (10, 11, 12):
        assert remote.proxy.trigger_capture(seq=seq).seq == seq
    assert remote.sim.camera.trigger_seq == 12


def test_configuration_persists_between_remote_calls(remote):
    assert remote.proxy.set_exposure_us(5000) == 5000.0
    assert remote.proxy.exposure_us == 5000.0
    assert remote.sim.camera.exposure_us == 5000.0, "the sidecar never saw the write"


def test_a_crop_really_moved_at_the_simulated_camera(remote):
    remote.proxy.set_crop(10, 20, 300, 200)
    assert remote.sim.camera.crop == (10, 20, 300, 200)
    assert remote.proxy.trigger_capture(seq=1).width == 300
    remote.proxy.clear_crop()
    assert remote.sim.camera.crop is None

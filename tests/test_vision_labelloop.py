"""The capture-and-label loop against three simulators and a fake sysfs tree.

What matters here is not that frames land in directories — it is the four
properties a labelled dataset is worthless without, each pinned by looking at
the simulators' own state rather than the loop's report:

- a frame is labelled with the state that was *commanded* when it was taken,
  and a capture that fails is a recorded discard, never a mislabel;
- states are interleaved across rounds, so a drift cannot become a class;
- every actuator is restored to how it was found, even when the loop dies
  half way, and the restore reaches the far side of the link seam;
- the manifest is complete enough to train from and to audit: per-frame
  sha256 matching the bytes on disk, the commanded actuator state, the camera
  settings read back rather than assumed.
"""

from __future__ import annotations

import hashlib
import json

import pytest

from benchctrl.vision.labelloop import (
    ACTUATOR_KINDS,
    LabelSpec,
    LabelSpecError,
    SysfsLedActuator,
    _target_key,
    build_actuators,
    run_label_capture,
)


class _FakeOutputState:
    def __init__(self, channel: int, enabled: bool) -> None:
        self.channel = channel
        self.enabled = enabled


class FakeGenerator:
    """The two calls the label loop makes on an SDG1032X, and nothing else.

    ``set_output`` answers with the *read-back*, like the driver. ``lie``
    makes the read-back disagree with the request on the named channel, the
    way a generator whose output is interlocked would report.
    """

    def __init__(self, enabled: dict[int, bool] | None = None) -> None:
        self.enabled = dict(enabled or {1: True, 2: False})
        self.calls: list[tuple[str, int, bool | None]] = []
        self.lie: set[int] = set()

    def get_output(self, channel: int) -> _FakeOutputState:
        self.calls.append(("get", channel, None))
        return _FakeOutputState(channel, self.enabled[channel])

    def set_output(self, channel: int, on: bool, *, verify: bool = True) -> _FakeOutputState:
        assert isinstance(on, bool)
        self.calls.append(("set", channel, on))
        if channel not in self.lie:
            self.enabled[channel] = on
        return _FakeOutputState(channel, self.enabled[channel])


@pytest.fixture
def leds(tmp_path):
    """A fake /sys/class/leds with ACT (trigger mmc0, lit) and PWR."""
    root = tmp_path / "leds"
    for name, trig in (("ACT", "none [mmc0] default-on"), ("PWR", "[none] default-on")):
        d = root / name
        d.mkdir(parents=True)
        (d / "trigger").write_text(trig + "\n")
        (d / "brightness").write_text("0\n")
    return root


@pytest.fixture
def bench():
    """Simulated vision + PDU + CP2112, opened through the sim factories."""
    from benchctrl.sim.factories import make_bench_vision, make_cp2112, make_pdu41002

    vision = make_bench_vision()
    pdu = make_pdu41002()
    gpio = make_cp2112(allowed_lines=(2, 3))
    try:
        yield vision, pdu, gpio
    finally:
        gpio.close()
        pdu.close()
        vision.close()


def spec_for(*states, **kw) -> LabelSpec:
    raw = {
        "name": "t",
        "states": list(states),
        "frames_per_state": 3,
        "settle_s": 0,
        "rounds": 2,
        "seq_start": 100,
    }
    raw.update(kw)
    return LabelSpec.from_dict(raw)


LED_LIT = {"label": "lit", "actuator": {"device": "sysfs_led", "led": "ACT", "brightness": 0}}
LED_DARK = {"label": "dark", "actuator": {"device": "sysfs_led", "led": "ACT", "brightness": 1}}
PDU_ON = {
    "label": "powered",
    "actuator": {"device": "cyberpower_pdu41002", "outlet": 3, "on": True},
}
PDU_OFF = {
    "label": "unpowered",
    "actuator": {"device": "cyberpower_pdu41002", "outlet": 3, "on": False},
}
GPIO_RST = {
    "label": "in_reset",
    "actuator": {"device": "silabs_cp2112", "line": 2, "asserted": True},
}
GPIO_RUN = {
    "label": "running",
    "actuator": {"device": "silabs_cp2112", "line": 2, "asserted": False},
}
SDG_ON = {
    "label": "driven",
    "actuator": {"device": "siglent_sdg1032x", "channel": 1, "output": True},
}
SDG_OFF = {
    "label": "idle",
    "actuator": {"device": "siglent_sdg1032x", "channel": 1, "output": False},
}


# ---------------------------------------------------------------- the spec


def test_a_spec_refuses_what_the_loop_will_not_do():
    with pytest.raises(LabelSpecError):
        spec_for()  # no states
    with pytest.raises(LabelSpecError):
        spec_for(LED_LIT, LED_LIT)  # duplicate label
    with pytest.raises(LabelSpecError):
        spec_for({"label": "x", "actuator": {"device": "otii_arc"}})  # not an actuator
    with pytest.raises(LabelSpecError):
        spec_for({"label": "a/b", "actuator": LED_LIT["actuator"]})  # label is a path segment
    with pytest.raises(LabelSpecError):
        spec_for(LED_LIT, wait_s=30)  # beyond the driver's bound
    with pytest.raises(LabelSpecError):
        spec_for({"label": "x", "actuator": {"device": "sysfs_led", "led": "ACT", "brightness": 7}})


def test_the_schedule_interleaves_states_within_each_round():
    spec = spec_for(LED_LIT, LED_DARK, rounds=3)
    order = [(r, s.label) for r, s in spec.schedule()]
    assert order == [(0, "lit"), (0, "dark"), (1, "lit"), (1, "dark"), (2, "lit"), (2, "dark")]
    grouped = spec_for(LED_LIT, LED_DARK, rounds=2, interleave=False)
    assert [s.label for _, s in grouped.schedule()] == ["lit", "lit", "dark", "dark"]


def test_a_spec_digest_changes_with_the_spec():
    assert spec_for(LED_LIT).digest() != spec_for(LED_DARK).digest()


# ---------------------------------------------------------------- the loop


def test_frames_are_labelled_by_the_commanded_state_and_checksummed(bench, leds, tmp_path):
    vision, pdu, gpio = bench
    spec = spec_for(LED_LIT, LED_DARK)
    out = tmp_path / "ds"
    m = run_label_capture(vision, build_actuators({}, sysfs_root=str(leds)), spec, out)
    assert m.counts == {"lit": 6, "dark": 6} and not m.discards and m.error is None
    for rec in m.frames:
        data = (out / rec.file).read_bytes()
        assert hashlib.sha256(data).hexdigest() == rec.sha256 and len(data) == rec.bytes
        assert rec.file.startswith(f"frames/{rec.label}/")
        assert rec.actuator_state == {"led": "ACT", "brightness": 0 if rec.label == "lit" else 1}
    seqs = [f.seq for f in m.frames]
    assert seqs == sorted(seqs) and len(set(seqs)) == len(seqs) and seqs[0] == 101
    labels = [f.label for f in m.frames]
    assert labels == ["lit"] * 3 + ["dark"] * 3 + ["lit"] * 3 + ["dark"] * 3, "not interleaved"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["counts"] == {"lit": 6, "dark": 6}
    assert manifest["camera"]["model"].endswith("-SIM") and manifest["camera"]["triggered"] is True
    assert manifest["spec_digest"] == spec.digest() and manifest["restored"] is True
    csv_lines = (out / "labels.csv").read_text().splitlines()
    assert csv_lines[0].startswith("file,label,seq") and len(csv_lines) == 13


def test_a_failed_capture_is_a_discard_never_a_label(bench, leds, tmp_path):
    vision, _, _ = bench
    spec = spec_for(LED_LIT, LED_DARK, rounds=1)
    sim = vision._benchctrl_sim
    n = 0

    def flaky(kind, data):
        nonlocal n
        if kind == "state" and data["label"] == "dark":
            sim.camera.fail_next_trigger = True

    m = run_label_capture(
        vision, build_actuators({}, sysfs_root=str(leds)), spec, tmp_path / "ds", on_event=flaky
    )
    assert m.counts == {"lit": 3, "dark": 2}
    assert len(m.discards) == 1 and m.discards[0]["label"] == "dark"
    assert "VisionCaptureError" in m.discards[0]["error"]
    assert not (tmp_path / "ds" / "frames" / "dark" / f"{m.discards[0]['seq']:06d}.jpg").exists()


def test_the_host_led_is_parked_during_the_run_and_restored_after(bench, leds, tmp_path):
    vision, _, _ = bench
    act = leds / "ACT"
    seen: list[str] = []

    def watch(kind, data):
        if kind == "captured":
            seen.append((act / "trigger").read_text().strip())

    run_label_capture(
        vision,
        build_actuators({}, sysfs_root=str(leds)),
        spec_for(LED_LIT, LED_DARK, rounds=1),
        tmp_path / "ds",
        on_event=watch,
    )
    assert seen and all(t == "none" for t in seen), (
        "the disk-activity trigger blinked the LED mid-run"
    )
    assert (act / "trigger").read_text().strip() == "mmc0", "trigger not restored"
    assert (act / "brightness").read_text().strip() == "0", "brightness not restored"


def test_a_missing_host_led_is_refused_before_anything_moves(bench, leds, tmp_path):
    vision, _, _ = bench
    spec = spec_for(
        {"label": "x", "actuator": {"device": "sysfs_led", "led": "NOPE", "brightness": 0}}
    )
    with pytest.raises(LabelSpecError, match="NOPE"):
        run_label_capture(vision, build_actuators({}, sysfs_root=str(leds)), spec, tmp_path / "ds")
    assert vision._benchctrl_sim.camera.frame_id == 0


def test_pdu_and_cp2112_states_are_commanded_and_restored_at_the_far_side(bench, leds, tmp_path):
    """Read back at the simulators, not through the drivers: a loop that only
    believed its own bookkeeping would pass with nothing having moved."""
    vision, pdu, gpio = bench
    pdu_sim, gpio_sim = pdu._benchctrl_sim, gpio._benchctrl_sim
    assert pdu_sim.outlet_state[3] is True
    assert not gpio_sim.direction & (1 << 2), "line 2 starts as an input"
    seen: list[tuple[str, bool, bool]] = []

    def watch(kind, data):
        if kind == "captured":
            seen.append(
                (data["label"], pdu_sim.outlet_state[3], bool(gpio_sim.direction & (1 << 2)))
            )

    spec = spec_for(PDU_ON, PDU_OFF, GPIO_RST, GPIO_RUN, rounds=1)
    m = run_label_capture(
        vision,
        build_actuators({"cyberpower_pdu41002": pdu, "silabs_cp2112": gpio}, sysfs_root=str(leds)),
        spec,
        tmp_path / "ds",
        on_event=watch,
    )
    assert m.counts == {"powered": 3, "unpowered": 3, "in_reset": 3, "running": 3}
    assert ("unpowered", False, False) in seen, "outlet 3 was never actually switched off"
    # The outlet stays as last commanded while the CP2112 states run: an
    # actuator only moves when a state names it, so it reads False here.
    assert ("in_reset", False, True) in seen, "line 2 was never actually driven"
    assert ("running", False, True) in seen, "line 2 should stay an output, released"
    # restored: outlet back on, line back to an input
    assert pdu_sim.outlet_state[3] is True
    assert not gpio_sim.direction & (1 << 2)
    assert m.as_found["cyberpower_pdu41002:3"] == {"outlet": 3, "on": True}
    assert m.as_found["silabs_cp2112:2"]["is_output"] is False


def test_an_exception_mid_run_still_restores_and_writes_the_manifest(bench, leds, tmp_path):
    vision, pdu, _ = bench
    pdu_sim = pdu._benchctrl_sim

    def boom(kind, data):
        if kind == "captured" and data["label"] == "unpowered":
            raise RuntimeError("operator hit stop")

    out = tmp_path / "ds"
    with pytest.raises(RuntimeError):
        run_label_capture(
            vision,
            build_actuators({"cyberpower_pdu41002": pdu}, sysfs_root=str(leds)),
            spec_for(PDU_OFF, PDU_ON, rounds=1),
            out,
            on_event=boom,
        )
    assert pdu_sim.outlet_state[3] is True, "outlet left off after a failure"
    manifest = json.loads((out / "manifest.json").read_text())
    assert manifest["restored"] is True and "operator hit stop" in manifest["error"]
    assert manifest["counts"] == {"unpowered": 3}


def test_a_state_naming_an_actuator_that_is_not_open_is_refused_before_anything_moves(
    bench, leds, tmp_path
):
    vision, _, _ = bench
    with pytest.raises(LabelSpecError, match="cyberpower_pdu41002"):
        run_label_capture(
            vision, build_actuators({}, sysfs_root=str(leds)), spec_for(PDU_ON), tmp_path / "ds"
        )
    assert vision._benchctrl_sim.camera.frame_id == 0


def test_camera_setup_is_applied_and_read_back_into_the_manifest(bench, leds, tmp_path):
    vision, _, _ = bench
    spec = spec_for(LED_LIT, rounds=1, crop=[100, 100, 300, 200], exposure_us=0.5, gain_db=2)
    m = run_label_capture(vision, build_actuators({}, sysfs_root=str(leds)), spec, tmp_path / "ds")
    assert m.camera["crop"] == {"x": 100, "y": 100, "w": 300, "h": 200}
    assert m.camera["exposure_us"] == 20.0, (
        "exposure is what the camera read back (clamped), not what was asked"
    )
    assert m.camera["gain_db"] == 2.0


def test_the_sanity_read_is_stored_beside_every_frame_when_asked(bench, leds, tmp_path):
    vision, _, _ = bench
    calls = []

    def fake_sanity(jpeg, roi):
        calls.append(roi)
        return 42.5

    m = run_label_capture(
        vision,
        build_actuators({}, sysfs_root=str(leds)),
        spec_for(LED_LIT, rounds=1, sanity_roi=[1, 2, 3, 4]),
        tmp_path / "ds",
        sanity=fake_sanity,
    )
    assert calls == [(1, 2, 3, 4)] * 3 and all(f.sanity == 42.5 for f in m.frames)
    assert "42.50" in (tmp_path / "ds" / "labels.csv").read_text()


def test_mean_brightness_reads_a_region_with_pillow():
    pytest.importorskip("PIL")
    from benchctrl.sim.vision import TINY_JPEG
    from benchctrl.vision.labelloop import mean_brightness

    v = mean_brightness(TINY_JPEG, (0, 0, 16, 16))
    assert 0 <= v <= 255


def test_sysfs_led_actuator_reads_the_active_trigger_from_the_bracketed_word(leds):
    a = SysfsLedActuator(str(leds))
    assert a.snapshot({"led": "ACT"}) == {"led": "ACT", "trigger": "mmc0", "brightness": 0}
    assert a.snapshot({"led": "PWR"})["trigger"] == "none"


# ---------------------------------------------------------------- SDG1032X output


def test_an_sdg_state_needs_channel_1_or_2_and_a_bool_output():
    assert "siglent_sdg1032x" in ACTUATOR_KINDS
    ok = spec_for(SDG_ON, SDG_OFF)
    assert [s.label for s in ok.states] == ["driven", "idle"]
    for bad in (
        {"channel": True, "output": True},  # a bool is not a channel number
        {"channel": 3, "output": True},  # the SDG1032X has two
        {"channel": "1", "output": True},
        {"channel": 1, "output": 1},  # not a bool
        {"channel": 1, "output": "ON"},
        {"channel": 1},
    ):
        with pytest.raises(LabelSpecError, match="SDG1032X"):
            spec_for({"label": "x", "actuator": {"device": "siglent_sdg1032x", **bad}})


def test_sdg_target_key_is_per_channel():
    assert _target_key(SDG_ON["actuator"]) == "siglent_sdg1032x:1"
    assert _target_key({"device": "siglent_sdg1032x", "channel": 2, "output": False}) == (
        "siglent_sdg1032x:2"
    )
    assert _target_key(SDG_ON["actuator"]) == _target_key(SDG_OFF["actuator"]), (
        "on and off of one channel are the same target: one snapshot, one restore"
    )


def test_the_sdg_actuator_is_built_only_when_the_generator_is_open(bench, leds, tmp_path):
    vision, _, _ = bench
    assert "siglent_sdg1032x" not in build_actuators({}, sysfs_root=str(leds))
    gen = FakeGenerator()
    built = build_actuators({"siglent_sdg1032x": gen}, sysfs_root=str(leds))
    assert built["siglent_sdg1032x"].gen is gen
    with pytest.raises(LabelSpecError, match="siglent_sdg1032x"):
        run_label_capture(
            vision, build_actuators({}, sysfs_root=str(leds)), spec_for(SDG_ON), tmp_path / "ds"
        )
    assert vision._benchctrl_sim.camera.frame_id == 0 and gen.calls == []


def test_sdg_output_states_are_commanded_labelled_from_the_read_back_and_restored(
    bench, leds, tmp_path
):
    vision, _, _ = bench
    gen = FakeGenerator({1: True, 2: False})  # channel 1 found ON
    seen: list[tuple[str, bool]] = []

    def watch(kind, data):
        if kind == "captured":
            seen.append((data["label"], gen.enabled[1]))

    out = tmp_path / "ds"
    m = run_label_capture(
        vision,
        build_actuators({"siglent_sdg1032x": gen}, sysfs_root=str(leds)),
        spec_for(SDG_ON, SDG_OFF),
        out,
        on_event=watch,
    )
    assert m.counts == {"driven": 6, "idle": 6} and not m.discards and m.error is None
    assert ("driven", True) in seen and ("idle", False) in seen, "output never actually moved"
    for rec in m.frames:
        assert rec.actuator_state == {"channel": 1, "output": rec.label == "driven"}
    assert m.as_found == {"siglent_sdg1032x:1": {"channel": 1, "output": True}}
    assert m.restored is True and gen.enabled[1] is True, "as-found ON not restored ON"
    assert gen.calls[0] == ("get", 1, None), "snapshot before the first command"
    assert gen.calls[-1] == ("set", 1, True), "the last thing the loop does is restore"
    assert gen.enabled[2] is False and not any(c[1] == 2 for c in gen.calls), "channel 2 untouched"
    manifest = json.loads((out / "manifest.json").read_text())
    assert {f["actuator_state"]["output"] for f in manifest["frames"]} == {True, False}


def test_an_sdg_read_back_that_disagrees_with_the_request_is_what_gets_recorded(
    bench, leds, tmp_path
):
    """The loop trusts the generator's read-back, not its own command: a
    channel that reports OFF after being told ON is recorded as OFF."""
    vision, _, _ = bench
    gen = FakeGenerator({1: False, 2: False})
    gen.lie.add(1)  # channel 1 stays OFF whatever it is told
    m = run_label_capture(
        vision,
        build_actuators({"siglent_sdg1032x": gen}, sysfs_root=str(leds)),
        spec_for(SDG_ON, rounds=1),
        tmp_path / "ds",
    )
    assert m.counts == {"driven": 3}
    assert all(f.actuator_state == {"channel": 1, "output": False} for f in m.frames), (
        "the manifest must carry what the generator reported, not what was asked"
    )
    assert ("set", 1, True) in gen.calls

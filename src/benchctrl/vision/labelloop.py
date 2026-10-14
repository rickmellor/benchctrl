"""Capture-and-label: build a labelled frame set from states benchctrl commanded.

Why this exists
---------------
The NPU ships with YOLOv8n, which knows 80 everyday classes and nothing about
this bench. What the bench needs answered is "is this LED lit, what colour, is
it blinking", and training that needs a few hundred labelled frames. Labelling
by hand is slow and wrong often enough to matter; but benchctrl already *knows*
the truth whenever it commands a PDU outlet, a CP2112 line, an SDG1032X
generator output, or the bench box's own status LED. So: command a state,
settle, capture N frames tagged with a
``seq`` this loop chose, and label each frame with the state that was commanded
when it was taken.

The rules, each earned by the metis repo's board-reader experiment
(``notes/wargames-vision-report.md`` § 7):

* **Label from the command, verify from the frame.** A frame that comes back
  carrying a ``seq`` other than the one fired is discarded, never labelled
  (the driver raises; the loop records the discard). Optionally a classical
  read of a region (``sanity``) is stored beside every frame, so a frame whose
  pixels disagree with the commanded label is *flagged* rather than trusted.
* **Interleave states across rounds.** A round visits every state before any
  state is revisited, so lighting drift, auto white balance and warm-up cannot
  masquerade as a class.
* **Restore what you touched.** Actuators are snapshotted before the first
  command and restored in a ``finally`` — the DUT is left as found even when
  a capture fails half way, exactly like ``trigger_reset_pulse``.
* **Real frames.** Synthetic-only training collapsed to 48 %; 200 real frames
  gave 100 %.

Stdlib only, like :py:mod:`benchctrl.vision.service`: this runs on the bench
box (local mode) or the host (remote mode) with nothing installed beyond
benchctrl. The optional sanity read imports Pillow lazily and only when asked.
"""

from __future__ import annotations

import csv
import hashlib
import json
import logging
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from benchctrl._version import __version__

log = logging.getLogger("benchctrl.vision.labelloop")

#: Actuator kinds a state may name. Everything else is refused at spec time.
ACTUATOR_KINDS = ("cyberpower_pdu41002", "silabs_cp2112", "sysfs_led", "siglent_sdg1032x")

#: Where a host's LEDs live. Raspberry Pi 5: ``ACT`` (green status) and ``PWR``.
SYSFS_LEDS = "/sys/class/leds"


class LabelSpecError(ValueError):
    """The spec asks for something the loop will not do."""


# ---------------------------------------------------------------- the spec


@dataclass(frozen=True)
class LabelState:
    """One commanded state and the label frames taken in it receive."""

    label: str
    actuator: dict[str, Any]

    def validate(self) -> None:
        if not self.label or "/" in self.label or self.label.startswith("."):
            raise LabelSpecError(f"bad label {self.label!r}: one path segment, no slash")
        kind = self.actuator.get("device")
        if kind not in ACTUATOR_KINDS:
            raise LabelSpecError(
                f"state {self.label!r}: actuator device must be one of {ACTUATOR_KINDS}, "
                f"got {kind!r}"
            )
        if kind == "cyberpower_pdu41002":
            if not isinstance(self.actuator.get("outlet"), int) or not isinstance(
                self.actuator.get("on"), bool
            ):
                raise LabelSpecError(
                    f"state {self.label!r}: PDU actuator needs int outlet + bool on"
                )
        elif kind == "silabs_cp2112":
            if not isinstance(self.actuator.get("line"), int) or not isinstance(
                self.actuator.get("asserted"), bool
            ):
                raise LabelSpecError(
                    f"state {self.label!r}: CP2112 actuator needs int line + bool asserted"
                )
        elif kind == "siglent_sdg1032x":
            ch = self.actuator.get("channel")
            if (
                isinstance(ch, bool)
                or ch not in (1, 2)
                or not isinstance(self.actuator.get("output"), bool)
            ):
                raise LabelSpecError(
                    f"state {self.label!r}: SDG1032X actuator needs channel 1 or 2 + bool output"
                )
        elif kind == "sysfs_led":
            led = self.actuator.get("led")
            if not isinstance(led, str) or "/" in led or not led:
                raise LabelSpecError(f"state {self.label!r}: sysfs_led needs a led name")
            if self.actuator.get("brightness") not in (0, 1):
                raise LabelSpecError(f"state {self.label!r}: sysfs_led brightness must be 0 or 1")


@dataclass(frozen=True)
class LabelSpec:
    """What to capture. ``seq_start`` makes every frame's tag unique per run."""

    name: str
    states: tuple[LabelState, ...]
    frames_per_state: int = 20
    settle_s: float = 1.0
    rounds: int = 2
    interleave: bool = True
    seq_start: int = 1000
    wait_s: float = 3.0
    #: Camera setup applied before the first capture; None leaves it alone.
    crop: Optional[tuple[int, int, int, int]] = None
    exposure_us: Optional[float] = None
    gain_db: Optional[float] = None
    #: Optional classical read stored beside every frame: a region in *frame*
    #: pixels (after crop) whose mean brightness is recorded.
    sanity_roi: Optional[tuple[int, int, int, int]] = None

    def validate(self) -> None:
        if not self.name or "/" in self.name:
            raise LabelSpecError("spec needs a name with no slash")
        if not self.states:
            raise LabelSpecError("spec has no states")
        labels = [s.label for s in self.states]
        if len(set(labels)) != len(labels):
            raise LabelSpecError(f"duplicate labels: {sorted(labels)}")
        for s in self.states:
            s.validate()
        if self.frames_per_state < 1 or self.rounds < 1:
            raise LabelSpecError("frames_per_state and rounds must be >= 1")
        if self.settle_s < 0 or not 0 < self.wait_s <= 10:
            raise LabelSpecError("settle_s must be >= 0 and wait_s in (0, 10]")

    @classmethod
    def from_dict(cls, raw: dict) -> LabelSpec:
        states = tuple(
            LabelState(label=str(s["label"]), actuator=dict(s["actuator"]))
            for s in raw.get("states", [])
        )
        kw = {
            k: raw[k]
            for k in (
                "frames_per_state",
                "settle_s",
                "rounds",
                "interleave",
                "seq_start",
                "wait_s",
                "exposure_us",
                "gain_db",
            )
            if k in raw
        }
        for key in ("crop", "sanity_roi"):
            if raw.get(key) is not None:
                kw[key] = tuple(int(v) for v in raw[key])
        spec = cls(name=str(raw.get("name", "")), states=states, **kw)
        spec.validate()
        return spec

    def to_dict(self) -> dict:
        d = asdict(self)
        d["states"] = [asdict(s) for s in self.states]
        return d

    def digest(self) -> str:
        return hashlib.sha256(json.dumps(self.to_dict(), sort_keys=True).encode()).hexdigest()[:16]

    def schedule(self) -> list[tuple[int, LabelState]]:
        """``(round, state)`` in capture order. Interleaved: every state once per round."""
        if self.interleave:
            return [(r, s) for r in range(self.rounds) for s in self.states]
        return [(r, s) for s in self.states for r in range(self.rounds)]


# ---------------------------------------------------------------- actuators


class PduActuator:
    """One PDU outlet. Snapshot is the outlet's state as found."""

    def __init__(self, pdu: Any) -> None:
        self.pdu = pdu

    def snapshot(self, actuator: dict) -> dict:
        return {"outlet": actuator["outlet"], "on": bool(self.pdu.outlet_state(actuator["outlet"]))}

    def apply(self, actuator: dict) -> dict:
        got = self.pdu.set_outlet_state(actuator["outlet"], actuator["on"])
        return {"outlet": actuator["outlet"], "on": bool(got)}

    def restore(self, found: dict) -> None:
        self.pdu.set_outlet_state(found["outlet"], found["on"])


class Cp2112Actuator:
    """One CP2112 line, driven open-drain. Snapshot is the line's mode + level."""

    def __init__(self, gpio: Any) -> None:
        self.gpio = gpio

    def snapshot(self, actuator: dict) -> dict:
        st = self.gpio.read_line_state(actuator["line"])
        return {"line": actuator["line"], "is_output": bool(st.is_output), "asserted": st.asserted}

    def apply(self, actuator: dict) -> dict:
        line = actuator["line"]
        self.gpio.set_line_mode(line, output=True)
        st = self.gpio.set_line_asserted(line, actuator["asserted"])
        return {"line": line, "asserted": bool(getattr(st, "asserted", actuator["asserted"]))}

    def restore(self, found: dict) -> None:
        line = found["line"]
        if found["is_output"]:
            self.gpio.set_line_asserted(line, bool(found["asserted"]))
        else:
            self.gpio.set_line_mode(line, output=False)


class SdgOutputActuator:
    """One SDG1032X output channel (on / off). Snapshot is the output as found.

    ``set_output`` verifies: it returns what the instrument read back and
    raises ``SDG1032XVerifyError`` when that differs from what was asked. The
    *read-back* is what ``apply`` returns, so the label's provenance is the
    generator's own report of its output, not the command the loop sent.
    """

    def __init__(self, gen: Any) -> None:
        self.gen = gen

    def snapshot(self, actuator: dict) -> dict:
        ch = actuator["channel"]
        return {"channel": ch, "output": bool(self.gen.get_output(ch).enabled)}

    def apply(self, actuator: dict) -> dict:
        ch = actuator["channel"]
        st = self.gen.set_output(ch, actuator["output"])
        return {"channel": ch, "output": bool(st.enabled)}

    def restore(self, found: dict) -> None:
        self.gen.set_output(found["channel"], found["output"])


class SysfsLedActuator:
    """A host LED under ``/sys/class/leds`` — the bench box's own status light.

    The trigger is parked at ``none`` for the run (a Pi's ``ACT`` blinks on disk
    activity otherwise) and put back on restore, with the brightness as found.
    Note the Pi 5's ``ACT`` is active-low in hardware: brightness 0 is *lit*.
    The spec spells the raw value per state and labels it, so the truth is the
    label, not the number.
    """

    def __init__(self, root: str = SYSFS_LEDS) -> None:
        self.root = Path(root)

    def _dir(self, led: str) -> Path:
        d = self.root / led
        if not (d / "brightness").exists():
            raise LabelSpecError(f"no LED {led!r} under {self.root}")
        return d

    def snapshot(self, actuator: dict) -> dict:
        d = self._dir(actuator["led"])
        trig = (d / "trigger").read_text()
        cur = trig[trig.find("[") + 1 : trig.find("]")] if "[" in trig else "none"
        return {
            "led": actuator["led"],
            "trigger": cur,
            "brightness": int((d / "brightness").read_text().strip() or 0),
        }

    def apply(self, actuator: dict) -> dict:
        d = self._dir(actuator["led"])
        (d / "trigger").write_text("none\n")
        (d / "brightness").write_text(f"{int(actuator['brightness'])}\n")
        return {"led": actuator["led"], "brightness": int(actuator["brightness"])}

    def restore(self, found: dict) -> None:
        d = self._dir(found["led"])
        (d / "brightness").write_text(f"{int(found['brightness'])}\n")
        (d / "trigger").write_text(f"{found['trigger']}\n")


def build_actuators(devices: dict[str, Any], *, sysfs_root: str = SYSFS_LEDS) -> dict[str, Any]:
    """Adapters keyed by actuator kind, from open driver objects.

    ``devices`` maps ``"cyberpower_pdu41002"`` / ``"silabs_cp2112"`` /
    ``"siglent_sdg1032x"`` to open drivers (local, remote or simulated — the
    loop cannot tell). The host LED needs no driver and is always available;
    whether the LED exists is checked when a state names it.
    """
    out: dict[str, Any] = {"sysfs_led": SysfsLedActuator(sysfs_root)}
    if "cyberpower_pdu41002" in devices:
        out["cyberpower_pdu41002"] = PduActuator(devices["cyberpower_pdu41002"])
    if "silabs_cp2112" in devices:
        out["silabs_cp2112"] = Cp2112Actuator(devices["silabs_cp2112"])
    if "siglent_sdg1032x" in devices:
        out["siglent_sdg1032x"] = SdgOutputActuator(devices["siglent_sdg1032x"])
    return out


# ---------------------------------------------------------------- the loop


@dataclass
class FrameRecord:
    seq: int
    frame_id: int
    ts: float
    label: str
    round: int
    file: str
    sha256: str
    bytes: int
    actuator_state: dict
    sanity: Optional[float] = None


@dataclass
class LabelManifest:
    name: str
    spec: dict
    spec_digest: str
    benchctrl_version: str
    started: float
    finished: float = 0.0
    camera: dict = field(default_factory=dict)
    frames: list[FrameRecord] = field(default_factory=list)
    discards: list[dict] = field(default_factory=list)
    as_found: dict = field(default_factory=dict)
    restored: bool = False
    error: Optional[str] = None

    @property
    def counts(self) -> dict[str, int]:
        c: dict[str, int] = {}
        for f in self.frames:
            c[f.label] = c.get(f.label, 0) + 1
        return c

    def to_dict(self) -> dict:
        d = asdict(self)
        d["counts"] = self.counts
        return d

    def summary(self) -> dict:
        return {
            "name": self.name,
            "frames": len(self.frames),
            "counts": self.counts,
            "discards": len(self.discards),
            "restored": self.restored,
            "error": self.error,
            "spec_digest": self.spec_digest,
        }


def run_label_capture(
    vision: Any,
    actuators: dict[str, Any],
    spec: LabelSpec,
    out_dir: str | Path,
    *,
    on_event: Optional[Callable[[str, dict], None]] = None,
    sanity: Optional[Callable[[bytes, tuple[int, int, int, int]], float]] = None,
    sleep: Callable[[float], None] = time.sleep,
) -> LabelManifest:
    """Command each state, capture, label, restore. Returns the manifest.

    ``vision`` is an open ``BenchVision`` (or its remote proxy / simulated
    twin). ``actuators`` comes from :py:func:`build_actuators`. ``sanity``, if
    given, is called with the JPEG bytes and ``spec.sanity_roi`` and its return
    value is stored on the frame record; :py:func:`mean_brightness` is the
    Pillow-backed default the CLI wires when asked.
    """
    spec.validate()
    out = Path(out_dir)
    frames_dir = out / "frames"
    frames_dir.mkdir(parents=True, exist_ok=True)
    manifest = LabelManifest(
        name=spec.name,
        spec=spec.to_dict(),
        spec_digest=spec.digest(),
        benchctrl_version=__version__,
        started=time.time(),
    )

    def emit(kind: str, **data: Any) -> None:
        log.info("labelloop: %s %s", kind, data)
        if on_event:
            on_event(kind, data)

    # Every kind the spec names must have an adapter, before anything moves.
    for s in spec.states:
        kind = s.actuator["device"]
        if kind not in actuators:
            raise LabelSpecError(f"state {s.label!r} needs actuator {kind!r}, which is not open")

    # Camera setup, read back rather than assumed.
    if spec.exposure_us is not None:
        vision.set_exposure_us(spec.exposure_us)
    if spec.gain_db is not None:
        vision.set_gain_db(spec.gain_db)
    if spec.crop is not None:
        vision.set_crop(*spec.crop)
    st = vision.read_status()
    info = vision.read_identity()
    manifest.camera = {
        "model": info.camera_model,
        "serial": info.camera_serial,
        "exposure_us": st.exposure_us,
        "gain_db": st.gain_db,
        "fps": st.fps,
        "triggered": st.triggered,
        "crop": st.crop.to_dict() if st.crop else None,
        "aipu_present": st.aipu_present,
        "model_name": st.model_name,
    }

    # As-found, one snapshot per distinct actuator target, taken before the
    # first command so restore() puts back the bench, not the previous state.
    found: dict[str, dict] = {}
    for s in spec.states:
        key = _target_key(s.actuator)
        if key not in found:
            found[key] = actuators[s.actuator["device"]].snapshot(s.actuator)
    manifest.as_found = found
    emit("start", name=spec.name, states=[s.label for s in spec.states], as_found=found)

    seq = spec.seq_start
    try:
        for rnd, state in spec.schedule():
            adapter = actuators[state.actuator["device"]]
            applied = adapter.apply(state.actuator)
            emit("state", label=state.label, round=rnd, applied=applied)
            if spec.settle_s:
                sleep(spec.settle_s)
            label_dir = frames_dir / state.label
            label_dir.mkdir(exist_ok=True)
            for _ in range(spec.frames_per_state):
                seq += 1
                try:
                    frame = vision.trigger_capture(seq=seq, wait_s=spec.wait_s)
                except Exception as exc:  # noqa: BLE001 - recorded, never labelled
                    manifest.discards.append(
                        {
                            "seq": seq,
                            "label": state.label,
                            "round": rnd,
                            "error": f"{type(exc).__name__}: {exc}",
                        }
                    )
                    emit("discard", seq=seq, label=state.label, error=repr(exc))
                    continue
                if frame.seq != seq:  # the driver promises this; hold it to it
                    manifest.discards.append(
                        {
                            "seq": seq,
                            "label": state.label,
                            "round": rnd,
                            "error": f"frame carried seq {frame.seq}",
                        }
                    )
                    continue
                path = label_dir / f"{seq:06d}.jpg"
                path.write_bytes(frame.jpeg)
                rec = FrameRecord(
                    seq=seq,
                    frame_id=frame.frame_id,
                    ts=frame.ts,
                    label=state.label,
                    round=rnd,
                    file=str(path.relative_to(out)),
                    sha256=hashlib.sha256(frame.jpeg).hexdigest(),
                    bytes=len(frame.jpeg),
                    actuator_state=applied,
                )
                if sanity is not None and spec.sanity_roi is not None:
                    try:
                        rec.sanity = float(sanity(frame.jpeg, spec.sanity_roi))
                    except Exception as exc:  # noqa: BLE001 - the read is advisory
                        log.debug("labelloop: sanity read failed: %r", exc)
                manifest.frames.append(rec)
            emit("captured", label=state.label, round=rnd, total=len(manifest.frames))
    except BaseException as exc:
        manifest.error = f"{type(exc).__name__}: {exc}"
        raise
    finally:
        # Reverse order, each in its own try: one failed restore must not
        # stop the next, and the DUT is what is at stake.
        ok = True
        for key, snap in reversed(list(found.items())):
            kind = key.split(":", 1)[0]
            try:
                actuators[kind].restore(snap)
            except Exception as exc:  # noqa: BLE001
                ok = False
                emit("restore_failed", target=key, error=repr(exc))
        manifest.restored = ok
        manifest.finished = time.time()
        _write_manifest(out, manifest)
        emit("done", **manifest.summary())
    return manifest


def _target_key(actuator: dict) -> str:
    kind = actuator["device"]
    if kind == "cyberpower_pdu41002":
        return f"{kind}:{actuator['outlet']}"
    if kind == "silabs_cp2112":
        return f"{kind}:{actuator['line']}"
    if kind == "siglent_sdg1032x":
        return f"{kind}:{actuator['channel']}"
    return f"{kind}:{actuator['led']}"


def _write_manifest(out: Path, manifest: LabelManifest) -> None:
    (out / "manifest.json").write_text(json.dumps(manifest.to_dict(), indent=2) + "\n")
    with (out / "labels.csv").open("w", newline="") as fh:
        w = csv.writer(fh)
        w.writerow(["file", "label", "seq", "round", "sha256", "sanity"])
        for f in manifest.frames:
            w.writerow(
                [
                    f.file,
                    f.label,
                    f.seq,
                    f.round,
                    f.sha256,
                    "" if f.sanity is None else f"{f.sanity:.2f}",
                ]
            )


# ---------------------------------------------------------------- sanity read


def mean_brightness(jpeg: bytes, roi: tuple[int, int, int, int]) -> float:
    """Mean luminance (0-255) of ``roi`` in the frame. Needs Pillow; lazy."""
    import io

    from PIL import Image  # optional dependency, imported only here

    x, y, w, h = roi
    im = Image.open(io.BytesIO(jpeg)).convert("L").crop((x, y, x + w, y + h))
    data = im.tobytes()
    return sum(data) / max(len(data), 1)


# ---------------------------------------------------------------- CLI


def main(argv: Optional[list[str]] = None) -> int:
    """``python -m benchctrl.vision.labelloop spec.json out_dir [--sanity]``.

    Devices resolve through ``benchctrl.session`` (local / remote / sim per the
    active config), so the same spec runs on the bench box or from a host.
    """
    import argparse

    p = argparse.ArgumentParser(
        prog="benchctrl.vision.labelloop", description=__doc__.split("\n")[0]
    )
    p.add_argument("spec", help="JSON spec file")
    p.add_argument("out_dir", help="dataset directory (created)")
    p.add_argument(
        "--sanity",
        action="store_true",
        help="store a mean-brightness read of spec.sanity_roi beside each frame (needs Pillow)",
    )
    p.add_argument("--log-level", default="info")
    args = p.parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level.upper()),
        format="%(asctime)s %(levelname)-7s %(name)s: %(message)s",
    )

    spec = LabelSpec.from_dict(json.loads(Path(args.spec).read_text()))
    from benchctrl import session
    from benchctrl.drivers.bench_vision import BenchVision

    session.configure_from_environment()
    devices: dict[str, Any] = {}
    kinds = {s.actuator["device"] for s in spec.states}
    if "cyberpower_pdu41002" in kinds:
        from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

        devices["cyberpower_pdu41002"] = session.resolve(
            "cyberpower_pdu41002", opener=CyberPowerPDU41002.open
        )
    if "silabs_cp2112" in kinds:
        from benchctrl.drivers.silabs_cp2112 import CP2112

        devices["silabs_cp2112"] = session.resolve("silabs_cp2112", opener=CP2112.open)
    if "siglent_sdg1032x" in kinds:
        from benchctrl.drivers.siglent_sdg1032x import SiglentSDG1032X

        devices["siglent_sdg1032x"] = session.resolve(
            "siglent_sdg1032x", opener=SiglentSDG1032X.open
        )
    vision = session.resolve("bench_vision", opener=BenchVision.open)
    try:
        manifest = run_label_capture(
            vision,
            build_actuators(devices),
            spec,
            args.out_dir,
            sanity=mean_brightness if args.sanity else None,
        )
    finally:
        session.shutdown()
    print(json.dumps(manifest.summary(), indent=2))
    return 0 if manifest.error is None else 1


if __name__ == "__main__":
    sys.exit(main())

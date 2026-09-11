# Bench vision — a camera, and a Metis NPU where the host has one

`bench_vision` is the device key for reading the bench with a camera: is the
DUT's LED on, what colour, what blink pattern; is the display showing what the
run expects. The production hardware is a **Basler a2A1920-160uc** USB3 Vision
camera and an **Axelera Metis M.2** AI accelerator (INT8 NPU, 4 cores, ~10 W),
proven in the `metis` R&D repository at YOLOv8n ≈ 510 FPS on-device and ~46 FPS
end to end. This document is the benchctrl half: the driver, the tools, the
sidecar contract, and what carries across local, remote and sim mode.

The Metis needs PCIe. A **Raspberry Pi 5** (M.2 HAT) or a desktop has it; an
**Arduino Uno Q does not**, so on that board the device is camera-only at best
and every detection call raises `VisionCapabilityError`. See
[`KNOWN_LIMITATIONS.md`](../KNOWN_LIMITATIONS.md) § V-1.

## The split, and why it is what makes the device portable

```
host (any)                                  bench box: Pi 5, scrub, or the host itself
benchctrl-mcp ── vision_* tools ──► BenchVision (stdlib urllib) ──HTTP/JSON──► benchctrl-vision sidecar
   local : driver in-process                                                  (pypylon + axelera.runtime,
   remote: session.resolve() → agent → the same driver, on the bench box       in its own Ubuntu container,
   sim   : SimulatedVisionSidecar — the same HTTP router, synthetic camera     loopback :8095, no auth)
```

- **The driver** (`benchctrl.drivers.bench_vision.BenchVision`) is a stdlib HTTP
  client. It adds nothing to benchctrl's dependencies, so it runs anywhere the
  agent runs.
- **The sidecar** (`benchctrl-vision`, [`benchctrl.vision`](../src/benchctrl/vision/__init__.py))
  owns the camera SDK and the Axelera runtime. The runtime maps PCIe BARs and
  is only supported inside its Ubuntu 24.04 container, so the sidecar ships as a
  container (`deploy/vision/`) bound to **loopback**. It has no authentication:
  the agent is the network face, as for every other instrument.
- **One HTTP router.** `benchctrl.vision.service.VisionService` is stdlib-only
  and is what *both* the production sidecar and the simulator serve; only the
  camera and detector collaborators differ. The simulator therefore cannot
  drift from the API the driver was written against, and CI exercises the
  driver's real `urllib` path over a real socket.

So the same driver, the same tools and the same config work whether the camera
and Metis sit in the Pi, in a desktop, or in the host running benchctrl:

| mode | what opens the driver | which sidecar it talks to |
|---|---|---|
| `local` | `benchctrl-mcp` on this host | `http://127.0.0.1:8095` on this host |
| `remote` | `benchctrl-agent` on the bench box | the bench box's loopback sidecar |
| `sim` | `benchctrl.sim.factories.make_bench_vision` | an in-process `SimulatedVisionSidecar` |

The sidecar URL is the *opener's* concern: `open.bench_vision.url` in the
agent's `agent.json`, or `BENCHCTRL_VISION_URL` in the agent's environment,
defaulting to loopback. A host in remote mode never needs to know it.

## `seq` — the property everything rests on

`trigger_capture(seq=N)` fires a software trigger tagged `N` and returns
**only** the frame carrying that tag. A frame from an earlier trigger, or a
free-run frame, is a `VisionCaptureError`, never a plausible-looking success.
That is what lets a labelled dataset say "this frame was taken while outlet 3
was commanded on" and mean it, and what lets a run assert an LED state against
the state it just commanded.

The sidecar enforces it (`/capture` refuses a wrong-`seq` frame), and the driver
holds the sidecar to its promise (it checks again). The hardware trigger cable,
when it lands, hits the same `TriggerSource` seam in the sidecar and needs
nothing in the driver.

`trigger_capture` and every `set_*`/`clear_crop` are **mutators** — they need
the writer claim in remote mode, because two clients interleaving triggers would
make the correlation undecidable. `read_frame`, `detect`, `read_status` and every
property need no claim: a dashboard can watch the bench without taking the
camera.

## Quick start

```python
from benchctrl.drivers.bench_vision import BenchVision

with BenchVision.open() as cam:                 # $BENCHCTRL_VISION_URL or loopback :8095
    print(cam.read_identity())                  # camera model/serial, sidecar version, aipu_present
    cam.set_exposure_us(6000)                   # tune exposure FIRST — SNR beats frame rate
    cam.set_crop(640, 300, 400, 300)            # the region the LEDs live in

    frame = cam.trigger_capture(seq=42, infer=True)
    assert frame.seq == 42                      # or it would have raised
    open("led.jpg", "wb").write(frame.jpeg)
    for box in frame.detections.items:
        print(box.label, box.score, (box.x1, box.y1, box.x2, box.y2))
```

Through the MCP server the same thing is `vision_open()`, `vision_set_exposure_us(6000)`,
`vision_set_crop(640, 300, 400, 300)`, `vision_trigger_capture(seq=42, infer=True, save_to="led.jpg")`.
**No tool returns image bytes** — a JPEG in a tool result would land in the
transcript — so the tools take `save_to` and return the path, size and SHA-256
with the metadata.

Simulate the whole thing with no camera:

```bash
BENCHCTRL_SIM_DEVICES=bench_vision benchctrl-mcp        # sim mode for this key only
benchctrl-agent --simulate --devices bench_vision       # or an agent serving a fake one
```

## Driver surface

| Method | Claim | Returns |
|---|---|---|
| `BenchVision.open(url=None, *, timeout_s=5.0)` | — | `BenchVision`; checks `/health` |
| `read_identity()` | read | `VisionInfo` |
| `read_status()` | read | `VisionStatus` — the `/status` document, typed |
| `read_frame(*, wait_for=None, wait_s=5.0)` | read | `Frame`: latest, or the next after frame id `wait_for` |
| `detect(*, min_conf=0.4)` | read | `Detections` on the latest frame; no trigger |
| `trigger_capture(*, seq=None, infer=False, min_conf=0.4, wait_s=2.0)` | **write** | `Frame` carrying `seq` |
| `set_exposure_us(us)` / `set_gain_db(db)` / `set_fps(fps)` | **write** | the value the camera **read back** (clamped) |
| `set_crop(x, y, w, h)` / `clear_crop()` | **write** | `Crop` / `None` |
| `close()` | — | forgets the sidecar; changes nothing on the camera |

Properties — `camera_model`, `camera_serial`, `frame_id`, `trigger_seq`, `crop`,
`exposure_us`, `gain_db`, `fps`, `triggered`, `aipu_present`, `aipu_temp_c`,
`model_name`, `stream_url`, `url`, `is_open` — all read from **one** cached
`/status` (`STATUS_TTL_S = 0.2 s`). The agent piggybacks every property onto
every response, so this is what keeps a remote call at one request rather than
fifteen. A property never raises; it logs and reads `None`.

`wait_s` is bounded to `(0, 10]`: the agent's worker times a device call out at
20 s, and a wait must fit under that with the HTTP round trip on top.

### Dataclasses (all cross the agent wire with their types intact)

- `Frame(frame_id, seq, ts, width, height, jpeg: bytes, crop, detections)` —
  `jpeg` rides inline below 64 KB and as a **blob reference** above (a
  1920×1200 q80 frame is ~150–300 KB), fetched in chunks and SHA-256 verified.
  `to_dict()` never includes the bytes.
- `Detections(frame_id, seq, model_name, infer_ms, items: tuple[Detection, ...])`,
  `Detection(class_id, label, score, x1, y1, x2, y2)` — coordinates are in
  frame pixels, i.e. after any crop.
- `Crop(x, y, w, h)`, `VisionInfo`, `VisionStatus`.

### Exceptions

| Exception | Means | Do |
|---|---|---|
| `VisionConnectionError` | no sidecar answering | check `benchctrl-vision.service` on the box that opens the driver |
| `VisionCapabilityError` | no AIPU / no model here | fall back (classical CV, a human); never retry |
| `VisionCaptureError` | not trigger-ready, grab failed, or **wrong `seq`** | discard; never label |
| `VisionTimeoutError` | the wait, or the request, ran out | retry |
| `VisionValueError` | bad argument, or the camera refused it (crop past the sensor) | fix the call |
| `VisionProtocolError` | the sidecar answered in a shape the driver does not know | version mismatch |

All seven (with `VisionError`) survive the agent wire as themselves.

## The sidecar HTTP contract

JSON everywhere except the two browser endpoints. Errors are
`{"error": {"type": ..., "message": ...}}`; the driver keys on `type`.

| Route | Purpose |
|---|---|
| `GET /health` | `{ok, camera, aipu, version, api}` |
| `GET /status` | `camera{model, serial, width, height, exposure_us, gain_db, fps, triggered, crop, frame_id, trigger_seq, dropped}`, `aipu{present, model_name, firmware, temp_c, cores, infer_ms_last}`, `sidecar{version, api, uptime_s}` |
| `GET /config`, `PUT /config` | `exposure_us`, `gain_db`, `fps`, `crop` — PUT returns values **read back** from the camera |
| `POST /capture {seq, infer, min_conf, wait_s}` | trigger + wait + optional infer, one call; wrong-`seq` frame → `capture` error |
| `GET /frame.json?wait=<fid>&wait_s=` | latest frame (long-poll with `wait`); observer-safe |
| `POST /detect {min_conf}` | infer on the latest frame; `capability` error without an AIPU |
| `GET /trigger?seq=N` | fire a trigger (kept for the browser workflow) |
| `GET /frame.jpg`, `GET /stream`, `GET /crop?x=&y=&w=&h=` / `?off` | for a human focusing the camera: raw JPEG, MJPEG, live ROI |

Error types → status: `value` 400, `not_found` 404, `capability` 409,
`capture` 503, `timeout` 504, `internal` 500.

`benchctrl-vision` flags: `--bind 127.0.0.1 --port 8095 --exposure-us --gain-db
--fps --triggered|--free-run --model /models/yolov8n-coco.axm --aipu-cores 4
--no-aipu --jpeg-quality 80`.

## Watching the camera

Two read-only views, neither able to trigger or configure:

- **The dashboard.** The FUI's VISION · LIVE quadrant shows the stream, relayed
  by the FUI server from the sidecar's loopback (`/vision/stream`), so it works
  on the kiosk panel and through an ssh tunnel to the FUI alike.
- **A browser on the LAN.** With `VIEW_PORT=8096` in `vision.env` the sidecar
  also serves `/stream`, `/frame.jpg` and `/health` on
  `http://<bench-box>:8096/`, and refuses everything else there with 403.

To focus: put the sidecar in free-run (`TRIGGER_MODE=free-run`, restart) so the
picture follows the lens, turn the ring until label text reads, then put
`TRIGGER_MODE` back to `triggered`.

## Tuning notes (from the metis R&D write-up)

- **Exposure first.** Signal-to-noise beats frame rate for LED and indicator
  reading; set exposure and gain with `/stream` open in a browser, then crop.
- **Real frames beat synthetic.** A classifier trained on synthetic LED frames
  collapsed to 48 %; 200 real labelled frames took it to 100 %. Capture them
  with the label loop (`docs/vision.md` § Label loop, once it lands) against
  states benchctrl *commanded* — a PDU outlet or a CP2112 line — so the labels
  come from ground truth you already have.
- **Keep a sanity channel.** Classical CV fails safe and cheap; CNNs fail
  confident. Parity-check a model's answer against the commanded state, and
  require `N` consistent frames before acting on a transition.

## Simulator

`benchctrl.sim.vision.SimulatedVisionSidecar` runs the real router on a
loopback socket with a `SyntheticCamera` and a `CannedDetector`. It is faithful
about the things a driver could get wrong: no frame without a trigger in
triggered mode, `seq` landing on the *next* frame only, `dropped` counting an
early re-trigger, crops changing the reported dimensions, exposure/gain reading
back clamped. `frame_bytes=200_000` pushes frames over the blob threshold;
`aipu=False` models a camera-only host; `fail_next_trigger=True` makes one
trigger fail. The JPEG is real (a 16×16 baseline JPEG padded with `COM`
segments to any size — any decoder accepts it), and the `-SIM` suffix on the
model name says what it is.

## Deploying the sidecar

See [`deploy/vision/README.md`](../deploy/vision/README.md): the container
build, the `metis-dkms` driver install, `benchctrl-vision.service`, and the
Raspberry Pi 5 bring-up runbook. Platform table in
[`remote.md`](remote.md) § *Deploying the agent*.

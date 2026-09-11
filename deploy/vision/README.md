# Deploying the vision sidecar

Everything here runs on the **bench machine that has the camera and the Metis**
— a Raspberry Pi 5 with an M.2 HAT, or a desktop with an M.2 slot. The
benchctrl driver (`bench_vision`) needs none of it; it is a stdlib HTTP client
that talks to what this directory installs. The architecture and the HTTP
contract are in [`docs/vision.md`](../../docs/vision.md).

| | |
|---|---|
| [`install-metis-driver.sh`](install-metis-driver.sh) | the Axelera `metis-dkms` kernel driver, `axelera` group, udev, load at boot |
| [`Dockerfile`](Dockerfile) + [`build.sh`](build.sh) | the sidecar image: Axelera runtime + pylon + OpenCV on Ubuntu 24.04 (arm64 or amd64) |
| [`fetch-model.sh`](fetch-model.sh) | put a compiled `.axm` where the sidecar mounts it, checksum recorded |
| [`install-vision.sh`](install-vision.sh) | `benchctrl-vision.service` + `/etc/benchctrl/vision.env` |
| [`run-vision.sh`](run-vision.sh) | what the unit runs: the privileged container, port on loopback |
| [`udev/72-axelera.rules`](udev/72-axelera.rules) | reference copy of the rule the driver package installs |

`vision.env` knobs: `IMAGE`, `SRC_DIR`, `MODEL_DIR`, `MODEL`, `PORT` (control,
loopback only), `VIEW_PORT` (read-only stream + still on the LAN; 0 = off),
`EXPOSURE_US`, `GAIN_DB`, `FPS`, `TRIGGER_MODE`, `JPEG_QUALITY`, `AIPU_CORES`,
`NO_AIPU`.

All scripts are POSIX `sh`. The two `install-*` and the driver installer are
root-only and idempotent; `build.sh` and `fetch-model.sh` run as the user.

## Why a container, and why privileged

The Axelera runtime is packaged for Ubuntu 24.04 only and **maps the card's
PCIe BARs from user space**, which needs `CAP_SYS_RAWIO`. The bench boxes run
Raspberry Pi OS and Arch. So the runtime lives in a container, and that
container runs `--privileged` with `/dev` and `/sys` mounted — the invocation
verified on scrub (2026-08-27) and reused unchanged. A narrower grant
(`--cap-add SYS_RAWIO,SYS_ADMIN --device /dev/metis-… --device
/dev/dma_heap/system …`) is written out in `run-vision.sh` for the day it is
worth revisiting.

What the container does **not** hold is benchctrl: the checkout's `src/` is
bind-mounted read-only, so upgrading the sidecar's Python is `git pull` and
`systemctl restart benchctrl-vision`, never a rebuild. Rebuild only when the
Axelera runtime version (`AX_VERSION`) or the Python wheels change.

The sidecar has **no authentication**. `run-vision.sh` publishes its port on
`127.0.0.1` only; the benchctrl agent is the network face. Do not widen that.

## Bring-up on a Raspberry Pi 5 (verified 2026-09-11 on `benchpi`)

Prerequisites: the agent already installed (`deploy/install-agent.sh`),
`docker.io` installed with the user in the `docker` group, the Metis on an M.2
HAT and visible to `lspci -d 1f9d:1100`, the Basler on USB 3.

```bash
cd ~/benchctrl

# 1. kernel driver — DKMS builds against the running kernel (~1 min on a Pi 5)
sudo DEB=~/metis-dkms_1.6.2_all.deb ./deploy/vision/install-metis-driver.sh
ls -l /dev/metis*                     # metis-1-3-0 and its colon symlink
#   the bus address moves with the HAT slot; the script finds the card by 1f9d:1100

# 2. the image (~3.5 GB; 15–25 min on a Pi 5, once)
./deploy/vision/build.sh
docker run --rm --privileged -v /dev:/dev -v /sys:/sys --entrypoint axdevice benchctrl-vision:1.8.0
#   want: "Device 0: metis-… 1GiB metis-m2 flver=1.8.0 bcver=7.4 …"
#   flver < 1.4.0 has no thermal management and hard-hangs under load; flash first

# 3. the model (compiled on scrub with the devkit; never on the Pi)
./deploy/vision/fetch-model.sh        # scp from scrub, sha256 into models/MANIFEST

# 4. the service
sudo ./deploy/vision/install-vision.sh
curl -s localhost:8095/health         # {"ok": true, "camera": true, "aipu": true, …}
curl -s localhost:8095/status | python3 -m json.tool

# 5. tell the agent
#   /etc/benchctrl/agent.json: add "bench_vision" to "devices"
sudo systemctl restart benchctrl-agent
```

Then from the host, with `bench_vision` set to `remote` in the client config:
`vision_open()` → `vision_trigger_capture(seq=42, infer=True, save_to="f.jpg")`
must return `seq == 42` and a detection list.

### Focusing and exposure

The control port is loopback only, but the read-only **view port**
(`VIEW_PORT=8096` in `vision.env`) serves `/stream` and `/frame.jpg` on the LAN:
open `http://benchpi.home.arpa:8096/stream`, or watch the dashboard's VISION
quadrant. Put the sidecar in free-run (`TRIGGER_MODE=free-run`, restart) while
focusing so the picture follows the lens; back to `triggered` afterwards. Set
exposure and gain with `vision_set_exposure_us` / `vision_set_gain_db` (or
`PUT /config`), then crop. Exposure first: signal-to-noise beats frame rate.

### Link speed and the HAT

A Pi 5 has one PCIe lane. The Metis advertises Gen3 x4 and gets whatever the
HAT allows: the official M.2 HAT+ (2230/2242 only — the Metis is 2280 and does
not fit) or a switchless 2280 board gives Gen3 x1 (~7.9 Gb/s); a dual-slot HAT
with an ASM1182e switch gives Gen2 x1 (4 Gb/s). Gen2 x1 is ample for
YOLOv8n-class work (~0.6 Gb/s at 60 fps). `install-metis-driver.sh` prints the
negotiated link.

### Thermal

The card draws ~10 W and needs a heatsink and airflow. Firmware 1.8.0 restored
the on-chip thermal management (HW throttle at 105 °C); on 1.3.0 the card
hard-hung under load at ~100 s. `vision_status()` reports `aipu_temp_c` (via
`axcmd --board-temp` inside the container; `None` if that fails) and
`aipu_firmware`. Soak before trusting an unattended run.

## Upgrading

| What changed | Do |
|---|---|
| benchctrl (any Python) | `git pull && sudo systemctl restart benchctrl-vision` |
| a model | `fetch-model.sh` then restart |
| `AX_VERSION` / wheels | `build.sh`, then edit `IMAGE=` in `/etc/benchctrl/vision.env`, restart |
| the kernel | DKMS rebuilds `metis` on boot (`AUTOINSTALL=yes`); check `dkms status` |

## Verifying

```bash
systemctl status benchctrl-vision --no-pager
journalctl -u benchctrl-vision -n 20 --no-pager   # want: "benchctrl-vision x.y.z serving a2A1920-160uc … aipu=yes"
curl -s localhost:8095/health
docker ps --filter name=benchctrl-vision
```

A `/health` with `"aipu": false` on a box that has the card means the driver is
not loaded (`ls /dev/metis*`), the model is missing (`ls ~/benchctrl/models`),
or `NO_AIPU=1` is set in `vision.env`. The camera keeps working either way.

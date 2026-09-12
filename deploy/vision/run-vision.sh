#!/bin/sh
# Run the benchctrl-vision sidecar container. Installed as
# /usr/local/bin/benchctrl-vision-run by install-vision.sh; the systemd unit
# calls it with --foreground.
#
#     benchctrl-vision-run                # detached (bring-up by hand)
#     benchctrl-vision-run --foreground   # what the unit does
#
# Configuration comes from /etc/benchctrl/vision.env (see vision.env.example).
#
# --privileged, and why: the Axelera runtime mmaps the card's PCIe BARs, which
# needs CAP_SYS_RAWIO (dropped by Docker's default set); /sys is needed for
# device enumeration (/sys/class/metis) and /dev carries the node, its colon
# symlink, dma_heap, and the camera's /dev/bus/usb. The narrower grant below
# was tried once during bring-up — see deploy/vision/README.md for the result.
set -eu

ENV_FILE=${ENV_FILE:-/etc/benchctrl/vision.env}
if [ -f "$ENV_FILE" ]; then
    # shellcheck disable=SC1090
    . "$ENV_FILE"
fi
IMAGE=${IMAGE:-benchctrl-vision:1.8.0}
SRC_DIR=${SRC_DIR:-/home/rick/benchctrl/src}
MODEL_DIR=${MODEL_DIR:-/home/rick/benchctrl/models}
MODEL=${MODEL:-yolov8n-coco.axm}
CLASSIFIERS=${CLASSIFIERS:-}
PORT=${PORT:-8095}
EXPOSURE_US=${EXPOSURE_US:-8000}
GAIN_DB=${GAIN_DB:-0}
FPS=${FPS:-60}
TRIGGER_MODE=${TRIGGER_MODE:-triggered}
JPEG_QUALITY=${JPEG_QUALITY:-80}
AIPU_CORES=${AIPU_CORES:-4}
NO_AIPU=${NO_AIPU:-0}
VIEW_PORT=${VIEW_PORT:-0}
NAME=benchctrl-vision

detach=-d
if [ "${1:-}" = "--foreground" ]; then
    detach=""
fi

if [ ! -d "$SRC_DIR/benchctrl/vision" ]; then
    echo "no benchctrl/vision under $SRC_DIR — set SRC_DIR in $ENV_FILE" >&2
    exit 1
fi

case "$TRIGGER_MODE" in
    triggered) mode_flag=--triggered ;;
    free-run)  mode_flag=--free-run ;;
    *) echo "TRIGGER_MODE must be triggered or free-run, got '$TRIGGER_MODE'" >&2; exit 1 ;;
esac

model_args=""
if [ "$NO_AIPU" = "1" ]; then
    model_args="--no-aipu"
elif [ -f "$MODEL_DIR/$MODEL" ]; then
    model_args="--model /models/$MODEL --aipu-cores $AIPU_CORES"
else
    echo "no model at $MODEL_DIR/$MODEL — serving the camera only (deploy/vision/fetch-model.sh)" >&2
    model_args="--no-aipu"
fi

# Indicator classifiers: CLASSIFIERS names directories under MODEL_DIR, each
# holding a compiled model.json + classes.json (deploy/vision/fetch-classifier.sh).
classifier_args=""
if [ "$NO_AIPU" != "1" ]; then
    for c in $CLASSIFIERS; do
        if [ -f "$MODEL_DIR/$c/model.json" ]; then
            classifier_args="$classifier_args --classifier /models/$c"
        else
            echo "no classifier at $MODEL_DIR/$c/model.json — skipping it (deploy/vision/fetch-classifier.sh)" >&2
        fi
    done
fi

# A stale container from a previous run must not block this one.
docker rm -f "$NAME" >/dev/null 2>&1 || true

# The read-only view listener (MJPEG stream + still) may go on the LAN: it
# can fire nothing and configure nothing. VIEW_PORT=0 leaves it off.
view_publish=""
view_args=""
if [ "$VIEW_PORT" != "0" ]; then
    view_publish="-p $VIEW_PORT:$VIEW_PORT"
    view_args="--view-port $VIEW_PORT --view-bind 0.0.0.0"
fi

# The control port is published on loopback only: the sidecar has no authentication.
# shellcheck disable=SC2086
exec docker run --rm $detach --name "$NAME" \
    --privileged \
    -v /dev:/dev \
    -v /sys:/sys \
    -v "$SRC_DIR:/opt/benchctrl/src:ro" \
    -v "$MODEL_DIR:/models:ro" \
    -p "127.0.0.1:$PORT:$PORT" \
    $view_publish \
    "$IMAGE" \
    --bind 0.0.0.0 --port "$PORT" $view_args \
    $mode_flag --exposure-us "$EXPOSURE_US" --gain-db "$GAIN_DB" --fps "$FPS" \
    --jpeg-quality "$JPEG_QUALITY" $model_args $classifier_args

# Narrow-grant variant, for when --privileged is worth revisiting. Replace the
# three lines --privileged / -v /dev:/dev / -v /sys:/sys with:
#
#     --cap-add SYS_RAWIO --cap-add SYS_ADMIN \
#     --device /dev/metis-0:1:0 --device /dev/dma_heap/system \
#     -v /sys/class/metis:/sys/class/metis:ro \
#     -v /sys/bus/pci:/sys/bus/pci:ro \
#     -v /dev/bus/usb:/dev/bus/usb \
#     --group-add "$(getent group axelera | cut -d: -f3)" \
#
# Inside the container --bind 0.0.0.0 is correct: Docker's -p maps it to the
# host's 127.0.0.1, and nothing else can reach the container's network.

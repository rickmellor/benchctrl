#!/bin/sh
# Put a compiled Axelera model (.axm) where the sidecar mounts it, and record
# its checksum. Models are compiled on scrub with the Axelera devkit (the metis
# repo's compile pipeline) and never on the Pi; this copies the artifact.
#
#     ./deploy/vision/fetch-model.sh                      # yolov8n-coco.axm from scrub
#     SRC=/mnt/ug-models/axelera/yolov8n-coco.axm ./deploy/vision/fetch-model.sh
#     SRC=rick@192.168.1.240:~/repos/metis/experiments/models/yolov8n-coco.axm ./deploy/vision/fetch-model.sh
set -eu
MODEL=${MODEL:-yolov8n-coco.axm}
SRC=${SRC:-rick@192.168.1.240:~/repos/metis/experiments/models/$MODEL}
MODEL_DIR=${MODEL_DIR:-$HOME/benchctrl/models}

mkdir -p "$MODEL_DIR"
dest=$MODEL_DIR/$MODEL
case "$SRC" in
    *:*) scp -q "$SRC" "$dest" ;;
    *)   cp "$SRC" "$dest" ;;
esac
sum=$(sha256sum "$dest" | cut -d' ' -f1)
# One line per model; replace an existing entry.
manifest=$MODEL_DIR/MANIFEST
touch "$manifest"
grep -v "  $MODEL\$" "$manifest" > "$manifest.tmp" || true
printf '%s  %s\n' "$sum" "$MODEL" >> "$manifest.tmp"
mv "$manifest.tmp" "$manifest"
echo "$dest"
echo "sha256 $sum (recorded in $manifest)"

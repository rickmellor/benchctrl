#!/bin/sh
# Put a compiled indicator classifier (a directory: model.json, its blobs and
# classes.json) where the sidecar mounts it, and record its checksums.
# Classifiers are trained and compiled on scrub with the Axelera devkit (the
# metis repo's experiments/vision/bench_led/ pipeline), never on the Pi.
#
#     NAME=led ./deploy/vision/fetch-classifier.sh
#     NAME=led SRC=rick@192.168.1.240:~/repos/metis/experiments/vision/bench_led/compiled_led ./deploy/vision/fetch-classifier.sh
#
# Then add NAME to CLASSIFIERS in /etc/benchctrl/vision.env and restart the unit.
set -eu
NAME=${NAME:-led}
SRC=${SRC:-rick@192.168.1.240:~/repos/metis/experiments/vision/bench_led/compiled_$NAME}
MODEL_DIR=${MODEL_DIR:-$HOME/benchctrl/models}

mkdir -p "$MODEL_DIR"
dest=$MODEL_DIR/$NAME
rm -rf "$dest.tmp"
case "$SRC" in
    *:*) scp -qr "$SRC" "$dest.tmp" ;;
    *)   cp -r "$SRC" "$dest.tmp" ;;
esac
for f in model.json classes.json; do
    if [ ! -f "$dest.tmp/$f" ]; then
        echo "$SRC has no $f — not a compiled classifier directory" >&2
        rm -rf "$dest.tmp"
        exit 1
    fi
done
rm -rf "$dest"
mv "$dest.tmp" "$dest"
(cd "$dest" && find . -type f ! -name SHA256SUMS | sort | xargs sha256sum > SHA256SUMS)
sum=$(sha256sum "$dest/SHA256SUMS" | cut -d' ' -f1)
# One line per model; replace an existing entry.
manifest=$MODEL_DIR/MANIFEST
touch "$manifest"
grep -v "  $NAME/\$" "$manifest" > "$manifest.tmp" || true
printf '%s  %s/\n' "$sum" "$NAME" >> "$manifest.tmp"
mv "$manifest.tmp" "$manifest"
echo "$dest"
echo "sha256 $sum of SHA256SUMS (recorded in $manifest)"

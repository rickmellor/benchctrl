#!/bin/sh
# Build the benchctrl-vision sidecar image on this host (arm64 on a Pi 5, amd64
# on a desktop — same Dockerfile). ~3.5 GB, 15–25 min on a Pi 5.
#
#     ./deploy/vision/build.sh               # benchctrl-vision:1.8.0
#     AX_VERSION=1.8.0 ./deploy/vision/build.sh
set -eu
AX_VERSION=${AX_VERSION:-1.8.0}
IMAGE=${IMAGE:-benchctrl-vision:$AX_VERSION}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/../.." && pwd)
echo "building $IMAGE from $here/Dockerfile (context $repo)"
exec docker build --build-arg "AX_VERSION=$AX_VERSION" -t "$IMAGE" -f "$here/Dockerfile" "$repo"

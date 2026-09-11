#!/bin/sh
# Install the Axelera Metis kernel driver (metis-dkms 1.6.2) on a Debian-family
# host — Raspberry Pi OS on a Pi 5, Ubuntu on a desktop. Run as root:
#
#     sudo ./deploy/vision/install-metis-driver.sh
#     sudo DEB=/path/to/metis-dkms_1.6.2_all.deb ./deploy/vision/install-metis-driver.sh
#
# The package is arch `all` (DKMS source) and depends only on dkms, so dpkg
# installs it directly; it builds the module against the running kernel,
# installs /etc/udev/rules.d/72-axelera.rules and creates the `axelera` group.
# This script adds the build prerequisites, the service user to that group, a
# modules-load entry, and prints what to check. Idempotent.
#
# Scrub's original (Arch) port of the same package lives in the metis repo as
# scripts/install-driver.sh; this is the Debian-native form of it.
set -eu

VER=1.6.2
DEB_NAME=metis-dkms_${VER}_all.deb
DEB_SHA256=c1c6cd2dd3923ddc24bc8f61d86ce82a8ca3f8f2144b6f08f898ab47898364b1
# Where to get the .deb if DEB= is not given. Axelera's apt pool is the
# canonical source; a copy also lives in the metis repo (downloads/).
DEB_URL=${DEB_URL:-https://software.axelera.ai/artifactory/axelera-apt-source/pool/main/m/metis-dkms/$DEB_NAME}
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi
case "${SUDO_USER:-}" in
    ""|root) RUN_USER=${RUN_USER:-rick} ;;
    *)       RUN_USER=${RUN_USER:-$SUDO_USER} ;;
esac

echo "== 1. build prerequisites for kernel $(uname -r)"
export DEBIAN_FRONTEND=noninteractive
apt-get install -y --no-install-recommends dkms build-essential >/dev/null
# Raspberry Pi OS names its headers by board; other Debians by kernel release.
if ! apt-get install -y --no-install-recommends "linux-headers-$(uname -r)" >/dev/null 2>&1; then
    apt-get install -y --no-install-recommends linux-headers-rpi-2712 >/dev/null 2>&1 \
        || echo "  (no matching linux-headers package — DKMS will say so below)"
fi

echo "== 2. the package"
DEB=${DEB:-}
if [ -z "$DEB" ]; then
    for cand in "$here/$DEB_NAME" "/home/$RUN_USER/$DEB_NAME" "/tmp/$DEB_NAME"; do
        [ -f "$cand" ] && DEB=$cand && break
    done
fi
if [ -z "$DEB" ]; then
    DEB=/tmp/$DEB_NAME
    echo "  downloading $DEB_URL"
    if ! curl -fsSL -o "$DEB" "$DEB_URL"; then
        echo "download failed. Copy the package here and re-run with DEB=/path/to/$DEB_NAME" >&2
        echo "(a copy lives in the metis repo: downloads/$DEB_NAME)" >&2
        exit 1
    fi
fi
actual=$(sha256sum "$DEB" | cut -d' ' -f1)
if [ "$actual" != "$DEB_SHA256" ]; then
    echo "sha256 mismatch for $DEB" >&2
    echo "  expected $DEB_SHA256" >&2
    echo "  got      $actual" >&2
    exit 1
fi
echo "  $DEB (sha256 ok)"

echo "== 3. install (dkms add/build/install runs in postinst)"
if dkms status "metis/$VER" 2>/dev/null | grep -q installed; then
    echo "  metis/$VER already built for this kernel; reinstalling the package anyway"
fi
dpkg -i "$DEB"

echo "== 4. access for $RUN_USER, and load at boot"
groupadd -f axelera
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx axelera; then
    usermod -aG axelera "$RUN_USER"
    echo "  added $RUN_USER to axelera (re-login to take effect)"
fi
printf 'metis\n' > /etc/modules-load.d/metis.conf
udevadm control --reload-rules && udevadm trigger && udevadm settle || true
modprobe metis

echo
echo "== verification"
dkms status "metis/$VER" || true
echo "--- lspci (Axelera 1f9d:1100):"
lspci -k -d 1f9d:1100 || echo "  no Axelera device on the bus"
for d in /sys/bus/pci/devices/*; do
    [ "$(cat "$d/vendor" 2>/dev/null)" = "0x1f9d" ] || continue
    echo "  $(basename "$d"): link $(cat "$d/current_link_speed") x$(cat "$d/current_link_width")" \
         "(max $(cat "$d/max_link_speed") x$(cat "$d/max_link_width"))"
done
echo "--- device nodes:"
ls -l /dev/metis* 2>/dev/null || echo "  (no /dev/metis* yet — check: dmesg | grep -iE 'metis|axl')"
echo "--- dmesg:"
dmesg | grep -iE 'metis|axl' | tail -8 || true
echo "== done"

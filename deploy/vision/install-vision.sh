#!/bin/sh
# Install the benchctrl-vision sidecar as a systemd service.
#
# Run on the BENCH machine (the one with the camera and the Metis), as root,
# AFTER build.sh has produced the image and install-metis-driver.sh has loaded
# the driver:
#
#     sudo ./deploy/vision/install-vision.sh
#
# Idempotent: re-running upgrades the run script and the unit and leaves an
# existing /etc/benchctrl/vision.env alone.
set -eu

CONF_DIR=/etc/benchctrl
UNIT=benchctrl-vision.service
here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi
if ! command -v docker >/dev/null 2>&1; then
    echo "docker is required (apt install docker.io)" >&2
    exit 1
fi

# Derive the same way install-agent.sh does: a checkout beside deploy/, the
# invoking user, state under that user's home.
case "${SUDO_USER:-}" in
    ""|root) RUN_USER=${RUN_USER:-rick} ;;
    *)       RUN_USER=${RUN_USER:-$SUDO_USER} ;;
esac
SRC_DIR=${SRC_DIR:-$(CDPATH= cd -- "$here/../../src" && pwd)}
MODEL_DIR=${MODEL_DIR:-/home/$RUN_USER/benchctrl/models}
echo "install-vision: RUN_USER=$RUN_USER SRC_DIR=$SRC_DIR MODEL_DIR=$MODEL_DIR"

install -d -m 0755 "$CONF_DIR"
install -d -m 0755 -o "$RUN_USER" -g "$RUN_USER" "$MODEL_DIR"

install -m 0755 "$here/run-vision.sh" /usr/local/bin/benchctrl-vision-run
install -m 0755 "$here/metis-rescan.sh" /usr/local/bin/benchctrl-metis-rescan
install -m 0644 "$here/systemd/benchctrl-metis-rescan.service" /etc/systemd/system/

if [ -f "$CONF_DIR/vision.env" ]; then
    echo "keeping existing $CONF_DIR/vision.env"
else
    sed -e "s|^SRC_DIR=.*|SRC_DIR=$SRC_DIR|" \
        -e "s|^MODEL_DIR=.*|MODEL_DIR=$MODEL_DIR|" \
        "$here/vision.env.example" > "$CONF_DIR/vision.env"
    chmod 0644 "$CONF_DIR/vision.env"
    echo "wrote $CONF_DIR/vision.env"
fi

install -m 0644 "$here/systemd/$UNIT" "/etc/systemd/system/$UNIT"
systemctl daemon-reload
# The rescan runs once per boot; run it now too, so a first install on a host
# whose card is currently in the bad state comes up without a reboot.
systemctl enable benchctrl-metis-rescan.service
systemctl restart benchctrl-metis-rescan.service || true
systemctl enable "$UNIT"
systemctl restart "$UNIT"

# The container takes a few seconds to open the camera and load the model.
port=$(sed -n 's/^PORT=\([0-9]*\).*/\1/p' "$CONF_DIR/vision.env")
port=${port:-8095}
n=0
while [ $n -lt 30 ]; do
    if curl -fsS "http://127.0.0.1:$port/health" >/dev/null 2>&1; then
        echo
        echo "$UNIT is up:"
        curl -fsS "http://127.0.0.1:$port/health"; echo
        echo "  journalctl -u $UNIT -n 20 --no-pager"
        exit 0
    fi
    n=$((n + 1)); sleep 1
done
echo "$UNIT did not answer /health within 30 s:" >&2
systemctl status "$UNIT" --no-pager -l >&2 || true
journalctl -u "$UNIT" -n 30 --no-pager >&2 || true
exit 1

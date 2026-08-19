#!/bin/sh
# Install the DP/HDMI hotplug fix (see README.md — "Display hotplug").
#
# Only needed on a board whose display arrives as DisplayPort altmode through
# a USB-C hub, where the hub can negotiate DP after Xorg's startup probe.
#
#     sudo ./install-display-hotplug.sh

set -eu

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

for cmd in systemctl udevadm xrandr logger; do
    command -v "$cmd" >/dev/null 2>&1 || {
        echo "missing required command: $cmd" >&2
        exit 1
    }
done

install -m 0755 "$here/benchctrl-display-hotplug" /usr/local/bin/
install -m 0644 "$here/systemd/benchctrl-display-hotplug.service" /etc/systemd/system/
install -m 0644 "$here/udev/99-benchctrl-display-hotplug.rules" /etc/udev/rules.d/

systemctl daemon-reload
udevadm control --reload-rules

# Deliberately NOT enabled: the unit is pulled in on demand by the udev rule's
# SYSTEMD_WANTS, so enabling it would also run it once at every boot.
echo "installed. verify with:"
echo "  udevadm verify /etc/udev/rules.d/99-benchctrl-display-hotplug.rules"
echo "  udevadm trigger --action=change --subsystem-match=drm"
echo "  journalctl -t display-hotplug -n 5 --no-pager"

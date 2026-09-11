#!/bin/sh
# Boot the board straight into the fullscreen dashboard — no greeter, no login.
#
#     sudo ./install-kiosk.sh          # turn it on
#     sudo ./install-kiosk.sh --undo   # back to the normal xfce greeter
#
# Run install-fui.sh first, and confirm the display works over an ssh tunnel,
# because after this the board has no login prompt. With no keyboard attached,
# **ssh is the only way back in.** The script refuses to proceed unless it can
# see that ssh is actually running, since bricking the console on a
# keyboard-less board is a one-way trip otherwise.

set -eu

# The autologin user: whoever invoked sudo (the Pi's login user), else the Uno
# Q's `arduino`. Never root — see install-agent.sh for the same rule.
if [ -z "${RUN_USER:-}" ]; then
    case "${SUDO_USER:-}" in
        ""|root) RUN_USER=arduino ;;
        *)       RUN_USER=$SUDO_USER ;;
    esac
fi
DROPIN=/etc/lightdm/lightdm.conf.d/90-benchctrl-kiosk.conf
SESSION=/usr/share/xsessions/benchctrl-kiosk.desktop

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

# --- undo -----------------------------------------------------------------
if [ "${1:-}" = "--undo" ]; then
    rm -f "$DROPIN"
    echo "removed $DROPIN"
    echo "the kiosk session is left installed and still selectable at the greeter"
    echo "restart the greeter when ready:  systemctl restart lightdm"
    exit 0
fi

# --- refuse to strand the board -------------------------------------------
# Neither of these is paranoia: this change removes the only local way to log
# in, on a board whose console has no keyboard.
if ! systemctl is-active --quiet ssh && ! systemctl is-active --quiet sshd; then
    echo "refusing: no ssh service is active, and after this there is no local" >&2
    echo "login. Start ssh first (systemctl enable --now ssh)." >&2
    exit 1
fi

if [ ! -x /usr/local/bin/benchctrl-fui ]; then
    echo "refusing: /usr/local/bin/benchctrl-fui is missing." >&2
    echo "Run install-fui.sh first — otherwise this boots to a black" >&2
    echo "screen with no login prompt." >&2
    exit 1
fi

if [ ! -d /etc/lightdm ]; then
    echo "refusing: no /etc/lightdm — this script only handles lightdm." >&2
    exit 1
fi

# lightdm gained lightdm.conf.d in 1.12; the board runs 1.32. Check anyway,
# because a silently-ignored drop-in looks identical to a broken kiosk.
version=$(/usr/sbin/lightdm --version 2>&1 | sed -n 's/^lightdm *//p')
case "$version" in
1.[0-9].*|1.1[01].*)
    echo "refusing: lightdm $version predates lightdm.conf.d support." >&2
    exit 1
    ;;
esac

# --- install --------------------------------------------------------------
install -m 0755 "$here/benchctrl-kiosk" /usr/local/bin/
install -m 0644 "$here/xsessions/benchctrl-kiosk.desktop" "$SESSION"

# A Raspberry Pi's Xorg needs to be told which of its two DRM devices has the
# HDMI ports, or it never starts (see deploy/xorg/20-benchctrl-vc4.conf). Only
# where the vc4 driver is present: the Uno Q's msm display is unaffected.
if [ -d /sys/module/vc4 ] || [ -d /sys/bus/platform/drivers/vc4-drm ]; then
    install -d -m 0755 /etc/X11/xorg.conf.d
    install -m 0644 "$here/xorg/20-benchctrl-vc4.conf" /etc/X11/xorg.conf.d/
    echo "installed /etc/X11/xorg.conf.d/20-benchctrl-vc4.conf (vc4 display)"
fi

# Debian's lightdm-autologin PAM stack admits only members of `autologin`.
# Without this the drop-in below is silently ignored and the greeter appears.
if ! getent group autologin >/dev/null; then
    groupadd autologin
fi
if ! id -nG "$RUN_USER" | tr ' ' '\n' | grep -qx autologin; then
    usermod -aG autologin "$RUN_USER"
    echo "added $RUN_USER to the autologin group"
fi

# A board that boots to the console never starts a display manager, however
# enabled it is: lightdm is pulled in by graphical.target only.
if [ "$(systemctl get-default)" != "graphical.target" ]; then
    systemctl set-default graphical.target
    echo "default target -> graphical.target (was multi-user)"
fi

install -d -m 0755 /etc/lightdm/lightdm.conf.d
sed "s/^autologin-user=.*/autologin-user=$RUN_USER/" \
    "$here/lightdm/90-benchctrl-kiosk.conf" > "$DROPIN"
chmod 0644 "$DROPIN"

cat <<EOF

installed:
  $SESSION
  $DROPIN  (autologin-user=$RUN_USER)

Nothing else was changed. lightdm stays enabled — benchctrl-display-hotplug
needs it to start Xorg — and xfce is still installed and selectable.

Apply it:
  systemctl restart lightdm     # or just reboot

RECOVERY (there will be no login prompt on the panel):
  ssh $RUN_USER@<board>
  sudo rm $DROPIN
  sudo systemctl restart lightdm

Watch it come up:
  journalctl -t benchctrl-kiosk -t benchctrl-fui -f
EOF

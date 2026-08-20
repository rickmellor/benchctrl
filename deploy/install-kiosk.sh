#!/bin/sh
# Boot the board straight into the fullscreen dashboard — no greeter, no login.
#
#     sudo ./install-kiosk.sh          # turn it on
#     sudo ./install-kiosk.sh --undo   # back to the normal xfce greeter
#
# Run install-dashboard.sh first, and confirm the panel works, because after
# this the board has no login prompt. With no keyboard attached, **ssh is the
# only way back in.** The script refuses to proceed unless it can see that ssh
# is actually running, since bricking the console on a keyboard-less board is a
# one-way trip otherwise.

set -eu

RUN_USER=${RUN_USER:-arduino}
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

if [ ! -x /usr/local/bin/benchctrl-dashboard ]; then
    echo "refusing: /usr/local/bin/benchctrl-dashboard is missing." >&2
    echo "Run install-dashboard.sh first — otherwise this boots to a black" >&2
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
  journalctl -t benchctrl-kiosk -t benchctrl-dashboard -f
EOF

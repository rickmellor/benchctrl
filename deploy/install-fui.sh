#!/bin/sh
# Install the FUI status display: just the launcher.
#
#     sudo ./install-fui.sh
#
# No venv and no pip: the FUI is stdlib
# http.server plus static files, so it runs from the system python. That keeps
# deploy/'s pip-free story intact for the display as well as the agent, and it
# adds nothing to a root filesystem that is ~2 GB and over 80% full.
#
# This does NOT touch the display manager. Boot-to-kiosk is a separate,
# reversible step — run install-kiosk.sh afterwards, once you have confirmed the
# display works over an ssh tunnel.

set -eu

RUN_USER=${RUN_USER:-arduino}
SRC_DIR=${SRC_DIR:-/home/arduino/benchctrl-1.2.0/src}
SYSTEM_PYTHON=${SYSTEM_PYTHON:-/usr/bin/python3}
PORT=${BENCHCTRL_FUI_PORT:-8600}

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "user '$RUN_USER' does not exist; set RUN_USER=..." >&2
    exit 1
fi

if [ ! -d "$SRC_DIR/benchctrl/dashboards/fui" ]; then
    echo "no fui package at $SRC_DIR/benchctrl/dashboards/fui; set SRC_DIR=..." >&2
    exit 1
fi

# The static assets are the display. Shipping the python without them yields a
# server that 404s its own page, which on a kiosk is a blank screen with no clue.
for asset in index.html fui.css fui.js; do
    if [ ! -f "$SRC_DIR/benchctrl/dashboards/fui/static/$asset" ]; then
        echo "missing static asset: $asset — is the deployed tree complete?" >&2
        exit 1
    fi
done

# Prove it imports as the user that will run it, before declaring success. An
# import error at boot happens on a screen, with nobody able to log in and read
# it. Probed from /tmp because $HOME on this board contains a 'benchctrl' runs
# directory that shadows the package as a namespace package.
if ! su -s /bin/sh "$RUN_USER" -c \
    "cd /tmp && PYTHONPATH='$SRC_DIR' '$SYSTEM_PYTHON' -c 'import benchctrl.dashboards.fui.server'"
then
    echo "cannot import benchctrl.dashboards.fui.server with PYTHONPATH=$SRC_DIR" >&2
    exit 1
fi

install -m 0755 "$here/benchctrl-fui" /usr/local/bin/

# The launcher reads the agent's token file. It is 0640 root:$RUN_USER, so the
# desktop user can already read it via the group — check rather than assume,
# because the failure is a display stuck on an auth error.
if [ -f /etc/benchctrl/agent.json ]; then
    if ! su -s /bin/sh "$RUN_USER" -c 'test -r /etc/benchctrl/agent.json'; then
        echo "warning: $RUN_USER cannot read /etc/benchctrl/agent.json" >&2
        echo "  fix with: chown root:$RUN_USER /etc/benchctrl/agent.json" >&2
    fi
else
    echo "note: no /etc/benchctrl/agent.json yet — run install-agent.sh first," >&2
    echo "  or set BENCHCTRL_TOKEN in the environment." >&2
fi

cat <<EOF

installed. Test it BEFORE turning on boot-to-kiosk:

  sudo -u $RUN_USER /usr/local/bin/benchctrl-fui

then from your workstation:

  ssh -L $PORT:127.0.0.1:$PORT $RUN_USER@<board>
  # and open http://127.0.0.1:$PORT

Once the display shows the bench correctly:

  sudo $here/install-kiosk.sh

The kiosk session runs the FUI. There is no second display to fall back to; if
it will not come up, recover the board's normal login instead:

  sudo rm /etc/lightdm/lightdm.conf.d/90-benchctrl-kiosk.conf
  sudo systemctl restart lightdm
EOF

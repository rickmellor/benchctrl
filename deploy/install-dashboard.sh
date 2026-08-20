#!/bin/sh
# Install the HDMI status dashboard: a venv with Streamlit, plus the launcher.
#
#     sudo ./install-dashboard.sh
#
# This does NOT touch the display manager. Boot-to-kiosk is a separate,
# reversible step — run install-kiosk.sh afterwards, once you have confirmed
# the panel works over an ssh tunnel.
#
# Note this breaks deploy/'s otherwise pip-free story. That is deliberate and
# scoped: the AGENT stays pip-free, because it must install on a board with no
# pip at all. The dashboard is opt-in and needs one. A board can keep running
# the agent with no display and no venv, exactly as before.

set -eu

RUN_USER=${RUN_USER:-arduino}
# /home/arduino, never /: root on this board is ~2 GB and 80% full, and the
# venv is ~250 MB installed.
VENV=${VENV:-/home/arduino/benchctrl-dashboard-venv}
SRC_DIR=${SRC_DIR:-/home/arduino/benchctrl-1.2.0/src}
SYSTEM_PYTHON=${SYSTEM_PYTHON:-/usr/bin/python3}

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "user '$RUN_USER' does not exist; set RUN_USER=..." >&2
    exit 1
fi

if [ ! -d "$SRC_DIR/benchctrl" ]; then
    echo "no benchctrl package at $SRC_DIR/benchctrl; set SRC_DIR=..." >&2
    exit 1
fi

# python3-venv is a separate package on Debian and its absence shows up as a
# baffling failure deep inside `python3 -m venv`, so check for it up front.
if ! "$SYSTEM_PYTHON" -c 'import venv, ensurepip' 2>/dev/null; then
    echo "missing venv support. Install it first:" >&2
    echo "  sudo apt-get install -y python3-venv python3-pip" >&2
    exit 1
fi

# --- the venv -------------------------------------------------------------
# Owned by RUN_USER, not root: the kiosk session runs as that user, and a
# root-owned venv would work but leaves no way to add a package without sudo.
if [ -x "$VENV/bin/streamlit" ]; then
    echo "keeping existing venv at $VENV"
else
    echo "creating venv at $VENV (this pulls ~100 MB of wheels)"
    install -d -o "$RUN_USER" -g "$RUN_USER" "$(dirname "$VENV")"
    su -s /bin/sh "$RUN_USER" -c "$SYSTEM_PYTHON -m venv '$VENV'"
    su -s /bin/sh "$RUN_USER" -c "'$VENV/bin/pip' install --upgrade pip"
    # No version pin: every dependency has a prebuilt cp313 aarch64 wheel, and
    # pinning here would mean a second place to update. Add a constraints file
    # if this board ever needs reproducibility.
    su -s /bin/sh "$RUN_USER" -c "'$VENV/bin/pip' install streamlit"
fi

# Prove it imports before declaring success. A venv whose streamlit half-built
# fails at boot, on a screen, with nobody able to log in and read it.
if ! su -s /bin/sh "$RUN_USER" -c "'$VENV/bin/python' -c 'import streamlit'"; then
    echo "streamlit does not import in $VENV" >&2
    exit 1
fi

# And prove the dashboard's own modules import against it, which catches a
# wrong SRC_DIR now rather than as a blank panel later.
if ! su -s /bin/sh "$RUN_USER" -c \
    "PYTHONPATH='$SRC_DIR' '$VENV/bin/python' -c 'import benchctrl.dashboards.panel'"
then
    echo "cannot import benchctrl.dashboards.panel with PYTHONPATH=$SRC_DIR" >&2
    exit 1
fi

# --- the launcher ---------------------------------------------------------
install -m 0755 "$here/benchctrl-dashboard" /usr/local/bin/

# The launcher reads the agent's token file. It is 0640 root:$RUN_USER, so the
# desktop user can already read it via the group — check rather than assume,
# because the failure is a panel stuck on an auth error.
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

  sudo -u $RUN_USER /usr/local/bin/benchctrl-dashboard

then from your workstation:

  ssh -L 8501:127.0.0.1:8501 $RUN_USER@<board>
  # and open http://127.0.0.1:8501

Once the panel shows the bench correctly:

  sudo $here/install-kiosk.sh
EOF

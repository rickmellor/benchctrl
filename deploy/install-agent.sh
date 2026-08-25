#!/bin/sh
# Install the benchctrl agent as a systemd service.
#
# Run on the BENCH machine (the one with the instruments attached), as root:
#
#     sudo ./install-agent.sh
#
# Idempotent: re-running upgrades the unit and leaves an existing
# /etc/benchctrl/agent.json alone, so your token survives.

set -eu

PYTHON=${PYTHON:-/usr/bin/python3}
SRC_DIR=${SRC_DIR:-/home/arduino/benchctrl-1.2.0/src}
RUN_USER=${RUN_USER:-arduino}
CONF_DIR=/etc/benchctrl
UNIT=benchctrl-agent.service

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)

if [ "$(id -u)" -ne 0 ]; then
    echo "must run as root (try: sudo $0)" >&2
    exit 1
fi

if ! command -v systemctl >/dev/null 2>&1; then
    echo "no systemctl found — this script only handles systemd hosts" >&2
    exit 1
fi

if ! id "$RUN_USER" >/dev/null 2>&1; then
    echo "user '$RUN_USER' does not exist; set RUN_USER=..." >&2
    exit 1
fi

# Fail here rather than in a restart loop later: a wrong PYTHONPATH shows up
# as a unit that flaps every 5 s with an ImportError buried in the journal.
if ! PYTHONPATH="$SRC_DIR" "$PYTHON" -c 'import benchctrl, serial' 2>/dev/null; then
    echo "cannot import benchctrl and pyserial with" >&2
    echo "  PYTHONPATH=$SRC_DIR $PYTHON" >&2
    echo "Set SRC_DIR=/path/to/src (the dir holding both benchctrl/ and serial/)," >&2
    echo "or PYTHON=/path/to/venv/bin/python for a pip install." >&2
    exit 1
fi

install -d -m 0755 "$CONF_DIR"

# --- config: never clobber an existing token -------------------------------
if [ -f "$CONF_DIR/agent.json" ]; then
    echo "keeping existing $CONF_DIR/agent.json"
else
    install -m 0640 -o root -g "$RUN_USER" \
        "$here/systemd/agent.json.example" "$CONF_DIR/agent.json"
    token=$(PYTHONPATH="$SRC_DIR" "$PYTHON" -m benchctrl.agent.main --generate-token)
    # The placeholder is a fixed literal in the example file, so a plain
    # substitution is safe; the generated token is URL-safe base64 and can
    # contain '/', hence the '|' delimiter.
    sed -i "s|REPLACE-ME[^\"]*|$token|" "$CONF_DIR/agent.json"
    echo "wrote $CONF_DIR/agent.json with a fresh token (mode 0640)"
    echo "  client-side token: $token"
fi

# 0640 root:$RUN_USER — the agent warns loudly if the token file is
# group/world-readable, and it must stay readable by the service user.
chmod 0640 "$CONF_DIR/agent.json"
chown "root:$RUN_USER" "$CONF_DIR/agent.json"

# --- environment ----------------------------------------------------------
sed "s|^PYTHONPATH=.*|PYTHONPATH=$SRC_DIR|" \
    "$here/systemd/agent.env.example" > "$CONF_DIR/agent.env"
chmod 0644 "$CONF_DIR/agent.env"

# Device secrets live in a *separate* 0600 file, never in agent.env above:
# that one is world-readable on purpose (PYTHONPATH is diagnostic information
# an operator should be able to read without sudo), and a secret must not
# inherit that. The unit pulls this in with a leading `-`, so a bench with no
# secret-bearing device works with no file at all.
#
# Created empty-but-commented rather than not at all, so the place to put a
# credential is discoverable on the board instead of only in the docs — and it
# is never overwritten, because that would silently delete a working password
# on a re-install.
if [ ! -e "$CONF_DIR/agent.secrets.env" ]; then
    cat > "$CONF_DIR/agent.secrets.env" <<'SECRETS'
# Device credentials for benchctrl-agent. Mode 0600, read by systemd as root
# before the service drops privileges, so it need not be readable by the
# service user.
#
# No quotes, no `export`, no trailing spaces: systemd parses this itself and a
# quoted value arrives with the quotes attached.
#
# Switched PDU (CyberPower PDU41002). Uncomment and set to enable the network
# or serial console login; see docs/drivers.md.
#BENCHCTRL_PDU_PASSWORD=
SECRETS
    echo "wrote $CONF_DIR/agent.secrets.env (mode 0600, no secrets set)"
fi
chmod 0600 "$CONF_DIR/agent.secrets.env"

# --- unit -----------------------------------------------------------------
sed -e "s|^ExecStart=/usr/bin/python3 |ExecStart=$PYTHON |" \
    -e "s|^ExecStopPost=/usr/bin/python3 |ExecStopPost=$PYTHON |" \
    -e "s|^User=arduino$|User=$RUN_USER|" \
    -e "s|^Group=arduino$|Group=$RUN_USER|" \
    "$here/systemd/$UNIT" > "/etc/systemd/system/$UNIT"
chmod 0644 "/etc/systemd/system/$UNIT"

systemctl daemon-reload
systemctl enable "$UNIT"
systemctl restart "$UNIT"

# Type=simple reports "active" the moment fork() succeeds, so an immediate
# check proves nothing. Give it long enough to bind or die.
sleep 3
if systemctl is-active --quiet "$UNIT"; then
    echo
    echo "$UNIT is running. Verify the port is actually bound:"
    echo "  journalctl -u $UNIT -n 20 --no-pager"
else
    echo "$UNIT failed to start:" >&2
    systemctl status "$UNIT" --no-pager -l >&2 || true
    exit 1
fi

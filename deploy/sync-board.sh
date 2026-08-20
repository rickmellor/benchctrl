#!/bin/sh
# Push this checkout's benchctrl source to the board, and prove it landed.
#
#     ./sync-board.sh                      # sync, then verify
#     ./sync-board.sh --check              # verify only; change nothing
#     ./sync-board.sh --restart            # sync, verify, restart what runs it
#
# Runs from your workstation, not the board. Needs only ssh/scp/tar/python3 on
# both ends — the board has no rsync.
#
# Why this exists
# ---------------
#
# The FUI was installed on the board twice from a staging directory older than
# the repo. Both times install-fui.sh faithfully installed pre-fix software: the
# launchers lost their fullscreen flags and the deployed state.py had none of the
# observer-role safety check. Nothing caught it, because "the board is running
# the FUI" and "the board is running the FUI you reviewed" looked identical from
# outside — the display was up, so every check passed.
#
# So the point of this script is not the copy. It is that afterwards a manifest
# from each end is compared and it either says IN SYNC or names every file that
# differs. A sync you cannot verify is the failure it exists to prevent, which is
# why --check is a first-class mode: the useful question most days is "is the
# board current?", not "make it current".
#
# It deliberately does NOT restart anything unless asked. The board runs a bench;
# bouncing the agent disconnects instruments, and restarting lightdm blanks the
# panel. Both are the operator's call.
# END HELP

set -eu

BOARD=${BOARD:-arduino@192.168.1.86}
REMOTE_SRC=${REMOTE_SRC:-/home/arduino/benchctrl-1.2.0/src}
REMOTE_DEPLOY=${REMOTE_DEPLOY:-/home/arduino/deploy}
SSH_OPTS=${SSH_OPTS:-}
# The one subtree this script owns. Everything else under REMOTE_SRC — the
# vendored pyserial/pyusb/pyvisa the board needs because it has no pip — is left
# strictly alone: never compared, never transferred, never deleted.
PACKAGE=${PACKAGE:-benchctrl}

here=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
repo=$(CDPATH= cd -- "$here/.." && pwd)
LOCAL_SRC="$repo/src"
MANIFEST="$here/board_sync_manifest.py"

mode=sync
restart=no
for arg in "$@"; do
    case "$arg" in
    --check) mode=check ;;
    --restart) restart=yes ;;
    -h | --help)
        # Delimited by a sentinel rather than line numbers, which silently
        # truncate mid-sentence the moment the header is edited.
        sed -n '2,/^# END HELP$/p' "$0" | grep -v '^# END HELP$' | sed 's/^# \{0,1\}//'
        exit 0
        ;;
    *)
        echo "unknown argument: $arg (try --help)" >&2
        exit 2
        ;;
    esac
done

# --check changes nothing, so combining it with --restart can only mean one of
# the two was a mistake. Refuse rather than silently honouring whichever wins.
if [ "$mode" = check ] && [ "$restart" = yes ]; then
    echo "--check and --restart are contradictory: --check changes nothing" >&2
    exit 2
fi

# SSH_OPTS is deliberately word-split (SC2086), and SC2029's client-side
# expansion is intended: every path in these commands comes from this script's
# own variables, not from anything the board controls.
# shellcheck disable=SC2086,SC2029
ssh_board() { ssh $SSH_OPTS "$BOARD" "$@"; }

if [ ! -f "$MANIFEST" ]; then
    echo "missing $MANIFEST" >&2
    exit 1
fi

# --- validate the two variables that are the whole safety boundary ----------
# PACKAGE reaches a tar argument and a `find` root on the board, and that find
# feeds a delete sweep. PACKAGE=. would make the sweep the unscoped sweep the
# scoping exists to prevent, deleting the board's vendored pyserial — its only
# copy. So this is enforced here rather than trusted, and again in
# board_sync_manifest.py, because either entry point alone is enough to do it.
case "$PACKAGE" in
"" | . | .. | -* | */*)
    echo "PACKAGE must be a single plain directory name, not '$PACKAGE'" >&2
    exit 2
    ;;
esac

# The guard must test $PACKAGE, not a hardcoded name: a PACKAGE that exists
# nowhere would otherwise sail past here, both manifests would fail, and two
# empty manifests compare as in sync — the check reporting a board current
# without having looked at it.
if [ ! -d "$LOCAL_SRC/$PACKAGE" ]; then
    echo "no '$PACKAGE' package at $LOCAL_SRC — run this from a checkout" >&2
    exit 1
fi

# REMOTE_SRC is where the tarball is extracted and where the sweep runs, so a
# typo here is destructive rather than merely wrong. /home/arduino/benchctrl —
# one path component away from the default — is the live agent's blob and runs
# directory, holding verified run artifacts; extracting into it and then deleting
# everything the tarball did not contain would destroy them all. Requiring the
# path to end in /src is crude but it excludes exactly that mistake.
case "$REMOTE_SRC" in
*/src) ;;
*)
    echo "REMOTE_SRC must end in /src (got '$REMOTE_SRC')." >&2
    echo "This is a guard, not a style rule: the sweep deletes files under it," >&2
    echo "and /home/arduino/benchctrl is the agent's live runs directory." >&2
    exit 2
    ;;
esac

# --- refuse to sync a tree that is not what it appears to be ---------------
# Syncing uncommitted work is normal during development, so this warns rather
# than blocks. But it says so out loud: the whole point is knowing what is on
# the board, and "the working copy at 14:32" is a weaker answer than a commit.
if command -v git >/dev/null 2>&1 && git -C "$repo" rev-parse --git-dir >/dev/null 2>&1; then
    head=$(git -C "$repo" rev-parse --short HEAD 2>/dev/null || echo unknown)
    if [ -n "$(git -C "$repo" status --porcelain -- src 2>/dev/null)" ]; then
        echo "note: src/ has uncommitted changes; syncing the working tree, not $head"
    else
        echo "syncing src/ at commit $head"
    fi
fi

# --- the comparison, which is the actual product ---------------------------
# The manifest script is copied over each time rather than assumed present, so a
# board that has never been synced still works and a stale copy of the checker
# cannot itself be the source of a wrong answer.
check_sync() {
    # Every command here has its status checked explicitly. check_sync is always
    # called from an `if`, which SUSPENDS set -e for everything inside it — so an
    # unchecked failure would not abort, it would leave a variable empty and
    # carry on. Two empty manifests compare as in_sync, which means a failure to
    # look at the board would print "IN SYNC — 0 files identical". The one
    # outcome this script must never produce.

    # shellcheck disable=SC2086  # SSH_OPTS is deliberately word-split
    if ! scp -O -q $SSH_OPTS "$MANIFEST" "$BOARD:/tmp/board_sync_manifest.py"; then
        # Not just a transfer failure: continuing would run whatever version of
        # the checker a previous sync left in /tmp, so a stale checker could
        # itself become the source of a wrong answer.
        echo "could not copy the manifest script to the board" >&2
        return 1
    fi
    # --only PACKAGE on both ends. The board's src/ also holds vendored
    # serial/usb/pyvisa/pyvisa_py, which are supposed to be there and are not in
    # this repo; comparing the whole directory buries the real drift under ~120
    # phantom differences.
    if ! local_out=$(python3 "$MANIFEST" "$LOCAL_SRC" --only "$PACKAGE"); then
        echo "could not manifest $LOCAL_SRC/$PACKAGE" >&2
        return 1
    fi
    if ! remote_out=$(ssh_board "python3 /tmp/board_sync_manifest.py '$REMOTE_SRC' --only '$PACKAGE'"); then
        echo "could not manifest $BOARD:$REMOTE_SRC/$PACKAGE" >&2
        return 1
    fi
    printf '%s\n' "$local_out" >/tmp/sync-local.manifest
    printf '%s\n' "$remote_out" >/tmp/sync-remote.manifest
    # Belt and braces on the above: a zero-line manifest from either end is
    # refused rather than compared. An empty tree is not a legitimate state for
    # this package — src/benchctrl always has files — so this cannot be a false
    # alarm, and it closes the empty-compares-as-in-sync route no matter which
    # command failed to say so.
    if [ ! -s /tmp/sync-local.manifest ] || [ ! -s /tmp/sync-remote.manifest ]; then
        echo "a manifest came back empty — refusing to call that in sync" >&2
        return 1
    fi
    python3 -c "
import sys
sys.path.insert(0, '$here')
from board_sync_manifest import compare
local = open('/tmp/sync-local.manifest').read()
remote = open('/tmp/sync-remote.manifest').read()
r = compare(local, remote)
if r['in_sync']:
    n = len([l for l in local.splitlines() if l.strip()])
    print(f'IN SYNC — {n} files identical on both ends')
    raise SystemExit(0)
for label, key in (('differs', 'changed'), ('missing on board', 'missing'),
                   ('extra on board', 'extra')):
    for p in r[key]:
        print(f'  {label}: {p}')
raise SystemExit(1)
"
}

if [ "$mode" = check ]; then
    echo "checking $BOARD:$REMOTE_SRC against $LOCAL_SRC"
    if check_sync; then
        exit 0
    fi
    echo
    echo "board is NOT current. Run without --check to fix it." >&2
    exit 1
fi

# --- warn about the agent before touching anything -------------------------
# Overwriting src/ under a running agent is not inert. registry.py imports each
# driver lazily, inside the opener closure, so an agent that has not yet opened a
# given instrument will import the NEW driver into a process whose already-loaded
# safety.py, dispatch.py and server.py are the OLD ones. A mixed-version agent
# driving instruments is worse than either version. Restarting it is still the
# operator's call — it disarms the bench — so this warns rather than blocks, but
# it must not be silent, because the closing message used to imply the opposite.
if ssh_board "pgrep -f 'benchctrl[.]agent[.]main' >/dev/null 2>&1"; then
    echo "note: the agent is RUNNING. Syncing under it leaves that process on a"
    echo "      mix of old and new modules (drivers import lazily). Restart it"
    echo "      when convenient — see the commands printed at the end."
fi

# --- push -----------------------------------------------------------------
# One tarball, not a file-at-a-time scp. That is what went wrong before: each
# individual copy succeeded while the tree as a whole stayed mixed.
#
# It is NOT atomic, and it would be wrong to claim so: tar extracts member by
# member into the live tree, so a dropped connection or a full disk (the board's
# / runs ~84%) leaves exactly the mixed tree this exists to eliminate. What makes
# that survivable is the trap below — the board is never left modified-but-unverified
# without saying so — plus `set -eu` in the remote block, which aborts before the
# delete sweep rather than sweeping against a partial extraction.
#
# --exclude the caches rather than filtering after the fact, so bytecode
# compiled by a different interpreter never reaches the board at all.
echo "packing $LOCAL_SRC"
# Trailing X's: a template with X's in the middle relies on GNU mktemp's implied
# --suffix, and this half runs on the operator's workstation, which may not be.
tarball=$(mktemp /tmp/benchctrl-src-XXXXXX)
trap 'rm -f "$tarball"' EXIT HUP INT TERM
tar czf "$tarball" -C "$LOCAL_SRC" \
    --exclude='__pycache__' \
    --exclude='*.pyc' \
    --exclude='*.pyo' \
    --exclude='*.egg-info' \
    --exclude='.mypy_cache' \
    --exclude='.pytest_cache' \
    --exclude='.ruff_cache' \
    "$PACKAGE"

echo "sending to $BOARD:$REMOTE_SRC"
# shellcheck disable=SC2086  # SSH_OPTS is deliberately word-split
scp -O -q $SSH_OPTS "$tarball" "$BOARD:/tmp/benchctrl-src-sync.tgz"

# From here on the board gets modified, so any nonzero exit must say that the
# tree was touched and never verified. Silently exiting 1 after a half-finished
# extraction is the same class of failure as the original: the operator is left
# believing less happened than did.
board_touched=no
on_exit() {
    status=$?
    rm -f "$tarball"
    if [ "$status" -ne 0 ] && [ "$board_touched" = yes ]; then
        echo >&2
        echo "*** the board WAS modified and was NOT verified. Its tree may be a" >&2
        echo "*** mix of old and new files. Re-run with --check before trusting it." >&2
    fi
    exit "$status"
}
trap on_exit EXIT
trap 'exit 1' HUP INT TERM
board_touched=yes

# Extract over the top, then delete what the tarball did not contain — see
# board_apply_sync.sh, which does both and explains why the deletion half is
# necessary. It lives in its own file rather than inline here so that the only
# destructive code in the sync is lintable and testable; embedded in a quoted ssh
# argument it was neither, and `sh -n` on this script could not see inside it.
echo "applying on the board"
# shellcheck disable=SC2086  # SSH_OPTS is deliberately word-split
scp -O -q $SSH_OPTS "$here/board_apply_sync.sh" "$BOARD:/tmp/board_apply_sync.sh"
ssh_board "sh /tmp/board_apply_sync.sh '$REMOTE_SRC' '$PACKAGE' /tmp/benchctrl-src-sync.tgz"
ssh_board "rm -f /tmp/benchctrl-src-sync.tgz /tmp/board_apply_sync.sh"

# --- deploy/ scripts ------------------------------------------------------
# The launchers drifted independently of the python and caused their own
# regression, so they are synced by the same command. Copied into the staging
# dir, NOT installed: putting them in /usr/local/bin needs root, and this script
# deliberately never asks for it.
echo "sending deploy/ scripts to $BOARD:$REMOTE_DEPLOY"
# shellcheck disable=SC2086  # SSH_OPTS is deliberately word-split
scp -O -q $SSH_OPTS \
    "$here/benchctrl-fui" \
    "$here/benchctrl-kiosk" \
    "$here/install-fui.sh" \
    "$here/install-kiosk.sh" \
    "$here/board_sync_manifest.py" \
    "$here/board_apply_sync.sh" \
    "$BOARD:$REMOTE_DEPLOY/"

# --- prove it -------------------------------------------------------------
echo
if ! check_sync; then
    echo
    echo "sync ran but the trees still differ — investigate before trusting the board" >&2
    exit 1
fi

# Importability is a separate question from byte-equality: a tree can be a
# perfect copy and still fail to import under the board's python. Probed from
# /tmp because $HOME holds a 'benchctrl' runs directory that shadows the package.
#
# /usr/bin/python3 explicitly, not `python3` from PATH — that is what the agent
# unit and benchctrl-fui both run. Probing a different interpreter than production
# proves the wrong thing, which is the same mistake as verifying as the wrong user.
echo "checking it imports under the board's python"
ssh_board "cd /tmp && PYTHONPATH='$REMOTE_SRC' /usr/bin/python3 -c '
import benchctrl.dashboards.fui.server, benchctrl.dashboards.state, benchctrl.agent.main
print(\"  imports OK\")
'"

if [ "$restart" = yes ]; then
    # lightdm, not pkill. benchctrl-kiosk starts the dashboard once and then
    # `exec`s the browser, which replaces the shell and discards its cleanup
    # trap — so nothing supervises the FUI and nothing respawns it. Killing it
    # leaves the browser on a dead port showing its own error page, on a panel
    # with no keyboard. Restarting the session is what actually works.
    #
    # That needs root, which this script does not have and will not ask for, so
    # this prints the command rather than running it. The agent is untouched
    # either way: restarting it disconnects instruments mid-bench.
    cat <<EOF

--restart cannot do this unprivileged, and killing the FUI would not work anyway
(the kiosk session execs the browser, so nothing respawns the dashboard — you
would be left with a dead port on a keyboard-less panel). Run:

  ssh -t $BOARD 'sudo systemctl restart lightdm'
EOF
fi

cat <<EOF

Source on the board is current and verified.

Nothing was restarted. The FUI is still running the OLD code. The agent is a
mixed case, not simply old: its drivers import lazily, so a running agent picks
up new driver modules while keeping the old core it already loaded. Restart it
before trusting a driver change.

  whole panel:   ssh -t $BOARD 'sudo systemctl restart lightdm'
  the agent:     ssh -t $BOARD 'sudo systemctl restart benchctrl-agent'   # disarms the bench

If a launcher changed, it also needs installing (root):

  ssh -t $BOARD 'sudo install -m 0755 $REMOTE_DEPLOY/benchctrl-fui $REMOTE_DEPLOY/benchctrl-kiosk /usr/local/bin/'
EOF

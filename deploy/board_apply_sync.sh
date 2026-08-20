#!/bin/sh
# Apply a synced tarball to a source tree: extract it, then delete what it did
# not contain. Runs ON THE BOARD, invoked by sync-board.sh over ssh.
#
#     board_apply_sync.sh <src-dir> <package> <tarball>
#
# Why this is its own file rather than a string inside sync-board.sh
# ------------------------------------------------------------------
#
# This is the only destructive code in the sync. Embedded in a double-quoted ssh
# argument it was untestable and unlintable: `sh -n` on the outer script sees one
# string literal, so an unbalanced quote *inside* it passes, and shellcheck sees
# nothing at all. A syntax error here is discovered mid-deploy, against a tree
# that may already be half-updated.
#
# As a file it can be run against a temporary directory and checked — that the
# stale file is gone, that a vendored dependency beside the package survives, and
# that nothing above the root is touched. See tests/test_deploy_board_sync.py.
#
# What the delete sweep is for
# ----------------------------
#
# Extracting alone leaves a file you deleted in the repo still present on the
# board, which is how a removed module keeps running. So anything under the
# package that the tarball did not carry is stale and goes.
#
# The blast radius is the package subtree and nothing else. Under the board's
# src/ live vendored dependencies — serial, usb, pyvisa, pyvisa_py — which the
# board needs because it has no pip, which are not in the repo, and which are
# therefore exactly what an unscoped sweep would delete. It would take the agent,
# and the bench, down. Hence: $package is validated as a single plain name, both
# `find` calls are rooted at it, and everything runs after `cd <src-dir>`.

set -eu

if [ "$#" -ne 3 ]; then
    echo "usage: $0 <src-dir> <package> <tarball>" >&2
    exit 2
fi

src=$1
package=$2
tarball=$3

# The safety boundary, enforced here as well as in sync-board.sh and in
# board_sync_manifest.py. Three checks of the same thing is deliberate: this file
# deletes things, and it must be safe to run even if invoked directly.
case "$package" in
"" | . | .. | -* | */*)
    echo "package must be a single plain directory name, not '$package'" >&2
    exit 2
    ;;
esac

[ -d "$src" ] || {
    echo "no such directory: $src" >&2
    exit 2
}
[ -f "$tarball" ] || {
    echo "no such tarball: $tarball" >&2
    exit 2
}

CDPATH= cd -- "$src"

# A tarball that does not contain the package would make every existing file
# "stale" and delete the whole package. Check before extracting, not after.
if ! tar tzf "$tarball" | grep -q "^$package/"; then
    echo "tarball contains no '$package/' — refusing to sweep with it" >&2
    exit 2
fi

tar xzf "$tarball"

shipped=$(mktemp)
present=$(mktemp)
stale=$(mktemp)
trap 'rm -f "$shipped" "$present" "$stale"' EXIT HUP INT TERM

tar tzf "$tarball" | sed 's:/$::' | sort -u >"$shipped"

# Purge __pycache__ before listing, so bytecode never shows up as "stale".
#
# Not because it is importable: a .pyc inside __pycache__ is NOT importable once
# its source is gone — sourceless import needs the legacy location beside the
# package, which IS manifested and IS swept by the comm below. This is deleted
# because bytecode from the board's 3.13 cannot match the workstation's, so
# keeping it would be permanent noise in the comparison.
#
# Failure is tolerated but reported. Silently leaving stale bytecode is the kind
# of quiet best-effort that hid the original drift.
find "$package" -name '__pycache__' -type d -prune -exec rm -rf {} + ||
    echo "  warning: could not remove all __pycache__ dirs (stale bytecode may remain)"

# No -L: a symlinked directory is -type l, so it is neither listed nor descended.
# board_sync_manifest.py skips symlinks for the same reason, so the two halves of
# the sync agree about what exists.
find "$package" -type f | sort -u >"$present"

comm -13 "$shipped" "$present" >"$stale" || true

if [ -s "$stale" ]; then
    echo "removing files no longer in the repo:"
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        # A path that escaped the package subtree means the listing is not what
        # this script assumes, so stop rather than delete on a guess. `find`
        # rooted at "$package" cannot produce one; this catches the case where
        # that stops being true.
        case "$f" in
        "$package"/*) ;;
        *)
            echo "  refusing to delete outside '$package': $f" >&2
            exit 1
            ;;
        esac
        echo "  $f"
        rm -f -- "$f"
    done <"$stale"
fi

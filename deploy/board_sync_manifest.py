#!/usr/bin/env python3
"""Content manifest for a deployed source tree — the answer to "is the board current?"

Run on either end of a sync with the same arguments and the output is
byte-comparable:

    python3 board_sync_manifest.py /home/arduino/benchctrl-1.2.0/src --only benchctrl

Each line is ``<sha256>  <relative/path>``, sorted by path. Sorting matters:
without it the manifest depends on directory iteration order, which differs
between filesystems, and two identical trees would compare unequal on different
hardware.

``--only`` is not optional in practice. The board's ``src/`` holds **vendored
dependencies** beside our package — ``serial``, ``usb``, ``pyvisa``, ``pyvisa_py``
— because the board has no pip and no network install path. They are supposed to
be there and they are not in this repo. Manifesting the whole directory reports
120-odd phantom differences that bury the real ones, and a sync that treated
"absent from the repo" as "delete it" would remove the board's only copy of
pyserial and take the agent down with it. So the comparison is scoped to exactly
the subtree we own.

Why this exists
---------------

The FUI shipped to the board twice from a staging directory that was older than
the repo, so ``install-fui.sh`` faithfully installed pre-fix software both times.
The launchers were missing their fullscreen flags and the deployed ``state.py``
had none of the observer-role check — while every surface-level check said the
display was fine, because the display *was* running, just not the version that
had been reviewed.

What made that possible is that "deployed" was a claim nobody could check. Files
went over one at a time with scp and nothing compared the result to anything. So
this module is not a convenience wrapper around a copy: the copy is the easy
half. Establishing that the board holds exactly the tree you think it does is
the part that was missing, and a sync that cannot prove it is the failure mode
it is meant to prevent.

Deliberately no third-party imports and no benchctrl imports. It runs under the
board's bare system python (3.13, no pip, ``EXTERNALLY-MANAGED``), and it has to
work on a tree whose ``benchctrl`` package is *broken* — diagnosing a bad deploy
is exactly when you need it, so it cannot depend on the thing it is measuring.

Compatible with Python 3.9 (the project floor) through 3.13 (the board). CI does
not lint or type-check ``deploy/``, so the only thing enforcing that floor is
``tests/test_deploy_board_sync.py`` loading this module by path on every leg of
the version matrix. Keep it that way — it is the only check there is.
"""

from __future__ import annotations

import argparse
import hashlib
import sys
from collections.abc import Iterator
from pathlib import Path

#: Directory names never shipped or compared, matched at any depth.
#:
#: ``__pycache__`` is here because bytecode compiled by one interpreter never
#: matches another's — the board is on 3.13 and the workstation on 3.12, so
#: including it would report permanent, unfixable drift. The sync *deletes* these
#: on the board rather than transferring them.
#:
#: Note what this does **not** protect against, because the distinction is easy
#: to get backwards: a ``.pyc`` inside ``__pycache__`` is **not** importable once
#: its source is gone (PEP 3147 sourceless imports require the legacy location).
#: A ``.pyc`` sitting *beside* the package — ``benchctrl/panel.pyc`` — **is**.
#: Verified both ways on 3.12. That is why legacy-location bytecode is
#: deliberately manifested rather than excluded: it is the form that can keep a
#: module you deleted running, so ``--check`` has to be able to see it.
EXCLUDE_DIRS = frozenset(
    {
        "__pycache__",
        ".git",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
    }
)

#: Directory *name suffixes* never shipped or compared. Distinct from
#: :py:data:`EXCLUDE_DIRS` because the name varies (``benchctrl.egg-info``), and
#: distinct from a filename check because the files inside it — ``PKG-INFO`` and
#: friends — need excluding too, which a suffix match on the leaf name misses.
EXCLUDE_DIR_SUFFIXES = (".egg-info",)

#: Read size for hashing. The largest file in the tree is a few hundred KB, so
#: this is about not holding a surprise in memory rather than about speed.
CHUNK = 65536


def _excluded(rel: Path) -> bool:
    """Whether ``rel`` is filtered out of the manifest.

    Only caches are filtered. Everything else under the scoped subtree is
    manifested, including bytecode in the legacy location and ``.so`` files —
    anything there that is not in the repo is drift, and the point of this
    module is to say so rather than to curate.
    """
    for part in rel.parts[:-1]:
        if part in EXCLUDE_DIRS or part.endswith(EXCLUDE_DIR_SUFFIXES):
            return True
    return False


def _check_only(only: str) -> None:
    """Reject an ``only`` that is not a single plain directory name.

    This is the whole safety boundary, so it is enforced rather than documented.
    ``only`` reaches a ``tar`` argument and a ``find`` root on the board, where
    the sweep deletes what the tarball did not contain. ``--only .`` would make
    that sweep the unscoped sweep the scoping exists to prevent — deleting the
    board's vendored pyserial, which is its only copy — and ``--only ..`` walks
    out of the tree entirely (``relative_to`` is lexical, so it emits ``../``
    paths quite happily rather than raising).
    """
    parts = Path(only).parts
    if (
        not only
        or len(parts) != 1
        or parts[0] in (".", "..")
        or only.startswith("-")
        or "/" in only
    ):
        raise ValueError(
            f"--only must be a single plain directory name, not {only!r}"
        )


def iter_files(root: Path, only: str | None = None) -> Iterator[Path]:
    """Every file to be manifested, as paths relative to ``root``, sorted.

    ``only`` restricts the walk to one subdirectory while keeping paths relative
    to ``root``, so a manifest of ``src --only benchctrl`` is directly comparable
    to one taken the same way on the board. See the module docstring for why
    scoping is load-bearing rather than a convenience, and :py:func:`_check_only`
    for what it refuses.

    Symlinked *files* are followed to their content rather than recorded as
    links: the board's tree is a plain copy, and a manifest that compared link
    targets would report drift between two trees holding identical bytes.

    Symlinked *directories* are not descended. They would otherwise walk outside
    the scoped subtree — a link inside ``benchctrl/`` pointing at
    ``/home/arduino/benchctrl`` would pull the live agent's run artifacts into
    the manifest, reported as ``extra``, which is the category the operator is
    told means "this will be deleted". The sweep on the board uses ``find``
    without ``-L`` and so cannot remove them, making it permanent unresolvable
    drift; a false report nobody can act on trains the operator to ignore the
    tool, which is worse than the tool not existing.
    """
    if only is not None:
        _check_only(only)
    base = root if only is None else root / only
    if not base.is_dir() or base.is_symlink():
        # An empty manifest compares as "in sync" against another empty one, so
        # a mistyped --only must be loud rather than reassuring.
        raise FileNotFoundError(f"no such subtree: {base}")
    rels = []
    stack = [base]
    while stack:
        current = stack.pop()
        for path in sorted(current.iterdir(), key=lambda p: p.name):
            rel = path.relative_to(root)
            if path.is_symlink():
                # Neither followed nor recorded. Recording it would compare a
                # link target across two machines whose absolute paths differ.
                continue
            if path.is_dir():
                if not _excluded(rel / "x"):
                    stack.append(path)
                continue
            if path.is_file() and not _excluded(rel):
                rels.append(rel)
    # Sort on the POSIX string, not the Path. Path sorting is component-wise,
    # which orders "a/b.py" and "a-b.py" differently from the string form, and
    # the two ends of a sync must agree exactly.
    return iter(sorted(rels, key=lambda p: p.as_posix()))


def hash_file(path: Path) -> str:
    digest = hashlib.sha256()
    with open(path, "rb") as fh:
        while True:
            block = fh.read(CHUNK)
            if not block:
                break
            digest.update(block)
    return digest.hexdigest()


def manifest(root: Path, only: str | None = None) -> str:
    """The full manifest for ``root`` as text, one ``sha256  path`` line each.

    Ends with a trailing newline when non-empty so the output diffs cleanly and
    does not provoke "\\ No newline at end of file" noise.
    """
    lines = [
        f"{hash_file(root / rel)}  {rel.as_posix()}" for rel in iter_files(root, only)
    ]
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def compare(local: str, remote: str) -> dict:
    """Drift between two manifests, from the local tree's point of view.

    Returns ``changed`` / ``missing`` / ``extra`` lists of paths, sorted, plus
    ``in_sync``. ``extra`` is what exists on the remote and not locally — the
    category that held the deleted ``panel.py``, and the reason this returns
    three lists rather than a boolean.
    """

    def parse(text: str) -> dict:
        out: dict = {}
        for raw in text.splitlines():
            # rstrip the newline only. A full strip() would make a path with
            # trailing whitespace collide with the same path without it, which
            # is drift silently reported as agreement.
            line = raw.rstrip("\r\n")
            if not line.strip():
                continue
            # partition on the two-space separator, not split(): a path may
            # contain spaces — including consecutive ones — and a hex digest
            # never does, so everything after the first double space is path.
            digest, _, path = line.partition("  ")
            if not path:
                raise ValueError(f"unparseable manifest line: {line!r}")
            # A truncated or garbled transfer is far more likely than a genuinely
            # odd path, and it must not read as agreement. Same argument as the
            # ValueError above: garbage in cannot look like a match.
            if len(digest) != 64 or any(c not in "0123456789abcdef" for c in digest):
                raise ValueError(f"not a sha256 digest: {digest!r}")
            if path in out:
                raise ValueError(f"duplicate path in manifest: {path!r}")
            out[path] = digest
        return out

    lhs, rhs = parse(local), parse(remote)
    changed = sorted(p for p in lhs if p in rhs and lhs[p] != rhs[p])
    missing = sorted(p for p in lhs if p not in rhs)
    extra = sorted(p for p in rhs if p not in lhs)
    return {
        "changed": changed,
        "missing": missing,
        "extra": extra,
        "in_sync": not (changed or missing or extra),
    }


def main(argv: list[str] | None = None) -> int:
    # __doc__ is None under -OO, and a crash in the tool you reach for when a
    # deploy is broken is the last thing wanted.
    summary = (__doc__ or "manifest a deployed source tree").splitlines()[0]
    parser = argparse.ArgumentParser(description=summary)
    parser.add_argument("root", help="directory to manifest")
    parser.add_argument(
        "--only",
        default=None,
        metavar="SUBDIR",
        help="restrict to this subdirectory (paths stay relative to root). "
        "Use this to exclude vendored dependencies living beside the package. "
        "Must be a single plain directory name.",
    )
    args = parser.parse_args(argv)

    root = Path(args.root)
    if not root.is_dir():
        print(f"not a directory: {root}", file=sys.stderr)
        return 2
    try:
        sys.stdout.write(manifest(root, args.only))
    except (FileNotFoundError, ValueError) as exc:
        print(str(exc), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

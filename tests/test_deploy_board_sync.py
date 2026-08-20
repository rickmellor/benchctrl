"""The board-sync manifest: does it actually detect drift?

Why these tests exist
---------------------

The FUI reached the board twice as pre-review software. Each individual ``scp``
succeeded, so nothing looked wrong; the tree as a whole was mixed. The launchers
had lost their fullscreen flags and the deployed ``state.py`` had none of the
observer-role check, while the panel was up and every surface check passed.

``deploy/board_sync_manifest.py`` exists to make "the board is current" a
checkable claim rather than an assumption. So the thing worth testing is not that
it can hash a file — it is that each *category* of drift that actually bit is one
it reports:

- a file whose contents changed (the launchers, ``state.py``)
- a file the board never received (a new module)
- a file deleted from the repo but still present on the board (``panel.py``, and
  its bytecode, which python will happily import after the source is gone)

A manifest that missed the third category would have called the board in sync
while it was still running a module we had deleted.

The module is loaded by path because ``deploy/`` is not a package — it is a
directory of scripts that run on a board with no benchctrl installed.
"""

from __future__ import annotations

import importlib.util
import pathlib
import shutil
import subprocess
import sys

import pytest

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"
MANIFEST_PY = DEPLOY / "board_sync_manifest.py"


def load_module():
    """Import board_sync_manifest.py by path, since deploy/ is not a package."""
    spec = importlib.util.spec_from_file_location("board_sync_manifest", MANIFEST_PY)
    assert spec and spec.loader, f"cannot load {MANIFEST_PY}"
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


bsm = load_module()


@pytest.fixture
def tree(tmp_path):
    """A minimal source tree, plus a helper to write into it."""

    def write(root: str, rel: str, text: str) -> pathlib.Path:
        p = tmp_path / root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return p

    return tmp_path, write


# --------------------------------------------------------------- the manifest


def test_two_identical_trees_produce_identical_manifests(tree):
    root, write = tree
    for side in ("local", "remote"):
        write(side, "benchctrl/state.py", "x = 1\n")
        write(side, "benchctrl/fui/static/fui.js", "// js\n")
    a = bsm.manifest(root / "local")
    b = bsm.manifest(root / "remote")
    assert a == b
    assert a.count("\n") == 2, f"expected 2 entries, got: {a!r}"


def test_the_manifest_is_sorted_regardless_of_creation_order(tree):
    """Unsorted output would depend on filesystem iteration order.

    Two identical trees on different hardware would then compare unequal, which
    is the one thing this file must never do — a false drift report trains the
    operator to ignore it.
    """
    root, write = tree
    for rel in ("benchctrl/z.py", "benchctrl/a.py", "benchctrl/m/n.py"):
        write("local", rel, "pass\n")
    for rel in ("benchctrl/m/n.py", "benchctrl/a.py", "benchctrl/z.py"):
        write("remote", rel, "pass\n")
    paths = [line.split("  ", 1)[1] for line in bsm.manifest(root / "local").splitlines()]
    assert paths == sorted(paths)
    assert bsm.manifest(root / "local") == bsm.manifest(root / "remote")


def test_caches_are_excluded_but_importable_bytecode_is_not(tree):
    """``__pycache__`` is excluded; a legacy-location ``.pyc`` is manifested.

    The asymmetry is the point, and an earlier version of this test had it
    backwards — it excluded both and called that "bytecode handled".

    Bytecode compiled by the board's 3.13 never matches the workstation's 3.12,
    so manifesting ``__pycache__`` would report permanent, unfixable drift. But a
    ``__pycache__`` ``.pyc`` is *not* importable once its source is gone: PEP 3147
    sourceless imports require the legacy location. Verified both ways on 3.12 —
    with ``pkg/mod.py`` deleted, ``pkg/mod.pyc`` imports and returns its value,
    while ``pkg/__pycache__/mod.cpython-312.pyc`` raises ImportError.

    So ``benchctrl/panel.pyc`` is the form that can keep a deleted module running
    on the board. Excluding it would make ``--check`` structurally unable to see
    the one bytecode case that matters.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("local", "benchctrl/__pycache__/state.cpython-313.pyc", "bytecode\n")
    # A non-.pyc file inside __pycache__: without it, a suffix-based exclusion
    # and a directory-based one are indistinguishable, and gutting the directory
    # check still passed.
    write("local", "benchctrl/__pycache__/leftover.json", "{}\n")
    write("local", "benchctrl.egg-info/PKG-INFO", "Name: benchctrl\n")
    write("local", "benchctrl/panel.pyc", "importable bytecode\n")
    out = bsm.manifest(root / "local")
    assert "__pycache__" not in out
    assert "egg-info" not in out
    assert "state.py" in out
    assert "benchctrl/panel.pyc" in out, "importable bytecode must be visible"


# ------------------------------------------------------- the three drift kinds


def test_a_changed_file_is_reported(tree):
    """The launchers and state.py case: same path, different content."""
    root, write = tree
    write("local", "benchctrl/state.py", "observer_denied = None\n")
    write("remote", "benchctrl/state.py", "# no observer check at all\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["in_sync"] is False
    assert r["changed"] == ["benchctrl/state.py"]
    assert r["missing"] == [] and r["extra"] == []


def test_a_file_the_board_never_got_is_reported(tree):
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("local", "benchctrl/fui/view.py", "y = 2\n")
    write("remote", "benchctrl/state.py", "x = 1\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["in_sync"] is False
    assert r["missing"] == ["benchctrl/fui/view.py"]


def test_a_file_deleted_from_the_repo_but_still_on_the_board_is_reported(tree):
    """The panel.py case, and the reason compare() returns three lists.

    A boolean "do the shipped files match" would call this in sync. The board
    would still hold an importable module we deleted, which is how the removed
    Streamlit panel could have kept running behind the FUI.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("remote", "benchctrl/state.py", "x = 1\n")
    write("remote", "benchctrl/panel.py", "import streamlit\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["in_sync"] is False
    assert r["extra"] == ["benchctrl/panel.py"]
    assert r["changed"] == [] and r["missing"] == []


def test_all_three_kinds_at_once(tree):
    """The realistic case — the board had all three simultaneously."""
    root, write = tree
    write("local", "benchctrl/state.py", "new\n")
    write("remote", "benchctrl/state.py", "old\n")
    write("local", "benchctrl/fui/view.py", "new file\n")
    write("remote", "benchctrl/panel.py", "deleted upstream\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r == {
        "changed": ["benchctrl/state.py"],
        "missing": ["benchctrl/fui/view.py"],
        "extra": ["benchctrl/panel.py"],
        "in_sync": False,
    }


def test_identical_trees_are_in_sync(tree):
    root, write = tree
    for side in ("local", "remote"):
        write(side, "benchctrl/state.py", "x = 1\n")
        write(side, "benchctrl/fui/view.py", "y = 2\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["in_sync"] is True
    assert (r["changed"], r["missing"], r["extra"]) == ([], [], [])


def test_a_change_past_the_first_read_block_is_still_detected(tree):
    """Hashing must consume the whole file, not just its first chunk.

    Every other file in this test module is a few bytes, so a hash that read one
    64 KB block and stopped would pass all of them. The real tree's largest file
    is fui.js, and the governor this project cares about lives near its top while
    the renderer is below — a truncated hash would call two different versions of
    it identical.
    """
    root, write = tree
    head = "// identical prologue\n" * 8000  # comfortably past CHUNK
    assert len(head.encode()) > bsm.CHUNK, "prologue must exceed one read block"
    write("local", "benchctrl/fui/static/fui.js", head + "const TIER = 'FULL';\n")
    write("remote", "benchctrl/fui/static/fui.js", head + "const TIER = 'MINIMAL';\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["changed"] == ["benchctrl/fui/static/fui.js"], r


def test_a_path_containing_spaces_still_parses(tree):
    """The separator is two spaces and paths may contain them.

    Splitting on the first space would truncate the path and report phantom
    drift. None of our files have spaces today, which is exactly why nothing
    else would catch this.
    """
    root, write = tree
    write("local", "benchctrl/static/a file.css", "body {}\n")
    write("remote", "benchctrl/static/a file.css", "body {}\n")
    r = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert r["in_sync"] is True, r


def test_a_garbled_digest_raises_rather_than_comparing_as_drift():
    """A truncated transfer must be loud, not reported as a changed file.

    This runs over ssh, so a partial read is more likely than a genuinely odd
    manifest. Without the length/charset check, a truncated digest simply differs
    from the real one and lands in ``changed`` — indistinguishable from actual
    drift, which sends the operator to diff a file that is in fact identical. And
    a *shortened* digest on both ends would compare equal and read as in sync.
    """
    good = "a" * 64
    for bad in ("abc", "z" * 64, "A" * 64, "a" * 63, "a" * 65):
        with pytest.raises(ValueError, match="not a sha256 digest"):
            bsm.compare(f"{bad}  benchctrl/state.py\n", f"{good}  benchctrl/state.py\n")
    # The valid form still works, or the check above would be vacuous.
    assert bsm.compare(f"{good}  x\n", f"{good}  x\n")["in_sync"] is True


def test_a_duplicated_path_raises_rather_than_silently_winning():
    """Two lines for one path means the manifest is not what it claims.

    Last-write-wins would hide one of the two digests, so a comparison could
    report in sync on the strength of a line that was overwritten.
    """
    d = "a" * 64
    e = "b" * 64
    with pytest.raises(ValueError, match="duplicate path"):
        bsm.compare(f"{d}  benchctrl/state.py\n{e}  benchctrl/state.py\n", "")


def test_trailing_whitespace_in_a_path_is_not_collapsed():
    """``strip()`` would make "state.py " and "state.py" the same path.

    That is drift silently reported as agreement — the one outcome this module
    must never produce. Only the line ending is stripped.
    """
    d = "a" * 64
    r = bsm.compare(f"{d}  benchctrl/state.py \n", f"{d}  benchctrl/state.py\n")
    assert r["in_sync"] is False
    assert r["missing"] == ["benchctrl/state.py "]
    assert r["extra"] == ["benchctrl/state.py"]


def test_a_path_with_consecutive_spaces_parses_on_the_first_separator():
    """The separator is two spaces and the path may itself contain two.

    Everything after the first double-space is path, so a name like "a  b.css"
    round-trips. Splitting on the last separator, or on any separator, would
    truncate it and report phantom drift.
    """
    d = "a" * 64
    r = bsm.compare(f"{d}  benchctrl/a  b.css\n", f"{d}  benchctrl/a  b.css\n")
    assert r["in_sync"] is True, r


def test_an_unparseable_manifest_line_raises_rather_than_reads_as_in_sync():
    """Garbage in must not look like agreement.

    A manifest that silently ignored a malformed line would return in_sync for
    an empty parse of a truncated transfer — the check reporting success
    precisely when it failed.
    """
    with pytest.raises(ValueError):
        bsm.compare("not-a-manifest-line\n", "")


# ------------------------------------------------- scoping to the subtree we own
# The board's src/ holds vendored serial/usb/pyvisa beside our package, because it
# has no pip. Running --check against the real board without scoping reported ~120
# phantom differences — and put the board's only copy of pyserial in scope of the
# sync's stale-file delete sweep, which would have taken the agent down. The tests
# above all passed while that was true, so these exist specifically for it.


def test_only_excludes_siblings_of_the_package(tree):
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("local", "serial/__init__.py", "# vendored pyserial\n")
    write("local", "pyvisa/highlevel.py", "# vendored pyvisa\n")
    out = bsm.manifest(root / "local", "benchctrl")
    assert "benchctrl/state.py" in out
    assert "serial" not in out and "pyvisa" not in out
    assert out.count("\n") == 1, f"expected exactly one entry, got: {out!r}"


def test_only_keeps_paths_relative_to_root_not_to_the_subtree(tree):
    """Both ends must agree on the path text, or every file reads as drift.

    The board is manifested at ``.../src --only benchctrl`` and the workstation at
    ``<repo>/src --only benchctrl``; the roots differ, so paths have to be
    root-relative. Emitting them relative to the subtree instead ("state.py")
    would still compare equal — which is why this asserts the prefix rather than
    just comparing two manifests.
    """
    root, write = tree
    write("local", "benchctrl/fui/view.py", "y = 2\n")
    out = bsm.manifest(root / "local", "benchctrl")
    assert out.split("  ", 1)[1].strip() == "benchctrl/fui/view.py"


def test_vendored_deps_are_not_reported_as_extra_on_the_board(tree):
    """The failure this scoping prevents, stated as the operator sees it.

    Unscoped, these land in ``extra`` — "present on the board, absent from the
    repo" — which is the same category as the deleted ``panel.py`` the sync is
    built to delete. Being in that list is not cosmetic; it is a deletion.
    """
    root, write = tree
    for side in ("local", "remote"):
        write(side, "benchctrl/state.py", "x = 1\n")
    write("remote", "serial/__init__.py", "# the board's only pyserial\n")
    write("remote", "usb/core.py", "# vendored pyusb\n")

    unscoped = bsm.compare(bsm.manifest(root / "local"), bsm.manifest(root / "remote"))
    assert unscoped["extra"] == ["serial/__init__.py", "usb/core.py"], unscoped

    scoped = bsm.compare(
        bsm.manifest(root / "local", "benchctrl"),
        bsm.manifest(root / "remote", "benchctrl"),
    )
    assert scoped["in_sync"] is True, scoped
    assert scoped["extra"] == []


def test_only_still_reports_drift_inside_the_subtree(tree):
    """Scoping must narrow the walk, not soften the comparison."""
    root, write = tree
    write("local", "benchctrl/state.py", "observer_denied = None\n")
    write("remote", "benchctrl/state.py", "# no observer check\n")
    write("remote", "serial/__init__.py", "# vendored\n")
    r = bsm.compare(
        bsm.manifest(root / "local", "benchctrl"),
        bsm.manifest(root / "remote", "benchctrl"),
    )
    assert r["changed"] == ["benchctrl/state.py"], r


def test_a_missing_subtree_raises_rather_than_returning_an_empty_manifest(tree):
    """Two empty manifests compare as in_sync.

    So a mistyped --only, or a board whose package genuinely is not there, must
    raise. Returning "" would report IN SYNC — 0 files identical on both ends —
    which is the check confirming the deploy it failed to look at.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    with pytest.raises(FileNotFoundError):
        bsm.manifest(root / "local", "bnechctrl")
    # And the consequence being guarded against, made explicit.
    assert bsm.compare("", "")["in_sync"] is True


def test_only_must_be_a_single_plain_directory_name(tree):
    """The safety boundary, enforced rather than documented.

    ``only`` becomes a ``tar`` argument and a ``find`` root on the board, and that
    find feeds a delete sweep. ``--only .`` makes the sweep the unscoped sweep the
    scoping exists to prevent — deleting the board's vendored pyserial, its only
    copy. ``--only ..`` walks out of the tree entirely: ``relative_to`` is lexical,
    so it emits ``../OUTSIDE.py`` paths rather than raising. Both were accepted
    before this check existed; verified against a temp tree.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("local", "OUTSIDE.py", "not ours\n")
    for bad in (".", "..", "../local", "benchctrl/fui", "-rf", "", "a/b"):
        with pytest.raises(ValueError, match="single plain directory name"):
            bsm.manifest(root / "local", bad)


def test_only_pointing_at_a_file_is_an_error(tree):
    """A file is not a subtree; iterating one yields nothing, silently."""
    root, write = tree
    write("local", "state.py", "x = 1\n")
    with pytest.raises(FileNotFoundError):
        bsm.manifest(root / "local", "state.py")


def test_only_pointing_at_a_symlinked_directory_is_an_error(tree):
    """Otherwise the scope is whatever the link points at, which defeats it."""
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    (root / "elsewhere").mkdir()
    (root / "local" / "linked").symlink_to(root / "elsewhere", target_is_directory=True)
    with pytest.raises(FileNotFoundError):
        bsm.manifest(root / "local", "linked")


def test_a_symlinked_directory_inside_the_package_is_not_descended(tree):
    """It would walk outside the scoped subtree and report unfixable drift.

    A link inside ``benchctrl/`` pointing at ``/home/arduino/benchctrl`` — the
    agent's live runs directory — would pull run artifacts into the manifest as
    ``extra``, the category the operator is told means "this will be deleted". The
    sweep uses ``find`` without ``-L`` and so can never remove them, making it
    permanent drift nobody can resolve, which teaches the operator to ignore the
    tool.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("runs", "artifact.json", "{}\n")
    (root / "local" / "benchctrl" / "linked").symlink_to(
        root / "runs", target_is_directory=True
    )
    out = bsm.manifest(root / "local", "benchctrl")
    assert "artifact.json" not in out
    assert out.count("\n") == 1, out


# ------------------------------------------------------------ the CLI contract
# The shell script shells out to this, so its exit codes and stdout are an
# interface, not an implementation detail.


def test_the_cli_prints_a_manifest_and_exits_zero(tree):
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    out = subprocess.run(
        [sys.executable, str(MANIFEST_PY), str(root / "local")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == bsm.manifest(root / "local")
    assert "benchctrl/state.py" in out.stdout


def test_the_cli_fails_loudly_on_a_missing_directory(tmp_path):
    """A typo'd path must not print an empty manifest and exit 0.

    Two empty manifests compare as in_sync, so a silent failure here would
    report a board as current without having looked at it.
    """
    out = subprocess.run(
        [sys.executable, str(MANIFEST_PY), str(tmp_path / "nope")],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode != 0
    assert out.stdout == ""
    assert "not a directory" in out.stderr


def test_the_cli_scopes_with_only(tree):
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    write("local", "serial/__init__.py", "# vendored\n")
    out = subprocess.run(
        [sys.executable, str(MANIFEST_PY), str(root / "local"), "--only", "benchctrl"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, out.stderr
    assert out.stdout == bsm.manifest(root / "local", "benchctrl")
    assert "serial" not in out.stdout


def test_the_cli_exits_nonzero_on_a_mistyped_only(tree):
    """The shell script branches on this exit code.

    If a typo'd --only exited 0 with empty stdout, check_sync would compare two
    empty manifests and print IN SYNC.
    """
    root, write = tree
    write("local", "benchctrl/state.py", "x = 1\n")
    out = subprocess.run(
        [sys.executable, str(MANIFEST_PY), str(root / "local"), "--only", "bnechctrl"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode != 0
    assert out.stdout == ""
    assert "no such subtree" in out.stderr


def test_both_shell_scripts_pass_shell_syntax_check():
    """A syntax error in either is discovered mid-deploy otherwise.

    ``board_apply_sync.sh`` is the reason this is worth having. While that code
    was a double-quoted string inside an ssh argument, ``sh -n`` on the outer
    script could not see into it — to the outer parser it was one string literal,
    so an unbalanced quote *inside* passed this check while failing on the board,
    against a tree already half-updated. Being a real file, it is now parsed.
    """
    for name in ("sync-board.sh", "board_apply_sync.sh"):
        out = subprocess.run(
            ["sh", "-n", str(DEPLOY / name)], capture_output=True, text=True, timeout=60
        )
        assert out.returncode == 0, f"{name}: {out.stderr}"


# ------------------------------------------------- the sweep, actually executed
# board_apply_sync.sh is the only destructive code in the sync. These run it for
# real against a temporary tree laid out like the board's: our package beside the
# vendored dependencies it must not touch.

APPLY = DEPLOY / "board_apply_sync.sh"


@pytest.fixture
def board(tmp_path):
    """A fake board ``src/``: our package plus vendored deps, and a tarball maker.

    Mirrors the real layout, because the layout is the hazard — ``serial`` and
    ``usb`` sit *beside* ``benchctrl`` under the same ``src/``, are absent from
    the repo, and are the board's only copies.
    """
    src = tmp_path / "src"
    (src / "benchctrl" / "fui").mkdir(parents=True)
    (src / "benchctrl" / "__init__.py").write_text("")
    (src / "benchctrl" / "state.py").write_text("old\n")
    (src / "benchctrl" / "panel.py").write_text("import streamlit\n")
    (src / "benchctrl" / "fui" / "view.py").write_text("old view\n")
    # The vendored deps. The board has no pip; these are its only copies.
    (src / "serial").mkdir()
    (src / "serial" / "__init__.py").write_text("# pyserial 3.5\n")
    (src / "usb").mkdir()
    (src / "usb" / "core.py").write_text("# pyusb\n")
    (src / "typing_extensions.py").write_text("# vendored\n")
    # Something above src/, standing in for /home/arduino/benchctrl.
    (tmp_path / "PRECIOUS_RUN_ARTIFACT").write_text("do not delete\n")

    def make_tarball(files: dict, package: str = "benchctrl") -> pathlib.Path:
        """Build a tarball of ``package`` containing exactly ``files``."""
        stage = tmp_path / "stage"
        if stage.exists():
            shutil.rmtree(stage)
        for rel, text in files.items():
            p = stage / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            p.write_text(text)
        tgz = tmp_path / "sync.tgz"
        subprocess.run(
            ["tar", "czf", str(tgz), "-C", str(stage), package],
            check=True,
            timeout=60,
        )
        return tgz

    def apply(tgz, package="benchctrl", src_dir=None):
        return subprocess.run(
            ["sh", str(APPLY), str(src_dir or src), package, str(tgz)],
            capture_output=True,
            text=True,
            timeout=60,
        )

    return src, tmp_path, make_tarball, apply


def test_the_sweep_deletes_a_file_removed_from_the_repo(board):
    """panel.py, the case that motivated the whole thing."""
    src, _, make_tarball, apply = board
    tgz = make_tarball(
        {
            "benchctrl/__init__.py": "",
            "benchctrl/state.py": "new\n",
            "benchctrl/fui/view.py": "new view\n",
        }
    )
    out = apply(tgz)
    assert out.returncode == 0, out.stderr
    assert not (src / "benchctrl" / "panel.py").exists()
    assert (src / "benchctrl" / "state.py").read_text() == "new\n"
    assert "panel.py" in out.stdout


def test_the_sweep_does_not_touch_the_vendored_dependencies(board):
    """The failure that would take the agent, and the bench, down.

    An unscoped sweep sees serial/ and usb/ as "present on the board, absent from
    the tarball" — the same category as panel.py above — and deletes the board's
    only pyserial.
    """
    src, _, make_tarball, apply = board
    tgz = make_tarball({"benchctrl/__init__.py": "", "benchctrl/state.py": "new\n"})
    out = apply(tgz)
    assert out.returncode == 0, out.stderr
    assert (src / "serial" / "__init__.py").read_text() == "# pyserial 3.5\n"
    assert (src / "usb" / "core.py").exists()
    assert (src / "typing_extensions.py").exists()
    assert "serial" not in out.stdout and "usb" not in out.stdout


def test_the_sweep_does_not_touch_anything_above_the_source_root(board):
    """Standing in for /home/arduino/benchctrl, the agent's live runs directory."""
    src, top, make_tarball, apply = board
    tgz = make_tarball({"benchctrl/__init__.py": ""})
    out = apply(tgz)
    assert out.returncode == 0, out.stderr
    assert (top / "PRECIOUS_RUN_ARTIFACT").read_text() == "do not delete\n"


def test_the_sweep_removes_stale_bytecode_in_the_importable_location(board):
    """A legacy-location .pyc keeps a deleted module importable.

    Verified on 3.12: with ``pkg/mod.py`` gone, ``pkg/mod.pyc`` still imports,
    while a ``pkg/__pycache__/mod.*.pyc`` does not (PEP 3147 sourceless imports
    require the legacy location). So this is the form that can keep removed code
    running on the board, and the sweep has to remove it.
    """
    src, _, make_tarball, apply = board
    (src / "benchctrl" / "panel.pyc").write_bytes(b"\x00\x00stale bytecode")
    (src / "benchctrl" / "__pycache__").mkdir()
    (src / "benchctrl" / "__pycache__" / "state.cpython-313.pyc").write_bytes(b"x")
    tgz = make_tarball({"benchctrl/__init__.py": ""})
    out = apply(tgz)
    assert out.returncode == 0, out.stderr
    assert not (src / "benchctrl" / "panel.pyc").exists()
    assert not (src / "benchctrl" / "__pycache__").exists()


def test_the_sweep_refuses_a_package_name_that_would_widen_it(board):
    """``.`` is the mutation that deletes the vendored deps.

    Rooting the sweep at ``.`` under src/ makes every vendored file "stale". This
    is refused rather than documented because it is the whole safety boundary,
    and because board_apply_sync.sh can be run directly.
    """
    src, _, make_tarball, apply = board
    tgz = make_tarball({"benchctrl/__init__.py": ""})
    for bad in (".", "..", "../src", "benchctrl/fui", "-rf", ""):
        out = apply(tgz, package=bad)
        assert out.returncode == 2, f"package={bad!r} was accepted: {out.stdout}"
        assert (src / "serial" / "__init__.py").exists(), f"package={bad!r} deleted it"


def test_the_package_guard_is_load_bearing_on_its_own(board, tmp_path):
    """Separates the package check from the tarball-contents check.

    With a tarball built the usual way, ``--only .`` is also caught by "the
    tarball contains no ``./``", so removing the package validation entirely still
    passed — the two guards were indistinguishable, which is the same trap that
    once hid a gutted cache-exclusion check.

    A tarball rooted at ``.`` has ``./`` in its listing, so it gets past the
    contents check. Measured against a mutant with the package validation removed:
    the sweep then reports ``./serial/__init__.py`` and deletes the board's only
    pyserial. This is the tarball shape that proves the guard earns its place.
    """
    src, top, _, apply = board
    stage = top / "dotstage"
    (stage / "benchctrl").mkdir(parents=True)
    (stage / "benchctrl" / "state.py").write_text("new\n")
    tgz = top / "dot.tgz"
    subprocess.run(
        ["tar", "czf", str(tgz), "-C", str(stage), "."], check=True, timeout=60
    )
    listing = subprocess.run(
        ["tar", "tzf", str(tgz)], capture_output=True, text=True, timeout=60
    ).stdout
    assert "./" in listing, "premise: this tarball must get past the contents check"

    out = apply(tgz, package=".")
    assert out.returncode == 2, out.stdout
    assert (src / "serial" / "__init__.py").exists(), "the board's only pyserial"
    assert (src / "usb" / "core.py").exists()


def test_a_tarball_without_the_package_is_refused_before_extracting(board):
    """Otherwise every existing file is "stale" and the package is wiped.

    An empty or wrong-package tarball is a plausible outcome of a truncated
    transfer, and the sweep's logic turns it into "delete everything".
    """
    src, tmp, _, apply = board
    stage = tmp / "empty-stage"
    (stage / "somethingelse").mkdir(parents=True)
    (stage / "somethingelse" / "x").write_text("x")
    tgz = tmp / "wrong.tgz"
    subprocess.run(
        ["tar", "czf", str(tgz), "-C", str(stage), "somethingelse"],
        check=True,
        timeout=60,
    )
    out = apply(tgz)
    assert out.returncode == 2, out.stdout
    assert (src / "benchctrl" / "state.py").exists(), "the package was swept away"
    assert (src / "benchctrl" / "panel.py").exists()


def test_the_sweep_leaves_a_symlinked_directory_alone(board):
    """The manifest skips symlinks, so the sweep must not delete through them.

    A symlink inside the package pointing at the agent's runs directory must
    neither be followed into (deleting artifacts) nor be reported as drift the
    operator cannot resolve.
    """
    src, top, make_tarball, apply = board
    outside = top / "runs"
    outside.mkdir()
    (outside / "artifact.json").write_text("{}\n")
    (src / "benchctrl" / "linked").symlink_to(outside, target_is_directory=True)
    tgz = make_tarball({"benchctrl/__init__.py": ""})
    out = apply(tgz)
    assert out.returncode == 0, out.stderr
    assert (outside / "artifact.json").read_text() == "{}\n"


def test_applying_twice_is_idempotent(board):
    """A re-run after an interrupted sync must converge, not keep deleting."""
    src, _, make_tarball, apply = board
    tgz = make_tarball({"benchctrl/__init__.py": "", "benchctrl/state.py": "new\n"})
    first = apply(tgz)
    assert first.returncode == 0, first.stderr
    second = apply(tgz)
    assert second.returncode == 0, second.stderr
    assert "removing files" not in second.stdout
    assert (src / "benchctrl" / "state.py").read_text() == "new\n"


# ------------------------------------------- the caller's handling of a failure
# The library raises loudly on a bad --only, but the shell has to act on that.
# check_sync is called from an `if`, which suspends set -e, so an unchecked
# failure there would leave an empty variable and carry on — and two empty
# manifests compare as in_sync. That is the check reporting a board as current
# without having looked at it.


def test_the_sync_script_checks_the_status_of_both_manifest_commands():
    text = (DEPLOY / "sync-board.sh").read_text(encoding="utf-8")
    assert 'if ! local_out=$(python3' in text, "local manifest status unchecked"
    assert "if ! remote_out=$(ssh_board" in text, "remote manifest status unchecked"
    assert 'if ! scp -O -q $SSH_OPTS "$MANIFEST"' in text, "checker scp unchecked"
    # And the independent backstop, which closes the route regardless of which
    # command failed to report it.
    assert "! -s /tmp/sync-local.manifest" in text
    assert "! -s /tmp/sync-remote.manifest" in text


def test_the_sync_script_validates_the_two_variables_that_bound_the_damage():
    """PACKAGE and REMOTE_SRC are the safety boundary, so they are enforced."""
    text = (DEPLOY / "sync-board.sh").read_text(encoding="utf-8")
    # PACKAGE rejected as ., .., a path, or an option.
    assert '"" | . | .. | -* | */*)' in text
    # The preflight guard must test $PACKAGE, not a hardcoded name: a PACKAGE
    # that exists nowhere would otherwise reach the manifest step, where both
    # ends fail and two empty manifests read as in sync.
    assert '[ ! -d "$LOCAL_SRC/$PACKAGE" ]' in text
    assert '[ ! -d "$LOCAL_SRC/benchctrl" ]' not in text
    # REMOTE_SRC=/home/arduino would extract into, and sweep, the agent's live
    # runs directory.
    assert "*/src) ;;" in text


def test_the_sync_script_does_not_claim_the_kiosk_respawns_the_fui():
    """benchctrl-kiosk execs the browser, discarding its cleanup trap.

    So nothing supervises the dashboard and pkill leaves a dead port on a
    keyboard-less panel. An earlier draft told the operator the session would
    respawn it, which is worse than saying nothing.
    """
    kiosk = (DEPLOY / "benchctrl-kiosk").read_text(encoding="utf-8")
    assert "exec" in kiosk, "premise changed: the kiosk no longer execs"
    # Check what the operator is *told*, not the comments explaining why. Strip
    # comment lines first, or this passes/fails on the rationale rather than the
    # advice — the whole point is that the printed guidance is accurate.
    sync = (DEPLOY / "sync-board.sh").read_text(encoding="utf-8")
    printed = "\n".join(
        ln for ln in sync.splitlines() if not ln.lstrip().startswith("#")
    )
    # "nothing respawns" is the correct statement; "the session respawns it" was
    # the false one. Match the claim, not the word.
    assert "kiosk session respawns" not in printed
    assert "respawns it" not in printed
    # And it must not pkill the FUI, which is what left a dead port on a panel
    # with no keyboard: there is nothing to bring it back.
    assert "pkill" not in printed, "killing the FUI leaves nothing to restart it"
    assert "systemctl restart lightdm" in printed


# ------------------------------------------------------------ the CLI contract

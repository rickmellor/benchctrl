"""Tests for tools/plancheck.py, the build-derived plan status.

Every gate is tested against the same clean baseline with ONE perturbation,
and each test asserts that the set of gates that fire is exactly the gate
under test. A check that two gates both catch can't prove either works (a
mutant of one survives on the other), so each fixture is built so that only
its own gate rejects it.
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
_spec = importlib.util.spec_from_file_location("plancheck", ROOT / "tools" / "plancheck.py")
assert _spec and _spec.loader
plancheck = importlib.util.module_from_spec(_spec)
sys.modules["plancheck"] = plancheck
_spec.loader.exec_module(plancheck)

pytestmark = pytest.mark.plan("CORE-P0.1")

PLAN = """\
# Test plan

## P0 <!-- plan:phase id=T-P0 state=active -->

- **T-P0.1** A tested thing. `verify: test`
- **T-P0.2** A measured thing, with a long description that wraps
  onto a second line. `verify: evidence`
- **T-P0.3** Not started yet. `verify: test`

## P1 <!-- plan:phase id=T-P1 state=planned -->

- **T-P1.1** Later. `verify: test+evidence`
"""

TESTS = """\
import pytest

pytestmark = []

@pytest.mark.plan("T-P0.1")
def test_one():
    pass

class TestGroup:
    @pytest.mark.plan("T-P0.1")
    def test_two(self):
        pass

def test_unrelated():
    pass
"""

JUNIT = """\
<?xml version="1.0" encoding="utf-8"?>
<testsuites><testsuite name="pytest">
  <testcase classname="tests.test_fake" name="test_one"/>
  <testcase classname="tests.test_fake.TestGroup" name="test_two"/>
  <testcase classname="tests.test_fake" name="test_unrelated"/>
</testsuite></testsuites>
"""

EVIDENCE = """\
---
id: T-P0.2
commit: {commit}
date: 2026-10-14
rig: benchpi
operator_role: bench-engineer
measurements:
  trip_latency_s: 15.42
  trips: 1
---

Notes.
"""


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True, text=True
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo holding the clean baseline: every gate passes."""
    _git(tmp_path, "init", "-q")
    _git(tmp_path, "config", "user.email", "ci@example.invalid")
    _git(tmp_path, "config", "user.name", "ci")
    (tmp_path / "docs" / "plan" / "evidence").mkdir(parents=True)
    (tmp_path / "docs" / "plan" / "plan.md").write_text(PLAN)
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_fake.py").write_text(TESTS)
    (tmp_path / "junit.xml").write_text(JUNIT)
    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "base")
    commit = _git(tmp_path, "rev-parse", "HEAD")
    (tmp_path / "docs" / "plan" / "evidence" / "T-P0.2.md").write_text(
        EVIDENCE.format(commit=commit)
    )
    return tmp_path


def run(repo: Path, *extra: str, git: bool = True) -> tuple[int, dict]:
    out = repo / "status.json"
    argv = [
        "--repo",
        str(repo),
        "--plan",
        str(repo / "docs" / "plan" / "plan.md"),
        "--tests",
        str(repo / "tests"),
        "--junit",
        str(repo / "junit.xml"),
        "--evidence",
        str(repo / "docs" / "plan" / "evidence"),
        "--out-json",
        str(out),
        *extra,
    ]
    if not git:
        argv.append("--no-git")
    rc = plancheck.main(argv)
    return rc, (json.loads(out.read_text()) if out.exists() else {})


def gates(status: dict) -> set[str]:
    return {f["gate"] for f in status["failures"]}


def edit(path: Path, old: str, new: str) -> None:
    text = path.read_text()
    assert old in text, f"perturbation target {old!r} not in {path.name}"
    path.write_text(text.replace(old, new, 1))


# ---------------------------------------------------------------- baseline


def test_clean_baseline_passes_every_gate(repo: Path) -> None:
    rc, st = run(repo)
    assert rc == 0
    assert st["failures"] == [] and st["warnings"] == []
    items = st["items"]
    assert items["T-P0.1"]["status"] == "done"
    assert items["T-P0.2"]["status"] == "done"
    assert items["T-P0.3"]["status"] == "todo"
    assert items["T-P1.1"]["status"] == "todo"
    # Both T-P0.1 references were found in the results, and the unrelated
    # case was counted as collected but not referenced.
    assert st["counts"]["referenced_tests"] == 2
    assert st["counts"]["referenced_tests_found_in_results"] == 2
    assert st["counts"]["junit_cases"] == 3


def test_wrapped_item_text_keeps_its_verify_tag(repo: Path) -> None:
    plan = plancheck.parse_plans([repo / "docs" / "plan" / "plan.md"])
    assert plan.items["T-P0.2"].verify == "evidence"
    assert "second line" in plan.items["T-P0.2"].text


# ---------------------------------------------------------------- statuses


def test_skipped_test_is_not_done(repo: Path) -> None:
    edit(repo / "junit.xml", 'name="test_one"/>', 'name="test_one"><skipped/></testcase>')
    rc, st = run(repo)
    assert st["items"]["T-P0.1"]["status"] == "in-progress"
    assert any("skipped" in r for r in st["items"]["T-P0.1"]["reasons"])
    assert rc == 0  # in-progress in an active phase is not a gate failure


def test_failed_test_is_not_done(repo: Path) -> None:
    edit(repo / "junit.xml", 'name="test_one"/>', 'name="test_one"><failure/></testcase>')
    _, st = run(repo)
    assert st["items"]["T-P0.1"]["status"] == "in-progress"


def test_referenced_test_absent_from_results_is_not_done(repo: Path) -> None:
    edit(repo / "junit.xml", '<testcase classname="tests.test_fake" name="test_one"/>', "")
    _, st = run(repo)
    assert st["items"]["T-P0.1"]["status"] == "in-progress"
    assert st["counts"]["referenced_tests_found_in_results"] == 1
    assert st["counts"]["referenced_tests"] == 2


def test_parametrized_cases_all_count(repo: Path) -> None:
    edit(
        repo / "junit.xml",
        '<testcase classname="tests.test_fake" name="test_one"/>',
        '<testcase classname="tests.test_fake" name="test_one[a]"/>'
        '<testcase classname="tests.test_fake" name="test_one[b]"><skipped/></testcase>',
    )
    _, st = run(repo)
    assert st["items"]["T-P0.1"]["status"] == "in-progress"


def test_same_name_in_another_module_does_not_satisfy_a_reference(repo: Path) -> None:
    edit(
        repo / "junit.xml",
        'classname="tests.test_fake" name="test_one"',
        'classname="tests.test_other" name="test_one"',
    )
    _, st = run(repo)
    assert st["items"]["T-P0.1"]["status"] == "in-progress"


def test_junit_property_reference_counts_without_a_static_one(repo: Path) -> None:
    edit(
        repo / "junit.xml",
        '<testcase classname="tests.test_fake" name="test_unrelated"/>',
        '<testcase classname="tests.test_fake" name="test_unrelated"><properties>'
        '<property name="plan" value="T-P0.3"/></properties></testcase>',
    )
    _, st = run(repo)
    assert st["items"]["T-P0.3"]["status"] == "done"


def test_evidence_item_without_evidence_is_todo(repo: Path) -> None:
    (repo / "docs" / "plan" / "evidence" / "T-P0.2.md").unlink()
    _, st = run(repo)
    assert st["items"]["T-P0.2"]["status"] == "todo"


def test_test_plus_evidence_needs_both(repo: Path) -> None:
    edit(
        repo / "tests" / "test_fake.py",
        "def test_unrelated",
        '@pytest.mark.plan("T-P1.1")\ndef test_unrelated',
    )
    _, st = run(repo)
    assert st["items"]["T-P1.1"]["status"] == "in-progress"
    assert any("evidence" in r for r in st["items"]["T-P1.1"]["reasons"])


def test_go_reference(repo: Path) -> None:
    (repo / "tests" / "x_test.go").write_text(
        "package x\n\n// plan: T-P0.3\nfunc TestGo(t *testing.T) {}\n"
    )
    edit(
        repo / "junit.xml",
        "</testsuite>",
        '<testcase classname="example.com/x" name="TestGo"/></testsuite>',
    )
    _, st = run(repo)
    assert st["items"]["T-P0.3"]["status"] == "done"


# ---------------------------------------------------------------- gate 1


def test_gate_unknown_id_from_a_test(repo: Path) -> None:
    edit(
        repo / "tests" / "test_fake.py",
        'plan("T-P0.1")\ndef test_one',
        'plan("T-P0.9")\ndef test_one',
    )
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"unknown-id"}


def test_gate_unknown_id_from_evidence(repo: Path) -> None:
    ev = repo / "docs" / "plan" / "evidence"
    text = (ev / "T-P0.2.md").read_text().replace("id: T-P0.2", "id: T-P0.8")
    (ev / "T-P0.8.md").write_text(text)
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"unknown-id"}


def test_gate_duplicate_id(repo: Path) -> None:
    edit(repo / "docs" / "plan" / "plan.md", "- **T-P1.1** Later.", "- **T-P0.3** Again.")
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"unknown-id"}
    assert [f["id"] for f in st["failures"]] == ["T-P0.3"]


# ---------------------------------------------------------------- gate 2


def test_gate_closed_phase_with_an_unfinished_item(repo: Path) -> None:
    # Close P0 with everything done except T-P0.3, which nothing references.
    edit(repo / "docs" / "plan" / "plan.md", "id=T-P0 state=active", "id=T-P0 state=closed")
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"closed-phase"}
    assert [f["id"] for f in st["failures"]] == ["T-P0.3"]


def test_closed_phase_with_everything_done_passes(repo: Path) -> None:
    edit(repo / "docs" / "plan" / "plan.md", "id=T-P0 state=active", "id=T-P0 state=closed")
    edit(repo / "docs" / "plan" / "plan.md", "- **T-P0.3** Not started yet. `verify: test`\n", "")
    rc, st = run(repo)
    assert rc == 0, st["failures"]


# ---------------------------------------------------------------- gate 3


def test_gate_regressed_against_base(repo: Path) -> None:
    _, base = run(repo)
    assert base["items"]["T-P0.1"]["status"] == "done"
    (repo / "base.json").write_text(json.dumps(base))
    edit(repo / "junit.xml", 'name="test_one"/>', 'name="test_one"><failure/></testcase>')
    rc, st = run(repo, "--base-status", str(repo / "base.json"))
    assert rc == 1
    assert gates(st) == {"regressed"}
    assert st["items"]["T-P0.1"]["status"] == "regressed"
    assert {"id": "T-P0.1", "from": "done", "to": "regressed"} in st["transitions"]


def test_progress_against_base_is_a_transition_not_a_failure(repo: Path) -> None:
    (repo / "docs" / "plan" / "evidence" / "T-P0.2.md").rename(repo / "held.md")
    _, base = run(repo)
    (repo / "base.json").write_text(json.dumps(base))
    (repo / "held.md").rename(repo / "docs" / "plan" / "evidence" / "T-P0.2.md")
    rc, st = run(repo, "--base-status", str(repo / "base.json"))
    assert rc == 0
    assert {"id": "T-P0.2", "from": "todo", "to": "done"} in st["transitions"]


# ---------------------------------------------------------------- gate 4


@pytest.mark.parametrize(
    ("old", "new", "needle"),
    [
        ("operator_role: bench-engineer", "operator_role: someone@example.com", "email"),
        ("rig: benchpi\n", "", "rig"),
        ("  trips: 1", "  trips: one", "not a number"),
        ("date: 2026-10-14", "date: 14/10/2026", "YYYY-MM-DD"),
        ("id: T-P0.2", "id: T-P0.1", "does not match"),
    ],
)
def test_gate_bad_evidence_fields(repo: Path, old: str, new: str, needle: str) -> None:
    edit(repo / "docs" / "plan" / "evidence" / "T-P0.2.md", old, new)
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"bad-evidence"}
    assert any(needle in f["detail"] for f in st["failures"]), st["failures"]
    # The item it would have completed is not done either.
    assert st["items"]["T-P0.2"]["status"] == "in-progress"


def test_gate_bad_evidence_commit_not_an_ancestor(repo: Path) -> None:
    _git(repo, "checkout", "-q", "-b", "side")
    (repo / "side.txt").write_text("x")
    _git(repo, "add", "side.txt")
    _git(repo, "commit", "-q", "-m", "side")
    side = _git(repo, "rev-parse", "HEAD")
    _git(repo, "checkout", "-q", "-")
    ev = repo / "docs" / "plan" / "evidence" / "T-P0.2.md"
    text = ev.read_text()
    head = _git(repo, "rev-parse", "HEAD")
    ev.write_text(text.replace(head, side))
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"bad-evidence"}
    assert "ancestor" in st["failures"][0]["detail"]
    # --no-git skips only that check.
    rc, _ = run(repo, git=False)
    assert rc == 0


def test_gate_bad_evidence_no_front_matter(repo: Path) -> None:
    (repo / "docs" / "plan" / "evidence" / "T-P0.2.md").write_text("just notes\n")
    rc, st = run(repo)
    assert rc == 1
    assert gates(st) == {"bad-evidence"}


# ---------------------------------------------------------------- gate 5


def test_warning_untested_item_edited_in_this_change(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evidence")
    base = _git(repo, "rev-parse", "HEAD")
    edit(repo / "docs" / "plan" / "plan.md", "Not started yet.", "Not started yet, reworded.")
    _git(repo, "commit", "-q", "-am", "reword")
    rc, st = run(repo, "--diff-base", base)
    assert rc == 0  # a warning never fails the build
    assert st["failures"] == []
    assert [w["id"] for w in st["warnings"]] == ["T-P0.3"]


def test_no_warning_for_untouched_or_planned_items(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "evidence")
    base = _git(repo, "rev-parse", "HEAD")
    edit(repo / "docs" / "plan" / "plan.md", "- **T-P1.1** Later.", "- **T-P1.1** Later still.")
    _git(repo, "commit", "-q", "-am", "reword planned")
    _, st = run(repo, "--diff-base", base)
    assert st["warnings"] == []


# ---------------------------------------------------------------- inputs


def test_bad_plan_input_is_exit_2(repo: Path) -> None:
    edit(repo / "docs" / "plan" / "plan.md", "state=planned", "state=someday")
    rc, _ = run(repo)
    assert rc == 2


def test_item_without_verify_tag_is_exit_2(repo: Path) -> None:
    edit(repo / "docs" / "plan" / "plan.md", "Later. `verify: test+evidence`", "Later.")
    rc, _ = run(repo)
    assert rc == 2


def test_markdown_report_shows_ran_vs_referenced(repo: Path) -> None:
    md = repo / "status.md"
    run(repo, "--out-md", str(md))
    text = md.read_text()
    assert "**2 of 2**" in text
    assert "### T-P0 (active): 2/3 done" in text
    assert "No base status" in text


# ---------------------------------------------------------------- the repo's own plan


@pytest.mark.plan("CORE-P0.3")
def test_repo_plan_parses_and_every_reference_names_a_real_item() -> None:
    """The real plan and the real test tree: no junit, so nothing is done, but
    gate 1 (unknown or duplicate ID) still applies to every reference."""
    plan_file = ROOT / "docs" / "plan" / "pro-core.md"
    plan = plancheck.parse_plans([plan_file])
    refs = plancheck.scan_tests([ROOT / "tests"], ROOT)
    report = plancheck.evaluate(plan, refs, [], {})
    assert [f for f in report.failures if f["gate"] == "unknown-id"] == []
    assert plan.phases["CORE-P0"].state in ("active", "closed")
    assert any(r.plan_id == "CORE-P0.1" for r in refs)


def test_item_belongs_to_the_phase_its_id_names(repo: Path) -> None:
    # A T-P1 item written under the P0 heading still belongs to T-P1, so
    # closing P0 doesn't demand it.
    edit(
        repo / "docs" / "plan" / "plan.md",
        "- **T-P0.3** Not started yet.",
        "- **T-P1.2** Not started yet.",
    )
    edit(repo / "docs" / "plan" / "plan.md", "id=T-P0 state=active", "id=T-P0 state=closed")
    rc, st = run(repo)
    assert rc == 0, st["failures"]
    assert st["items"]["T-P1.2"]["phase"] == "T-P1"


def test_item_whose_phase_has_no_marker_is_exit_2(repo: Path) -> None:
    edit(repo / "docs" / "plan" / "plan.md", "- **T-P0.3** Not", "- **T-P7.1** Not")
    rc, _ = run(repo)
    assert rc == 2

#!/usr/bin/env python3
"""Derive where the code stands against a plan file, and gate the build on it.

The plan is a reviewed Markdown file next to the code (``docs/plan/*.md``).
Every deliverable is one ID'd item, and every phase carries a machine-readable
marker::

    ## P0 · Core hardening   <!-- plan:phase id=CORE-P0 state=active -->

    - **CORE-P0.1** Per-owner deadman ... `verify: test`

Nothing in that file says whether an item is *done*. That is derived here, from
the tests that reference the item (``@pytest.mark.plan("CORE-P0.1")``, or
``// plan: PRO-P1b.4`` above a Go ``func TestX``), the junit results of this
build, and evidence files (``docs/plan/evidence/<ID>.md``) for what CI cannot
measure. A status that is hand-written goes stale; one the build derives cannot.

Status per item:

``todo``        nothing references it
``in-progress`` referenced, but a referenced test failed, was **skipped**, or is
                missing from the results, or required evidence is missing
``done``        every referenced test ran and passed, and any evidence it needs
                is present and well-formed
``regressed``   ``done`` in the base status and not any more

A skipped or uncollected test is never a pass. An ``importorskip`` that never
succeeds once removed 30 cases from this repo's CI without failing anything;
the report therefore prints how many referenced tests actually *ran*.

Gates — any one fails the build (exit 1):

1. ``unknown-id``     a test or evidence file names an ID the plan does not
                      have, or the plan defines an ID twice
2. ``closed-phase``   a phase marked ``state=closed`` has an item that is not
                      ``done``: closing a phase is a claim, and the build checks it
3. ``regressed``      an item was ``done`` on the base and is not now
4. ``bad-evidence``   an evidence file is missing a field, has non-numeric
                      measurements, names a commit that is not an ancestor of
                      HEAD, or has an ``operator_role`` that looks like an email

Plus one warning that never fails the build:

5. ``untested-item``  an ``active`` phase's ``verify: test`` item has no test
                      reference and this change edits that item's text

Stdlib only, so it runs under benchctrl's own constraints and on any CI image.
Exit codes: 0 clean, 1 a gate failed, 2 bad input.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from pathlib import Path

SCHEMA = "benchctrl.plan-status/1"

ID_RE = r"[A-Z][A-Z0-9]*-P[0-9][0-9a-z]*\.[0-9]+"
_ID_ONLY = re.compile(rf"^{ID_RE}$")
_PHASE_RE = re.compile(r"<!--\s*plan:phase\s+([^>]*?)\s*-->")
_ATTR_RE = re.compile(r"(\w+)=(\S+)")
_ITEM_RE = re.compile(rf"^\s*[-*]\s+\*\*({ID_RE})\*\*(.*)$")
_VERIFY_RE = re.compile(r"`verify:\s*([a-z+]+)`")
_GO_PLAN_RE = re.compile(r"^\s*//\s*plan:\s*(.+?)\s*$")
_GO_FUNC_RE = re.compile(r"^\s*func\s+(Test\w+)\s*\(")
_EMAIL_RE = re.compile(r"[^@\s]+@[^@\s]+\.[^@\s]+")
_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")
_COMMIT_RE = re.compile(r"^[0-9a-f]{7,40}$")

PHASE_STATES = ("planned", "active", "closed")
VERIFY_KINDS = ("test", "evidence", "test+evidence")
EVIDENCE_FIELDS = ("id", "commit", "date", "rig", "operator_role", "measurements")

TODO, IN_PROGRESS, DONE, REGRESSED = "todo", "in-progress", "done", "regressed"


class InputError(Exception):
    """The inputs cannot be read as a plan, results or evidence at all."""


# --------------------------------------------------------------------------
# The plan
# --------------------------------------------------------------------------


@dataclass
class Item:
    id: str
    phase: str
    verify: str
    text: str
    plan_file: str
    line: int

    @property
    def needs_test(self) -> bool:
        return "test" in self.verify

    @property
    def needs_evidence(self) -> bool:
        return "evidence" in self.verify


@dataclass
class Phase:
    id: str
    state: str
    plan_file: str
    line: int


@dataclass
class Plan:
    phases: dict[str, Phase] = field(default_factory=dict)
    items: dict[str, Item] = field(default_factory=dict)
    duplicates: list[str] = field(default_factory=list)


def parse_plans(paths: list[Path]) -> Plan:
    plan = Plan()
    for path in paths:
        _parse_plan(path, plan)
    _check_plan(plan)
    return plan


def _parse_plan(path: Path, plan: Plan) -> None:
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        raise InputError(f"cannot read plan {path}: {exc}") from exc
    item: Item | None = None
    for n, line in enumerate(lines, 1):
        marker = _PHASE_RE.search(line)
        if marker:
            attrs = dict(_ATTR_RE.findall(marker.group(1)))
            pid, state = attrs.get("id"), attrs.get("state")
            if not pid or state not in PHASE_STATES:
                raise InputError(
                    f"{path}:{n}: phase marker needs id= and state= one of "
                    f"{'|'.join(PHASE_STATES)}, got {marker.group(0)!r}"
                )
            plan.phases[pid] = Phase(pid, state, str(path), n)
            item = None
            continue
        m = _ITEM_RE.match(line)
        if m:
            # An item belongs to the phase its ID names (CORE-P1b.3 -> CORE-P1b),
            # wherever it sits: one heading can hold both repos' items.
            item = Item(
                m.group(1), m.group(1).rsplit(".", 1)[0], "", m.group(2).strip(), str(path), n
            )
            if item.id in plan.items:
                plan.duplicates.append(item.id)
            else:
                plan.items[item.id] = item
            _take_verify(item, m.group(2))
            continue
        # An item's text may wrap onto indented continuation lines, and the
        # `verify:` tag is often on the last of them.
        if item is not None and line.strip() and line[:1].isspace():
            item.text = f"{item.text} {line.strip()}"
            _take_verify(item, line)
        elif item is not None and line.strip() and not line[:1].isspace():
            item = None


def _check_plan(plan: Plan) -> None:
    for it in plan.items.values():
        if it.phase not in plan.phases:
            raise InputError(
                f"{it.plan_file}:{it.line}: {it.id} has no phase marker for {it.phase}"
            )
        if it.verify not in VERIFY_KINDS:
            raise InputError(
                f"{it.plan_file}:{it.line}: {it.id} needs a `verify: {'|'.join(VERIFY_KINDS)}` tag"
            )


def _take_verify(item: Item, text: str) -> None:
    v = _VERIFY_RE.search(text)
    if v and not item.verify:
        item.verify = v.group(1)


# --------------------------------------------------------------------------
# Static references in the test sources
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class TestRef:
    plan_id: str
    lang: str  # "py" | "go"
    module: str  # dotted path for Python, package dir for Go
    name: str  # test function name, or Class::method
    source: str  # file:line, for messages

    @property
    def func(self) -> str:
        return self.name.rsplit("::", 1)[-1]


def scan_tests(roots: list[Path], repo: Path) -> list[TestRef]:
    refs: list[TestRef] = []
    for root in roots:
        files = [root] if root.is_file() else sorted(root.rglob("*"))
        for path in files:
            if path.suffix == ".py" and path.name.startswith("test"):
                refs.extend(_scan_python(path, repo))
            elif path.name.endswith("_test.go"):
                refs.extend(_scan_go(path, repo))
    return refs


def _plan_ids_from_decorators(decorators: list[ast.expr]) -> list[str]:
    ids: list[str] = []
    for dec in decorators:
        if not isinstance(dec, ast.Call):
            continue
        fn = dec.func
        if isinstance(fn, ast.Attribute) and fn.attr == "plan":
            ids.extend(_constant_strings(dec.args))
    return ids


def _constant_strings(nodes: list[ast.expr]) -> list[str]:
    return [n.value for n in nodes if isinstance(n, ast.Constant) and isinstance(n.value, str)]


def _module_pytestmark(tree: ast.Module) -> list[str]:
    ids: list[str] = []
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets):
            continue
        values = node.value.elts if isinstance(node.value, (ast.List, ast.Tuple)) else [node.value]
        ids.extend(_plan_ids_from_decorators(list(values)))
    return ids


def _scan_python(path: Path, repo: Path) -> list[TestRef]:
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    except (OSError, SyntaxError) as exc:
        raise InputError(f"cannot parse {path}: {exc}") from exc
    rel = _rel(path, repo)
    module = rel[:-3].replace("/", ".")
    module_ids = _module_pytestmark(tree)
    refs: list[TestRef] = []

    def add(ids: list[str], name: str, line: int) -> None:
        for pid in ids:
            refs.append(TestRef(pid, "py", module, name, f"{rel}:{line}"))

    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name.startswith(
            "test"
        ):
            add(module_ids + _plan_ids_from_decorators(node.decorator_list), node.name, node.lineno)
        elif isinstance(node, ast.ClassDef) and node.name.startswith("Test"):
            class_ids = module_ids + _plan_ids_from_decorators(node.decorator_list)
            for sub in node.body:
                if isinstance(sub, (ast.FunctionDef, ast.AsyncFunctionDef)) and sub.name.startswith(
                    "test"
                ):
                    ids = class_ids + _plan_ids_from_decorators(sub.decorator_list)
                    add(ids, f"{node.name}::{sub.name}", sub.lineno)
    return refs


def _scan_go(path: Path, repo: Path) -> list[TestRef]:
    rel = _rel(path, repo)
    package = str(Path(rel).parent)
    refs: list[TestRef] = []
    pending: list[str] = []
    for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        m = _GO_PLAN_RE.match(line)
        if m:
            pending.extend(p.strip() for p in m.group(1).split(","))
            continue
        f = _GO_FUNC_RE.match(line)
        if f and pending:
            for pid in pending:
                refs.append(TestRef(pid, "go", package, f.group(1), f"{rel}:{n}"))
        if not line.strip().startswith("//"):
            pending = []
    return refs


def _rel(path: Path, repo: Path) -> str:
    try:
        return path.resolve().relative_to(repo.resolve()).as_posix()
    except ValueError:
        return path.as_posix()


# --------------------------------------------------------------------------
# Results
# --------------------------------------------------------------------------

PASSED, FAILED, SKIPPED = "passed", "failed", "skipped"


@dataclass
class Case:
    classname: str
    name: str
    outcome: str
    plan_ids: tuple[str, ...]

    @property
    def func(self) -> str:
        return self.name.split("[", 1)[0]


def parse_junit(paths: list[Path]) -> list[Case]:
    cases: list[Case] = []
    for path in paths:
        try:
            root = ET.parse(path).getroot()
        except (OSError, ET.ParseError) as exc:
            raise InputError(f"cannot read junit {path}: {exc}") from exc
        for tc in root.iter("testcase"):
            if tc.find("failure") is not None or tc.find("error") is not None:
                outcome = FAILED
            elif tc.find("skipped") is not None:
                outcome = SKIPPED
            else:
                outcome = PASSED
            ids = tuple(
                p.get("value", "")
                for p in tc.iter("property")
                if p.get("name") == "plan" and p.get("value")
            )
            cases.append(Case(tc.get("classname", ""), tc.get("name", ""), outcome, ids))
    return cases


def _matches(ref: TestRef, case: Case) -> bool:
    if case.func != ref.func:
        return False
    if ref.lang == "go":
        return True
    # pytest's classname is "tests.test_x" or "tests.test_x.TestClass".
    expected = ref.module
    if "::" in ref.name:
        expected = f"{ref.module}.{ref.name.split('::', 1)[0]}"
    return case.classname == expected or case.classname.endswith("." + expected)


# --------------------------------------------------------------------------
# Evidence
# --------------------------------------------------------------------------


@dataclass
class Evidence:
    path: str
    fields: dict
    problems: list[str]


def parse_front_matter(text: str) -> dict:
    """The YAML subset evidence uses: ``key: value`` and one level of nesting."""
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        raise ValueError("no front matter (the file must start with ---)")
    out: dict = {}
    current: str | None = None
    for line in lines[1:]:
        if line.strip() == "---":
            return out
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        if line[:1].isspace():
            if current is None or not isinstance(out.get(current), dict):
                raise ValueError(f"indented line outside a mapping: {line!r}")
            key, sep, value = line.strip().partition(":")
            if not sep:
                raise ValueError(f"not a key: value line: {line!r}")
            out[current][key.strip()] = _scalar(value.strip())
            continue
        key, sep, value = line.partition(":")
        if not sep:
            raise ValueError(f"not a key: value line: {line!r}")
        key, value = key.strip(), value.strip()
        if value:
            out[key] = _scalar(value)
            current = None
        else:
            out[key] = {}
            current = key
    raise ValueError("front matter is not closed with ---")


def _scalar(value: str):
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
        return value[1:-1]
    for cast in (int, float):
        try:
            return cast(value)
        except ValueError:
            pass
    return value


def load_evidence(directory: Path | None, repo: Path, *, check_git: bool) -> dict[str, Evidence]:
    found: dict[str, Evidence] = {}
    if directory is None or not directory.is_dir():
        return found
    for path in sorted(directory.glob("*.md")):
        if path.name.lower() == "readme.md":
            continue
        stem = path.stem
        problems: list[str] = []
        try:
            fm = parse_front_matter(path.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            found[stem] = Evidence(_rel(path, repo), {}, [str(exc)])
            continue
        for name in EVIDENCE_FIELDS:
            if fm.get(name) in (None, "", {}):
                problems.append(f"missing field {name!r}")
        if fm.get("id") and fm["id"] != stem:
            problems.append(f"id {fm['id']!r} does not match the file name {stem!r}")
        date = fm.get("date")
        if date and not _DATE_RE.match(str(date)):
            problems.append(f"date {date!r} is not YYYY-MM-DD")
        role = str(fm.get("operator_role", ""))
        if role and _EMAIL_RE.search(role):
            problems.append(
                "operator_role looks like an email address; evidence records a "
                "role label, never a person"
            )
        measurements = fm.get("measurements")
        if isinstance(measurements, dict):
            for key, value in measurements.items():
                if isinstance(value, bool) or not isinstance(value, (int, float)):
                    problems.append(f"measurement {key!r} is not a number: {value!r}")
        elif measurements not in (None, "", {}):
            problems.append("measurements must be a mapping of name: number")
        commit = str(fm.get("commit", ""))
        if commit and not _COMMIT_RE.match(commit):
            problems.append(f"commit {commit!r} is not a hex commit id")
        elif commit and check_git and not _is_ancestor(repo, commit):
            problems.append(f"commit {commit} is not an ancestor of HEAD")
        found[stem] = Evidence(_rel(path, repo), fm, problems)
    return found


def _is_ancestor(repo: Path, commit: str) -> bool:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "merge-base", "--is-ancestor", commit, "HEAD"],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError:
        return False
    return proc.returncode == 0


# --------------------------------------------------------------------------
# Changed item text (gate 5)
# --------------------------------------------------------------------------


def changed_item_ids(repo: Path, base: str, plan_files: list[Path]) -> set[str]:
    try:
        proc = subprocess.run(
            ["git", "-C", str(repo), "diff", "-U0", f"{base}...HEAD", "--"]
            + [str(p) for p in plan_files],
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise InputError(f"git diff failed: {exc}") from exc
    if proc.returncode != 0:
        raise InputError(f"git diff against {base!r} failed: {proc.stderr.strip()}")
    ids: set[str] = set()
    for line in proc.stdout.splitlines():
        if line.startswith("+") and not line.startswith("+++"):
            m = _ITEM_RE.match(line[1:])
            if m:
                ids.add(m.group(1))
    return ids


# --------------------------------------------------------------------------
# Evaluation
# --------------------------------------------------------------------------


@dataclass
class ItemStatus:
    item: Item
    status: str
    tests: list[dict]
    evidence: str | None
    reasons: list[str]


@dataclass
class Report:
    plan: Plan
    items: dict[str, ItemStatus]
    failures: list[dict]
    warnings: list[dict]
    counts: dict
    base_used: bool
    transitions: list[dict]

    @property
    def ok(self) -> bool:
        return not self.failures


def evaluate(
    plan: Plan,
    refs: list[TestRef],
    cases: list[Case],
    evidence: dict[str, Evidence],
    *,
    base_status: dict | None = None,
    changed_ids: set[str] | None = None,
) -> Report:
    failures: list[dict] = []
    warnings: list[dict] = []

    for dup in sorted(set(plan.duplicates)):
        failures.append({"gate": "unknown-id", "id": dup, "detail": "defined more than once"})

    # References by ID: static ones from the sources, plus any junit case that
    # carries a plan property but was not found statically (a dynamically
    # applied marker). Both must name a real ID.
    by_id: dict[str, list[TestRef]] = {}
    for ref in refs:
        by_id.setdefault(ref.plan_id, []).append(ref)
        if ref.plan_id not in plan.items:
            failures.append(
                {"gate": "unknown-id", "id": ref.plan_id, "detail": f"referenced by {ref.source}"}
            )
    junit_ids: dict[str, list[Case]] = {}
    for case in cases:
        for pid in case.plan_ids:
            junit_ids.setdefault(pid, []).append(case)
            if pid not in plan.items and pid not in by_id:
                failures.append(
                    {
                        "gate": "unknown-id",
                        "id": pid,
                        "detail": f"carried by junit case {case.classname}::{case.name}",
                    }
                )
    for stem, ev in evidence.items():
        if stem not in plan.items:
            failures.append(
                {"gate": "unknown-id", "id": stem, "detail": f"evidence file {ev.path}"}
            )
        for problem in ev.problems:
            failures.append({"gate": "bad-evidence", "id": stem, "detail": problem})

    statuses: dict[str, ItemStatus] = {}
    ran = referenced = 0
    for pid, item in plan.items.items():
        tests: list[dict] = []
        reasons: list[str] = []
        item_refs = by_id.get(pid, [])
        matched_cases: set[int] = set()
        for ref in item_refs:
            referenced += 1
            hits = [c for c in cases if _matches(ref, c)]
            matched_cases.update(id(c) for c in hits)
            if not hits:
                tests.append({"test": f"{ref.module}::{ref.name}", "outcome": "missing"})
                reasons.append(f"{ref.module}::{ref.name} is not in the results")
                continue
            ran += 1
            for c in hits:
                tests.append({"test": f"{c.classname}::{c.name}", "outcome": c.outcome})
                if c.outcome != PASSED:
                    reasons.append(f"{c.classname}::{c.name} {c.outcome}")
        for c in junit_ids.get(pid, []):
            if id(c) in matched_cases:
                continue
            tests.append({"test": f"{c.classname}::{c.name}", "outcome": c.outcome})
            if c.outcome != PASSED:
                reasons.append(f"{c.classname}::{c.name} {c.outcome}")

        ev = evidence.get(pid)
        has_tests = bool(tests)
        if not has_tests and ev is None:
            status = TODO
        else:
            if item.needs_test and not has_tests:
                reasons.append("verify: test, but no test references it")
            if item.needs_evidence and ev is None:
                reasons.append("verify: evidence, but there is no evidence file")
            if ev is not None and ev.problems:
                reasons.append("evidence is malformed")
            status = DONE if not reasons else IN_PROGRESS
        statuses[pid] = ItemStatus(item, status, tests, ev.path if ev else None, reasons)

    transitions: list[dict] = []
    base_items = (base_status or {}).get("items", {})
    for pid, st in statuses.items():
        before = (base_items.get(pid) or {}).get("status")
        if before == DONE and st.status != DONE:
            st.reasons.insert(0, f"was done on the base, now {st.status}")
            st.status = REGRESSED
            failures.append(
                {"gate": "regressed", "id": pid, "detail": "; ".join(st.reasons[1:]) or "?"}
            )
        if before is not None and before != st.status:
            transitions.append({"id": pid, "from": before, "to": st.status})
        elif before is None and base_status is not None and st.status != TODO:
            transitions.append({"id": pid, "from": "new", "to": st.status})

    for pid, st in statuses.items():
        phase = plan.phases[st.item.phase]
        if phase.state == "closed" and st.status != DONE:
            failures.append(
                {
                    "gate": "closed-phase",
                    "id": pid,
                    "detail": f"phase {phase.id} is closed but this item is {st.status}",
                }
            )
        if (
            changed_ids is not None
            and pid in changed_ids
            and phase.state == "active"
            and st.item.needs_test
            and not any(t["outcome"] != "missing" for t in st.tests)
            and not by_id.get(pid)
        ):
            warnings.append(
                {
                    "gate": "untested-item",
                    "id": pid,
                    "detail": "this change edits the item, and no test references it yet",
                }
            )

    counts = {
        "items": len(statuses),
        "by_status": {
            s: sum(1 for st in statuses.values() if st.status == s)
            for s in (TODO, IN_PROGRESS, DONE, REGRESSED)
        },
        "junit_cases": len(cases),
        "referenced_tests": referenced,
        "referenced_tests_found_in_results": ran,
    }
    return Report(plan, statuses, failures, warnings, counts, base_status is not None, transitions)


# --------------------------------------------------------------------------
# Output
# --------------------------------------------------------------------------


def to_json(report: Report) -> dict:
    return {
        "schema": SCHEMA,
        "ok": report.ok,
        "counts": report.counts,
        "phases": {
            p.id: {
                "state": p.state,
                "items": [i for i, st in report.items.items() if st.item.phase == p.id],
            }
            for p in report.plan.phases.values()
        },
        "items": {
            pid: {
                "phase": st.item.phase,
                "verify": st.item.verify,
                "status": st.status,
                "tests": st.tests,
                "evidence": st.evidence,
                "reasons": st.reasons,
            }
            for pid, st in report.items.items()
        },
        "failures": report.failures,
        "warnings": report.warnings,
        "base_used": report.base_used,
        "transitions": report.transitions,
    }


_ICON = {TODO: "⬜", IN_PROGRESS: "🟨", DONE: "✅", REGRESSED: "🟥"}


def to_markdown(report: Report) -> str:
    c = report.counts
    out = ["## Plan status", ""]
    out.append(
        f"{c['items']} items: "
        + ", ".join(f"{n} {s}" for s, n in c["by_status"].items())
        + f". Referenced tests found in this build's results: "
        f"**{c['referenced_tests_found_in_results']} of {c['referenced_tests']}** "
        f"(junit cases: {c['junit_cases']})."
    )
    if not report.base_used:
        out.append("")
        out.append("_No base status was available, so regressions were not checked._")
    if report.transitions:
        out += ["", "### Changes against the base", ""]
        out += [f"- **{t['id']}** {t['from']} → {t['to']}" for t in report.transitions]
    if report.failures:
        out += ["", "### Gate failures", ""]
        out += [f"- `{f['gate']}` **{f['id']}**: {f['detail']}" for f in report.failures]
    if report.warnings:
        out += ["", "### Warnings", ""]
        out += [f"- `{w['gate']}` **{w['id']}**: {w['detail']}" for w in report.warnings]
    for phase in report.plan.phases.values():
        rows = [st for st in report.items.values() if st.item.phase == phase.id]
        done = sum(1 for st in rows if st.status == DONE)
        out += ["", f"### {phase.id} ({phase.state}): {done}/{len(rows)} done", ""]
        out += ["| Item | Status | Verify | Detail |", "| --- | --- | --- | --- |"]
        for st in rows:
            detail = _detail(st)
            if st.evidence:
                detail += f"; evidence `{st.evidence}`"
            out.append(
                f"| {st.item.id} | {_ICON[st.status]} {st.status} | {st.item.verify} | "
                f"{detail.replace('|', '/')} |"
            )
    return "\n".join(out) + "\n"


def _detail(st: ItemStatus) -> str:
    """One table cell: outcome counts, then the first few reasons (the JSON has all)."""
    parts = []
    if st.tests:
        tally: dict[str, int] = {}
        for t in st.tests:
            tally[t["outcome"]] = tally.get(t["outcome"], 0) + 1
        parts.append(", ".join(f"{n} {o}" for o, n in sorted(tally.items())))
    shown = st.reasons[:2]
    parts.extend(shown)
    if len(st.reasons) > len(shown):
        parts.append(f"and {len(st.reasons) - len(shown)} more")
    return "; ".join(parts) or "-"


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    ap.add_argument("--plan", action="append", required=True, type=Path, help="plan file(s)")
    ap.add_argument(
        "--tests", action="append", default=[], type=Path, help="test source dir(s) to scan"
    )
    ap.add_argument("--junit", action="append", default=[], type=Path, help="junit XML file(s)")
    ap.add_argument("--evidence", type=Path, help="evidence directory (<ID>.md files)")
    ap.add_argument("--base-status", type=Path, help="plan-status.json from the base branch")
    ap.add_argument("--diff-base", help="git ref to diff the plan against, for warnings")
    ap.add_argument("--repo", type=Path, default=Path("."), help="repository root")
    ap.add_argument("--out-json", type=Path)
    ap.add_argument("--out-md", type=Path)
    ap.add_argument("--no-git", action="store_true", help="skip the evidence commit-ancestry check")
    args = ap.parse_args(argv)

    try:
        plan = parse_plans(args.plan)
        refs = scan_tests(args.tests, args.repo)
        cases = parse_junit(args.junit)
        evidence = load_evidence(args.evidence, args.repo, check_git=not args.no_git)
        base = None
        if args.base_status is not None:
            if args.base_status.is_file():
                base = json.loads(args.base_status.read_text(encoding="utf-8"))
                if base.get("schema") != SCHEMA:
                    raise InputError(f"{args.base_status} is not a {SCHEMA} file")
            else:
                print(f"plancheck: no base status at {args.base_status}", file=sys.stderr)
        changed = changed_item_ids(args.repo, args.diff_base, args.plan) if args.diff_base else None
    except (InputError, json.JSONDecodeError) as exc:
        print(f"plancheck: {exc}", file=sys.stderr)
        return 2

    report = evaluate(plan, refs, cases, evidence, base_status=base, changed_ids=changed)
    markdown = to_markdown(report)
    if args.out_json:
        args.out_json.write_text(json.dumps(to_json(report), indent=2) + "\n", encoding="utf-8")
    if args.out_md:
        args.out_md.write_text(markdown, encoding="utf-8")
    print(markdown)
    for f in report.failures:
        print(f"plancheck: FAIL [{f['gate']}] {f['id']}: {f['detail']}", file=sys.stderr)
    for w in report.warnings:
        print(f"plancheck: warning [{w['gate']}] {w['id']}: {w['detail']}", file=sys.stderr)
    return 0 if report.ok else 1


if __name__ == "__main__":
    sys.exit(main())

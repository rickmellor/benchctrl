"""The ``plan`` marker reaches the junit XML that tools/plancheck.py reads.

Run in a subprocess against this repo's real conftest and pytest's default
junit family, because the property only matters if it survives into the file.
"""

from __future__ import annotations

import shutil
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

pytestmark = pytest.mark.plan("CORE-P0.2")

CONFTEST = Path(__file__).with_name("conftest.py")


def _run(tmp_path: Path, body: str) -> tuple[subprocess.CompletedProcess, Path]:
    shutil.copy(CONFTEST, tmp_path / "conftest.py")
    (tmp_path / "test_sample.py").write_text(body)
    junit = tmp_path / "junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "-p",
            "no:cacheprovider",
            "-o",
            "markers=plan: plan item",
            f"--junitxml={junit}",
            str(tmp_path),
        ],
        capture_output=True,
        text=True,
        cwd=tmp_path,
    )
    return proc, junit


def _properties(junit: Path) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    for tc in ET.parse(junit).getroot().iter("testcase"):
        out[tc.get("name", "")] = [
            p.get("value", "") for p in tc.iter("property") if p.get("name") == "plan"
        ]
    return out


def test_marker_becomes_a_junit_property(tmp_path: Path) -> None:
    proc, junit = _run(
        tmp_path,
        "import pytest\n"
        "pytestmark = pytest.mark.plan('CORE-P0.9')\n"
        "@pytest.mark.plan('CORE-P0.1', 'CORE-P0.2')\n"
        "def test_a():\n    pass\n"
        "def test_b():\n    pass\n"
        "@pytest.mark.skip(reason='x')\n"
        "@pytest.mark.plan('CORE-P0.3')\n"
        "def test_c():\n    pass\n",
    )
    assert proc.returncode == 0, proc.stdout + proc.stderr
    props = _properties(junit)
    assert sorted(props["test_a"]) == ["CORE-P0.1", "CORE-P0.2", "CORE-P0.9"]
    assert props["test_b"] == ["CORE-P0.9"]
    # A skipped test still carries its reference, so plancheck can see that
    # the item's test was skipped rather than never referenced.
    assert sorted(props["test_c"]) == ["CORE-P0.3", "CORE-P0.9"]


@pytest.mark.parametrize("bad", ["'core-p0.1'", "'CORE-P0'", "", "12"])
def test_malformed_plan_id_is_a_usage_error(tmp_path: Path, bad: str) -> None:
    proc, _ = _run(tmp_path, f"import pytest\n@pytest.mark.plan({bad})\ndef test_a():\n    pass\n")
    assert proc.returncode == 4, proc.stdout + proc.stderr  # pytest's ExitCode.USAGE_ERROR
    assert "plan" in (proc.stdout + proc.stderr)

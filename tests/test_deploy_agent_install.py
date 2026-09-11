"""The agent installer on a second platform: does it still do the safe thing?

Why these tests exist
---------------------

``deploy/install-agent.sh`` was written for one board — the Arduino Uno Q,
user ``arduino``, package unzipped at a fixed path, system python — and every
default encoded that. Bringing up a Raspberry Pi 5 as a second agent platform
(git checkout, venv, login user ``rick``) meant the defaults had to become
*derived* rather than fixed, and a derived default has more ways to be wrong:

- ``RUN_USER`` from ``SUDO_USER`` must never resolve to ``root``. The unit
  drops privileges on purpose; ``sudo ./install-agent.sh`` from a root shell
  would otherwise install a root-owned service without anyone asking for it.
- The Uno Q must keep working with **no knobs set** — the fallbacks are the
  old fixed values, and a test pins them so the Pi work cannot silently move
  the Uno Q off its documented layout.
- The fresh ``agent.json`` must point ``blob_dir``/``runs_dir`` at the service
  user's home. A systemd service's cwd is ``/``, which ``ProtectSystem=full``
  makes read-only, and the code-level fallbacks (``default_spill_dir``,
  ``default_runs_dir``) only know about ``/home/arduino``.

The scripts are POSIX ``sh`` and root-only, so they are checked the way the
board-sync scripts are: ``sh -n`` for syntax, and the *text* for the contracts
that matter, comment lines stripped so a test cannot pass on the rationale.
"""

from __future__ import annotations

import pathlib
import re
import subprocess

import pytest

DEPLOY = pathlib.Path(__file__).resolve().parent.parent / "deploy"

SCRIPTS = (
    "install-agent.sh",
    "install-fui.sh",
    "verify-ch341-qr10x.sh",
    "install-display-hotplug.sh",
    "install-kiosk.sh",
)


def _code_lines(name: str) -> str:
    """The script minus comment lines, so assertions bind to behaviour."""
    text = (DEPLOY / name).read_text(encoding="utf-8")
    return "\n".join(ln for ln in text.splitlines() if not ln.lstrip().startswith("#"))


@pytest.mark.parametrize("name", SCRIPTS)
def test_install_scripts_pass_shell_syntax_check(name):
    out = subprocess.run(
        ["sh", "-n", str(DEPLOY / name)], capture_output=True, text=True, timeout=60
    )
    assert out.returncode == 0, f"{name}: {out.stderr}"


@pytest.mark.parametrize("name", ("install-agent.sh", "install-fui.sh"))
def test_run_user_is_derived_from_sudo_user_but_never_root(name):
    """``sudo`` from a login shell installs for that login; from root, not for root."""
    code = _code_lines(name)
    assert "SUDO_USER" in code, f"{name}: RUN_USER is not derived from SUDO_USER"
    # The case arm that maps an empty or root SUDO_USER to the Uno Q default.
    assert re.search(r'""\|root\)\s+RUN_USER=arduino', code), (
        f"{name}: an empty or root SUDO_USER must fall back to arduino, never root"
    )


@pytest.mark.parametrize("name", ("install-agent.sh", "install-fui.sh"))
def test_the_uno_q_layout_is_still_the_fallback(name):
    """No knobs, no checkout beside the script: the old fixed paths must win."""
    code = _code_lines(name)
    assert "SRC_DIR=/home/arduino/benchctrl-1.2.0/src" in code
    assert "RUN_USER=arduino" in code


def test_a_checkout_beside_the_script_is_preferred_over_the_uno_q_path():
    """A git clone on a Pi has ``src/benchctrl`` next to ``deploy/``."""
    code = _code_lines("install-agent.sh")
    assert '"$here/../src/benchctrl"' in code
    assert '"$here/../.venv/bin/python"' in code
    assert "PYTHON=/usr/bin/python3" in code, "system python must remain the fallback"


def test_the_installer_looks_before_it_touches_systemd():
    """The import probe must still run, and run *after* the defaults resolve.

    The whole point of deriving defaults is that a wrong guess must fail at
    the probe with a readable message, not as a unit flapping every 5 s.
    """
    code = _code_lines("install-agent.sh")
    probe = code.index("import benchctrl, serial")
    resolved = code.index("install-agent: SRC_DIR=")
    reload = code.index("systemctl daemon-reload")
    assert resolved < probe < reload


def test_a_fresh_agent_json_points_state_at_the_service_users_home():
    code = _code_lines("install-agent.sh")
    assert "STATE_DIR=${STATE_DIR:-/home/$RUN_USER/benchctrl}" in code
    assert "$STATE_DIR/blobs" in code
    assert "$STATE_DIR/runs" in code
    # ...and only on first install: the rewrite must sit inside the branch that
    # generates a token, i.e. after the "keeping existing" check.
    keep = code.index("keeping existing")
    rewrite = code.index("$STATE_DIR/blobs")
    assert keep < rewrite


def test_the_installer_still_installs_the_safety_line():
    """``--safe-stop`` on stop is the reason the unit exists; a refactor of the
    sed that rewrites ExecStart/ExecStopPost must not drop the second line."""
    code = _code_lines("install-agent.sh")
    assert "ExecStopPost=$PYTHON" in code
    unit = (DEPLOY / "systemd" / "benchctrl-agent.service").read_text(encoding="utf-8")
    assert "--safe-stop" in unit


def test_the_ch341_verifier_stops_where_the_kernel_has_the_driver():
    """On a host with ``ch341`` (Raspberry Pi OS, desktop Linux) the userspace
    bridge is never selected, so "verifying" it would prove the wrong thing.
    The script must say so and exit 0 *before* installing the udev rule."""
    code = _code_lines("verify-ch341-qr10x.sh")
    guard = code.index("/sys/bus/usb-serial/drivers/ch341")
    install = code.index("install -m 0644")
    assert guard < install
    # exit 0, not 1: "not needed here" is not a failure.
    tail = code[guard : guard + 600]
    assert "exit 0" in tail

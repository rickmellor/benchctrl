"""The hardware-free suite must collect on a host with no POSIX tty layer.

Windows CI proved this the hard way: four modules imported ``termios``/``fcntl``/
``pty`` at top level, so pytest could not even *collect* — 23 errors before a
single test ran, on every Python from 3.9 to 3.13. The fix lives in those modules
(guarded import, raise at the first use); this file is what pins it, because the
failure mode is invisible from a Linux box — here everything imports either way,
so nothing else would notice a regression.

There is no Windows runner here, so the host is emulated the way the platform
actually differs: by making the POSIX-only modules unimportable with
``sys.modules[name] = None``, which is exactly what ``import termios`` raises
``ModuleNotFoundError`` for on a host that lacks it. Every check runs in a
subprocess, both because the block has to be in place *before* the modules
import and because a module set to ``None`` poisons the process afterwards.
``BENCHCTRL_BLOCK_POSIX=1`` is the switch, read by a prelude handed to
``python -c``; no production code reads it, so the code under test is the same
path Linux CI takes.

pyserial is imported before the block, deliberately. On a real Windows host
``serial`` resolves to ``serialwin32``, which needs none of these modules; here
it resolves to ``serialposix``, which imports ``fcntl`` and ``termios`` itself.
Blocking first would fail pyserial's own import and report a problem this package
does not have.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]

#: Every subprocess here does an import or a collection, never real I/O.
TIMEOUT_S = 120

#: The modules whose top-level imports broke collection on Windows.
POSIX_LINK_MODULES = (
    "benchctrl.sim.loopback",
    "benchctrl.drivers.cyberpower_pdu41002.links",
    "benchctrl.drivers.silabs_cp2112.hidraw",
    "benchctrl.drivers.ontrak_adu218.usbfs",
)

#: What a POSIX host has and Windows does not. ``pty`` and ``tty`` are included
#: because each pulls in ``termios``, so guarding ``termios`` alone does not make
#: a module that imports them importable.
POSIX_ONLY_MODULES = ("termios", "fcntl", "pty", "tty")

#: Runs before anything of ours, in every subprocess here.
_PRELUDE = (
    "import os, sys\n"
    'sys.path.insert(0, os.path.join(os.getcwd(), "src"))\n'
    "import serial, serial.tools.list_ports\n"
    'if os.environ.get("BENCHCTRL_BLOCK_POSIX") == "1":\n'
    "    for _name in " + repr(POSIX_ONLY_MODULES) + ":\n"
    "        sys.modules[_name] = None\n"
)


#: The emulation is for Linux hosts: it proves the guarded imports work on a
#: host that *has* the POSIX layer and hides it. On Windows the layer is really
#: absent, so the same checks run unemulated and the real run is the proof.
_LINUX_ONLY_EMULATION = pytest.mark.skipif(
    sys.platform == "win32",
    reason="emulates a Windows host by hiding POSIX modules from a POSIX one; "
    "on Windows there is nothing to hide and the real run is the proof",
)


def _run(body: str, *, block: bool) -> subprocess.CompletedProcess:
    """Run ``body`` in a fresh interpreter, with or without the POSIX modules."""
    env = dict(os.environ, BENCHCTRL_BLOCK_POSIX="1" if block else "0")
    proc = subprocess.run(
        [sys.executable, "-c", _PRELUDE + body],
        env=env,
        cwd=str(ROOT),
        capture_output=True,
        text=True,
        timeout=TIMEOUT_S,
    )
    assert proc.returncode >= 0, f"interpreter killed by a signal; stderr:\n{proc.stderr}"
    return proc


# ---------------------------------------------------------------------------
# 1. The four modules import on a host with no POSIX tty layer
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("module", POSIX_LINK_MODULES)
def test_module_imports_without_posix(module: str):
    """Collection reaches a module long before it runs a test, so import is the bar.

    One module that cannot be imported takes the whole run down with it, which is
    how a Windows CI job ends with 23 errors and no test results at all.
    """
    proc = _run(f"import importlib; importlib.import_module({module!r})", block=True)
    assert proc.returncode == 0, f"{module} failed to import without POSIX:\n{proc.stderr}"


@_LINUX_ONLY_EMULATION
def test_posix_block_is_real():
    """Otherwise every check below passes for the wrong reason.

    ``sys.modules[name] = None`` only helps if it makes the import fail the way a
    genuinely missing module does, and if the guarded modules see the difference.
    """
    assert _run("import termios, fcntl, pty, tty", block=False).returncode == 0, (
        "this host is not POSIX after all, so it proves nothing"
    )

    blocked = _run("import termios", block=True)
    assert blocked.returncode != 0 and "ModuleNotFoundError" in blocked.stderr, (
        "blocking termios did not make it unimportable"
    )

    # And the guard is what saves us: the name is present but None, so the module
    # body ran to completion.
    probe = _run(
        "import benchctrl.sim.loopback as m\n"
        "assert m.termios is None, m.termios\n"
        "assert m.tty is None, m.tty\n",
        block=True,
    )
    assert probe.returncode == 0, probe.stderr


# ---------------------------------------------------------------------------
# 2. The POSIX-only calls raise the module's own error, not an AttributeError
# ---------------------------------------------------------------------------

#: (module, name bound to it, expression that needs POSIX, exception name).
#: Each expression is the first thing in its module that touches the tty layer,
#: reached past any argument or "is it open" check that works cross-platform.
_POSIX_CALLS = (
    (
        "benchctrl.sim.loopback",
        "loopback",
        "loopback.SerialLoopback()",
        "BenchConnectionError",
    ),
    (
        "benchctrl.drivers.cyberpower_pdu41002.links",
        "links",
        "links.SshLink('h').open()",
        "LinkError",
    ),
    (
        "benchctrl.drivers.silabs_cp2112.hidraw",
        "hidraw",
        "_opened(hidraw.HidrawLink('/dev/hidraw9')).get_feature(1, 8)",
        "HidrawError",
    ),
    (
        "benchctrl.drivers.ontrak_adu218.usbfs",
        "usbfs",
        "_opened(usbfs.Adu218UsbfsLink())._transfer(2, bytearray(4), 10)",
        "Adu218LinkError",
    ),
)


@pytest.mark.parametrize("module,binding,call,exc_name", _POSIX_CALLS)
def test_posix_only_call_raises_the_module_error(
    module: str, binding: str, call: str, exc_name: str
):
    """The honest failure is "this host cannot do this", raised at the call.

    A bare ``fcntl = None`` imports fine and then raises
    ``AttributeError: 'NoneType' object has no attribute 'ioctl'`` from inside a
    link, which reads as a bug in the driver rather than a limit of the host. The
    message is pinned too: an operator on Windows should not have to infer the
    cause from a stack frame.
    """
    body = (
        f"import importlib\n"
        f"{binding} = importlib.import_module({module!r})\n"
        "def _opened(obj):\n"
        "    obj._fd = 999\n"
        "    return obj\n"
        f"try:\n    {call}\n"
        "except Exception as exc_obj:\n"
        "    print(type(exc_obj).__name__ + '|' + str(exc_obj))\n"
        "else:\n"
        "    print('NO-RAISE')\n"
    )
    proc = _run(body, block=True)
    assert proc.returncode == 0, f"the probe itself failed:\n{proc.stderr}"
    name, _, message = proc.stdout.strip().partition("|")
    assert name == exc_name, f"expected {exc_name}, got {name}: {message}\n{proc.stderr}"
    assert "needs a POSIX host" in message, f"{exc_name} did not name the cause: {message!r}"


@_LINUX_ONLY_EMULATION
def test_posix_errors_stay_module_local_on_linux():
    """The guards must not change what Linux sees.

    On a POSIX host these same calls still raise what they always raised, so the
    guarded import is invisible to every existing test and caller.
    """
    proc = _run(
        "import benchctrl.sim.loopback as m\n"
        "assert m.termios.__name__ == 'termios', m.termios\n"
        "assert m.tty.__name__ == 'tty', m.tty\n"
        "import benchctrl.drivers.ontrak_adu218.usbfs as u\n"
        "assert u.fcntl.__name__ == 'fcntl', u.fcntl\n"
        "import benchctrl.drivers.silabs_cp2112.hidraw as h\n"
        "assert h.fcntl.__name__ == 'fcntl', h.fcntl\n"
        "import benchctrl.drivers.cyberpower_pdu41002.links as l\n"
        "assert l.pty.__name__ == 'pty', l.pty\n",
        block=False,
    )
    assert proc.returncode == 0, proc.stderr


# ---------------------------------------------------------------------------
# 3. Collection is the same with and without the POSIX layer
# ---------------------------------------------------------------------------

_COLLECTED = re.compile(r"(\d+) tests? collected")


def _collect(block: bool) -> subprocess.CompletedProcess:
    """``pytest --collect-only`` in a subprocess, POSIX available or not."""
    return _run(
        "import pytest, sys\n"
        "sys.exit(pytest.main(['--collect-only', '-q', '-m', 'not hardware',"
        " '-p', 'no:cacheprovider']))\n",
        block=block,
    )


def _count(stdout: str) -> int:
    match = _COLLECTED.search(stdout)
    assert match, f"no collected-count in:\n{stdout[-2000:]}"
    return int(match.group(1))


def test_collection_reports_no_errors_without_posix():
    """The CI failure as an assertion: collection completes with 0 errors.

    The returncode is the load-bearing half. pytest exits 2 with
    ``Interrupted: N errors during collection`` if any module fails to import, so
    an unguarded top-level ``import termios`` anywhere under ``tests/`` trips
    this. The summary line is checked too but cannot stand alone — test names in
    this suite contain the word "error".
    """
    proc = _collect(block=True)
    interrupted = [ln for ln in proc.stdout.splitlines() if "Interrupted" in ln]
    errors = [ln for ln in proc.stdout.splitlines() if ln.startswith("ERROR ")]
    detail = "\n".join(interrupted + errors) or proc.stderr[-2000:]
    assert proc.returncode == 0, (
        f"collection failed on a host without POSIX (rc={proc.returncode}):\n{detail}"
    )
    assert not interrupted and not errors, f"collection problems: {interrupted + errors}"
    assert _count(proc.stdout) > 1000, "suspiciously little collected"


def test_collection_is_the_same_size_with_and_without_posix():
    """A guard that hid tests instead of protecting them would show up here.

    The same count both ways means nothing quietly became uncollectable. The
    ``skipif`` markers that keep POSIX-only tests from *running* on Windows are a
    runtime decision and do not change what collection finds.
    """
    clean = _collect(block=False)
    assert clean.returncode == 0, f"the suite does not collect here at all:\n{clean.stdout[-1500:]}"
    blocked = _collect(block=True)
    assert blocked.returncode == 0, (
        f"collection errors without POSIX:\n{blocked.stdout[-1500:]}\n{blocked.stderr[-1500:]}"
    )
    assert _count(blocked.stdout) == _count(clean.stdout), (
        f"{_count(blocked.stdout)} collected without POSIX vs {_count(clean.stdout)} with it"
    )

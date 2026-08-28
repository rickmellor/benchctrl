"""Open, call, and tear down one device for a single CLI invocation.

The generated subcommands (:py:mod:`benchctrl.cli_generated`) are the easy half.
This is the half that cannot be generated, because a one-shot process has a
lifecycle an MCP server does not: it opens a device, makes one call, and exits
inside a second. Four things follow, and each is a defect if it is guessed.

1. Teardown depends on the mode
-------------------------------
``local`` / ``sim``
    Call the driver's ``*_close`` tool.

``remote``
    ``close()`` is **refused** by the proxy (``net/proxy.py``) — closing is
    governor-mediated so an armed output is never orphaned. The correct exit is
    :py:func:`benchctrl.session.shutdown`, which drops the client; the agent
    reads a clean disconnect as consent to release the writer claim
    (``server.drop_session``) and, if that was the last session holding an armed
    device, to drive it to its safe state after the grace period.

    ``agent.close`` is **not** the remote equivalent and must not be substituted.
    It calls ``governor.trip(TripReason.OPERATOR, ...)``, which is bench-wide:
    one CLI command finishing would drive every armed device on the bench to its
    safe state, including instruments this invocation never touched.

2. Three closes change output state, and two report failure in the *result*
--------------------------------------------------------------------------
It would be convenient if closing were always inert. It is not:

===================  ======================================================
``dl3031a_close``    disables the load input; on failure sets
                     ``input_off_failed`` in the returned dict
``dp2031_close``     disables all three outputs; on failure sets
                     ``outputs_off_failed``
``cp2112_close``     releases asserted lines and restores the as-found GPIO
                     configuration, so a DUT is not left held in reset
===================  ======================================================

The first two report a failed de-energise **as a dict key, not an exception** —
deliberately, so the close still completes. A teardown that discarded the result
would therefore swallow "the load may still be sinking current" precisely when it
is true. :py:func:`teardown` returns the result and
:py:func:`describe_teardown_failure` renders it; the caller prints it to stderr
and it becomes a non-zero exit.

3. A one-shot must not disarm what a previous one armed
------------------------------------------------------
``adu218_open`` defaults ``disarm_watchdog=True`` and its ``_connect`` sends
``WD0``. That default is right for a long-lived server — the watchdog's setting
reads 0 both for "timed out" and "never enabled" (``KNOWN_LIMITATIONS`` §F-22),
so a fresh session cannot interpret an inherited value — and wrong here, because
every CLI invocation is a fresh session. Left alone,

    benchctrl adu218 set-watchdog 3     # arms it
    benchctrl adu218 relay-states       # a *read* — and it disarms the watchdog

So the CLI overrides the default to ``False``. The driver is not changed: the
default is correct for its other caller.

4. Opening is implicit, and its arguments come from config
----------------------------------------------------------
``benchctrl pdu41002 outlet-states`` must work without the operator also typing
an open command, so the open is implicit. Its arguments — port, host, allowlists
— are per-bench facts that belong in ``bench.toml``, not on every command line.
Credentials are never among them: the PDU password comes from
``BENCHCTRL_PDU_PASSWORD`` in the environment where the driver runs, because
``DeviceConfig.open`` is emitted verbatim by ``to_dict()`` and crosses the
authenticated-but-unencrypted RPC wire.
"""

from __future__ import annotations

import importlib
import logging
from typing import Any, Callable, Optional

log = logging.getLogger("benchctrl.cli")

#: CLI group -> (device key, module, open tool, close tool). The Arc is absent
#: from the open column on purpose: it has no ``*_open`` tool, opening on first
#: use and closing through ``disconnect``.
DEVICE_LIFECYCLE: dict[str, dict[str, Optional[str]]] = {
    "arc": {
        "device_key": "otii_arc",
        "module": "benchctrl.drivers.otii_arc.mcp_tools",
        "open": None,  # opens lazily on first tool call
        "close": "disconnect",
    },
    "qr10x": {
        "device_key": "eastwood_qr10x",
        "module": "benchctrl.drivers.eastwood_qr10x.mcp_tools",
        "open": "qr10x_open",
        "close": "qr10x_close",
    },
    "dl3031a": {
        "device_key": "rigol_dl3031a",
        "module": "benchctrl.drivers.rigol_dl3031a.mcp_tools",
        "open": "dl3031a_open",
        "close": "dl3031a_close",
    },
    "dp2031": {
        "device_key": "rigol_dp2031",
        "module": "benchctrl.drivers.rigol_dp2031.mcp_tools",
        "open": "dp2031_open",
        "close": "dp2031_close",
    },
    "sdm4065a": {
        "device_key": "siglent_sdm4065a",
        "module": "benchctrl.drivers.siglent_sdm4065a.mcp_tools",
        "open": "sdm4065a_open",
        "close": "sdm4065a_close",
    },
    "pdu41002": {
        "device_key": "cyberpower_pdu41002",
        "module": "benchctrl.drivers.cyberpower_pdu41002.mcp_tools",
        "open": "pdu41002_open",
        "close": "pdu41002_close",
    },
    "cp2112": {
        "device_key": "silabs_cp2112",
        "module": "benchctrl.drivers.silabs_cp2112.mcp_tools",
        "open": "cp2112_open",
        "close": "cp2112_close",
    },
    "adu218": {
        "device_key": "ontrak_adu218",
        "module": "benchctrl.drivers.ontrak_adu218.mcp_tools",
        "open": "adu218_open",
        "close": "adu218_close",
    },
    # The framework tools work on saved files and, for the profiler/emulator,
    # on whatever SMU the Arc's singleton holds. No lifecycle of their own.
    "framework": {
        "device_key": None,
        "module": "benchctrl.framework_tools",
        "open": None,
        "close": None,
    },
}

#: Open-argument overrides the CLI applies because a one-shot process differs
#: from a long-lived server. Each needs the reason, because each contradicts a
#: driver default that is correct in its own context.
ONE_SHOT_OPEN_OVERRIDES: dict[str, dict[str, Any]] = {
    "adu218_open": {
        # See the module docstring, point 2: the driver's True is right for a
        # server and would make a CLI *read* disarm a watchdog.
        "disarm_watchdog": False,
    },
}


def lifecycle_for(group: str) -> dict[str, Optional[str]]:
    """Lifecycle entry for a CLI group, or raise naming the omission."""
    try:
        return DEVICE_LIFECYCLE[group]
    except KeyError:
        raise KeyError(
            f"no lifecycle entry for CLI group {group!r} in "
            f"benchctrl.cli_lifecycle.DEVICE_LIFECYCLE — a group whose device "
            f"cannot be opened or closed would leak the port"
        ) from None


def open_kwargs_for(
    open_tool: str,
    device_key: Optional[str],
    bench: Optional[dict] = None,
) -> dict[str, Any]:
    """Arguments for the implicit open: bench config, then the one-shot overrides.

    ``bench`` maps device key -> open kwargs. Overrides are applied *last* and
    deliberately win over the config. They exist because a one-shot process is
    not a server, which is not something a per-bench config file should have to
    know or be able to get wrong.
    """
    kwargs: dict[str, Any] = {}
    if bench and device_key:
        kwargs.update(bench.get(device_key, {}))
    kwargs.update(ONE_SHOT_OPEN_OVERRIDES.get(open_tool, {}))
    return kwargs


def bench_open_kwargs(cfg: Any = None) -> dict[str, dict[str, Any]]:
    """Per-device open arguments from the active config.

    Reuses ``DeviceConfig.open`` — the dict the MCP server and the agent already
    forward as ``**open_kwargs`` — rather than adding a second, CLI-only config
    file. A bench whose PDU lives at a fixed address should not have to say so
    once for the server and again for the CLI, and two files would eventually
    disagree about which outlets are allowed.

    ``open`` is not a place for a secret: it is emitted by ``to_dict()`` and it
    crosses the authenticated-but-unencrypted RPC wire. The PDU password comes
    from ``BENCHCTRL_PDU_PASSWORD`` in the environment where the driver runs.
    """
    from benchctrl import config, session

    cfg = session.current_config() if cfg is None else cfg
    return {
        key: dict(cfg.device(key).open)
        for key in config.DEVICE_KEYS
        if cfg.device(key).open
    }


def _tool(module_name: str, tool_name: str) -> Callable:
    return getattr(importlib.import_module(module_name), tool_name)


#: Keys a ``*_close`` tool sets to report that it could not de-energise. The
#: close still succeeded — the *hardware* did not — so these arrive as dict keys
#: rather than exceptions, and a teardown that ignored the result would drop them.
TEARDOWN_FAILURE_KEYS: tuple[str, ...] = (
    "input_off_failed",  # dl3031a_close: the load may still be sinking current
    "outputs_off_failed",  # dp2031_close: an output may still be energised
)


def describe_teardown_failure(result: Any) -> Optional[str]:
    """An operator-facing warning if a close failed to de-energise, else None.

    Separated from :py:func:`teardown` so it can be tested against a literal
    driver result dict without a device, and so the caller decides where the
    text goes (stderr) and what it costs (the exit code).
    """
    if not isinstance(result, dict):
        return None
    hits = [(k, result[k]) for k in TEARDOWN_FAILURE_KEYS if result.get(k)]
    if not hits:
        return None
    detail = "; ".join(f"{k}: {v}" for k, v in hits)
    return (
        f"teardown could not return the device to its safe state ({detail}). "
        f"An output may still be energised or sinking current — check the "
        f"instrument's front panel before touching the DUT."
    )


def teardown(group: str, *, mode: str) -> Optional[dict]:
    """Release the device the way its mode requires.

    Args:
        group: the CLI group just used.
        mode: ``"local"``, ``"sim"``, or ``"remote"`` for that device.

    Returns:
        The close tool's result, or ``None`` when teardown was a session
        shutdown (remote) or the group has no device (framework).

    Never raises, because a teardown failure must not replace the command's own
    outcome — the operator needs the measurement they asked for, and an exception
    here would discard it. But it must not *hide* one either: an exception is
    returned as a result dict carrying a failure key, so the same
    :py:func:`describe_teardown_failure` path reports it. Silence and success
    have to look different.
    """
    entry = lifecycle_for(group)
    if entry["close"] is None:
        return None

    if mode == "remote":
        # close() is refused by the proxy, and agent.close would trip the
        # governor bench-wide. A clean client disconnect is the supported exit.
        from benchctrl import session

        try:
            session.shutdown()
        except Exception as exc:
            log.warning("cli: session.shutdown() raised: %r", exc)
            # The claim may still be held until the deadman expires, which for
            # an armed device is the difference between seconds and the full
            # window. Worth saying out loud.
            return {"outputs_off_failed": f"session.shutdown(): {exc!r}"}
        return None

    close_tool = entry["close"]
    try:
        return _tool(entry["module"], close_tool)()
    except Exception as exc:
        log.warning("cli: %s raised: %r", close_tool, exc)
        # dl3031a_close and dp2031_close disable outputs *before* closing, so a
        # raise here can mean the de-energise never happened.
        return {"outputs_off_failed": f"{close_tool}(): {exc!r}"}


def run_tool(
    group: str,
    fn: Callable,
    kwargs: dict[str, Any],
    *,
    bench: Optional[dict] = None,
) -> tuple[Any, Optional[str]]:
    """Open if needed, call ``fn``, then tear down for the resolved mode.

    Returns ``(result, teardown_warning)`` — the warning is ``None`` on a clean
    teardown. Returned rather than printed so this stays testable without
    capturing streams, and so the caller owns the exit code.

    The mode is read once, before the call, from the same
    :py:mod:`benchctrl.session` state the tools use — so teardown cannot pick a
    different mode than the open did.
    """
    from benchctrl import session

    entry = lifecycle_for(group)
    device_key = entry["device_key"]
    mode = session.mode_for(device_key) if device_key else "local"

    open_tool = entry["open"]
    # Calling the open tool *as* the command is not an error — it is how an
    # operator checks a device is reachable — but doing it twice is, since every
    # driver's open refuses when already open. Same for calling close directly:
    # teardown would then be the second close, which reports "was not open".
    if open_tool is not None and fn.__name__ not in (open_tool, entry["close"]):
        opener = _tool(entry["module"], open_tool)
        opener(**open_kwargs_for(open_tool, device_key, bench))

    warning: Optional[str] = None
    try:
        result = fn(**kwargs)
    except BaseException:
        # Tear down on the failure path too — an exception mid-command is
        # exactly when a port must not be left held. The warning is dropped
        # here on purpose: the original exception is the more important news,
        # and describe_teardown_failure's text is logged either way.
        failure = teardown(group, mode=mode)
        note = describe_teardown_failure(failure)
        if note:
            log.error("cli: %s", note)
        raise

    # A command that *was* the close must not be closed again.
    if fn.__name__ != entry["close"]:
        warning = describe_teardown_failure(teardown(group, mode=mode))
    else:
        warning = describe_teardown_failure(result)
    return result, warning

"""Generate the CLI's device subcommands from the MCP tool surface.

One subcommand per MCP tool, built by reflection over each module's ``_TOOLS``
tuple. A tool added to a driver becomes a CLI subcommand with no edit here —
that is the whole point, and it is why the ``_TOOLS`` parity tests in
``test_mcp.py`` are load-bearing rather than decorative.

What is *not* generated
-----------------------
Risk. :py:mod:`benchctrl.cli_tiers` classifies every tool explicitly, and a
tool with no entry raises rather than defaulting — see that module for why
``dispatch.is_mutator()`` was measured and rejected as the gate.

Why ``_TOOLS`` and not ``mcp.list_tools()``
------------------------------------------
Importing :py:mod:`benchctrl.mcp` costs ~0.64 s (FastMCP alone is 0.44 s of
that) against 0.06 s for all eight drivers, and a full remote round trip is
63 ms — so asking a live server would be slower than every call the CLI makes.
More decisively, it would hard-depend the ``benchctrl`` console script on the
optional ``[mcp]`` extra, which requires Python >=3.10 while the package
declares ``requires-python = ">=3.9"``.

Booleans are the trap
---------------------
``argparse`` with ``type=bool`` is broken for this: ``bool("off")`` is ``True``,
so ``--state off`` would energise the thing the operator was turning off. Every
boolean parameter therefore gets :py:func:`_parse_onoff` with an explicit
choice list, and a test asserts ``off`` arrives as ``False``.
"""

from __future__ import annotations

import argparse
import importlib
import inspect
import json
import os
import typing
from typing import Any, Callable, Optional

from benchctrl import cli_tiers

#: Modules contributing tools, in help-display order. The same list the parity
#: tests walk — a driver missing here is missing from the CLI *and* from the
#: parity check, which is a visible failure rather than a silent one.
TOOL_MODULES: tuple[tuple[str, str], ...] = (
    ("arc", "benchctrl.drivers.otii_arc.mcp_tools"),
    ("qr10x", "benchctrl.drivers.eastwood_qr10x.mcp_tools"),
    ("dl3031a", "benchctrl.drivers.rigol_dl3031a.mcp_tools"),
    ("dp2031", "benchctrl.drivers.rigol_dp2031.mcp_tools"),
    ("sdm4065a", "benchctrl.drivers.siglent_sdm4065a.mcp_tools"),
    ("pdu41002", "benchctrl.drivers.cyberpower_pdu41002.mcp_tools"),
    ("cp2112", "benchctrl.drivers.silabs_cp2112.mcp_tools"),
    ("adu218", "benchctrl.drivers.ontrak_adu218.mcp_tools"),
    ("framework", "benchctrl.framework_tools"),
)

#: Accepted spellings for a boolean, and what they mean. Deliberately short:
#: ``y``/``n`` are omitted because ``n`` next to a numeric argument reads as a
#: typo, and there is no value in accepting six words for one bit.
_TRUE = ("on", "true", "1", "yes", "enable", "enabled")
_FALSE = ("off", "false", "0", "no", "disable", "disabled")

#: What the operator may type. ``on``/``off`` first so they lead the help text.
ONOFF_CHOICES = ("on", "off", "true", "false", "1", "0", "yes", "no")


class CliError(Exception):
    """A CLI-level refusal: a missing gate, an unknown tool, a bad argument.

    Distinct from :py:class:`benchctrl.exceptions.BenchError`, which means the
    *instrument* or the library refused. Keeping them apart is what lets the
    exit codes differ, so a script can tell "you did not authorise this" from
    "the device is not there".
    """


def _parse_onoff(text: str) -> bool:
    """Coerce an on/off word to a bool, refusing anything ambiguous.

    Never ``type=bool``. ``bool("off")`` is ``True`` — a CLI passes strings, so
    a boolean parameter routed through ``bool`` energises on every spelling
    including the ones that mean "no". The PDU driver's ``isinstance(on, bool)``
    guard catches the resulting non-bool, but relying on that would mean the CLI
    is only safe because a driver three layers down is defensive.
    """
    lowered = text.strip().lower()
    if lowered in _TRUE:
        return True
    if lowered in _FALSE:
        return False
    raise argparse.ArgumentTypeError(
        f"expected one of {'/'.join(ONOFF_CHOICES)}, got {text!r}"
    )


def _subcommand_name(tool_name: str, prefix: str) -> str:
    """``pdu41002_set_outlet_state`` -> ``set-outlet-state``.

    The device prefix is dropped because it is already the group: the command
    reads ``benchctrl pdu41002 set-outlet-state``. The Arc's tools carry no
    prefix, so they pass through unchanged.
    """
    stem = tool_name
    if prefix != "arc" and tool_name.startswith(prefix + "_"):
        stem = tool_name[len(prefix) + 1 :]
    return stem.replace("_", "-")


def _summary_line(fn: Callable) -> str:
    """First line of the docstring — the tool description a model sees, reused
    verbatim as ``--help`` text so the two interfaces cannot describe a tool
    differently."""
    doc = inspect.getdoc(fn) or ""
    return doc.strip().split("\n", 1)[0] if doc else ""


def _resolve_hints(fn: Callable) -> dict[str, Any]:
    """Type hints with ``from __future__ import annotations`` resolved.

    Every tool module uses postponed evaluation, so ``__annotations__`` holds
    strings. ``get_type_hints`` evaluates them in the function's own module
    namespace, which is the only place ``Optional`` and the driver types are
    bound.
    """
    try:
        return typing.get_type_hints(fn)
    except Exception:  # pragma: no cover - a tool with an unresolvable hint
        # Better a subcommand whose arguments are all strings than no
        # subcommand at all; the tool still runs, the driver still validates.
        return {}


def _unwrap_optional(hint: Any) -> tuple[Any, bool]:
    """``Optional[int]`` -> ``(int, True)``; anything else -> ``(hint, False)``."""
    origin = typing.get_origin(hint)
    if origin is typing.Union:
        args = [a for a in typing.get_args(hint) if a is not type(None)]
        if len(args) == 1:
            return args[0], True
    return hint, False


def _flag_name(param: str) -> str:
    return "--" + param.replace("_", "-")


def add_tool_arguments(parser: argparse.ArgumentParser, fn: Callable) -> None:
    """Give ``parser`` one argument per tool parameter.

    Required parameters become positionals, optional ones become flags — so the
    common call reads like a command rather than a form. Parameters listed in
    :py:data:`cli_tiers.SUPPRESSED` are skipped entirely and their reason is
    recorded in the parser's epilog, because an operator who cannot find
    ``--no-verify`` deserves to know it was withheld on purpose.
    """
    hints = _resolve_hints(fn)
    suppressed = cli_tiers.suppressed_params(fn.__name__)
    sig = inspect.signature(fn)

    withheld = []
    for name, param in sig.parameters.items():
        if name in suppressed:
            withheld.append(f"{_flag_name(name)}: {suppressed[name]}")
            continue

        hint, _optional = _unwrap_optional(hints.get(name, str))
        required = param.default is inspect.Parameter.empty

        kwargs: dict[str, Any] = {}
        if hint is bool:
            kwargs["type"] = _parse_onoff
            kwargs["metavar"] = "|".join(("on", "off"))
        elif hint is int:
            kwargs["type"] = int
        elif hint is float:
            kwargs["type"] = float
        elif typing.get_origin(hint) is list:
            kwargs["nargs"] = "*"
        # Everything else — str, and the handful of dict/Any parameters —
        # arrives as a string and is validated by the driver, which owns the
        # real vocabulary ("low"/"high", channel codes, and so on). Duplicating
        # those choice lists here would be a second place to get them wrong.

        if required:
            parser.add_argument(name, **kwargs)
        else:
            kwargs["default"] = param.default
            if hint is bool:
                kwargs["help"] = f"(default: {'on' if param.default else 'off'})"
            parser.add_argument(_flag_name(name), dest=name, **kwargs)

    if withheld:
        parser.epilog = "Deliberately not exposed:\n" + "\n".join(
            f"  {w}" for w in withheld
        )
        parser.formatter_class = argparse.RawDescriptionHelpFormatter


def tool_kwargs(fn: Callable, args: argparse.Namespace) -> dict[str, Any]:
    """Collect the parsed values this tool actually takes.

    Filtered by signature rather than passing the whole namespace, so the
    top-level flags (``--remote``, ``--yes``, …) cannot collide with a tool
    parameter of the same name.
    """
    suppressed = cli_tiers.suppressed_params(fn.__name__)
    out = {}
    for name in inspect.signature(fn).parameters:
        if name in suppressed:
            continue
        if hasattr(args, name):
            out[name] = getattr(args, name)
    return out


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def check_gate(
    tool_name: str,
    *,
    yes: bool,
    env: Optional[typing.Mapping[str, str]] = None,
) -> None:
    """Raise :py:class:`CliError` unless this tool's tier is satisfied.

    Checked before the device is opened, so a refusal costs nothing and cannot
    leave a half-configured instrument behind. The tier lookup itself raises on
    an unclassified tool — that is deliberate, see :py:mod:`benchctrl.cli_tiers`.
    """
    environ = os.environ if env is None else env
    tier = cli_tiers.tier_for(tool_name)

    if tier == cli_tiers.READ:
        return

    if tier == cli_tiers.TIER2:
        return

    if not yes:
        raise CliError(
            f"{tool_name} changes what the hardware is doing and needs --yes.\n"
            f"Nothing has been sent to the device."
        )

    if tier == cli_tiers.TIER1_ENV:
        var = cli_tiers.env_gate_for(tool_name)
        if environ.get(var) != "1":
            raise CliError(
                f"{tool_name} needs {var}=1 as well as --yes.\n"
                f"--yes says you meant to run a command; {var} says you know "
                f"what is physically attached. Those are different claims, and "
                f"this operation's consequence outlives the command.\n"
                f"Nothing has been sent to the device."
            )


# ---------------------------------------------------------------------------
# Parser construction
# ---------------------------------------------------------------------------


def iter_tools():
    """Yield ``(group, module_name, fn)`` for every generated subcommand."""
    for group, mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        for fn in mod._TOOLS:
            yield group, mod_name, fn


def add_device_groups(sub: argparse._SubParsersAction) -> dict[str, Callable]:
    """Add one group per device, each with one subcommand per tool.

    Returns a ``{(group, subcommand): fn}`` map keyed by the two names the
    operator types, which is what :py:func:`dispatch` resolves against.
    """
    registry: dict[tuple[str, str], Callable] = {}
    grouped: dict[str, list[Callable]] = {}
    for group, _mod_name, fn in iter_tools():
        grouped.setdefault(group, []).append(fn)

    for group, fns in grouped.items():
        gp = sub.add_parser(
            group,
            help=_GROUP_HELP.get(group, f"{group} tools"),
            description=_GROUP_HELP.get(group, f"{group} tools"),
        )
        gsub = gp.add_subparsers(dest="tool", required=True, metavar="TOOL")
        for fn in fns:
            name = _subcommand_name(fn.__name__, group)
            tier = cli_tiers.tier_for(fn.__name__)
            help_text = _summary_line(fn)
            if tier in (cli_tiers.TIER1, cli_tiers.TIER1_ENV):
                # Marked in the listing, not just in the failure message: an
                # operator scanning `--help` should be able to see which
                # commands move hardware before typing one.
                help_text = f"[{_TIER_MARK[tier]}] {help_text}"
            tp = gsub.add_parser(name, help=help_text, description=inspect.getdoc(fn))
            tp.formatter_class = argparse.RawDescriptionHelpFormatter
            add_tool_arguments(tp, fn)
            # ``_group`` travels with the parsed args because teardown needs it:
            # which device to close is not derivable from the function alone.
            tp.set_defaults(_tool_fn=fn, _group=group)
            registry[(group, name)] = fn

    return registry


_GROUP_HELP = {
    "arc": "Otii Arc source-measure unit",
    "qr10x": "Eastwood QR10x programmable resistance standard",
    "dl3031a": "Rigol DL3031A electronic load",
    "dp2031": "Rigol DP2031 triple-output supply",
    "sdm4065a": "Siglent SDM4065A digital multimeter",
    "pdu41002": "CyberPower PDU41002 switched PDU (switches mains)",
    "cp2112": "SiLabs CP2112 GPIO / DUT reset lines",
    "adu218": "Ontrak ADU218 relays, inputs, counters and watchdog",
    "framework": "recording I/O and battery analytics (no device needed)",
}

_TIER_MARK = {
    cli_tiers.TIER1: "!",
    cli_tiers.TIER1_ENV: "!!",
}


def render_result(value: Any, *, as_json: bool) -> str:
    """Format a tool's return value for a terminal or for a pipe.

    Tools return dicts. ``--json`` emits them verbatim for scripting; the
    default is a flat ``key: value`` listing, because a one-shot command whose
    answer is "17.513" should not make the operator read JSON to find it.
    """
    if as_json:
        return json.dumps(value, indent=2, default=str)
    if isinstance(value, dict):
        return "\n".join(f"{k}: {_render_scalar(v)}" for k, v in value.items())
    return str(value)


def _render_scalar(v: Any) -> str:
    if isinstance(v, bool):
        return "on" if v else "off"
    if isinstance(v, (list, tuple)):
        return ", ".join(_render_scalar(x) for x in v) if v else "-"
    if isinstance(v, dict):
        return json.dumps(v, default=str)
    if v is None:
        return "-"
    return str(v)

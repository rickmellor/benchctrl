"""The generated CLI: reflection, argument shapes, gating, and lifecycle.

Three properties carry this module. Each was a real defect risk, not a
hypothetical:

1. **``off`` must arrive as ``False``.** ``bool("off")`` is ``True``, and a CLI
   passes strings — so ``type=bool`` would energise on every spelling including
   the ones meaning "no". The premise is pinned so this test cannot go vacuous.
2. **The lifecycle table must name tools that exist.** A typo in a ``*_close``
   name is a port left held, and nothing else in the suite would notice.
3. **Teardown must differ by mode.** ``close()`` is refused by the remote proxy,
   so a CLI that always called the close tool would fail every remote command
   at the end, after the write had already landed.
"""

from __future__ import annotations

import argparse
import contextlib
import importlib
import inspect
import sys
import types

import pytest

from benchctrl import cli_generated, cli_lifecycle, cli_tiers
from benchctrl.cli_generated import CliError


def _all_tools() -> dict[str, object]:
    out = {}
    for _group, mod_name in cli_generated.TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        for fn in mod._TOOLS:
            out[fn.__name__] = fn
    return out


# ---------------------------------------------------------------------------
# Reflection: the generated surface is the MCP surface
# ---------------------------------------------------------------------------


def test_every_tool_becomes_exactly_one_subcommand():
    """No tool dropped, none duplicated.

    A duplicate would be worse than a drop: ``add_parser`` with a repeated name
    raises on some Python versions and silently shadows on others, so the tool
    that answers would depend on the interpreter.
    """
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    registry = cli_generated.add_device_groups(sub)

    assert len(registry) == len(_all_tools()), (
        "a tool was dropped or two tools collapsed to one subcommand name"
    )
    assert {fn.__name__ for fn in registry.values()} == set(_all_tools())


def test_the_device_prefix_is_dropped_but_the_arc_is_not_mangled():
    assert cli_generated._subcommand_name("pdu41002_set_outlet_state", "pdu41002") == (
        "set-outlet-state"
    )
    assert cli_generated._subcommand_name("set_voltage", "arc") == "set-voltage"
    assert cli_generated._subcommand_name("state", "arc") == "state"


def test_the_arc_exemption_is_reachable_only_by_a_tool_named_arc_something():
    """The input the guard exists for — which no tool has *yet*.

    ``_subcommand_name`` special-cases ``arc`` because the Arc's tools are
    unprefixed. Dropping that special case is invisible on today's surface: no
    Arc tool is named ``arc_*``, so both branches agree on all 324 tools and a
    test over the real surface cannot tell them apart. The discriminating input
    has to be constructed. With the guard, an Arc tool called ``arc_reset``
    becomes ``arc-reset``; without it, ``reset`` — a name that could then
    collide with a genuinely unprefixed tool.
    """
    assert cli_generated._subcommand_name("arc_reset", "arc") == "arc-reset"
    # ...whereas for a prefixed group the same shape *is* stripped.
    assert cli_generated._subcommand_name("qr10x_reset", "qr10x") == "reset"


def test_subcommand_names_are_unique_within_every_group():
    parser = argparse.ArgumentParser()
    sub = parser.add_subparsers()
    registry = cli_generated.add_device_groups(sub)
    by_group: dict[str, list[str]] = {}
    for group, name in registry:
        by_group.setdefault(group, []).append(name)
    for group, names in by_group.items():
        assert len(names) == len(set(names)), f"duplicate subcommand in {group}"


def test_every_group_has_help_text():
    """An undescribed group in ``--help`` is how a device goes unnoticed."""
    groups = {g for g, _ in cli_generated.TOOL_MODULES}
    assert groups == set(cli_generated._GROUP_HELP)
    for text in cli_generated._GROUP_HELP.values():
        assert text.strip()


# ---------------------------------------------------------------------------
# The bool trap
# ---------------------------------------------------------------------------


def test_bool_of_off_is_true_which_is_why_parse_onoff_exists():
    """The premise, pinned.

    If this ever stops being true the whole ``_parse_onoff`` layer is
    unnecessary, and this test should fail loudly rather than leave a comment
    claiming a hazard that no longer exists.
    """
    assert bool("off") is True
    assert bool("false") is True
    assert bool("0") is True


@pytest.mark.parametrize("word", ["off", "OFF", "false", "0", "no", "disable", " off "])
def test_negative_words_all_parse_to_false(word):
    assert cli_generated._parse_onoff(word) is False


@pytest.mark.parametrize("word", ["on", "ON", "true", "1", "yes", "enable"])
def test_positive_words_all_parse_to_true(word):
    assert cli_generated._parse_onoff(word) is True


@pytest.mark.parametrize("word", ["maybe", "", "2", "-1", "onn", "of"])
def test_ambiguous_words_are_refused_rather_than_guessed(word):
    with pytest.raises(argparse.ArgumentTypeError):
        cli_generated._parse_onoff(word)


def test_a_bool_parameter_reaches_the_tool_as_false_not_as_a_string():
    """End to end through argparse, which is where ``type=bool`` would fail.

    Asserts the *value*, not merely that parsing succeeded: a string ``"off"``
    would also parse fine and then be truthy at the driver.
    """
    parser = argparse.ArgumentParser()
    tools = _all_tools()
    cli_generated.add_tool_arguments(parser, tools["adu218_set_relay_state"])
    args = parser.parse_args(["3", "off"])
    assert args.on is False
    assert not isinstance(args.on, str)

    args = parser.parse_args(["3", "on"])
    assert args.on is True


def test_every_bool_parameter_in_the_whole_surface_uses_the_onoff_parser():
    """Derived, not spot-checked.

    A driver gaining a new boolean parameter is exactly the case a hand-written
    list misses, and the failure mode is energising on ``off``.
    """
    checked = 0
    for name, fn in _all_tools().items():
        hints = cli_generated._resolve_hints(fn)
        for param in inspect.signature(fn).parameters:
            if param in cli_tiers.suppressed_params(name):
                continue
            hint, _opt = cli_generated._unwrap_optional(hints.get(param, str))
            if hint is not bool:
                continue
            checked += 1
            parser = argparse.ArgumentParser()
            cli_generated.add_tool_arguments(parser, fn)
            action = next(
                a for a in parser._actions if a.dest == param
            )
            assert action.type is cli_generated._parse_onoff, (
                f"{name}.{param} is a bool but does not use _parse_onoff"
            )
    assert checked > 10, f"only {checked} bool parameters found — has reflection broken?"


# ---------------------------------------------------------------------------
# Argument shapes
# ---------------------------------------------------------------------------


def test_required_parameters_are_positional_and_optional_ones_are_flags():
    parser = argparse.ArgumentParser()
    cli_generated.add_tool_arguments(parser, _all_tools()["pdu41002_set_outlet_state"])
    # index and on are required; delayed has a default.
    args = parser.parse_args(["4", "off"])
    assert args.index == 4 and args.on is False and args.delayed is False
    args = parser.parse_args(["4", "off", "--delayed", "on"])
    assert args.delayed is True


def test_an_int_parameter_is_coerced_not_left_as_a_string():
    parser = argparse.ArgumentParser()
    cli_generated.add_tool_arguments(parser, _all_tools()["adu218_counter"])
    assert parser.parse_args(["5"]).index == 5


def test_suppressed_parameters_get_no_flag_and_are_named_in_the_epilog():
    """Withheld on purpose, and said so.

    An operator who cannot find ``--verify`` will otherwise assume the CLI is
    incomplete and reach for the Python API, where nothing stops them.
    """
    parser = argparse.ArgumentParser()
    fn = _all_tools()["pdu41002_set_outlet_state"]
    cli_generated.add_tool_arguments(parser, fn)
    assert "verify" not in {a.dest for a in parser._actions}
    assert "--verify" in (parser.epilog or "")
    assert "acknowledges nothing" in (parser.epilog or "")


def test_a_suppressed_parameter_cannot_reach_the_tool_through_the_namespace():
    """Belt and braces: even if something sets ``verify`` on the namespace —
    a future top-level flag, a test, a typo — ``tool_kwargs`` must not forward
    it. Omitting the flag and filtering the value are two different guards, and
    the second is the one that holds if the first is edited."""
    fn = _all_tools()["pdu41002_set_outlet_state"]
    ns = argparse.Namespace(index=1, on=False, delayed=False, verify=False)
    assert cli_generated.tool_kwargs(fn, ns) == {
        "index": 1, "on": False, "delayed": False
    }


def test_top_level_flags_are_not_forwarded_as_tool_arguments():
    """``--json`` is a CLI concern. A tool with a ``json`` parameter would
    otherwise receive it, which is why this filters by signature rather than
    passing the namespace through."""
    fn = _all_tools()["adu218_relay_states"]
    ns = argparse.Namespace(json=True, yes=True, remote="x", port=None)
    assert cli_generated.tool_kwargs(fn, ns) == {}


def test_help_text_comes_from_the_docstring_the_model_also_reads():
    """One description, two interfaces. If these could drift, the CLI and the
    MCP tool could document the same operation differently."""
    fn = _all_tools()["adu218_reset_relays"]
    assert cli_generated._summary_line(fn) == inspect.getdoc(fn).split("\n")[0]


# ---------------------------------------------------------------------------
# Gating
# ---------------------------------------------------------------------------


def test_a_read_needs_no_authorisation():
    cli_generated.check_gate("adu218_relay_states", yes=False, env={})


def test_a_tier2_write_needs_no_confirmation():
    """Deliberate. Requiring ``--yes`` for ``set_voltage`` trains the reflex
    that makes ``--yes`` meaningless on the commands that matter."""
    assert cli_tiers.tier_for("set_voltage") == cli_tiers.TIER2
    cli_generated.check_gate("set_voltage", yes=False, env={})


def test_a_tier1_write_is_refused_without_yes_and_says_nothing_was_sent():
    with pytest.raises(CliError) as exc:
        cli_generated.check_gate("adu218_set_relay_state", yes=False, env={})
    assert "--yes" in str(exc.value)
    # The operator's next question is "did that half-happen?". Answer it.
    assert "Nothing has been sent to the device." in str(exc.value)


def test_a_tier1_write_passes_with_yes():
    cli_generated.check_gate("adu218_set_relay_state", yes=True, env={})


def test_mains_switching_needs_yes_and_the_env_gate_and_yes_alone_is_not_enough():
    with pytest.raises(CliError) as exc:
        cli_generated.check_gate("pdu41002_set_outlet_state", yes=True, env={})
    assert "BENCHCTRL_PDU_ALLOW_SWITCHING" in str(exc.value)
    assert "Nothing has been sent to the device." in str(exc.value)

    cli_generated.check_gate(
        "pdu41002_set_outlet_state",
        yes=True,
        env={"BENCHCTRL_PDU_ALLOW_SWITCHING": "1"},
    )


def test_the_env_gate_is_not_satisfied_by_a_merely_present_variable():
    """``=0`` and ``=""`` must not authorise.

    An env var set to ``0`` is the shape a config file or a CI matrix produces
    when someone means "no", and truthiness on the string would read it as yes —
    the same class of defect as ``bool("off")``.
    """
    for value in ("0", "", "no", "false", "yes", "true"):
        with pytest.raises(CliError):
            cli_generated.check_gate(
                "pdu41002_set_outlet_state",
                yes=True,
                env={"BENCHCTRL_PDU_ALLOW_SWITCHING": value},
            )


def test_one_env_gate_does_not_grant_the_other():
    """Authorising mains switching says nothing about arming a watchdog."""
    with pytest.raises(CliError):
        cli_generated.check_gate(
            "adu218_set_watchdog",
            yes=True,
            env={"BENCHCTRL_PDU_ALLOW_SWITCHING": "1"},
        )
    with pytest.raises(CliError):
        cli_generated.check_gate(
            "pdu41002_set_outlet_state",
            yes=True,
            env={"BENCHCTRL_ADU218_ARM_WATCHDOG": "1"},
        )


def test_an_unclassified_tool_is_refused_rather_than_gated_by_guess():
    with pytest.raises(KeyError):
        cli_generated.check_gate("pdu41002_detonate", yes=True, env={})


def test_every_tool_can_be_gated_without_raising_a_lookup_error():
    """The gate is on the path of every command, so a missing tier is a crash
    rather than a policy failure. Runs the real lookup over the real surface."""
    for name in _all_tools():
        try:
            cli_generated.check_gate(name, yes=True, env={"X": "1"})
        except CliError:
            pass  # a refused env gate is a correct outcome here
        except KeyError as exc:  # pragma: no cover - the failure this catches
            pytest.fail(f"{name} has no tier: {exc}")


def test_a_cli_refusal_is_not_a_bench_error():
    """Distinct types so the exit codes can differ: "you did not authorise
    this" is a different thing for a script to handle than "the device is not
    there"."""
    from benchctrl.exceptions import BenchError

    assert not issubclass(CliError, BenchError)


# ---------------------------------------------------------------------------
# Result rendering
# ---------------------------------------------------------------------------


def test_a_bool_result_renders_as_on_off_not_true_false():
    out = cli_generated.render_result({"armed": False}, as_json=False)
    assert out == "armed: off"


def test_json_mode_is_verbatim_and_machine_readable():
    import json

    value = {"index": 3, "state": False, "note": None}
    assert json.loads(cli_generated.render_result(value, as_json=True)) == value


def test_an_empty_collection_renders_as_a_dash_not_as_nothing():
    """``energised: `` reads as a truncated line; ``energised: -`` reads as an
    answer."""
    assert cli_generated.render_result({"energised": []}, as_json=False) == (
        "energised: -"
    )


def test_a_nested_dict_of_bools_still_renders_as_on_off():
    """The defect this closes, found by running the commands in ``docs/cli.md``.

    A nested dict is the *normal* shape for the per-channel reads, not an edge
    case: ``adu218_relay_states`` returns ``{"relays": {0: False, ...}}``. The
    renderer used to ``json.dumps`` a nested dict, so the primary read on the
    relay device printed

        relays: {"0": false, "1": false, ...}

    — the ``on``/``off`` rendering the module goes out of its way to do never
    reached the values that need it most, and the operator got raw JSON from the
    non-JSON output mode.
    """
    out = cli_generated.render_result(
        {"relays": {0: False, 1: True}, "mask": 2}, as_json=False
    )
    assert out == "relays: 0=off, 1=on\nmask: 2"
    assert "false" not in out and "{" not in out


def test_a_collection_one_level_down_is_bracketed_so_the_separators_do_not_merge():
    """``adu218_input_states`` returns a dict *of lists*: ``{"A": [...], "B": [...]}``.

    Without brackets the two separators are the same character sequence and the
    line cannot be parsed by eye — ``ports: A=off, off, off, B=off`` gives no
    clue where port A ends. This is the case a single-level fix would miss.
    """
    out = cli_generated.render_result(
        {"ports": {"A": [False, True], "B": [True, False]}}, as_json=False
    )
    assert out == "ports: A=[off, on], B=[on, off]"


def test_a_top_level_list_is_not_bracketed():
    """Brackets one level down, not at the top, where the key already delimits
    and they would be noise on every line."""
    assert cli_generated.render_result({"energised": [1, 2]}, as_json=False) == (
        "energised: 1, 2"
    )


def test_the_real_relay_read_renders_without_json_punctuation(monkeypatch):
    """Asserted against the *driver's* actual return value, not a hand-built one.

    The hand-built cases above pin the renderer; this pins the shape. A test
    built only from literals would have kept passing through the original bug,
    because I wrote the literals from the same wrong assumption that produced
    it — that a tool returns a flat dict of scalars.
    """
    from benchctrl import config, session
    from benchctrl.drivers.ontrak_adu218 import mcp_tools

    session.configure(config.build(sim_devices=["ontrak_adu218"]))
    try:
        mcp_tools.adu218_open(disarm_watchdog=False)
        result = mcp_tools.adu218_relay_states()
    finally:
        with contextlib.suppress(Exception):  # teardown
            mcp_tools.adu218_close()
        session.configure(config.Config())

    assert isinstance(result["relays"], dict), (
        "the shape assumed by this test changed; the renderer test above is now "
        "pinning something the driver no longer returns"
    )
    out = cli_generated.render_result(result, as_json=False)
    for punctuation in ('{"', '": ', "false", "true"):
        assert punctuation not in out, f"raw JSON leaked into the plain output: {out}"
    assert "0=off" in out


# ---------------------------------------------------------------------------
# Lifecycle: the table must describe reality
# ---------------------------------------------------------------------------


def test_every_cli_group_has_a_lifecycle_entry_and_vice_versa():
    groups = {g for g, _ in cli_generated.TOOL_MODULES}
    assert groups == set(cli_lifecycle.DEVICE_LIFECYCLE)


def test_every_lifecycle_tool_name_actually_exists():
    """A typo here is a port left held, and nothing else in the suite notices —
    the command succeeds, prints its answer, and leaks the device."""
    for group, entry in cli_lifecycle.DEVICE_LIFECYCLE.items():
        mod = importlib.import_module(entry["module"])
        names = {fn.__name__ for fn in mod._TOOLS}
        for role in ("open", "close"):
            tool = entry[role]
            if tool is None:
                continue
            assert tool in names, (
                f"{group}: {role} tool {tool!r} is not in {entry['module']}._TOOLS"
            )


def test_every_lifecycle_device_key_is_a_real_device_key():
    from benchctrl.config import DEVICE_KEYS

    for group, entry in cli_lifecycle.DEVICE_LIFECYCLE.items():
        key = entry["device_key"]
        if key is None:
            continue
        assert key in DEVICE_KEYS, f"{group}: {key!r} is not in config.DEVICE_KEYS"


def test_the_lifecycle_module_matches_the_generator_module():
    """Two tables naming the same modules is a drift risk; assert they agree
    rather than hoping."""
    gen = dict(cli_generated.TOOL_MODULES)
    for group, entry in cli_lifecycle.DEVICE_LIFECYCLE.items():
        assert entry["module"] == gen[group]


def test_the_arc_has_no_open_tool_and_closes_by_disconnect():
    """Measured, not assumed. The Arc opens lazily on first call, so a lifecycle
    layer that insisted on an ``arc_open`` would crash on every Arc command."""
    entry = cli_lifecycle.lifecycle_for("arc")
    assert entry["open"] is None
    assert entry["close"] == "disconnect"
    names = {
        fn.__name__
        for fn in importlib.import_module(entry["module"])._TOOLS
    }
    assert "open" not in names and "arc_open" not in names


def test_the_framework_group_has_no_device_to_open_or_close():
    entry = cli_lifecycle.lifecycle_for("framework")
    assert entry["device_key"] is None
    assert entry["open"] is None and entry["close"] is None
    assert cli_lifecycle.teardown("framework", mode="local") is None


def test_an_unknown_group_raises_naming_the_table():
    with pytest.raises(KeyError) as exc:
        cli_lifecycle.lifecycle_for("nosuchdevice")
    assert "DEVICE_LIFECYCLE" in exc.value.args[0]
    assert "nosuchdevice" in exc.value.args[0]


# ---------------------------------------------------------------------------
# The one-shot watchdog conflict
# ---------------------------------------------------------------------------


def test_the_driver_default_would_disarm_a_watchdog_on_a_read():
    """The premise for the override, pinned to the driver's own signature.

    ``adu218_open`` sends ``WD0`` when ``disarm_watchdog`` is true, and every
    CLI invocation opens. So without the override:

        benchctrl adu218 set-watchdog 3   # arms
        benchctrl adu218 relay-states     # a read — and disarms it

    If the driver default ever changes, this fails and the override becomes
    redundant rather than silently wrong.
    """
    fn = _all_tools()["adu218_open"]
    assert inspect.signature(fn).parameters["disarm_watchdog"].default is True


def test_the_cli_overrides_it_so_a_read_leaves_an_armed_watchdog_armed():
    kwargs = cli_lifecycle.open_kwargs_for("adu218_open", "ontrak_adu218")
    assert kwargs["disarm_watchdog"] is False


def test_the_override_wins_over_bench_config():
    """A per-bench file must not be able to reintroduce the hazard: whether a
    process is one-shot is not a fact a config file knows."""
    bench = {"ontrak_adu218": {"disarm_watchdog": True, "serial": "ABC"}}
    kwargs = cli_lifecycle.open_kwargs_for("adu218_open", "ontrak_adu218", bench)
    assert kwargs["disarm_watchdog"] is False
    assert kwargs["serial"] == "ABC", "config must still supply everything else"


def test_no_other_device_gets_a_silent_open_override():
    """Each override contradicts a driver default that is correct in its own
    context, so each needs its own justification. One entry, deliberately."""
    assert set(cli_lifecycle.ONE_SHOT_OPEN_OVERRIDES) == {"adu218_open"}


def test_open_kwargs_are_empty_when_there_is_no_config_and_no_override():
    assert cli_lifecycle.open_kwargs_for("qr10x_open", "eastwood_qr10x") == {}


# ---------------------------------------------------------------------------
# Teardown: mode-dependent, and it must not swallow a failed de-energise
# ---------------------------------------------------------------------------


@pytest.fixture
def fake_group(monkeypatch):
    """A CLI group backed by a recording stub module.

    Real drivers cannot be used here: the point is to observe *which* calls the
    lifecycle makes and in what order, and a simulator would answer them all
    successfully either way.
    """
    calls: list[tuple[str, dict]] = []

    mod = types.ModuleType("benchctrl._fake_lifecycle_device")

    def fake_open(**kwargs):
        calls.append(("open", kwargs))
        return {"opened": True}

    def fake_close(**kwargs):
        calls.append(("close", kwargs))
        return {"closed": True}

    def fake_read(**kwargs):
        calls.append(("read", kwargs))
        return {"value": 1.0}

    fake_open.__name__ = "fake_open"
    fake_close.__name__ = "fake_close"
    fake_read.__name__ = "fake_read"
    mod.fake_open = fake_open
    mod.fake_close = fake_close
    mod.fake_read = fake_read
    mod._TOOLS = (fake_open, fake_close, fake_read)
    monkeypatch.setitem(sys.modules, mod.__name__, mod)

    entry = {
        "device_key": "ontrak_adu218",  # a real key so mode_for works
        "module": mod.__name__,
        "open": "fake_open",
        "close": "fake_close",
    }
    monkeypatch.setitem(cli_lifecycle.DEVICE_LIFECYCLE, "fake", entry)
    return types.SimpleNamespace(calls=calls, mod=mod, entry=entry)


def test_a_command_opens_the_device_then_closes_it(fake_group, monkeypatch):
    monkeypatch.setattr(cli_lifecycle, "open_kwargs_for", lambda *a, **k: {})
    result, warning = cli_lifecycle.run_tool("fake", fake_group.mod.fake_read, {})
    assert result == {"value": 1.0}
    assert warning is None
    assert [name for name, _ in fake_group.calls] == ["open", "read", "close"]


def test_the_open_command_itself_is_not_opened_twice(fake_group):
    """Every driver's ``open`` refuses when already open, so a pre-open would
    turn ``benchctrl qr10x open`` — the way an operator checks a device is
    reachable — into a guaranteed error."""
    cli_lifecycle.run_tool("fake", fake_group.mod.fake_open, {})
    assert [name for name, _ in fake_group.calls].count("open") == 1


def test_the_close_command_itself_neither_opens_first_nor_closes_twice(fake_group):
    """``benchctrl adu218 close`` must be exactly one close and no open.

    Two distinct defects hide here and counting closes alone catches only one:

    * closing twice makes the command report ``was not open``, having just
      closed it;
    * opening first makes ``close`` *connect to the device in order to
      disconnect* — which on the ADU218 means an open that sends ``WD0``, so
      asking to release the device would disarm a watchdog on the way out.

    So the whole call sequence is asserted, not a count.
    """
    result, _ = cli_lifecycle.run_tool("fake", fake_group.mod.fake_close, {})
    assert [name for name, _ in fake_group.calls] == ["close"]
    assert result == {"closed": True}


def test_the_device_is_closed_even_when_the_command_raises(fake_group):
    """An exception mid-command is exactly when a port must not be left held —
    and the original exception must still be what propagates."""

    def boom(**kwargs):
        fake_group.calls.append(("boom", kwargs))
        raise RuntimeError("instrument said no")

    boom.__name__ = "boom"
    with pytest.raises(RuntimeError, match="instrument said no"):
        cli_lifecycle.run_tool("fake", boom, {})
    assert [name for name, _ in fake_group.calls] == ["open", "boom", "close"]


def test_remote_teardown_is_a_session_shutdown_and_never_the_close_tool(
    fake_group, monkeypatch
):
    """``close()`` is refused by the proxy, so calling the close tool remotely
    fails *after* the write has landed — the command would report an error for
    an operation that succeeded."""
    from benchctrl import session

    shutdowns = []
    monkeypatch.setattr(session, "shutdown", lambda: shutdowns.append(1))

    assert cli_lifecycle.teardown("fake", mode="remote") is None
    assert shutdowns == [1]
    assert [name for name, _ in fake_group.calls] == [], (
        "the close tool must not be called in remote mode"
    )


def test_local_and_sim_teardown_both_call_the_close_tool(fake_group):
    for mode in ("local", "sim"):
        fake_group.calls.clear()
        assert cli_lifecycle.teardown("fake", mode=mode) == {"closed": True}
        assert [name for name, _ in fake_group.calls] == ["close"]


def test_the_mode_is_resolved_once_so_teardown_cannot_disagree_with_the_open(
    fake_group, monkeypatch
):
    """If ``run_tool`` re-read the mode after the call, a config change mid-flight
    would close a remote device with the local path — the failure the proxy
    refusal exists to prevent."""
    from benchctrl import session

    seen = []
    monkeypatch.setattr(session, "mode_for", lambda key: seen.append(key) or "local")
    cli_lifecycle.run_tool("fake", fake_group.mod.fake_read, {})
    assert seen == ["ontrak_adu218"], "mode_for should be consulted exactly once"


# --- the failure that arrives as a dict key, not an exception ---------------


def test_a_failed_load_input_disable_is_reported_not_swallowed():
    """``dl3031a_close`` reports a failed de-energise in its *result*.

    A teardown that discarded the result would print the measurement and exit 0
    with the load still sinking current. The literal key is the driver's, copied
    from ``rigol_dl3031a/mcp_tools.py``.
    """
    note = cli_lifecycle.describe_teardown_failure(
        {"closed": True, "input_off_failed": "TimeoutError: no response"}
    )
    assert note is not None
    assert "sinking current" in note
    assert "TimeoutError: no response" in note, "the driver's own detail must survive"


def test_a_failed_output_disable_is_reported():
    note = cli_lifecycle.describe_teardown_failure(
        {"closed": True, "outputs_off_failed": ["CH1: TimeoutError"]}
    )
    assert note is not None and "CH1" in note


def test_a_clean_close_produces_no_warning():
    assert cli_lifecycle.describe_teardown_failure({"closed": True}) is None
    assert cli_lifecycle.describe_teardown_failure(None) is None
    assert cli_lifecycle.describe_teardown_failure("closed") is None


def test_a_falsy_failure_value_is_not_a_failure():
    """The drivers set these keys only on failure, but an empty list of failed
    channels means nothing failed — reporting it would be a warning nobody can
    act on, which is how real warnings get ignored."""
    assert cli_lifecycle.describe_teardown_failure({"outputs_off_failed": []}) is None
    assert cli_lifecycle.describe_teardown_failure({"input_off_failed": ""}) is None


def test_the_failure_keys_are_the_ones_the_drivers_actually_set():
    """Derived from the driver sources, because a renamed key silently disables
    this whole path — the CLI would go back to exiting 0 on a live output."""
    from pathlib import Path

    root = Path(cli_lifecycle.__file__).parent / "drivers"
    sources = "\n".join(
        p.read_text() for p in root.glob("*/mcp_tools.py")
    )
    for key in cli_lifecycle.TEARDOWN_FAILURE_KEYS:
        assert f'"{key}"' in sources, (
            f"{key!r} is no longer set by any driver — the teardown warning is "
            f"inert and a failed de-energise would exit 0"
        )


def test_a_raising_close_is_reported_as_a_failure_not_as_silence(fake_group):
    """``dl3031a_close`` disables the input *before* closing, so an exception
    can mean the de-energise never happened. Silence and success must not look
    the same."""

    def angry_close(**kwargs):
        raise OSError("port vanished")

    angry_close.__name__ = "fake_close"
    fake_group.mod.fake_close = angry_close

    result = cli_lifecycle.teardown("fake", mode="local")
    note = cli_lifecycle.describe_teardown_failure(result)
    assert note is not None
    assert "port vanished" in note


def test_a_failed_remote_shutdown_is_reported(monkeypatch, fake_group):
    """A held claim on an armed device is the difference between the grace
    period and the full deadman window."""
    from benchctrl import session

    def angry(**kwargs):
        raise OSError("socket already gone")

    monkeypatch.setattr(session, "shutdown", angry)
    note = cli_lifecycle.describe_teardown_failure(
        cli_lifecycle.teardown("fake", mode="remote")
    )
    assert note is not None and "socket already gone" in note


def test_a_teardown_failure_does_not_replace_the_commands_own_result(fake_group):
    """The operator asked for a measurement. They still get it — plus the
    warning. Losing the reading to a teardown problem would make the CLI
    unusable exactly when the bench is misbehaving."""

    def failing_close(**kwargs):
        return {"closed": True, "input_off_failed": "TimeoutError"}

    failing_close.__name__ = "fake_close"
    fake_group.mod.fake_close = failing_close

    result, warning = cli_lifecycle.run_tool("fake", fake_group.mod.fake_read, {})
    assert result == {"value": 1.0}
    assert warning is not None and "TimeoutError" in warning

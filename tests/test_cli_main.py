"""``main()``: exit codes, flag wiring, and the legacy commands still working.

Exit codes are the CLI's contract with a shell script, so they are asserted
rather than assumed. The one that needed inventing is **4**: a command that ran
fine but whose *teardown* could not de-energise. ``dl3031a_close`` reports that
in its result dict, not as an exception, so without a distinct code the CLI would
print the measurement and exit 0 with the load still sinking current.

The legacy Arc commands are covered here too. They predate all of this and are
documented in ``docs/getting_started.md``; adding nine device groups to the same
parser must not have moved them.
"""

from __future__ import annotations

import argparse

import pytest

from benchctrl import cli, cli_lifecycle
from benchctrl.exceptions import BenchError


def test_the_parser_builds_with_every_group_and_the_legacy_commands():
    """One assertion over the whole surface: 324 generated subcommands plus the
    ten hand-written ones, in one parser, with no name collision."""
    parser = cli.build_parser()
    sub = next(a for a in parser._actions if isinstance(a, argparse._SubParsersAction))
    names = set(sub.choices)
    # Legacy, documented since v0.1.
    for legacy in (
        "discover", "info", "set-voltage", "set-output", "set-range",
        "set-current-limit", "set-exp-voltage", "set-gpo", "capture", "stream",
    ):
        assert legacy in names, f"legacy command {legacy} disappeared"
    # Generated groups.
    for group in ("arc", "qr10x", "dl3031a", "dp2031", "sdm4065a",
                  "pdu41002", "cp2112", "adu218", "framework"):
        assert group in names, f"device group {group} missing"


def test_a_legacy_command_still_resolves_to_its_own_handler():
    """The legacy commands carry ``func``; generated ones carry ``_tool_fn``.
    ``main`` dispatches on that difference, so a legacy command that lost its
    ``func`` would silently be treated as a generated one."""
    args = cli.build_parser().parse_args(["set-voltage", "3.3"])
    assert args.func is cli.cmd_set_voltage
    assert not hasattr(args, "_tool_fn")


def test_a_generated_command_carries_its_tool_and_its_group():
    args = cli.build_parser().parse_args(["adu218", "relay-states"])
    assert args._tool_fn.__name__ == "adu218_relay_states"
    assert args._group == "adu218"
    assert not hasattr(args, "func")


def test_the_legacy_and_generated_paths_do_not_collide_on_set_voltage():
    """``benchctrl set-voltage`` is the legacy Arc command; ``benchctrl arc
    set-voltage`` is the generated one. Both must exist and be different."""
    legacy = cli.build_parser().parse_args(["set-voltage", "3.3"])
    generated = cli.build_parser().parse_args(["arc", "set-voltage", "3.3"])
    assert legacy.func is cli.cmd_set_voltage
    assert generated._tool_fn.__name__ == "set_voltage"


# ---------------------------------------------------------------------------
# The flags docs/remote.md has documented all along
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "argv,attr,expected",
    [
        (["--remote", "board:9737", "adu218", "info"], "remote", "board:9737"),
        (["--sim", "ontrak_adu218", "adu218", "info"], "sim", ["ontrak_adu218"]),
        (["--local", "otii_arc", "adu218", "info"], "local", ["otii_arc"]),
        (["--yes", "adu218", "info"], "yes", True),
        (["--json", "adu218", "info"], "json", True),
    ],
)
def test_the_top_level_flags_parse(argv, attr, expected):
    assert getattr(cli.build_parser().parse_args(argv), attr) == expected


def test_sim_and_local_are_repeatable():
    """A bench is rarely all-one-mode: "Arc on the board, Rigols here" needs two
    of the same flag."""
    args = cli.build_parser().parse_args(
        ["--sim", "otii_arc", "--sim", "ontrak_adu218", "adu218", "info"]
    )
    assert args.sim == ["otii_arc", "ontrak_adu218"]


def test_the_flags_actually_reach_session_rather_than_parsing_and_being_ignored(
    monkeypatch,
):
    """The defect this closes.

    ``docs/remote.md`` documented ``--remote``/``--local``/``--sim`` as CLI flags
    while only ``benchctrl-agent`` had them. A flag that parses and is discarded
    is worse than a missing one: a device asked to be *simulated* would drive the
    real instrument. So this asserts the config reaches ``session``, not merely
    that the flag parsed.
    """
    from benchctrl import session

    captured = {}

    def fake_configure_from_environment(**kwargs):
        captured.update(kwargs)
        return session.current_config()

    monkeypatch.setattr(
        session, "configure_from_environment", fake_configure_from_environment
    )
    args = cli.build_parser().parse_args(["--sim", "ontrak_adu218", "adu218", "info"])
    cli._install_session_config(args)

    assert "cli" in captured, "the CLI flags never reached session"
    assert captured["cli"].mode_for("ontrak_adu218") == "sim"


def test_no_flags_means_no_cli_config_so_the_documented_precedence_holds(monkeypatch):
    """With no flags the CLI must pass ``cli=None`` — otherwise an empty Config
    would count as an override and beat the environment and the config file,
    inverting the precedence documented in ``config.py``."""
    from benchctrl import session

    captured = {}
    monkeypatch.setattr(
        session,
        "configure_from_environment",
        lambda **kw: captured.update(kw) or session.current_config(),
    )
    cli._install_session_config(cli.build_parser().parse_args(["adu218", "info"]))
    assert captured == {"cli": None}


def test_a_non_local_binding_is_announced_on_stderr(monkeypatch, capsys):
    """"Why did that read a simulator" is the question this line answers. On
    stderr so it never contaminates a ``--json`` pipe."""
    from benchctrl import config, session

    monkeypatch.setattr(
        session,
        "configure_from_environment",
        lambda **kw: config.build(sim_devices=["ontrak_adu218"]),
    )
    cli._install_session_config(cli.build_parser().parse_args(["adu218", "info"]))
    err = capsys.readouterr().err
    assert "ontrak_adu218 -> sim" in err


def test_an_all_local_bench_says_nothing(monkeypatch, capsys):
    """Silence is the correct output when nothing was rebound — a line per
    device would train the operator to ignore the one that matters."""
    from benchctrl import config, session

    monkeypatch.setattr(session, "configure_from_environment", lambda **kw: config.Config())
    cli._install_session_config(cli.build_parser().parse_args(["adu218", "info"]))
    assert capsys.readouterr().err == ""


# ---------------------------------------------------------------------------
# Exit codes
# ---------------------------------------------------------------------------


def _stub_run(monkeypatch, result, warning):
    monkeypatch.setattr(
        cli_lifecycle, "run_tool", lambda *a, **k: (result, warning)
    )
    monkeypatch.setattr(cli, "_install_session_config", lambda args: None)


def test_a_successful_command_exits_zero_and_prints_the_result(
    monkeypatch, capsys
):
    _stub_run(monkeypatch, {"mask": 0}, None)
    assert cli.main(["adu218", "relay-states"]) == 0
    assert "mask: 0" in capsys.readouterr().out


def test_a_missing_authorisation_exits_3_and_never_calls_the_tool(
    monkeypatch, capsys
):
    """Code 3 is distinct from 2 so a script can tell "you did not authorise
    this" from "the device is not there" — and the tool must not have run."""
    called = []
    monkeypatch.setattr(
        cli_lifecycle, "run_tool", lambda *a, **k: called.append(1) or ({}, None)
    )
    rc = cli.main(["adu218", "set-relay-state", "0", "on"])
    assert rc == 3
    assert called == [], "the tool ran despite the refusal"
    err = capsys.readouterr().err
    assert "--yes" in err and "Nothing has been sent to the device." in err


def test_the_env_gate_also_exits_3(monkeypatch, capsys):
    monkeypatch.delenv("BENCHCTRL_PDU_ALLOW_SWITCHING", raising=False)
    monkeypatch.setattr(cli_lifecycle, "run_tool", lambda *a, **k: ({}, None))
    rc = cli.main(["--yes", "pdu41002", "set-outlet-state", "1", "off"])
    assert rc == 3
    assert "BENCHCTRL_PDU_ALLOW_SWITCHING" in capsys.readouterr().err


def test_a_device_refusal_exits_2(monkeypatch, capsys):
    def boom(*a, **k):
        raise BenchError("no such port")

    monkeypatch.setattr(cli_lifecycle, "run_tool", boom)
    monkeypatch.setattr(cli, "_install_session_config", lambda args: None)
    assert cli.main(["adu218", "relay-states"]) == 2
    assert "no such port" in capsys.readouterr().err


# --- the eight driver hierarchies that are not BenchError -------------------


def test_no_driver_exception_subclasses_bench_error():
    """The premise, measured. This is why ``device_error_types`` exists.

    Every driver's exceptions descend straight from ``RuntimeError``, so
    ``except BenchError`` does not catch a single one of them. If that is ever
    fixed at the source this test fails, and the CLI's registry-walk becomes
    redundant rather than silently load-bearing for nothing.
    """
    from benchctrl.drivers.cyberpower_pdu41002.driver import PDU41002Error
    from benchctrl.drivers.ontrak_adu218.driver import ADU218Error

    assert not issubclass(ADU218Error, BenchError)
    assert not issubclass(PDU41002Error, BenchError)


@pytest.mark.parametrize(
    "module,name",
    [
        ("benchctrl.drivers.ontrak_adu218.driver", "ADU218ConnectionError"),
        ("benchctrl.drivers.cyberpower_pdu41002.driver", "PDU41002PolicyError"),
        ("benchctrl.drivers.silabs_cp2112.driver", "CP2112VerifyError"),
        ("benchctrl.drivers.eastwood_qr10x.driver", "QR10xTimeoutError"),
        ("benchctrl.drivers.rigol_dl3031a.driver", "RigolDLConnectionError"),
        ("benchctrl.drivers.rigol_dp2031.driver", "RigolDP2031ValueError"),
        ("benchctrl.drivers.siglent_sdm4065a.driver", "SDM4065AOverloadError"),
    ],
)
def test_a_driver_exception_exits_2_rather_than_escaping_as_a_traceback(
    monkeypatch, capsys, module, name
):
    """The defect this closes.

    An unplugged device is the single most common CLI failure. Before this, it
    escaped ``main()`` uncaught: a Python traceback and exit code **1**, which is
    already what ``discover`` returns for "found nothing". A script could not
    tell "no device" from "device says no", and neither could a person.
    """
    import importlib

    cls = getattr(importlib.import_module(module), name)

    def boom(*a, **k):
        raise cls("device is not there")

    monkeypatch.setattr(cli_lifecycle, "run_tool", boom)
    monkeypatch.setattr(cli, "_install_session_config", lambda args: None)
    assert cli.main(["adu218", "relay-states"]) == 2
    err = capsys.readouterr().err
    assert "device is not there" in err
    # The class name is most of the diagnosis when eight hierarchies exist.
    assert name in err


def test_every_driver_error_in_the_wire_registry_is_caught_by_the_cli():
    """Derived from ``net/errors.py``, so a new driver is covered the moment its
    exceptions are made remote-safe — rather than needing a second hand-kept
    list that would be remembered once and then not."""
    from benchctrl.net import errors

    caught = cli.device_error_types()
    missed = [
        name
        for name, cls in errors._registry().items()
        if cls.__module__.startswith("benchctrl.")
        and not issubclass(cls, caught)
    ]
    assert missed == [], f"these would escape main() as a traceback: {missed}"


def test_the_cli_does_not_catch_bare_builtin_exceptions():
    """A bug in the CLI must not be reported as though the instrument refused.

    ``net/errors._registry()`` deliberately contains ``ValueError``, ``OSError``
    and friends so they survive the wire. Catching them here would turn a typo
    in the CLI into "benchctrl error: KeyError", sending the operator to check
    their cabling.
    """
    caught = cli.device_error_types()
    for builtin in (ValueError, TypeError, OSError, RuntimeError, KeyError,
                    AttributeError, TimeoutError, NotImplementedError):
        assert builtin not in caught
        assert not issubclass(AssertionError, caught)


def test_a_cli_refusal_is_checked_before_the_device_error_handler():
    """``PolicyError`` is a ``BenchValueError`` and lives in the same registry,
    so ordering decides the exit code. A refused authorisation must be 3, not 2 —
    otherwise the two cases a script most needs to distinguish collapse."""
    from benchctrl.net.errors import PolicyError

    assert issubclass(PolicyError, BenchError)
    # And a genuine CliError still wins.
    from benchctrl.cli_generated import CliError

    assert not issubclass(CliError, tuple(cli.device_error_types()))


# --- config failures must not degrade silently to local ---------------------


def test_a_malformed_config_file_fails_rather_than_falling_back_to_local(
    tmp_path, monkeypatch, capsys
):
    """The dangerous failure mode is a *silent* one: a config that cannot be
    read, treated as "no config", means every device resolves local — so a
    device the operator asked to simulate drives the real instrument."""
    bad = tmp_path / "config.json"
    bad.write_text('{"devices": {"ontrak_adu218": {"mode": "sim"')
    monkeypatch.setenv("BENCHCTRL_CONFIG", str(bad))
    assert cli.main(["adu218", "relay-states"]) == 2
    assert "could not read config" in capsys.readouterr().err


def test_an_invalid_mode_in_config_fails_loudly(tmp_path, monkeypatch, capsys):
    bad = tmp_path / "config.json"
    bad.write_text('{"devices": {"ontrak_adu218": {"mode": "smi"}}}')
    monkeypatch.setenv("BENCHCTRL_CONFIG", str(bad))
    assert cli.main(["adu218", "relay-states"]) == 2
    err = capsys.readouterr().err
    assert "smi" in err and "local" in err, "the message should name the valid modes"


def test_open_arguments_come_from_the_config_the_server_already_uses(
    monkeypatch,
):
    """One source of truth. A bench whose PDU is at a fixed address should not
    have to say so once for the MCP server and again for the CLI — two files
    would eventually disagree about which outlets are allowed."""
    from benchctrl import config

    cfg = config.Config(
        devices={
            "cyberpower_pdu41002": config.DeviceConfig(
                open={"host": "pdu-benchctrl", "allowed_outlets": [4]}
            )
        }
    )
    bench = cli_lifecycle.bench_open_kwargs(cfg)
    assert bench["cyberpower_pdu41002"] == {
        "host": "pdu-benchctrl", "allowed_outlets": [4]
    }
    # Devices with nothing configured must not appear as empty dicts, or every
    # open would be handed a kwargs dict it has to ignore.
    assert "otii_arc" not in bench


def test_bench_open_kwargs_is_empty_when_nothing_is_configured():
    from benchctrl import config

    assert cli_lifecycle.bench_open_kwargs(config.Config()) == {}


def test_the_configured_open_arguments_actually_reach_the_open_call(monkeypatch):
    """``bench_open_kwargs`` being correct is not enough — it has to be *passed*.

    Without this, ``bench=None`` at the call site is invisible: the config layer
    tests all still pass, and the symptom is a PDU that ignores its configured
    host and allowlist. For the PDU specifically, an ignored ``allowed_outlets``
    means the driver falls back to its own default rather than the operator's
    narrower list.
    """
    captured = {}
    monkeypatch.setattr(
        cli_lifecycle,
        "run_tool",
        lambda group, fn, kwargs, **kw: captured.update(kw) or ({}, None),
    )
    monkeypatch.setattr(cli, "_install_session_config", lambda args: None)
    monkeypatch.setattr(
        cli_lifecycle,
        "bench_open_kwargs",
        lambda *a: {"cyberpower_pdu41002": {"allowed_outlets": [4]}},
    )
    assert cli.main(["pdu41002", "outlet-states"]) == 0
    assert captured.get("bench") == {"cyberpower_pdu41002": {"allowed_outlets": [4]}}, (
        "the configured open arguments never reached run_tool"
    )


def test_a_refused_command_produces_no_other_output_because_the_gate_runs_first(
    monkeypatch, capsys
):
    """Ordering, asserted rather than commented.

    The gate is checked before anything is configured or opened. Today the only
    visible consequence is that a refusal is not preceded by a "-> sim" binding
    line, which is cosmetic — but the ordering is what guarantees a refusal
    cannot leave a half-open port behind, and that stops being cosmetic the
    moment anything before the call acquires a resource.
    """
    monkeypatch.setattr(
        cli, "_install_session_config",
        lambda args: pytest.fail("config was installed before the gate refused"),
    )
    monkeypatch.setattr(
        cli_lifecycle, "run_tool",
        lambda *a, **k: pytest.fail("the tool ran despite the refusal"),
    )
    assert cli.main(["--sim", "ontrak_adu218", "adu218", "set-relay-state", "0", "on"]) == 3
    out = capsys.readouterr()
    assert out.out == "", "a refused command printed to stdout"
    assert "-> sim" not in out.err


def test_a_teardown_that_could_not_de_energise_exits_4_but_still_prints_the_result(
    monkeypatch, capsys
):
    """Both halves matter.

    The non-zero code is what stops a CI job continuing with a live output. The
    printed result is what the operator asked for — losing the reading to a
    teardown problem would make the CLI unusable exactly when the bench is
    misbehaving.
    """
    _stub_run(monkeypatch, {"volts": 3.3}, "teardown could not ... sinking current")
    rc = cli.main(["dl3031a", "measure-voltage"])
    out = capsys.readouterr()
    assert rc == 4
    assert "volts: 3.3" in out.out
    assert "sinking current" in out.err


def test_the_teardown_warning_goes_to_stderr_so_json_stays_parseable(
    monkeypatch, capsys
):
    """A warning on stdout would corrupt ``--json``, and the caller most likely
    to be piping JSON is the one least likely to notice."""
    import json

    _stub_run(monkeypatch, {"volts": 3.3}, "teardown could not de-energise")
    assert cli.main(["--json", "dl3031a", "measure-voltage"]) == 4
    out = capsys.readouterr()
    assert json.loads(out.out) == {"volts": 3.3}
    assert "de-energise" in out.err


def test_an_interrupt_exits_130(monkeypatch, capsys):
    def boom(*a, **k):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli_lifecycle, "run_tool", boom)
    monkeypatch.setattr(cli, "_install_session_config", lambda args: None)
    assert cli.main(["adu218", "relay-states"]) == 130


def test_the_documented_exit_codes_are_the_ones_main_can_return():
    """The module docstring is the contract a script reads. Keeping the table
    and the code in agreement is the point — a stale table is worse than none.
    """
    doc = cli.__doc__
    for code, meaning in (
        ("0", "the command ran"),
        ("2", "BenchError"),
        ("3", "CliError"),
        ("4", "de-energise"),
        ("130", "interrupted"),
    ):
        assert code in doc and meaning in doc, f"exit code {code} undocumented"


def test_verbose_turns_on_logging_without_it_being_on_by_default(monkeypatch):
    """Teardown failures are logged as well as returned, so a ``-v`` run shows
    them even on the exception path where the warning is dropped."""
    parser = cli.build_parser()
    assert parser.parse_args(["adu218", "info"]).verbose is False
    assert parser.parse_args(["-v", "adu218", "info"]).verbose is True

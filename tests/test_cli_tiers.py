"""The CLI's write-risk classification is complete, explicit, and not derived.

These tests are the reason the generated CLI can be trusted. The generator
reflects over ``_TOOLS`` tuples, so subcommands appear automatically when a
driver gains a tool — which means a *gate* must not be generated the same way,
or a new tool would ship with whatever risk level its name happened to imply.

The load-bearing test is :py:func:`test_every_tool_has_a_tier`: adding a tool
without classifying it fails the suite.
"""

from __future__ import annotations

import pytest

from benchctrl import cli_tiers
from benchctrl.cli_tiers import READ, TIER1, TIER1_ENV, TIER2, TOOL_TIERS


def _all_tool_names() -> list[str]:
    """Every generated subcommand, by the same route the CLI uses."""
    import importlib

    from tests.test_mcp import TOOL_MODULES

    names = []
    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        names += [fn.__name__ for fn in mod._TOOLS]
    return names


def test_every_tool_has_a_tier():
    """Completeness, in both directions.

    An unclassified tool is the failure this whole module exists to prevent: it
    would become a CLI subcommand whose risk nobody assessed. A *stale* entry
    matters too — it means the table is being maintained by accretion, and a
    reader can no longer trust that an entry corresponds to a real tool.
    """
    tools = set(_all_tool_names())
    tiered = set(TOOL_TIERS)

    assert sorted(tools - tiered) == [], (
        "generated CLI subcommands with no tier — classify each one in "
        "benchctrl.cli_tiers.TOOL_TIERS with the reasoning"
    )
    assert sorted(tiered - tools) == [], (
        "tiers for tools that no longer exist — a stale table hides the "
        "entries that matter"
    )


def test_tier_values_are_all_known():
    unknown = {name: t for name, t in TOOL_TIERS.items() if t not in cli_tiers.TIERS}
    assert unknown == {}


def test_tier_for_raises_on_an_unclassified_tool():
    """Not a default.

    Defaulting to READ ships a write ungated. Defaulting to TIER1 is safe but
    silent — the omission surfaces as a confirmation prompt on a read, which
    reads as a bug in the wrong place. So it raises, naming the table.
    """
    with pytest.raises(KeyError) as exc:
        cli_tiers.tier_for("pdu41002_detonate")
    # Assert on args, not str(): KeyError's repr adds quotes and would make a
    # containment check pass on a message that never mentions the table.
    assert "TOOL_TIERS" in exc.value.args[0]
    assert "pdu41002_detonate" in exc.value.args[0]


def test_tier_for_returns_the_table_entry():
    assert cli_tiers.tier_for("pdu41002_set_outlet_state") == TIER1_ENV
    assert cli_tiers.tier_for("sdm4065a_measure_dc_voltage") == READ


# ---------------------------------------------------------------------------
# The classification is not dispatch.is_mutator() — and must not become it
# ---------------------------------------------------------------------------


def test_raw_scpi_escapes_are_tier1_although_is_mutator_calls_them_reads():
    """The measurement that killed the original design.

    ``dispatch.is_mutator`` is a bare name-prefix match, and the raw-SCPI
    escapes match no prefix — so a CLI gate derived from it would file
    "send arbitrary bytes to an instrument" under no-confirmation. This test
    pins both halves: the predicate really does say False, *and* the table
    says TIER1 anyway. If a future prefix change makes the predicate agree,
    the first assertion fails and this test needs rewriting rather than
    silently becoming vacuous.
    """
    from benchctrl.agent import dispatch

    for method, tool in (("write", "sdm4065a_write"), ("query", "sdm4065a_query")):
        assert dispatch.is_mutator(method) is False, (
            f"is_mutator({method!r}) is now True — the CLI table no longer "
            f"disagrees with it here, so this test's premise has changed"
        )
        assert TOOL_TIERS[tool] == TIER1


def test_tools_matching_a_mutator_prefix_are_not_automatically_gated():
    """The disagreement runs the other way too.

    ``take_snapshot`` matches the ``take_`` prefix and ``dp2031_apply`` matches
    ``apply``, so both need the writer claim on the wire — correctly, they touch
    the instrument. Neither energises anything, so requiring ``--yes`` for them
    would be the kind of reflexive confirmation that stops being a confirmation.
    """
    from benchctrl.agent import dispatch

    assert dispatch.is_mutator("take_snapshot") is True
    assert TOOL_TIERS["take_snapshot"] == READ

    assert dispatch.is_mutator("apply") is True
    assert TOOL_TIERS["dp2031_apply"] == TIER2


def test_a_tool_matching_no_mutator_prefix_can_still_be_tier1():
    """``dp2031_step_voltage_up`` moves a live output and matches nothing;
    ``dp2031_trigger_in_immediate`` fires a configured response now."""
    from benchctrl.agent import dispatch

    assert dispatch.is_mutator("step_voltage_up") is False
    assert TOOL_TIERS["dp2031_step_voltage_up"] == TIER2
    assert TOOL_TIERS["dp2031_load_file"] == TIER1
    assert dispatch.is_mutator("load_file") is False


# ---------------------------------------------------------------------------
# The specific operations the operator asked to be writable
# ---------------------------------------------------------------------------


def test_pdu_switching_is_present_and_double_gated():
    """PDU switching is in the CLI by explicit instruction, at the highest tier.

    Both directions matter: it must be *present* (an earlier revision of the
    design excluded it, and the operator reversed that), and it must carry the
    env gate, because ``--yes`` on a command line is not evidence anyone knows
    what is plugged into outlet 4.
    """
    assert TOOL_TIERS["pdu41002_set_outlet_state"] == TIER1_ENV
    assert TOOL_TIERS["pdu41002_reset_outlet"] == TIER1_ENV
    assert (
        cli_tiers.env_gate_for("pdu41002_set_outlet_state")
        == "BENCHCTRL_PDU_ALLOW_SWITCHING"
    )
    # A power-cycle is an off *and* an on, so it cannot be gated more weakly
    # than a plain switch.
    assert cli_tiers.env_gate_for("pdu41002_reset_outlet") == "BENCHCTRL_PDU_ALLOW_SWITCHING"


def test_every_pdu_write_is_exposed_and_none_is_ungated():
    """Derived, for the same reason the ADU218 check below is.

    The two assertions above name the switching tools, so they cannot regress —
    but they say nothing about a write added *later*. ``test_every_tool_has_a_tier``
    does not cover that case either: it compares the table against ``_TOOLS``, so
    a driver that grows a mutator with no tool at all satisfies both. On the one
    device that switches mains, "the driver can do it and the CLI cannot" should
    be a test failure rather than something noticed by reading.
    """
    from benchctrl.agent import dispatch
    from benchctrl.drivers.cyberpower_pdu41002.driver import CyberPowerPDU41002

    surface = dispatch.introspect(
        CyberPowerPDU41002.__new__(CyberPowerPDU41002), "cyberpower_pdu41002"
    )
    for method in sorted(surface.mutators):
        tool = f"pdu41002_{method}"
        assert tool in TOOL_TIERS, (
            f"PDU41002.{method} mutates a mains switch but {tool} is not a CLI "
            f"subcommand"
        )
        assert TOOL_TIERS[tool] != READ, (
            f"{tool} is a wire-level mutator classified READ, so it would run "
            f"with no authorisation at all"
        )


def test_every_adu218_write_is_exposed():
    """"All features of the ADU218 must be writable as well."

    Asserted against the driver's own mutator set rather than a hand-written
    list, so a new ADU218 write cannot be added to the driver and quietly left
    out of the CLI. The tool names are the method names with an ``adu218_``
    prefix, which is this driver's convention.
    """
    from benchctrl.agent import dispatch
    from benchctrl.drivers.ontrak_adu218.driver import OntrakADU218

    surface = dispatch.introspect(OntrakADU218.__new__(OntrakADU218), "ontrak_adu218")
    for method in sorted(surface.mutators):
        tool = f"adu218_{method}"
        assert tool in TOOL_TIERS, (
            f"ADU218.{method} mutates the device but {tool} is not a CLI "
            f"subcommand — every ADU218 write must be exposed"
        )
        assert TOOL_TIERS[tool] in (TIER2, TIER1, TIER1_ENV), (
            f"{tool} is a write classified as {TOOL_TIERS[tool]}"
        )

    # And the six specifically: the complete write surface, named, so the
    # requirement is legible without running introspection.
    for tool in (
        "adu218_set_relay_state",
        "adu218_set_relay_port",
        "adu218_reset_relays",
        "adu218_clear_counter",
        "adu218_set_debounce",
        "adu218_set_watchdog",
    ):
        assert tool in TOOL_TIERS, tool


def test_arming_the_watchdog_needs_its_own_named_env_gate():
    """``WDn`` sets *and arms* in one command, and the consequence outlives the
    call — the relay opens later, after the CLI has exited. So this is the other
    TIER1_ENV, and its variable names the watchdog specifically rather than
    sharing the PDU's."""
    assert TOOL_TIERS["adu218_set_watchdog"] == TIER1_ENV
    assert cli_tiers.env_gate_for("adu218_set_watchdog") == "BENCHCTRL_ADU218_ARM_WATCHDOG"
    assert (
        cli_tiers.env_gate_for("adu218_set_watchdog")
        != cli_tiers.env_gate_for("pdu41002_set_outlet_state")
    ), "one authorisation must not grant the other"


def test_every_tier1_env_tool_has_a_gate_and_vice_versa():
    """The two structures cannot drift: a TIER1_ENV tool with no variable would
    be un-runnable, and a variable for a non-TIER1_ENV tool would be dead."""
    env_tools = {name for name, t in TOOL_TIERS.items() if t == TIER1_ENV}
    assert env_tools == set(cli_tiers.ENV_GATES)


def test_de_energising_is_never_harder_than_energising():
    """Asymmetry, on purpose.

    If reaching the safe state needs more ceremony than leaving it, an operator
    fighting a live output has a gate in the way. Each pair below is
    (energise, de-energise) on one device.
    """
    pairs = [
        ("enable_output", "disable_output"),
        ("adu218_set_relay_state", "adu218_reset_relays"),
        ("cp2112_set_line_asserted", "cp2112_reset_lines"),
        ("battery_emulator_start", "battery_emulator_stop"),
    ]
    rank = {t: i for i, t in enumerate(cli_tiers.TIERS)}
    for on, off in pairs:
        assert rank[TOOL_TIERS[off]] <= rank[TOOL_TIERS[on]], (
            f"{off} is gated harder than {on}"
        )


# ---------------------------------------------------------------------------
# Suppressed parameters — the safety mechanism reflection cannot see
# ---------------------------------------------------------------------------


def test_suppressed_params_name_real_parameters():
    """A suppression for a parameter that does not exist protects nothing, and
    reads as though it does. Checks the signature, so a renamed parameter fails
    here rather than silently reopening the hole."""
    import importlib
    import inspect

    from tests.test_mcp import TOOL_MODULES

    by_name = {}
    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        for fn in mod._TOOLS:
            by_name[fn.__name__] = fn

    for tool, params in cli_tiers.SUPPRESSED.items():
        assert tool in by_name, f"SUPPRESSED names a tool that does not exist: {tool}"
        actual = set(inspect.signature(by_name[tool]).parameters)
        for param, reason in params.items():
            assert param in actual, (
                f"{tool} has no parameter {param!r} — the suppression is inert"
            )
            assert reason.strip(), f"{tool}.{param} suppressed with no reason"


def test_verify_is_suppressed_on_every_tool_that_has_it():
    """Derived from the signatures, not listed by hand.

    ``oltctrl`` acknowledges nothing, so on the PDU the read-back is the only
    evidence a contactor moved; the ADU218 is the same shape. A ``--no-verify``
    flag would be a flag to stop checking. Enumerating the tools that *have* a
    ``verify`` parameter — rather than naming the two I knew about — is what
    catches a third one appearing later. (It already caught
    ``adu218_set_relay_port``.)
    """
    import importlib
    import inspect

    from tests.test_mcp import TOOL_MODULES

    have_verify = []
    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        for fn in mod._TOOLS:
            if "verify" in inspect.signature(fn).parameters:
                have_verify.append(fn.__name__)

    assert have_verify, "no tool has a verify parameter — has it been renamed?"
    for tool in have_verify:
        assert "verify" in cli_tiers.suppressed_params(tool), (
            f"{tool} takes verify= but the CLI would expose it as a flag"
        )


def test_operator_gates_are_absent_from_the_tool_signatures_entirely():
    """The stronger form of suppression, and the one reflection cannot see.

    ``allow_alternate_function`` is an operator observation about what is
    physically wired, so it is not a parameter of any MCP tool at all — the CLI
    inherits its absence rather than having to strip it. Asserting that here
    means a future tool that *adds* the parameter fails this test instead of
    quietly becoming a CLI flag.
    """
    import importlib
    import inspect

    from tests.test_mcp import TOOL_MODULES

    FORBIDDEN = {"allow_alternate_function", "password"}
    for mod_name in TOOL_MODULES:
        mod = importlib.import_module(mod_name)
        for fn in mod._TOOLS:
            params = set(inspect.signature(fn).parameters)
            leaked = params & FORBIDDEN
            assert leaked == set(), (
                f"{fn.__name__} takes {sorted(leaked)} — this must be an "
                f"operator gate or an environment secret, not a tool parameter"
            )


# ---------------------------------------------------------------------------
# Sanity properties over the whole table
# ---------------------------------------------------------------------------


def test_no_read_tool_is_a_wire_level_mutator_without_a_reason():
    """Cross-check the two classifications and require the disagreements to be
    the known ones.

    A tool the agent treats as a mutation but the CLI calls READ is worth
    looking at every time: it means the CLI will run it with no confirmation
    while the agent demands a writer claim. Three are legitimate — they touch
    instrument state without driving anything — and they are listed here so a
    *fourth* fails the test.
    """
    from benchctrl.agent import dispatch

    KNOWN = {
        "take_snapshot",  # samples the channels
        "sdm4065a_reading_timeout_ms",  # a property read; matches no prefix anyway
    }
    offenders = set()
    for tool, tier in TOOL_TIERS.items():
        if tier != READ:
            continue
        # Strip the device prefix to recover the method name the agent sees.
        method = tool.split("_", 1)[1] if "_" in tool and tool not in ("info", "state") else tool
        if dispatch.is_mutator(method) and tool not in KNOWN:
            offenders.add(tool)
    assert offenders == set(), (
        f"classified READ but the agent treats as a mutation: {sorted(offenders)}"
    )


def test_the_table_covers_every_device_and_the_framework_tools():
    """A whole module missing would otherwise show up only as many small
    failures in test_every_tool_has_a_tier."""
    prefixes = (
        "qr10x_",
        "dl3031a_",
        "dp2031_",
        "sdm4065a_",
        "pdu41002_",
        "cp2112_",
        "adu218_",
        "battery_",
    )
    for prefix in prefixes:
        assert any(k.startswith(prefix) for k in TOOL_TIERS), prefix
    # The Arc's tools are unprefixed.
    assert TOOL_TIERS["enable_output"] == TIER1


def test_every_entry_at_tier1_or_above_is_a_write():
    """A READ misclassified upward is not dangerous, but it is a lie in the
    table. Everything at TIER1+ should be defensible as a write; the raw-SCPI
    ``query`` escape is the one exception, because a query string can carry a
    setting command."""
    EXPECTED_READ_SHAPED = {"sdm4065a_query"}
    for tool, tier in TOOL_TIERS.items():
        if tier not in (TIER1, TIER1_ENV):
            continue
        if tool in EXPECTED_READ_SHAPED:
            continue
        method = tool.split("_", 1)[1] if "_" in tool else tool
        looks_like_read = method.startswith(("get_", "measure_", "read_", "fetch_"))
        assert not looks_like_read, (
            f"{tool} is gated at {tier} but is named like a read — either the "
            f"tier is wrong or the tool name is misleading"
        )

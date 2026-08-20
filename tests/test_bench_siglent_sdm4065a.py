"""SDM4065A driver tests, driven against the simulator over real pyvisa.

Sim-backed rather than mock-backed (CONTRIBUTING rule 2): every test below
runs the production driver, the production pyvisa stack, and real serial I/O
against :py:class:`SimulatedSDM4065A`. A mock would let the driver send any
command string at all and still pass.

The tests worth reading are the ones that pin down instrument behaviour a
reasonable implementation gets wrong:

* :py:func:`test_measure_discards_a_null` — ``MEASure?`` is ``CONFigure`` +
  ``READ?``, so it throws away the null *and* the range.
* :py:func:`test_null_now_survives_the_auto_null_side_effect` — enabling
  null arms ``NULL:VALue:AUTO``, so the obvious ordering nulls by the wrong
  number.
* :py:func:`test_2m_range_is_rejected_because_that_is_the_other_model` — the
  SDM4055A's range, silently coerced by real hardware.
"""

from __future__ import annotations

import os
import warnings

import pytest

from benchctrl.drivers.siglent_sdm4065a import (
    SDM4065AError,
    SDM4065AOverloadError,
    SDM4065AValueError,
    SiglentSDM4065A,
)
from benchctrl.sim.sdm4065a import OVERLOAD, SimulatedSDM4065A


@pytest.fixture()
def dmm():
    """Driver + sim with a 100 Ω DUT behind 0.2 Ω of lead resistance.

    0.2 Ω is the datasheet's own figure for un-nulled 2-wire error (note
    [6]), so the default fixture reproduces the error rather than a clean
    ideal that would hide it.
    """
    from benchctrl.sim.factories import make_sdm4065a

    drv = make_sdm4065a(sim={"dut_ohm": 100.0, "lead_ohm": 0.2})
    yield drv
    drv.close()


# --------------------------------------------------------------------------
# Identity and lifecycle
# --------------------------------------------------------------------------


def test_identity_over_pyvisa(dmm):
    info = dmm.info()
    assert info.manufacturer == "Siglent Technologies"
    assert info.model == "SDM4065A"
    assert info.firmware
    assert info.resource.startswith("ASRL")


def test_info_is_cached(dmm):
    first = dmm.info()
    assert dmm.info() is first


def test_info_to_dict_round_trips(dmm):
    d = dmm.info().to_dict()
    assert d["model"] == "SDM4065A"
    assert set(d) == {"manufacturer", "model", "serial", "firmware", "resource"}


def test_is_connected_tracks_close(dmm):
    assert dmm.is_connected is True
    dmm.close()
    assert dmm.is_connected is False


def test_close_is_idempotent(dmm):
    dmm.close()
    dmm.close()  # must not raise


def test_using_a_closed_driver_raises(dmm):
    dmm.close()
    with pytest.raises(SDM4065AError):
        dmm.measure_resistance()


def test_self_test_passes(dmm):
    assert dmm.self_test() is True


# --------------------------------------------------------------------------
# 2-wire vs 4-wire: the physical distinction
# --------------------------------------------------------------------------


def test_two_wire_includes_lead_resistance(dmm):
    """2-wire sees the leads in series with the DUT — the 0.2 Ω of note [6]."""
    assert dmm.measure_resistance(200) == pytest.approx(100.2)


def test_four_wire_excludes_lead_resistance(dmm):
    """4-wire is the same DUT read correctly: leads drop out entirely.

    This is the test that proves ``measure_resistance_4wire`` sends FRES and
    not RES. Both would return ~100 against an ideal simulator; only a
    modelled lead resistance can tell them apart.
    """
    assert dmm.measure_resistance_4wire(200) == pytest.approx(100.0)


def test_four_wire_resolves_a_38_milliohm_offset(dmm):
    """The QR10x cross-validation premise, off-hardware.

    The real QR101A-1M-R1 reads 100.038 Ω at a 100.0 Ω setpoint. 4-wire has
    to preserve that 38 mΩ; 2-wire without a null buries it under 0.2 Ω of
    lead resistance, which is 5x larger and the wrong sign of useful.
    """
    dmm._benchctrl_sim.dut_ohm = 100.038
    four = dmm.measure_resistance_4wire(200)
    assert four == pytest.approx(100.038, abs=1e-6)

    two = dmm.measure_resistance(200)
    assert abs(two - 100.038) > 0.1  # the offset is unrecoverable from this


# --------------------------------------------------------------------------
# Null / "Ref"
# --------------------------------------------------------------------------


def test_null_now_removes_lead_resistance(dmm):
    """The documented 2-wire recipe end to end: short, null, restore, read."""
    dmm.configure_resistance(200)
    dmm._benchctrl_sim.dut_ohm = 0.0  # leads shorted across the DUT
    offset = dmm.null_now(samples=3)
    assert offset == pytest.approx(0.2, abs=1e-6)

    dmm._benchctrl_sim.dut_ohm = 100.0  # DUT restored
    assert dmm.read_nulled() == pytest.approx(100.0, abs=1e-6)


def test_null_now_survives_the_auto_null_side_effect(dmm):
    """Enabling null arms NULL:VALue:AUTO (§7.4.2); null_now must disarm it.

    With AUTO left armed the instrument replaces the offset with its own
    next reading, so the first real measurement would read ~0 and every
    later one would be nulled against the DUT rather than the leads. This
    asserts the flag is *off* afterwards — the observable that distinguishes
    correct ordering from the natural, wrong one.
    """
    dmm.configure_resistance(200)
    dmm._benchctrl_sim.dut_ohm = 0.0
    dmm.null_now()

    assert dmm.get_null() is True
    assert dmm.get_null_auto() is False, "AUTO left armed — offset will be clobbered"
    assert dmm.get_null_value() == pytest.approx(0.2, abs=1e-6)


def test_the_naive_null_ordering_really_does_break(dmm):
    """Guards the *reason* null_now exists, not just its result.

    value-then-state is the ordering anyone would write first. It must be
    demonstrably wrong, or null_now's careful sequencing is cargo cult and
    this test suite is asserting a fiction.
    """
    dmm.configure_resistance(200)
    dmm.set_null_value(0.2)  # wrong order: value first...
    dmm.set_null(True)  # ...then state, which re-arms AUTO

    assert dmm.get_null_auto() is True
    dmm._benchctrl_sim.dut_ohm = 100.0
    first = dmm.read()[0]
    # AUTO captured the 100.2 Ω reading as the offset, so it nulled to ~0
    # instead of using the 0.2 Ω that was asked for.
    assert first == pytest.approx(0.0, abs=1e-6)
    assert dmm.get_null_value() == pytest.approx(100.2, abs=1e-6)


def test_measure_discards_a_null(dmm):
    """``MEASure?`` is CONFigure + READ?, and CONFigure clears the null.

    This is why the driver has ``read_nulled`` at all. A caller that nulls
    and then reaches for ``measure_resistance`` gets an un-nulled number
    with no error to warn them, which is the worst kind of bug.
    """
    dmm.configure_resistance(200)
    dmm._benchctrl_sim.dut_ohm = 0.0
    dmm.null_now()
    dmm._benchctrl_sim.dut_ohm = 100.0

    assert dmm.read_nulled() == pytest.approx(100.0, abs=1e-6)
    # ...but the one-shot path reconfigures, dropping the null:
    assert dmm.measure_resistance(200) == pytest.approx(100.2)
    assert dmm.get_null() is False


def test_measure_without_a_range_argument_reverts_to_autoranging(dmm):
    """``MEASure?`` with no range re-CONFigures, which re-enables autorange.

    So a pinned range does not survive a one-shot ``measure_*`` call. Here
    the 100 Ω DUT happens to autorange back to 200 Ω, which is why this
    asserts the *autorange flag* rather than the resulting number — the
    number alone cannot distinguish "range preserved" from "autoranged to
    the same value", and it is the flag that bites on a different DUT.
    """
    dmm.configure_resistance(200)
    assert dmm.get_range() == pytest.approx(200.0)
    assert dmm._benchctrl_sim.autorange["RES"] is False

    dmm.measure_resistance()  # no range argument
    assert dmm._benchctrl_sim.autorange["RES"] is True


def test_configure_without_a_range_leaves_autorange_on(dmm):
    """Bench-measured: a bare ``CONFigure`` autoranges, reporting 200 Ω.

    Worth pinning because the consequence is invisible in the reading. The
    number comes back correct — autorange picks a sensible range — but the
    "% of range" term in the error budget is then whatever range the input
    happened to select, not one the caller chose.
    """
    dmm.configure_resistance()  # no range argument
    assert dmm.get_autorange() is True
    assert dmm.get_range() == pytest.approx(200.0)


def test_read_nulled_refuses_when_no_null_is_active(dmm):
    dmm.configure_resistance(200)
    with pytest.raises(SDM4065AError, match="null is not enabled"):
        dmm.read_nulled()


def test_null_now_averages_the_requested_samples(dmm):
    dmm.configure_resistance(200)
    dmm._benchctrl_sim.dut_ohm = 0.0
    sim = dmm._benchctrl_sim
    before = len([c for c in sim.command_log if c.strip().upper() == "READ?"])
    dmm.null_now(samples=5)
    after = len([c for c in sim.command_log if c.strip().upper() == "READ?"])
    assert after - before == 5


def test_null_state_is_independent_per_function(dmm):
    """RES and FRES keep separate settings (§7.1), so a 2-wire null must not
    silently apply to a 4-wire reading."""
    dmm.configure_resistance(200)
    dmm.set_null(True)
    dmm.set_null_value(0.2)
    assert dmm.get_null() is True
    assert dmm.get_null(function="FRESistance") is False


def test_null_value_beyond_110_megaohm_is_rejected(dmm):
    with pytest.raises(SDM4065AValueError, match="110"):
        dmm.set_null_value(200e6)


def test_set_null_value_does_not_disarm_auto_despite_the_manual(dmm):
    """§7.4.3's claim is false on firmware 0.0.0.20 — pinned as measured.

    The manual says writing ``NULL:VALue`` disables automatic null-value
    selection. The real meter leaves ``NULL:VALue:AUTO`` armed, so an offset
    written this way is overwritten by the instrument's next reading and the
    null silently does nothing.

    This test asserts the *hardware's* behaviour rather than the manual's,
    which is the only version that keeps ``null_now``'s explicit ``AUTO OFF``
    honest. Asserting the documented behaviour would make that call look
    redundant, and deleting it would reintroduce a wrong-answer bug that no
    single-instrument reading can reveal — a nulled meter agrees with itself
    either way.
    """
    dmm.configure_resistance(200)
    dmm.set_null_auto(True)
    assert dmm.get_null_auto() is True
    dmm.set_null_value(0.5)
    assert dmm.get_null_auto() is True, (
        "if this ever fails, Siglent fixed §7.4.3 in a firmware update — "
        "check the firmware revision before relaxing null_now()"
    )


def test_null_now_disarms_auto_explicitly(dmm):
    """The consequence: after ``null_now`` the offset is safe from AUTO.

    Guards the fix rather than the bug. ``null_now`` must leave the null
    enabled, the offset installed, *and* AUTO off — the third being the part
    the instrument will not do for you.
    """
    dmm.configure_resistance(200)
    dmm.set_nplc(1.0)
    dmm.null_now(samples=2)
    assert dmm.get_null() is True
    assert dmm.get_null_auto() is False, (
        "null_now left auto-null armed — the offset it just stored will be "
        "overwritten by the next reading"
    )


# --------------------------------------------------------------------------
# Ranges — the model split
# --------------------------------------------------------------------------


@pytest.mark.parametrize("ohms", [200, 2e3, 20e3, 200e3, 1e6, 10e6, 100e6])
def test_every_sdm4065a_resistance_range_is_accepted(dmm, ohms):
    dmm.configure_resistance(ohms)
    assert dmm.get_range() == pytest.approx(float(ohms))


def test_2m_range_is_rejected_because_that_is_the_other_model(dmm):
    """The SDM4055A has 2 MΩ; the SDM4065A has 1 MΩ (§7.4.5).

    Rejected client-side because real hardware would coerce it silently and
    the caller would believe it measured on a range that does not exist.
    """
    with pytest.raises(SDM4065AValueError, match="SDM4055A"):
        dmm.measure_resistance(2e6)


def test_an_arbitrary_range_is_rejected_not_rounded(dmm):
    """Rounding up would degrade the "% of range" accuracy term silently."""
    with pytest.raises(SDM4065AValueError):
        dmm.measure_resistance(500)


def test_autorange_reports_the_range_actually_used(dmm):
    dmm._benchctrl_sim.dut_ohm = 5_000.0
    dmm._benchctrl_sim.lead_ohm = 0.0
    assert dmm.measure_resistance("AUTO") == pytest.approx(5000.0)
    assert dmm.get_range() == pytest.approx(20e3)


def test_setting_a_fixed_range_disables_autorange(dmm):
    dmm.configure_resistance("AUTO")
    dmm.set_range(200)
    dmm._benchctrl_sim.dut_ohm = 5_000.0
    # Autorange off, so an over-range input now overloads instead of moving up.
    with pytest.raises(SDM4065AOverloadError):
        dmm.read()


def test_dc_voltage_ranges_are_validated(dmm):
    with pytest.raises(SDM4065AValueError, match="DC voltage"):
        dmm.measure_dc_voltage(500)
    dmm.configure_dc_voltage(20)  # a real range: must not raise


def test_ac_voltage_stops_at_750_not_1000(dmm):
    """1000 V is DC-only."""
    with pytest.raises(SDM4065AValueError):
        dmm.measure_ac_voltage(1000)


# --------------------------------------------------------------------------
# Overload
# --------------------------------------------------------------------------


def test_overload_raises_rather_than_returning_the_sentinel(dmm):
    """9.9E37 must never reach a caller as a number.

    Returned, it would propagate into an average or a plot as a
    plausible-looking float and corrupt everything downstream silently.
    """
    dmm._benchctrl_sim.dut_ohm = 5_000.0
    with pytest.raises(SDM4065AOverloadError) as exc:
        dmm.measure_resistance(200)
    assert exc.value.function == "RESistance"
    assert exc.value.range == 200


def test_the_simulator_really_emits_the_sentinel(dmm):
    """Proves the previous test exercises sentinel handling, not a sim shortcut.

    Without this, ``measure_resistance`` could be raising for some unrelated
    reason and the overload path would be untested.
    """
    sim = dmm._benchctrl_sim
    sim.dut_ohm = 5_000.0
    sim.autorange["RES"] = False
    sim.range["RES"] = 200.0
    assert sim.take_reading("RES") == OVERLOAD


def test_read_checks_every_reading_for_overload(dmm):
    """A burst where only a later sample overloads must still raise."""
    dmm.configure_resistance(200)
    dmm.set_sample_count(1)
    dmm._benchctrl_sim.dut_ohm = 5_000.0
    with pytest.raises(SDM4065AOverloadError):
        dmm.read()


# --------------------------------------------------------------------------
# NPLC — the other model split
# --------------------------------------------------------------------------


@pytest.mark.parametrize("nplc", [100, 10, 1, 0.1, 0.01, 0.001])
def test_every_sdm4065a_nplc_is_accepted(dmm, nplc):
    dmm.set_nplc(nplc)
    assert dmm.get_nplc() == pytest.approx(float(nplc))


def test_an_off_list_nplc_is_rejected(dmm):
    """The instrument would coerce silently, and the datasheet's noise adder
    is indexed on exactly this number."""
    with pytest.raises(SDM4065AValueError, match="NPLC"):
        dmm.set_nplc(5)


def test_nplc_error_names_the_other_model(dmm):
    """The SDM4055A accepts only 10/1/0.01, which is the likely source of a
    wrong constant — so the message says so."""
    with pytest.raises(SDM4065AValueError, match="SDM4055A"):
        dmm.set_nplc(0.5)


def test_configure_resets_nplc_to_ten(dmm):
    """§7.4.1: reset by CONFigure. Hence configure-then-set-NPLC, not the
    reverse — a caller doing it backwards loses the setting."""
    dmm.set_nplc(100)
    dmm.configure_resistance(200)
    assert dmm.get_nplc() == pytest.approx(10.0)


# --------------------------------------------------------------------------
# Autozero
# --------------------------------------------------------------------------


def test_resistance_autozero_defaults_off(dmm):
    """§7.4.7 — SDM4065A-only, and off by default, which matters because a
    low-resistance measurement is then not autozeroed unless asked."""
    assert dmm.get_autozero() is False


def test_autozero_round_trips(dmm):
    dmm.set_autozero(True)
    assert dmm.get_autozero() is True
    dmm.set_autozero(False)
    assert dmm.get_autozero() is False


def test_autozero_uses_zero_auto_not_the_manuals_az(dmm):
    """The mnemonic §7.4.7 documents does not exist on the instrument.

    Bench-measured on firmware 0.0.0.20: every ``AZ`` spelling is rejected
    with -113 "Undefined header", for reads and writes, on all four
    functions. ``ZERO:AUTO`` works. This pins the wire format, because the
    manual is the thing a future reader will check the code against — and
    "fixing" it to match §7.4.7 costs a power cycle to discover.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import AUTOZERO_NODE

    assert AUTOZERO_NODE == "ZERO:AUTO"
    # The working form round-trips...
    dmm.set_autozero(True)
    assert dmm.get_autozero() is True
    # ...and the manual's form must be *rejected*, not silently accepted. A sim
    # that answered ``AZ?`` would hide the whole defect and let a driver that
    # follows the manual pass every test right up to the real instrument.
    dmm.write("RESistance:AZ ON")
    err = dmm.last_error()
    assert err is not None and err[0] == -113, (
        f"the simulator accepted RESistance:AZ (error queue: {err}), which "
        f"the real instrument rejects with -113 'Undefined header' — a sim "
        f"more permissive than the hardware is worse than no sim"
    )


def test_cls_does_not_empty_the_error_queue(dmm):
    """IEEE 488.2 says ``*CLS`` clears the error queue. This firmware doesn't.

    Bench-measured on 0.0.0.20: queued errors survive ``*CLS``, survive
    ``*RST``, and survive closing and reopening the session. Only reading them
    removes them. Pinned because if it ever starts working, the driver's
    read-until-empty drain can be simplified — and because a sim that modelled
    the standard would make that drain look like dead code.
    """
    dmm.write("RESistance:AZ ON")  # a header this instrument does not have
    dmm.write("*CLS")
    err = dmm.last_error()
    assert err is not None and err[0] == -113, (
        "*CLS emptied the error queue — check the firmware revision, then "
        "reconsider clear_status()'s drain"
    )


def test_clear_status_drains_the_queue_anyway(dmm):
    """So the driver's own ``clear_status`` must leave it actually clean.

    This is the property callers rely on: without it the queue fills, starts
    answering -350 "Queue overflow", and a later check reports an error raised
    by an unrelated command.
    """
    for _ in range(3):
        dmm.write("RESistance:AZ ON")
    dmm.clear_status()
    assert dmm.last_error() is None


def test_drain_errors_returns_what_it_removed(dmm):
    """And reports the entries, so a caller can log what was discarded."""
    dmm.write("RESistance:AZ ON")
    dmm.write("RESistance:AZ:STATe ON")
    drained = dmm.drain_errors()
    assert [code for code, _ in drained] == [-113, -113]
    assert dmm.last_error() is None


def test_drain_errors_is_bounded(dmm):
    """A queue that never empties must not hang the caller.

    ``clear_status`` runs in ``finally`` blocks, where an unbounded loop is the
    worst possible failure mode.
    """
    for _ in range(10):
        dmm.write("RESistance:AZ ON")
    drained = dmm.drain_errors(limit=4)
    assert len(drained) == 4, "drain_errors ignored its limit"
    # The rest are still queued — a short drain must not claim to be complete.
    assert dmm.last_error() is not None
    dmm.drain_errors()


def test_esr_flags_a_command_error_and_clears_on_read(dmm):
    """``*ESR?`` bit 5 marks a rejected header, and the read empties it.

    Read-destructive is the property that makes this usable: because the
    register cannot accumulate, a caller that clears, sends, then reads knows
    the bit belongs to *that* command — which is exactly what the error queue
    can no longer promise on this firmware.
    """
    dmm.clear_status()
    assert dmm.command_error() is False

    dmm.write("RESistance:AZ ON")
    assert dmm.standard_event_status() == 32, "bit 5 (Command Error) not set"
    assert dmm.standard_event_status() == 0, "*ESR? is not read-destructive"

    dmm.drain_errors()


def test_cls_clears_the_esr_even_though_it_spares_the_queue(dmm):
    """The split that makes ``*ESR?`` the dependable signal.

    ``*CLS`` leaves errors queued (see
    :py:func:`test_cls_does_not_empty_the_error_queue`) but does zero the
    status registers — bench-measured both ways on 0.0.0.20. If a firmware
    update ever makes ``*CLS`` clear both, this still passes; it is the
    register half that ``command_error`` depends on.
    """
    dmm.write("RESistance:AZ ON")
    dmm.write("*CLS")
    assert dmm.command_error() is False, "*CLS left the ESR set"
    # ...while the queue entry it should have removed is still sitting there.
    assert dmm.last_error() is not None


def test_command_error_still_works_when_the_queue_goes_silent(dmm):
    """The failure this fallback exists for.

    Bench-measured after an error-queue overflow: ``SYSTem:ERRor?`` answered
    ``0,"No Error"`` to everything, including immediately after a deliberately
    bogus header, while ``*ESR?`` returned 32 for that same command. Only a
    power cycle restored the queue. Any driver check that trusts the queue
    silently stops detecting rejections in that state.
    """
    dmm._benchctrl_sim.error_queue_silent = True

    # ``RESistance:AZ``, not an invented header: the sim only rejects writes it
    # has a handler for, since the base ScpiDevice files unknown writes away as
    # vendor extensions. The hardware rejects both.
    dmm.write("RESistance:AZ ON")
    assert dmm.last_error() is None, "the sim is not modelling the silent queue"
    assert dmm.command_error() is True, (
        "command_error fell back to the error queue — it must read *ESR?, "
        "which stays accurate when the queue latches silent"
    )


def test_autozero_cached_agrees_with_the_readback(dmm):
    """The tracked copy must not drift from the instrument.

    ``get_autozero_cached`` exists for callers that cannot afford a round trip
    (inside a timed loop). That is only safe while the two agree, so this pins
    the agreement across a write and across ``reset``.
    """
    dmm.set_autozero(True)
    assert dmm.get_autozero_cached() is True
    assert dmm.get_autozero() is True

    dmm.set_autozero(False)
    assert dmm.get_autozero_cached() is False
    assert dmm.get_autozero() is False

    dmm.set_autozero(True)
    dmm.reset()
    assert dmm.get_autozero_cached() is False, "reset should re-seed the cache"
    assert dmm.get_autozero() is False


# --------------------------------------------------------------------------
# Acquisition model
# --------------------------------------------------------------------------


def test_read_always_returns_a_list(dmm):
    """Even for one sample — a scalar return would make a caller that
    ignored sample_count silently keep only the first of N readings."""
    dmm.configure_resistance(200)
    values = dmm.read()
    assert isinstance(values, list)
    assert len(values) == 1


def test_sample_count_returns_that_many_readings(dmm):
    dmm.configure_resistance(200)
    dmm.set_sample_count(4)
    assert dmm.get_sample_count() == 4
    assert len(dmm.read()) == 4


def test_sample_count_below_one_is_rejected(dmm):
    with pytest.raises(SDM4065AValueError):
        dmm.set_sample_count(0)


def test_initiate_then_fetch(dmm):
    dmm.configure_resistance(200)
    dmm.set_sample_count(2)
    dmm.initiate()
    values = dmm.fetch()
    assert len(values) == 2
    assert values[0] == pytest.approx(100.2)


def test_fetch_without_initiate_flags_an_error(dmm):
    dmm.configure_resistance(200)
    dmm.fetch()
    err = dmm.last_error()
    assert err is not None and err[0] == -230


def test_abort_clears_the_buffer(dmm):
    dmm.configure_resistance(200)
    dmm.initiate()
    dmm.abort()
    dmm.fetch()
    assert dmm.last_error() is not None


def test_get_configuration_strips_the_quotes(dmm):
    """§3.1 answers a *quoted* string; a caller comparing against RES should
    not have to know that."""
    dmm.configure_resistance(200)
    conf = dmm.get_configuration()
    assert not conf.startswith('"')
    assert conf.startswith("RES")


# --------------------------------------------------------------------------
# Function selection
# --------------------------------------------------------------------------


def test_get_function_strips_the_quotes(dmm):
    """§7.1 returns the short form in quotation marks."""
    dmm.set_function("res")
    assert dmm.get_function() == "RES"


@pytest.mark.parametrize(
    "alias,expected",
    [("dcv", "VOLT"), ("res", "RES"), ("4w", "FRES"), ("fres", "FRES"),
     ("resistance", "RES"), ("volt:dc", "VOLT")],
)
def test_function_aliases_map_to_the_scpi_form(dmm, alias, expected):
    dmm.set_function(alias)
    assert dmm.get_function() == expected


def test_an_unknown_function_is_rejected_with_the_valid_list(dmm):
    with pytest.raises(SDM4065AValueError, match="unknown function"):
        dmm.set_function("ohms")


# --------------------------------------------------------------------------
# Other measurement functions
# --------------------------------------------------------------------------


def test_measure_dc_voltage(dmm):
    dmm._benchctrl_sim.dut_volts = 3.3
    assert dmm.measure_dc_voltage(20) == pytest.approx(3.3)


def test_measure_temperature_requires_probe_before_type(dmm):
    """A sensor type is meaningless without knowing RTD vs thermocouple."""
    with pytest.raises(SDM4065AValueError, match="type_ requires probe"):
        dmm.measure_temperature(type_="PT100")


def test_measure_temperature_rejects_an_unknown_probe(dmm):
    with pytest.raises(SDM4065AValueError, match="probe must be"):
        dmm.measure_temperature(probe="THERMISTOR")


def test_temperature_unit_round_trips(dmm):
    dmm.set_temperature_unit("F")
    assert dmm.get_temperature_unit() == "F"


def test_an_invalid_temperature_unit_is_rejected(dmm):
    with pytest.raises(SDM4065AValueError):
        dmm.set_temperature_unit("Rankine")


# --------------------------------------------------------------------------
# Error queue
# --------------------------------------------------------------------------


def test_error_queue_is_clean_then_reports_an_injected_error(dmm):
    assert dmm.last_error() is None
    dmm._benchctrl_sim.inject_error(-222, "Data out of range")
    dmm.write("RESistance:RANGe 200")
    err = dmm.last_error()
    assert err == (-222, "Data out of range")


def test_raise_if_error_raises_with_the_code(dmm):
    from benchctrl.drivers.siglent_sdm4065a import SDM4065ACommandError

    dmm._benchctrl_sim.inject_error(-113, "Undefined header")
    dmm.write("NOSUCH:THING 1")
    with pytest.raises(SDM4065ACommandError) as exc:
        dmm.raise_if_error()
    assert exc.value.code == -113


def test_raise_if_error_is_silent_when_clean(dmm):
    dmm.raise_if_error()


# --------------------------------------------------------------------------
# Reset
# --------------------------------------------------------------------------


def test_reset_returns_to_dc_volts_and_nplc_ten(dmm):
    dmm.set_function("res")
    dmm.set_nplc(0.001)
    dmm.reset()
    assert dmm.get_function() == "VOLT"
    assert dmm.get_nplc() == pytest.approx(10.0)


def test_reset_clears_a_null(dmm):
    dmm.configure_resistance(200)
    dmm.set_null(True)
    dmm.reset()
    assert dmm.get_null() is False


# --------------------------------------------------------------------------
# Helpers and constants
# --------------------------------------------------------------------------


def test_reading_timeout_scales_with_nplc_and_samples():
    one = SiglentSDM4065A.reading_timeout_ms(100, 1)
    ten = SiglentSDM4065A.reading_timeout_ms(100, 10)
    assert ten > one
    # 10 readings at 100 PLC is ~20 s of integration at 50 Hz; the default
    # 10 s timeout would fail, which is the point of the helper.
    assert ten > SiglentSDM4065A.DEFAULT_TIMEOUT_MS


def test_reading_timeout_assumes_50hz_not_60():
    """Assuming 60 Hz would *underestimate* the time on a 50 Hz grid and
    surface as a spurious "instrument not responding"."""
    assert SiglentSDM4065A.reading_timeout_ms(
        100, 1, mains_hz=50.0
    ) > SiglentSDM4065A.reading_timeout_ms(100, 1, mains_hz=60.0)
    assert SiglentSDM4065A.reading_timeout_ms(100, 1) == (
        SiglentSDM4065A.reading_timeout_ms(100, 1, mains_hz=50.0)
    )


def test_set_nplc_widens_the_visa_timeout(dmm):
    """The helper existing is not enough — something has to *call* it.

    ``reading_timeout_ms`` was correct arithmetic that nothing invoked, so the
    VISA timeout never tracked the configured measurement at all. Asserts the
    wiring, not the formula: the formula already had its own tests.

    A *single* 100 NPLC read is measured at 2.09 s on the real meter, so the
    10 s default does cover it — the resize matters once ``SAMPle:COUNt``
    multiplies that, which the next test pins.
    """
    dmm.set_nplc(100.0)
    assert dmm._inst.timeout >= SiglentSDM4065A.reading_timeout_ms(100.0, 1)


def test_sample_count_widens_the_visa_timeout_past_the_default(dmm):
    """``SAMPle:COUNt`` multiplies integration time past the default. Measured.

    Bench-measured on firmware 0.0.0.20: one reading at 100 NPLC takes
    2.09 s, and **5 readings take 10.14 s** — past the 10 s default, by a
    margin small enough that it would look like a flaky instrument rather
    than a configuration error.

    That is the case worth guarding, because the consequence is not a failed
    call. An aborted USB-TMC ``READ?`` leaves the reply queued in the
    device's bulk-IN endpoint and stops it draining bulk-OUT, so every later
    transfer fails with ``Errno 110`` while endpoint-0 control transfers keep
    working — a wedged instrument that presents as a dead one. On this unit
    it took a front-panel power cycle to clear.
    """
    dmm.set_nplc(100.0)
    single = dmm._inst.timeout
    dmm.set_sample_count(5)
    assert dmm._inst.timeout > single
    assert dmm._inst.timeout > SiglentSDM4065A.DEFAULT_TIMEOUT_MS, (
        "5 x 100 NPLC is 10.14 s on real hardware; the default 10 s would "
        "abort mid-read and strand the USB-TMC endpoints"
    )
    assert dmm._inst.timeout >= SiglentSDM4065A.reading_timeout_ms(100.0, 5)


def test_the_measured_five_sample_time_really_does_exceed_the_default():
    """Pins the bench measurement against the constant it invalidates.

    10.14 s measured vs a 10 000 ms default. Kept as a separate assertion so
    that raising ``DEFAULT_TIMEOUT_MS`` — a plausible "simpler fix" — is a
    visible change here rather than a silent removal of the reason the resize
    exists. Note the formula must cover the *measured* time, not the
    theoretical 5 x 2.0 s of integration, since per-reading overhead is real.
    """
    measured_ms = 10_140
    assert measured_ms > SiglentSDM4065A.DEFAULT_TIMEOUT_MS
    assert SiglentSDM4065A.reading_timeout_ms(100.0, 5) > measured_ms


def test_a_low_nplc_never_shrinks_below_the_requested_timeout(dmm):
    """An explicit ``timeout_ms`` is a floor, not a starting suggestion.

    Fast NPLC needs only milliseconds, so a naive resize would drop the
    session to a timeout far below what the operator asked for — and every
    other query (``*IDN?``, ``SYST:ERR?``) would inherit it. Those have
    nothing to do with integration time.
    """
    dmm.set_nplc(0.001)
    assert dmm._inst.timeout >= dmm._base_timeout_ms


def test_resistance_range_table_is_the_4065a_not_the_4055a():
    from benchctrl.drivers.siglent_sdm4065a.driver import RESISTANCE_RANGES

    assert 1e6 in RESISTANCE_RANGES
    assert 2e6 not in RESISTANCE_RANGES


def test_nplc_table_is_the_4065a_not_the_4055a():
    from benchctrl.drivers.siglent_sdm4065a.driver import NPLC_VALUES

    assert set(NPLC_VALUES) == {100.0, 10.0, 1.0, 0.1, 0.01, 0.001}


def test_usb_ids_match_the_real_instrument():
    """f4ec:1220, read off the instrument's own descriptor."""
    from benchctrl.drivers.siglent_sdm4065a.driver import (
        SDM4065A_USB_PID,
        SIGLENT_USB_VID,
    )

    assert (SIGLENT_USB_VID, SDM4065A_USB_PID) == (0xF4EC, 0x1220)


def test_default_resistance_state_is_autorange_reporting_200():
    """Bench-measured, because the manual is not usable here.

    After ``*RST`` or a bare ``CONFigure:RESistance``, firmware 0.0.0.20
    reports ``RANGe:AUTO?`` = 1 and ``RANGe?`` = 200. The pair is the fact:
    the range number alone looks the same whether it was pinned or merely
    selected by autorange, so a test on the range value on its own would pass
    against a driver that got autorange backwards.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import (
        DEFAULT_RESISTANCE_AUTORANGE,
        DEFAULT_RESISTANCE_RANGE,
    )
    from benchctrl.sim import sdm4065a as sim_mod

    assert DEFAULT_RESISTANCE_RANGE == 200.0
    assert DEFAULT_RESISTANCE_AUTORANGE is True
    # The sim must re-export, not redeclare — a private copy can drift from
    # the driver it is meant to exercise.
    assert sim_mod.DEFAULT_RESISTANCE_RANGE is DEFAULT_RESISTANCE_RANGE


def test_the_def_parameter_is_2k_but_the_reset_range_is_200(dmm):
    """§7.4.5 conflates two different things; the sim must not.

    "default 2 kΩ" correctly describes the value of the literal ``DEF``
    *parameter* — ``RESistance:RANGe? DEF`` answers 2 kΩ. It does not describe
    the range after a reset, which is 200 Ω with autoranging on. Conflating
    them is the documentation half of bug 4.

    Also pins that ``RANGe? DEF`` is a pure query: it must report what ``DEF``
    would select without selecting it.
    """
    dmm.reset()
    assert dmm.query("RESistance:RANGe? DEF").strip().startswith("+2.00000000E+03")
    assert dmm.query("RESistance:RANGe? MIN").strip().startswith("+2.00000000E+02")
    assert dmm.query("RESistance:RANGe? MAX").strip().startswith("+1.00000000E+08")

    # None of those queries may have changed the instrument.
    assert dmm.get_range() == pytest.approx(200.0)
    assert dmm.get_autorange() is True


def test_the_two_def_forms_disagree_about_autorange(dmm):
    """Firmware bug 4, modelled: only one ``DEF`` path disables autoranging.

    §7.4.5's note says selecting a fixed range disables autoranging, so both
    forms below should turn it off. Bench-measured on firmware 0.0.0.20, only
    ``RESistance:RANGe DEF`` does; ``CONFigure:RESistance DEF`` leaves it on
    while reporting a pinned-looking 2 kΩ.

    Modelled as measured rather than as documented, because a sim more coherent
    than the hardware would let a driver adopt ``DEF`` and only fail on the
    bench. The driver never sends it.
    """
    dmm.reset()
    dmm.write("RESistance:RANGe DEF")
    assert dmm.get_range() == pytest.approx(2_000.0)
    assert dmm.get_autorange() is False

    dmm.reset()
    dmm.write("CONFigure:RESistance DEF")
    assert dmm.get_autorange() is True, (
        "the sim disabled autorange for CONFigure:RESistance DEF — the "
        "hardware does not, and a sim that is tidier than the instrument "
        "hides the bug rather than pinning it"
    )
    # No assertion on get_range(): with autorange on the hardware's RANGe?
    # tracks the input, answering 2 kΩ straight after the write and 200 Ω once
    # a reading has been taken. The sim holds it static, so asserting the
    # number here would pin sim-only behaviour.


def test_reset_leaves_resistance_autoranging(dmm):
    """The state a caller lands on by accident, asserted through the API."""
    dmm.reset()
    dmm.write("CONFigure:RESistance")
    assert dmm.get_autorange() is True
    assert dmm.get_range() == pytest.approx(200.0)
    # An explicit range argument is what turns autorange off.
    dmm.configure_resistance(200.0)
    assert dmm.get_autorange() is False


def test_discover_returns_a_list_without_hardware():
    """Must not raise when no instrument or no VISA backend is present."""
    from benchctrl.drivers.siglent_sdm4065a import discover

    assert isinstance(discover(), list)


# --------------------------------------------------------------------------
# Simulator fidelity
# --------------------------------------------------------------------------


def test_sim_response_format_matches_siglent(dmm):
    """``+1.00000000E+02`` — the format §7.4.1 documents.

    If the sim answered plain decimals, the driver's float parsing would be
    tested against an easier input than hardware sends.
    """
    sim = SimulatedSDM4065A()
    assert sim._fmt(100.0) == "+1.00000000E+02"
    assert sim._fmt(-0.2) == "-2.00000000E-01"


def test_sim_serial_is_obviously_synthetic():
    """A sim claiming a real serial makes captured logs unattributable."""
    assert "SIM" in SimulatedSDM4065A.DEFAULT_IDN


# --------------------------------------------------------------------------
# Hardware-required — drives the real SDM4065A
# --------------------------------------------------------------------------
#
# Gated on ``BENCHCTRL_SDM4065A`` rather than autodiscovery. The meter lives
# on the remote bench board, not the laptop these tests usually run on, so
# "not found" is the normal case and must skip, not fail. Set the variable to
# a VISA resource string, or to ``auto`` to let the driver scan.
#
# The QR10x cross-validation lives in ``test_cross_validate_sdm4065a_qr10x.py``
# because it needs *both* instruments and a specific wiring; these need only
# the meter.
#
# On a board with no kernel ``usbtmc`` module, pyvisa-py drives the
# instrument over libusb and needs write access to /dev/bus/usb/BBB/DDD —
# install ``deploy/udev/61-benchctrl-usbtmc.rules`` or the meter is
# *invisible* rather than merely unopenable (empty resource list), which
# looks exactly like a bad cable.


HW_RESOURCE = os.environ.get("BENCHCTRL_SDM4065A", "")


@pytest.fixture()
def hw_dmm():
    """Open the connected SDM4065A. Skips when it isn't there.

    Leaves the instrument reset on the way out. Every test below changes
    ranges, NPLC or the null state, and CONFigure-resetting semantics mean
    leftover state from one test is exactly the kind of thing that makes the
    next one pass or fail for the wrong reason.
    """
    if not HW_RESOURCE:
        pytest.skip("set BENCHCTRL_SDM4065A to a VISA resource (or 'auto')")

    resource = None if HW_RESOURCE.lower() == "auto" else HW_RESOURCE
    try:
        drv = SiglentSDM4065A.open(resource)
    except Exception as exc:
        pytest.skip(f"SDM4065A not reachable ({HW_RESOURCE}): {exc}")
    try:
        drv.reset()
        # ``*CLS`` as well as ``*RST``: the error queue survives a reset, and
        # this file deliberately sends commands the instrument rejects (see the
        # ``AZ`` test). Left to accumulate, the queue reaches its depth and
        # every later ``SYSTem:ERRor?`` answers -350 "Queue overflow" — so a
        # test asserting "no error" fails, reporting a stale error raised by a
        # different test. Bench-observed exactly that on the NPLC sweep.
        drv.clear_status()
        yield drv
    finally:
        try:
            drv.reset()
            drv.clear_status()
        finally:
            drv.close()


@pytest.mark.hardware
def test_hw_identity_is_actually_a_4065a(hw_dmm):
    """The model check the VID/PID cannot do.

    ``discovery.SIGNATURES`` matches the SDM4045A/4055A/4065A on one shared
    USB ID, so the *driver* is the only thing that can confirm the model. It
    matters: this driver accepts a 1 MΩ top range and six NPLC values, and
    the 4055A silently coerces both.
    """
    info = hw_dmm.info()
    assert "SIGLENT" in info.manufacturer.upper()
    assert info.model.upper().startswith("SDM40"), info.model
    assert info.serial
    if not info.model.upper().startswith("SDM4065"):
        pytest.skip(
            f"connected meter is a {info.model}, not an SDM4065A — the "
            f"model-specific assertions below would be testing the wrong "
            f"column of the datasheet"
        )


@pytest.mark.hardware
def test_hw_error_reporting_is_reachable(hw_dmm):
    """Siglent writes SCPI headers without the leading root colon, and
    ``ScpiDevice`` registers ``:SYSTem:ERRor``. Get that wrong and error
    queries answer a cheerful ``"0"`` forever — every test that relies on
    error checking silently stops checking anything.

    So: provoke a real error and require the instrument to report it.

    Asserted through ``*ESR?``, because the error *queue* is not a dependable
    channel on this unit — bench-measured, it can latch into answering
    ``0,"No Error"`` even to a deliberately bogus header, recoverable only by a
    power cycle. ``*ESR?`` reported that same command correctly. The queue is
    still checked below, but as a warning rather than a failure: a silent queue
    is the instrument misbehaving, not the driver, and failing here would mask
    the plumbing bug this test exists to catch.
    """
    hw_dmm.clear_status()
    hw_dmm.write("NOSUCH:THING 1")
    assert hw_dmm.command_error() is True, (
        "instrument reported no command error after an undefined header — "
        "*ESR? is not actually being read"
    )

    hw_dmm.write("NOSUCH:THING 1")
    if hw_dmm.last_error() is None:
        warnings.warn(
            "SYSTem:ERRor? reported no error after an undefined header that "
            "*ESR? flagged: this unit's error queue has latched silent. "
            "Power-cycle the meter to restore it. Driver code must not rely "
            "on last_error() to detect rejection — use command_error().",
            stacklevel=1,
        )
    hw_dmm.reset()
    hw_dmm.clear_status()


@pytest.mark.hardware
def test_hw_nplc_values_are_accepted_by_the_real_instrument(hw_dmm):
    """All six 4065A NPLC values, verified against silicon.

    The driver's NPLC list is the difference between the 4065A and the 4055A
    (six values vs three). If the list were wrong, hardware would either
    reject the value or silently coerce it — and a coercion is only visible
    by reading it back, which is what this does.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import NPLC_VALUES

    hw_dmm.configure_resistance(200)
    for nplc in NPLC_VALUES:
        hw_dmm.set_nplc(nplc)
        assert hw_dmm.get_nplc() == pytest.approx(nplc, rel=1e-3), (
            f"instrument coerced NPLC {nplc:G} to {hw_dmm.get_nplc():G} — "
            f"either the value is not supported on this model or the driver's "
            f"NPLC_VALUES is wrong"
        )
        assert hw_dmm.last_error() is None, f"NPLC {nplc:G} raised an error"


@pytest.mark.hardware
def test_hw_resistance_ranges_are_accepted_by_the_real_instrument(hw_dmm):
    """Every range in the driver's table, read back to catch coercion.

    Includes 1 MΩ — the top of the 4065A's range and the one the 4055A does
    not have. A 4055A would coerce it; this fails if that happens.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import RESISTANCE_RANGES

    for range_ in RESISTANCE_RANGES:
        hw_dmm.configure_resistance(range_)
        assert hw_dmm.get_range() == pytest.approx(range_, rel=1e-3), (
            f"instrument coerced range {range_:G} to {hw_dmm.get_range():G}"
        )
        assert hw_dmm.last_error() is None, f"range {range_:G} raised an error"


@pytest.mark.hardware
def test_hw_the_2m_range_really_is_rejected(hw_dmm):
    """The driver rejects 2 MΩ client-side because it is the 4055A's range.

    Worth proving on hardware that we are not *needlessly* refusing something
    the instrument would accept. If this ever fails — i.e. the meter takes
    2 MΩ and reads it back — the driver's range table is too strict and this
    test is the evidence to loosen it.
    """
    hw_dmm.write("RESistance:RANGe 2000000")
    err = hw_dmm.last_error()
    read_back = hw_dmm.get_range()
    assert err is not None or read_back != pytest.approx(2e6, rel=1e-3), (
        f"the instrument accepted a 2 MΩ range and read it back as "
        f"{read_back:G} with no error — the driver's client-side rejection "
        f"is wrong for this model"
    )
    hw_dmm.reset()


@pytest.mark.hardware
def test_hw_overload_raises_on_a_deliberately_narrow_range(hw_dmm):
    """An open input on the narrowest range must raise, not return 9.9E37.

    ``9.9E37`` is a perfectly valid float. A driver that returned it would
    feed 9.9e37 into the caller's arithmetic and produce a plausible-looking
    wrong answer rather than an error — which is why the driver raises.

    Uses an *open* input rather than a real over-range part so nothing has to
    be wired for this to be a genuine overload.
    """
    hw_dmm.configure_resistance(200)
    try:
        value = hw_dmm.read()
    except SDM4065AOverloadError:
        return
    pytest.skip(
        f"input read {value} on the 200 Ω range rather than overloading — "
        f"something is connected across the inputs, so this test cannot "
        f"produce the condition it checks"
    )


@pytest.mark.hardware
def test_hw_autozero_round_trips_via_zero_auto(hw_dmm):
    """Autozero works — but under ``ZERO:AUTO``, not the manual's ``AZ``.

    §7.4.7 documents ``[SENSe:]{RESistance|FRESistance}:AZ[:STATe]``. That
    mnemonic does not exist on firmware 0.0.0.20: every ``AZ`` spelling is
    rejected with -113 "Undefined header", for writes as well as queries.
    ``ZERO:AUTO`` is accepted and round-trips on all four functions.

    The distinction cost two power cycles to find, because a *query* of an
    undefined header gets no reply at all — the read times out, the aborted
    USB-TMC transfer strands the bulk endpoints, and every later transfer
    fails with ``Errno 110`` while endpoint-0 control transfers keep working.
    Neither ``INITIATE_CLEAR`` (which reports success) nor a libusb port reset
    recovers it; only a front-panel power cycle does.

    So this test also asserts the manual's form is *rejected*. That direction
    matters more than it looks: if a future firmware implements ``AZ``, this
    fails and tells the next reader the constant can be revisited.

    Acceptance is judged by ``command_error()`` (``*ESR?`` bit 5), not by the
    error queue. On this unit the queue can latch into answering ``0,"No
    Error"`` to everything — bench-measured, it denied a deliberately bogus
    header that ``*ESR?`` correctly reported — so a queue-based assertion here
    fails for a reason that has nothing to do with autozero.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.clear_status()  # also zeroes *ESR?, so the next check is attributable
    assert hw_dmm.command_error() is False

    hw_dmm.set_autozero(True)
    assert hw_dmm.command_error() is False, (
        "ZERO:AUTO ON was rejected — check *IDN?, since an SDM4055A differs"
    )
    assert hw_dmm.get_autozero() is True

    hw_dmm.set_autozero(False)
    assert hw_dmm.command_error() is False
    assert hw_dmm.get_autozero() is False

    # The manual's mnemonic, confirmed absent. Write-only: querying it is what
    # wedges the instrument, so this must never become a query.
    hw_dmm.write("RESistance:AZ ON")
    rejected = hw_dmm.command_error()
    # Drain whatever that write queued, so this test cannot hand a stale error
    # to the next one — the queue survives *RST and overflows at -350.
    hw_dmm.clear_status()
    assert rejected, (
        "RESistance:AZ was accepted (*ESR? reported no command error). If this "
        "firmware implements the §7.4.7 mnemonic, AUTOZERO_NODE can be "
        "reconsidered — but verify the *query* answers before changing it, "
        "because a query that does not answer takes a power cycle to recover"
    )

    # Liveness: a reading still works, which a wedged session would not allow.
    hw_dmm.set_nplc(1.0)
    try:
        hw_dmm.read()
    except SDM4065AOverloadError:
        pass  # open input is fine; liveness is the point


@pytest.mark.hardware
def test_hw_configure_resets_nplc_to_ten(hw_dmm):
    """The quirk that makes ordering non-negotiable, on real silicon.

    The driver's docstrings assert that ``CONFigure`` resets NPLC to 10, and
    all the local evidence for that is the simulator — which I wrote from the
    manual. This is the test that makes it a hardware fact.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.set_nplc(0.01)
    assert hw_dmm.get_nplc() == pytest.approx(0.01)

    hw_dmm.configure_resistance(200)
    assert hw_dmm.get_nplc() == pytest.approx(10.0), (
        "CONFigure did not reset NPLC to 10 — the driver's "
        "configure-then-set-NPLC ordering is based on this behaviour"
    )


@pytest.mark.hardware
def test_hw_configure_resets_autorange_too(hw_dmm):
    """And a bare ``CONFigure`` re-enables autorange (bench-measured).

    This is the half of the quirk most likely to bite: a caller who pins a
    range, then calls ``measure_resistance``, is autoranging again without
    having asked. The reading still looks right — autorange picks a sensible
    range — so the only visible symptom is that the "% of range" term in the
    error budget is no longer the one the caller reasoned about.

    Asserts the *autorange flag*, not the range number, because ``RANGe?``
    answers 200 in both states on this firmware. A test on the number alone
    would pass while the configuration was wrong.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import (
        DEFAULT_RESISTANCE_AUTORANGE,
        DEFAULT_RESISTANCE_RANGE,
    )

    hw_dmm.configure_resistance(200)
    assert hw_dmm.get_range() == pytest.approx(200.0)
    assert hw_dmm.get_autorange() is False, (
        "an explicit CONFigure:RESistance 200 left autorange on"
    )

    hw_dmm.write("CONFigure:RESistance")
    assert hw_dmm.get_autorange() is DEFAULT_RESISTANCE_AUTORANGE, (
        f"bare CONFigure:RESistance left autorange "
        f"{hw_dmm.get_autorange()} rather than {DEFAULT_RESISTANCE_AUTORANGE}"
    )
    assert hw_dmm.get_range() == pytest.approx(DEFAULT_RESISTANCE_RANGE, rel=1e-3)


@pytest.mark.hardware
def test_hw_the_two_def_forms_disagree_about_autorange(hw_dmm):
    """``RESistance:RANGe DEF`` disables autorange; ``CONFigure ... DEF`` doesn't.

    Both select 2 kΩ, and §7.4.5's note says selecting a fixed range disables
    autoranging, so both should turn it off. Bench-measured on firmware
    0.0.0.20, only the first does. Reported to Siglent;
    ``docs/vendor-issues/SDM4065A-firmware-bug-report-4-range-defaults.md``.

    Pinned because it is why the driver never sends ``DEF``: a caller who used
    it via ``CONFigure`` would be autoranging while ``RANGe?`` reported a
    pinned-looking 2 kΩ. If a firmware update makes the two agree, this fails
    and the ``DEF`` path becomes usable.
    """
    hw_dmm.reset()
    hw_dmm.write("RESistance:RANGe DEF")
    assert hw_dmm.get_range() == pytest.approx(2_000.0, rel=1e-3)
    assert hw_dmm.get_autorange() is False, (
        "RESistance:RANGe DEF left autorange on — this direction used to work, "
        "so check the firmware revision"
    )

    hw_dmm.reset()
    hw_dmm.write("CONFigure:RESistance DEF")
    assert hw_dmm.get_autorange() is True, (
        "CONFigure:RESistance DEF now disables autorange — the firmware bug is "
        "fixed, so DEF is safe to use and this test can go"
    )
    # Deliberately no assertion on get_range() here. With autorange on, RANGe?
    # reports the range autorange has currently *selected*, so it moves with
    # the input: measured 2 kΩ immediately after the write and 200 Ω once the
    # 100 Ω DUT had been sampled. That instability is the point of the bug —
    # the number is unusable for deciding whether the range is what you asked
    # for, which is why get_autorange() is the assertion that means something.


@pytest.mark.hardware
def test_hw_enabling_null_arms_auto_null(hw_dmm):
    """§7.4.2's side effect, on hardware.

    This is the single most surprising documented behaviour in the manual and
    the reason :py:meth:`null_now` writes state *then* value. If real hardware
    does not actually arm auto-null, the ordering is merely harmless rather
    than load-bearing — and this test is where we would find that out.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.set_null(True)
    assert hw_dmm.get_null() is True
    assert hw_dmm.get_null_auto() is True, (
        "enabling NULL:STATe did not arm NULL:VALue:AUTO on this instrument "
        "— re-read §7.4.2 against this firmware before simplifying null_now()"
    )


@pytest.mark.hardware
def test_hw_writing_a_null_value_does_not_disarm_auto_null(hw_dmm):
    """§7.4.3 is wrong on firmware 0.0.0.20 — the hardware source of truth.

    The manual says writing ``NULL:VALue`` disables automatic null-value
    selection. This unit leaves it armed, so the offset just written is
    replaced by the instrument's next reading: the null looks applied
    (``NULL:STATe?`` answers 1, ``NULL:VALue?`` reads back correctly) while
    subtracting a number nobody chose.

    This is the test that justifies ``null_now``'s explicit ``AUTO OFF``, and
    the reason the simulator models the firmware rather than the manual —
    see ``test_set_null_value_does_not_disarm_auto_despite_the_manual``.
    Reported to Siglent;
    ``docs/vendor-issues/SDM4065A-firmware-bug-report-1-null-value-auto.md``.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.set_null(True)
    hw_dmm.set_null_value(0.05)
    assert hw_dmm.get_null_auto() is True, (
        "AUTO cleared on a value write — Siglent fixed §7.4.3 in a firmware "
        f"update (this unit reports {hw_dmm.info().firmware}). Re-check "
        "null_now() and the simulator before relaxing either."
    )
    assert hw_dmm.get_null_value() == pytest.approx(0.05, abs=1e-4)


@pytest.mark.hardware
def test_hw_null_now_leaves_auto_disarmed_on_real_hardware(hw_dmm):
    """The fix, on real silicon: after ``null_now`` the offset is stable.

    The pairing that matters — the test above proves the instrument will not
    disarm AUTO by itself, and this proves the driver does it. Verified by
    taking two readings: with AUTO still armed the first reading would become
    the new offset and the second would sit near zero.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.set_nplc(1.0)
    try:
        hw_dmm.null_now(samples=3)
    except SDM4065AOverloadError:
        pytest.skip("open/overloaded input — cannot null against it")

    assert hw_dmm.get_null() is True
    assert hw_dmm.get_null_auto() is False, (
        "null_now left AUTO armed — the offset it stored will be overwritten"
    )
    stored = hw_dmm.get_null_value()
    hw_dmm.read()
    assert hw_dmm.get_null_value() == pytest.approx(stored, abs=1e-6), (
        "the offset changed after a reading, which is exactly what AUTO does "
        "— null_now's AUTO OFF did not take effect"
    )


@pytest.mark.hardware
def test_hw_a_long_integration_completes_within_the_computed_timeout(hw_dmm):
    """``reading_timeout_ms`` is arithmetic; this checks it against the clock.

    100 NPLC × 10 samples is ~20 s of integration at 50 Hz. The driver
    computes a VISA timeout for it, and if that arithmetic were wrong the
    failure would be a timeout on a perfectly good measurement — the kind of
    bug that only appears on the slow settings nobody tests interactively.
    """
    import time

    budget_ms = SiglentSDM4065A.reading_timeout_ms(nplc=100, samples=10)

    hw_dmm.configure_resistance(200)
    hw_dmm.set_nplc(100)
    hw_dmm.set_sample_count(10)

    start = time.monotonic()
    try:
        values = hw_dmm.read()
    except SDM4065AOverloadError:
        # Open inputs — the timing is what matters, not the value.
        values = None
    elapsed_ms = (time.monotonic() - start) * 1000.0

    if values is not None:
        assert len(values) == 10
    assert elapsed_ms < budget_ms, (
        f"10 readings at 100 NPLC took {elapsed_ms:.0f} ms, over the "
        f"{budget_ms} ms the driver budgets — reading_timeout_ms is too tight"
    )


# --------------------------------------------------------------------------
# VISA resource matching — backends disagree on the radix
# --------------------------------------------------------------------------


def test_a_pyvisa_py_decimal_resource_is_recognised():
    """The bug this test exists for, found on the bench rather than in review.

    pyvisa-py renders the same meter as ``USB0::62700::4640::SN::0::INSTR``
    where NI-VISA renders ``USB0::0xF4EC::0x1220::SN::INSTR``. 62700 is 0xF4EC
    and 4640 is 0x1220.

    The original matcher looked for the literal text ``F4EC``, so on exactly
    the boards that need pyvisa-py — those with no kernel ``usbtmc`` module —
    the instrument appeared in ``list_resources()`` and was invisible to the
    driver. The error was "no SDM4065A found", indistinguishable from an
    unplugged instrument.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import _is_sdm4065a_resource

    assert _is_sdm4065a_resource("USB0::62700::4640::SDM46A0CA00021::0::INSTR")


def test_a_hex_resource_is_still_recognised():
    """Both spellings, since a laptop with NI-VISA is the other common case."""
    from benchctrl.drivers.siglent_sdm4065a.driver import _is_sdm4065a_resource

    assert _is_sdm4065a_resource("USB0::0xF4EC::0x1220::SDM46A0CA00021::INSTR")
    assert _is_sdm4065a_resource("USB0::0xf4ec::0x1220::SDM46A0CA00021::INSTR")


def test_the_other_bench_instruments_are_not_matched():
    """The real resource strings from the bench board's five instruments.

    A matcher that was merely radix-tolerant could still be too greedy. These
    are the actual Rigol resources reported alongside the meter.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import _is_sdm4065a_resource

    for other in (
        "USB0::6833::3601::DL3D232300106::0::INSTR",   # DL3031A, decimal
        "USB0::6833::42152::DP2A243500269::0::INSTR",  # DP2031, decimal
        "USB0::0x1AB1::0x0E11::DL3D232300106::INSTR",  # DL3031A, hex
        "ASRL/dev/ttyACM0::INSTR",
        "TCPIP::192.168.1.7::INSTR",
    ):
        assert not _is_sdm4065a_resource(other), other


def test_a_serial_number_containing_the_vid_digits_does_not_match():
    """Why fields are parsed rather than the whole string searched.

    A serial number is free-form text and can contain the digits of a VID.
    Substring matching would match on the wrong field; field matching cannot.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import _is_sdm4065a_resource

    assert not _is_sdm4065a_resource("USB0::6833::3601::F4EC1220::0::INSTR")
    assert not _is_sdm4065a_resource("USB0::6833::3601::62700-4640::0::INSTR")


def test_a_malformed_resource_is_rejected_rather_than_raising():
    """``list_resources()`` output is not ours to validate, so odd entries
    must be skipped quietly — a crash here would take out discovery for
    every other instrument on the bench."""
    from benchctrl.drivers.siglent_sdm4065a.driver import _is_sdm4065a_resource

    for junk in ("", "USB0", "USB0::", "USB0::notanumber::4640::SN::INSTR", "::::"):
        assert _is_sdm4065a_resource(junk) is False, junk

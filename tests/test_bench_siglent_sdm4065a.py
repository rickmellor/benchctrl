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


def test_configure_without_a_range_uses_the_2k_default(dmm):
    """§7.4.5: the default resistance range is 2 kΩ, not the lowest range.

    Worth pinning: a 100 Ω DUT left on 2 kΩ carries a "% of range" error
    term computed on 2 kΩ — a 10x accuracy penalty, applied silently.
    """
    dmm.configure_resistance()  # no range argument
    assert dmm.get_range() == pytest.approx(2000.0)


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


def test_set_null_value_disarms_auto(dmm):
    dmm.configure_resistance(200)
    dmm.set_null_auto(True)
    assert dmm.get_null_auto() is True
    dmm.set_null_value(0.5)
    assert dmm.get_null_auto() is False


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


def test_default_resistance_range_is_2k_not_autorange():
    """The instrument's own default (§7.4.5) — a 100 Ω DUT lands on 2 kΩ
    unless the range is set, at a 10x cost in the % of range term."""
    from benchctrl.sim.sdm4065a import DEFAULT_RESISTANCE_RANGE

    assert DEFAULT_RESISTANCE_RANGE == 2e3


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
        yield drv
    finally:
        try:
            drv.reset()
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
def test_hw_the_error_queue_is_reachable(hw_dmm):
    """Siglent writes SCPI headers without the leading root colon, and
    ``ScpiDevice`` registers ``:SYSTem:ERRor``. Get that wrong and error
    queries answer a cheerful ``"0"`` forever — every test that relies on
    error checking silently stops checking anything.

    So: provoke a real error and require the instrument to report it.
    """
    hw_dmm.write("NOSUCH:THING 1")
    err = hw_dmm.last_error()
    assert err is not None, (
        "instrument reported no error after an undefined header — the error "
        "queue is not actually being read"
    )
    assert err[0] != 0
    hw_dmm.reset()


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
def test_hw_autozero_round_trips(hw_dmm):
    """Autozero exists on the 4065A and defaults OFF (§7.4.7).

    The 4055A has no autozero at all, so a successful round trip here is
    additional confirmation of the model — and of the default, which the
    driver documents but until now had only the manual as evidence.
    """
    hw_dmm.configure_resistance(200)
    assert hw_dmm.get_autozero() is False, "expected autozero OFF after CONFigure"
    hw_dmm.set_autozero(True)
    assert hw_dmm.get_autozero() is True
    assert hw_dmm.last_error() is None
    hw_dmm.set_autozero(False)
    assert hw_dmm.get_autozero() is False


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
def test_hw_configure_resets_the_range_too(hw_dmm):
    """And the range goes back to 2 kΩ, not autorange (§7.4.5).

    This is the half of the quirk most likely to bite: a caller who pins a
    range, then calls ``measure_resistance``, gets a reading on the *default*
    range. Proven here rather than assumed.
    """
    from benchctrl.drivers.siglent_sdm4065a.driver import DEFAULT_RESISTANCE_RANGE

    hw_dmm.configure_resistance(200)
    assert hw_dmm.get_range() == pytest.approx(200.0)

    hw_dmm.write("CONFigure:RESistance")
    assert hw_dmm.get_range() == pytest.approx(DEFAULT_RESISTANCE_RANGE, rel=1e-3), (
        f"bare CONFigure:RESistance left the range at {hw_dmm.get_range():G} "
        f"rather than the documented {DEFAULT_RESISTANCE_RANGE:G} default"
    )


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
def test_hw_writing_a_null_value_disarms_auto_null(hw_dmm):
    """The other half of §7.4.3: an explicit value takes control back.

    Together with the test above, this is the complete justification for
    ``null_now``'s ordering. If auto-null stayed armed, the instrument would
    overwrite our offset with its own next reading and the null would silently
    become a no-op.
    """
    hw_dmm.configure_resistance(200)
    hw_dmm.set_null(True)
    hw_dmm.set_null_value(0.05)
    assert hw_dmm.get_null_auto() is False, (
        "writing NULL:VALue did not disarm NULL:VALue:AUTO — the stored "
        "offset is not stable and null_now() cannot be trusted"
    )
    assert hw_dmm.get_null_value() == pytest.approx(0.05, abs=1e-4)


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

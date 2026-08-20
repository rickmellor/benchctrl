"""Cross-validate the SDM4065A against the QR10x — two instruments, one resistance.

Why this file exists
--------------------
Every other test in the tree checks that a driver says what the manual says it
should. None of them can catch a driver that is *consistently* wrong: a units
error, a range mis-scaling, a null applied with the wrong sign. Those produce
self-consistent readings that pass every single-instrument test.

Two independent instruments measuring the same physical resistance is the check
that catches them, because the QR10x's error and the SDM4065A's error have no
common cause. If both agree on 100.04 Ω, neither has a sign error.

What is being compared
----------------------
Three distinct quantities, easy to conflate:

* **SP** — what the QR10x was *commanded* to be.
* **PV** — what the QR10x *believes* it is (its own internal measurement).
* the SDM4065A reading.

DMM-vs-PV is the real cross-validation: both are measurements of the same
physical thing, so the comparison is limited only by the two accuracy specs.
DMM-vs-SP additionally includes the QR10x's *setting* error, which is a
different (and larger) budget. The tests below keep them separate, because
folding them together would produce a tolerance loose enough to hide a real
disagreement.

What this can and cannot resolve
--------------------------------
The QR10x's ±0.05% spec dominates the meter's, so the *agreement* budget at
100 Ω is about 0.07 Ω — nearly 2x the ~38 mΩ offset the QR101A-1M-R1 shows at
a 100 Ω setpoint. So this file is a check for **gross** errors — units,
scaling, a swapped 2-/4-wire function, a null with the wrong sign — not a
precision comparison. The meter alone (0.02 Ω at 100 Ω on the 200 Ω range)
resolves 38 mΩ; the pair does not. ``test_only_the_meter_resolves_the_38_
milliohm_offset_not_the_comparison`` pins that distinction so the tolerance
here is never mistaken for a precision claim.

Wiring
------
Set ``BENCHCTRL_SDM4065A_WIRING=4`` when sense leads are connected (the
primary path — 4-wire is the only way the datasheet's resistance spec holds)
or ``=2`` for source leads only. With 2-wire, the tests null first and say so;
without a null, 2-wire carries ~0.2 Ω of lead error (datasheet note [6]) —
larger than the entire agreement budget it is added to, so an unnulled 2-wire
comparison is nearly useless.

Requires both instruments:
``BENCHCTRL_SDM4065A`` (VISA resource, or ``auto``) and
``BENCHCTRL_QR10X_PORT``.
"""

from __future__ import annotations

import os

import pytest

from benchctrl.drivers.eastwood_qr10x import QR10x
from benchctrl.drivers.siglent_sdm4065a import (
    SDM4065AOverloadError,
    SiglentSDM4065A,
)

HW_RESOURCE = os.environ.get("BENCHCTRL_SDM4065A", "")
HW_QR_PORT = os.environ.get("BENCHCTRL_QR10X_PORT", "")
WIRING = os.environ.get("BENCHCTRL_SDM4065A_WIRING", "4").strip()

#: NPLC for every comparison below. 100 is the *only* setting at which the
#: datasheet's accuracy figures apply as written — note [1]: "Specifications
#: are for 90 minutes warm-up, and integration time 100 PLC. For integration
#: time < 100 PLC, add the appropriate RMS Noise Adder". Using 100 NPLC means
#: :py:func:`dmm_accuracy_ohm` needs no noise term, so the budget below is the
#: datasheet's own number rather than my arithmetic on top of it.
CROSS_VAL_NPLC = 100.0

#: SDM4065A 1-year resistance accuracy, ±(% of reading + % of range).
#: Datasheet "DC Characteristics", Resistance rows. 1-year rather than 24-hour
#: because we have no recent calibration date for this unit — assuming the
#: 24-hour column would silently tighten the budget by 3x on an uncalibrated
#: meter and turn ordinary drift into a test failure.
_DMM_RES_ACCURACY: dict[float, tuple[float, float]] = {
    200.0: (0.010, 0.005),
    2e3: (0.010, 0.001),
    20e3: (0.010, 0.001),
    200e3: (0.010, 0.001),
    1e6: (0.012, 0.001),
}

#: QR10x accuracy, as a fraction of setpoint. ``docs/drivers.md`` records
#: "±0.02% to ±0.05% accuracy" depending on model; the worse figure is used
#: because the driver cannot read its own accuracy class off the instrument.
#: Deliberately conservative: this test exists to catch gross driver errors,
#: and a false failure from an over-tight QR10x budget would be noise.
QR10X_REL_ACCURACY = 0.0005

#: Floor on the QR10x's contribution. A pure percentage goes to zero at low
#: resistance, which no real instrument does.
QR10X_FLOOR_OHM = 0.02

#: Datasheet note [6]: unnulled 2-wire adds this much lead/contact resistance.
#:
#: Kept at the datasheet's figure deliberately, though our own leads measure
#: 78.9 mΩ (see :py:func:`test_4_wire_beats_2_wire_by_about_the_lead_resistance`
#: and ``KNOWN_LIMITATIONS`` H-5). This is a *budget bound*, and lead resistance
#: is a property of whatever cables are plugged in, not of the meter — tightening
#: it to our measurement would make the suite fail for anyone with longer leads
#: or dirtier contacts, which is not a driver defect. The measured number is
#: recorded as evidence that 4-wire works; the bound stays conservative.
TWO_WIRE_LEAD_OHM = 0.2

#: The setpoints to compare at. Confined to the 200 Ω range: that is where the
#: QR10x is most accurate, where lead resistance matters most (so where the
#: 4-wire path is actually being exercised), and it keeps every reading on one
#: DMM range so a range-scaling error cannot hide behind a range change.
#:
#: Filtered against the instrument's own ``RLIMIT`` at run time — see
#: :py:func:`usable_setpoints`.
SETPOINTS_OHM = (10.0, 47.0, 100.0, 150.0)


def usable_setpoints(rlimit_ohm: float) -> tuple[float, ...]:
    """The setpoints this QR10x will actually reach, given its ``RLIMIT``.

    ``RLIMIT`` is a device-enforced *minimum* resistance — a power-rating
    guard — and any setpoint below it is **clamped silently**, not rejected.
    The bench unit ships at 12 Ω, which would clamp the 10 Ω entry above.

    A clamp is the dangerous case: the meter would correctly read ~12 Ω, the
    test would compare it against a requested 10 Ω, and the resulting 2 Ω
    disagreement would look like a driver bug. Filtering here means an
    unreachable setpoint is *skipped and reported* rather than silently
    turned into a false failure — and lowering RLIMIT to widen the sweep
    stays an explicit operator decision about power dissipation, not
    something a test does behind your back.
    """
    return tuple(sp for sp in SETPOINTS_OHM if sp > rlimit_ohm)


def dmm_accuracy_ohm(reading_ohm: float, range_ohm: float) -> float:
    """The SDM4065A's own 1-year accuracy budget, in ohms, at 100 NPLC.

    ±(% of reading + % of range) — the "% of range" term is why measuring
    100 Ω on the 200 Ω range is far better than on the 2 kΩ range, and why
    these tests pin the range instead of autoranging.
    """
    pct_reading, pct_range = _DMM_RES_ACCURACY[range_ohm]
    return abs(reading_ohm) * pct_reading / 100.0 + range_ohm * pct_range / 100.0


def qr10x_accuracy_ohm(setpoint_ohm: float) -> float:
    """The QR10x's accuracy budget, in ohms."""
    return max(QR10X_FLOOR_OHM, setpoint_ohm * QR10X_REL_ACCURACY)


def agreement_budget_ohm(
    setpoint_ohm: float,
    reading_ohm: float,
    range_ohm: float,
    *,
    four_wire: bool,
    nulled: bool,
) -> float:
    """How far the two instruments may legitimately disagree, in ohms.

    A **linear sum** of the two budgets, not a root-sum-square. RSS is right
    for independent random errors; these are specification *bounds*, and two
    instruments each allowed ±X can legitimately sit 2X apart. Using RSS here
    would produce a tolerance the hardware is permitted to violate, i.e. a
    test that fails on conforming instruments.

    The 2-wire lead term is added only when no null has been taken, because
    that is exactly the condition note [6] describes.
    """
    budget = dmm_accuracy_ohm(reading_ohm, range_ohm) + qr10x_accuracy_ohm(
        setpoint_ohm
    )
    if not four_wire and not nulled:
        budget += TWO_WIRE_LEAD_OHM
    return budget


# ---------------------------------------------------------------------------
# Hardware-free — the budget arithmetic itself
# ---------------------------------------------------------------------------
#
# These run everywhere. The threshold is the load-bearing part of this file:
# if it is too loose the cross-validation proves nothing, and a silently
# loosened budget is invisible in a passing test run. So the numbers are
# pinned here against the datasheet, independently of any hardware.


def test_dmm_accuracy_at_100_ohm_on_the_200_ohm_range():
    """±(0.010% of reading + 0.005% of range) at 100 Ω on the 200 Ω range.

    = 0.01 Ω + 0.01 Ω = 0.02 Ω. Worth having as a literal: it says the meter
    can resolve the QR10x's 38 mΩ offset, which is the premise of the whole
    cross-validation.
    """
    assert dmm_accuracy_ohm(100.0, 200.0) == pytest.approx(0.02, abs=1e-9)


def test_the_range_term_dominates_at_low_resistance():
    """At 10 Ω the reading term is 1 mΩ and the range term is still 10 mΩ.

    This is why the tests pin the 200 Ω range rather than autoranging: on the
    2 kΩ range the range term alone would be 20 mΩ, and the QR10x's offset
    would be inside the noise.
    """
    assert dmm_accuracy_ohm(10.0, 200.0) == pytest.approx(0.011, abs=1e-9)
    assert dmm_accuracy_ohm(10.0, 2e3) == pytest.approx(0.021, abs=1e-9)


def test_only_the_meter_resolves_the_38_milliohm_offset_not_the_comparison():
    """Which claim the 38 mΩ figure actually supports — and which it doesn't.

    The QR101A-1M-R1 reads 100.038 Ω at a 100.0 Ω setpoint. It is tempting to
    say the cross-validation "resolves" that 38 mΩ. It does not, and the
    arithmetic says so plainly:

    * The **meter alone** has a 0.02 Ω budget at 100 Ω on the 200 Ω range, so
      it can see a 38 mΩ deviation. That is what makes DMM-vs-SP meaningful.
    * The **agreement budget between the two instruments** is 0.07 Ω, because
      the QR10x's own ±0.05% spec (0.05 Ω at 100 Ω) dominates the meter's
      contribution. So DMM-vs-PV cannot resolve 38 mΩ — it is a check for
      gross errors (units, scaling, sign), not a precision comparison.

    Pinned because conflating the two would justify a tolerance the hardware
    is allowed to violate, and the resulting failures would look like driver
    bugs.
    """
    offset = 0.038
    assert dmm_accuracy_ohm(100.0, 200.0) < offset, (
        "the meter must resolve the offset, or DMM-vs-SP proves nothing"
    )
    budget = agreement_budget_ohm(100.0, 100.0, 200.0, four_wire=True, nulled=False)
    assert budget > offset, (
        "if this ever passes, the QR10x spec has tightened and DMM-vs-PV "
        "became a precision comparison — revisit the docs' accuracy claim"
    )
    assert qr10x_accuracy_ohm(100.0) > dmm_accuracy_ohm(100.0, 200.0), (
        "the QR10x is expected to be the dominant term; if the meter ever "
        "dominates, tighten SETPOINTS_OHM rather than the budget"
    )


def test_the_2_wire_lead_term_dominates_even_the_agreement_budget():
    """The quantitative reason 4-wire is the primary path.

    Unnulled 2-wire adds 0.2 Ω of lead and contact resistance — larger than
    the entire two-instrument agreement budget it is added to, and over 5x the
    38 mΩ offset. On that path a driver could be wrong by the whole effect and
    still pass.
    """
    four = agreement_budget_ohm(100.0, 100.0, 200.0, four_wire=True, nulled=False)
    two = agreement_budget_ohm(100.0, 100.0, 200.0, four_wire=False, nulled=False)
    assert two - four == pytest.approx(TWO_WIRE_LEAD_OHM, abs=1e-9)
    assert four < TWO_WIRE_LEAD_OHM, (
        "the lead term is meant to dominate the whole budget — that is the "
        "argument for 4-wire"
    )
    assert two > 3 * four


def test_a_null_buys_back_the_lead_term():
    assert agreement_budget_ohm(
        100.0, 100.0, 200.0, four_wire=False, nulled=True
    ) == pytest.approx(
        agreement_budget_ohm(100.0, 100.0, 200.0, four_wire=True, nulled=False),
        abs=1e-9,
    )


def test_the_budget_is_a_linear_sum_not_a_quadrature_sum():
    """Pins the choice so a later "tidy-up" to RSS is a failing change.

    RSS of 0.02 and 0.05 is 0.054; the linear sum is 0.07. Two instruments
    each within spec can sit 0.07 apart, so RSS would fail on conforming
    hardware.
    """
    total = agreement_budget_ohm(100.0, 100.0, 200.0, four_wire=True, nulled=False)
    dmm = dmm_accuracy_ohm(100.0, 200.0)
    qr = qr10x_accuracy_ohm(100.0)
    assert total == pytest.approx(dmm + qr, abs=1e-9)
    assert total > (dmm**2 + qr**2) ** 0.5


def test_the_qr10x_floor_applies_at_low_setpoints():
    """A pure percentage would claim 0.5 mΩ accuracy at 1 Ω."""
    assert qr10x_accuracy_ohm(1.0) == QR10X_FLOOR_OHM
    assert qr10x_accuracy_ohm(1000.0) == pytest.approx(0.5)


def test_every_setpoint_is_inside_the_200_ohm_range():
    """A setpoint over 200 Ω would force a range change mid-sweep and break
    the single-range premise above."""
    assert all(0 < sp <= 200.0 for sp in SETPOINTS_OHM)


# ---------------------------------------------------------------------------
# Hardware-required — both instruments, one resistance
# ---------------------------------------------------------------------------


FOUR_WIRE = WIRING == "4"


@pytest.fixture()
def pair():
    """Both instruments, or a skip. Restores what it changed.

    The QR10x is left at its original setpoint and the meter reset, because
    these tests sweep resistance and change NPLC — leaving either altered
    makes the *next* test's result depend on which tests ran before it.
    """
    if not HW_RESOURCE or not HW_QR_PORT:
        pytest.skip(
            "cross-validation needs both BENCHCTRL_SDM4065A and "
            "BENCHCTRL_QR10X_PORT"
        )
    if WIRING not in ("2", "4"):
        pytest.skip(f"BENCHCTRL_SDM4065A_WIRING must be 2 or 4, got {WIRING!r}")

    resource = None if HW_RESOURCE.lower() == "auto" else HW_RESOURCE
    try:
        dmm = SiglentSDM4065A.open(resource)
    except Exception as exc:
        pytest.skip(f"SDM4065A not reachable ({HW_RESOURCE}): {exc}")
    try:
        qr = QR10x.open(HW_QR_PORT)
    except Exception as exc:
        dmm.close()
        pytest.skip(f"QR10x not reachable on {HW_QR_PORT}: {exc}")

    original_sp = None
    try:
        original_sp = qr.get_setpoint()
        rlimit = qr.get_safety_limit()
        usable = usable_setpoints(rlimit)
        if not usable:
            pytest.skip(
                f"RLIMIT is {rlimit:g} Ω, which clamps every setpoint in "
                f"SETPOINTS_OHM {SETPOINTS_OHM} — lower it deliberately (mind "
                f"the power rating) to run the sweep"
            )
        if len(usable) < len(SETPOINTS_OHM):
            dropped = [sp for sp in SETPOINTS_OHM if sp not in usable]
            print(
                f"\nRLIMIT {rlimit:g} Ω excludes setpoint(s) {dropped} — "
                f"sweeping {list(usable)}"
            )
        dmm.reset()
        yield dmm, qr
    finally:
        try:
            if original_sp is not None:
                qr.set_resistance(original_sp)
        finally:
            try:
                qr.close()
            finally:
                dmm.reset()
                dmm.close()


def _configure(dmm: SiglentSDM4065A, range_ohm: float) -> None:
    """Range, then NPLC — in that order, because CONFigure resets NPLC.

    Getting this backwards is the single easiest mistake to make with this
    instrument, and it would silently drop the integration time back to 10,
    invalidating the datasheet budget these tests are built on.
    """
    dmm.configure_resistance(range_ohm, four_wire=FOUR_WIRE)
    dmm.set_nplc(CROSS_VAL_NPLC)


def _read(dmm: SiglentSDM4065A) -> float:
    values = dmm.read()
    assert values, "READ? returned no samples"
    return values[0]


@pytest.mark.hardware
def test_both_instruments_are_present_and_identify_themselves(pair):
    """Fail fast and legibly before any number is compared."""
    dmm, qr = pair
    dmm_info = dmm.info()
    qr_info = qr.info()
    assert dmm_info.model.upper().startswith("SDM40")
    assert qr_info.device_type
    assert qr_info.serial


@pytest.mark.hardware
def test_the_dmm_agrees_with_the_qr10x_own_measurement(pair):
    """The core cross-validation: DMM vs PV at each setpoint.

    Both instruments measure the same physical resistance, so disagreement
    beyond the summed specs means one of them — or one of the two drivers —
    is wrong. This is the test that catches a units or scaling error, which no
    single-instrument test can see.
    """
    dmm, qr = pair
    _configure(dmm, 200.0)
    if not FOUR_WIRE:
        pytest.skip("2-wire needs a null first — see the nulled test below")

    failures = []
    for sp in usable_setpoints(qr.get_safety_limit()):
        qr.set_resistance(sp)
        pv = qr.actual_resistance()
        reading = _read(dmm)
        budget = agreement_budget_ohm(
            sp, reading, 200.0, four_wire=True, nulled=False
        )
        delta = abs(reading - pv)
        if delta > budget:
            failures.append(
                f"SP={sp:g} Ω: DMM read {reading:.5f}, QR10x PV {pv:.5f}, "
                f"|Δ|={delta:.5f} Ω > budget {budget:.5f} Ω"
            )
    assert not failures, "instruments disagree beyond spec:\n" + "\n".join(failures)


@pytest.mark.hardware
def test_the_dmm_agrees_with_the_qr10x_setpoint(pair):
    """DMM vs SP — a looser check that also covers the QR10x's setting error.

    Separate from the PV comparison on purpose. If this fails while the PV
    comparison passes, the QR10x is not reaching its setpoint — an instrument
    finding, not a driver bug — and keeping the two apart is what makes that
    distinction visible.
    """
    dmm, qr = pair
    _configure(dmm, 200.0)
    if not FOUR_WIRE:
        pytest.skip("2-wire needs a null first — see the nulled test below")

    failures = []
    for sp in usable_setpoints(qr.get_safety_limit()):
        qr.set_resistance(sp)
        reading = _read(dmm)
        # Twice the QR10x term: setting error and measurement error are
        # separate contributions and both are in play here.
        budget = dmm_accuracy_ohm(reading, 200.0) + 2 * qr10x_accuracy_ohm(sp)
        delta = abs(reading - sp)
        if delta > budget:
            failures.append(
                f"SP={sp:g} Ω: DMM read {reading:.5f}, |Δ|={delta:.5f} Ω > "
                f"budget {budget:.5f} Ω"
            )
    assert not failures, "DMM disagrees with setpoint beyond spec:\n" + "\n".join(
        failures
    )


@pytest.mark.hardware
def test_the_null_sequence_recovers_2_wire_accuracy(pair):
    """The 2-wire recipe, validated against an independent instrument.

    ``null_now`` on a shorted input, then ``read_nulled`` — and the claim is
    that this recovers 4-wire-grade agreement. Only a second instrument can
    confirm that, because a nulled meter agrees with itself by construction.

    Needs the QR10x set near zero to stand in for a short, which is exactly
    what a programmable resistance is for.
    """
    dmm, qr = pair
    _configure(dmm, 200.0)

    # Lowest the QR10x will go — its RLIMIT floor means this is not a true
    # short, so the residual is measured rather than assumed to be zero.
    qr.set_resistance(0.0)
    short_pv = qr.actual_resistance()

    offset = dmm.null_now(samples=10)
    assert dmm.get_null() is True
    assert dmm.get_null_auto() is False, (
        "auto-null is still armed — the instrument will overwrite the offset "
        "and the null below is a no-op"
    )

    # The offset should be the QR10x's residual plus lead resistance. On
    # 4-wire the lead term drops out, so the offset is the residual alone.
    if FOUR_WIRE:
        assert offset == pytest.approx(
            short_pv, abs=dmm_accuracy_ohm(short_pv, 200.0) + QR10X_FLOOR_OHM
        ), f"4-wire null offset {offset:.5f} Ω does not match QR10x {short_pv:.5f} Ω"
    else:
        assert offset > short_pv - QR10X_FLOOR_OHM, (
            f"2-wire null offset {offset:.5f} Ω is below the QR10x's own "
            f"residual {short_pv:.5f} Ω — the null captured the wrong thing"
        )

    failures = []
    for sp in usable_setpoints(qr.get_safety_limit()):
        qr.set_resistance(sp)
        pv = qr.actual_resistance()
        nulled = dmm.read_nulled()
        # The null removed the lead term, so claim the 4-wire budget even on
        # 2-wire — that is precisely the claim under test. Plus the residual
        # the null subtracted along with the leads.
        budget = (
            agreement_budget_ohm(sp, nulled, 200.0, four_wire=True, nulled=True)
            + short_pv
        )
        delta = abs(nulled - pv)
        if delta > budget:
            failures.append(
                f"SP={sp:g} Ω: nulled read {nulled:.5f}, QR10x PV {pv:.5f}, "
                f"|Δ|={delta:.5f} Ω > budget {budget:.5f} Ω"
            )
    assert not failures, (
        "nulled readings disagree beyond spec:\n" + "\n".join(failures)
    )


@pytest.mark.hardware
@pytest.mark.skipif(
    os.environ.get("BENCHCTRL_SDM4065A_WIRING", "4").strip() != "4",
    reason="needs sense leads connected (BENCHCTRL_SDM4065A_WIRING=4)",
)
def test_4_wire_beats_2_wire_by_about_the_lead_resistance(pair):
    """Measure the lead error instead of quoting the datasheet at it.

    ``KNOWN_LIMITATIONS`` H-5 currently cites the datasheet's 0.2 Ω because
    we had no measurement of our own. This produces one: the same resistance,
    read both ways, on the same leads.

    Asserted loosely — lead resistance depends on the leads, and the point is
    to record a number and confirm its sign, not to pin someone else's cable.
    """
    dmm, qr = pair
    qr.set_resistance(100.0)
    pv = qr.actual_resistance()

    dmm.configure_resistance(200.0, four_wire=True)
    dmm.set_nplc(CROSS_VAL_NPLC)
    four = _read(dmm)

    dmm.configure_resistance(200.0, four_wire=False)
    dmm.set_nplc(CROSS_VAL_NPLC)
    two = _read(dmm)

    lead_ohm = two - four
    print(
        f"\nmeasured lead+contact resistance: {lead_ohm * 1000:.1f} mΩ "
        f"(2-wire {two:.5f} Ω, 4-wire {four:.5f} Ω, QR10x PV {pv:.5f} Ω)"
    )

    assert four == pytest.approx(
        pv, abs=agreement_budget_ohm(100.0, four, 200.0, four_wire=True, nulled=False)
    )
    # Lead resistance is series resistance: 2-wire must read *higher*. A
    # negative result means the two functions are swapped in the driver —
    # which is the kind of error this whole file exists to catch.
    assert lead_ohm > -0.005, (
        f"2-wire read {lead_ohm * 1000:.1f} mΩ *lower* than 4-wire, which is "
        f"physically impossible for series lead resistance — check that "
        f"measure_resistance and measure_resistance_4wire are not swapped"
    )
    assert lead_ohm < 2.0, (
        f"lead+contact resistance {lead_ohm:.3f} Ω is implausibly high — "
        f"suspect a poor connection rather than a driver fault"
    )


@pytest.mark.hardware
def test_readings_are_repeatable_across_a_setpoint_round_trip(pair):
    """Return to the same setpoint and get the same number.

    Catches state left behind by the sweep — a range that autoranged away, a
    null that got re-armed — which would otherwise show up as an unexplained
    accuracy failure in whichever test happened to run next.
    """
    dmm, qr = pair
    _configure(dmm, 200.0)

    qr.set_resistance(100.0)
    first = _read(dmm)

    qr.set_resistance(10.0)
    _read(dmm)

    qr.set_resistance(100.0)
    second = _read(dmm)

    assert second == pytest.approx(first, abs=2 * dmm_accuracy_ohm(first, 200.0)), (
        f"same setpoint read {first:.5f} then {second:.5f} Ω"
    )


@pytest.mark.hardware
def test_an_over_range_setpoint_overloads_the_pinned_range(pair):
    """Overload from the other direction: a real resistance, deliberately too big.

    ``test_hw_overload_raises_on_a_deliberately_narrow_range`` uses an open
    input, which is a degenerate case. This one drives a genuine out-of-range
    resistance through the same path, so the sentinel is proven on a real
    measurement rather than on an absent one.
    """
    dmm, qr = pair
    _configure(dmm, 200.0)
    qr.set_resistance(1000.0)
    try:
        value = _read(dmm)
    except SDM4065AOverloadError:
        return
    pytest.fail(
        f"1 kΩ on the 200 Ω range returned {value} instead of raising — if "
        f"this is 9.9e37 the overload sentinel is not being checked"
    )

"""The cinematic display, held to the same honesty rules as the plain one.

A sci-fi console is a hostile place to put safety-relevant data: the whole visual
language says "live telemetry", so a number that is fabricated, defaulted, or
merely old is believed harder here than on a plain panel. These tests exist to
make sure the styling did not buy plausibility at the cost of truth.

The rules under test:

- an instrument slot shows a real reading or the literal ``NO LINK``
- a device the agent stopped reporting goes dark, rather than keeping its glow
- staleness reaches the readouts themselves, not just a global banner
- the footer banner never says "SYSTEM READY" about a bench it cannot see
- the alert counter counts things to act on, and startup is not one
- the HTTP surface is read-only and cannot be walked out of its static dir

The renderer is JavaScript and not tested here; that is exactly why
:py:mod:`benchctrl.dashboards.fui.view` exists as pure data. Everything the
display is *allowed to claim* is decided in Python and asserted below.
"""

from __future__ import annotations

import dataclasses
import json
import pathlib
import shutil
import subprocess
import threading
import urllib.error
import urllib.request

import pytest

import benchctrl.dashboards.fui as fui_static
from benchctrl.config import EndpointConfig
from benchctrl.dashboards.feed import AgentFeed
from benchctrl.dashboards.fui.server import FuiServer
from benchctrl.dashboards.fui.view import (
    ABSENT,
    FAULT,
    IN_RUN,
    INSTRUMENTS,
    NO_LINK,
    NOT_SERVED,
    OPEN,
    RECENT_ACTION_S,
    STAGES,
    STANDBY,
    UNDETERMINED,
    UNSCANNED,
    _enrolled_run,
    _recent_action,
    _slot_state,
    build_view,
)
from benchctrl.dashboards.state import BenchStatus

# The name of the floor tier, so the assertions below say what they mean rather
# than repeating a string the source could rename out from under them.
TIERS_CHEAPEST = "MINIMAL"

WELCOME = {
    "agent": "benchctrl-agent",
    "observer": True,
    "heartbeat_s": 5.0,
    "deadman_s": 15.0,
}


def status_payload(devices=None, *, since_contact=0.1):
    return {
        "safety": {
            "armed": [k for k, v in (devices or {}).items() if v.get("armed")],
            "seconds_since_contact": since_contact,
            "deadman_s": 15.0,
            "devices": devices or {},
            "trips": [],
        }
    }


def dev(armed=False, recording=False):
    return {"armed": armed, "recording": recording, "emulating": False}


def view_of(status: BenchStatus) -> dict:
    """Build the view the way the server does, from the flat snapshot."""
    snap = status.to_dict()
    snap["reconnects"] = 0
    return build_view(snap, status)


@pytest.fixture()
def live():
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_status(status_payload({"otii_arc": dev()}), now=100.0)
    return s


def slot(view, key):
    return next(s for s in view["instruments"] if s["key"] == key)


# --------------------------------------------------------------------------
# NO LINK is the only alternative to a real reading
# --------------------------------------------------------------------------


def test_an_unreported_instrument_reads_no_link_not_a_plausible_value():
    """The core rule. A believable number beside an unplugged instrument is the
    hazard this whole package is styled to avoid."""
    view = view_of(BenchStatus())
    for s in view["instruments"]:
        assert s["status"] == NO_LINK, s
        assert not s["linked"]
        assert not s["armed"]


def test_every_declared_instrument_gets_a_slot_even_when_absent():
    """A missing instrument must be a visibly dark slot, not a missing row.

    An absent row is easy to not notice; a dark slot in a fixed rail is not.
    """
    view = view_of(BenchStatus())
    assert [s["key"] for s in view["instruments"]] == [i["key"] for i in INSTRUMENTS]


def test_a_reported_instrument_the_governor_tracks_reads_as_open(live):
    """The FUI must not invent a status vocabulary that disagrees with the model —
    and "IDLE" was never in either vocabulary.

    The governor creates a device's state lazily, on the first call that could arm
    something, so its bare existence means a handle was opened. That is what OPEN
    says. It used to read IDLE, the governor's *resting* label published as a
    status word from the top rung of the ladder, which put it above IN RUN and
    made enrollment unreachable for every device the governor tracked.
    """
    view = view_of(live)
    arc = slot(view, "otii_arc")
    assert arc["linked"]
    assert arc["status"] == OPEN
    assert arc["armed"] is False
    assert arc["status"] != NO_LINK


def test_an_armed_instrument_is_flagged_armed(live):
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    view = view_of(live)
    assert slot(view, "otii_arc")["armed"]
    assert view["armed"] == ["otii_arc"]


def test_an_instrument_that_stops_being_reported_goes_dark_again(live):
    """The dangerous direction: a slot keeping its last glow after the agent
    stopped mentioning it. build_view only ever narrows, so this cannot happen
    by holding state — but a future cache would break it silently."""
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    assert slot(view_of(live), "otii_arc")["armed"]

    live.apply_status(status_payload({}), now=102.0)
    arc = slot(view_of(live), "otii_arc")
    assert arc["status"] == NO_LINK
    assert not arc["linked"]
    assert not arc["armed"]


def test_an_inferred_arm_is_marked_unconfirmed(live):
    """Inferred from an event, never confirmed by a snapshot. The renderer draws
    these dashed; they must not read as confirmed."""
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    arc = slot(view_of(live), "otii_arc")
    assert arc["armed"]
    assert arc["inferred"]


def test_staleness_reaches_the_readouts_and_not_just_the_banner(live):
    """A global warning is not enough when the numbers are what gets believed.

    Someone reading a 6-digit readout from across the bench is looking at the
    digits, not at a banner two panels away.
    """
    live.apply_event({"kind": "events_dropped", "count": 3}, now=101.0)
    view = view_of(live)
    assert view["stale_reason"]
    assert slot(view, "otii_arc")["stale"], "the readout did not know it was stale"


def test_a_trustworthy_view_marks_nothing_stale(live):
    """The complement: striking through live readings would be its own lie."""
    view = view_of(live)
    assert view["trustworthy"]
    assert not any(s["stale"] for s in view["instruments"])


# --------------------------------------------------------------------------
# Which dark a dark slot is
#
# NO LINK used to be the answer to every question a slot could not answer. On
# the real board that meant all five slots read NO LINK while four instruments
# sat on the bus, powered and ready — because the safety governor creates a
# device's state lazily, so safety.devices is {} on a healthy idle agent.
# --------------------------------------------------------------------------


def test_a_served_instrument_on_the_bus_reads_standby_not_no_link(live):
    """The state that motivated all of this.

    An instrument the agent serves, found on the bus, with no arm state yet, is
    the resting state of a *healthy* bench — devices open lazily on first use.
    Reporting NO LINK for it is not a harmless understatement: it is the panel
    saying the bench is unpopulated when it is fully populated.
    """
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])
    live.apply_inventory(
        {"devices": [{"device_key": "rigol_dp2031", "confidence": "exact",
                      "path": "USB0::6833::42152::DP2A243500269::0::INSTR"}]}
    )
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["status"] == STANDBY
    assert psu["present"] is True
    assert psu["ready"] is True
    # Still not "linked": that word is reserved for a device the agent is
    # actually talking to, and the renderer's bright-cyan treatment with it.
    assert not psu["linked"]
    assert not psu["armed"]


def test_scanned_and_absent_is_not_the_same_slot_as_never_scanned(live):
    """The distinction that earns its keep.

    A measurement of absence and the absence of a measurement look identical on
    a screen and are not the same fact. Collapsing them makes the panel assert
    an empty bench for the first seconds after every boot, before any scan has
    completed — and that is the window an operator is most likely to be looking.
    """
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])

    unscanned = slot(view_of(live), "rigol_dp2031")
    assert unscanned["status"] == UNSCANNED
    assert unscanned["present"] is None, "presence was asserted before any scan"

    # Now a scan lands and genuinely does not find it.
    live.apply_inventory({"devices": []})
    scanned = slot(view_of(live), "rigol_dp2031")
    assert scanned["status"] == ABSENT
    assert scanned["present"] is False
    assert scanned["status"] != unscanned["status"]


def test_an_instrument_the_agent_does_not_serve_says_so(live):
    """A configuration fact, not a hardware one, and the operator's fix differs.

    The instrument has to be *on the bus* for this to be sayable: NOT SERVED is
    "the hardware is here and nothing will drive it". A device that is neither
    served nor attached gets no row at all, because on a bench that never had one
    there is nothing to report — see
    :py:func:`~benchctrl.dashboards.fui.view._rail_specs`.
    """
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_inventory(
        {"devices": [{"device_key": "siglent_sdm4065a", "confidence": "exact",
                      "path": "USB0::62700::4640::SDM46A0CA00021::0::INSTR"}]}
    )
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == NOT_SERVED
    assert not dmm["served"]


def test_hardware_present_that_nothing_will_drive_demands_attention(live):
    """Served-and-absent is a bench with a cable to plug in. Present-and-unserved
    is a bench with the hardware already there and a config gap — the operator
    can fix that now, so it is flagged, while a plain absence is not."""
    # rigol_dl3031a IS served, so it can be the served-but-absent complement;
    # siglent_sdm4065a is not served but is on the bus.
    live.apply_registry(
        [{"key": "otii_arc", "open": True}, {"key": "rigol_dl3031a", "open": False}]
    )
    live.apply_inventory(
        {"devices": [{"device_key": "siglent_sdm4065a", "confidence": "exact",
                      "path": "USB0::62700::4640::SDM46A0CA00021::0::INSTR"}]}
    )
    view = view_of(live)
    dmm = slot(view, "siglent_sdm4065a")
    assert dmm["status"] == NOT_SERVED
    assert dmm["present"] is True
    assert dmm.get("attention"), "present-but-undrivable should be actionable"

    # And the complement, so this asserts a distinction rather than a constant:
    # served but genuinely absent is not an alert, it is a cable to plug in.
    load = slot(view, "rigol_dl3031a")
    assert load["status"] == ABSENT
    assert not load.get("attention")

    # The third leg: unserved AND absent. Nothing on this bench has ever referred
    # to that device, so the rail gives it no row at all — which is the same
    # "quietest of the three" fact the old assertion made when the rail was a
    # fixed five slots. Kept as the complement so `attention` cannot be keyed on
    # "not served" alone and still pass.
    assert not any(sl["key"] == "rigol_dp2031" for sl in view["instruments"]), (
        "a device neither served nor attached is not this bench's business"
    )


def test_an_open_failure_outranks_every_other_dark_state(live):
    """The only dark state an operator can usually act on, so it wins.

    It also has to beat NOT_SERVED, and proving that needs a device carrying an
    open error while NOT being in the current table — otherwise the served check
    never runs on this input and moving FAULT below it changes nothing. That
    combination is reachable: an agent restarted with a shorter --devices list
    drops the key from its table while the recorded failure is still on the slot.
    """
    live.apply_registry(
        [{"key": "rigol_dp2031", "open": False,
          "open_error": "BenchConnectionError('no DP2031 found')"}]
    )
    live.apply_inventory({"devices": []})
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["status"] == FAULT
    assert psu["attention"] is True
    assert "DP2031" in psu["open_error"], "the agent's own message is the useful part"

    # Now the agent stops serving it, keeping the recorded error. FAULT must
    # still win: the failure is the actionable fact, and it is also the more
    # specific one — the agent demonstrably tried.
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.slots["rigol_dp2031"].open_error = "BenchConnectionError('no DP2031 found')"
    unserved = slot(view_of(live), "rigol_dp2031")
    assert not unserved["served"], "premise: this slot is no longer in the table"
    assert unserved["status"] == FAULT, (
        "an open failure was displaced by a configuration detail"
    )


def test_arm_state_outranks_everything_including_a_fault(live):
    """Precedence at the top end. A live output must never be displaced by a
    configuration or open-failure detail, however actionable that detail is."""
    live.apply_registry(
        [{"key": "otii_arc", "open": True, "open_error": "stale error"}]
    )
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    arc = slot(view_of(live), "otii_arc")
    assert arc["status"] == "ARMED"
    assert arc["armed"]
    assert arc["linked"]


def test_no_session_means_no_presence_claim_at_all(live):
    """Presence is dropped on disconnect while arm state is kept-but-marked.

    Opposite treatments on purpose: a stale ARMED over-warns, which is the safe
    direction, but a stale ATTACHED under-warns by asserting something about the
    physical bench that nobody has checked since the link died.
    """
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])
    live.apply_inventory(
        {"devices": [{"device_key": "rigol_dp2031", "confidence": "exact"}]}
    )
    assert slot(view_of(live), "rigol_dp2031")["present"] is True

    live.apply_disconnected("cable pulled")
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["status"] == NO_LINK
    assert psu["present"] is None, "claimed presence with no session to back it"


def test_an_agent_that_sends_no_device_table_is_not_reported_unserved():
    """Not-knowing vs knowing, one level up from presence.

    With no table at all, a key missing from the slots means nothing. Treating it
    as NOT SERVED would make an older agent — or the first frame of any session —
    render five confident claims about a bench nobody has asked about.
    """
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)  # no "devices" key
    assert not s.registry_known
    for sl in view_of(s)["instruments"]:
        assert sl["status"] == NO_LINK, sl


def test_the_device_table_arrives_with_the_welcome_frame():
    """It rides along in WELCOME, so which devices an agent serves is known from
    the first frame — no extra round trip, and available while every other
    readout is still dark."""
    s = BenchStatus()
    s.apply_connected(
        {**WELCOME, "devices": [{"key": "otii_arc", "open": False}]}, now=100.0
    )
    assert s.registry_known
    assert s.slots["otii_arc"].served
    assert not s.slots["otii_arc"].opened


def test_a_reconnect_does_not_carry_the_old_agents_device_table():
    """An agent restarted with a different --devices list must not have the
    previous one's configuration attributed to it."""
    s = BenchStatus()
    s.apply_connected(
        {**WELCOME, "devices": [{"key": "siglent_sdm4065a", "open": True}]}, now=100.0
    )
    assert s.slots["siglent_sdm4065a"].served
    s.apply_disconnected("agent restarted")
    assert not s.registry_known
    s.apply_connected({**WELCOME, "devices": [{"key": "otii_arc", "open": True}]}, now=101.0)
    assert not s.slots["siglent_sdm4065a"].served
    assert s.slots["otii_arc"].served


def test_unclaimed_hardware_is_reported_because_the_rail_cannot_show_it(live):
    """A fixed five-slot rail structurally cannot say "something is plugged in
    that benchctrl cannot drive". On the development board that is a real pair of
    instruments — an SDG1032X and a DS1000Z scope with no drivers yet.
    """
    live.apply_inventory(
        {
            "devices": [
                {"device_key": None, "usb_id": "f4ec:1103", "label": "",
                 "product": "SDG1032X", "path": "USB0::..."},
                {"device_key": None, "usb_id": "1ab1:04ce",
                 "label": "Rigol DS1xx4Z", "path": "USB0::..."},
            ]
        }
    )
    unclaimed = view_of(live)["unclaimed"]
    assert {u["usb_id"] for u in unclaimed} == {"f4ec:1103", "1ab1:04ce"}
    assert any("SDG1032X" in u["label"] for u in unclaimed)


def test_unidentified_devices_without_a_usb_id_are_not_listed_as_hardware(live):
    """The board's inventory is mostly noise: four /dev/ttyS* plus a VISA alias
    for each. Listing those would make a two-instrument bench look like a
    fourteen-instrument one, which is the same failure as inventing a reading.
    """
    live.apply_inventory(
        {
            "devices": [
                {"device_key": None, "path": "/dev/ttyS0", "usb_id": None},
                {"device_key": None, "path": "ASRL/dev/ttyS1::INSTR", "usb_id": None},
                {"device_key": None, "path": "auto", "usb_id": "1a86:7523",
                 "label": "CH340 USB-serial bridge"},
            ]
        }
    )
    unclaimed = view_of(live)["unclaimed"]
    assert [u["usb_id"] for u in unclaimed] == ["1a86:7523"]


def test_one_instrument_on_two_transports_is_listed_once(live):
    """Discovery reports a CH340 with no tty and its VISA alias separately. They
    are one piece of hardware, and counting it twice overstates the bench."""
    live.apply_inventory(
        {
            "devices": [
                {"device_key": None, "path": "auto", "usb_id": "1a86:7523",
                 "label": "CH340 USB-serial bridge"},
                {"device_key": None, "path": "ASRL/dev/ttyACM9::INSTR",
                 "usb_id": "1a86:7523", "label": "CH340 USB-serial bridge"},
            ]
        }
    )
    assert len(view_of(live)["unclaimed"]) == 1


def test_the_rail_summary_cannot_disagree_with_the_rail(live):
    """The header counts are derived from the slots that were just built, not
    recomputed from the snapshot — so there is no arithmetic that can drift out
    of step with the column underneath it."""
    live.apply_registry(
        [{"key": "otii_arc", "open": True}, {"key": "rigol_dp2031", "open": False}]
    )
    live.apply_inventory(
        {"devices": [{"device_key": "rigol_dp2031", "confidence": "exact"},
                     {"device_key": "otii_arc", "confidence": "exact"}]}
    )
    view = view_of(live)
    assert view["bench"]["linked"] == sum(1 for s in view["instruments"] if s["linked"])
    assert view["bench"]["present"] == sum(
        1 for s in view["instruments"] if s["present"] is True
    )
    # The rail adapts to the bench, so the denominator has to track the column
    # rather than the driver catalogue: asserting len(INSTRUMENTS) here would be
    # asserting the development bench, and would let the header claim "2/5" on a
    # two-instrument bench — exactly the disagreement this test exists to forbid.
    assert view["bench"]["total"] == len(view["instruments"]) == 2
    assert view["bench"]["inventory_taken"] is True


def test_a_scan_that_cannot_decide_does_not_report_an_absence(live):
    """The one device a bus scan structurally cannot find.

    ``eastwood_qr10x`` is the only declared key with no VID/PID signature — it
    sits behind a generic CH340 bridge whose USB ID says nothing about what is on
    the other end, and ``inventory()`` never runs the serial probe that could
    tell. So "the scan completed and it was not in it" is not evidence of
    absence, and NOT FOUND there would be a false negative dressed as a
    measurement.

    The PSU in the same empty scan *is* signatured, so it reads NOT FOUND. That
    complement is the point: without it this passes on a view that reports NO ID
    for everything absent, and the guard would be doing nothing.
    """
    live.apply_registry(
        [{"key": "eastwood_qr10x", "open": False}, {"key": "rigol_dp2031", "open": False}]
    )
    live.apply_inventory({"devices": []})
    view = view_of(live)

    qr = slot(view, "eastwood_qr10x")
    assert qr["status"] == UNDETERMINED
    # The underlying fact is unchanged — the scan really did not find it. What
    # differs is whether the panel is willing to call that an absence.
    assert qr["present"] is False
    assert not qr.get("attention"), "unknowable is not actionable"

    assert slot(view, "rigol_dp2031")["status"] == ABSENT


def test_giving_an_instrument_a_signature_upgrades_its_slot(live, monkeypatch):
    """The undecidable set is read from discovery's own table, not listed here.

    A hardcoded exception list would keep saying "cannot tell" after the thing
    that could tell arrived. Adding a signature for the QR10x must therefore
    turn its slot into a real present/absent answer with no edit to the view —
    which is also why the table is re-read per frame rather than cached.
    """
    import benchctrl.discovery as discovery

    live.apply_registry([{"key": "eastwood_qr10x", "open": False}])
    live.apply_inventory({"devices": []})
    assert slot(view_of(live), "eastwood_qr10x")["status"] == UNDETERMINED

    added = dataclasses.replace(discovery.SIGNATURES[0], device_key="eastwood_qr10x")
    monkeypatch.setattr(discovery, "SIGNATURES", (*discovery.SIGNATURES, added))
    assert slot(view_of(live), "eastwood_qr10x")["status"] == ABSENT


def test_the_bus_denominator_excludes_what_no_scan_can_find(live):
    """"4/5 ON BUS" on a fully populated bench would read as a missing
    instrument forever. The header counts against what a scan can speak to."""
    view = view_of(live)
    assert view["bench"]["total"] == len(INSTRUMENTS)
    assert view["bench"]["scannable"] == len(INSTRUMENTS) - 1, (
        "the QR10x has no signature, so it cannot be in the bus denominator"
    )


def test_a_heuristic_identification_is_carried_through_as_a_guess(live):
    """The QR10x behind a CH340 is a guess, and the rail must be able to show it
    as one. Discovery already grades confidence; dropping the grade here would
    launder a heuristic match into a fact."""
    live.apply_registry([{"key": "eastwood_qr10x", "open": False}])
    live.apply_inventory(
        {"devices": [{"device_key": "eastwood_qr10x", "confidence": "heuristic",
                      "path": "auto"}]}
    )
    qr = slot(view_of(live), "eastwood_qr10x")
    assert qr["present"] is True
    assert qr["confidence"] == "heuristic"


def test_the_rail_shows_only_the_instruments_this_bench_has():
    """The rail used to be the development bench's five instruments, hardcoded.
    A user with one DMM would get four permanently dark slots for hardware they
    do not own, which is furniture pretending to be a bench."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_registry([{"key": "siglent_sdm4065a", "open": False}])
    keys = [sl["key"] for sl in build_view(s.to_dict(), s)["instruments"]]
    assert keys == ["siglent_sdm4065a"]


def test_a_device_this_build_cannot_name_still_gets_a_slot():
    """The agent serving something the FUI has no spec for is a fact about the
    bench. Dropping the row would make the rail quietly incomplete — worse than
    an ugly label, because nothing on screen would hint anything was missing."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_registry([{"key": "keysight_34470a", "open": False}])
    sl = build_view(s.to_dict(), s)["instruments"]
    assert [x["key"] for x in sl] == ["keysight_34470a"]
    assert sl[0]["label"] == "34470A", "an unnamed slot is worse than an ugly one"
    assert sl[0]["kind"] == "generic"


def test_hardware_on_the_bus_that_nothing_serves_keeps_its_row():
    """The NOT SERVED case is the whole point of the rail adapting to the *union*
    of served and found, rather than to the served list alone: a config gap on a
    bench that has the hardware is precisely what an operator must see."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_registry([{"key": "siglent_sdm4065a", "open": False}])
    s.apply_inventory(
        {"devices": [{"device_key": "rigol_dp2031", "confidence": "exact",
                      "path": "USB0::6833::42152::DP2A243500269::0::INSTR"}]}
    )
    view = build_view(s.to_dict(), s)
    assert slot(view, "rigol_dp2031")["status"] == NOT_SERVED


def test_the_rail_is_never_blank_before_a_device_table_arrives():
    """An empty column reads as "this bench has no instruments", which is a claim
    nobody has checked. Until the agent says what it serves, show the full table
    dark rather than nothing at all."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    assert len(build_view(s.to_dict(), s)["instruments"]) == len(INSTRUMENTS)


def test_a_bench_that_loses_a_device_loses_its_row_not_its_order():
    """Membership follows configuration, never cable state: an instrument the
    agent still serves keeps its row and goes dark, so the operator sees the
    absence. Only a device the agent stops serving leaves the rail."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_registry(
        [{"key": "otii_arc", "open": False}, {"key": "siglent_sdm4065a", "open": False}]
    )
    s.apply_inventory({"devices": [{"device_key": "otii_arc", "confidence": "exact"}]})
    before = [sl["key"] for sl in build_view(s.to_dict(), s)["instruments"]]
    assert before == ["otii_arc", "siglent_sdm4065a"]
    # Unplugged, but still served: the row stays and reports the absence.
    assert slot(build_view(s.to_dict(), s), "siglent_sdm4065a")["status"] == ABSENT
    # Reconfigured away: now it is not this bench's business.
    s.apply_registry([{"key": "otii_arc", "open": False}])
    assert [sl["key"] for sl in build_view(s.to_dict(), s)["instruments"]] == [
        "otii_arc"
    ]


def test_the_rails_short_label_survives_the_hardware_label(live):
    """The slot spec's ``label`` is the three-metre-readable name ("PSU"); the
    scan's is a sentence ("Rigol DP2000-series power supply"). build_view merges
    the slot state *over* the spec, so carrying the hardware label under the key
    ``label`` would silently replace the rail's name with prose that does not
    fit the column. They are two different fields and must stay that way."""
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])
    live.apply_inventory(
        {
            "devices": [
                {
                    "device_key": "rigol_dp2031",
                    "label": "Rigol DP2000-series power supply",
                    "path": "USB0::6833::42152::DP2A243500269::0::INSTR",
                    "usb_id": "1ab1:a4a8",
                    "confidence": "exact",
                }
            ]
        }
    )
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["label"] == "PSU", "the rail's own label was overwritten"
    assert psu["hw_label"] == "Rigol DP2000-series power supply"
    assert psu["serial_number"] == "DP2A243500269"
    assert psu["usb_id"] == "1ab1:a4a8"


def test_identity_is_not_shown_for_an_instrument_nobody_has_scanned(live):
    """Identity comes only from a live scan. Before one lands — and after the
    session that vouched for it dies — the rail must have nothing to print
    rather than a serial number for hardware nobody has checked since."""
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["hw_label"] is None
    assert psu["serial_number"] is None


def test_a_view_built_from_a_snapshot_with_no_slots_still_renders():
    """Forward/backward compatibility: build_view is called with whatever an
    AgentFeed produced, and a snapshot predating these fields must degrade to
    "unknown" rather than raising and blanking the entire panel."""
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    snap = s.to_dict()
    del snap["slots"]
    del snap["inventory_taken"]
    del snap["registry_known"]
    snap["reconnects"] = 0
    view = build_view(snap, s)
    assert [sl["status"] for sl in view["instruments"]] == [NO_LINK] * len(INSTRUMENTS)


# --------------------------------------------------------------------------
# What is happening at the device right now
#
# A slot used to report only configuration and arm state, so a bench being driven
# looked exactly like an untouched one: a sequence of measurements with no output
# armed reads STANDBY on every row. The agent already reports which worker thread
# is inside which call (`agent.status`'s `workers` table), so the rail can say it.
#
# The wart these tests pin down: the agent's job label is `f"{key}.{method}"`, so
# `busy_with` arrives already carrying the device key — on a row headed DMM it
# reads "siglent_sdm4065a.measure_resistance_4wire".
# --------------------------------------------------------------------------


def workers_payload(devices=None, *, workers=None, since_contact=0.1):
    """A status snapshot carrying a ``workers`` table alongside the safety one."""
    payload = status_payload(devices, since_contact=since_contact)
    payload["workers"] = workers or {}
    return payload


def busy(method, *, depth=0):
    """One entry of ``WorkerPool.stats()``, as the agent renders it."""
    return {"busy_with": method, "depth": depth}


def test_a_busy_slot_names_the_call_without_repeating_the_device_key(live):
    """The rail's job is to say what is happening AT this device, and the row is
    already headed by the device. The agent labels its jobs "{key}.{method}", so
    printing ``busy_with`` verbatim spends a slot's narrowest line restating the
    heading above it — "siglent_sdm4065a.measure_resistance_4wire" under DMM."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={
                "siglent_sdm4065a": busy("siglent_sdm4065a.measure_resistance_4wire")
            }
        ),
        now=101.0,
    )
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["busy"] == "measure_resistance_4wire"
    assert dmm["key"] not in dmm["busy"], "the slot restated its own heading"


def test_a_method_name_containing_a_dot_is_not_truncated_to_its_tail(live):
    """The stripping must remove a known prefix, not "everything before the last
    dot". A driver method reached through a sub-object is labelled
    "rigol_dp2031.channel.set_current", and taking the tail would show
    ``set_current`` — a call that is running on a channel the operator cannot see,
    named as if it were the whole story. Naming the wrong call is worse than
    naming a long one."""
    live.apply_registry([{"key": "rigol_dp2031", "open": True}])
    live.apply_status(
        workers_payload(
            workers={"rigol_dp2031": busy("rigol_dp2031.channel.set_current")}
        ),
        now=101.0,
    )
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["busy"] == "channel.set_current"


def test_an_armed_device_that_is_also_busy_still_reports_armed(live):
    """The safety one, and the reason ``busy`` is its own field.

    Arming means an output can be live; being mid-call is the bench working
    normally. If a routine measurement could take the status word, it would hide
    a live output — exactly the inversion
    :py:func:`~benchctrl.dashboards.fui.view._slot_state`'s precedence docstring
    exists to prevent, and the same ordering ``BenchStatus.headline`` applies when
    it keeps ARMED above ACTIVE. Both facts are true at once and both are shown.
    """
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(
        workers_payload(
            {"otii_arc": dev(armed=True)},
            workers={"otii_arc": busy("otii_arc.set_output")},
        ),
        now=101.0,
    )
    arc = slot(view_of(live), "otii_arc")
    assert arc["status"] == "ARMED", "a routine call displaced a live output"
    assert arc["armed"]
    # And the busy fact is not lost to make room for it: the operator needs both.
    assert arc["busy"] == "set_output"


def test_an_idle_device_claims_no_activity_at_all(live):
    """No busy value, not "idle" and not an empty string that reads as one.

    The complement of the test above: it proves ``busy`` tracks the workers table
    rather than being present whenever the slot is linked.

    The status word here is ``OPEN``, not ``STANDBY``: the registry reports this
    device open, and an open session is a stronger statement than "on the bus, not
    opened yet". That is the point being made — the status reflects the *session*
    while ``busy`` reflects the *worker*, so an open-but-idle device asserts the
    first and withholds the second.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_inventory(
        {"devices": [{"device_key": "siglent_sdm4065a", "confidence": "exact"}]}
    )
    live.apply_status(workers_payload(workers={}), now=101.0)
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == OPEN
    assert dmm["busy"] is None
    assert dmm["queued"] == 0
    # And no past-tense marker either: no action event has ever arrived for it.
    assert dmm["recent"] is None


def test_work_queued_behind_the_current_call_is_counted(live):
    """A depth is the difference between "this will free up" and "there are nine
    more behind it", and it is the only warning that starting a second run will
    queue rather than fail."""
    live.apply_registry([{"key": "rigol_dl3031a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={"rigol_dl3031a": busy("rigol_dl3031a.set_current", depth=3)}
        ),
        now=101.0,
    )
    load = slot(view_of(live), "rigol_dl3031a")
    assert load["busy"] == "set_current"
    assert load["queued"] == 3


def test_a_busy_key_the_rail_does_not_show_creates_no_phantom_slot(live):
    """The workers table is keyed independently of the rail, and the two can
    legitimately disagree — a device dropped from a restarted agent's --devices
    list, or a key this build has no driver for. An unknown key must be ignored,
    not turned into a row: a slot invented from a method name would have no
    presence, no identity and no status behind it, which is the fabricated readout
    this module exists to prevent."""
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(
        workers_payload(
            {"otii_arc": dev()},
            workers={"nonexistent_widget": busy("nonexistent_widget.calibrate")},
        ),
        now=101.0,
    )
    view = view_of(live)
    keys = [sl["key"] for sl in view["instruments"]]
    assert "nonexistent_widget" not in keys
    assert keys == ["otii_arc"]
    assert slot(view, "otii_arc")["busy"] is None


def test_a_stale_view_shows_no_call_in_flight(live):
    """An activity marker has no honest stale form.

    Every other readout can be struck through and still mean something — a
    crossed-out ARMED warns about a bench nobody can see. "executing
    measure_resistance_4wire" is an assertion about *this instant*: drawn at all,
    it says a call is running now, and it would say so indefinitely because no
    snapshot is arriving to correct it. It reads as "a run is driving this, leave
    it alone", which is the reassurance that stops someone checking.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            {"siglent_sdm4065a": dev()},
            workers={
                "siglent_sdm4065a": busy(
                    "siglent_sdm4065a.measure_resistance_4wire", depth=2
                )
            },
        ),
        now=101.0,
    )
    assert slot(view_of(live), "siglent_sdm4065a")["busy"] == "measure_resistance_4wire"

    live.apply_event({"kind": "events_dropped", "count": 3}, now=102.0)
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["stale"], "premise: the view must know it is behind"
    assert dmm["busy"] is None, "a call was left frozen in flight on a stale view"
    assert dmm["queued"] == 0


def test_a_dead_session_leaves_no_call_in_flight(live):
    """The disconnect half of the same rule, and deliberately not re-implemented
    in the view: ``BenchStatus.apply_disconnected`` already drops ``busy_devices``
    with presence and identity, for the same reason. This asserts the view relies
    on that rather than duplicating it — two places deciding one fact is how the
    quieter one ends up contradicting the louder one on screen."""
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(
        workers_payload(
            {"otii_arc": dev()}, workers={"otii_arc": busy("otii_arc.set_output")}
        ),
        now=101.0,
    )
    assert slot(view_of(live), "otii_arc")["busy"] == "set_output"

    live.apply_disconnected("cable pulled")
    assert live.busy_devices == {}, "the state model kept a call the link cannot back"
    arc = slot(view_of(live), "otii_arc")
    assert arc["busy"] is None
    assert arc["queued"] == 0


@pytest.mark.parametrize(
    "workers",
    [
        "siglent_sdm4065a.measure",  # not a mapping at all
        ["siglent_sdm4065a"],  # a list where a table was expected
        42,
        None,
        # Well-formed keys carrying values of the wrong type. `busy_devices` is a
        # flat {key: method} map and `queued_devices` a flat {key: depth} one, so
        # each of these is unusable for at least one of the two.
        {"siglent_sdm4065a": {"busy_with": "measure", "depth": 2}},  # raw table
        {"siglent_sdm4065a": ["measure"]},
        {"siglent_sdm4065a": None},
        {"siglent_sdm4065a": True},  # bool: an int by isinstance, not a count
        {"siglent_sdm4065a": -3},  # a negative depth is not a queue
        {12: "measure"},  # non-string key
    ],
)
def test_a_malformed_busy_payload_costs_the_marker_and_nothing_else(live, workers):
    """This field crosses a process boundary from an agent that may be a release
    ahead or behind. Raising here would take the arm state and the whole rail down
    with it — a missing word traded for a blank screen.

    Both maps get each payload, because either one can be the malformed half and
    the two are read by different code: a method where a depth belongs must not
    become a count, and a depth where a method belongs must not become a name.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload({"siglent_sdm4065a": dev()}, workers=None), now=101.0
    )
    snap = live.to_dict()
    # Injected past the state model on purpose: build_view is called with whatever
    # an AgentFeed produced, so it must validate what it is handed rather than
    # trusting an upstream that has already been fixed.
    snap["busy_devices"] = workers
    snap["queued_devices"] = workers
    snap["reconnects"] = 0
    view = build_view(snap, live)
    dmm = slot(view, "siglent_sdm4065a")
    assert dmm["busy"] is None
    assert dmm["queued"] == 0
    assert dmm["status"] == OPEN, "a bad workers table cost the rail its arm state"


def test_a_snapshot_with_no_busy_fields_at_all_still_renders(live):
    """An agent, or a stored snapshot, predating the workers table. Same rule as
    the missing-``slots`` case: degrade to "nothing said" rather than raising."""
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(workers_payload({"otii_arc": dev()}), now=101.0)
    snap = live.to_dict()
    del snap["busy_devices"]
    del snap["queued_devices"]
    snap["reconnects"] = 0
    arc = slot(build_view(snap, live), "otii_arc")
    assert arc["busy"] is None
    assert arc["queued"] == 0
    assert arc["status"] == OPEN


# --------------------------------------------------------------------------
# An open session is its own state, and outranks the not-knowing states
#
# On the real bench a 6-setpoint 4-wire resistance sweep ran for minutes with
# the agent driving the DMM and the QR10x, and both rails read STANDBY for the
# whole test — a word whose own definition is "not opened yet".
# --------------------------------------------------------------------------


def test_a_device_the_agent_has_open_does_not_read_standby(live):
    """The reported bug, at its smallest.

    STANDBY is defined as "on the bus, agent serves it, *not opened yet*", so
    printing it over a live session is not vagueness, it is the wrong fact. The
    operator reading it concludes nothing is talking to the instrument, which is
    the opposite of what is happening.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_inventory(
        {"devices": [{"device_key": "siglent_sdm4065a", "confidence": "exact"}]}
    )
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == OPEN
    assert dmm["opened"] is True
    # ``linked`` is what earns the renderer's bright-cyan treatment, and "the
    # agent is talking to this device" is exactly what it is meant to mark.
    assert dmm["linked"] is True
    # An open session is reported in the agent.devices table, not deduced from an
    # event, so it must not be drawn dashed like a guess.
    assert dmm["inferred"] is False


def test_a_served_but_unopened_device_still_reads_standby(live):
    """The other half of the same rung, pinned so the fix did not delete a state.

    STANDBY is the resting state of a *healthy* bench — the registry opens devices
    lazily on first use — and it was the answer to the previous round of this bug,
    where all five slots read NO LINK on a fully populated bench. A change that
    made every served device read OPEN would trade one wrong word for another.
    """
    live.apply_registry([{"key": "rigol_dp2031", "open": False}])
    live.apply_inventory(
        {"devices": [{"device_key": "rigol_dp2031", "confidence": "exact"}]}
    )
    psu = slot(view_of(live), "rigol_dp2031")
    assert psu["status"] == STANDBY
    assert psu["opened"] is False
    assert psu["linked"] is False


def test_an_open_session_outranks_a_scan_that_cannot_decide(live):
    """The QR10x's case, and the reason this rung sits where it does.

    The QR10x is behind a driverless CH340 with no VID/PID of its own, so it is
    not ``discoverable``: no non-probing scan will *ever* confirm it, and the
    periodic presence sweeps run ``probe=False`` deliberately so they cannot
    write a stray command at a mid-measurement instrument. Ranked below
    UNDETERMINED, an open session would therefore read "cannot tell" for the
    entire duration of a test that was actively driving it — permanently, not
    just briefly.

    An open session is the stronger evidence in any case: UNDETERMINED means no
    scan has answered the question, and nothing opens a handle to hardware that
    is not there.
    """
    live.apply_registry([{"key": "qr10x", "open": True}])
    # A scan that ran and did not find it, on a key discovery structurally cannot
    # identify — which is what UNDETERMINED exists to say.
    live.apply_inventory({"devices": []})
    qr = slot(view_of(live), "qr10x")
    assert qr["discoverable"] is False, "fixture no longer exercises the QR10x case"
    assert qr["status"] == OPEN


def test_an_open_session_outranks_a_bus_nobody_has_scanned(live):
    """Same ordering, the first-frame case.

    For the seconds between connecting and the first sweep landing, ``present`` is
    None for everything. A device the agent already has open is not unknown during
    that window, and this is the window an operator is most likely to be looking
    at — right after opening the panel.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["present"] is None, "fixture no longer exercises the unscanned case"
    assert dmm["status"] == OPEN


def test_a_scan_that_found_nothing_outranks_a_session_the_agent_thinks_it_has(live):
    """The one place these two facts genuinely contradict each other.

    ``opened`` says the agent obtained a handle at some point, not that it has
    just proved the handle works — pull the USB cable mid-session and it stays
    True against a dead file descriptor, with nothing arriving to correct it. A
    presence sweep that looked and did not find the device is both the newer
    measurement and the one the operator can act on, so it takes the word.
    Showing OPEN here would be the panel vouching for a cable that is on the
    bench floor.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_inventory({"devices": []})
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == ABSENT
    assert dmm["present"] is False
    # Not linked: that word is reserved for a device the agent is demonstrably
    # talking to, and a scan just said it is not there.
    assert dmm["linked"] is False


def test_an_arm_state_still_outranks_an_open_session(live):
    """The safety ordering, re-pinned against the new rung.

    Every device the governor tracks is open, so if OPEN could displace an arm
    state it would displace it *always* rather than in some corner — the new rung
    would silently blank the one word on the rail that is about a live output.
    """
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    arc = slot(view_of(live), "otii_arc")
    assert arc["status"] == "ARMED"
    assert arc["armed"] is True


def test_an_open_failure_still_outranks_an_open_session(live):
    """A registry can report both — a device it is serving whose last open raised.

    FAULT is the only dark state the operator can act on directly, and it carries
    ``attention``. Losing it to OPEN would drop both the actionable word and the
    alert count for a device that is not working.
    """
    live.apply_registry(
        [
            {
                "key": "siglent_sdm4065a",
                "open": True,
                "open_error": "VI_ERROR_RSRC_NFOUND",
            }
        ]
    )
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == FAULT
    assert dmm["attention"] is True


def test_a_dead_session_reports_no_open_state_either(live):
    """No session, so ``opened`` is of unknown age like every other slot fact.

    The registry table came from an agent that is now unreachable; a device it had
    open may have been closed, unplugged, or the agent may have exited. NO LINK is
    the only honest answer, and OPEN is a particularly bad one to leave glowing —
    it is the rail's brightest state.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_disconnected("agent went away")
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == NO_LINK
    assert dmm["linked"] is False


# --------------------------------------------------------------------------
# What a device was last seen doing
#
# ``busy_devices`` is sampled on the 5 s status poll while a device call takes
# ~200 ms, so the poll almost never lands inside one: the live sweep above spent
# minutes making calls and the rails showed no activity at any point. Action
# events arrive as each call completes, which is the only signal that can see it.
# --------------------------------------------------------------------------


def action_event(key, method, *, ok=True):
    """One ``action`` event, shaped as the agent publishes it."""
    return {"kind": "action", "device": key, "method": method, "ok": ok}


def test_a_recent_call_is_reported_with_its_age(live):
    """The fix for the rails looking untouched through a live test.

    Carrying the age rather than a boolean is what keeps it honest: "recently
    active" with no number gets read as "active", and this marker is always about
    the past.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_event(
        action_event("siglent_sdm4065a", "siglent_sdm4065a.measure_resistance_4wire"),
        now=101.0,
    )
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": 2.0}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] == {"age_s": 2.0, "action": "measure_resistance_4wire"}


def test_a_recent_call_never_displaces_the_call_in_flight(live):
    """Two different claims, and the present tense must win the one line it gets.

    ``busy`` says a call is executing at this instant; ``recent`` says one
    finished N seconds ago. A slot that is genuinely mid-call must show the call,
    not a completed one, or the marker would go backwards exactly when the bench
    got busiest.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={"siglent_sdm4065a": busy("siglent_sdm4065a.read_voltage")}
        ),
        now=101.0,
    )
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": 1.0}
    snap["last_action"] = {"siglent_sdm4065a": "measure_resistance_4wire"}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["busy"] == "read_voltage"
    # Both facts are carried; which one the renderer prints is its business, and
    # it prefers busy. Nothing is lost to make room.
    assert dmm["recent"]["action"] == "measure_resistance_4wire"


def test_an_action_older_than_the_window_is_dropped_entirely(live):
    """The marker has to expire, and expire to *nothing* rather than to a big
    number.

    A rail that keeps showing "measure_resistance_4wire · 400s ago" is claiming
    the last thing it knows about is still worth reading. The window is sized
    against the 5 s status poll — long enough to bridge the gap that hid the
    activity, short enough that nobody reads it as now.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": RECENT_ACTION_S + 0.5}
    snap["last_action"] = {"siglent_sdm4065a": "measure_resistance_4wire"}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] is None


def test_an_action_inside_the_window_survives_the_poll_gap(live):
    """The complement, and the property the window exists for.

    A marker that expired faster than the status poll would leave the same blind
    gap it was added to cover, so the boundary is asserted from both sides.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": RECENT_ACTION_S - 0.5}
    snap["last_action"] = {"siglent_sdm4065a": "measure_resistance_4wire"}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] is not None
    assert dmm["recent"]["age_s"] == round(RECENT_ACTION_S - 0.5, 1)


def test_staleness_is_refused_at_both_places_that_can_refuse_it():
    """The activity marker's staleness rule is guarded twice, and this pins the
    halves apart.

    ``_slot_state`` blanks ``recent`` on an untrustworthy view and
    ``_recent_action`` independently refuses to build one, which is deliberate:
    ``_slot_state`` is called directly by tests and by anything that does not go
    through ``build_view``, and ``_recent_action`` is the reusable half. Because
    either guard alone satisfies a whole-view test, deleting one is invisible from
    the outside — so each is exercised with an input only it sees.

    What is at stake is the field that ages: an age computed against a feed that
    stopped arriving understates itself, so "2 s ago" would sit frozen on the
    glass while the true answer grew to minutes.
    """
    ages = {"siglent_sdm4065a": 1.0}
    names = {"siglent_sdm4065a": "measure_resistance_4wire"}
    # The lower guard, reached with no _slot_state above it to cover for it.
    assert _recent_action("siglent_sdm4065a", ages, names, trustworthy=False) is None
    assert _recent_action("siglent_sdm4065a", ages, names, trustworthy=True) is not None
    # The upper guard, handed a marker that is already built and valid — so only
    # _slot_state's own blanking can drop it.
    built = _recent_action("siglent_sdm4065a", ages, names, trustworthy=True)
    state = _slot_state(
        None,
        {"served": True, "opened": True, "present": True},
        trustworthy=False,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        recent=built,
    )
    assert state["recent"] is None


def test_a_stale_view_reports_no_recent_activity(live):
    """An age measured against a feed that stopped arriving understates itself.

    "2 s ago" would sit frozen on the glass while the true answer grew to
    minutes — worse than ``busy``, which at least has no stale form to be
    mistaken for. Withheld for the same reason ``busy`` is.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(workers_payload(workers={}), now=101.0)
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": 1.0}
    snap["last_action"] = {"siglent_sdm4065a": "measure_resistance_4wire"}
    snap["stale_reason"] = "no frames for 40 s"
    snap["trustworthy"] = False
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] is None


@pytest.mark.parametrize(
    "ages",
    [
        "not-a-dict",
        None,
        {"siglent_sdm4065a": "2.0"},  # a string age is not a number
        {"siglent_sdm4065a": None},
        {"siglent_sdm4065a": True},  # bool: an int by isinstance, not an age
        {"siglent_sdm4065a": -1.0},  # an action in the future is not an age
    ],
)
def test_a_malformed_age_costs_the_marker_and_nothing_else(live, ages):
    """Same rule as the workers table: this field crossed a process boundary from
    an agent that may be a release ahead or behind, and a bad optional field must
    not take the rail's arm state down with it."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(workers_payload({"siglent_sdm4065a": dev()}), now=101.0)
    snap = live.to_dict()
    snap["action_age"] = ages
    snap["last_action"] = {"siglent_sdm4065a": "measure_resistance_4wire"}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] is None
    assert dmm["status"] == OPEN, "a bad age cost the rail its arm state"


def test_an_age_with_no_action_name_still_reports_the_age(live):
    """The age is the load-bearing half. An older agent that stamps activity
    without naming it should still light the rail, because "something happened 2 s
    ago" is the fact that was missing."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": 2.0}
    snap["last_action"] = {}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert dmm["recent"] == {"age_s": 2.0, "action": ""}


def test_a_snapshot_with_no_activity_fields_at_all_still_renders(live):
    """An agent, or a stored snapshot, predating these fields. Degrade to "nothing
    said" rather than raising."""
    live.apply_registry([{"key": "otii_arc", "open": True}])
    live.apply_status(workers_payload({"otii_arc": dev()}), now=101.0)
    snap = live.to_dict()
    del snap["action_age"]
    del snap["last_action"]
    snap["reconnects"] = 0
    arc = slot(build_view(snap, live), "otii_arc")
    assert arc["recent"] is None
    assert arc["status"] == OPEN


def test_a_long_action_name_is_clipped_before_it_reaches_the_glass(live):
    """A slot's activity line is its narrowest, and an unclipped method name
    pushes the rail wide enough to break the layout at three metres."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    snap = live.to_dict()
    snap["action_age"] = {"siglent_sdm4065a": 1.0}
    snap["last_action"] = {"siglent_sdm4065a": "m" * 400}
    snap["reconnects"] = 0
    dmm = slot(build_view(snap, live), "siglent_sdm4065a")
    assert len(dmm["recent"]["action"]) < 400


def test_activity_is_carried_on_a_dark_slot_without_lighting_it(live):
    """Every slot gets the field, including the ones nothing is reported for, so
    the renderer never has to ask whether it is missing or empty. None draws as
    nothing — not as "idle", which would be a claim."""
    view = view_of(BenchStatus())
    assert view["instruments"], "no slots to check"
    assert all("recent" in s for s in view["instruments"])
    assert all(s["recent"] is None for s in view["instruments"])


# --------------------------------------------------------------------------
# The footer banner
# --------------------------------------------------------------------------


def test_the_banner_never_claims_system_ready_without_a_link():
    view = view_of(BenchStatus())
    assert "READY" not in view["operation"]
    assert "LINKING" in view["operation"]


def test_the_banner_says_no_link_once_an_attempt_has_failed():
    s = BenchStatus()
    s.apply_disconnected("cannot reach the agent: refused")
    view = view_of(s)
    assert "NO LINK" in view["operation"]
    assert "READY" not in view["operation"]


def test_the_banner_leads_with_an_unconfirmed_output(live):
    """Worst-case first, the same discipline as the headline."""
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    live.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=102.0)
    view = view_of(live)
    assert "DO NOT TOUCH" in view["operation"]
    assert view["unsafe"]


def test_the_banner_announces_a_live_output(live):
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    assert "ARMED" in view_of(live)["operation"]
    assert "OTII_ARC" in view_of(live)["operation"]


def test_an_idle_linked_bench_is_allowed_to_say_ready(live):
    """The complement: a display that never says "ready" is as useless as one
    that always does."""
    assert "READY" in view_of(live)["operation"]


def test_the_two_banners_cannot_disagree_about_whether_the_bench_is_busy(live):
    """The reported bug, and the reason it is worth a test rather than a one-liner.

    The headline had an ACTIVE rung and the footer did not, so the footer's notion
    of activity was "a *run* is in progress" — and the bench is driven by direct
    device calls interactively and in every demo, no run object involved. On the
    real board the top read ACTIVE while the bottom read BENCH IDLE at the same
    instant.

    Two banners disagreeing is worse than either being wrong on its own: it tells
    the operator the panel cannot be trusted, and there is no way to tell from the
    glass which half to believe.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={
                "siglent_sdm4065a": busy("siglent_sdm4065a.measure_resistance_4wire")
            }
        ),
        now=101.0,
    )
    view = view_of(live)
    assert "ACTIVE" in view["headline"]
    assert "ACTIVE" in view["operation"]
    assert "IDLE" not in view["operation"]


def test_both_banners_see_a_sweep_the_status_poll_keeps_missing(live):
    """The deeper half of the report: "both usually said idle".

    Making the two banners agree was not enough, because they agreed on the wrong
    answer. Both read ``any_busy``, which came only from the ``workers`` table —
    sampled every 5 s while a device call takes ~200 ms, so the poll lands inside
    a call about 4% of the time. A bench being driven continuously therefore read
    IDLE almost always and flickered ACTIVE for single frames.

    Here the poll lands *between* calls, which is the overwhelmingly common case,
    and the panel must still say the bench is working — that is what an operator
    watching a running test needs the top of the screen to tell them.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_event(
        action_event("siglent_sdm4065a", "siglent_sdm4065a.measure_resistance_4wire"),
    )
    # An empty workers table: no call in flight as of this poll.
    live.apply_status(workers_payload(workers={}))
    view = view_of(live)
    assert view["headline"] == "ACTIVE"
    assert "ACTIVE" in view["operation"]
    assert "IDLE" not in view["operation"]


def test_the_footer_names_the_finished_call_in_the_past_tense(live):
    """"BENCH ACTIVE" with no detail is the failure this fallback exists for: the
    ``workers`` table it would normally quote is empty, so without it the whole
    sweep shows a bare word.

    Tensed and aged deliberately. Quoting a completed call as though it were
    running would have the footer assert an instant it cannot vouch for, which is
    the one thing this panel is not allowed to do.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_event(
        action_event("siglent_sdm4065a", "siglent_sdm4065a.measure_resistance_4wire"),
    )
    live.apply_status(workers_payload(workers={}))
    operation = view_of(live)["operation"]
    assert "MEASURE_RESISTANCE_4WIRE" in operation
    assert "AGO" in operation, "a finished call was quoted as though it were running"


def test_a_call_in_flight_is_named_ahead_of_a_finished_one(live):
    """The present tense wins the line when there is one. The fallback must not
    displace the sharper fact it exists to cover for the absence of."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_event(action_event("siglent_sdm4065a", "siglent_sdm4065a.read_voltage"))
    live.apply_status(
        workers_payload(
            workers={
                "siglent_sdm4065a": busy("siglent_sdm4065a.measure_resistance_4wire")
            }
        ),
    )
    operation = view_of(live)["operation"]
    assert "MEASURE_RESISTANCE_4WIRE" in operation
    assert "AGO" not in operation


def test_a_bench_that_has_gone_quiet_is_allowed_to_read_idle_again(live):
    """The activity window has to close, or the panel says ACTIVE forever after the
    first call of the session and the word stops meaning anything.

    This is the property that makes the window safe to widen: it is bounded, so
    the cost of covering the poll gap is a few seconds of lag on the way down, not
    a permanent claim.
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_event(action_event("siglent_sdm4065a", "siglent_sdm4065a.read_voltage"))
    live.apply_status(workers_payload(workers={}))
    assert view_of(live)["headline"] == "ACTIVE"

    # Age the stamps past the window rather than sleeping through it.
    live.last_action_at = {
        k: t - (BenchStatus.ACTIVITY_WINDOW_S + 1.0)
        for k, t in live.last_action_at.items()
    }
    view = view_of(live)
    assert view["headline"] == "IDLE"
    assert "IDLE" in view["operation"]


def test_one_still_working_instrument_keeps_the_bench_active(live):
    """A bench is busy if *anything* on it is, so the window is measured from the
    newest call and not the oldest.

    Needs two devices with genuinely different ages to say anything: with one
    device, or two stamped together, "newest" and "oldest" are the same number and
    a panel reading either looks identical. The realistic shape is a sweep that has
    moved on — the QR10x was set once several seconds ago and the DMM is still
    reading it every couple of hundred milliseconds. Measuring from the oldest
    would declare that bench idle while a measurement was in progress.
    """
    live.apply_registry(
        [{"key": "siglent_sdm4065a", "open": True}, {"key": "qr10x", "open": True}]
    )
    live.apply_event(action_event("qr10x", "qr10x.set_resistance"))
    live.apply_event(
        action_event("siglent_sdm4065a", "siglent_sdm4065a.measure_resistance_4wire"),
    )
    live.apply_status(workers_payload(workers={}))
    # The setpoint call has aged out of the window; the measurement has not.
    stale_stamp = (
        live.last_action_at["qr10x"] - BenchStatus.ACTIVITY_WINDOW_S - 1.0
    )
    live.last_action_at["qr10x"] = stale_stamp
    view = view_of(live)
    assert view["headline"] == "ACTIVE"
    # And the footer names the one that is actually recent, not the stale one.
    assert "MEASURE_RESISTANCE_4WIRE" in view["operation"]
    assert "SET_RESISTANCE" not in view["operation"]


def test_the_activity_window_is_wider_than_the_gap_it_covers():
    """A window narrower than the status poll leaves the same blind spot it was
    added to close — the panel would go IDLE between polls of a bench that never
    stopped working, which is the original bug with extra code."""
    from benchctrl.dashboards.feed import DEFAULT_POLL_S

    assert BenchStatus.ACTIVITY_WINDOW_S > DEFAULT_POLL_S


def test_the_footer_names_what_the_bench_is_doing(live):
    """"BENCH ACTIVE" alone leaves the operator watching a screen that says
    something is happening without saying what — the question they then have to
    answer by walking to the instrument."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={
                "siglent_sdm4065a": busy("siglent_sdm4065a.measure_resistance_4wire")
            }
        ),
        now=101.0,
    )
    operation = view_of(live)["operation"]
    assert "MEASURE_RESISTANCE_4WIRE" in operation


def test_a_run_in_progress_still_outranks_plain_activity(live):
    """A run is the more specific fact and names something with a beginning and an
    end, so it keeps the line. Both are true; the footer has one line."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={"siglent_sdm4065a": busy("siglent_sdm4065a.read_voltage")}
        ),
        now=101.0,
    )
    live.apply_event(
        {"kind": "run_started", "run": "sweep", "state": "running"}, now=101.0
    )
    operation = view_of(live)["operation"]
    assert "RUN IN PROGRESS" in operation


def test_an_armed_output_still_outranks_activity_in_the_footer(live):
    """The safety ordering, and the same inversion the slot rail refuses.

    A routine measurement must never displace a live output — and it would do so
    constantly, because an armed bench is exactly the one making calls. The
    footer's ladder has to match the headline's here or the two disagree again, in
    the direction that matters most.
    """
    live.apply_status(
        workers_payload(
            {"otii_arc": dev(armed=True)},
            workers={"otii_arc": busy("otii_arc.read_voltage")},
        ),
        now=101.0,
    )
    operation = view_of(live)["operation"]
    assert "ARMED" in operation
    assert "OUTPUT IS LIVE" in operation


def test_a_stale_view_reports_staleness_rather_than_activity(live):
    """``busy`` on a stale snapshot is a claim about this instant sourced from a
    frame that stopped arriving. "BENCH ACTIVE" would be asserting the bench is
    working right now on the strength of a reading nobody can vouch for, and the
    operator's actual problem is the dead link."""
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_status(
        workers_payload(
            workers={"siglent_sdm4065a": busy("siglent_sdm4065a.read_voltage")}
        ),
        now=101.0,
    )
    snap = live.to_dict()
    snap["stale_reason"] = "no frames for 40 s"
    snap["trustworthy"] = False
    snap["reconnects"] = 0
    operation = build_view(snap, live)["operation"]
    assert "STALE" in operation
    assert "ACTIVE" not in operation


# --------------------------------------------------------------------------
# Alerts
# --------------------------------------------------------------------------


def test_starting_up_raises_no_alerts():
    """A booting kiosk has raised nothing. A flashing red counter on every boot
    is a counter nobody reads."""
    view = view_of(BenchStatus())
    assert view["starting"]
    assert view["alerts"] == 0


def test_an_idle_bench_raises_no_alerts(live):
    assert view_of(live)["alerts"] == 0


def test_an_unsafe_armed_stale_bench_counts_all_three(live):
    live.apply_status(status_payload({"otii_arc": dev(armed=True)}), now=101.0)
    live.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=102.0)
    view = view_of(live)
    assert view["alerts"] == 3, view


def test_a_failed_connection_raises_an_alert():
    s = BenchStatus()
    s.apply_disconnected("refused")
    assert view_of(s)["alerts"] >= 1


# --------------------------------------------------------------------------
# The test-sequence flowchart
# --------------------------------------------------------------------------


def test_an_idle_bench_lights_no_stage(live):
    """An idle bench is genuinely not part-way through a sequence. Pulsing a
    node anyway is the cinematic lie the view module refuses."""
    view = view_of(live)
    assert [s["name"] for s in view["stages"]] == list(STAGES)
    assert not any(s["active"] for s in view["stages"])


def test_a_run_that_reported_a_stage_lights_exactly_that_one(live):
    """The bench says where it is, and the row shows it.

    The whole point of the change these stages came from. Previously the lit node
    was derived from run *status*, which has three interesting values and so could
    never distinguish five stages: two nodes were unreachable and the third lit
    ``ANALYSIS`` from the first setpoint onwards.
    """
    live.apply_event({"kind": "run_start", "run_id": "r1"}, now=101.0)
    live.apply_event(
        {"kind": "run_stage", "run_id": "r1", "stage": "EXECUTE"}, now=101.5
    )
    view = view_of(live)
    assert [s["name"] for s in view["stages"] if s["active"]] == ["EXECUTE"]
    # And the nodes it has already been through read as traversed, so the row is a
    # progress track rather than one lit box in a row of dark ones.
    assert [s["name"] for s in view["stages"] if s["done"]] == ["INIT", "PREPARE"]


def test_a_running_run_that_reported_no_stage_lights_nothing(live):
    """"A run is in flight" does not say which stage it is in.

    The honest rendering of "the bench has not told us where it is" is no node,
    which is why ``running`` maps to nothing. The old mapping's answer was
    ``ANALYSIS`` for the entire run — a word that was wrong for almost the whole
    time it was lit. Reached in practice by an older agent, or by a panel that
    connected mid-run and missed the transition.
    """
    live.apply_event({"kind": "run_start", "run_id": "r1"}, now=101.0)
    assert not any(s["active"] for s in view_of(live)["stages"])


def test_a_reported_stage_outranks_the_state_derived_fallback(live):
    """Where both could answer, the bench's report wins.

    A ``pending`` run derives INIT, so a run that has reported EXECUTE while its
    state still reads pending is the case that pins the precedence. A measurement
    of where the run is beats an inference from its status.
    """
    live.apply_event({"kind": "run_start", "run_id": "r1"}, now=101.0)
    live.apply_event(
        {"kind": "run_stage", "run_id": "r1", "stage": "EXECUTE"}, now=101.5
    )
    live.runs["r1"].state = "pending"
    assert [s["name"] for s in view_of(live)["stages"] if s["active"]] == ["EXECUTE"]


def test_a_stage_this_build_cannot_place_is_named_rather_than_dropped(live):
    """A newer agent naming a stage this row does not carry must not read as idle.

    Showing NO SEQUENCE beside a run that is plainly in progress would say "no test
    running", which is the class of lie this panel exists to avoid. The name is
    passed through so the display can say it cannot place it.
    """
    live.apply_event({"kind": "run_start", "run_id": "r1"}, now=101.0)
    live.apply_event(
        {"kind": "run_stage", "run_id": "r1", "stage": "CALIBRATE"}, now=101.5
    )
    view = view_of(live)
    assert not any(s["active"] for s in view["stages"])
    assert view["stage_unknown"] == "CALIBRATE"


def test_a_finished_run_lights_done(live):
    live.apply_event({"kind": "run_started", "run_id": "r1"}, now=101.0)
    live.apply_event(
        {"kind": "run_finished", "run_id": "r1", "state": "finished"}, now=102.0
    )
    active = [s["name"] for s in view_of(live)["stages"] if s["active"]]
    assert active == ["DONE"]


def test_an_unknown_run_state_lights_nothing_rather_than_guessing(live):
    """Lighting the wrong node is worse than lighting none: it asserts the bench
    is somewhere it is not."""
    live.apply_event({"kind": "run_started", "run_id": "r1"}, now=101.0)
    live.runs["r1"].state = "quantum-superposition"
    assert not any(s["active"] for s in view_of(live)["stages"])


def _lit(runs: dict) -> list:
    """Which stages light, for a view whose only content is its run states."""
    snap = BenchStatus().to_dict()
    snap["reconnects"] = 0
    snap["runs"] = runs
    return [s["name"] for s in build_view(snap)["stages"] if s["active"]]


def test_the_lit_stage_does_not_depend_on_dict_order():
    """Two runs, same states, different insertion order -> the same lit node.

    The flowchart sits beside a *sorted* run list, so a node chosen by whichever
    run happened to be last in the agent's iteration order could visibly disagree
    with the list next to it. Same set in, same answer out.
    """
    assert _lit({"run-a": "finished", "run-b": "not-a-real-state"}) == ["DONE"]
    assert _lit({"run-b": "not-a-real-state", "run-a": "finished"}) == ["DONE"]


def test_an_in_flight_run_outranks_a_finished_one():
    """What the bench is doing beats what it has done, regardless of order."""
    assert _lit({"early": "pending", "late": "finished"}) == ["INIT"]
    assert _lit({"late": "finished", "early": "pending"}) == ["INIT"]


def _staged(runs: dict, stages) -> dict:
    """The whole stage half of the view, for given ``runs`` and ``run_stages``.

    Takes both maps as they arrive on the snapshot, so a test can build the
    combinations a live model cannot easily be walked into — a stage for a run the
    snapshot does not list, a ``run_stages`` that is not a dict at all — which is
    exactly the shape an agent a release ahead or behind can send.
    """
    snap = BenchStatus().to_dict()
    snap["reconnects"] = 0
    snap["runs"] = runs
    snap["run_stages"] = stages
    view = build_view(snap)
    return {
        "active": [s["name"] for s in view["stages"] if s["active"]],
        "done": [s["name"] for s in view["stages"] if s["done"]],
        "unknown": view["stage_unknown"],
    }


def _dut_of(runs: dict, dut_map, names=None) -> dict:
    """The DUT half of the view, for given ``runs`` and ``run_dut``.

    Same reason :py:func:`_staged` takes its maps directly: the combinations that
    matter here — a DUT for a run the snapshot does not list, no map at all — are
    what an agent a release ahead or behind sends, and are awkward to walk a live
    model into.
    """
    snap = BenchStatus().to_dict()
    snap["reconnects"] = 0
    snap["runs"] = runs
    snap["run_dut"] = dut_map
    snap["run_names"] = names if names is not None else {}
    view = build_view(snap)
    return {
        "dut": view["dut"],
        "dut_known": view["dut_known"],
        "run_name": view["run_name"],
    }


def test_an_idle_bench_claims_no_dut():
    """Nothing on this panel may assert a DUT that no run declared.

    With no runs there is no headline run, so ``dut_known`` must be False and the
    label reads NO RUN. A view that hardcoded it True would have an idle bench
    showing UNSPECIFIED — a statement about a run that does not exist.
    """
    got = _dut_of({}, {})
    assert got["dut_known"] is False
    assert got["dut"] == ""


def test_a_run_the_snapshot_lists_without_a_dut_entry_is_not_known():
    """The mid-run-connect case, at the view layer.

    ``run_dut`` omits runs whose ``run_start`` was never seen, so a run present in
    ``runs`` and absent from ``run_dut`` is precisely "we were not told". It must
    not be reported as a declared-empty DUT.
    """
    got = _dut_of({"r1": "running"}, {})
    assert got["dut_known"] is False


def test_a_declared_empty_dut_is_known_and_empty():
    """The other side of the same distinction, which one flag has to carry.

    Present in the map with an empty value means the run said nothing is named —
    a fact about the spec, and the input that makes ``dut_known`` load-bearing
    rather than decorative.
    """
    got = _dut_of({"r1": "running"}, {"r1": ""})
    assert got["dut_known"] is True
    assert got["dut"] == ""


def test_the_dut_shown_belongs_to_the_run_the_sequence_row_is_about():
    """The two must name the same run, or the panel is self-contradictory.

    An in-flight run owns the row — the same rule ``_active_stage`` follows — so
    with a finished run sorting first the DUT must still be the live run's. A
    display pairing one run's DUT with another's stage would be worse than showing
    neither, because both halves look authoritative.
    """
    runs = {"a-finished": "complete", "b-live": "running"}
    got = _dut_of(runs, {"a-finished": "old-board", "b-live": "room-temp-sensor"})
    assert got["dut"] == "room-temp-sensor"


def test_the_run_name_comes_from_the_same_run_as_the_dut():
    got = _dut_of(
        {"a-finished": "complete", "b-live": "running"},
        {"b-live": "room-temp-sensor"},
        {"a-finished": "old-sweep", "b-live": "cr2032-assoc-24h"},
    )
    assert got["run_name"] == "cr2032-assoc-24h"


def test_a_run_dut_payload_that_is_not_a_map_costs_the_panel_nothing_else():
    """An older agent sends no such key at all; a confused one could send anything.

    The rest of the frame must still build — this panel's job is to keep reporting
    the bench through a partial or unexpected snapshot, not to go dark because one
    field was the wrong type.
    """
    for bad in (None, "room-temp-sensor", ["r1"], 3):
        got = _dut_of({"r1": "running"}, bad)
        assert got["dut_known"] is False, bad
        assert got["dut"] == "", bad


def test_the_active_node_is_not_also_marked_done():
    """A run *in* EXECUTE has not been through EXECUTE.

    The done-track is a strictly-earlier-than test, and the off-by-one is not
    cosmetic: a node drawn as both traversed and pulsing is the row saying the run
    has finished the stage it is still in, which is the one thing an operator reads
    this row to know. Pinned separately from the lit-node assertions because those
    hold either way.
    """
    staged = _staged({"r1": "running"}, {"r1": "EXECUTE"})
    assert staged["active"] == ["EXECUTE"]
    assert staged["done"] == ["INIT", "PREPARE"]
    assert "EXECUTE" not in staged["done"]


def test_the_first_stage_has_nothing_behind_it():
    """INIT active means an empty done-track, not a track containing INIT.

    The boundary case of the comparison above, and the one where a ``<=`` reads as
    plausible: with the run at the first node there is genuinely nothing traversed
    yet, so any lit history is invented.
    """
    staged = _staged({"r1": "pending"}, {})
    assert staged["active"] == ["INIT"]
    assert staged["done"] == []


def test_a_finished_runs_reported_stage_still_beats_the_derived_one():
    """Reported-over-derived holds for a run that has stopped, too.

    The precedence is tested for an in-flight run elsewhere; this is the other
    branch of ``_active_stage``, and it is the branch that decides what the row
    shows *after* a run. A run that failed in ANALYZE derives DONE from its status,
    and DONE would say the sequence completed. Where the bench told us where it got
    to, that is the more useful and the more honest node.
    """
    staged = _staged({"r1": "failed"}, {"r1": "ANALYZE"})
    assert staged["active"] == ["ANALYZE"]


def test_the_furthest_stage_any_stopped_run_reached_is_the_one_lit():
    """With nothing in flight, the row shows how far the sequence actually got.

    Position in :py:data:`STAGES` decides, not iteration order, so the answer is
    the same whichever run the agent happens to list first — the same
    order-independence the lit node needs everywhere else on this row.
    """
    runs = {"r1": "failed", "r2": "complete"}
    assert _staged(runs, {"r1": "PREPARE", "r2": "ANALYZE"})["active"] == ["ANALYZE"]
    assert _staged(runs, {"r2": "ANALYZE", "r1": "PREPARE"})["active"] == ["ANALYZE"]


def test_a_stage_reported_for_a_run_the_snapshot_does_not_list_lights_nothing():
    """``run_stages`` is keyed by run id and only speaks about runs that exist.

    Reachable across the two maps drifting by one frame — a replayed stage for a run
    trimmed from the model, or a snapshot stitched by an older feed. Lighting a node
    from it would have the row claim a sequence is in progress beside a run list that
    does not contain it, and an operator reading "EXECUTE" with nothing running has
    no way to tell which of the two is lying.

    Asserted twice: with no real runs at all, and beside a run the snapshot *does*
    list. The second case is the one that bites — the row is derived from the run
    map, so a stage key that leaked into the run set would be looked up in a table
    it is not in, taking the frame with it.
    """
    assert _staged({}, {"ghost": "EXECUTE"})["active"] == []
    staged = _staged({"r1": "complete"}, {"ghost": "EXECUTE"})
    assert staged["active"] == ["DONE"]
    assert staged["unknown"] is None


def test_an_empty_stage_map_falls_back_to_the_derived_node():
    """No bench has said anything, so the coarse mapping gets its say.

    The older-agent and connected-mid-run case, which is the only reason the
    derived fallback still exists. It must still work: a finished run with no
    reported stage reads DONE rather than leaving the row dark.
    """
    assert _staged({"r1": "complete"}, {})["active"] == ["DONE"]


@pytest.mark.parametrize("stages", [None, "EXECUTE", ["EXECUTE"], 7])
def test_a_run_stages_payload_that_is_not_a_map_costs_the_row_nothing_else(stages):
    """A malformed stage map degrades to the derived node, not to an exception.

    Same defensive treatment as every other newer field on this snapshot: it
    crossed a process boundary from an agent that may be a release behind — one
    that sends no ``run_stages`` at all — and the row must lose its reported stage
    rather than the panel losing its frame.
    """
    assert _staged({"r1": "complete"}, stages)["active"] == ["DONE"]


def test_a_stopped_run_naming_a_stage_this_build_cannot_place_lights_nothing():
    """An unplaceable name contributes nothing rather than being forced onto a node.

    The row's nodes come from this build's :py:data:`STAGES` while the names come
    off the wire, so a newer agent can report a stage that has no position here.
    Guessing one — nearest, or last — would put a run somewhere it never said it
    was; better a dark row than a confidently wrong node.
    """
    staged = _staged({"r1": "complete"}, {"r1": "CALIBRATE"})
    assert staged["active"] == []
    assert staged["done"] == []


def test_a_placeable_stage_is_not_reported_as_unknown():
    """``stage_unknown`` names only what the row cannot draw.

    The negative half of the unknown-stage test: a field that always reported None
    would silence a newer agent, and one that always reported the stage would have
    the renderer print "cannot place EXECUTE" beside a lit EXECUTE node. Both maps
    are asserted at once so the two halves cannot disagree.
    """
    staged = _staged({"r1": "running"}, {"r1": "EXECUTE"})
    assert staged["active"] == ["EXECUTE"]
    assert staged["unknown"] is None


# --------------------------------------------------------------------------
# The log pane
# --------------------------------------------------------------------------


def test_the_log_carries_real_events_only(live):
    """The reference art fills this pane with invented hex chatter. That belongs
    in the renderer's decorative layer, not in a list an operator scans to find
    out what actually happened."""
    live.apply_event(
        {"kind": "safety_trip", "severity": "alarm", "devices": ["otii_arc"], "seq": 7},
        now=101.0,
    )
    log = view_of(live)["log"]
    assert [e["kind"] for e in log] == ["safety_trip"]
    assert log[0]["severity"] == "alarm"
    assert log[0]["seq"] == 7


def test_the_log_is_empty_when_nothing_has_happened(live):
    assert view_of(live)["log"] == []


def test_the_log_is_bounded(live):
    for i in range(60):
        live.apply_event({"kind": "tick", "seq": i}, now=101.0 + i)
    assert len(view_of(live)["log"]) <= 24


def _action(**overrides):
    event = {
        "kind": "action",
        "severity": "info",
        "method": "device.call",
        "action": "set_voltage",
        "device": "rigol_dp2031",
        "detail": "3.3,channel=1 → none",
        "count": 1,
        "ok": True,
        "seq": 11,
    }
    event.update(overrides)
    return event


def test_an_action_reaches_the_pane_with_its_verb_and_detail(live):
    """The pane exists to show what the bench did, so the row has to say what.

    ``device.call`` carries every driver method on the bench, so without the
    device-level verb every row would read ``device.call`` and the pane would be
    24 identical lines.
    """
    live.apply_event(_action(), now=101.0)
    row = view_of(live)["log"][-1]
    assert row["action"] == "set_voltage"
    assert row["device"] == "rigol_dp2031"
    assert row["detail"] == "3.3,channel=1 → none"
    assert row["count"] == 1
    assert row["ok"] is True


def test_a_failed_action_is_marked_and_carries_its_error(live):
    live.apply_event(
        _action(
            kind="action_failed",
            severity="alarm",
            ok=False,
            detail="",
            error="TimeoutError: no response in 5.0s",
        ),
        now=101.0,
    )
    row = view_of(live)["log"][-1]
    assert row["ok"] is False
    assert row["severity"] == "alarm"
    assert "TimeoutError" in row["error"]


def test_a_row_without_a_device_verb_falls_back_to_the_wire_method(live):
    """``agent.open`` has no device-level verb and is still worth a row.

    An empty first column would make the most important lines on the pane —
    opening and closing an instrument — the blankest ones.
    """
    live.apply_event(
        _action(method="agent.open", action="", detail="→ {key=x,open=true}"), now=101.0
    )
    assert view_of(live)["log"][-1]["action"] == "agent.open"


def test_a_long_value_is_truncated_before_it_reaches_the_glass(live):
    """The agent bounds these already; this pane must not depend on that.

    The limit is enforced in another process, across a version boundary, on a
    payload that arrives from the network. An agent one release behind — or a
    future one with a laxer limit — must cost this pane a shortened line, not a
    row that runs off the panel and drags the columns beside it out of alignment.
    A waveform must never be what is on screen.
    """
    from benchctrl.dashboards.fui.view import (
        _ACTION_CHARS,
        _DETAIL_CHARS,
        _ERROR_CHARS,
    )

    live.apply_event(
        _action(
            action="s" * 500,
            detail="9" * 100_000,
            kind="action_failed",
            ok=False,
            error="E" * 100_000,
        ),
        now=101.0,
    )
    row = view_of(live)["log"][-1]
    assert len(row["detail"]) <= _DETAIL_CHARS, f"detail is {len(row['detail'])} chars"
    assert len(row["error"]) <= _ERROR_CHARS, f"error is {len(row['error'])} chars"
    assert len(row["action"]) <= _ACTION_CHARS, f"action is {len(row['action'])} chars"


def test_a_multiline_error_stays_one_row(live):
    """A row count is how a reader judges how much the bench did.

    An exception whose message contains a newline would otherwise occupy two rows
    and make one failure look like two.
    """
    live.apply_event(
        _action(kind="action_failed", ok=False, error="OSError: read failed\n  retry 3"),
        now=101.0,
    )
    row = view_of(live)["log"][-1]
    assert "\n" not in row["error"]
    assert "retry 3" in row["error"]


def test_a_folded_burst_shows_its_count_rather_than_hiding_it(live):
    """A summarised log has to look summarised.

    The agent folds repeated identical reads into one line with a count. Dropping
    that count would make 47 reads render as one, which is the silent truncation
    the honesty rules forbid.
    """
    live.apply_event(_action(action="read_raw", severity="debug", count=47), now=101.0)
    view = view_of(live)
    assert view["log"][-1]["count"] == 47


def test_a_row_count_is_never_zero_or_missing(live):
    """A ``×0`` beside a line saying something happened is a contradiction."""
    for value in (None, 0, -5, "many", True):
        live.apply_event(_action(count=value), now=101.0)
        assert view_of(live)["log"][-1]["count"] >= 1


def test_the_pane_can_say_how_much_it_is_not_showing(live):
    """24 rows off a stream that can run at thousands of actions a second.

    The counts are what let the header admit the pane is a summary. Without them
    a chronically-folded log looks like a complete record of a quiet bench.
    """
    live.apply_event(_action(count=50, folded=4000), now=101.0)
    view = view_of(live)
    assert view["actions"] == 50
    assert view["actions_folded"] == 4000
    # And folding is not reported as event loss: different failures.
    assert view["dropped_events"] == 0


def test_the_pane_shows_the_newest_actions_when_a_run_floods_it(live):
    """Bounded, and bounded from the right end: the last 24, not the first."""
    for i in range(200):
        live.apply_event(_action(action="read_raw", severity="debug", seq=i), now=101.0 + i)
    log = view_of(live)["log"]
    assert len(log) == 24
    assert log[-1]["seq"] == 199, "the pane kept the oldest rows instead of the newest"
    assert log[0]["seq"] == 176


def test_the_view_survives_no_status_object(live):
    """The server passes one, but the view must not require it — a None here
    should cost the log pane, not the whole display."""
    snap = live.to_dict()
    snap["reconnects"] = 0
    view = build_view(snap)
    assert view["log"] == []
    assert view["headline"] == "IDLE"


def test_a_denied_observer_role_reaches_the_operator_in_plain_words():
    """The state model catches the downgrade (see test_dashboard_state.py); this
    is about it arriving on the glass.

    "NOT OBSERVER" is jargon on its own. The person standing at the bench needs
    the consequence, so the footer spells out that the deadman may not fire —
    and it counts as an alert, because it is something to act on.
    """
    s = BenchStatus()
    s.apply_connected({**WELCOME, "observer": False}, now=100.0)
    s.apply_status(status_payload({"otii_arc": dev()}), now=100.0)
    view = view_of(s)

    assert view["headline"] == "NOT OBSERVER"
    assert view["observer_denied"] is True
    assert "DEADMAN MAY NOT FIRE" in view["operation"]
    assert view["alerts"] >= 1
    assert not view["trustworthy"]


def test_a_denied_observer_role_never_reads_system_ready():
    """The specific lie to rule out: an idle bench on a downgraded session looks
    exactly like a healthy one, and this is the panel that must not say so."""
    s = BenchStatus()
    s.apply_connected({**WELCOME, "observer": False}, now=100.0)
    s.apply_status(status_payload({"otii_arc": dev()}), now=100.0)

    assert "SYSTEM READY" not in view_of(s)["operation"]


# --------------------------------------------------------------------------
# The frame governor
# --------------------------------------------------------------------------
#
# These run the real fui.js under node with a fake clock, because the bug they
# exist to catch cannot be seen by reading the file.
#
# The first governor measured cost as time spent inside the draw calls. That is
# structurally always ~0: canvas rasterisation happens off the main thread, so
# `performance.now()` around the drawing saw nothing while the board burned 130%
# of a core. The ladder never engaged. Nothing about that code looked wrong --
# the tiers were there, the thresholds were there, the comparison was there --
# and a test asserting the tiers exist, or grepping for `LATE_RATIO`, would have
# passed on the broken version just as happily as on this one.
#
# So the assertion has to be behavioural: feed the loop frames that arrive late
# and require the tier to actually drop. An inert governor fails that no matter
# how it is spelled.

FUI_JS = pathlib.Path(fui_static.__file__).parent / "static" / "fui.js"
FUI_CSS = pathlib.Path(fui_static.__file__).parent / "static" / "fui.css"

# Everything below the governor needs a DOM. We want the governor alone, so cut
# the file at the section marker rather than shimming a browser: a stub DOM rich
# enough to run the renderer is a second implementation to keep in step, and it
# would let the test pass against a governor that never ran in a real page.
_GOVERNOR_END = "/* ---------------------------------------------------------------- decoration */"


def governor_source() -> str:
    src = FUI_JS.read_text(encoding="utf-8")
    head, sep, _ = src.partition(_GOVERNOR_END)
    assert sep, f"section marker moved in {FUI_JS.name}; this test slices on it"
    return head


NODE = shutil.which("node") or shutil.which("nodejs")
needs_node = pytest.mark.skipif(NODE is None, reason="no node runtime for the JS governor")


def run_governor(js: str, *, window: str = "{}") -> dict:
    """Execute the shipped governor over a scripted sequence of frame times.

    `window` defaults to an object with no matchMedia, which is the governing
    path: REDUCED is false. It is spelled out rather than left undefined, because
    a missing `window` throws at module scope and every test here would then fail
    for a reason that has nothing to do with the governor.
    """
    harness = "\n".join([f"const window = {window};", governor_source(), js])
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


# A frame interval comfortably past LATE_RATIO for every tier, and one inside it.
LATE_MS = 2000 / 5 * 1.6      # later than even MINIMAL's target allows
ON_TIME_MS = 1000 / 30        # faster than any tier asks for


@needs_node
def test_the_governor_steps_down_when_frames_arrive_late():
    """The bug this catches: a governor that measures a number always near zero.

    It looks complete and does nothing. The only way to tell the difference is to
    make frames late and demand that something happens.
    """
    r = run_governor(f"""
      let t = 0;
      const seen = [];
      for (let i = 0; i < 400; i++) {{ t += {LATE_MS}; governFrame(t); seen.push(Q.now.name); }}
      console.log(JSON.stringify({{ tier: Q.now.name, ema: Q.ema, seen: [...new Set(seen)] }}));
    """)
    assert r["tier"] == TIERS_CHEAPEST, (
        f"late frames left the tier at {r['tier']}: the governor is inert"
    )
    # Every rung, in order -- not a jump straight to the floor. A governor that
    # bottoms out on the first late frame would degrade the display for a hiccup.
    assert r["seen"] == ["HIGH", "MED", "LOW", "MINIMAL"]


@needs_node
def test_the_governor_climbs_back_when_the_board_recovers():
    """Degradation has to be temporary, or one busy moment costs the display
    permanently and nobody knows why the panel looks coarse a week later."""
    r = run_governor(f"""
      let t = 0;
      for (let i = 0; i < 400; i++) {{ t += {LATE_MS}; governFrame(t); }}
      const bottom = Q.now.name;
      for (let i = 0; i < 4000; i++) {{ t += {ON_TIME_MS}; governFrame(t); }}
      console.log(JSON.stringify({{ bottom, tier: Q.now.name }}));
    """)
    assert r["bottom"] == TIERS_CHEAPEST
    assert r["tier"] == "FULL"


@needs_node
def test_stepping_down_is_fast_and_stepping_up_is_slow():
    """Asymmetric on purpose. An over-budget display is stealing time from the
    bench right now, so it must yield quickly; climbing back is cosmetic, so it
    waits long enough that the tier cannot visibly oscillate."""
    r = run_governor(f"""
      let t = 0, down = 0;
      const start = Q.now.name;
      while (Q.now.name === start) {{ t += {LATE_MS}; governFrame(t); down++; }}
      const at = Q.now.name;
      let up = 0;
      while (Q.now.name === at) {{ t += {ON_TIME_MS}; governFrame(t); up++; }}
      console.log(JSON.stringify({{ down, up }}));
    """)
    assert r["down"] <= 10, "a late display must yield within a few frames"
    assert r["up"] > r["down"] * 10, "recovery must be far slower than degradation"


@needs_node
def test_a_hidden_tab_is_not_mistaken_for_a_slow_board():
    """A backgrounded tab delivers one frame after minutes. Treated as lateness
    that reads as a catastrophically slow board, and the panel would come back
    from the screensaver permanently degraded for no reason."""
    r = run_governor("""
      let t = 0;
      for (let i = 0; i < 40; i++) { t += 1000 / 30; governFrame(t); }
      const before = Q.now.name;
      t += 600000;                    /* ten minutes hidden */
      governFrame(t);
      console.log(JSON.stringify({ before, tier: Q.now.name, over: Q.over }));
    """)
    assert r["tier"] == r["before"]
    assert r["over"] == 0


@needs_node
def test_the_governor_only_ever_degrades_decoration():
    """The load-bearing guarantee: no tier may touch the data clock.

    Every knob a tier owns is scenery -- frame interval, canvas resolution, glow.
    If a tier ever gained a field that scaled POLL_MS, throttling the animation
    would start making the readouts stale, which is the one thing the split
    clocks exist to prevent.
    """
    r = run_governor("console.log(JSON.stringify({ tiers: TIERS, poll: POLL_MS }))")
    assert r["poll"] == 500
    for tier in r["tiers"]:
        assert set(tier) == {"name", "glow", "res", "frameMs", "dsMs"}, (
            f"tier {tier['name']} grew a knob; if it can affect data, the "
            "governor is no longer decoration-only"
        )
    # And the data clock is on a timer of its own, not derived from a tier.
    assert "setInterval(poll, POLL_MS)" in FUI_JS.read_text(encoding="utf-8")


@needs_node
def test_the_cheapest_tier_still_draws_a_recognisable_display():
    """MINIMAL is a degraded panel, not a broken one. A tier that dropped glow to
    zero or resolution near zero would be unreadable from across the bench, which
    fails the display's purpose more thoroughly than costing a few percent CPU."""
    r = run_governor("console.log(JSON.stringify({ tiers: TIERS }))")
    floor = r["tiers"][-1]
    assert floor["name"] == TIERS_CHEAPEST
    assert floor["glow"] >= 1, "a glow-less FUI is a spreadsheet"
    assert floor["res"] >= 0.5, "half resolution is the floor for legibility"
    assert 1000 / floor["frameMs"] >= 4, "below ~4 fps the hologram reads as frozen"


@needs_node
def test_reduced_motion_pins_the_tier_instead_of_governing_it():
    """When the platform asks for reduced motion, the tier is a user preference
    and not a measurement -- the governor must not climb back out of it."""
    r = run_governor(
        """
        const pinned = Q.now.name;
        let t = 0;
        /* Frames arriving perfectly on time: the step-up path, if it ran. */
        for (let i = 0; i < 4000; i++) { t += 1000 / 60; governFrame(t); }
        console.log(JSON.stringify({ pinned, after: Q.now.name }));
        """,
        window="{ matchMedia: () => ({ matches: true }) }",
    )
    assert r["pinned"] == TIERS_CHEAPEST
    assert r["after"] == TIERS_CHEAPEST, "reduced motion must not be governed away"


# --------------------------------------------------------------------------
# The activity line, run under node
#
# The view decides what may be claimed and is asserted above; this covers the one
# step after it that Python cannot see. It matters here because the activity line
# is the only readout on the rail whose *tense* is carried by the renderer: the
# view hands over a number, and "3.2s ago" versus a bare "3.2" is the difference
# between reporting the past and asserting the present.
# --------------------------------------------------------------------------

# slotActivity needs no DOM — it takes a slot dict and returns a string — so it
# can be sliced out and run directly rather than shimmed a browser.
_ACTIVITY_FN_START = "function slotActivity(inst) {"


def activity_source() -> str:
    """The real ``slotActivity`` alone, lifted out of the renderer.

    Sliced from the shipped file rather than reimplemented, for the reason the
    governor tests give: a second copy would pass while the file that runs in the
    page was broken.
    """
    src = FUI_JS.read_text(encoding="utf-8")
    start = src.find(_ACTIVITY_FN_START)
    assert start >= 0, f"slotActivity moved in {FUI_JS.name}; this test slices on it"
    # Balance braces from the function's opening one, so the slice ends at the
    # function's own close rather than at whatever the next `}` happens to be.
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError("unbalanced braces slicing slotActivity")


def run_activity(inst: dict) -> str:
    harness = "\n".join(
        [
            activity_source(),
            f"console.log(JSON.stringify(slotActivity({json.dumps(inst)})));",
        ]
    )
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


@needs_node
def test_the_renderer_shows_a_call_in_flight_with_no_age():
    """A call that is running has no age to report — it has not finished. Printing
    one would be arithmetic dressed as information."""
    line = run_activity(
        {"busy": "measure_resistance_4wire", "queued": 0, "recent": None}
    )
    assert "measure_resistance_4wire" in line
    assert "ago" not in line


@needs_node
def test_the_renderer_marks_a_finished_call_as_past():
    """The tense is the renderer's contribution and the whole point of the field.

    A bare "measure_resistance_4wire" on the rail reads as running. The view
    deliberately hands over the age so this line can say the call is over; dropping
    the "ago" would turn an honest past-tense marker into a false present-tense
    claim, which is the one thing this panel is not allowed to do.
    """
    line = run_activity(
        {"busy": None, "queued": 0, "recent": {"age_s": 3.2, "action": "read_voltage"}}
    )
    assert "read_voltage" in line
    assert "3.2" in line
    assert "ago" in line


@needs_node
def test_the_renderer_prefers_the_call_in_flight_over_the_finished_one():
    """Same precedence the view keeps, enforced at the last step too: the present
    tense wins the one line the slot has for this."""
    line = run_activity(
        {
            "busy": "measure_resistance_4wire",
            "queued": 2,
            "recent": {"age_s": 1.0, "action": "read_voltage"},
        }
    )
    assert "measure_resistance_4wire" in line
    assert "read_voltage" not in line
    assert "+2" in line, "a waiting queue went unreported"


@needs_node
def test_the_renderer_draws_nothing_for_a_slot_with_no_activity():
    """Empty, not "idle". A word there would be a claim the view never made, and
    every dark slot carries these fields as None."""
    assert run_activity({"busy": None, "queued": 0, "recent": None}) == ""


@needs_node
def test_the_renderer_survives_an_age_with_no_action_name():
    """The age is the load-bearing half: "something happened 2s ago" is the fact
    that was missing from the rail, and an older agent may not name it."""
    line = run_activity(
        {"busy": None, "queued": 0, "recent": {"age_s": 2.0, "action": ""}}
    )
    assert "2" in line
    assert "ago" in line


# --------------------------------------------------------------------------
# The HTTP surface
# --------------------------------------------------------------------------


class FakeClient:
    is_connected = True
    welcome = dict(WELCOME)
    on_event = None

    def status(self):
        return status_payload({"otii_arc": dev()})

    def close(self):
        self.is_connected = False


@pytest.fixture()
def server():
    feed = AgentFeed(
        EndpointConfig(host="127.0.0.1", port=9737, token="t"),
        poll_s=0.05,
        connect=lambda: FakeClient(),
    )
    # Port 0: the OS picks a free one, so a developer's running dashboard does
    # not make the suite fail.
    srv = FuiServer(feed.endpoint, host="127.0.0.1", port=0, feed=feed).start()
    try:
        yield srv
    finally:
        srv.stop()


def get(srv, path, *, timeout=5.0):
    host, port = srv.address
    return urllib.request.urlopen(f"http://{host}:{port}{path}", timeout=timeout)


def test_the_view_endpoint_serves_the_model(server):
    body = json.loads(get(server, "/api/view").read())
    assert body["headline"] in ("STARTING", "IDLE")
    assert len(body["instruments"]) == len(INSTRUMENTS)


def test_the_page_and_its_assets_are_served(server):
    for path, needle in (
        ("/", b"BENCH CONTROL CORE"),
        ("/fui.css", b"--cyan"),
        ("/fui.js", b"/api/view"),
    ):
        assert needle in get(server, path).read(), path


def test_bench_status_is_never_cached(server):
    """A cached bench status is a lying bench status: a kiosk browser that served
    a 200 from cache would show a frozen screen that looks live."""
    assert get(server, "/api/view").headers["Cache-Control"] == "no-store"


def test_the_static_route_cannot_be_walked_out_of_its_directory(server):
    """The handler reads files by name, so this is the one real attack on it."""
    for path in (
        "/../server.py",
        "/..%2fserver.py",
        "/....//server.py",
        "/%2e%2e/%2e%2e/etc/passwd",
    ):
        with pytest.raises(urllib.error.HTTPError) as caught:
            get(server, path)
        assert caught.value.code == 404, path


def test_there_is_no_write_route(server):
    """Read-only by construction. Defence in depth — the observer session would
    refuse anyway — but a display should not offer a verb it must not honour."""
    host, port = server.address
    req = urllib.request.Request(
        f"http://{host}:{port}/api/view", data=b"{}", method="POST"
    )
    with pytest.raises(urllib.error.HTTPError) as caught:
        urllib.request.urlopen(req, timeout=5.0)
    assert caught.value.code in (400, 405, 501), caught.value.code


def test_the_feed_reaches_the_bench_through_the_server(server):
    """End to end: the HTTP layer really is showing what the feed folded."""
    deadline = threading.Event()
    for _ in range(100):
        body = json.loads(get(server, "/api/view").read())
        if body["connected"] and slot(body, "otii_arc")["linked"]:
            break
        deadline.wait(0.05)
    else:
        pytest.fail(f"the feed never reached the fake bench: {body}")
    assert slot(body, "otii_arc")["status"] == OPEN


# --------------------------------------------------------------------------
# IN RUN: an instrument a live run declared, held for the run's whole duration
#
# The complaint these answer: "the instrument cards still show standby while the
# device is in use", and the request that followed it — a run should identify the
# instruments it will use and hold them as in-use for the duration of the test.
#
# Activity and enrollment are deliberately two mechanisms because they answer
# two questions. A run's calls come in bursts: a supply set once at phase entry
# and held through a ten-minute dwell is in use the whole time while making no
# calls for all but 200 ms of it. Activity marks the instant; enrollment covers
# the dwell. Neither alone is enough.
# --------------------------------------------------------------------------


def test_a_device_a_live_run_declared_reads_as_in_a_run():
    """The headline property. A run that says it will drive this instrument makes
    the card say so, for as long as the run lasts."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        enrolled_run="r7",
    )
    assert state["status"] == IN_RUN
    assert state["run"] == "r7"
    # Marked linked: something is actively driving it, which is exactly the
    # condition the renderer's bright treatment is reserved for.
    assert state["linked"] is True


def test_enrollment_outranks_an_open_session():
    """Both are true at once and they compete for one word, so the order matters.

    OPEN says the agent holds a handle. IN RUN says something is using that handle
    and will keep using it. The second is the stronger claim about the same device,
    and it is the one an operator watching a test wants.
    """
    slot = {"served": True, "present": True, "opened": True}
    common = dict(
        trustworthy=True, inventory_taken=True, registry_known=True, connected=True
    )
    assert _slot_state(None, slot, **common)["status"] == OPEN
    assert _slot_state(None, slot, **common, enrolled_run="r7")["status"] == IN_RUN


def test_an_arm_state_outranks_enrollment():
    """A live output outranks every claim about what should be happening.

    The whole precedence ladder exists to stop a configuration detail displacing a
    hazard, and enrollment is the newest thing that could have broken it.
    """
    state = _slot_state(
        {"label": "armed", "armed": True, "inferred": False},
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        enrolled_run="r7",
    )
    assert state["status"] == "ARMED"
    assert state["armed"] is True


# --------------------------------------------------------------------------
# The governor's resting label is not a status word
#
# R1 says never show STANDBY for a device in use. STANDBY was fixed; IDLE was
# not, and it entered the ladder two rungs higher. ``DeviceView.label`` returns
# "idle" when nothing is armed, the top rung published it verbatim, and so every
# device the governor tracked was pinned to a word that outranked IN RUN, both
# forms of OPEN, and every hazard rung below it.
#
# Measured on hardware before the fix: a DMM a live run had declared read IDLE
# for all 82 in-flight samples, with no run attribution on the slot. These tests
# are the ladder-level statement of that, so it cannot come back on a bench
# nobody is watching.
# --------------------------------------------------------------------------


def test_the_governors_resting_label_does_not_outrank_a_run():
    """The captured defect. A governor-tracked device a run declared reads IN RUN.

    The governor creates state for a device on the first call that *could* arm
    something, so a run driving an instrument guarantees this combination — which
    is what made the old ordering not a corner case but the common one.
    """
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        {"served": True, "present": True, "opened": True},
        **_LADDER,
        enrolled_run="r7",
    )
    assert state["status"] == IN_RUN
    assert state["run"] == "r7"
    assert state["armed"] is False


def test_the_governors_resting_label_is_never_shown_as_a_status_word():
    """"IDLE" is not in this module's vocabulary, and a bare governor entry is not
    an activity report — it is evidence of an open handle, which OPEN already says.
    """
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        {"served": True, "present": True, "opened": True},
        **_LADDER,
    )
    assert state["status"] == OPEN
    assert state["status"] != "IDLE"


def test_a_governor_entry_alone_is_enough_to_report_an_open_session():
    """No device table, no ``served`` flag — but the governor has called this
    device, so the agent has told us about it by another route. Both of those gates
    mean "nobody said"; here somebody did, and NO LINK would be false."""
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        None,
        trustworthy=True,
        inventory_taken=False,
        registry_known=False,
        connected=True,
    )
    assert state["status"] == OPEN
    assert state["linked"] is True
    # Inferred, not confirmed: a governor entry implies the handle rather than
    # reporting it, and the renderer draws that dashed.
    assert state["inferred"] is True


def test_a_registry_confirmed_open_session_is_not_marked_inferred():
    """The other half of the pair above. When the device table *does* say opened,
    the reading is reported rather than deduced and must not be drawn dashed."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        **_LADDER,
    )
    assert state["status"] == OPEN
    assert state["inferred"] is False


def test_an_arm_state_still_outranks_everything_below_it():
    """The exemption above must not have cost the hazard rung its precedence. Any
    label that is not the resting one is a live output and stays at the top."""
    for label in ("armed", "emulating", "recording"):
        state = _slot_state(
            {"label": label, "armed": label == "armed", "inferred": False},
            {"served": True, "present": True, "opened": True},
            **_LADDER,
            enrolled_run="r7",
        )
        assert state["status"] == label.upper(), label
        assert state["status"] != IN_RUN


def test_an_armed_flag_is_believed_even_when_the_label_disagrees():
    """``armed`` is checked in its own right, not inferred from the label.

    :py:class:`DeviceView` derives one from the other, so on a self-consistent
    payload the two halves of the check are redundant — which is exactly why this
    test builds an inconsistent one. ``build_view`` is handed whatever an
    ``AgentFeed`` produced, and every other guard in this ladder is tested against
    a payload the state model would never emit for the same reason.

    Of the two fields, ``armed`` is the one that must win: it is the hazard flag,
    and a stale or wrong label must not be able to demote a live output to a run
    marker. Reading the label alone would do exactly that.
    """
    state = _slot_state(
        {"label": "idle", "armed": True, "inferred": False},
        {"served": True, "present": True, "opened": True},
        **_LADDER,
        enrolled_run="r7",
    )
    assert state["armed"] is True
    assert state["status"] != IN_RUN
    assert state["status"] != OPEN


def test_a_governor_entry_does_not_outrank_a_measured_absence():
    """A cable pulled mid-run leaves the governor's state object behind. The scan
    is the newer measurement and the actionable one, exactly as it is for a
    registry-reported session."""
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        {"served": True, "present": False, "opened": True},
        **_LADDER,
        discoverable=True,
        enrolled_run="r7",
    )
    assert state["status"] == ABSENT


def test_a_governor_entry_does_not_outrank_a_lost_session():
    """Nothing is known on a dead link, including what the governor last held."""
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=False,
    )
    assert state["status"] == NO_LINK


def test_a_governor_entry_does_not_outrank_an_open_failure():
    """An open that failed is the one thing here the operator can usually fix."""
    state = _slot_state(
        {"label": "idle", "armed": False, "inferred": False},
        {"served": True, "present": True, "opened": False, "open_error": "busy"},
        **_LADDER,
    )
    assert state["status"] == FAULT


def test_a_measured_absence_outranks_enrollment():
    """A run declaring it will drive an instrument is a statement of intent. A scan
    that looked and could not find the instrument is a measurement, and a
    measurement outranks intent — the operator needs to know the thing is gone,
    not that a run believes otherwise."""
    state = _slot_state(
        None,
        {"served": True, "present": False, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        discoverable=True,
        enrolled_run="r7",
    )
    assert state["status"] == ABSENT


def test_an_open_failure_outranks_enrollment():
    """An open that failed is something the operator can fix, and it means the run
    is not driving this device whatever it declared."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False, "open_error": "busy"},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        enrolled_run="r7",
    )
    assert state["status"] == FAULT


def test_enrollment_claims_nothing_without_a_session():
    """No session means every fact about this device is of unknown age, including
    a run's claim on it. A run id from the last connected moment must not keep a
    card lit after the link drops."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=False,
        enrolled_run="r7",
    )
    assert state["status"] == NO_LINK


def test_an_enrolled_slot_on_a_stale_view_is_marked_stale_not_dropped():
    """Deliberately different from the activity marker, which is withheld outright.

    An activity line is an assertion about this instant and has no honest stale
    form. Enrollment is not: "a run owned this device, and we have lost sight of
    it" is both true and the single most useful thing a panel that has gone quiet
    can say. So it survives staleness and is struck through instead.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        trustworthy=False,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        enrolled_run="r7",
    )
    assert state["status"] == IN_RUN
    assert state["stale"] is True


def test_a_slot_no_run_declared_says_nothing_about_runs():
    """Absence of enrollment is not a claim. A device nothing declared carries no
    run field for the renderer to key a highlight off."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
    )
    assert state["status"] == OPEN
    assert not state.get("run")


# ---- _enrolled_run: a table that crossed a process boundary --------------


def test_the_enrollment_table_names_the_run_that_holds_a_device():
    assert _enrolled_run("dmm", {"dmm": "r7"}) == "r7"


def test_a_device_no_run_holds_is_not_enrolled():
    assert _enrolled_run("dmm", {"psu": "r7"}) is None


def test_a_malformed_enrollment_table_costs_one_marker_not_the_render():
    """This field arrives from an agent that may be a release ahead or behind, and
    it is read inside the renderer. Anything unusable must yield None rather than
    raise: the cost of a bad payload is this slot's IN RUN marker, never the panel.
    """
    assert _enrolled_run("dmm", None) is None
    assert _enrolled_run("dmm", "r7") is None
    assert _enrolled_run("dmm", []) is None
    assert _enrolled_run("dmm", {"dmm": 7}) is None
    assert _enrolled_run("dmm", {"dmm": None}) is None


def test_a_blank_run_id_does_not_license_an_in_run_claim():
    """A truthy id is what earns the IN RUN word, so an empty one must not.

    "In a run I cannot name" is a worse readout than falling through to the
    open-session rung, which is still true and still says the agent is talking to
    the device.
    """
    assert _enrolled_run("dmm", {"dmm": ""}) is None
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        enrolled_run=_enrolled_run("dmm", {"dmm": ""}),
    )
    assert state["status"] == OPEN


# ---- build_view: the whole path, from a real model to rail slots ----------


def test_the_rail_shows_in_run_for_a_device_a_real_run_declared():
    """End to end through the real model and the real view, using the event kind
    the agent actually emits. The unit tests above all inject ``enrolled_run``
    directly; this one proves ``build_view`` computes it from a run event."""
    live = BenchStatus()
    live.apply_connected(
        {
            "observer": True,
            "agent": "bench",
            "devices": [{"key": "siglent_sdm4065a", "open": False}],
        },
        now=100.0,
    )
    live.apply_status(
        {
            "safety": {},
            "workers": {},
            "devices": {"siglent_sdm4065a": {"open": True}},
        },
        now=101.0,
    )
    live.apply_event(
        {
            "kind": "run_start",
            "run_id": "r7",
            "name": "sweep",
            "devices": ["siglent_sdm4065a"],
        },
        now=102.0,
    )
    view = build_view(live.to_dict(), live)
    row = next(i for i in view["instruments"] if i["key"] == "siglent_sdm4065a")
    assert row["status"] == IN_RUN
    assert row["run"] == "r7"


def test_the_rail_releases_a_device_when_its_run_ends():
    """No explicit clearing anywhere: enrollment has exactly the lifetime of the
    in-flight run that declared it, which is what stops a crashed run pinning an
    instrument to IN RUN forever."""
    live = BenchStatus()
    live.apply_connected(
        {
            "observer": True,
            "agent": "bench",
            "devices": [{"key": "siglent_sdm4065a", "open": False}],
        },
        now=100.0,
    )
    live.apply_status(
        {
            "safety": {},
            "workers": {},
            "devices": {"siglent_sdm4065a": {"open": True}},
        },
        now=101.0,
    )
    live.apply_event(
        {"kind": "run_start", "run_id": "r7", "devices": ["siglent_sdm4065a"]},
        now=102.0,
    )
    live.apply_event({"kind": "run_end", "run_id": "r7", "status": "ok"}, now=103.0)
    view = build_view(live.to_dict(), live)
    row = next(i for i in view["instruments"] if i["key"] == "siglent_sdm4065a")
    # Back to OPEN, not stuck on IN RUN and not fallen through to STANDBY: the
    # session is still open, which is exactly what OPEN means.
    assert row["status"] == OPEN
    assert not row.get("run")


def test_a_view_from_an_agent_that_declares_no_devices_still_renders():
    """An older agent's run_start carries no device list. The rail must lose the
    IN RUN marker and nothing else — the same defensive contract every other
    cross-process field on this panel has."""
    live = BenchStatus()
    live.apply_connected(
        {
            "observer": True,
            "agent": "bench",
            "devices": [{"key": "siglent_sdm4065a", "open": False}],
        },
        now=100.0,
    )
    live.apply_status(
        {
            "safety": {},
            "workers": {},
            "devices": {"siglent_sdm4065a": {"open": True}},
        },
        now=101.0,
    )
    live.apply_event({"kind": "run_start", "run_id": "r7"}, now=102.0)
    snap = live.to_dict()
    assert snap["enrolled"] == {}
    view = build_view(snap, live)
    row = next(i for i in view["instruments"] if i["key"] == "siglent_sdm4065a")
    assert row["status"] == OPEN


def test_build_view_survives_a_snapshot_with_no_enrolled_key_at_all():
    """``to_dict`` always emits the key, but ``build_view`` is also handed
    snapshots by tests and by any future caller. A missing key must read as "no
    enrollment", not raise inside the renderer."""
    live = BenchStatus()
    live.apply_connected(
        {
            "observer": True,
            "agent": "bench",
            "devices": [{"key": "siglent_sdm4065a", "open": False}],
        },
        now=100.0,
    )
    live.apply_status({"safety": {}, "workers": {}}, now=101.0)
    snap = live.to_dict()
    del snap["enrolled"]
    view = build_view(snap, live)
    row = next(i for i in view["instruments"] if i["key"] == "siglent_sdm4065a")
    assert not row.get("run")


def test_a_junk_enrolled_field_does_not_reach_the_slots():
    """A malformed table costs the rail its IN RUN markers and nothing else.

    Enforced in one place, ``_enrolled_run``, and this test says so deliberately.
    ``build_view`` briefly normalised the field as well; a mutation deleting that
    upstream guard killed no test, because no input reaches one check without
    reaching the other. It was removed rather than pinned with a test that could
    only pass — an untestable line that looks like a safety check is worse than no
    line, because the next reader trusts it.
    """
    live = BenchStatus()
    live.apply_connected(
        {
            "observer": True,
            "agent": "bench",
            "devices": [{"key": "siglent_sdm4065a", "open": False}],
        },
        now=100.0,
    )
    live.apply_status({"safety": {}, "workers": {}}, now=101.0)
    snap = live.to_dict()
    snap["enrolled"] = "r7"
    view = build_view(snap, live)
    row = next(i for i in view["instruments"] if i["key"] == "siglent_sdm4065a")
    assert not row.get("run")


# --------------------------------------------------------------------------
# OPEN inferred from activity: the seam between two feeds at different rates
#
# Measured on the board, not theorised. After the confirmed-OPEN rung landed, a
# 195 s sweep still showed the DMM reading STANDBY for the first ~4 s of traffic
# while its activity line was already updating — because activity arrives on the
# event stream in milliseconds and ``opened`` arrives on the 5 s status poll. Ten
# samples out of 170, and exactly the reported symptom: a card showing standby
# for a device in use, with "some text on the card updating".
#
# A device cannot execute a call without an open handle, so during that window the
# panel already holds proof the device is open — it just has not been *told*. The
# rung reports OPEN and marks it ``inferred`` (drawn dashed), which is what that
# flag means everywhere else here: deduced from an event, not reported.
# --------------------------------------------------------------------------

_LADDER = dict(
    trustworthy=True, inventory_taken=True, registry_known=True, connected=True
)


def test_a_device_that_just_answered_a_call_is_not_shown_as_standby():
    """The captured defect, at the sample that exhibited it.

    The DMM at t=17.3 s of the sweep: served, on the bus, activity 0.5 s old, and
    ``opened`` still False because no status poll had landed yet. It read STANDBY.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False, "confidence": "exact"},
        **_LADDER,
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
    )
    assert state["status"] == OPEN
    # Dashed, not solid: this is deduced from an event, and the seam should be
    # visible as a slightly-less-confirmed OPEN rather than papered over.
    assert state["inferred"] is True
    assert state["linked"] is True


def test_an_undiscoverable_device_in_use_is_not_shown_as_unidentifiable():
    """The QR10x's version of the same window, which its own rung cannot cover.

    It sits behind a driverless CH340 with no VID/PID, so ``present`` is False and
    ``discoverable`` is False — no scan will ever confirm it. During the sweep that
    was driving it, it read NO ID. A completed call answers the presence question
    that UNDETERMINED says nobody has answered.
    """
    state = _slot_state(
        None,
        {"served": True, "present": False, "opened": False},
        **_LADDER,
        discoverable=False,
        recent={"action": "set_resistance", "age_s": 0.0},
    )
    assert state["status"] == OPEN
    assert state["inferred"] is True


def test_a_quiet_device_still_reads_standby():
    """The rung must not make every served device read OPEN.

    STANDBY is the honest resting state of a healthy bench — the registry opens
    devices lazily, so "on the bus, not opened yet" is a real and common condition.
    A rung that fired without evidence would trade one wrong word for another.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False, "confidence": "exact"},
        **_LADDER,
        recent=None,
    )
    assert state["status"] == STANDBY


def test_being_busy_is_evidence_enough_on_its_own():
    """``busy`` means a call is in flight right now — strictly stronger evidence
    than ``recent``, which only says one completed. Either alone suffices."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False, "confidence": "exact"},
        **_LADDER,
        busy="measure_resistance_4wire",
    )
    assert state["status"] == OPEN
    assert state["inferred"] is True


def test_a_confirmed_open_session_is_not_marked_inferred():
    """Both rungs say OPEN, so the distinction lives entirely in ``inferred``.

    One poll after the seam closes, the confirmed rung must supersede the deduced
    one — otherwise the card would stay dashed for the whole session and the flag
    would stop meaning anything.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": True},
        **_LADDER,
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
    )
    assert state["status"] == OPEN
    assert state["inferred"] is False


def test_a_measured_absence_outranks_activity():
    """A scan that looked and could not find the device keeps the word.

    The safety ordering: activity is evidence about the recent past, ABSENT is a
    current measurement the operator can act on. A device whose cable was pulled
    mid-sweep has both — a few-seconds-old call and a scan that no longer sees it —
    and the actionable one must win.
    """
    state = _slot_state(
        None,
        {"served": True, "present": False, "opened": False, "confidence": "exact"},
        **_LADDER,
        discoverable=True,
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
    )
    assert state["status"] == ABSENT


def test_enrollment_outranks_activity_inferred_from_a_call():
    """IN RUN is the stronger claim and stays above both forms of OPEN."""
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False},
        **_LADDER,
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
        enrolled_run="r7",
    )
    assert state["status"] == IN_RUN


def test_an_untrustworthy_view_cannot_infer_an_open_session():
    """A stale view must not manufacture an open session out of an old event.

    ``busy`` and ``recent`` are both dropped at the top of the ladder when the view
    cannot be trusted, so this rung has nothing to fire on — which is the point:
    the age of an event measured against a feed that stopped arriving understates
    itself, so "0.5 s ago" would sit on the glass while the truth grew to minutes.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False, "confidence": "exact"},
        trustworthy=False,
        inventory_taken=True,
        registry_known=True,
        connected=True,
        busy="measure_resistance_4wire",
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
    )
    assert state["status"] == STANDBY


def test_activity_cannot_conjure_a_link_that_does_not_exist():
    """Without a session, nothing below the connection rung may be asserted.

    A view holding a last-known activity marker from before a disconnect must not
    use it to claim the agent is talking to hardware.
    """
    state = _slot_state(
        None,
        {"served": True, "present": True, "opened": False},
        trustworthy=True,
        inventory_taken=True,
        registry_known=True,
        connected=False,
        recent={"action": "measure_resistance_4wire", "age_s": 0.5},
    )
    assert state["status"] == NO_LINK
    assert state["linked"] is False


# --------------------------------------------------------------------------
# The renderer's class vocabulary: which card is highlighted, and when
#
# "when an operation is being performed on a device its instrument card should be
# highlighted so we can see which device is being acted on at that moment."
#
# The class assembly is sliced out of the shipped file and run in node, for the
# reason the other JS tests here give: a reimplementation would pass while the
# file the page actually loads was broken.
# --------------------------------------------------------------------------

_CLASSES_FN_START = "function slotClasses(inst, act) {"
_TOUCHED_FN_START = "function touchedNow(inst) {"


def _slice_fn(src: str, start_marker: str) -> str:
    """One brace-balanced function, lifted from the shipped renderer."""
    start = src.find(start_marker)
    assert start >= 0, (
        f"{start_marker!r} moved in {FUI_JS.name}; this test slices on it"
    )
    depth = 0
    for i in range(start, len(src)):
        if src[i] == "{":
            depth += 1
        elif src[i] == "}":
            depth -= 1
            if depth == 0:
                return src[start : i + 1]
    raise AssertionError(f"unbalanced braces slicing {start_marker!r}")


def classes_source() -> str:
    """``slotClasses`` and everything it calls, plus the window constant."""
    src = FUI_JS.read_text(encoding="utf-8")
    # The constant is sliced by name rather than hardcoded: a test that carried its
    # own 2.5 would keep passing after someone retuned the real one.
    const_line = next(
        line for line in src.splitlines() if line.startswith("const TOUCH_WINDOW_S")
    )
    return "\n".join(
        [
            const_line,
            _slice_fn(src, _TOUCHED_FN_START),
            _slice_fn(src, _CLASSES_FN_START),
        ]
    )


def run_classes(inst: dict, act: str = "") -> list[str]:
    harness = "\n".join(
        [
            classes_source(),
            f"console.log(JSON.stringify(slotClasses({json.dumps(inst)},"
            f" {json.dumps(act)})));",
        ]
    )
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout).split()


def js_touch_window() -> float:
    """The renderer's own TOUCH_WINDOW_S, read from the shipped file."""
    src = FUI_JS.read_text(encoding="utf-8")
    line = next(ln for ln in src.splitlines() if ln.startswith("const TOUCH_WINDOW_S"))
    return float(line.split("=")[1].strip().rstrip(";"))


@needs_node
def test_the_card_being_acted_on_right_now_is_highlighted():
    """The request, directly: the operator must be able to see which device is
    being driven at this moment."""
    got = run_classes(
        {"recent": {"age_s": 0.4, "action": "measure_resistance_4wire"}},
        "measure_resistance_4wire · 0.4s ago",
    )
    assert "touched" in got


@needs_node
def test_a_call_caught_in_flight_highlights_the_card():
    """``busy`` is the strongest evidence available — a call is inside the driver
    at this instant — so it highlights regardless of any action age."""
    got = run_classes({"busy": "measure_resistance_4wire", "recent": None}, "measure…")
    assert "touched" in got


@needs_node
def test_the_highlight_lets_go_sooner_than_the_activity_line_does():
    """The two windows are deliberately different, and this is the pair that proves
    it.

    The ``.act`` text may honestly say "6s ago" — that is a true report of the
    past. The highlight may not still be on, because a glow is read across the
    bench as "this one, now". An age inside the view's RECENT_ACTION_S but outside
    the renderer's TOUCH_WINDOW_S must produce a line and no highlight.
    """
    stale_age = js_touch_window() + 1.0
    assert stale_age < RECENT_ACTION_S, (
        "this test needs an age the view still reports but the renderer no longer "
        "highlights; the two windows have been retuned into agreement"
    )
    got = run_classes(
        {"recent": {"age_s": stale_age, "action": "read_voltage"}},
        f"read_voltage · {stale_age}s ago",
    )
    assert "touched" not in got
    # The past-tense line is still drawn — this is not a claim being suppressed,
    # only a highlight expiring.
    assert "recent" in got


@needs_node
def test_an_untouched_card_is_not_highlighted():
    got = run_classes({"recent": None, "busy": None}, "")
    assert "touched" not in got
    assert "working" not in got


@needs_node
def test_a_stale_view_cannot_highlight_a_card():
    """The view withholds ``recent`` entirely when it does not trust itself, so
    there is nothing for the highlight to key off. Asserted here as well as in the
    view because this is where the glow is actually drawn: a highlight surviving a
    dead feed is a card claiming to be in use on a bench nobody can see."""
    got = run_classes({"recent": None, "busy": None, "stale": True}, "")
    assert "touched" not in got
    assert "stale" in got


@needs_node
def test_a_card_with_a_malformed_age_is_not_highlighted():
    """The age crossed a process boundary. Anything non-numeric must read as "no
    claim", never as fresh — the direction that matters, since a truthy-but-junk
    age would light the card permanently."""
    for bad in (None, "0.4", {}, []):
        got = run_classes({"recent": {"age_s": bad, "action": "read"}}, "read")
        assert "touched" not in got, f"age_s={bad!r} lit the card"


@needs_node
def test_an_enrolled_card_wears_the_run_class_for_the_whole_run():
    """Steady state, not a pulse: enrollment lasts minutes, and the class must not
    depend on there being an activity line to accompany it."""
    got = run_classes({"run": "r7", "recent": None, "busy": None}, "")
    assert "inrun" in got
    # And with no traffic at all, it is emphatically not claiming to be mid-call.
    assert "touched" not in got
    assert "working" not in got


@needs_node
def test_a_card_can_be_in_a_run_and_being_driven_at_once():
    """The two mechanisms are orthogonal and both must show. This is the state a
    run actually spends its time in: enrolled throughout, driven in bursts."""
    got = run_classes(
        {"run": "r7", "recent": {"age_s": 0.2, "action": "read_voltage"}},
        "read_voltage · 0.2s ago",
    )
    assert "inrun" in got
    assert "touched" in got


@needs_node
def test_a_card_with_no_run_does_not_wear_the_run_class():
    got = run_classes({"run": None, "recent": None}, "")
    assert "inrun" not in got


@needs_node
def test_an_armed_card_being_driven_still_reads_as_armed():
    """A live output must never be repainted as routine. Both classes are present
    and the CSS pins the colour red for the pair; this asserts the renderer at
    least hands both over, so the stylesheet's guard has something to act on."""
    got = run_classes(
        {"armed": True, "recent": {"age_s": 0.2, "action": "read_current"}},
        "read_current · 0.2s ago",
    )
    assert "armed" in got
    assert "touched" in got


def test_the_stylesheet_pins_red_for_an_armed_card_that_is_being_driven():
    """The one place source order could silently break a safety property.

    ``.slot.touched`` sits after ``.slot.armed`` in the file, and both have the
    same specificity, so without an explicit pair rule the cascade would hand the
    border of an armed-and-being-measured slot to the cyan highlight. Asserted
    against the shipped stylesheet rather than trusted to review, because the
    failure mode is invisible until an output is live.
    """
    css = FUI_CSS.read_text(encoding="utf-8")
    assert ".slot.armed.touched" in css, (
        "an armed slot that is also being driven has no pinned colour; "
        ".slot.touched later in the file will repaint a live output cyan"
    )
    block = css.split(".slot.armed.touched", 1)[1]
    block = block[: block.find("}")]
    assert "--red" in block, "the armed+touched pair must re-assert red, not cyan"


# The sequence row's class vocabulary, and the DUT label
#
# Both are decision tables sliced out of the shipped file for the same reason
# ``slotClasses`` is: a reimplementation here would pass while the file the page
# actually loads was broken.
# --------------------------------------------------------------------------

_STAGE_FN_START = "function stageClasses(s) {"
_DUT_FN_START = "function dutLabel(v) {"


def _run_js(fn_start: str, call: str):
    """One sliced function from the renderer, invoked in node."""
    src = FUI_JS.read_text(encoding="utf-8")
    harness = "\n".join(
        [
            _slice_fn(src, fn_start),
            f"console.log(JSON.stringify({call}));",
        ]
    )
    out = subprocess.run(
        [NODE, "--input-type=module", "-e", harness],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert out.returncode == 0, f"node failed:\n{out.stderr}"
    return json.loads(out.stdout)


def run_stage_classes(stage: dict) -> list[str]:
    return _run_js(
        _STAGE_FN_START, f"stageClasses({json.dumps(stage)})"
    ).split()


def run_dut_label(view: dict) -> str:
    return _run_js(_DUT_FN_START, f"dutLabel({json.dumps(view)})")


@needs_node
def test_the_stage_the_run_is_in_is_the_one_that_pulses():
    got = run_stage_classes({"name": "EXECUTE", "active": True, "done": False})
    assert "active" in got


@needs_node
def test_a_stage_already_passed_reads_as_traversed_not_current():
    """The two must be distinguishable, or the row is five lit boxes saying
    nothing about where the run has got to."""
    got = run_stage_classes({"name": "INIT", "active": False, "done": True})
    assert "done" in got
    assert "active" not in got


@needs_node
def test_a_stage_not_yet_reached_wears_neither_class():
    got = run_stage_classes({"name": "ANALYZE", "active": False, "done": False})
    assert got == ["stage-node"]


@needs_node
def test_active_wins_over_done_on_the_same_node():
    """Reachable whenever the builder marks the current node done as well, and the
    two classes animate differently. Only one can win, and it must be the one that
    says "now" — a node showing where the run *is* must not be dimmed to the
    colour that means "already been here"."""
    got = run_stage_classes({"name": "EXECUTE", "active": True, "done": True})
    assert "active" in got
    assert "done" not in got


@needs_node
def test_the_dut_panel_names_what_the_run_is_testing_on():
    """The panel has been titled DEVICE UNDER TEST since it was written without
    ever being told what the device under test was."""
    got = run_dut_label(
        {"connected": True, "dut_known": True, "dut": "room-temp-sensor"}
    )
    assert got == "room-temp-sensor"


@needs_node
def test_a_run_that_declared_no_dut_says_so_rather_than_inventing_one():
    """``RunSpec.dut`` defaults to ``""``, so "the author did not say" is a real
    and common state. It must be legible as such: a blank would read as a display
    fault, and a placeholder would read as a DUT that exists."""
    got = run_dut_label({"connected": True, "dut_known": True, "dut": ""})
    assert got == "UNSPECIFIED"


@needs_node
def test_no_run_is_distinct_from_a_run_that_named_nothing():
    """The two are different facts and the operator acts differently on them.
    Collapsing them would let an idle bench look like a misconfigured run."""
    assert run_dut_label({"connected": True, "dut_known": False, "dut": ""}) == "NO RUN"


@needs_node
def test_an_unreachable_bench_claims_no_dut_at_all():
    """Nothing on this panel may outlive the link. Without a bench the DUT is not
    unspecified — it is unknown, which is what NO LINK says everywhere else."""
    got = run_dut_label({"connected": False, "dut_known": True, "dut": "cr2032-cell"})
    assert got == "NO LINK"

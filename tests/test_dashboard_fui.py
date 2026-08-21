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
    INSTRUMENTS,
    NO_LINK,
    NOT_SERVED,
    STAGES,
    STANDBY,
    UNDETERMINED,
    UNSCANNED,
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


def test_a_reported_instrument_shows_the_state_model_s_own_label(live):
    """The FUI must not invent a status vocabulary that disagrees with the model."""
    view = view_of(live)
    arc = slot(view, "otii_arc")
    assert arc["linked"]
    assert arc["status"] == "IDLE"
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
    """
    live.apply_registry([{"key": "siglent_sdm4065a", "open": True}])
    live.apply_inventory(
        {"devices": [{"device_key": "siglent_sdm4065a", "confidence": "exact"}]}
    )
    live.apply_status(workers_payload(workers={}), now=101.0)
    dmm = slot(view_of(live), "siglent_sdm4065a")
    assert dmm["status"] == STANDBY
    assert dmm["busy"] is None
    assert dmm["queued"] == 0


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
    assert dmm["status"] == "IDLE", "a bad workers table cost the rail its arm state"


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
    assert arc["status"] == "IDLE"


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


def test_a_running_run_lights_exactly_one_stage(live):
    live.apply_event({"kind": "run_started", "run_id": "r1"}, now=101.0)
    active = [s["name"] for s in view_of(live)["stages"] if s["active"]]
    assert len(active) == 1, active


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
    assert slot(body, "otii_arc")["status"] == "IDLE"

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
    INSTRUMENTS,
    NO_LINK,
    STAGES,
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

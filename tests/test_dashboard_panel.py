"""The panel's rendering, without Streamlit installed.

``render()`` takes ``st`` as an argument rather than importing it, so the view
is testable with a recorder. That is not a testing trick for its own sake: it
is what lets CI assert the one thing about this screen that actually matters —
**an untrustworthy view must look untrustworthy** — on a machine with no
display, no board, and no Streamlit wheel.

The assertions are about *what a human standing at the bench would see*, not
about markup. A stale panel that renders its warning in a collapsed expander
would pass a "the string appears somewhere" test and fail a real operator.
"""

from __future__ import annotations

import pytest

from benchctrl.config import DEFAULT_PORT
from benchctrl.dashboards.panel import COLOURS, endpoint_from_env, render
from benchctrl.dashboards.state import BenchStatus
from tests.test_dashboard_state import WELCOME, armed, idle, status_payload


class FakeExpander:
    def __init__(self, recorder, label):
        self.recorder = recorder
        self.label = label

    def __enter__(self):
        self.recorder.in_expander = True
        return self.recorder

    def __exit__(self, *exc):
        self.recorder.in_expander = False


class FakeSt:
    """Records what was drawn, and whether it was hidden inside an expander."""

    def __init__(self):
        self.calls: list[tuple[str, str]] = []
        self.in_expander = False

    def _record(self, kind, text):
        self.calls.append((kind, str(text)))

    def markdown(self, text, **kw):
        self._record("markdown", text)

    def warning(self, text, **kw):
        self._record("warning", text)

    def error(self, text, **kw):
        self._record("error", text)

    def write(self, text, **kw):
        self._record("write", text)

    def caption(self, text, **kw):
        self._record("caption", text)

    def subheader(self, text, **kw):
        self._record("subheader", text)

    def expander(self, label, **kw):
        self._record("expander", label)
        return FakeExpander(self, label)

    # --- queries used by the assertions ---------------------------------

    def kinds(self, kind):
        return [t for k, t in self.calls if k == kind]


def draw(status: BenchStatus) -> FakeSt:
    st = FakeSt()
    render(st, status.to_dict())
    return st


@pytest.fixture()
def live():
    s = BenchStatus()
    s.apply_connected(WELCOME, now=100.0)
    s.apply_status(status_payload({"otii_arc": idle()}), now=100.0)
    return s


# --------------------------------------------------------------------------
# The headline is the screen
# --------------------------------------------------------------------------


def test_the_headline_is_drawn_large_and_coloured(live):
    st = draw(live)
    banner = st.kinds("markdown")[0]
    assert "IDLE" in banner
    assert COLOURS["info"] in banner
    assert "font-size:5rem" in banner, "the headline is not the biggest thing on screen"


def test_an_armed_bench_is_drawn_in_the_alarm_colour(live):
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    st = draw(live)
    assert COLOURS["alarm"] in st.kinds("markdown")[0]
    assert "ARMED" in st.kinds("markdown")[0]


def test_an_unsafe_output_is_drawn_in_the_critical_colour(live):
    live.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=101.0)
    st = draw(live)
    assert COLOURS["critical"] in st.kinds("markdown")[0]
    assert "UNSAFE" in st.kinds("markdown")[0]


# --------------------------------------------------------------------------
# Staleness has to be visible, not buried
# --------------------------------------------------------------------------


def test_a_stale_view_shows_a_warning_outside_any_expander(live):
    """The whole point. A silently-stale bench panel is the failure mode."""
    live.apply_event({"kind": "events_dropped", "count": 5}, now=101.0)
    st = draw(live)
    warnings = st.kinds("warning")
    assert warnings, "a stale view drew no warning at all"
    assert "5 event(s) dropped" in warnings[0]
    # It must not be tucked into the collapsed health expander.
    order = [k for k, _ in st.calls]
    assert order.index("warning") < order.index("expander")


def test_a_trustworthy_view_draws_no_warning(live):
    """The complement: crying wolf every frame teaches the operator to ignore it."""
    st = draw(live)
    assert st.kinds("warning") == []


def test_the_first_frame_is_not_drawn_as_a_warning():
    """What the board actually showed on boot, at the render layer.

    The startup frame gets a caption, not a warning banner: a kiosk that opens
    every boot with a yellow alert trains the operator to ignore yellow alerts,
    which is expensive on the one boot where something is genuinely wrong.
    """
    st = draw(BenchStatus())
    assert "STARTING" in st.kinds("markdown")[0]
    assert COLOURS["info"] in st.kinds("markdown")[0]
    assert st.kinds("warning") == [], "the startup frame drew an alarm banner"
    assert any("connecting" in c for c in st.kinds("caption")), st.kinds("caption")


def test_the_connecting_caption_never_appears_beside_a_real_warning():
    """Reachable, and the one way the calm caption could reassure wrongly.

    A ``safety_failed`` event arrives on the client's rx thread, which can beat
    the feed's first status poll — so the model is still "starting" while
    holding an unsafe latch. Drawing both would put "connecting to the agent…"
    directly beneath an UNSAFE banner, which reads as "hang on, it's fine".
    """
    s = BenchStatus()
    s.apply_event({"kind": "safety_failed", "device": "otii_arc"}, now=100.0)
    assert s.starting, "the premise of this test stopped holding"
    st = draw(s)
    assert "UNSAFE" in st.kinds("markdown")[0]
    assert st.kinds("warning"), "the unsafe reason was not drawn"
    assert not any("connecting" in c for c in st.kinds("caption")), (
        "the startup caption was drawn alongside a real warning"
    )


def test_a_failed_connection_is_still_drawn_as_a_warning():
    """The complement, at the render layer: a real failure keeps its banner."""
    s = BenchStatus()
    s.apply_disconnected("cannot reach the agent: [Errno 111] refused")
    st = draw(s)
    assert "NO AGENT" in st.kinds("markdown")[0]
    assert COLOURS["warn"] in st.kinds("markdown")[0]
    warnings = st.kinds("warning")
    assert warnings, "an unreachable agent drew no warning"
    assert any("Errno 111" in w for w in warnings), warnings
    assert not any("connecting" in c for c in st.kinds("caption"))


def test_a_disconnected_panel_says_so(live):
    live.apply_disconnected("the agent closed the connection")
    st = draw(live)
    assert "NO AGENT" in st.kinds("markdown")[0]
    assert any("closed the connection" in w for w in st.kinds("warning"))


def test_an_armed_bench_still_shows_its_devices_when_stale(live):
    """Staleness demotes the headline; it must not hide the arm state."""
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    live.apply_event({"kind": "events_dropped", "count": 1}, now=102.0)
    st = draw(live)
    assert "STALE" in st.kinds("markdown")[0]
    assert any("otii_arc" in e for e in st.kinds("error")), (
        "the armed banner disappeared once the view went stale"
    )


def test_unconfirmed_device_state_is_labelled_as_such(live):
    """An inferred arm must not look like a confirmed reading."""
    live.apply_event({"kind": "device_armed", "device": "otii_arc"}, now=101.0)
    st = draw(live)
    written = st.kinds("write")
    assert any("unconfirmed" in w for w in written), written


def test_confirmed_device_state_is_not_labelled_unconfirmed(live):
    live.apply_status(status_payload({"otii_arc": armed()}), now=101.0)
    st = draw(live)
    assert not any("unconfirmed" in w for w in st.kinds("write"))


def test_a_bench_with_no_devices_says_so_rather_than_rendering_nothing(live):
    live.apply_status(status_payload({}), now=101.0)
    st = draw(live)
    assert any("no devices" in c for c in st.kinds("caption"))


def test_runs_are_only_shown_when_there_are_any(live):
    st = draw(live)
    assert "Runs" not in st.kinds("subheader")
    live.apply_event({"kind": "run_started", "run_id": "r1"}, now=101.0)
    st = draw(live)
    assert "Runs" in st.kinds("subheader")
    assert any("r1" in w for w in st.kinds("write"))


def test_every_reachable_headline_has_a_colour(live):
    """A headline with no colour would fall back to the wrong severity.

    Drives the *real* model into each state and reads the severity it reports,
    rather than restating the mapping — a copy of the table here would agree
    with itself while disagreeing with the code.

    ``render`` uses ``COLOURS.get(..., info)``, so a missing entry degrades a
    critical banner to calm teal instead of crashing. That is the failure this
    catches: silent, and on the wrong side.
    """
    reached = {}

    empty = BenchStatus()
    reached[empty.headline] = empty.severity  # STARTING

    gone = BenchStatus()
    gone.apply_disconnected("refused")
    reached[gone.headline] = gone.severity  # NO AGENT

    reached[live.headline] = live.severity  # IDLE

    rec = BenchStatus()
    rec.apply_connected(WELCOME, now=100.0)
    rec.apply_status(status_payload({"a": idle(recording=True)}), now=100.0)
    reached[rec.headline] = rec.severity  # RECORDING

    arm = BenchStatus()
    arm.apply_connected(WELCOME, now=100.0)
    arm.apply_status(status_payload({"a": armed()}), now=100.0)
    reached[arm.headline] = arm.severity  # ARMED

    stale = BenchStatus()
    stale.apply_connected(WELCOME, now=100.0)
    stale.apply_status(status_payload({"a": idle()}), now=100.0)
    stale.apply_event({"kind": "events_dropped", "count": 1}, now=101.0)
    reached[stale.headline] = stale.severity  # STALE

    unsafe = BenchStatus()
    unsafe.apply_connected(WELCOME, now=100.0)
    unsafe.apply_event({"kind": "safety_failed", "device": "a"}, now=101.0)
    reached[unsafe.headline] = unsafe.severity  # UNSAFE

    assert set(reached) == {
        "STARTING",
        "NO AGENT",
        "IDLE",
        "RECORDING",
        "ARMED",
        "STALE",
        "UNSAFE",
    }, f"a headline state became unreachable: {sorted(reached)}"

    for headline, severity in reached.items():
        assert severity in COLOURS, f"{headline} maps to uncoloured {severity!r}"


# --------------------------------------------------------------------------
# Configuration
# --------------------------------------------------------------------------


def test_the_endpoint_defaults_to_the_local_agent():
    """The case that matters: the panel runs on the board beside the agent."""
    ep = endpoint_from_env({})
    assert ep.host == "127.0.0.1"
    assert ep.port == DEFAULT_PORT


def test_the_endpoint_is_configurable_from_the_environment():
    """Streamlit owns argv, so configuration has to come through the env."""
    ep = endpoint_from_env(
        {
            "BENCHCTRL_DASHBOARD_HOST": "10.0.0.5",
            "BENCHCTRL_DASHBOARD_PORT": "9999",
            "BENCHCTRL_TOKEN": "sekrit",
        }
    )
    assert (ep.host, ep.port, ep.token) == ("10.0.0.5", 9999, "sekrit")

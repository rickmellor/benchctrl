"""Streamlit view over :py:class:`~benchctrl.dashboards.feed.AgentFeed`.

Run it via the launcher rather than directly::

    benchctrl-dashboard            # deploy/benchctrl-dashboard on the board
    streamlit run -m benchctrl.dashboards.panel

This module is deliberately thin. Every decision about *what is true* lives in
:py:mod:`benchctrl.dashboards.state`, which has no Streamlit import and is
tested without one; this file only decides how to draw it. That split is why
the interesting behaviour — staleness, the unsafe latch, pessimistic arming —
is covered by unit tests rather than by squinting at a screen.

Streamlit reruns this script top to bottom on a timer. The feed is cached in
``st.session_state`` so a rerun reads a snapshot instead of opening a new
socket; otherwise the agent would see a connection storm from the one client
least entitled to cause one.

Nothing here can command the bench: the feed holds an observer session, so the
protocol refuses anything that is not read-only. The e-stop, when the
touchscreen arrives, will be the single deliberate exception — see
``docs/dashboard.md``.
"""

from __future__ import annotations

import os
from typing import Optional

from benchctrl.config import DEFAULT_PORT, EndpointConfig
from benchctrl.dashboards.feed import AgentFeed

#: Refresh cadence. 2 s is comfortably faster than a human notices and far
#: slower than the event path, which pushes transitions the moment they happen.
REFRESH_MS = 2000

#: Colour per severity. High contrast because this is read from across a bench
#: under fluorescent light, not from a desk.
COLOURS = {
    "critical": "#c1121f",
    "alarm": "#e07a00",
    "warn": "#b58900",
    "info": "#2a9d8f",
}


def endpoint_from_env(env: Optional[dict] = None) -> EndpointConfig:
    """Where to find the agent.

    Reads the environment rather than taking CLI arguments because Streamlit
    owns ``argv``. Falls back to the local agent's default port, which is the
    case that matters: the panel almost always runs on the same board.
    """
    env = dict(os.environ if env is None else env)
    return EndpointConfig(
        host=env.get("BENCHCTRL_DASHBOARD_HOST", "127.0.0.1"),
        port=int(env.get("BENCHCTRL_DASHBOARD_PORT", DEFAULT_PORT)),
        token=env.get("BENCHCTRL_TOKEN", ""),
    )


def get_feed(st) -> AgentFeed:
    """One feed per browser session, surviving Streamlit's reruns."""
    feed = st.session_state.get("benchctrl_feed")
    if feed is None:
        feed = AgentFeed(endpoint_from_env()).start()
        st.session_state["benchctrl_feed"] = feed
    return feed


def render(st, snap: dict) -> None:
    """Draw one frame from a :py:meth:`AgentFeed.snapshot` dict."""
    colour = COLOURS.get(snap["severity"], COLOURS["info"])

    st.markdown(
        f"<div style='background:{colour};color:white;padding:0.4em 0.8em;"
        f"border-radius:8px;text-align:center;'>"
        f"<span style='font-size:5rem;font-weight:700;line-height:1;'>"
        f"{snap['headline']}</span></div>",
        unsafe_allow_html=True,
    )

    # The stale banner is not a footnote. If the view is not current, that is
    # the second-most important thing on the screen.
    if snap["stale_reason"]:
        st.warning(f"⚠ {snap['stale_reason']}", icon="⚠️")

    armed = snap["armed"]
    if armed:
        st.error(f"Armed: {', '.join(armed)}", icon="⚡")

    st.subheader("Instruments")
    devices = snap["devices"]
    if not devices:
        st.caption("no devices reported")
    else:
        for key, dev in sorted(devices.items()):
            suffix = "  ·  unconfirmed" if dev["inferred"] else ""
            st.write(f"**{key}** — {dev['label']}{suffix}")

    runs = snap["runs"]
    if runs:
        st.subheader("Runs")
        for run_id, state in sorted(runs.items()):
            st.write(f"`{run_id}` — {state}")

    with st.expander("Feed health", expanded=False):
        st.write(
            {
                "connected": snap["connected"],
                "trustworthy": snap["trustworthy"],
                "dropped_events": snap["dropped_events"],
                "reconnects": snap.get("reconnects", 0),
            }
        )


def main() -> None:  # pragma: no cover - exercised on the board, not in CI
    import streamlit as st

    st.set_page_config(
        page_title="benchctrl", layout="wide", initial_sidebar_state="collapsed"
    )
    # Hide Streamlit's chrome: on a kiosk panel with no keyboard, a "Deploy"
    # button and a hamburger menu are only ways to get lost.
    st.markdown(
        "<style>#MainMenu,header,footer{visibility:hidden;}"
        ".block-container{padding-top:1rem;}</style>",
        unsafe_allow_html=True,
    )

    feed = get_feed(st)

    # st.fragment re-runs just this function on a timer, leaving the feed and
    # the rest of the page alone. Preferred over the third-party autorefresh
    # helper: one less wheel to get onto an aarch64 board, and it does not
    # re-run the whole script (which would rebuild the feed's session state).
    if hasattr(st, "fragment"):

        @st.fragment(run_every=REFRESH_MS / 1000.0)
        def _panel() -> None:
            render(st, feed.snapshot())

        _panel()
    else:
        # Old Streamlit. Draw once and say the view is frozen rather than
        # letting a static screen pass for a live one — a bench display that
        # silently stops updating is the exact failure this design is about.
        render(st, feed.snapshot())
        st.error(
            "This Streamlit is too old for st.fragment, so the view above is a "
            "one-shot snapshot and will NOT update. Upgrade Streamlit.",
            icon="⚠️",
        )


if __name__ == "__main__":  # pragma: no cover
    main()

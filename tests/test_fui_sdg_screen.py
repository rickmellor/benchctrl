"""The SDG1032X · SCREEN pane: the generator's own display in the DMM quadrant.

Two rules, the same ones the camera relay is held to (see
:py:mod:`tests.test_fui_vision`), plus one of its own:

- The FUI stays read-only. The screen is a *read* the feed makes inside its
  observer session — a claim-free ``device.read`` of ``read_screen`` — and the
  HTTP handler only ever serves what the feed already holds. Nothing here can
  arm, claim, or configure.
- A picture is current or it is absent. A grab that fails clears the last
  bitmap rather than leaving it up as if it were live, and the handler answers
  404 rather than a stale image.
- **The instrument is read only while watched.** Each ``/sdg/screen`` hit arms
  the poll for :py:data:`SCREEN_WATCH_S`; with nobody fetching, the feed does
  not touch the generator at all. That is what keeps an idle kiosk from
  costing the generator's worker 0.5 s every 2 s forever.
"""

from __future__ import annotations

import http.client
import json
import pathlib
import struct
import time

import pytest

from benchctrl.config import EndpointConfig
from benchctrl.dashboards import feed as feed_mod
from benchctrl.dashboards.feed import SCREEN_DEVICE, SCREEN_POLL_S, SCREEN_WATCH_S, AgentFeed
from benchctrl.dashboards.fui import server as fui_server
from benchctrl.dashboards.fui.server import FuiServer
from benchctrl.dashboards.fui.view import build_view

ENDPOINT = EndpointConfig(host="127.0.0.1", port=1, token="t")

WELCOME = {
    "agent": "benchctrl-agent",
    "observer": True,
    "heartbeat_s": 5.0,
    "deadman_s": 15.0,
    "devices": [{"key": SCREEN_DEVICE, "open": True}, {"key": "otii_arc", "open": False}],
}

STATUS = {
    "safety": {
        "armed": [],
        "seconds_since_contact": 0.1,
        "deadman_s": 15.0,
        "devices": {},
        "trips": [],
    }
}


def _bmp(width: int = 4, height: int = 2) -> bytes:
    """A real, tiny 24-bit BMP: what the driver's ``read_screen`` returns, at
    a size a test can compare byte for byte."""
    row = width * 3
    padded = (row + 3) & ~3
    pixels = b"".join(b"\x00\x80\xff" * width + b"\x00" * (padded - row) for _ in range(height))
    size = 14 + 40 + len(pixels)
    header = b"BM" + struct.pack("<IHHI", size, 0, 0, 54)
    dib = struct.pack("<IiiHHIIiiII", 40, width, height, 1, 24, 0, len(pixels), 2835, 2835, 0, 0)
    return header + dib + pixels


class FakeClient:
    """The observer session, as the feed sees it, with ``device.read`` counted."""

    def __init__(self, *, screen=None, screen_raises=None):
        self.welcome = dict(WELCOME)
        self.is_connected = True
        self.on_event = None
        self.calls: list[tuple[str, dict]] = []
        self._screen = screen if screen is not None else _bmp()
        self._screen_raises = screen_raises

    def status(self):
        return STATUS

    def discover(self):
        return {"devices": []}

    def call(self, method, params=None):
        self.calls.append((method, dict(params or {})))
        if self._screen_raises is not None:
            raise self._screen_raises
        return self._screen

    def close(self):
        self.is_connected = False


def _served_feed(client: FakeClient, **kwargs) -> AgentFeed:
    """A feed that knows the generator is served, without running its thread:
    the poll step is driven by hand so the cadence is the test's, not a race."""
    feed = AgentFeed(ENDPOINT, connect=lambda: client, **kwargs)
    feed.status.apply_connected(client.welcome)
    feed.status.apply_status(client.status())
    return feed


def _get(fui: FuiServer, path: str):
    host, port = fui.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read()
    conn.close()
    return resp.status, dict(resp.getheaders()), data


@pytest.fixture
def stack():
    client = FakeClient()
    feed = _served_feed(client)
    # The feed thread is not started (FuiServer.start starts it; we stop it
    # again at once) so the tests own every grab. stop() with no thread is a
    # no-op, which is exactly what we want.
    fui = FuiServer(ENDPOINT, host="127.0.0.1", port=0, feed=feed)
    fui._httpd.feed = feed  # type: ignore[attr-defined]
    fui.start()
    feed.stop()
    try:
        yield client, feed, fui
    finally:
        fui.stop()


# --- the HTTP surface ----------------------------------------------------


def test_no_screen_is_a_404_that_arms_the_poll(stack):
    _, feed, fui = stack
    assert feed._screen_wanted_until == 0.0, "premise: nobody has asked yet"
    status, headers, data = _get(fui, "/sdg/screen")
    assert status == 404
    assert headers["Content-Type"] == "application/json"
    assert json.loads(data) == {"error": "no screen"}
    assert feed._screen_wanted_until > time.monotonic(), "the hit must arm the poll"
    assert feed._screen_wanted_until <= time.monotonic() + SCREEN_WATCH_S


def test_the_last_grab_is_served_as_a_bmp_and_never_cached(stack):
    client, feed, fui = stack
    feed.want_screen()
    assert feed._poll_screen(client) == SCREEN_POLL_S
    status, headers, data = _get(fui, "/sdg/screen?t=123")
    assert status == 200
    assert headers["Content-Type"] == "image/bmp"
    assert headers["Cache-Control"] == "no-store"
    assert data == _bmp()
    assert float(headers["X-Screen-Age"]) < 5.0


def test_the_view_carries_the_screen_state(stack):
    client, feed, fui = stack
    status, _, data = _get(fui, "/api/view")
    assert status == 200
    view = json.loads(data)
    assert view["sdg_screen"] == {"present": False, "age_s": None, "bytes": None}

    feed.want_screen()
    feed._poll_screen(client)
    view = json.loads(_get(fui, "/api/view")[2])
    screen = view["sdg_screen"]
    assert screen["present"] is True
    assert screen["bytes"] == len(_bmp())
    assert isinstance(screen["age_s"], float) and 0.0 <= screen["age_s"] < 5.0


def test_the_view_degrades_when_the_snapshot_has_no_screen_field():
    """An older feed, or a fixture predating the field: three keys, all dark."""
    feed = _served_feed(FakeClient())
    snap = feed.snapshot()
    del snap["sdg_screen"]
    assert build_view(snap, feed.bench)["sdg_screen"] == {
        "present": False,
        "age_s": None,
        "bytes": None,
    }


# --- the feed's poll: only while watched, never a claim ----------------------


def test_the_feed_does_not_read_the_generator_when_nobody_is_watching():
    client = FakeClient()
    feed = _served_feed(client)
    for _ in range(3):
        assert feed._poll_screen(client) is None
    assert client.calls == [], "no /sdg/screen hit, so the instrument must not be read"
    assert feed.latest_screen is None


def test_the_feed_reads_the_generator_while_watched_and_stops_afterwards(monkeypatch):
    client = FakeClient()
    feed = _served_feed(client)
    clock = [1000.0]
    monkeypatch.setattr(feed_mod.time, "monotonic", lambda: clock[0])

    feed.want_screen()
    assert feed._poll_screen(client) == SCREEN_POLL_S
    assert len(client.calls) == 1
    assert feed.latest_screen is not None and feed.latest_screen[0] == _bmp()

    # Inside the cadence: no second grab, and the step says how long to wait.
    clock[0] += 0.5
    due = feed._poll_screen(client)
    assert due == pytest.approx(SCREEN_POLL_S - 0.5)
    assert len(client.calls) == 1

    clock[0] += SCREEN_POLL_S
    feed._poll_screen(client)
    assert len(client.calls) == 2

    # The watch window lapses with no new hit: the grabs stop and the picture
    # goes with them, so the relay cannot serve a bitmap nobody refreshed.
    clock[0] += SCREEN_WATCH_S
    assert feed._poll_screen(client) is None
    assert len(client.calls) == 2
    assert feed.latest_screen is None


def test_the_grab_is_a_claim_free_read_of_read_screen():
    """The exact wire call, pinned: ``device.read`` of ``read_screen`` on the
    generator, no ``agent.open``, no ``agent.claim``. ``read_screen`` is not a
    mutator, so this passes the agent's writer check without a claim — and a
    dashboard holding the writer claim on an instrument a run is driving would
    be the bug this test exists to prevent."""
    client = FakeClient()
    feed = _served_feed(client)
    feed.want_screen()
    feed._poll_screen(client)
    assert client.calls == [
        (
            "device.read",
            {
                "device": SCREEN_DEVICE,
                "method": "read_screen",
                "args": [],
                "kwargs": {},
                "want_props": False,
            },
        )
    ]
    assert not any(m in ("agent.open", "agent.claim") for m, _ in client.calls)


def test_the_feed_does_not_read_a_generator_the_agent_does_not_serve():
    client = FakeClient()
    client.welcome = {**WELCOME, "devices": [{"key": "otii_arc", "open": False}]}
    feed = _served_feed(client)
    feed.want_screen()
    assert feed._poll_screen(client) is None
    assert client.calls == []


def test_a_failing_grab_clears_the_screen_and_does_not_raise():
    good = FakeClient()
    feed = _served_feed(good)
    feed.want_screen()
    feed._poll_screen(good)
    assert feed.latest_screen is not None

    bad = FakeClient(screen_raises=OSError("SCDP went unanswered"))
    feed._screen_grabbed_at = 0.0  # make the next grab due now
    assert feed._poll_screen(bad) == SCREEN_POLL_S, "a failure keeps the cadence, not the picture"
    assert feed.latest_screen is None
    assert feed.screen_snapshot() == {"present": False, "age_s": None, "bytes": None}


def test_a_grab_that_is_not_bytes_is_refused():
    client = FakeClient(screen="not a bitmap")
    feed = _served_feed(client)
    feed.want_screen()
    feed._poll_screen(client)
    assert feed.latest_screen is None


def test_an_injected_reader_replaces_the_wire_call():
    calls = []

    def reader(client):
        calls.append(client)
        return _bmp(2, 2)

    client = FakeClient()
    feed = _served_feed(client, screen_reader=reader)
    feed.want_screen()
    feed._poll_screen(client)
    assert calls == [client] and client.calls == []
    assert feed.screen_snapshot()["bytes"] == len(_bmp(2, 2))


def test_the_session_loop_grabs_on_its_own_when_watched():
    """End to end through the real loop: arm, and the feed thread reads the
    generator without anyone driving the step by hand."""
    client = FakeClient()
    feed = AgentFeed(ENDPOINT, poll_s=0.05, connect=lambda: client)
    with feed:
        deadline = time.monotonic() + 3.0
        while feed.snapshot()["connected"] is not True and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.calls == [], "connected and idle: the generator is untouched"
        feed.want_screen()
        while not client.calls and time.monotonic() < deadline:
            time.sleep(0.01)
        assert client.calls and client.calls[0][0] == "device.read"
        while feed.latest_screen is None and time.monotonic() < deadline:
            time.sleep(0.01)
        assert feed.snapshot()["sdg_screen"]["present"] is True


# --- the page -----------------------------------------------------------------


def test_the_page_carries_the_screen_image_and_the_title_switch():
    """Same rule as the camera: the image starts hidden with no src, and the
    title is a span the JS can swap. The JS gates on the AWG being *linked*, the
    word the rail reserves for a device the agent holds open."""
    static = pathlib.Path(fui_server.__file__).parent / "static"
    html = (static / "index.html").read_text()
    assert 'id="dmm-title"' in html and "DIGITAL MULTIMETER" in html
    img = html.split('id="sdg-screen"')[1].split(">")[0]
    assert 'class="hidden"' in img and "src=" not in img
    assert 'id="vision-stream"' in html, "the vision pane is untouched"
    js = (static / "fui.js").read_text()
    assert "SDG1032X · SCREEN" in js and "'DIGITAL MULTIMETER'" in js
    assert "/sdg/screen?t=" in js
    assert "i.kind === 'awg'" in js and "awg.linked" in js
    assert "NO SCREEN" in js
    css = (static / "fui.css").read_text()
    assert "#sdg-screen" in css and "object-fit: contain" in css

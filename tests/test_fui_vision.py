"""The VISION · LIVE quadrant: the FUI relays the camera, and only the camera.

The FUI server is read-only by construction (see its module docstring). Adding
a relay to another process is the first time it forwards anything, so the
tests pin the two properties that keep the rule intact: only the two view
paths are relayed (a control path through the FUI would be the sidecar's
unauthenticated surface on a port the kiosk exposes), and a sidecar that is
down produces a clean 503 the page turns into NO FEED — never a hung request,
which on a kiosk is a frozen quadrant with no explanation.

The sidecar under test is the real router with a synthetic camera
(:py:mod:`benchctrl.sim.vision`), so the relay is exercised against the bytes
the production sidecar would send.
"""

from __future__ import annotations

import http.client
import pathlib

import pytest

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.fui import server as fui_server
from benchctrl.dashboards.fui.server import FuiServer
from benchctrl.sim.vision import TINY_JPEG, SimulatedVisionSidecar


class _NoFeed:
    """A feed that never connects: these tests need the HTTP front end only."""

    bench = None

    def start(self) -> None:
        pass

    def stop(self) -> None:
        pass

    def snapshot(self) -> dict:
        return {}


@pytest.fixture
def stack():
    with SimulatedVisionSidecar() as sim:
        fui = FuiServer(
            EndpointConfig(host="127.0.0.1", port=1),
            host="127.0.0.1",
            port=0,
            feed=_NoFeed(),  # type: ignore[arg-type]
            vision_url=sim.url,
        ).start()
        try:
            yield sim, fui
        finally:
            fui.stop()


def _get(fui: FuiServer, path: str, *, read: bool = True):
    host, port = fui.address
    conn = http.client.HTTPConnection(host, port, timeout=5)
    conn.request("GET", path)
    resp = conn.getresponse()
    data = resp.read() if read else resp.fp.read(64)
    conn.close()
    return resp.status, resp.getheader("Content-Type") or "", data


def test_the_still_is_relayed_byte_for_byte(stack):
    sim, fui = stack
    sim.camera.trigger(1)
    status, ctype, data = _get(fui, "/vision/frame.jpg")
    assert status == 200 and ctype == "image/jpeg" and data == TINY_JPEG
    assert ("GET", "/frame.jpg") in sim.request_log


def test_the_stream_is_relayed_as_mjpeg(stack):
    sim, fui = stack
    sim.camera.trigger(1)
    status, ctype, head = _get(fui, "/vision/stream", read=False)
    assert status == 200 and ctype.startswith("multipart/x-mixed-replace")
    assert head.startswith(b"--frame")


def test_nothing_but_the_two_view_paths_is_relayed(stack):
    """A control path reaching the sidecar through the FUI would put the
    unauthenticated surface on a port the kiosk exposes. 404 from the static
    handler, and the sidecar must not have been asked."""
    sim, fui = stack
    before = len(sim.request_log)
    for path in (
        "/vision/status",
        "/vision/capture",
        "/vision/config",
        "/vision/trigger?seq=1",
        "/vision/../status",
        "/status",
    ):
        status, _, _ = _get(fui, path)
        assert status == 404, path
    assert len(sim.request_log) == before, "the sidecar saw a request the FUI should not relay"


def test_a_sidecar_that_is_down_is_a_clean_503():
    fui = FuiServer(
        EndpointConfig(host="127.0.0.1", port=1),
        host="127.0.0.1",
        port=0,
        feed=_NoFeed(),  # type: ignore[arg-type]
        vision_url="http://127.0.0.1:1",
    ).start()
    try:
        status, _, data = _get(fui, "/vision/frame.jpg")
        assert status == 503
        assert b"no vision feed" in data
    finally:
        fui.stop()


def test_the_vision_url_defaults_to_the_bench_boxs_loopback(monkeypatch):
    monkeypatch.delenv("BENCHCTRL_VISION_URL", raising=False)
    fui = FuiServer(
        EndpointConfig(host="127.0.0.1", port=1), host="127.0.0.1", port=0, feed=_NoFeed()
    )  # type: ignore[arg-type]
    assert fui.vision_url == fui_server.DEFAULT_VISION_URL == "http://127.0.0.1:8095"
    monkeypatch.setenv("BENCHCTRL_VISION_URL", "http://127.0.0.1:9999")
    fui2 = FuiServer(
        EndpointConfig(host="127.0.0.1", port=1), host="127.0.0.1", port=0, feed=_NoFeed()
    )  # type: ignore[arg-type]
    assert fui2.vision_url == "http://127.0.0.1:9999"


def test_the_page_carries_the_vision_quadrant_and_starts_dark():
    """Same rule as every other panel: the markup shows NO LINK / NO FEED until
    the JS proves otherwise, and the image starts hidden so a stalled script
    never leaves a broken-image glyph where a picture should be."""
    static = pathlib.Path(fui_server.__file__).parent / "static"
    html = (static / "index.html").read_text()
    assert 'id="vision-panel"' in html and "VISION · LIVE" in html
    assert "SUPPLY / LOAD" not in html, "the scope quadrant was replaced, not duplicated"
    verdict = html.split('id="vision-verdict"')[1].split("</span>")[0]
    assert "NO LINK" in verdict
    img = html.split('id="vision-stream"')[1].split(">")[0]
    assert 'class="hidden"' in img and "src=" not in img
    note = html.split('id="vision-note"')[1].split("</div>")[0]
    assert "NO FEED" in note
    js = (static / "fui.js").read_text()
    assert "/vision/stream" in js and "psu-verdict" not in js
    assert "i.key === 'bench_vision'" in js

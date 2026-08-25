"""Mains state from the contactor to the browser, with nothing hand-built.

Every other test of this feature holds one piece still. ``test_dashboard_state``
feeds :py:class:`BenchView` events written by hand; ``test_dashboard_fui``
renders snapshots written by hand; ``test_run_outlets`` drives the engine with a
local driver and no display at all. Each is the right shape for what it checks,
and together they leave one thing unproven: that the events the *agent actually
publishes* are the events the *view actually reads*.

That gap is where this feature's realistic failure lives. The mains panel is fed
by two publishers in two modules that share no code — ``server._mains_sweep``
and ``RunEngine._switch_outlets`` — and consumed by a third
(``dashboards/state.py``) which deliberately imports nothing from
:py:mod:`benchctrl.agent`, so a renamed key is a compile-clean silent blanking.
The dashboard cannot poll to recover from one either: it is an observer session,
``device.call`` is not an observer method, and reading a PDU means write-grade
access to the one device that switches mains. Push is the only channel, so a
key that nobody sends is a panel that says nothing forever.

So the stack here is complete — production driver over a pty, device worker,
agent, HMAC wire, real observer session, real ``AgentFeed``, real
``build_view``, fetched over real HTTP from the URL the browser fetches. Only
the silicon is fake.

Two conventions worth keeping if these are edited:

- **Assert against the display, never against the publisher.** The point is the
  round trip; reaching into ``feed.bench`` would skip the half that breaks.
- **Poll for a state rather than sleeping for a duration.** The sweep runs on a
  heartbeat multiple on its own thread, so every arrival time here is genuinely
  nondeterministic.
"""

from __future__ import annotations

import json
import time
import urllib.request

import pytest

from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.spec import Phase, RunSpec, Safety, Sampling
from benchctrl.agent.server import AgentServer, BenchAgent
from benchctrl.config import EndpointConfig
from benchctrl.dashboards.fui.server import FuiServer
from benchctrl.net.client import RemoteClient

TOKEN = "test-token-do-not-use-in-anger"
PDU_KEY = "cyberpower_pdu41002"
ARC_KEY = "otii_arc"
OUTLET = 3

#: Budget for one state to make it from the device to the browser. Generous
#: because the path includes a sweep interval, a worker queue and an HTTP fetch,
#: and a flaky integration test gets muted rather than fixed.
ARRIVAL_BUDGET_S = 25.0


class _Bench:
    """An agent, a display, and the plumbing to interrogate the display."""

    def __init__(self, keys, *, runs_dir, heartbeat_s=0.25):
        from benchctrl.sim.factories import make_pdu41002

        self.registry = DeviceRegistry()
        self.pdu = None
        if PDU_KEY in keys:
            # The *production* driver over a pty, so the agent holds what it
            # would hold over the FTDI cable.
            self.pdu = make_pdu41002(allowed_outlets=(OUTLET,))
            self.registry.register_open(PDU_KEY, self.pdu)
        if ARC_KEY in keys:
            from benchctrl.sim.factories import make_otii_arc

            self.registry.register_open(ARC_KEY, make_otii_arc())

        # A fast heartbeat only to keep the test short: the sweep interval is a
        # multiple of it, and this test would otherwise spend most of its time
        # waiting for a courtesy read.
        # ``runs_dir`` is not optional here even though only one test submits a
        # run: the default is ``$CWD/benchctrl-runs``, so without it a real run
        # writes its bundle into whatever directory pytest happened to start in
        # — which for this repo is the repo.
        self.agent = BenchAgent(
            self.registry,
            token=TOKEN,
            deadman_s=120.0,
            heartbeat_s=heartbeat_s,
            runs_dir=runs_dir,
        )
        self.server = AgentServer(self.agent, host="127.0.0.1", port=0).start()
        self.endpoint = EndpointConfig(
            host="127.0.0.1", port=self.server.port, token=TOKEN, heartbeat_s=1.0
        )
        self.fui = FuiServer(self.endpoint, host="127.0.0.1", port=0).start()
        host, port = self.fui.address
        self.url = f"http://{host}:{port}/api/view"

    def view(self) -> dict:
        with urllib.request.urlopen(self.url, timeout=10) as resp:
            return json.loads(resp.read())

    def mains(self) -> dict:
        return self.view()["mains"]

    def watch(self, pred, what, budget=ARRIVAL_BUDGET_S):
        """Poll the display until ``pred`` holds, keeping every state it showed.

        Returns ``(final, seen)``. ``seen`` matters for the transient states —
        a settle window is over in a second and asserting on the final state
        alone would miss it entirely.
        """
        seen: list[dict] = []
        deadline = time.monotonic() + budget
        while time.monotonic() < deadline:
            block = self.mains()
            if not seen or block != seen[-1]:
                seen.append(block)
            if pred(block):
                return block, seen
            time.sleep(0.1)
        pytest.fail(
            f"the display never showed {what} within {budget}s; "
            f"{len(seen)} distinct states, last={seen[-1] if seen else None}"
        )

    def switch(self, index, on):
        """Switch an outlet the way real callers do — on the device's worker.

        Not a detail. The PDU permits one CLI session across all transports, and
        the sweep runs on its own thread; a direct call from here interleaves two
        commands on one prompt and desyncs the link. Going through the worker is
        what the engine and the sweep both do, and it is the only reason they can
        coexist.
        """
        worker = self.agent.workers.get(PDU_KEY)
        return worker.submit(
            lambda: self.pdu.set_outlet_state(index, on, verify=True),
            label="test_switch",
            timeout=30.0,
        )

    def close(self):
        self.fui.stop()
        self.server.stop()
        self.registry.close_all()


@pytest.fixture()
def bench(tmp_path):
    b = _Bench([PDU_KEY], runs_dir=tmp_path / "runs")
    try:
        yield b
    finally:
        b.close()


@pytest.fixture()
def run_bench(tmp_path):
    b = _Bench([PDU_KEY, ARC_KEY], runs_dir=tmp_path / "runs")
    try:
        yield b
    finally:
        b.close()


def _cycle_spec(*, settle_s=1.0, duration_s=1.0) -> RunSpec:
    """A two-phase power cycle: cut the outlet, then restore it and drive."""
    return RunSpec(
        name="e2e-power-cycle",
        device=ARC_KEY,
        safety=Safety(
            max_voltage_V=4.0,
            max_current_A=0.5,
            max_duration_s=600,
            allowed_outlets=(OUTLET,),
        ),
        sampling=Sampling(
            channels=("mv",), chunk_s=60, metric_period_s=0.2, record=False
        ),
        phases=(
            Phase(
                name="cut",
                mode="idle",
                setpoints={"outlets": {OUTLET: False}},
                duration_s=duration_s,
                settle_s=settle_s,
            ),
            Phase(
                name="boot",
                mode="cv",
                setpoints={"outlets": {OUTLET: True}, "voltage_V": 3.0},
                duration_s=duration_s,
                settle_s=settle_s,
            ),
        ),
    )


# --- the sweep path -------------------------------------------------------


def test_the_agents_own_sweep_fills_the_panel(bench):
    """The whole point: state the agent published, rendered by the shipped view.

    Nothing in this test names an event key or a field, which is exactly why it
    catches what the unit tests cannot — a rename on either side of the wire
    fails here and nowhere else.
    """
    block, _ = bench.watch(lambda m: m["known"], "a mains reading")

    assert block["served"] is True
    assert block["status"] == "MAINS LIVE"
    assert block["device"] == PDU_KEY
    assert block["transport"] == bench.pdu.transport
    assert block["stale"] is False
    assert block["aged_out"] is False

    # All four figures the panel exists to show, each carrying its unit and none
    # of them the "plausible zero" that a blanked reading would look like.
    assert block["voltage"].endswith(" V") and block["voltage"] != "NO LINK"
    assert block["frequency"].endswith(" Hz") and block["frequency"] != "NO LINK"
    assert block["load_A"].endswith(" A") and block["load_A"] != "NO LINK"
    assert block["load_W"].endswith(" W") and block["load_W"] != "NO LINK"

    assert [p["index"] for p in block["outlets"]] == list(range(1, 9))
    assert block["reported"] == 8
    assert block["energised"] == 8
    assert all(p["on"] and p["label"] == "ON" for p in block["outlets"])


def test_the_pdu_never_reaches_the_instrument_rail_over_the_wire(run_bench):
    """The user's directive, asserted against the served bench and not a fixture.

    ``_rail_specs`` appends any key it does not recognise as an unknown
    instrument, so this is the arrangement that broke it: a bench serving a PDU
    *and* a real instrument, seen through the agent that serves both. The Arc
    must be there; the PDU must not, because it is core harness like the board
    itself and the rail's vocabulary (``ARMED``, the arm counter) describes an
    output, not a contactor.
    """
    run_bench.watch(lambda m: m["known"], "a mains reading")

    keys = [slot.get("key") for slot in run_bench.view()["instruments"]]
    assert ARC_KEY in keys
    assert PDU_KEY not in keys


def test_a_switch_on_the_device_reaches_the_display(bench):
    """A contactor moves and the panel follows, with no display-side read.

    The sweep is the only channel here — this bench runs no run engine — so this
    is the test that fails if the sweep stops publishing while everything else
    about the agent still looks healthy.
    """
    bench.watch(lambda m: m["energised"] == 8, "all eight energised")

    bench.switch(OUTLET, False)
    block, _ = bench.watch(
        lambda m: any(p["index"] == OUTLET and not p["on"] for p in m["outlets"]),
        f"outlet {OUTLET} de-energised",
    )
    assert block["energised"] == 7
    assert block["reported"] == 8, "a de-energised outlet is still a reported one"
    assert [p["index"] for p in block["outlets"] if not p["on"]] == [OUTLET]
    assert [p["label"] for p in block["outlets"] if not p["on"]] == ["OFF"]

    bench.switch(OUTLET, True)
    block, _ = bench.watch(
        lambda m: all(p["on"] for p in m["outlets"]), "the outlet restored"
    )
    assert block["energised"] == 8


def test_the_display_is_not_what_opens_the_pdu(tmp_path):
    """A bench that has never opened its PDU says so, rather than opening it.

    The inverse of the test above and the sharper half of the invariant: a sweep
    that used ``registry.get`` would make a *display connecting* the reason the
    bench takes the PDU's single CLI session — the session an operator needs for
    recovery. So this bench registers the PDU as served but never opens it, and
    the panel must resolve to "served, unheard" and stay there.
    """
    from benchctrl.sim.factories import make_pdu41002

    registry = DeviceRegistry()
    opened: list[str] = []

    def opener(**kwargs):
        opened.append(PDU_KEY)
        return make_pdu41002(allowed_outlets=(OUTLET,))

    registry.register(PDU_KEY, opener)
    agent = BenchAgent(
        registry,
        token=TOKEN,
        deadman_s=120.0,
        heartbeat_s=0.25,
        runs_dir=tmp_path / "runs",
    )
    server = AgentServer(agent, host="127.0.0.1", port=0).start()
    endpoint = EndpointConfig(
        host="127.0.0.1", port=server.port, token=TOKEN, heartbeat_s=1.0
    )
    fui = FuiServer(endpoint, host="127.0.0.1", port=0).start()
    fhost, fport = fui.address
    url = f"http://{fhost}:{fport}/api/view"
    try:
        # Long enough for several sweep intervals to come and go.
        deadline = time.monotonic() + 4.0
        while time.monotonic() < deadline:
            time.sleep(0.25)
            assert not opened, "the sweep opened the PDU on a display's behalf"
        with urllib.request.urlopen(url, timeout=10) as resp:
            block = json.loads(resp.read())["mains"]
    finally:
        fui.stop()
        server.stop()
        registry.close_all()

    assert not opened
    # Served but unheard is its own verdict, and deliberately not "NO PDU": one
    # means there is no mains control on this bench, the other means wait.
    assert block["served"] is True
    assert block["known"] is False
    assert block["status"] == "NOT REPORTED"
    assert block["outlets"] == []
    assert block["energised"] == 0


def test_a_bench_with_no_pdu_gets_no_panel(tmp_path):
    """No switched PDU served → the one self-hiding panel hides.

    Worth an integration test rather than only a unit one, because ``served`` is
    derived from the agent's own device table: a stray key in the slot map is
    enough to put an empty MAINS.MGR on a bench that has no mains control, which
    reads as "waiting for a reading" forever.
    """
    b = _Bench([ARC_KEY], runs_dir=tmp_path / "runs")
    try:
        b.watch(lambda m: True, "any view")  # one fetch, to prove it renders
        block = b.mains()
        assert block["served"] is False
        assert block["known"] is False
        assert block["status"] == "NO PDU"
        assert block["outlets"] == []
        assert ARC_KEY in [s.get("key") for s in b.view()["instruments"]]
    finally:
        b.close()


# --- the run path ---------------------------------------------------------


def test_a_run_power_cycling_a_dut_is_visible_on_the_panel(run_bench):
    """A real run's transitions reach the display, including the settle window.

    This is the second publisher, and it shares no code with the first. The
    engine emits ``run_outlet`` events so the panel stays current *between*
    sweeps — without them a power cycle that begins and ends inside one sweep
    interval would be invisible, and the panel would show a run power-cycling a
    DUT with every port reading as it did before the switch.

    ``settling_s`` is asserted from the recorded history rather than the final
    state, because the window closes on its own and is gone by the time the run
    ends.
    """
    run_bench.watch(lambda m: m["known"], "a mains reading")

    with RemoteClient(run_bench.endpoint) as client:
        client.call("agent.claim", {"device": ARC_KEY})
        # Mains switching needs a claim on the PDU's own key as well.
        client.call("agent.claim", {"device": PDU_KEY})
        submitted = client.call("run.submit", {"spec": _cycle_spec().to_dict()})
        run_id = submitted["run_id"]

        block, seen = run_bench.watch(
            lambda m: any(
                p["index"] == OUTLET and not p["on"] for p in m["outlets"]
            ),
            f"the run's cut of outlet {OUTLET}",
        )
        assert block["energised"] == 7
        assert block["transitions"] >= 1
        assert block["last_transition"] == {"outlet": OUTLET, "state": False}

        settling = [s for s in seen if s.get("settling_s") is not None]
        assert settling, "no settle window ever reached the display"
        # More specific about the same instant, so it outranks MAINS LIVE: the
        # samples being taken right now are a DUT booting.
        assert any(s["status"] == "DUT SETTLING" for s in settling), [
            s["status"] for s in settling
        ]

        block, _ = run_bench.watch(
            lambda m: all(p["on"] for p in m["outlets"]) and m["transitions"] >= 2,
            "the run's restore of the outlet",
        )
        assert block["last_transition"] == {"outlet": OUTLET, "state": True}

        deadline = time.monotonic() + 30.0
        status = client.call("run.status", {"run_id": run_id})
        while status.get("running") and time.monotonic() < deadline:
            time.sleep(0.2)
            status = client.call("run.status", {"run_id": run_id})
        assert status["status"] == "complete", status

    # A run never cuts mains on its way out: outlets stay where the last phase
    # put them, which for this spec is all eight energised.
    assert run_bench.mains()["energised"] == 8


def test_a_run_that_switches_mains_without_the_pdu_claim_is_refused(run_bench):
    """The writer gate, checked through the wire that would bypass it.

    ``run.submit`` is the one route to a contactor that does not go through a
    ``set_``-prefixed method, so the prefix-derived mutator gate cannot see it.
    Without the explicit second claim, a session holding only the Arc could
    switch mains — and the refusal has to name the key to claim, or the operator
    cannot act on it.
    """
    with RemoteClient(run_bench.endpoint) as client:
        client.call("agent.claim", {"device": ARC_KEY})
        with pytest.raises(Exception) as excinfo:
            client.call("run.submit", {"spec": _cycle_spec().to_dict()})
    message = str(excinfo.value)
    assert PDU_KEY in message, message
    assert "claim" in message.lower(), message

    # And nothing moved: the refusal happens before the PDU is even opened.
    block, _ = run_bench.watch(lambda m: m["known"], "a mains reading")
    assert block["energised"] == 8


# --- staleness ------------------------------------------------------------


def test_a_silent_sweep_ages_out_while_the_rest_of_the_panel_stays_green(bench):
    """The failure only this panel can detect, reproduced rather than simulated.

    Shutting down the agent's *listener* leaves the established session open, so
    the feed keeps its connection and the sweep simply stops arriving. That is
    the realistic shape of this fault — a held PDU session, a dead sweep thread,
    a device that stopped answering — and it is exactly the case the global
    ``STALE`` clock cannot cover, because that clock is driven by status frames
    the panel has no way to distinguish from healthy ones.

    Hence mains carrying its own age. The port map is *kept* here, dimmed rather
    than dropped: the link is still up and the last thing the bench said is the
    best available guess. What must change is the claim — ``aged_out`` says these
    figures are not current.

    The first transition to ``stale`` arrives within a second, before the 30 s
    budget, because the polled status snapshot stops too; ``aged_out`` is the
    slower, mains-specific verdict and is what this test waits for.
    """
    block, _ = bench.watch(lambda m: m["energised"] == 8, "a full reading")
    assert block["aged_out"] is False
    assert block["age_s"] < 30.0

    bench.server.stop()

    block, _ = bench.watch(
        lambda m: m["aged_out"], "the mains reading to age out", budget=45.0
    )
    assert block["age_s"] > 30.0, block["age_s"]
    # Kept, and that is the point: an aged-out reading is still the best guess,
    # so the figures survive and only their standing changes.
    assert block["known"] is True
    assert len(block["outlets"]) == 8
    assert block["voltage"] != "NO LINK"
    assert block["device"] == PDU_KEY
    assert block["transport"] == bench.pdu.transport


def test_losing_the_session_drops_the_outlet_map_rather_than_ageing_it(bench):
    """A *disconnect* erases the ports; it does not leave them dimmed.

    The other half of the pair above, and the opposite treatment, because the
    two faults differ in what remains true. A silent sweep on a live link still
    has a bench behind it. A dead session does not, and its ports are a claim
    about which contactors are closed that nothing can now correct.

    The rail's choice is the reverse and is right for the rail: a stale ``ARMED``
    over-warns, which is the safe direction. Here it inverts — a stale ``OFF``
    reads as "the DUT is de-powered" to somebody deciding whether to put a hand
    in an enclosure, and no amount of dimming makes that safe. So the map goes,
    while ``device`` and ``transport`` stay: which PDU it was is still true.

    Driven by closing the feed's own client, because that is what an agent
    restart, a dropped link or a killed process all look like from here —
    stopping the listener (the test above) deliberately does *not* do this.
    """
    block, _ = bench.watch(lambda m: m["energised"] == 8, "a full reading")
    assert block["outlets"]

    bench.server.stop()
    # The session survives its listener; ending it is what a real link loss does.
    client = bench.fui.feed._client
    assert client is not None, "the feed had no session to drop"
    client.close()

    block, _ = bench.watch(
        lambda m: not m["outlets"], "the outlet map to be dropped", budget=30.0
    )
    assert block["energised"] == 0
    assert block["reported"] == 0
    assert block["known"] is False
    assert block["status"] == "NOT REPORTED"
    assert block["voltage"] == "NO LINK"
    # Retained, because they are claims about identity rather than state.
    assert block["device"] == PDU_KEY
    assert block["transport"] == bench.pdu.transport

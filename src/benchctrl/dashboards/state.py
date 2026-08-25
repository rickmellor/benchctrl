"""What the panel knows, folded from an event stream.

Deliberately pure: no sockets, no renderer, no clock of its own beyond a
monotonic ``now`` passed in. Everything the display renders is derived here, so
the interesting behaviour — what happens on a gap, on a disconnect, on an event
arriving out of nowhere — is unit-testable without a bench.

The one rule this file exists to enforce
----------------------------------------

**A stale panel must say it is stale.** A bench display showing ``IDLE`` next
to a live output is worse than a blank screen, because it invites someone to
touch the DUT. So every path that could leave the view behind reality sets
:py:attr:`BenchStatus.stale_reason`, and none of them silently keep rendering
the last good frame:

- the observer session dropped (no events can arrive at all)
- the agent told us it shed events (``events_dropped``, the in-band gap notice
  from :py:mod:`benchctrl.agent.eventbus`)
- nothing has arrived for longer than we expect, given the agent's own
  heartbeat interval
- a ``safety_failed`` event, which means the agent could not prove the output
  went off — the display cannot claim safe either

That last one is the honest-reporting rule from CONTRIBUTING (no silent
fallbacks) applied to a screen instead of a return value.

Arming state is tracked *pessimistically*. An event that says a device armed is
believed immediately; disarm is only believed from an authoritative source (a
trip outcome or a full status snapshot). Getting this backwards would mean a
dropped disarm event leaves a scary-but-safe display, while a dropped arm event
leaves a reassuring-but-wrong one. Only one of those errs the right way.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Optional

#: Events whose severity should keep them on screen rather than scroll away.
STICKY_SEVERITIES = frozenset({"alarm", "critical"})

#: The agent's periodic link heartbeat (``LINK_KIND`` in
#: :py:mod:`benchctrl.agent.server`). A literal rather than an import, because
#: this module deliberately depends on nothing in ``agent`` — the dashboard runs
#: against a *remote* agent over a wire protocol, and a panel that could only be
#: built where the agent is importable would not be a remote panel. The event
#: kinds below are matched the same way for the same reason.
#:
#: The heartbeat exists because an idle bench is otherwise indistinguishable from
#: a dead link: with nothing armed and no run, the agent emits no events, and
#: this module cannot tell "quiet" from "gone". It updates liveness and is never
#: logged — see :py:meth:`BenchStatus.apply_event`.
LINK_KIND_NAME = "link"

#: Kind for the bench's periodic presence sweep, matched as a literal for the same
#: reason as the heartbeat above.
#:
#: Presence is the one fact on this panel that used to require the *display* to
#: initiate a ~1.65 s USB scan. The bench now pushes it, so the dashboard learns
#: that an instrument appeared or vanished without ever asking — and an unchanged
#: sweep is treated as liveness, not as news.
PRESENCE_KIND_NAME = "presence"

#: Kind for the bench's periodic mains sweep — the PDU's metering and outlet
#: states. A literal, like every other kind here, for the reason at the top of
#: this module.
#:
#: Pushed rather than polled, and for this device that is not merely a
#: preference: a dashboard is an observer session and cannot call ``device.call``
#: at all, so mains state is knowable to the panel *only* because the bench sends
#: it. See ``agent.server.MAINS_KIND``.
MAINS_KIND_NAME = "mains"

#: Kind for one verified outlet transition inside a run, emitted by
#: :py:class:`~benchctrl.agent.runs.engine.RunEngine`.
#:
#: Folded in addition to the periodic sweep because the two carry different
#: things. The sweep is a sample every ~10 s; a run's power cycle can be shorter
#: than that, and a panel driven by the sweep alone would show mains on, then
#: mains on, having silently missed a DUT losing power in between. This event is
#: emitted *at* the transition and carries the read-back state, so the panel
#: learns about the cycle that the sampler would have stepped over.
RUN_OUTLET_KIND_NAME = "run_outlet"

#: Kind for a run's post-switch settle window. Folded so the panel can say that
#: the bench is deliberately waiting for a DUT to boot, rather than showing a
#: phase that appears stalled.
RUN_OUTLET_SETTLE_KIND_NAME = "run_outlet_settle"

#: Kinds that start a run, and kinds that end one — both spellings of each.
#:
#: ``run_start``/``run_end`` are what
#: :py:class:`~benchctrl.agent.runs.engine.RunEngine` actually emits. The
#: ``run_started``/``run_finished`` pair was this module's own invention and
#: nothing in the agent has ever sent it, which is why :py:attr:`running_runs`
#: read empty through real runs while its tests passed on the invented spelling.
#: Both are accepted because a run's events are persisted and replayed on
#: reconnect, so either vocabulary can arrive from a store.
RUN_STARTED_KINDS = frozenset({"run_start", "run_started"})
RUN_FINISHED_KINDS = frozenset({"run_end", "run_finished"})

#: Kind for a test-sequence stage transition, matched as a literal like the rest.
#:
#: The bench emits one of these when a run moves between INIT/PREPARE/EXECUTE/
#: ANALYZE/DONE. It exists so the sequence display *reports* a stage instead of
#: deriving one from run status, which could only ever light one node and left two
#: others unreachable.
RUN_STAGE_KIND_NAME = "run_stage"

#: Every run lifecycle kind this model folds. ``run_error`` is included because a
#: run that died must not be left reading "running" forever — the one way this
#: panel could claim the bench was working when it had stopped.
RUN_EVENT_KINDS = (
    RUN_STARTED_KINDS
    | RUN_FINISHED_KINDS
    | frozenset(
        {
            "run_step",
            "run_aborted",
            "run_error",
            RUN_STAGE_KIND_NAME,
            RUN_OUTLET_KIND_NAME,
            RUN_OUTLET_SETTLE_KIND_NAME,
        }
    )
)

#: Run states that mean work is in flight. Taken from
#: :py:mod:`benchctrl.agent.runs.store` (``STATUS_PENDING``/``STATUS_RUNNING``)
#: and matched to what ``fui/view.py`` already treats as in-flight for its
#: flowchart, so the headline and the flowchart cannot disagree about whether a
#: run is happening. ``pending`` counts: a queued run is work the bench is
#: committed to, and the gap between accepting one and starting it is not a
#: window in which the panel should read IDLE.
IN_FLIGHT_RUN_STATES = frozenset({"pending", "starting", "running"})

#: How many recent events the panel keeps for its log pane. Small: this is a
#: status display, not a log viewer, and the artifact log is the record of
#: truth.
LOG_LIMIT = 50

#: Multiple of the agent's heartbeat interval after which silence is treated as
#: staleness. Two missed heartbeats rather than one, so ordinary scheduling
#: jitter on a small board does not make the panel cry wolf.
SILENCE_HEARTBEATS = 3.0

#: Fallback silence budget when the agent never told us its heartbeat.
DEFAULT_SILENCE_S = 45.0

#: How long the panel may show ``STARTING`` before it must admit there is no
#: agent. The feed connects on a background thread, so the first frame is drawn
#: before any attempt has finished and ``NO AGENT`` would be a guess rather than
#: a fact. Past this budget the opposite is true: no attempt has completed in a
#: length of time where one should have, and a screen still saying "starting" is
#: the stale-but-plausible display this module exists to prevent.
STARTUP_GRACE_S = 10.0


@dataclass
class DeviceView:
    """One instrument, as the panel understands it."""

    key: str
    armed: bool = False
    recording: bool = False
    emulating: bool = False
    #: Set when arming was inferred from an event rather than a status
    #: snapshot, so the panel can distinguish "the agent told us" from "we
    #: worked it out".
    inferred: bool = False
    last_change_mono: Optional[float] = None

    @property
    def label(self) -> str:
        if self.armed:
            return "ARMED"
        if self.emulating:
            return "EMULATING"
        if self.recording:
            return "RECORDING"
        return "idle"


@dataclass
class DeviceSlot:
    """What is known about one instrument *before* it has any arm state.

    Separate from :py:class:`DeviceView` because the two answer different
    questions and are learned from different places. ``DeviceView`` is "what is
    this device doing", folded from the safety governor's per-device states.
    This is "does this device exist, and does the agent own it", which comes
    from the WELCOME/``agent.devices`` table and from ``agent.discover``.

    Keeping them apart matters because the governor creates a state lazily, on
    the first call that could arm something. So on a freshly-started agent
    ``safety.devices`` is ``{}`` — an idle bench and an empty bench are
    indistinguishable there, and a rail driven from it alone reports ``NO LINK``
    for an instrument that is plugged in, powered, and ready.
    """

    key: str
    #: The agent lists this key in its registry, so it is a device the agent
    #: would open on demand. Says nothing about the hardware being attached.
    served: bool = False
    #: The registry has the device object open right now.
    opened: bool = False
    #: Why the last open attempt failed, verbatim from the agent. Kept because
    #: "the agent tried and could not" is a different fact from "not attached",
    #: and an operator can act on the first one.
    open_error: Optional[str] = None
    #: Found on the bus by ``agent.discover``. None means no inventory has been
    #: taken, which is NOT the same as "not attached" — see
    #: :py:attr:`BenchStatus.inventory_taken`.
    present: Optional[bool] = None
    #: How sure discovery is: ``exact`` (VID/PID matched a driver signature) or
    #: ``heuristic``/``unknown``. Never upgraded here.
    confidence: Optional[str] = None
    #: Where it was found — a tty path or a VISA resource string.
    path: Optional[str] = None
    #: What the instrument is, in words an operator recognises ("Rigol
    #: DP2000-series power supply"). The rail's own row heading is the device
    #: *key*, which is a benchctrl name for a driver, not a name for the box on
    #: the bench — and on a bench with two Rigols the key is the only thing
    #: telling them apart, so it is worth also saying which is which.
    label: Optional[str] = None
    #: The instrument's serial number, which is the only field that identifies
    #: *this* box rather than its model. It answers the question a rail of
    #: status words cannot: whether the supply now attached is the same one the
    #: last run was calibrated against. See :py:func:`_visa_serial` for why it
    #: often has to be read out of the resource string.
    serial_number: Optional[str] = None
    #: ``vvvv:pppp`` as discovery formats it. Kept alongside the label because
    #: it is the identifier that survives an unrecognised instrument — the same
    #: reason :py:attr:`BenchStatus.unclaimed` entries carry one — and it is
    #: what an operator types into ``lsusb`` when the rail and the bus disagree.
    usb_id: Optional[str] = None


@dataclass
class MainsView:
    """The bench's mains supply and its switched outlets.

    Core harness rather than an instrument, which is why this is its own model
    and not a :py:class:`DeviceSlot` field. A PDU is not something a test
    *measures with*; it is what the bench and the DUT are plugged into, peer to
    the board the agent runs on. An operator reads it to answer "is the DUT
    powered", and the rail of instruments answers a different question.

    Every field starts unknown, and unknown is a first-class state here for the
    same reason it is everywhere else in this module — with one extra edge. Mains
    state is knowable to a dashboard **only** because the bench pushes it: an
    observer session cannot call ``device.call``, so there is no fallback poll
    this model could fall back to. So a panel with no ``mains`` event has not
    failed to display something it could have read; it genuinely has not been
    told, and it must say so rather than draw a plausible 120 V.

    :py:attr:`outlets` is deliberately ``{}`` when nothing has arrived rather
    than a map of eight Falses. An outlet map that says "all off" is a claim that
    the DUT is de-powered, and inventing it would be the most consequential lie
    on the panel: someone reads "off" and reaches into a live enclosure.
    """

    #: Which served device these readings came from. Empty until a sweep lands.
    device: str = ""
    #: ``{outlet_index: energised}``, from the bench's sweep or a run's verified
    #: transition. Keys are ints; the wire carries strings and they are coerced
    #: on the way in.
    outlets: dict[int, bool] = field(default_factory=dict)
    voltage_V: Optional[float] = None
    frequency_Hz: Optional[float] = None
    load_A: Optional[float] = None
    load_W: Optional[float] = None
    #: ``"serial"`` or ``"ssh"`` — which wire the reading came over.
    transport: str = ""
    #: How many sweeps have landed. Proves the bench is reporting rather than the
    #: panel guessing, the same job :py:attr:`BenchStatus.presence_sweeps` does.
    sweeps: int = 0
    #: When the last sweep landed, monotonic. Its own mark rather than the
    #: general freshness one because this reads on a ~10 s clock of its own: a
    #: mains figure that is two minutes old must be shown as old even while the
    #: rest of the panel is current.
    last_sweep_mono: Optional[float] = None
    #: How many verified outlet transitions a run has reported this session, and
    #: which outlets they touched. Kept because a power cycle can be shorter than
    #: the sweep interval, so this is the only evidence the panel has that a DUT
    #: was cycled at all.
    transitions: int = 0
    #: The last transition a run reported, as ``{"outlet": n, "state": bool}``.
    last_transition: Optional[dict] = None
    #: Seconds a run is deliberately waiting for a DUT to boot after switching
    #: mains, from ``run_outlet_settle``. Set when the window opens and cleared
    #: when anything else about mains arrives — it is a statement about now, and
    #: a stale one would have the panel claim the bench is waiting when it has
    #: long since moved on.
    settling_s: Optional[float] = None

    @property
    def known(self) -> bool:
        """Whether anything has reported mains state at all this session."""
        return self.sweeps > 0 or self.transitions > 0

    @property
    def energised(self) -> int:
        """How many known outlets are on. Meaningless unless :py:attr:`known`."""
        return sum(1 for on in self.outlets.values() if on)


@dataclass
class RunView:
    run_id: str
    name: str = ""
    state: str = "unknown"
    step: str = ""
    progress: Optional[float] = None
    #: Device keys this run declared it would drive, from ``run_start``.
    #:
    #: Empty for a run whose start this session never saw — a panel that
    #: connected mid-run, or an older agent that did not declare them. Empty
    #: therefore means "not known", never "no devices", and the view must not
    #: render the difference as though a run had no instruments.
    devices: tuple[str, ...] = ()
    #: Which sequence stage this run last reported, from ``run_stage``.
    #:
    #: Empty means the bench has not said — an older agent, or a panel that
    #: connected mid-run. The display must render that as "no stage known" rather
    #: than picking one, which is the whole point of the change that added this:
    #: the stage used to be inferred from :py:attr:`state`, so it was always
    #: confidently wrong rather than sometimes absent.
    stage: str = ""
    #: The run's own name, from ``run_start``'s ``run_name``.
    #:
    #: Separate from :py:attr:`name`, which is written on *every* run event and so
    #: holds whatever the latest one carried. ``phase_start`` already sends a phase
    #: name under ``name``, so sharing the key would mean a run's name was replaced
    #: the moment phase events were routed into this fold — they are not today,
    #: which makes the separation cheap to keep and expensive to retrofit.
    run_name: str = ""
    #: What the run declared it is testing on, from ``run_start``'s ``dut``.
    #:
    #: A bare label is the whole of the spec's identity for a DUT. ``RunSpec.dut``
    #: defaults to ``""``, so empty means "the author did not say" for a run whose
    #: start we *did* see — which a display must be able to tell apart from having
    #: seen no run at all. :py:attr:`dut_known` carries that distinction, because
    #: this field alone cannot: both cases are the empty string.
    dut: str = ""
    #: Whether ``run_start`` was seen, and so whether :py:attr:`dut` means
    #: anything. False for a panel that connected mid-run or an older agent that
    #: sent no DUT — "nobody told us", as against a declared-empty DUT.
    dut_known: bool = False


@dataclass
class BenchStatus:
    """The whole panel's model. One instance, mutated by ``apply_*``."""

    connected: bool = False
    #: Why the view may not reflect reality; None when the view is trustworthy.
    stale_reason: Optional[str] = None
    devices: dict[str, DeviceView] = field(default_factory=dict)
    #: Per-key presence/ownership, keyed the same as :py:attr:`devices` but with
    #: a different lifetime: a slot persists across the governor forgetting a
    #: device, because "the agent serves this" stays true while "it is armed"
    #: stops being true.
    slots: dict[str, DeviceSlot] = field(default_factory=dict)
    #: True once an ``agent.discover`` inventory has been folded in. Until then
    #: every slot's ``present`` is None and the panel must say "not scanned"
    #: rather than "not attached" — the difference between not knowing and
    #: knowing there is nothing there.
    inventory_taken: bool = False
    #: True once a device table has been folded in from WELCOME or
    #: ``agent.devices``. The same not-knowing-vs-knowing split one level up: with
    #: no table at all, a key missing from :py:attr:`slots` means nothing, whereas
    #: with a table it means the agent will not drive that device. Without this
    #: flag an agent too old to send the table would make every slot read
    #: "NOT SERVED", which is a confident claim about a bench nobody asked.
    registry_known: bool = False
    #: Instruments discovery found that no driver claims, as
    #: ``{"usb_id": ..., "label": ..., "path": ...}``. Shown because the useful
    #: bench fact is often "something is plugged in that benchctrl cannot
    #: drive", and a rail of five fixed slots structurally cannot say that.
    unclaimed: list[dict] = field(default_factory=list)
    #: The bench's mains supply, if it has a PDU. Always present as an object and
    #: empty-by-default inside, so the view never has to guard on None — but see
    #: :py:attr:`MainsView.known` for the distinction that actually matters:
    #: "this bench has no PDU" and "this bench has one and we have not heard from
    #: it" must not render the same way, and neither may render as a reading.
    mains: MainsView = field(default_factory=MainsView)
    runs: dict[str, RunView] = field(default_factory=dict)
    #: Which device worker threads are executing a call right now, as
    #: ``{device_key: method}``. Folded from ``agent.status``'s ``workers``
    #: (``WorkerPool.stats()``). Empty means nothing is executing — which is not
    #: the same as nothing being queued, see :py:attr:`queued_devices`.
    #:
    #: This exists because a bench being *driven* used to read IDLE: arm state is
    #: the only thing the headline consulted, so a sequence of measurements with
    #: no output armed looked exactly like an untouched bench. That is not a
    #: safety lie but it is a misleading one — it invites someone to start a
    #: second run on top of the first.
    busy_devices: dict[str, str] = field(default_factory=dict)
    #: Per-device queue depth for anything waiting behind the current call, from
    #: the same ``workers`` payload. Kept separate from :py:attr:`busy_devices`
    #: because a worker can hold a queue with ``busy_with`` momentarily None,
    #: between finishing one job and picking up the next: work is still in
    #: flight there, and a headline that flickered IDLE across that gap would be
    #: wrong for exactly as long as anyone was looking at it.
    queued_devices: dict[str, int] = field(default_factory=dict)
    #: ``{device_key: monotonic}`` — when an action from this device last arrived,
    #: and in :py:attr:`last_action_name` what it was.
    #:
    #: Fed by ``action`` events rather than by the status poll, which is the whole
    #: point: a device call takes ~200 ms and the poll runs every 5 s, so
    #: :py:attr:`busy_devices` misses almost every one. These two say "last seen
    #: doing X, N seconds ago" — a statement about the past, which stays true as it
    #: ages, unlike "busy now", which does not.
    last_action_at: dict[str, float] = field(default_factory=dict)
    last_action_name: dict[str, str] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    sticky: list[dict] = field(default_factory=list)
    #: Count of events the agent told us it dropped. Cumulative and shown, so a
    #: chronically-behind panel is visibly chronic rather than briefly odd.
    dropped_events: int = 0
    #: How many discrete bench actions this session has heard about, counting the
    #: repeats an ``action`` event stands for rather than the events themselves.
    #: The pane shows a bounded window; this is the total behind it.
    actions_seen: int = 0
    #: How many actions the agent folded into a repeat count instead of sending
    #: as their own line, as the agent's own cumulative figure. Shown because a
    #: log that is a summary must be able to say so — a pane that quietly
    #: collapsed 4000 reads into eleven rows would be the silent truncation this
    #: module's header rules out. Distinct from :py:attr:`dropped_events`: folded
    #: actions were *counted and deliberately summarised* at the producer, while
    #: dropped events were lost because this consumer could not keep up.
    actions_folded: int = 0
    #: Link heartbeats received. Not shown as log rows (they are never logged),
    #: but exposed so a header can prove the link is live on a bench where
    #: nothing else is happening, and so a test can assert the heartbeat is
    #: actually arriving rather than merely that the panel is not stale — those
    #: are different claims, and the dashboard's own polling would satisfy the
    #: weaker one on its own.
    link_beats: int = 0
    #: Bench-pushed presence sweeps received. Like :py:attr:`link_beats`, exposed
    #: so the panel can show that the bench is confirming its hardware rather than
    #: the display having to ask — the difference between a screen that is being
    #: told and one that is guessing.
    presence_sweeps: int = 0
    #: When the last presence sweep landed. Separate from
    #: :py:attr:`last_snapshot_mono` because a sweep answers a different question
    #: (what is *attached*) on a much slower clock, so folding it into the general
    #: freshness mark would make a stale bus inventory look as current as a 5 s
    #: status poll.
    last_presence_mono: Optional[float] = None
    seconds_since_contact: Optional[float] = None
    deadman_s: Optional[float] = None
    heartbeat_s: Optional[float] = None
    last_event_mono: Optional[float] = None
    last_snapshot_mono: Optional[float] = None
    last_trip: Optional[dict] = None
    #: Set by a ``safety_failed`` event and never cleared automatically: the
    #: agent could not prove an output went off, and only a human can decide
    #: that is resolved.
    unsafe_latch: Optional[dict] = None
    #: Set when the agent's WELCOME did not confirm the observer role we asked
    #: for, and never cleared automatically — see :py:meth:`apply_connected`.
    observer_denied: Optional[dict] = None
    agent_name: str = ""
    #: Monotonically increasing ``seq`` last seen. The event bus guarantees
    #: order is never permuted, so a decrease means a reconnect, not a bug.
    last_seq: Optional[int] = None
    #: Whether any connection attempt has finished yet, either way. Separates
    #: "I have not tried" from "I tried and there is no agent" — see
    #: :py:attr:`headline`.
    attempted: bool = False
    #: When this model was built, for :py:meth:`expire_startup_grace`. Uses the
    #: real clock by default and is settable, so tests do not have to sleep.
    created_mono: float = field(default_factory=time.monotonic)

    # --- derived --------------------------------------------------------

    @property
    def armed_devices(self) -> list[str]:
        return sorted(k for k, d in self.devices.items() if d.armed)

    @property
    def any_armed(self) -> bool:
        return bool(self.armed_devices)

    @property
    def running_runs(self) -> list[str]:
        """Run ids the agent reports as in flight, from :py:attr:`runs`.

        Reuses the run state the model already folds from ``run_start`` /
        ``run_end`` rather than keeping a second notion of "a run is happening" —
        two counters for one fact drift, and the one that drifts is always the one
        on screen.
        """
        return sorted(
            k for k, r in self.runs.items() if r.state in IN_FLIGHT_RUN_STATES
        )

    @property
    def enrolled_devices(self) -> dict[str, str]:
        """Device key -> run id, for every device committed to an in-flight run.

        The answer to "which instruments is this test using", asked of the run
        rather than of the traffic. A device only reveals itself through calls,
        and a run makes calls in bursts — a supply set once at phase entry then
        held for a ten-minute dwell is *in use* the whole time while looking idle
        for all but 200 ms of it. Enrollment covers the dwell; activity marks the
        instant.

        Only in-flight runs contribute, so a finished run releases its devices
        without anything having to clear them: the enrollment has exactly the
        lifetime of the run that declared it, which is the property that keeps a
        crashed or aborted run from pinning an instrument to IN RUN forever.

        Ties go to the lowest run id purely for determinism — two runs cannot hold
        one device (``RunManager.submit`` refuses it), so a tie means a stale view,
        and a stable answer is easier to read than a flickering one.
        """
        out: dict[str, str] = {}
        for run_id in self.running_runs:
            for key in self.runs[run_id].devices:
                out.setdefault(key, run_id)
        return out

    #: How long after its last completed call the bench still counts as busy.
    #:
    #: Sized against the status poll (``feed.DEFAULT_POLL_S``, 5 s) with margin,
    #: for the reason below: the window has to be *wider* than the sampling gap it
    #: is covering, or it leaves the same blind spot.
    ACTIVITY_WINDOW_S = 8.0

    @property
    def any_busy(self) -> bool:
        """True when the bench is executing, queueing, or has just finished work.

        A run in flight counts even with every worker momentarily idle: the run
        engine spends most of its time between device calls (settling, waiting
        out a dwell), and those gaps are not moments when the bench is free.

        A call that *completed* within :py:attr:`ACTIVITY_WINDOW_S` counts too,
        and that clause is the one that makes this property usable. The other
        three terms all come from the ``workers`` table, which is **sampled** on
        the 5 s status poll while a device call takes ~200 ms — so the poll lands
        inside a call roughly 4% of the time. Measured on the bench: a six-setpoint
        4-wire resistance sweep ran for minutes with two instruments being driven
        continuously, and both the headline and the footer read IDLE for almost
        all of it, flickering ACTIVE for one frame when a poll happened to land.

        ``last_action_at`` is fed by ``action`` events instead, which arrive as
        each call *completes*, so it sees the calls the poll cannot. Folding it in
        here rather than at the two readouts is deliberate: the headline and the
        footer are separate code paths that both consume this one property, and
        the bug the operator actually reported was the two of them disagreeing.
        One source cannot contradict itself.

        The claim stays honest because it is deliberately not "a call is in flight
        now" — it is "the bench is working", which a bench mid-sweep is during the
        200 ms gaps between its calls. The per-device rails keep the sharper
        distinction: they show ``busy`` for a call in flight and a past-tense
        ``recent`` with its age for one that has finished, so nothing on the glass
        claims an instant it cannot vouch for.
        """
        if self.busy_devices or self.queued_devices or self.running_runs:
            return True
        if not self.last_action_at:
            return False
        # No argument, so this is always the panel's own clock — the same clock the
        # stamps in last_action_at were taken from, never an agent's.
        newest = max(self.last_action_at.values())
        return (_mono(None) - newest) <= self.ACTIVITY_WINDOW_S

    @property
    def busy_summary(self) -> str:
        """One short phrase naming what is in flight, for the detail line.

        ``ACTIVE`` on its own says less than it looks like it does — an operator
        who cannot see WHICH device is busy has to guess whether the panel means
        their run or someone else's. Names the device and method when they are
        known, because that is the difference between a status word and an answer.

        Falls back to the most recent *completed* call, in the past tense and with
        its age, when nothing is in flight as of the last poll. Without this the
        footer says "BENCH ACTIVE" with no detail through a whole sweep, because
        :py:attr:`any_busy`'s activity window is what made it active and the
        ``workers`` table it would otherwise quote is empty. Tensed and aged
        because that is what it is: quoting a finished call as though it were
        running would be the panel asserting an instant it cannot vouch for.
        """
        parts = [f"{key}: {method}" for key, method in sorted(self.busy_devices.items())]
        parts += [
            f"{key}: {depth} queued"
            for key, depth in sorted(self.queued_devices.items())
            if key not in self.busy_devices
        ]
        parts += [f"run {run_id}" for run_id in self.running_runs]
        if parts:
            return ", ".join(parts)
        if not self.last_action_at:
            return ""
        key = max(self.last_action_at, key=lambda k: self.last_action_at[k])
        age = max(0.0, _mono(None) - self.last_action_at[key])
        name = self.last_action_name.get(key) or ""
        what = f"{key}: {name}" if name else key
        return f"{what} {age:.0f}s ago"

    @property
    def trustworthy(self) -> bool:
        # observer_denied counts: the readings may be perfectly current, but a
        # session that could be holding the deadman open is not a session this
        # panel should be drawing in confident bright cyan.
        return (
            self.connected
            and self.stale_reason is None
            and self.observer_denied is None
        )

    @property
    def starting(self) -> bool:
        """True only before the first connection attempt has finished.

        ``NO AGENT`` is a claim about the bench; during startup it would be a
        guess about ourselves. The feed connects on a background thread, so the
        very first frame is drawn before any attempt has resolved — a panel that
        shouts there is no agent in that window has cried wolf, and a display
        that cries wolf on every boot is one an operator learns to ignore.

        The window is bounded two ways: any finished attempt sets
        :py:attr:`attempted` (so a real failure shows ``NO AGENT`` with its real
        reason, usually within milliseconds on localhost), and
        :py:meth:`expire_startup_grace` ends it after
        :py:data:`STARTUP_GRACE_S` regardless.
        """
        return not self.attempted and not self.connected

    @property
    def headline(self) -> str:
        """One word for the top of the screen, worst-case first.

        Order matters and is the whole point: an unproven-unsafe output beats
        a staleness warning, which beats an armed-but-known state, which beats
        idle. The most dangerous true thing wins the largest text.

        ``ACTIVE`` sits at the bottom, above only ``IDLE``. Being busy is not a
        hazard — it is the bench working normally — and every state above it is
        either a hazard or a reason to distrust the screen. A bench that is both
        armed and busy must read ``ARMED``: promoting ACTIVE would let a routine
        measurement hide a live output, turning the ordering that exists to
        surface hazards into the thing that buries them.
        """
        if self.unsafe_latch is not None:
            return "UNSAFE"
        if self.starting:
            return "STARTING"
        if not self.connected:
            return "NO AGENT"
        # Above STALE: a stale view is a display problem, but an unconfirmed
        # observer role means this display may be holding the bench's deadman
        # open. That is a hazard to the bench itself, and it outranks.
        if self.observer_denied is not None:
            return "NOT OBSERVER"
        if self.stale_reason is not None:
            return "STALE"
        if self.any_armed:
            return "ARMED"
        if any(d.recording for d in self.devices.values()):
            return "RECORDING"
        if self.any_busy:
            return "ACTIVE"
        return "IDLE"

    @property
    def severity(self) -> str:
        """Severity of :py:attr:`headline`, for colouring."""
        return {
            "UNSAFE": "critical",
            # Starting up is not a warning: nothing is known to be wrong yet.
            "STARTING": "info",
            "NO AGENT": "warn",
            # critical, not warn: this one is about the bench being at risk
            # rather than about the display being behind.
            "NOT OBSERVER": "critical",
            "STALE": "warn",
            "ARMED": "alarm",
            "RECORDING": "info",
            # info, not warn: work in progress is the bench doing its job. An
            # amber panel every time a run makes a call is a panel whose colour
            # stops meaning anything, and the colours here are load-bearing for
            # the states that are genuinely wrong.
            "ACTIVE": "info",
            "IDLE": "info",
        }[self.headline]

    # --- transitions ----------------------------------------------------

    def apply_connected(self, welcome: dict, *, now: Optional[float] = None) -> None:
        """A session came up. Clears staleness that a reconnect actually fixes.

        Asking for the observer role is not the same as having it. The feed
        connects with ``observer=True``, but only the *agent* can enforce what
        that means, and this display cannot function safely without it: an
        observer's frames do not call ``Governor.touch()``, and a session whose
        frames DO would pin ``seconds_since_contact`` near zero and stop the
        deadman from ever tripping. An always-on panel would then be the thing
        keeping an armed output alive after the operator's real client died.

        The agent echoes the role back specifically so a client can check
        (``agent/server.py``: *"Echoed so a client can assert it got the role it
        asked for"*), so check it. This is a downgrade detector, not a guarantee
        — against an older agent, or one whose observer branch regressed, it is
        the only warning available. It latches for the same reason
        :py:attr:`unsafe_latch` does: a later reconnect that happens to succeed
        says nothing about how long the deadman was held open.
        """
        self.connected = True
        self.attempted = True
        if welcome.get("observer") is not True:
            self.observer_denied = {
                "agent": str(welcome.get("agent", "")),
                "got": welcome.get("observer"),
            }
        self.agent_name = str(welcome.get("agent", ""))
        self.deadman_s = _as_float(welcome.get("deadman_s"))
        self.heartbeat_s = _as_float(welcome.get("heartbeat_s"))
        # A reconnect restarts the agent's seq counter, and the monotonicity
        # guarantee is per-session. Forget it rather than reporting a bogus gap.
        self.last_seq = None
        self.last_event_mono = _mono(now)
        # The registry table rides along in WELCOME, so which devices this agent
        # serves is known from the first frame — no extra call, and available
        # even while every other readout is still NO LINK.
        self.apply_registry(welcome.get("devices"))
        # Connecting does NOT clear unsafe_latch: a fresh socket says nothing
        # about whether the output ever went off.
        self.stale_reason = "connected — waiting for the first status snapshot"

    def apply_registry(self, described: object) -> None:
        """Fold in the agent's device table (WELCOME, or ``agent.devices``).

        Each entry is ``registry.DeviceEntry.to_dict()``: ``key``, ``open``, and
        ``open_error`` when the last attempt failed.

        A key the agent no longer serves has its ``served`` flag cleared rather
        than the slot deleted, because discovery may still have found the
        hardware — "attached but this agent will not drive it" is a real and
        confusing bench state, and it is worth being able to show it.
        """
        if not isinstance(described, list):
            # An older agent, or a malformed frame. Leaving the previous table in
            # place would be worse than knowing nothing: it would attribute the
            # last agent's devices to this session.
            return
        seen: set[str] = set()
        for entry in described:
            if not isinstance(entry, dict):
                continue
            key = entry.get("key")
            if not isinstance(key, str) or not key:
                continue
            seen.add(key)
            slot = self.slots.setdefault(key, DeviceSlot(key=key))
            slot.served = True
            slot.opened = bool(entry.get("open"))
            error = entry.get("open_error")
            slot.open_error = str(error) if error else None
        for key, slot in self.slots.items():
            if key not in seen:
                slot.served = False
                slot.opened = False
        self.registry_known = True

    def apply_inventory(self, inventory: object) -> None:
        """Fold in an ``agent.discover`` result: what is physically on the bus.

        This is the only source that can say an instrument is *attached*, and it
        is deliberately a separate call from the status poll — on the bench board
        it costs ~1.65 s against ~5 ms for ``agent.status``, because identifying
        a USB-TMC instrument means reading its string descriptors over libusb.
        Folding it into the fast poll would pace the whole panel at the slowest
        thing it does.

        Sets :py:attr:`inventory_taken`, which is what licenses the view to draw
        the difference between "not attached" and "not scanned yet".
        """
        if not isinstance(inventory, dict):
            return
        devices = inventory.get("devices")
        if not isinstance(devices, list):
            return

        found: dict[str, dict] = {}
        unclaimed: list[dict] = []
        for dev in devices:
            if not isinstance(dev, dict):
                continue
            key = dev.get("device_key")
            if isinstance(key, str) and key:
                # First wins: discover() returns a sorted list, so this is
                # deterministic, and a second copy of one instrument does not
                # overwrite the identification of the first.
                found.setdefault(key, dev)
                continue
            # Unidentified. Only worth reporting if it carries a USB ID — the
            # board's four /dev/ttyS* and its own VISA aliases are neither
            # instruments nor absences, and listing them would make a two-
            # instrument bench look like a fourteen-instrument one.
            usb_id = dev.get("usb_id")
            if not usb_id:
                continue
            unclaimed.append(
                {
                    "usb_id": str(usb_id),
                    "label": str(dev.get("label") or dev.get("product") or ""),
                    "path": str(dev.get("path") or ""),
                }
            )

        for key, dev in found.items():
            slot = self.slots.setdefault(key, DeviceSlot(key=key))
            slot.present = True
            confidence = dev.get("confidence")
            slot.confidence = str(confidence) if confidence else None
            path = dev.get("path")
            slot.path = str(path) if path else None
            # Identity, so the rail can say what the instrument IS and not only
            # how it is doing. All three are best-effort: an agent too old to
            # send them leaves them None, which the view renders as nothing
            # rather than as a claim.
            label = dev.get("label") or dev.get("product") or dev.get("manufacturer")
            slot.label = str(label) if label else None
            usb_id = dev.get("usb_id")
            slot.usb_id = str(usb_id) if usb_id else None
            slot.serial_number = _device_serial(dev)
        # Absence is only recorded for slots we already know about. Anything the
        # scan did not find is present=False now — an authoritative negative,
        # which is exactly what makes a dark slot mean "looked, not there".
        for key, slot in self.slots.items():
            if key not in found:
                slot.present = False
                slot.confidence = None
                slot.path = None
                slot.label = None
                slot.serial_number = None
                slot.usb_id = None

        # De-duplicated by USB ID: the same instrument can surface on more than
        # one transport (a CH340 with no tty, plus its VISA alias).
        by_id: dict[str, dict] = {}
        for item in unclaimed:
            by_id.setdefault(item["usb_id"], item)
        self.unclaimed = [by_id[k] for k in sorted(by_id)]
        self.inventory_taken = True

    def apply_presence(self, event: object, *, now: Optional[float] = None) -> None:
        """Fold in a bench-pushed presence sweep: what is on the bus right now.

        The agent's answer to "are my instruments still there", arriving without
        the panel having asked. Sets :py:attr:`inventory_taken`, because a sweep
        *is* somebody having looked — before this, a panel that connected to an
        already-running bench showed UNSCANNED until its own first inventory poll
        completed.

        Deliberately narrower than :py:meth:`apply_inventory`: it carries presence
        only, not identity. A sweep runs with ``probe=False`` and reports device
        keys, so it can say "the DMM is on the bus" but has nothing new to say
        about its serial number. Identity is left exactly as the last full
        inventory left it rather than being cleared — a sweep that blanked the
        serial numbers every 30 s would make the rails flicker between naming a
        specific instrument and naming none.

        Absence is recorded only for keys the sweep actually considered
        (``served``). A key the agent does not serve is not something this sweep
        looked for, and marking it absent would be asserting a negative nobody
        measured — the same not-knowing-vs-nothing rule
        :py:class:`DeviceSlot` exists to keep.
        """
        if not isinstance(event, dict):
            return
        present = event.get("present")
        served = event.get("served")
        if not isinstance(present, list) or not isinstance(served, list):
            # A malformed sweep costs the panel this update and nothing else. This
            # runs on the feed's receive path, where raising would drop the session
            # and blank the screen over a bad optional field.
            return
        present_keys = {k for k in present if isinstance(k, str)}
        served_keys = [k for k in served if isinstance(k, str)]
        for key in served_keys:
            slot = self.slots.setdefault(key, DeviceSlot(key=key))
            slot.present = key in present_keys
        self.inventory_taken = True
        self.presence_sweeps += 1
        self.last_presence_mono = _mono(now)

    def apply_mains(self, event: object, *, now: Optional[float] = None) -> None:
        """Fold in a bench-pushed mains sweep: the PDU's metering and outlets.

        The panel's only route to mains state. A dashboard is an observer session
        and cannot call ``device.call``, so unlike every other reading here there
        is no poll to fall back on if these events stop — which is why a sweep
        that fails to parse leaves the previous reading in place rather than
        blanking it, and why staleness is carried separately (see
        :py:attr:`MainsView.last_sweep_mono`).

        Outlet keys arrive as strings, because JSON object keys are strings, and
        are coerced to ints here. A key that is not an integer is dropped rather
        than kept as a string: the view sorts these to lay out a row of outlets,
        and a mixed-type key set would raise on the render path — on the frame
        after a malformed event, in the process that draws the screen.

        The whole outlet map is **replaced**, not merged. A sweep is a complete
        statement about every outlet at one instant, and merging would let an
        outlet the PDU stopped reporting keep its last value forever, which is
        precisely the stale-but-plausible reading this module exists to prevent.
        """
        if not isinstance(event, dict):
            return
        outlets = event.get("outlets")
        if not isinstance(outlets, dict):
            # A malformed sweep costs this update and nothing else — the same rule
            # apply_presence follows, on the same receive path.
            return
        coerced: dict[int, bool] = {}
        for key, value in outlets.items():
            try:
                coerced[int(key)] = bool(value)
            except (TypeError, ValueError):
                continue
        mains = self.mains
        mains.outlets = coerced
        device = event.get("device")
        if isinstance(device, str) and device:
            mains.device = device
        transport = event.get("transport")
        if isinstance(transport, str) and transport:
            mains.transport = transport
        # Each read with _as_float and assigned only when it parses: a metering
        # field the PDU printed as "----" (it does, for power factor at zero load)
        # must leave the previous number alone rather than turn into 0.0. Zero
        # volts is a claim about the mains supply and nothing here should be able
        # to invent it.
        for attr in ("voltage_V", "frequency_Hz", "load_A", "load_W"):
            value = _as_float(event.get(attr))
            if value is not None:
                setattr(mains, attr, value)
        mains.sweeps += 1
        mains.last_sweep_mono = _mono(now)
        # A completed sweep supersedes any settle window: the bench has answered
        # about *now*, so a note saying it is waiting for a DUT to boot is either
        # finished or about to be re-announced by the run.
        mains.settling_s = None

    def apply_run_outlet(self, event: dict, *, now: Optional[float] = None) -> None:
        """Fold one verified outlet transition a run reported.

        Folded in addition to the periodic sweep, not instead of it, because the
        two see different things. The sweep samples every ~10 s; a power-cycle
        phase can cut and restore mains inside that window, so a panel driven by
        the sweep alone would show "on, on" across a DUT losing power — the
        transition that the whole test is about, invisible.

        The engine emits this *after* reading the contactor back, and the
        ``state`` field is that read-back rather than what was requested. So this
        is a measurement and is treated as one: it updates the outlet map. A
        ``requested`` that disagrees with ``state`` never reaches here — the
        engine raises and fails the phase instead.
        """
        nested = event.get("data")
        fields = {**nested, **event} if isinstance(nested, dict) else event
        try:
            index = int(fields["outlet"])
        except (KeyError, TypeError, ValueError):
            return
        # ``state`` only. A missing read-back is not a transition we can believe,
        # and falling back to ``requested`` would defeat the reason the engine
        # verifies at all: ``oltctrl`` acknowledges nothing, so the requested
        # state is exactly the thing that carries no evidence.
        state = fields.get("state")
        if not isinstance(state, bool):
            return
        mains = self.mains
        mains.outlets[index] = state
        mains.transitions += 1
        mains.last_transition = {"outlet": index, "state": state}
        mains.settling_s = None
        mains.last_sweep_mono = _mono(now)

    def apply_run_outlet_settle(self, event: dict) -> None:
        """Note that a run is waiting for a DUT to come up after switching mains.

        Kept so the panel can distinguish a deliberate dead time from a stalled
        phase. Without it, the seconds after a power cycle look identical to a
        run that has hung: no samples, no events, nothing moving.
        """
        nested = event.get("data")
        fields = {**nested, **event} if isinstance(nested, dict) else event
        value = _as_float(fields.get("settle_s"))
        if value is not None and value > 0:
            self.mains.settling_s = value

    def forget_inventory(self, reason: str = "") -> None:
        """Discard what only the live session could vouch for: the bus inventory
        and the agent's device table.

        Called on disconnect. A cable pulled while the panel was blind would
        otherwise leave a slot claiming ATTACHED forever, and that claim is the
        most dangerous kind — it is about the physical bench rather than about
        the display. Reverting to None makes the panel say "not scanned", which
        is true.

        The slots themselves survive, so the keys stay stable across a reconnect
        and the rail does not flicker; only the claims they carry are dropped.

        Identity — label, serial number, USB ID — is dropped with the rest, and
        for a sharper version of the same reason. A serial number reads as proof:
        a rail showing ``DP2A243500269 ATTACHED`` names a specific box, so nobody
        reads it as a guess, and it stays convincing long after the cable it came
        from was pulled. Presence going to None already makes the slot say "not
        scanned"; leaving the serial behind would put a confident identity next
        to it and undo that. Anything only a live scan could vouch for goes when
        the session that vouched for it does.
        """
        self.inventory_taken = False
        self.unclaimed = []
        # The device table belongs to the session that sent it. A reconnect may
        # land on an agent restarted with a different --devices list, so holding
        # the old table would attribute the previous agent's configuration to the
        # new one.
        self.registry_known = False
        for slot in self.slots.values():
            slot.present = None
            slot.confidence = None
            slot.path = None
            slot.label = None
            slot.serial_number = None
            slot.usb_id = None

    def apply_disconnected(self, reason: str = "") -> None:
        """The session went away. No events can arrive, so nothing is current.

        Device state is kept rather than cleared, because the last thing we
        knew is the best available guess and blanking it would make an armed
        bench look idle. It is all marked ``inferred`` so the panel renders it
        as unconfirmed.

        The busy readout goes the other way and is dropped, with presence rather
        than with arm state. The test is which direction the stale claim errs in.
        A kept ``ARMED`` over-warns: it describes a hazard that has probably
        gone, and someone treats a safe bench carefully. A kept ``ACTIVE``
        under-warns in the same shape a kept ``ATTACHED`` does — ``busy_with``
        was true for the instant the last snapshot was taken and nothing since,
        so it is an assertion that a call is in flight *now* on a link nobody can
        see down. It reads as "a run is driving this, leave it alone", which is
        the reassurance that stops someone checking, and it would be indefinite:
        no snapshot can arrive to correct it. Dropping it makes the panel fall
        back to ``NO AGENT``, which is the true thing.
        """
        self.connected = False
        # A finished attempt, however it finished, ends the startup grace: from
        # here on the panel has a fact to report rather than an unknown.
        self.attempted = True
        self.stale_reason = reason or "no session with the agent"
        for device in self.devices.values():
            device.inferred = True
        # Only the live session could vouch for an in-flight call. See above for
        # why this drops rather than latching the way arm state does.
        self.busy_devices = {}
        self.queued_devices = {}
        # Zeroed with the busy readout, not kept with arm state. A heartbeat
        # count is a claim about *this link* being alive, so carrying it across a
        # disconnect is the one thing it must never do: it would read as "beats
        # are arriving" on a session where, by definition, none can. The next
        # session counts its own from zero.
        self.link_beats = 0
        # Same reasoning, same direction: a sweep count is a claim that the bench
        # is actively confirming its hardware *to this session*. Carried across a
        # disconnect it would say the bench is still checking in on a link where
        # nothing can arrive.
        self.presence_sweeps = 0
        self.last_presence_mono = None
        # Mains goes with presence — dropped, not kept-and-marked — and it is the
        # clearest case for that direction on the panel.
        #
        # The test everywhere else here is which way a stale claim errs. A kept
        # ``ARMED`` over-warns and is therefore safe to keep. A kept outlet map
        # can err *either* way and one of those ways is the worst outcome this
        # module can produce: an outlet last seen ``off`` reads as "the DUT is
        # de-powered", which is the reassurance that precedes someone reaching
        # into an enclosure — and mains may have been restored by a run's next
        # phase in the very window where nothing can reach the panel to say so.
        #
        # There is also no correcting poll. Every other reading here is re-
        # established by ``agent.status`` on reconnect; mains exists only because
        # the bench pushes it, so a stale outlet map would persist until the next
        # sweep of a session that may never come.
        #
        # ``device`` and ``transport`` survive: they say *which* PDU and which
        # wire, which stays true across a reconnect and is not a reading.
        self.mains.outlets = {}
        self.mains.voltage_V = None
        self.mains.frequency_Hz = None
        self.mains.load_A = None
        self.mains.load_W = None
        self.mains.sweeps = 0
        self.mains.transitions = 0
        self.mains.last_transition = None
        self.mains.last_sweep_mono = None
        self.mains.settling_s = None
        # Dropped with the busy readout for the same reason: "the DMM was reading
        # 2 s ago" is a claim about a session that no longer exists, and on
        # reconnect the ages would be measured from before the gap.
        self.last_action_at = {}
        self.last_action_name = {}
        # Arm state is kept-but-marked-inferred above, because the last known
        # arm state is the safest available guess. Presence gets the opposite
        # treatment and is dropped outright: a stale "ARMED" over-warns, while a
        # stale "ATTACHED" under-warns by asserting something about the physical
        # bench that nobody has checked since the link died.
        self.forget_inventory()

    def expire_startup_grace(self, *, now: Optional[float] = None) -> None:
        """End the ``STARTING`` window once :py:data:`STARTUP_GRACE_S` has passed.

        The backstop for a feed that never reports either way — a thread that
        died before its first attempt, or a ``start()`` that was never called.
        Without this, "starting" could stay on screen indefinitely, which is the
        stale-but-plausible display the module header rules out.
        """
        if self.attempted:
            return
        if _mono(now) - self.created_mono >= STARTUP_GRACE_S:
            self.attempted = True
            self.stale_reason = (
                f"no connection attempt has completed in {STARTUP_GRACE_S:.0f}s — "
                f"is the dashboard feed running?"
            )

    def apply_status(self, status: dict, *, now: Optional[float] = None) -> None:
        """Fold in an authoritative ``agent.status`` snapshot.

        This is the only thing that may clear staleness, and the only thing
        trusted to say a device is *not* armed.
        """
        stamp = _mono(now)
        safety = status.get("safety") or {}
        self.seconds_since_contact = _as_float(safety.get("seconds_since_contact"))
        self.deadman_s = _as_float(safety.get("deadman_s")) or self.deadman_s

        reported = safety.get("devices") or {}
        for key, raw in reported.items():
            view = self.devices.setdefault(key, DeviceView(key=key))
            was_armed = view.armed
            view.armed = bool(raw.get("armed", raw.get("output_armed", False)))
            view.recording = bool(raw.get("recording", False))
            view.emulating = bool(raw.get("emulating", False))
            view.inferred = False
            if view.armed != was_armed:
                view.last_change_mono = stamp
        # A device the agent no longer reports is gone, not silently armed.
        for key in [k for k in self.devices if k not in reported]:
            del self.devices[key]

        self._apply_workers(status.get("workers"))
        self._apply_sessions(status.get("devices"))

        trips = safety.get("trips") or []
        if trips:
            self.last_trip = trips[-1]

        self.last_snapshot_mono = stamp
        if self.connected and self.unsafe_latch is None:
            self.stale_reason = None

    def _apply_workers(self, workers: object) -> None:
        """Fold ``agent.status``'s ``workers`` table: what is executing *now*.

        Each entry is one :py:class:`~benchctrl.agent.worker.DeviceWorker` as
        ``WorkerPool.stats()`` renders it — ``busy_with`` is the method that
        worker's thread is inside at this instant, ``depth`` what is queued
        behind it.

        Replaced wholesale rather than merged, because this is the one field on
        the snapshot with no lifetime past the snapshot: a device that stopped
        being busy is reported by *absence*, exactly as a disarmed device is. A
        merge would leave the panel claiming a call is running that returned
        minutes ago.

        Two shapes both mean idle and must be treated identically: no entry at
        all, and an entry with ``busy_with=None`` and ``depth=0``. Workers are
        created lazily per device, so on the live board an idle bench reports
        ``workers: {}`` — the same not-knowing-vs-nothing trap
        :py:class:`DeviceSlot` documents for ``safety.devices``, except here the
        two genuinely are the same fact and conflating them is correct.

        Defensive throughout: an older agent sends no ``workers`` key at all,
        and a malformed one must cost the panel its busy readout and nothing
        else. Raising here would take the arm state and the staleness clear down
        with it, trading a missing word for a blind screen.
        """
        busy: dict[str, str] = {}
        queued: dict[str, int] = {}
        if isinstance(workers, dict):
            for key, raw in workers.items():
                if not isinstance(key, str) or not isinstance(raw, dict):
                    continue
                method = raw.get("busy_with")
                if method:
                    busy[key] = _strip_device_prefix(key, str(method))
                depth = raw.get("depth")
                if isinstance(depth, int) and not isinstance(depth, bool) and depth > 0:
                    queued[key] = depth
        self.busy_devices = busy
        self.queued_devices = queued

    def _apply_sessions(self, devices: object) -> None:
        """Fold ``agent.status``'s ``devices`` table: which devices are open now.

        The fix for a defect that made the instrument rail lie for a whole test.
        :py:attr:`DeviceSlot.opened` had exactly one writer,
        :py:meth:`apply_registry`, called from exactly one place — the WELCOME
        frame at connect, when the honest answer is always "nothing is open yet".
        Nothing updated it afterwards, so every card fell through to STANDBY
        ("configured, not opened") for the entire session, including for the two
        instruments a live resistance sweep was driving. The activity marker moved
        because it comes from action events, which do flow; the status word did
        not, because it comes from this flag, which did not.

        Only ``opened`` and ``open_error`` are folded here. ``served`` is left
        alone on purpose: this table is keyed by the same registry, so a device
        missing from it is one the agent no longer serves — but that is
        :py:meth:`apply_registry`'s claim to make from an authoritative
        enumeration, not an inference from a status field. A device absent here
        simply keeps whatever the last registry frame said and is marked closed.

        Defensive for the same reason :py:meth:`_apply_workers` is: an older agent
        sends no ``devices`` key at all, and this method runs inside the poll that
        also clears staleness. Raising over a malformed field would trade one
        missing word for a blank screen. An absent key means "this agent does not
        report open state" and must leave the flags untouched, which is why the
        ``isinstance`` check returns rather than clearing.
        """
        if not isinstance(devices, dict):
            return
        for key, raw in devices.items():
            if not isinstance(key, str) or not isinstance(raw, dict):
                continue
            slot = self.slots.setdefault(key, DeviceSlot(key=key))
            slot.opened = bool(raw.get("open"))
            error = raw.get("open_error")
            slot.open_error = str(error) if error else None
        for key, slot in self.slots.items():
            if key not in devices:
                slot.opened = False

    def apply_event(self, event: dict, *, now: Optional[float] = None) -> None:
        """Fold in one event frame."""
        stamp = _mono(now)
        self.last_event_mono = stamp
        kind = str(event.get("kind", ""))
        severity = str(event.get("severity", "info"))

        seq = event.get("seq")
        if isinstance(seq, int):
            self.last_seq = seq

        if kind == LINK_KIND_NAME:
            # Liveness only, and it returns before the log append below.
            #
            # That early return is the whole point of handling this kind
            # separately. LOG.MGR shows 24 rows; a heartbeat every 5s would
            # evict every real bench action within about two minutes and leave
            # the operator watching the connection talk about itself. The
            # timestamp taken at the top of this method has already done the one
            # job a heartbeat has.
            #
            # `armed` is deliberately NOT folded in from here. It rides along for
            # a consumer that wants it, but arm state on this panel comes from
            # `device_armed`/`safety_trip` and `agent.status` — the governor's own
            # sources. Two writers for one safety fact means the quieter one
            # eventually contradicts the louder one on screen, which is the same
            # rule the action handler below follows for the same reason.
            self.link_beats += 1
            hb = _as_float(event.get("heartbeat_s"))
            if hb is not None and hb > 0:
                # The agent's live figure, which outranks the one from WELCOME:
                # a reconfigured agent reports its new interval here, and the
                # silence budget should follow it rather than a value fixed at
                # connect time.
                self.heartbeat_s = hb
            return

        if kind == PRESENCE_KIND_NAME:
            # The bench telling us, unbidden, what it can see on the bus. This is
            # the push half of "the UI never probes": presence used to be knowable
            # only by the dashboard calling agent.discover on its own timer, which
            # made the display the cause of a USB scan.
            #
            # Only a *changed* sweep is logged. An unchanged one is the bench
            # saying "still four instruments" every 30 s, and at 24 visible rows
            # that would evict real bench actions exactly as a logged heartbeat
            # would — the same trap, from a second source.
            self.apply_presence(event, now=stamp)
            if not event.get("changed"):
                return
        elif kind == MAINS_KIND_NAME:
            # The bench telling us what its PDU is doing. The same push-not-poll
            # shape as presence, and the same log discipline: only a sweep whose
            # outlet states *changed* earns a row, because an unchanged one every
            # ~10 s would evict the 24 visible rows of real bench actions inside
            # four minutes. Mains voltage drifting by 0.3 V is not news.
            self.apply_mains(event, now=stamp)
            if not event.get("changed"):
                return
        elif kind == "events_dropped":
            # The agent is telling us our own view has holes. Believe it.
            count = event.get("count")
            self.dropped_events += int(count) if isinstance(count, int) else 1
            self.stale_reason = (
                f"{self.dropped_events} event(s) dropped — this view is incomplete"
            )
        elif kind == "safety_trip":
            self.last_trip = {
                "reason": event.get("reason"),
                "devices": event.get("devices") or [],
                "at": time.time(),
            }
            # A trip disarms; this is an authoritative disarm source.
            for key in event.get("devices") or []:
                view = self.devices.setdefault(key, DeviceView(key=key))
                view.armed = False
                view.emulating = False
                view.last_change_mono = stamp
        elif kind == "safety_failed":
            # The agent could not prove the output went off. Latch it: nothing
            # short of a human should make this go away.
            self.unsafe_latch = {
                "device": event.get("device"),
                "guidance": event.get("guidance")
                or "Output may still be live — physically disconnect the DUT.",
                "at": time.time(),
            }
            self.stale_reason = "an output could not be confirmed safe"
        elif kind in ("device_armed", "output_armed"):
            key = str(event.get("device", ""))
            if key:
                view = self.devices.setdefault(key, DeviceView(key=key))
                view.armed = True
                view.inferred = True
                view.last_change_mono = stamp
        elif kind in ("action", "action_failed"):
            # Every discrete thing the agent did: a command sent, a value read, an
            # open, an arm. Folded into the log below like any other event and
            # deliberately nothing more — an action does NOT touch arm state.
            #
            # It is tempting, because the event carries the method name and
            # ``set_output`` is right there. It would also be wrong in the
            # dangerous direction: the log's grading is a presentation guess,
            # while ``device_armed`` and ``safety_trip`` come from the governor,
            # which owns the driver and knows whether the call took effect. Two
            # sources for one fact means the quieter one eventually contradicts
            # the louder one on screen.
            #
            # Both counters are read defensively. ``int(event["count"])`` would
            # raise on a malformed payload, and this method is called from the
            # feed's receive path: one bad event would kill the session and blank
            # the panel. That is the "a malformed event does not kill the feed"
            # rule already tested in this suite, and a numeric field arriving from
            # another process is exactly where it gets tested for real.
            self.actions_seen += _as_count(event.get("count"))
            # When this device was last *observed* doing something, and what.
            #
            # Not the same claim as ``busy_devices``, and deliberately a separate
            # field: ``busy_devices`` comes from ``agent.status``'s worker table
            # and means "a call is in flight at this instant", while this means "a
            # call completed at this moment in the past". The distinction is what
            # makes this safe to keep — it never says a call is running.
            #
            # It exists because ``busy_devices`` is *sampled* on the 5 s status
            # poll while a real device call takes ~200 ms, so the poll essentially
            # never lands inside one. Driving a 6-setpoint sweep left both
            # instruments reading STANDBY for the entire test and the headline
            # flickering ACTIVE only when a poll got lucky. Action events, by
            # contrast, arrive as each call completes, so they see every call —
            # they are the only source that can show a busy bench at this
            # timescale.
            key = str(event.get("device", ""))
            if key:
                action = event.get("action") or event.get("method") or ""
                self.last_action_at[key] = stamp
                self.last_action_name[key] = _strip_device_prefix(key, str(action))
            folded = event.get("folded")
            if isinstance(folded, int) and not isinstance(folded, bool) and folded >= 0:
                # Cumulative on the agent side, so this is an assignment rather
                # than an accumulation — adding would multiply the count. Never
                # allowed to go backwards: a decrease means a reconnect to a
                # restarted agent, and the larger figure is the one this session
                # actually saw folded.
                self.actions_folded = max(self.actions_folded, folded)
        elif kind in RUN_EVENT_KINDS:
            self._apply_run_event(kind, event)

        self.log.append(event)
        if len(self.log) > LOG_LIMIT:
            del self.log[: -LOG_LIMIT]
        if severity in STICKY_SEVERITIES:
            self.sticky.append(event)
            if len(self.sticky) > LOG_LIMIT:
                del self.sticky[:-LOG_LIMIT]

    def _apply_run_event(self, kind: str, event: dict) -> None:
        """Fold one run lifecycle event.

        Accepts both spellings of each kind, and the agent's is the one that
        matters. :py:class:`~benchctrl.agent.runs.engine.RunEngine` emits
        ``run_start`` and ``run_end`` (asserted in ``test_run_engine.py``); this
        model was written against ``run_started``/``run_finished``, which nothing
        in the agent has ever sent. The result was that :py:attr:`running_runs`
        stayed empty for the whole of a real run — and its tests passed anyway,
        because they fed it the dashboard's own invented spelling. A vocabulary
        mismatch between two processes is exactly what a single-process test
        cannot see.

        Both are kept rather than the wrong pair simply replaced: a run's events
        are persisted by ``store.append_event`` and replayed on reconnect via
        ``since_seq``, so a store written by an older or newer agent can hand this
        model either spelling, and dropping one would silently lose runs from the
        panel again.
        """
        run_id = str(event.get("run_id", "") or event.get("run", ""))
        if not run_id:
            return
        # The engine puts every payload field under ``data``
        # (``store.Event.to_dict``), and nothing between it and here flattens
        # that: ``Session.send_event`` copies the dict and adds only seq/ts, and
        # ``AgentFeed._on_event`` hands it straight to ``apply_event``. Reading
        # only the top level is why a real run enrolled no devices at all while
        # this model's unit tests passed — the same class of bug as the
        # ``run_started`` spelling, one layer down.
        #
        # Merged rather than replaced, and the top level wins: hand-built and
        # replayed events carry these fields flat, a store from another agent
        # version may use either shape, and where both somehow exist the outer one
        # is the more specific statement about this event.
        nested = event.get("data")
        fields = (
            {**nested, **event} if isinstance(nested, dict) else event
        )
        view = self.runs.setdefault(run_id, RunView(run_id=run_id))
        view.name = str(fields.get("name", "") or view.name)
        if kind in RUN_STARTED_KINDS:
            view.state = "running"
            declared = fields.get("devices")
            if isinstance(declared, list):
                view.devices = tuple(
                    d for d in declared if isinstance(d, str) and d
                )
            # Read only on ``run_start``, unlike ``name`` above: these describe the
            # run itself, so a later event has nothing new to say about them and a
            # phase event carrying a stray key must not redefine what is on test.
            run_name = fields.get("run_name")
            if isinstance(run_name, str) and run_name:
                view.run_name = run_name
            # ``dut_known`` turns on for any ``run_start``, including one that sent
            # no DUT at all: the start is what we witnessed, and "this run declared
            # nothing" is a different fact from "we never saw the run". A non-str
            # value is treated as not declared rather than coerced — ``str(None)``
            # would put the word "None" in a panel titled DEVICE UNDER TEST.
            view.dut_known = True
            dut = fields.get("dut")
            view.dut = dut if isinstance(dut, str) else ""
        elif kind in RUN_FINISHED_KINDS:
            # ``run_end`` carries its outcome under ``status``; ``run_finished``
            # used ``state``. Read both so the rail shows "safe_stopped" rather
            # than a flat "finished" for the one outcome that matters most.
            outcome = fields.get("status") or fields.get("state") or "finished"
            view.state = str(outcome)
        elif kind == "run_error":
            view.state = "error"
        elif kind == "run_aborted":
            view.state = "aborted"
        elif kind == RUN_STAGE_KIND_NAME:
            # Recorded verbatim and never checked against a stage list here. This
            # module deliberately knows nothing about ``benchctrl.agent``, so the
            # vocabulary lives with the bench that emits it; a panel that dropped a
            # stage it did not recognise would go blank against a newer agent,
            # which is the opposite of the failure this fixes. The renderer decides
            # how to draw a name it does not know.
            stage = fields.get("stage")
            if isinstance(stage, str) and stage:
                view.stage = stage
            # Deliberately no ``else`` clearing it: a malformed event is not news
            # that the run left its stage.
        elif kind == RUN_OUTLET_KIND_NAME:
            # Routed to the mains model rather than the run view. It is folded
            # here, inside the run branch, because it is a run event and arrives
            # with a ``run_id`` — but what it says is about the bench's mains, and
            # the panel shows it in the harness panel, not against the run.
            #
            # Its own branch because the ``else`` below folds nothing: it writes
            # ``view.step``, and these carry no ``step``. Note what that does
            # *not* mean — the ``else`` is written ``fields.get("step", "") or
            # view.step``, so falling into it would leave the step intact rather
            # than blanking it. An earlier version of this comment claimed
            # otherwise, and a mutation removing the branch proved it wrong: the
            # step survived and the outlet map stayed empty. So the cost of losing
            # this branch is the whole mains fold, silently — the panel would show
            # a run power-cycling a DUT with every port still reading as it did
            # before the switch. That is the failure to guard against, and it is a
            # worse one than a blanked step.
            self.apply_run_outlet(event)
        elif kind == RUN_OUTLET_SETTLE_KIND_NAME:
            self.apply_run_outlet_settle(event)
        else:
            view.step = str(fields.get("step", "") or view.step)
        progress = _as_float(fields.get("progress"))
        if progress is not None:
            view.progress = progress

    def check_silence(self, *, now: Optional[float] = None) -> Optional[str]:
        """Mark the view stale if nothing has arrived recently.

        Called from the render loop rather than driven by a timer, so there is
        no thread here to leak. Returns the new stale reason, if any.

        The budget comes from the agent's own heartbeat interval when it told
        us one: a bench configured for slow heartbeats should not be reported
        as stale merely for honouring its own configuration.
        """
        if not self.connected:
            return self.stale_reason
        stamp = _mono(now)
        # `is not None`, not truthiness: a mark of exactly 0.0 is a real
        # timestamp, and `if m` discarded it. `time.monotonic()` is never 0.0 on
        # a running system so this could not fire in production, but a test
        # passing `now=0.0` — the obvious base for a synthetic clock — had its
        # first mark silently dropped, so the whole staleness assertion passed
        # against a view that was never checked. A guard that cannot be tested is
        # the failure mode this module exists to prevent.
        marks = [
            m
            for m in (self.last_event_mono, self.last_snapshot_mono)
            if m is not None
        ]
        if not marks:
            return self.stale_reason
        quiet = stamp - max(marks)
        budget = (
            self.heartbeat_s * SILENCE_HEARTBEATS
            if self.heartbeat_s
            else DEFAULT_SILENCE_S
        )
        if quiet > budget:
            self.stale_reason = (
                f"nothing from the agent for {quiet:.0f}s "
                f"(expected something within {budget:.0f}s)"
            )
        return self.stale_reason

    def to_dict(self) -> dict:
        """Flat snapshot, for logging and for tests to assert against."""
        # Ages, not raw monotonics: a monotonic from another process's clock is
        # meaningless to the renderer, and "3 s ago" is what the operator reads.
        # Computed here so there is one clock reading for the whole snapshot and
        # two rails cannot disagree about what "now" was.
        stamp = _mono(None)
        action_age = {
            key: max(0.0, stamp - at) for key, at in self.last_action_at.items()
        }
        return {
            "headline": self.headline,
            "severity": self.severity,
            "connected": self.connected,
            "starting": self.starting,
            "trustworthy": self.trustworthy,
            "stale_reason": self.stale_reason,
            "armed": self.armed_devices,
            "dropped_events": self.dropped_events,
            "actions_seen": self.actions_seen,
            "actions_folded": self.actions_folded,
            "link_beats": self.link_beats,
            "presence_sweeps": self.presence_sweeps,
            # {device_key: seconds since its last action} and what that action was.
            "action_age": action_age,
            "last_action": dict(self.last_action_name),
            "unsafe": self.unsafe_latch is not None,
            "observer_denied": self.observer_denied is not None,
            "devices": {
                k: {"armed": d.armed, "label": d.label, "inferred": d.inferred}
                for k, d in self.devices.items()
            },
            "slots": {
                k: {
                    "served": s.served,
                    "opened": s.opened,
                    "open_error": s.open_error,
                    "present": s.present,
                    "confidence": s.confidence,
                    "path": s.path,
                    "label": s.label,
                    "serial_number": s.serial_number,
                    "usb_id": s.usb_id,
                }
                for k, s in self.slots.items()
            },
            "inventory_taken": self.inventory_taken,
            "registry_known": self.registry_known,
            "unclaimed": list(self.unclaimed),
            # Core harness, not an instrument — a flat sub-dict of its own rather
            # than an entry in ``devices`` or ``slots``, because it is neither a
            # thing the bench measures with nor a slot on the instrument rail.
            #
            # ``known`` is the field the view turns on, and it is computed here
            # rather than left to the renderer to infer from an empty map: "no PDU
            # on this bench" and "a PDU we have not heard from" both produce empty
            # outlets, and the difference decides whether the panel shows a state
            # or hides itself. Inferring it downstream is how one of those two ends
            # up rendered as the other.
            #
            # ``age_s`` rather than the raw monotonic, matching ``action_age``
            # above: a monotonic from this process is meaningless to a browser, and
            # mains reads on a ~10 s clock of its own, so it goes stale while the
            # rest of the panel is current.
            "mains": {
                "known": self.mains.known,
                "device": self.mains.device,
                "outlets": {
                    str(i): on for i, on in sorted(self.mains.outlets.items())
                },
                "energised": self.mains.energised,
                "voltage_V": self.mains.voltage_V,
                "frequency_Hz": self.mains.frequency_Hz,
                "load_A": self.mains.load_A,
                "load_W": self.mains.load_W,
                "transport": self.mains.transport,
                "sweeps": self.mains.sweeps,
                "transitions": self.mains.transitions,
                "last_transition": (
                    dict(self.mains.last_transition)
                    if self.mains.last_transition
                    else None
                ),
                "settling_s": self.mains.settling_s,
                "age_s": (
                    max(0.0, stamp - self.mains.last_sweep_mono)
                    if self.mains.last_sweep_mono is not None
                    else None
                ),
            },
            "runs": {k: r.state for k, r in self.runs.items()},
            # {run_id: stage} for runs that have reported one, as a map alongside
            # ``runs`` rather than folded into it: that one is {id: state}, a bare
            # string several consumers index directly, and widening it to a dict
            # would churn all of them to carry one more field. Runs that never
            # reported a stage are absent rather than present-and-empty, so "the
            # bench has not said" stays distinguishable from any stage name.
            "run_stages": {
                k: r.stage for k, r in self.runs.items() if r.stage
            },
            # {run_id: name} and {run_id: dut}, parallel maps for the same reason.
            # ``run_dut`` keeps runs whose declared DUT is empty, because for those
            # the empty string is the answer — the run said nothing is named. A run
            # whose start we never saw is absent from both, so the display can tell
            # "declared nothing" from "we were not told".
            "run_names": {
                k: r.run_name for k, r in self.runs.items() if r.run_name
            },
            "run_dut": {
                k: r.dut for k, r in self.runs.items() if r.dut_known
            },
            # {device_key: run_id} for devices a live run declared it would drive.
            # Distinct from busy_devices on purpose: this holds for the run's whole
            # duration, including the dwells when no call is in flight.
            "enrolled": self.enrolled_devices,
            "busy": self.any_busy,
            "busy_devices": dict(self.busy_devices),
            "queued_devices": dict(self.queued_devices),
            "busy_summary": self.busy_summary,
        }


def _device_serial(dev: dict) -> Optional[str]:
    """The serial number of one ``agent.discover`` entry, however it is carried.

    The payload's own ``serial_number`` wins when it has one: it was read from
    the device's string descriptors, whereas the resource string is a backend's
    rendering of them.

    It usually does not have one. Measured against the bench board, every VISA
    instrument on it — DP2031, DL3031A, SDM4065A — reports
    ``serial_number: null`` while its serial sits in plain sight as the third
    field of the resource string it was found at. pyvisa's ``list_resources``
    does not open a resource, so nothing has read a descriptor at that point.
    Falling back to the resource string is what makes the rail show a serial on
    the instruments that make up most of this bench.
    """
    reported = dev.get("serial_number")
    if reported:
        return str(reported)
    path = dev.get("path")
    return _visa_serial(path) if isinstance(path, str) else None


def _visa_serial(resource: str) -> Optional[str]:
    """The serial-number field of a VISA USB resource string, or None.

    ``USB0::<vid>::<pid>::<serial>::<iface>::INSTR`` — but the interface number
    is **optional**, and which form you get depends on the backend rather than
    on the instrument. The same DP2031 is
    ``USB0::0x1AB1::0xA4A8::DP2A243500269::INSTR`` under NI-VISA and
    ``USB0::6833::42152::DP2A243500269::0::INSTR`` under pyvisa-py, so a parser
    that only handled the six-field form would show a serial on the bench board
    (which needs pyvisa-py, having no kernel ``usbtmc`` module) and nothing at
    all on a laptop with NI-VISA installed. Field 3 is the serial in both, which
    is why this indexes from the front rather than counting back from ``INSTR``.

    Splits on ``::`` rather than searching the string, the same rule
    :py:func:`benchctrl.discovery._visa_usb_ids` and the SDM4065A driver's
    ``_is_sdm4065a_resource`` follow: a serial number can contain the digits of
    a VID, so a substring match can land on the wrong field.

    Non-USB resources have no serial field at all — ``ASRL/dev/ttyS0::INSTR``,
    ``TCPIP0::192.168.1.5::inst0::INSTR`` — and get None. Inventing one from
    whatever is in that position would put a wrong serial on the rail, which is
    worse than an empty one: an operator can act on a blank, and would act
    wrongly on a plausible-looking lie.
    """
    parts = resource.split("::")
    if not parts[0].upper().startswith("USB"):
        return None
    if len(parts) < 4:
        return None
    serial = parts[3].strip()
    return serial or None


def _as_count(value: object) -> int:
    """How many actions one ``action`` event stands for: an int >= 1.

    Defaults to 1 rather than 0 for anything unusable. An event that arrived
    describes at least the one action that produced it, so counting it as zero
    would make the pane's total disagree with the rows visible above it.
    """
    if isinstance(value, bool) or not isinstance(value, int):
        return 1
    return value if value >= 1 else 1


def _strip_device_prefix(key: str, label: str) -> str:
    """``siglent_sdm4065a.measure_resistance_4wire`` → ``measure_resistance_4wire``.

    The worker's ``busy_with`` is the label ``WorkerPool.submit`` was given, which
    is already ``f"{key}.{name}"``. Left alone, the detail line read
    ``siglent_sdm4065a: siglent_sdm4065a.measure_resistance_4wire`` — observed on
    the board — and the doubling costs real width on a 1080p panel next to the
    device name the panel just printed.

    Strips only this device's own prefix, never on the first dot. A method name may
    contain one, and a blind ``split(".")[-1]`` would silently rename it; a label
    that is not prefixed at all is passed through unchanged rather than guessed at.
    """
    prefix = key + "."
    if label.startswith(prefix) and len(label) > len(prefix):
        return label[len(prefix) :]
    return label


def _mono(now: Optional[float]) -> float:
    return time.monotonic() if now is None else now


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None

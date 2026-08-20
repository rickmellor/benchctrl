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


@dataclass
class RunView:
    run_id: str
    name: str = ""
    state: str = "unknown"
    step: str = ""
    progress: Optional[float] = None


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
    runs: dict[str, RunView] = field(default_factory=dict)
    log: list[dict] = field(default_factory=list)
    sticky: list[dict] = field(default_factory=list)
    #: Count of events the agent told us it dropped. Cumulative and shown, so a
    #: chronically-behind panel is visibly chronic rather than briefly odd.
    dropped_events: int = 0
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
        # Absence is only recorded for slots we already know about. Anything the
        # scan did not find is present=False now — an authoritative negative,
        # which is exactly what makes a dark slot mean "looked, not there".
        for key, slot in self.slots.items():
            if key not in found:
                slot.present = False
                slot.confidence = None
                slot.path = None

        # De-duplicated by USB ID: the same instrument can surface on more than
        # one transport (a CH340 with no tty, plus its VISA alias).
        by_id: dict[str, dict] = {}
        for item in unclaimed:
            by_id.setdefault(item["usb_id"], item)
        self.unclaimed = [by_id[k] for k in sorted(by_id)]
        self.inventory_taken = True

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

    def apply_disconnected(self, reason: str = "") -> None:
        """The session went away. No events can arrive, so nothing is current.

        Device state is kept rather than cleared, because the last thing we
        knew is the best available guess and blanking it would make an armed
        bench look idle. It is all marked ``inferred`` so the panel renders it
        as unconfirmed.
        """
        self.connected = False
        # A finished attempt, however it finished, ends the startup grace: from
        # here on the panel has a fact to report rather than an unknown.
        self.attempted = True
        self.stale_reason = reason or "no session with the agent"
        for device in self.devices.values():
            device.inferred = True
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

        trips = safety.get("trips") or []
        if trips:
            self.last_trip = trips[-1]

        self.last_snapshot_mono = stamp
        if self.connected and self.unsafe_latch is None:
            self.stale_reason = None

    def apply_event(self, event: dict, *, now: Optional[float] = None) -> None:
        """Fold in one event frame."""
        stamp = _mono(now)
        self.last_event_mono = stamp
        kind = str(event.get("kind", ""))
        severity = str(event.get("severity", "info"))

        seq = event.get("seq")
        if isinstance(seq, int):
            self.last_seq = seq

        if kind == "events_dropped":
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
        elif kind in ("run_started", "run_step", "run_finished", "run_aborted"):
            self._apply_run_event(kind, event)

        self.log.append(event)
        if len(self.log) > LOG_LIMIT:
            del self.log[: -LOG_LIMIT]
        if severity in STICKY_SEVERITIES:
            self.sticky.append(event)
            if len(self.sticky) > LOG_LIMIT:
                del self.sticky[:-LOG_LIMIT]

    def _apply_run_event(self, kind: str, event: dict) -> None:
        run_id = str(event.get("run_id", "") or event.get("run", ""))
        if not run_id:
            return
        view = self.runs.setdefault(run_id, RunView(run_id=run_id))
        view.name = str(event.get("name", "") or view.name)
        if kind == "run_started":
            view.state = "running"
        elif kind == "run_finished":
            view.state = str(event.get("state", "finished"))
        elif kind == "run_aborted":
            view.state = "aborted"
        else:
            view.step = str(event.get("step", "") or view.step)
        progress = _as_float(event.get("progress"))
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
        marks = [m for m in (self.last_event_mono, self.last_snapshot_mono) if m]
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
        return {
            "headline": self.headline,
            "severity": self.severity,
            "connected": self.connected,
            "starting": self.starting,
            "trustworthy": self.trustworthy,
            "stale_reason": self.stale_reason,
            "armed": self.armed_devices,
            "dropped_events": self.dropped_events,
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
                }
                for k, s in self.slots.items()
            },
            "inventory_taken": self.inventory_taken,
            "registry_known": self.registry_known,
            "unclaimed": list(self.unclaimed),
            "runs": {k: r.state for k, r in self.runs.items()},
        }


def _mono(now: Optional[float]) -> float:
    return time.monotonic() if now is None else now


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None

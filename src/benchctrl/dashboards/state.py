"""What the panel knows, folded from an event stream.

Deliberately pure: no sockets, no Streamlit, no clock of its own beyond a
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
    agent_name: str = ""
    #: Monotonically increasing ``seq`` last seen. The event bus guarantees
    #: order is never permuted, so a decrease means a reconnect, not a bug.
    last_seq: Optional[int] = None

    # --- derived --------------------------------------------------------

    @property
    def armed_devices(self) -> list[str]:
        return sorted(k for k, d in self.devices.items() if d.armed)

    @property
    def any_armed(self) -> bool:
        return bool(self.armed_devices)

    @property
    def trustworthy(self) -> bool:
        return self.connected and self.stale_reason is None

    @property
    def headline(self) -> str:
        """One word for the top of the screen, worst-case first.

        Order matters and is the whole point: an unproven-unsafe output beats
        a staleness warning, which beats an armed-but-known state, which beats
        idle. The most dangerous true thing wins the largest text.
        """
        if self.unsafe_latch is not None:
            return "UNSAFE"
        if not self.connected:
            return "NO AGENT"
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
            "NO AGENT": "warn",
            "STALE": "warn",
            "ARMED": "alarm",
            "RECORDING": "info",
            "IDLE": "info",
        }[self.headline]

    # --- transitions ----------------------------------------------------

    def apply_connected(self, welcome: dict, *, now: Optional[float] = None) -> None:
        """A session came up. Clears staleness that a reconnect actually fixes."""
        self.connected = True
        self.agent_name = str(welcome.get("agent", ""))
        self.deadman_s = _as_float(welcome.get("deadman_s"))
        self.heartbeat_s = _as_float(welcome.get("heartbeat_s"))
        # A reconnect restarts the agent's seq counter, and the monotonicity
        # guarantee is per-session. Forget it rather than reporting a bogus gap.
        self.last_seq = None
        self.last_event_mono = _mono(now)
        # Connecting does NOT clear unsafe_latch: a fresh socket says nothing
        # about whether the output ever went off.
        self.stale_reason = "connected — waiting for the first status snapshot"

    def apply_disconnected(self, reason: str = "") -> None:
        """The session went away. No events can arrive, so nothing is current.

        Device state is kept rather than cleared, because the last thing we
        knew is the best available guess and blanking it would make an armed
        bench look idle. It is all marked ``inferred`` so the panel renders it
        as unconfirmed.
        """
        self.connected = False
        self.stale_reason = reason or "no session with the agent"
        for device in self.devices.values():
            device.inferred = True

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
            "trustworthy": self.trustworthy,
            "stale_reason": self.stale_reason,
            "armed": self.armed_devices,
            "dropped_events": self.dropped_events,
            "unsafe": self.unsafe_latch is not None,
            "devices": {
                k: {"armed": d.armed, "label": d.label, "inferred": d.inferred}
                for k, d in self.devices.items()
            },
            "runs": {k: r.state for k, r in self.runs.items()},
        }


def _mono(now: Optional[float]) -> float:
    return time.monotonic() if now is None else now


def _as_float(value: object) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None

"""Arm tracking and the deadman.

Locally, a dropped connection is not a hazard: the process that opened the
device is the process that died, and ``__exit__`` runs. Over a network the
host can vanish while the bench keeps driving current into a DUT, with
nothing watching. That is the failure this module exists for.

The agent tracks ``armed`` authoritatively, because it owns the driver
instances and sees every ``set_output(True)`` / ``set_input(True)`` go past.
Client bookkeeping is not trusted for this.

The honest limit
----------------
**Software cannot guarantee the output goes off.** If the driver thread is
wedged inside a blocking read, the governor cannot get a command out; and
closing the serial port does not command the Arc's output off — it holds its
last commanded state when the host disappears. The ladder below improves the
odds and reports honestly when it fails, but for an unattended overnight run
the only real guarantee is a hardware interlock: a relay on the DUT rail
driven from a GPIO, or the Arc's own GPO. Anything in this file that reads
like a promise is a best effort.
"""

from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Optional

from benchctrl.agent.worker import PRIORITY_SAFETY, DeviceWorker

log = logging.getLogger("benchctrl.agent.safety")

#: How long a priority safe_state may take before we escalate.
SAFE_STATE_TIMEOUT_S = 0.5

#: Grace after a socket closes before tripping. Short when armed.
GRACE_IDLE_S = 10.0
GRACE_ARMED_S = 3.0


class TripReason(str, Enum):
    HEARTBEAT_LOST = "heartbeat_lost"
    CLIENT_DISCONNECT = "client_disconnect"
    OPERATOR = "operator"
    SHUTDOWN = "shutdown"
    RUN_ABORT = "run_abort"


class TripOutcome(str, Enum):
    SAFE = "safe"  # device reached its safe state
    RECOVERED = "recovered"  # only after a transport reset
    FAILED = "failed"  # could not be made safe — operator action needed
    NOT_ARMED = "not_armed"  # nothing to do


@dataclass
class ArmState:
    """What a device is currently doing that could hurt something."""

    device_key: str
    output_armed: bool = False
    recording: bool = False
    emulating: bool = False
    last_armed_at: Optional[float] = None
    armed_by_session: Optional[str] = None

    @property
    def is_armed(self) -> bool:
        return self.output_armed or self.emulating

    def to_dict(self) -> dict:
        return {
            "device": self.device_key,
            "output_armed": self.output_armed,
            "recording": self.recording,
            "emulating": self.emulating,
            "armed": self.is_armed,
            "armed_by_session": self.armed_by_session,
        }


#: Methods whose invocation changes the arm state, and what they set it to.
#: Watched by ``observe_call`` so arming is inferred from the wire rather
#: than trusted from the client.
_ARMING_CALLS: dict[str, str] = {
    "set_output": "output",
    "enable_output": "output",
    "disable_output": "output_off",
    "set_input": "output",  # electronic loads sink, which is equally live
}


@dataclass
class SafetyGovernor:
    """Tracks arm state and drives devices to safety when contact is lost."""

    deadman_s: float = 15.0
    safe_state_timeout_s: float = SAFE_STATE_TIMEOUT_S
    on_event: Optional[Callable[[dict], None]] = None

    _states: dict[str, ArmState] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _last_contact: float = field(default_factory=time.monotonic)
    _trips: list[dict] = field(default_factory=list)

    # --- arm tracking ---------------------------------------------------

    def state_for(self, device_key: str) -> ArmState:
        with self._lock:
            state = self._states.get(device_key)
            if state is None:
                state = ArmState(device_key=device_key)
                self._states[device_key] = state
            return state

    def observe_call(
        self,
        device_key: str,
        method: str,
        args: tuple,
        kwargs: dict,
        *,
        session_id: Optional[str] = None,
    ) -> None:
        """Update arm state from a call that just succeeded."""
        kind = _ARMING_CALLS.get(method)
        if kind is None:
            return
        enabled = _first_bool(args, kwargs)
        if kind == "output_off":
            enabled = False
        state = self.state_for(device_key)
        with self._lock:
            was = state.is_armed
            state.output_armed = bool(enabled)
            if state.output_armed:
                state.last_armed_at = time.monotonic()
                state.armed_by_session = session_id
            elif not state.is_armed:
                state.armed_by_session = None
            if was != state.is_armed:
                log.info(
                    "safety: %s %s (via %s)",
                    device_key,
                    "ARMED" if state.is_armed else "disarmed",
                    method,
                )

    def set_recording(self, device_key: str, active: bool) -> None:
        with self._lock:
            self.state_for(device_key).recording = active

    def set_emulating(self, device_key: str, active: bool, session_id=None) -> None:
        state = self.state_for(device_key)
        with self._lock:
            state.emulating = active
            if active:
                state.last_armed_at = time.monotonic()
                state.armed_by_session = session_id

    @property
    def armed_devices(self) -> list[str]:
        with self._lock:
            return [k for k, s in self._states.items() if s.is_armed]

    @property
    def any_armed(self) -> bool:
        return bool(self.armed_devices)

    # --- deadman --------------------------------------------------------

    def touch(self) -> None:
        """Record contact from a client. Any inbound frame counts."""
        with self._lock:
            self._last_contact = time.monotonic()

    @property
    def seconds_since_contact(self) -> float:
        with self._lock:
            return time.monotonic() - self._last_contact

    def should_trip(self) -> bool:
        return self.any_armed and self.seconds_since_contact > self.deadman_s

    def grace_for_disconnect(self) -> float:
        """Seconds to wait after a socket closes before tripping."""
        return GRACE_ARMED_S if self.any_armed else GRACE_IDLE_S

    # --- the ladder -----------------------------------------------------

    def trip(
        self,
        reason: TripReason,
        devices: dict[str, Any],
        workers: dict[str, DeviceWorker],
        *,
        safe_state_fns: Optional[dict[str, Callable[[Any], None]]] = None,
    ) -> dict[str, TripOutcome]:
        """Drive every armed device to its safe state.

        Returns per-device outcomes. Never raises: a governor that can throw
        is a governor that can skip the rest of the bench.
        """
        outcomes: dict[str, TripOutcome] = {}
        targets = self.armed_devices
        if not targets:
            return outcomes

        log.warning("safety: TRIP (%s) — armed devices: %s", reason.value, targets)
        self._emit(
            {
                "kind": "safety_trip",
                "severity": "critical",
                "reason": reason.value,
                "devices": targets,
            }
        )

        for key in targets:
            obj = devices.get(key)
            worker = workers.get(key)
            if obj is None or worker is None:
                outcomes[key] = TripOutcome.FAILED
                continue
            fn = (safe_state_fns or {}).get(key) or default_safe_state
            outcomes[key] = self._make_safe(key, obj, worker, fn)

        self._trips.append(
            {
                "reason": reason.value,
                "outcomes": {k: v.value for k, v in outcomes.items()},
                "at": time.time(),
            }
        )
        return outcomes

    def _make_safe(
        self,
        device_key: str,
        obj: Any,
        worker: DeviceWorker,
        safe_state: Callable[[Any], None],
    ) -> TripOutcome:
        # Step 1 — priority job, jumps the queue.
        job = worker.submit_nowait(
            lambda: safe_state(obj),
            priority=PRIORITY_SAFETY,
            label="safe_state",
        )
        if job.done.wait(timeout=self.safe_state_timeout_s):
            if job.error is None:
                self._clear(device_key)
                log.info("safety: %s reached safe state", device_key)
                return TripOutcome.SAFE
            log.error("safety: %s safe_state raised: %r", device_key, job.error)
        else:
            log.error(
                "safety: %s did not respond within %.1fs (busy with %r) — "
                "escalating to transport reset",
                device_key,
                self.safe_state_timeout_s,
                worker.busy_with,
            )

        # Step 2 — the worker is wedged or the command failed. Reset the
        # transport out-of-band and retry. This bypasses the queue by design.
        outcome = self._reset_and_retry(device_key, obj, safe_state)
        if outcome is TripOutcome.RECOVERED:
            self._clear(device_key)
            return outcome

        # Step 3 — could not make it safe. Say so, loudly and repeatedly.
        log.critical(
            "safety: %s COULD NOT BE MADE SAFE. The output may still be live. "
            "Physically disconnect the DUT. This is the case a hardware "
            "interlock exists for.",
            device_key,
        )
        self._emit(
            {
                "kind": "safety_failed",
                "severity": "critical",
                "device": device_key,
                "guidance": (
                    "Output may still be live — physically disconnect the DUT."
                ),
            }
        )
        return TripOutcome.FAILED

    def _reset_and_retry(
        self, device_key: str, obj: Any, safe_state: Callable[[Any], None]
    ) -> TripOutcome:
        transport = getattr(obj, "_transport", None)
        try:
            if transport is not None:
                transport.close()
                transport.open()
                connect = getattr(obj, "_connect", None)
                if callable(connect):
                    connect()
            safe_state(obj)
            log.warning("safety: %s recovered after a transport reset", device_key)
            return TripOutcome.RECOVERED
        except Exception as exc:  # noqa: BLE001
            log.error("safety: %s transport reset failed: %r", device_key, exc)
            return TripOutcome.FAILED

    def _clear(self, device_key: str) -> None:
        with self._lock:
            state = self.state_for(device_key)
            state.output_armed = False
            state.emulating = False
            state.armed_by_session = None

    def _emit(self, event: dict) -> None:
        if self.on_event is None:
            return
        try:
            self.on_event(event)
        except Exception:  # pragma: no cover - event sink must never break safety
            log.exception("safety: event sink raised")

    # --- reporting ------------------------------------------------------

    def status(self) -> dict:
        with self._lock:
            return {
                "armed": self.armed_devices,
                "seconds_since_contact": round(self.seconds_since_contact, 3),
                "deadman_s": self.deadman_s,
                "devices": {k: s.to_dict() for k, s in self._states.items()},
                "trips": list(self._trips[-10:]),
            }


def default_safe_state(obj: Any) -> None:
    """Best-effort "stop driving" for any instrument.

    Tries each known disarm in turn and does not stop at the first failure —
    a device that rejects ``set_output`` may still honour ``set_input``, and
    on a safety path every avenue is worth taking.
    """
    errors = []
    for method, args in (
        ("stop_recording", ()),
        ("set_output", (False,)),
        ("set_input", (False,)),
        ("set_current_limit_enabled", (True,)),
    ):
        fn = getattr(obj, method, None)
        if not callable(fn):
            continue
        try:
            fn(*args)
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{method}: {exc!r}")
    if errors:
        log.debug("safe_state partial failures: %s", "; ".join(errors))


def _first_bool(args: tuple, kwargs: dict) -> bool:
    """Extract the on/off argument from a call like ``set_output(True)``."""
    for value in args:
        if isinstance(value, bool):
            return value
    for key in ("enable", "on", "state", "enabled"):
        if key in kwargs:
            return bool(kwargs[key])
    # A no-argument enable_output() means "on".
    return True

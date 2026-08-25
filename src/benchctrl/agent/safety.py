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
#:
#: **``set_outlet_state`` is deliberately absent.** Energising a mains outlet
#: is not "arming an output": nothing is being driven, and the instrument on
#: the other side of that outlet arms itself separately and is tracked on its
#: own key. Adding it here would start a deadman countdown on every switch —
#: so an operator who powered up the bench and then went to lunch would come
#: back to a tripped governor — and it would put the PDU in
#: :py:attr:`armed_devices`, where :py:func:`default_safe_state` would be
#: called on it. That call is inert (see below), so the visible effect would
#: be a device that is permanently armed and can never be disarmed.
_ARMING_CALLS: dict[str, str] = {
    "set_output": "output",
    "enable_output": "output",
    "disable_output": "output_off",
    "set_input": "output",  # electronic loads sink, which is equally live
}

#: How long a panic outlet cut may take to *confirm*, after the commands have
#: all been sent. Nothing like :py:data:`SAFE_STATE_TIMEOUT_S`, and the gap is
#: not slack: ``oltctrl index N act off`` honours the outlet's configured
#: ``td_off`` (3 s as shipped, operator-settable), so a PDU physically cannot
#: report the cut inside half a second. Sized to cover the shipped delay plus
#: a couple of read round trips.
PANIC_CUT_CONFIRM_S = 8.0

#: Allowance per outlet for getting the cut *command* out, on top of the
#: confirm window. The measured ``oltctrl`` round trip is ~0.62 s; this is
#: roughly double, because a panic cut is not the moment to discover the
#: budget was tight.
PANIC_CUT_PER_OUTLET_S = 1.5


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

        # A device that authorised a panic outlet cut is a target even though
        # it is never *armed* — energising an outlet is not arming an output
        # (see `_ARMING_CALLS`), so the PDU would otherwise never appear here
        # and `panic_outlets` would be a setting that did nothing. Ordered
        # after the armed devices deliberately: turn off what is driving
        # current before cutting the mains feeding it.
        panic_targets = [
            key
            for key, obj in devices.items()
            if key not in targets and panic_outlets_of(obj)
        ]

        if not targets:
            # No armed device means no trip, and therefore no cut. A lost
            # heartbeat on an idle bench must not power anything down: the
            # panic cut exists to stop an *unattended live output*, and there
            # is no live output here.
            return outcomes

        log.warning("safety: TRIP (%s) — armed devices: %s", reason.value, targets)
        self._emit(
            {
                "kind": "safety_trip",
                "severity": "critical",
                "reason": reason.value,
                "devices": targets,
                "panic_outlet_devices": panic_targets,
            }
        )

        for key in list(targets) + panic_targets:
            obj = devices.get(key)
            worker = workers.get(key)
            if obj is None or worker is None:
                outcomes[key] = TripOutcome.FAILED
                continue
            fn = (safe_state_fns or {}).get(key)
            timeout_s = self.safe_state_timeout_s
            if fn is None and key in panic_targets:
                fn, timeout_s = _panic_cut_for(obj)
            outcomes[key] = self._make_safe(
                key, obj, worker, fn or default_safe_state, timeout_s=timeout_s
            )

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
        *,
        timeout_s: Optional[float] = None,
    ) -> TripOutcome:
        # `timeout_s` is per-device because a mains contactor and a register
        # write are not the same order of magnitude — see `_panic_cut_for`.
        timeout_s = self.safe_state_timeout_s if timeout_s is None else timeout_s
        # Step 1 — priority job, jumps the queue.
        job = worker.submit_nowait(
            lambda: safe_state(obj),
            priority=PRIORITY_SAFETY,
            label="safe_state",
        )
        if job.done.wait(timeout=timeout_s):
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
                timeout_s,
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
        # Not just `is not None`: the PDU41002's `_transport` is the *string*
        # `"serial"` or `"ssh"` — a name collision, since its byte pipe is
        # `_link`. Duck-typing on the attribute alone would call `.close()` on
        # a str, and the resulting AttributeError would be caught below and
        # reported as "transport reset failed", hiding the fact that the retry
        # never happened. Check for the methods, not the name.
        if not callable(getattr(transport, "close", None)) or not callable(
            getattr(transport, "open", None)
        ):
            transport = None
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

    **This is inert on the PDU41002, and that is the intended behaviour.** The
    PDU implements none of these four methods, so the loop below skips every
    one of them and the function does nothing — a quiet accident of ``getattr``
    that happens to be exactly right, and therefore needs saying out loud
    before somebody "fixes" the omission.

    For every other instrument here, safe means *stop sourcing or sinking*.
    For a PDU, cutting mains **is** the disruptive act: it de-powers a DUT
    mid-measurement and drops the other instruments' sessions along with it.
    A safe state that cut outlets would turn one lost heartbeat into a
    bench-wide power failure, including outlets an operator never authorised
    the governor to touch.

    Cutting mains on a trip is available, opt-in and per-outlet:
    :py:func:`panic_outlet_safe_state`, driven by the driver's
    ``panic_outlets``, which is empty by default and must be a subset of
    ``allowed_outlets``. Wire it in through ``trip(safe_state_fns=...)``, not
    by adding outlet calls here.
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


def panic_outlets_of(obj: Any) -> frozenset:
    """The outlets ``obj`` has authorised the governor to cut, or empty.

    Duck-typed on purpose: :py:mod:`benchctrl.agent.safety` must not import a
    driver, and "has a ``panic_outlets`` property" is the whole contract. Any
    non-iterable or absent attribute reads as "no authorisation", so a device
    that knows nothing about this is untouched.
    """
    outlets = getattr(obj, "panic_outlets", None)
    if not outlets:
        return frozenset()
    try:
        return frozenset(int(n) for n in outlets)
    except (TypeError, ValueError):  # pragma: no cover - defensive
        log.error("safety: %r has an unusable panic_outlets; ignoring it", obj)
        return frozenset()


def _panic_cut_for(obj: Any) -> tuple[Callable[[Any], None], float]:
    """A cut function for ``obj`` and the worker timeout it needs.

    The timeout is the reason this is not a one-liner. Instruments get
    :py:data:`SAFE_STATE_TIMEOUT_S` (half a second) to disarm, which is
    generous for a register write and *physically impossible* for a contactor
    honouring a 3 s ``td_off``. Reusing it would make every trip escalate to a
    transport reset — reconnecting the link mid-cut — and report ``RECOVERED``
    at best. So the budget is derived from the outlet count instead.
    """
    budget = PANIC_CUT_CONFIRM_S + PANIC_CUT_PER_OUTLET_S * len(panic_outlets_of(obj))

    def cut(target: Any) -> None:
        panic_outlet_safe_state(target, deadline_s=budget)

    return cut, budget + 2.0


def panic_outlet_safe_state(obj: Any, *, deadline_s: float) -> None:
    """Cut every outlet in ``obj.panic_outlets``, then prove they are off.

    Commands first, verification second — deliberately not
    ``set_outlet_state(n, False, verify=True)`` in a loop. Each outlet honours
    its own configured ``td_off`` (3 s as shipped), so verifying one at a time
    makes the second outlet wait out the first one's settle. On a safety path
    the thing that matters is getting the *commands* out; confirmation can
    overlap.

    Raises if any outlet cannot be confirmed off, so the caller reports
    ``FAILED`` rather than ``SAFE``. Reporting a DUT as de-powered when its
    contactor never moved is the one outcome worse than failing loudly.
    """
    outlets = sorted(panic_outlets_of(obj))
    if not outlets:
        return

    log.warning("safety: cutting panic outlets %s", outlets)
    unsent: dict[int, str] = {}
    for outlet in outlets:
        try:
            # verify=False here only because the confirmation loop below does
            # it for all of them at once. It is not "unverified".
            obj.set_outlet_state(outlet, False, verify=False)
        except Exception as exc:  # noqa: BLE001
            # Keep going: the other outlets are still worth cutting, and one
            # refused outlet must not leave the rest live.
            unsent[outlet] = repr(exc)
            log.error("safety: could not command outlet %d off: %r", outlet, exc)

    deadline = time.monotonic() + deadline_s
    pending = set(outlets)
    last_error: Optional[str] = None
    while pending:
        for outlet in sorted(pending):
            try:
                if obj.outlet_state(outlet) is False:
                    pending.discard(outlet)
            except Exception as exc:  # noqa: BLE001
                last_error = repr(exc)
        if not pending or time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    if pending:
        detail = f" (last read error: {last_error})" if last_error else ""
        refused = f" commands refused for {sorted(unsent)}:" if unsent else ""
        raise RuntimeError(
            f"panic outlets {sorted(pending)} did not read off within "
            f"{deadline_s:.1f}s.{refused}{detail} Mains may still be live on "
            f"them."
        )
    log.warning("safety: panic outlets %s confirmed off", outlets)


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

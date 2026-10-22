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
from collections.abc import Iterable
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
    #: A run engine stopped proving it is alive: its tick loop has not touched
    #: its own ``run:<id>`` owner for ``deadman_s``. The engine is wedged, so
    #: its device is driven safe by the governor rather than by the engine.
    RUN_WATCHDOG = "run_watchdog"


#: Owner-key prefix for a run engine. A session's owner key is its session id.
RUN_OWNER_PREFIX = "run:"


def run_owner(run_id: str) -> str:
    return f"{RUN_OWNER_PREFIX}{run_id}"


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
    #: The **owner** whose silence trips this device: a session id, or
    #: ``run:<id>`` for a run engine. ``None`` means nobody in particular, and
    #: such a device is kept alive by contact from anyone, as every device was
    #: before contact was tracked per owner. The name predates run owners and
    #: is kept because it is on the wire (``safety.status``).
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
    "disable_outputs": "output_off",  # the all-channels disarm (DP2031, SDG1032X)
    "set_input": "output",  # electronic loads sink, which is equally live
}

#: **``set_relay_state`` and ``set_relay_port`` are deliberately absent too**,
#: for the ADU218, and for a different reason than the PDU's above.
#:
#: Closing a 1 A signal relay is not arming an output — nothing is sourced or
#: sunk, and whatever the relay connects arms itself on its own device key. The
#: sharper point is what including them would break: the ADU218's *hardware*
#: watchdog already de-energises every relay if the device hears nothing for the
#: configured interval, with no software in the decision path. A governor
#: countdown started by a relay switch would be a second, weaker deadman layered
#: over a strictly better one — weaker because it needs a live agent to fire,
#: which is the failure it would be claiming to cover.
#:
#: Verified rather than assumed: the intersection of this dict's keys with the
#: ADU218's method surface is empty, and ``tests/test_bench_adu218.py`` asserts
#: that so a later rename cannot quietly create the overlap.

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
    #: Device keys the governor never treats as armed and never drives to a
    #: safe state — **bench infrastructure**, declared per bench in
    #: ``agent.json`` ``safe_stop_exempt``. The motivating case is a bench
    #: supply that powers the NPU's fan and the NPU itself: "safe" for a DUT
    #: source is "off", and off is exactly what that bench must not do on a
    #: service restart or a lost session. Same reasoning as the PDU's
    #: ``set_outlet_state`` being absent from :py:data:`_ARMING_CALLS`.
    exempt: frozenset[str] = frozenset()

    #: Monotonic seconds. Injectable so deadman and watchdog timing can be
    #: tested against a synthetic clock instead of by sleeping.
    clock: Callable[[], float] = time.monotonic

    _states: dict[str, ArmState] = field(default_factory=dict)
    _lock: threading.RLock = field(default_factory=threading.RLock)
    _last_contact: Optional[float] = None
    #: Last contact per owner. A device armed by an owner is kept alive by
    #: that owner alone, so one chatty client cannot hold another client's
    #: abandoned output on.
    _contact: dict[str, float] = field(default_factory=dict)
    _trips: list[dict] = field(default_factory=list)

    def __post_init__(self) -> None:
        if self._last_contact is None:
            self._last_contact = self.clock()

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
        if device_key in self.exempt:
            log.debug(
                "safety: %s is safe_stop_exempt — %s not tracked as arming", device_key, method
            )
            return
        enabled = _first_bool(args, kwargs)
        if kind == "output_off":
            enabled = False
        state = self.state_for(device_key)
        with self._lock:
            was = state.is_armed
            state.output_armed = bool(enabled)
            if state.output_armed:
                state.last_armed_at = self.clock()
                state.armed_by_session = session_id
                self._seed_contact(session_id)
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
                state.last_armed_at = self.clock()
                state.armed_by_session = session_id
                self._seed_contact(session_id)

    @property
    def armed_devices(self) -> list[str]:
        with self._lock:
            return [k for k, s in self._states.items() if s.is_armed]

    @property
    def any_armed(self) -> bool:
        return bool(self.armed_devices)

    # --- deadman --------------------------------------------------------

    def touch(self, owner: Optional[str] = None) -> None:
        """Record contact. ``owner`` is a session id or ``run:<id>``.

        Every touch counts as contact for devices armed with no owner; a touch
        with an owner also keeps that owner's own devices alive. Nothing else
        does: contact from session B is not evidence that session A is alive.
        """
        now = self.clock()
        with self._lock:
            self._last_contact = now
            if owner is not None:
                self._contact[owner] = now

    def _seed_contact(self, owner: Optional[str]) -> None:
        # Arming is contact: the owner just acted. Without this, an owner that
        # arms before its first touch would be judged by a missing entry.
        if owner is not None:
            self._contact[owner] = self.clock()

    def owner_of(self, device_key: str) -> Optional[str]:
        with self._lock:
            state = self._states.get(device_key)
            return state.armed_by_session if state is not None and state.is_armed else None

    def transfer_owner(self, device_key: str, owner: str) -> bool:
        """Hand an armed device to ``owner``, who becomes the one keeping it alive.

        Used when a new session claims a device that a departed session left
        armed, so a client that reconnects (and so gets a new session id) takes
        over its own output instead of having it trip under it. Refreshes the
        new owner's contact; the clock does not run backwards for the device.
        """
        with self._lock:
            state = self._states.get(device_key)
            if state is None or not state.is_armed:
                return False
            state.armed_by_session = owner
            self._seed_contact(owner)
            return True

    def forget_owner(self, owner: str) -> None:
        """Drop ``owner``'s contact record once it owns nothing armed."""
        with self._lock:
            if not any(s.is_armed and s.armed_by_session == owner for s in self._states.values()):
                self._contact.pop(owner, None)

    @property
    def seconds_since_contact(self) -> float:
        """Seconds since contact from anyone (the bench-wide figure on displays)."""
        with self._lock:
            return self.clock() - self._last_contact

    def seconds_since_owner_contact(self, owner: Optional[str]) -> float:
        with self._lock:
            if owner is None:
                return self.clock() - self._last_contact
            # An owner with no record has never been heard from since arming
            # was tracked per owner; judge it from the bench-wide figure rather
            # than treating it as infinitely stale.
            last = self._contact.get(owner, self._last_contact)
            return self.clock() - last

    def stale_devices(self) -> dict[str, Optional[str]]:
        """Armed devices whose owner has been silent past ``deadman_s``.

        Returns ``{device_key: owner}``. Only these trip: a stale owner's
        devices, never a live owner's.
        """
        with self._lock:
            return {
                key: state.armed_by_session
                for key, state in self._states.items()
                if state.is_armed
                and self.seconds_since_owner_contact(state.armed_by_session) > self.deadman_s
            }

    def should_trip(self) -> bool:
        return bool(self.stale_devices())

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
        only: Optional[Iterable[str]] = None,
    ) -> dict[str, TripOutcome]:
        """Drive every armed device (or the armed ones in ``only``) to its safe state.

        ``only`` targets a trip at the devices of one stale owner, or one
        device being closed. Panic outlets are cut only by a trip that leaves
        nothing armed: cutting mains under a live owner's working output
        would turn one client's silence into an outage for another.

        Returns per-device outcomes. Never raises: a governor that can throw
        is a governor that can skip the rest of the bench.
        """
        outcomes: dict[str, TripOutcome] = {}
        armed = self.armed_devices
        wanted = None if only is None else set(only)
        targets = armed if wanted is None else [k for k in armed if k in wanted]
        bench_wide = len(targets) == len(armed)

        # A device that authorised a panic outlet cut is a target even though
        # it is never *armed* — energising an outlet is not arming an output
        # (see `_ARMING_CALLS`), so the PDU would otherwise never appear here
        # and `panic_outlets` would be a setting that did nothing. Ordered
        # after the armed devices deliberately: turn off what is driving
        # current before cutting the mains feeding it.
        panic_candidates = [
            key for key, obj in devices.items() if key not in targets and panic_outlets_of(obj)
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
                "panic_outlet_devices": panic_candidates if bench_wide else [],
            }
        )

        for key in targets:
            outcomes[key] = self._make_safe_target(key, devices, workers, safe_state_fns)

        # A targeted trip leaves other owners' outputs working, so it cuts
        # mains only when one of its own targets could not be made safe: a
        # live output nobody can turn off is what the panic cut is for.
        if bench_wide or any(v is TripOutcome.FAILED for v in outcomes.values()):
            for key in panic_candidates:
                outcomes[key] = self._make_safe_target(
                    key, devices, workers, safe_state_fns, panic=True
                )

        self._trips.append(
            {
                "reason": reason.value,
                "outcomes": {k: v.value for k, v in outcomes.items()},
                "at": time.time(),
            }
        )
        return outcomes

    def _make_safe_target(
        self,
        key: str,
        devices: dict[str, Any],
        workers: dict[str, DeviceWorker],
        safe_state_fns: Optional[dict[str, Callable[[Any], None]]],
        *,
        panic: bool = False,
    ) -> TripOutcome:
        obj = devices.get(key)
        worker = workers.get(key)
        if obj is None or worker is None:
            return TripOutcome.FAILED
        fn = (safe_state_fns or {}).get(key)
        timeout_s = self.safe_state_timeout_s
        if fn is None and panic:
            fn, timeout_s = _panic_cut_for(obj)
        return self._make_safe(key, obj, worker, fn or default_safe_state, timeout_s=timeout_s)

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
                "guidance": ("Output may still be live — physically disconnect the DUT."),
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
                "owners": {
                    owner: round(self.clock() - at, 3) for owner, at in self._contact.items()
                },
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

    **It is also inert on the ADU218, and there the case is stronger.** That
    device implements none of these four either, so the loop skips it — and
    adding ``reset_relays()`` here would be actively worse than the omission.
    The ADU218 carries a *hardware* watchdog that de-energises all eight relays
    when the device stops hearing commands, and a governor trip is very often the
    same silence: an agent that has stopped talking to its devices is also not
    feeding the watchdog. So the relays already drop, by a mechanism that works
    when this function cannot run at all. A software de-energise here would add
    nothing in the case it fires and would mask, in testing, the fact that the
    interlock is the thing actually protecting the bench.
    """
    errors = []
    # ``disable_outputs()`` comes first and takes no arguments: a multi-channel
    # instrument's ``set_output(channel, on)`` cannot be reached by the
    # single-argument ``set_output(False)`` below (the DP2031 rejects a bool
    # channel), which left the PSU armed across a service stop until the
    # no-argument disarm was added. Every output-bearing driver with channels
    # provides it.
    for method, args in (
        ("stop_recording", ()),
        ("disable_outputs", ()),
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

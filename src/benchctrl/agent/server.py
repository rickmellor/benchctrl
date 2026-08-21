"""The agent: a threaded TCP server exposing the bench.

One connection per client, one thread per connection, one worker thread per
device. Sessions are authenticated, and exactly one may hold the *writer
claim* per device; the rest are read-only observers. That preserves the
single-writer semantics the MCP layer already assumes while letting a second
client watch a long run without being able to disturb it.

Stdlib only — no asyncio, no framework. The board runs Python 3.13 with
nothing but ``pyserial`` installed, and the MCP stack cannot be installed
there because its dependencies ship compiled wheels for the wrong
architecture. Keeping the agent to the standard library is what makes it
deployable at all.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import socketserver
import threading
import time
import traceback
from typing import Any, Optional

from benchctrl.agent import dispatch
from benchctrl.agent.blobs import CHUNK_BYTES, BlobStore
from benchctrl.agent.eventbus import (
    DEFAULT_MAX_QUEUE,
    DISPLAY_MAX_QUEUE,
    EventBus,
    EventSubscriber,
)
from benchctrl.agent.recordings import PREVIEW_HZ, IteratorTable, RecordingTable
from benchctrl.agent.registry import DeviceRegistry
from benchctrl.agent.runs.engine import RunManager
from benchctrl.agent.runs.spec import RunSpec
from benchctrl.agent.safety import SafetyGovernor, TripReason
from benchctrl.agent.worker import PRIORITY_NORMAL, WorkerPool, clamp_blocking
from benchctrl.exceptions import BenchValueError
from benchctrl.net import auth as authmod
from benchctrl.net.codec import Decoder, Encoder
from benchctrl.net.errors import PolicyError, encode_exception
from benchctrl.net.frames import FrameReader, FrameType, FrameWriter

log = logging.getLogger("benchctrl.agent.server")

AGENT_NAME = "benchctrl-agent"


def _json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")


#: Methods an observer session may call. Read-only by construction: every one
#: of these only reports state, so an observer cannot change what it observes.
#:
#: Deliberately a strict allowlist rather than a denylist of mutating verbs. A
#: new method added later defaults to *forbidden* for observers, which is the
#: safe direction — the alternative silently grants access to every verb someone
#: forgets to add to a blocklist. Same reasoning as the value-codec allowlist in
#: ``net.codec``.
OBSERVER_METHODS = frozenset(
    {
        "agent.hello",
        "agent.devices",
        "agent.status",
        "agent.time",
        "agent.discover",
        "run.list",
        "run.status",
        "run.events",
    }
)


class Session:
    """Per-connection state.

    ``observer`` sessions are read-only status consumers — the HDMI dashboard is
    the motivating case. Two things make them different from a normal client,
    and both are safety properties rather than conveniences:

    1. **Their traffic does not count as operator contact.** ``_serve`` calls
       ``governor.touch()`` on every inbound frame from a normal session, so a
       polling observer would pin ``seconds_since_contact`` near zero and the
       deadman could never trip. A status display must not be able to keep an
       armed bench alive.
    2. **They may only call** :py:data:`OBSERVER_METHODS`. No opening, no
       claiming, no device calls.
    """

    _ids = itertools.count(1)

    def __init__(
        self,
        agent: "BenchAgent",
        sock: socket.socket,
        address,
        *,
        observer: bool = False,
    ) -> None:
        self.agent = agent
        self.sock = sock
        self.address = address
        self.session_id = f"s-{next(self._ids)}"
        self.reader = FrameReader(sock)
        self.writer = FrameWriter(sock)
        self.authenticated = False
        self.observer = observer
        self.claims: set[str] = set()
        self.opened_at = time.monotonic()
        self._event_seq = itertools.count(1)

    @property
    def peer(self) -> str:
        try:
            return self.address[0]
        except (TypeError, IndexError):  # pragma: no cover
            return str(self.address)

    def holds(self, device_key: str) -> bool:
        return device_key in self.claims

    def send_event(self, event: dict) -> None:
        """Write one event frame. **Blocks** — only the sender thread calls it.

        Called from this session's :py:class:`~benchctrl.agent.eventbus.EventSubscriber`
        thread, never from a producer. ``sendall`` has no timeout, so calling
        this from the governor's trip path is what used to let a stalled client
        delay a disarm.
        """
        payload = dict(event)
        payload.setdefault("seq", next(self._event_seq))
        payload.setdefault("ts_mono", round(time.monotonic(), 6))
        payload.setdefault("ts_utc", time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()))
        payload.setdefault("severity", "info")
        self.writer.send(FrameType.EVT, _json(payload))


class BenchAgent:
    """Owns the devices, the workers, and the safety governor."""

    def __init__(
        self,
        registry: DeviceRegistry,
        *,
        token: Optional[str] = None,
        deadman_s: float = 15.0,
        heartbeat_s: float = 5.0,
        max_blocking_s: float = 5.0,
        max_recording_s: float = 300.0,
        blob_store: Optional[BlobStore] = None,
        runs_dir=None,
        llm_base_url: str = "",
    ) -> None:
        self.registry = registry
        self.token = token
        self.deadman_s = deadman_s
        self.heartbeat_s = heartbeat_s
        self.max_blocking_s = max_blocking_s
        self.workers = WorkerPool(max_blocking_s=max_blocking_s)
        self.blobs = blob_store or BlobStore()
        self.recordings = RecordingTable(max_recording_s=max_recording_s)
        self.iterators = IteratorTable()
        self.runs = RunManager(runs_dir)
        self.llm_base_url = llm_base_url
        self.failures = authmod.FailureTracker()
        # Built before the governor: the governor's event sink publishes onto it.
        self.events = EventBus()
        self.governor = SafetyGovernor(
            deadman_s=deadman_s, on_event=self._broadcast_event
        )
        self._sessions: dict[str, Session] = {}
        self._claims: dict[str, str] = {}  # device -> session id
        self._lock = threading.RLock()
        self._deadman_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        # The agent's one VISA ResourceManager, built lazily by
        # :py:meth:`resource_manager`. Its own lock, not ``self._lock``: see
        # there for why.
        self._rm: Optional[Any] = None
        self._rm_tried = False
        self._rm_lock = threading.Lock()

    # --- lifecycle ------------------------------------------------------

    def start_deadman(self) -> None:
        if self._deadman_thread is not None:
            return
        self._stop.clear()
        self._deadman_thread = threading.Thread(
            target=self._deadman_loop, name="deadman", daemon=True
        )
        self._deadman_thread.start()

    def _deadman_loop(self) -> None:
        while not self._stop.wait(0.5):
            try:
                if self.governor.should_trip():
                    self.trip(TripReason.HEARTBEAT_LOST)
            except Exception:  # pragma: no cover
                log.exception("deadman loop raised")

    def trip(self, reason: TripReason) -> dict:
        return self.governor.trip(
            reason,
            self.registry.open_devices(),
            {k: self.workers.get(k) for k in self.registry.open_devices()},
        )

    def resource_manager(self) -> Optional[Any]:
        """The agent's single VISA ``ResourceManager``, or None if there can't be one.

        Exists because ``pyvisa.ResourceManager()`` is a **singleton** — a second
        call returns the same underlying object — so any code that makes "its
        own" manager and closes it afterwards closes *everyone's*, and every
        already-open instrument's next ``query()`` fails with ``InvalidSession:
        Invalid session handle``. That is not hypothetical: on the bench board, a
        read-only HDMI status dashboard calling ``agent.discover`` on its 30 s
        inventory timer killed a live 4-wire resistance sweep mid-run (Eastwood
        QR10x + Siglent SDM4065A), two setpoints in, because the scan created and
        closed a manager the DMM's session depended on. **The dashboard must
        never be able to disrupt bench function** — that is a project invariant,
        and this is the object that upholds it: one long-lived manager, owned by
        the agent, handed to every scan so no scan has cause to make one.

        Lazy, and never fatal. pyvisa stays an *optional* dependency (a bench of
        serial-only instruments is a valid bench), so importing it at module
        scope would break agents that have no VISA hardware. A missing pyvisa or
        an unusable backend returns None, which every discovery entry point
        already treats as "make your own if you need one" — a VISA problem must
        not take down request routing.

        Uses a dedicated ``self._rm_lock`` rather than ``self._lock``.
        ``self._lock`` guards the session and claim tables and is taken by
        ``register_session`` / ``drop_session`` / ``claim`` / ``release`` on
        every connect and disconnect; ``ResourceManager()`` enumerates USB and
        can take ~1.6 s, so holding the session lock across construction would
        stall unrelated connects behind a bus scan. One lock for one field is
        also deadlock-free by construction: nothing is called while it is held
        except pyvisa's constructor, which knows nothing of this agent.
        """
        with self._rm_lock:
            # ``_rm_tried`` and not ``_rm is None``: a bench without a working
            # VISA backend would otherwise retry the ~1.6 s construction on
            # every 30 s inventory poll, forever.
            if self._rm_tried:
                return self._rm
            self._rm_tried = True
            try:
                import pyvisa

                self._rm = pyvisa.ResourceManager()
                log.info("agent: opened the shared VISA resource manager")
            except Exception as exc:  # noqa: BLE001 - optional dependency
                log.info("agent: no shared VISA resource manager (%s)", exc)
                self._rm = None
            return self._rm

    def shutdown(self) -> None:
        self._stop.set()
        if self._deadman_thread is not None:
            self._deadman_thread.join(timeout=2.0)
            self._deadman_thread = None
        # Anything still armed must not be left that way.
        if self.governor.any_armed:
            log.warning("agent: devices armed at shutdown — driving to safe state")
            self.trip(TripReason.SHUTDOWN)
        self.runs.abort_all("agent shutdown")
        self.iterators.close_all()
        self.workers.stop_all()
        self.registry.close_all()
        # Only now, and only here. Closing the shared manager invalidates every
        # VISA session made from it (that is the whole hazard this object
        # exists to avoid), so it must come after the trip that drives the
        # bench safe and after ``close_all`` has released the instruments —
        # never on a scan. Idempotent: the drivers' own ``close`` may have
        # closed the same singleton already, and pyvisa swallows the second
        # close as ``InvalidSession``.
        self._close_resource_manager()
        self.blobs.close()
        # Last: the trip above emits events, and closing the bus first would
        # discard the record of the agent driving the bench safe on the way out.
        self.events.close(timeout=1.0)

    def _close_resource_manager(self) -> None:
        """Release the shared VISA manager, at shutdown only. Never raises.

        Also latches ``_rm_tried``, so a scan arriving on a request thread
        during teardown gets None and makes nothing new, rather than resurrecting
        a manager the agent has finished with.
        """
        with self._rm_lock:
            rm, self._rm = self._rm, None
            self._rm_tried = True
        if rm is None:
            return
        try:
            rm.close()
        except Exception:  # noqa: BLE001 - shutdown must not raise
            log.debug("agent: error closing the VISA resource manager", exc_info=True)

    # --- sessions -------------------------------------------------------

    def register_session(self, session: Session) -> None:
        with self._lock:
            self._sessions[session.session_id] = session
        # Each session gets its own bounded queue and sender thread, so its
        # socket is never on a producer's critical path. An observer (the HDMI
        # dashboard) gets a shallow, droppable queue: it wants current state,
        # and falling behind must cost it freshness, never cost the bench.
        self.events.subscribe(
            EventSubscriber(
                session.session_id,
                session.send_event,
                max_queue=DISPLAY_MAX_QUEUE if session.observer else DEFAULT_MAX_QUEUE,
                droppable=session.observer,
            )
        )

    def drop_session(self, session: Session) -> None:
        # Stop the sender thread first so it cannot write to a closing socket.
        self.events.unsubscribe(session.session_id, timeout=0.5)
        with self._lock:
            self._sessions.pop(session.session_id, None)
            released = [d for d, s in self._claims.items() if s == session.session_id]
            for device in released:
                self._claims.pop(device, None)
        for device in released:
            log.info("agent: %s released claim on %s", session.session_id, device)
        # If that was the last client and something is armed, the deadman
        # thread will trip after the grace period. Shorten the clock so the
        # grace actually applies from the disconnect, not from last contact.
        if released and self.governor.any_armed:
            log.warning(
                "agent: client holding an armed device disconnected; "
                "safe state in <= %.0fs unless it reconnects",
                self.governor.grace_for_disconnect(),
            )

    def _broadcast_event(self, event: dict) -> None:
        """Fan out to every session without ever blocking the caller.

        This is the governor's event sink, and ``Governor.trip()`` calls it
        **before** driving instruments to a safe state, on the deadman thread.
        It therefore must not touch a socket: it enqueues and returns. See
        :py:mod:`benchctrl.agent.eventbus`.
        """
        self.events.publish(event)

    def claim(self, session: Session, device_key: str) -> dict:
        self.registry.entry(device_key)
        with self._lock:
            holder = self._claims.get(device_key)
            if holder is not None and holder != session.session_id:
                raise PolicyError(
                    f"{device_key} is claimed by session {holder}; this session "
                    f"is a read-only observer until it is released"
                )
            self._claims[device_key] = session.session_id
            session.claims.add(device_key)
        return {"device": device_key, "claimed": True, "session": session.session_id}

    def release(self, session: Session, device_key: str) -> dict:
        with self._lock:
            if self._claims.get(device_key) == session.session_id:
                self._claims.pop(device_key, None)
            session.claims.discard(device_key)
        return {"device": device_key, "claimed": False}

    # --- codecs ---------------------------------------------------------

    def encoder(self, session: Session) -> Encoder:
        return Encoder(
            store_blob=lambda data: self.blobs.put(data).blob_id,
            register_iterator=lambda gen: self.iterators.register(
                "unknown", gen, session_id=session.session_id
            ).iter_id,
            register_recording=lambda rec: self.recordings.start(
                "unknown", rec, session_id=session.session_id
            ).rec_id,
        )

    @staticmethod
    def decoder() -> Decoder:
        return Decoder()


class _Handler(socketserver.BaseRequestHandler):
    """One client connection."""

    agent: BenchAgent  # injected by the server factory

    def handle(self) -> None:  # noqa: C901 - protocol dispatch is inherently branchy
        session = Session(self.agent, self.request, self.client_address)
        log.info("agent: connection from %s (%s)", session.peer, session.session_id)
        try:
            if not self._authenticate(session):
                return
            self.agent.register_session(session)
            self._serve(session)
        except Exception as exc:  # noqa: BLE001
            log.info("agent: session %s ended: %r", session.session_id, exc)
        finally:
            self.agent.drop_session(session)
            session.writer.close()
            try:
                self.request.close()
            except OSError:
                pass
            log.info("agent: %s disconnected", session.session_id)

    # --- handshake ------------------------------------------------------

    def _authenticate(self, session: Session) -> bool:
        agent = self.agent
        peer = session.peer

        if agent.failures.is_blocked(peer):
            remaining = agent.failures.seconds_remaining(peer)
            log.warning("agent: rejecting tarpitted %s (%.0fs left)", peer, remaining)
            self._send_error(
                session, 0, PolicyError(f"too many failures; retry in {remaining:.0f}s")
            )
            return False

        frame = session.reader.read_frame(timeout=authmod.HANDSHAKE_TIMEOUT)
        if frame is None or frame[0] != FrameType.HELLO:
            log.warning("agent: %s did not send HELLO", peer)
            return False
        hello = json.loads(frame[1] or b"{}")

        version_error = authmod.check_version(hello)
        if version_error:
            self._send_error(session, 0, PolicyError(version_error))
            return False

        # A client may ask for the read-only observer role. Self-declared on
        # purpose: it only ever *removes* capability, so there is nothing to gain
        # by lying about it. Still token-authenticated below — an observer can
        # read run history and the device inventory, which is not public.
        if bool(hello.get("observer", False)):
            session.observer = True
            log.info("agent: %s is a read-only observer session", session.peer)

        if not agent.token:
            # No token configured: accept, but be loud about it. This is a
            # bench that anyone on the subnet can drive.
            log.warning(
                "agent: no token configured — %s authenticated without proof. "
                "Anyone on this network can drive the instruments.",
                peer,
            )
            session.authenticated = True
            self._send_welcome(session)
            return True

        challenge, nonce_s = authmod.build_challenge(AGENT_NAME)
        session.writer.send(FrameType.CHALLENGE, _json(challenge))

        frame = session.reader.read_frame(timeout=authmod.HANDSHAKE_TIMEOUT)
        if frame is None or frame[0] != FrameType.AUTH:
            agent.failures.record_failure(peer)
            return False
        presented = json.loads(frame[1] or b"{}").get("mac", "")

        if not authmod.verify_mac(agent.token, hello.get("nonce_c", ""), nonce_s, presented):
            agent.failures.record_failure(peer)
            log.warning("agent: bad token from %s", peer)
            self._send_error(session, 0, PolicyError("authentication failed"))
            return False

        agent.failures.record_success(peer)
        session.authenticated = True
        self._send_welcome(session)
        return True

    def _send_welcome(self, session: Session) -> None:
        agent = self.agent
        session.writer.send(
            FrameType.WELCOME,
            _json(
                {
                    "session": session.session_id,
                    "agent": AGENT_NAME,
                    # Echoed so a client can assert it got the role it asked
                    # for rather than discovering it via a PolicyError later.
                    "observer": session.observer,
                    "heartbeat_s": agent.heartbeat_s,
                    "deadman_s": agent.deadman_s,
                    "clock_utc": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                    "devices": agent.registry.describe(),
                    "limits": {
                        "max_blocking_s": agent.max_blocking_s,
                        "max_recording_s": agent.recordings.max_recording_s,
                        "chunk_bytes": CHUNK_BYTES,
                    },
                }
            ),
        )

    # --- main loop ------------------------------------------------------

    def _serve(self, session: Session) -> None:
        agent = self.agent
        while True:
            frame = session.reader.read_frame(timeout=max(agent.deadman_s * 2, 30.0))
            if frame is None:
                return
            frame_type, payload = frame
            # An observer's traffic must NOT count as operator contact. A status
            # display polling every second would otherwise pin
            # seconds_since_contact near zero, and should_trip() — which is
            # `any_armed and seconds_since_contact > deadman_s` — could never
            # fire. The bench would stay armed after the real client died,
            # because the thing reporting on it kept it alive.
            if not session.observer:
                agent.governor.touch()

            if frame_type == FrameType.PING:
                session.writer.send(FrameType.PONG)
                continue
            if frame_type == FrameType.REQ:
                self._handle_request(session, json.loads(payload or b"{}"))
                continue
            if frame_type == FrameType.CANCEL:
                continue  # best-effort; the worker checks job.cancelled
            log.debug("agent: ignoring frame type 0x%02X", frame_type)

    def _handle_request(self, session: Session, request: dict) -> None:
        req_id = request.get("id", 0)
        method = request.get("m", "")
        params = request.get("p") or {}
        try:
            result, props = self._route(session, method, params)
            body: dict = {"id": req_id, "r": result}
            if props is not None:
                body["props"] = props
            session.writer.send(FrameType.RSP, _json(body))
        except Exception as exc:  # noqa: BLE001 - relayed to the client
            self._send_error(
                session,
                req_id,
                exc,
                device=params.get("device"),
                method=params.get("method") or method,
            )

    def _send_error(self, session, req_id, exc, device=None, method=None) -> None:
        payload = encode_exception(
            exc,
            device=device,
            method=method,
            traceback_text="".join(
                traceback.format_exception(type(exc), exc, exc.__traceback__)
            )[-4000:],
        )
        try:
            session.writer.send(FrameType.ERR, _json({"id": req_id, "e": payload}))
        except Exception:  # noqa: BLE001
            pass

    # --- routing --------------------------------------------------------

    def _route(self, session: Session, method: str, p: dict):
        agent = self.agent

        if session.observer and method not in OBSERVER_METHODS:
            raise PolicyError(
                f"{method!r} is not available to an observer session; observers "
                f"are read-only status consumers and may call only: "
                f"{', '.join(sorted(OBSERVER_METHODS))}"
            )

        if method == "agent.hello":
            return {"agent": AGENT_NAME, "devices": agent.registry.describe()}, None
        if method == "agent.devices":
            return agent.registry.describe(), None
        if method == "agent.discover":
            from benchctrl import discovery

            # The agent's own manager, never a fresh one: a scan that built and
            # closed its own would close this singleton and invalidate every
            # instrument session on the bench. See
            # :py:meth:`BenchAgent.resource_manager` for the sweep it killed.
            return discovery.inventory(
                resource_manager=agent.resource_manager()
            ), None
        if method == "agent.status":
            return {
                "safety": agent.governor.status(),
                "workers": agent.workers.stats(),
                "blobs": agent.blobs.stats(),
                "recordings": agent.recordings.describe(),
            }, None
        if method == "agent.time":
            return {"utc": time.time(), "mono": time.monotonic()}, None
        if method == "agent.open":
            entry = agent.registry.open(p["device"], **(p.get("open") or {}))
            return entry.to_dict(), None
        if method == "agent.close":
            key = p["device"]
            agent.governor.trip(
                TripReason.OPERATOR,
                {key: agent.registry.open_devices().get(key)},
                {key: agent.workers.get(key)},
            )
            return {"device": key, "closed": agent.registry.close(key)}, None
        if method == "agent.claim":
            return agent.claim(session, p["device"]), None
        if method == "agent.release":
            return agent.release(session, p["device"]), None

        if method == "device.call":
            return self._device_call(session, p)
        if method == "device.read_window":
            return self._read_window(p), None
        if method == "device.getprops":
            key = p["device"]
            obj = agent.registry.get(key)
            surface = agent.registry.surface_of(key)
            names = tuple(p.get("names") or surface.snapshot_props)
            raw = dispatch.snapshot_properties(obj, surface, names)
            return agent.encoder(session).encode(raw), None

        if method == "blob.fetch":
            return self._blob_fetch(session, p), None
        if method == "blob.release":
            return {"released": agent.blobs.release(p["blob"])}, None

        if method == "rec.start":
            return self._rec_start(session, p), None
        if method == "rec.stop":
            return self._rec_stop(session, p), None
        if method == "rec.stats":
            return self._rec_stats(p), None
        if method == "rec.release":
            return {"released": agent.recordings.release(p["rec_id"])}, None

        if method == "run.submit":
            return self._run_submit(session, p), None
        if method == "run.status":
            return self.agent.runs.get(p["run_id"]).status_dict(), None
        if method == "run.list":
            return agent.runs.list(), None
        if method == "run.events":
            store = agent.runs.store_for(p["run_id"])
            return store.events_since(int(p.get("since_seq", 0)),
                                      limit=int(p.get("limit", 500))), None
        if method == "run.abort":
            engine = agent.runs.get(p["run_id"])
            engine.abort(p.get("reason", "operator"))
            return {"run_id": engine.run_id, "aborting": True}, None
        if method == "run.artifacts":
            store = agent.runs.store_for(p["run_id"])
            return {
                "run_id": p["run_id"],
                "chunks": store.chunks(),
                "phases": store.phases(),
                "info": store.info(),
            }, None
        if method == "run.fetch_chunk":
            return self._run_fetch_chunk(session, p), None

        if method == "iter.open":
            return self._iter_open(session, p), None
        if method == "iter.next":
            return self._iter_next(p), None
        if method == "iter.close":
            return {"closed": agent.iterators.close(int(p["iter_id"]))}, None

        raise PolicyError(f"unknown method {method!r}")

    # --- device calls ---------------------------------------------------

    def _device_call(self, session: Session, p: dict):
        agent = self.agent
        key = p["device"]
        name = p.get("method", "")
        obj = agent.registry.get(key)
        surface = agent.registry.surface_of(key)

        # Property read — a distinct path on purpose. If unknown names fell
        # through to a callable, `enable_output`'s `current_limit is None`
        # gate would always see a bound method and arm with no limit set.
        if name in surface.properties:
            raw = dispatch.snapshot_properties(obj, surface, (name,))[name]
            return agent.encoder(session).encode(raw), None

        dispatch.check_callable(surface, name)
        dispatch.check_writer(surface, name, session.holds(key))

        decoder = agent.decoder()
        args = [decoder.decode(a) for a in (p.get("args") or [])]
        kwargs = {k: decoder.decode(v) for k, v in (p.get("kwargs") or {}).items()}

        # Clamp caller-supplied blocking durations so the safety lane is
        # never starved for longer than max_blocking_s.
        args, kwargs = _clamp_durations(name, args, kwargs, agent.max_blocking_s)

        fn = getattr(obj, name)
        worker = agent.workers.get(key)
        result = worker.submit(
            lambda: fn(*args, **kwargs),
            priority=PRIORITY_NORMAL,
            timeout=agent.max_blocking_s * 4,
            label=f"{key}.{name}",
        )
        agent.governor.observe_call(
            key, name, tuple(args), kwargs, session_id=session.session_id
        )

        encoder = agent.encoder(session)
        encoded = encoder.encode(result)
        props = None
        if p.get("want_props", True) and surface.snapshot_props:
            props = encoder.encode(dispatch.snapshot_properties(obj, surface))
        return encoded, props

    # --- runs -----------------------------------------------------------

    def _run_submit(self, session: Session, p: dict) -> dict:
        """Hand the bench a long experiment and let go of it."""
        agent = self.agent
        spec = RunSpec.from_dict(p["spec"])
        if not session.holds(spec.device):
            raise PolicyError(
                f"submitting a run for {spec.device} requires the writer claim "
                f"— call agent.claim first"
            )
        device = agent.registry.get(spec.device)
        engine = agent.runs.submit(
            spec,
            device,
            worker=agent.workers.get(spec.device),
            governor=agent.governor,
            on_event=agent._broadcast_event,
            clock_scale=float(p.get("clock_scale", 1.0)),
        )
        if spec.llm.enabled:
            from benchctrl.agent.llm.supervisor import build_supervisor

            supervisor = build_supervisor(engine, base_url=agent.llm_base_url)
            if supervisor is not None:
                engine.attach_llm(supervisor.start())
        engine.start()
        return {"run_id": engine.run_id, "spec_sha256": spec.sha256}

    def _run_fetch_chunk(self, session: Session, p: dict) -> dict:
        """Expose one recorded chunk as a blob for the host to pull."""
        from pathlib import Path as _Path

        agent = self.agent
        store = agent.runs.store_for(p["run_id"])
        idx = int(p["idx"])
        match = next((c for c in store.chunks() if c["idx"] == idx), None)
        if match is None:
            raise BenchValueError(f"run {p['run_id']} has no chunk {idx}")
        data = _Path(match["path"]).read_bytes()
        info = agent.blobs.put(data, content_type="application/x-opensmu")
        return {"idx": idx, **info.to_dict()}

    def _read_window(self, p: dict) -> dict:
        """Drain a measurement window, returning a code-keyed dict.

        A dedicated verb because the local method keys its result by *the
        caller's own channel objects*, and object identity cannot cross a
        wire. Codes travel; the proxy rebuilds the caller's mapping.
        """
        agent = self.agent
        key = p["device"]
        obj = agent.registry.get(key)
        codes = list(p.get("channels") or ())
        duration = clamp_blocking(float(p.get("duration_s", 0.0)), agent.max_blocking_s)
        worker = agent.workers.get(key)
        result = worker.submit(
            lambda: obj.read_window(codes, duration),
            timeout=duration + agent.max_blocking_s * 2,
            label=f"{key}.read_window",
        )
        return {
            _code_of(channel): list(values) for channel, values in (result or {}).items()
        }

    # --- recordings -----------------------------------------------------

    def _rec_start(self, session: Session, p: dict) -> dict:
        agent = self.agent
        key = p["device"]
        if not session.holds(key):
            raise PolicyError(
                f"recording {key} mutates device state — call agent.claim first"
            )
        agent.recordings.check_duration(p.get("expected_s"))
        obj = agent.registry.get(key)
        channels = tuple(p.get("channels") or ())
        name = p.get("name", "recording")
        worker = agent.workers.get(key)
        rec = worker.submit(
            lambda: obj.start_recording(name=name, channels=channels or None),
            label=f"{key}.start_recording",
        )
        handle = agent.recordings.start(
            key, rec, name=name, session_id=session.session_id
        )
        agent.governor.set_recording(key, True)
        return handle.to_dict()

    def _rec_stop(self, session: Session, p: dict) -> dict:
        agent = self.agent
        handle = agent.recordings.get(p["rec_id"])
        obj = agent.registry.get(handle.device_key)
        worker = agent.workers.get(handle.device_key)
        worker.submit(obj.stop_recording, label=f"{handle.device_key}.stop_recording")
        agent.governor.set_recording(handle.device_key, False)

        data = handle.recording.to_bytes()
        info = agent.blobs.put(data, content_type="application/x-opensmu")
        agent.recordings.finish(
            handle.rec_id, blob_id=info.blob_id, sha256=info.sha256, size=info.size
        )
        out = handle.to_dict()
        out["summary"] = {
            ch.code: _stats_dict(handle.recording.statistics(ch))
            for ch in handle.recording.channels
        }
        return out

    def _rec_stats(self, p: dict) -> dict:
        handle = self.agent.recordings.get(p["rec_id"])
        out: dict = {"rec_id": handle.rec_id, "counts": handle.counts()}
        code = p.get("channel")
        if code:
            out["statistics"] = _stats_dict(handle.recording.statistics(code))
        if p.get("preview"):
            out["preview"] = handle.preview(code)
        return out

    # --- blobs ----------------------------------------------------------

    def _blob_fetch(self, session: Session, p: dict) -> dict:
        agent = self.agent
        blob_id = p["blob"]
        info = agent.blobs.info(blob_id)
        session.writer.send(FrameType.BLOB_HDR, _json({"id": p.get("id", 0), **info.to_dict()}))
        try:
            for chunk in agent.blobs.iter_chunks(blob_id):
                session.writer.send(FrameType.BLOB_CHUNK, chunk)
        except Exception as exc:  # noqa: BLE001
            session.writer.send(
                FrameType.BLOB_END,
                _json({"blob": blob_id, "ok": False, "error": repr(exc)}),
            )
            raise
        session.writer.send(FrameType.BLOB_END, _json({"blob": blob_id, "ok": True}))
        return info.to_dict()

    # --- iterators ------------------------------------------------------

    def _iter_open(self, session: Session, p: dict) -> dict:
        agent = self.agent
        key = p["device"]
        if not session.holds(key):
            raise PolicyError(f"streaming {key} requires the writer claim")
        obj = agent.registry.get(key)
        kwargs = {k: v for k, v in (p.get("kwargs") or {}).items()}
        gen = obj.stream(**kwargs)
        handle = agent.iterators.register(key, gen, session_id=session.session_id)
        return {"iter_id": handle.iter_id}

    def _iter_next(self, p: dict) -> dict:
        handle = self.agent.iterators.get(int(p["iter_id"]))
        items = handle.take(int(p.get("max", 64)))
        return {
            "items": [_sample_dict(s) for s in items],
            "exhausted": handle.exhausted,
            "error": handle.error,
        }


def _clamp_durations(name: str, args: list, kwargs: dict, limit: float):
    """Bound the blocking duration of calls that take one."""
    if name in ("read_raw", "read_window", "read_for") and args:
        first = args[0]
        if isinstance(first, (int, float)):
            args = [clamp_blocking(float(first), limit)] + list(args[1:])
    for key in ("seconds", "duration_s", "timeout"):
        if key in kwargs and isinstance(kwargs[key], (int, float)):
            kwargs[key] = clamp_blocking(float(kwargs[key]), limit * 4)
    return args, kwargs


def _code_of(channel) -> str:
    """Reduce a channel object (or string) to its two-letter code."""
    code = getattr(channel, "code", None)
    return code if isinstance(code, str) else str(channel)


def _stats_dict(stats) -> dict:
    return {
        "sample_count": stats.sample_count,
        "duration": stats.duration,
        "min": stats.min,
        "max": stats.max,
        "average": stats.average,
        "rms": stats.rms,
        "energy": stats.energy,
        "charge": stats.charge,
    }


def _sample_dict(sample) -> dict:
    return {
        "channel": getattr(getattr(sample, "channel", None), "code", None),
        "timestamp": getattr(sample, "timestamp", None),
        "value": getattr(sample, "value", None),
    }


class _ThreadedTCPServer(socketserver.ThreadingTCPServer):
    allow_reuse_address = True
    daemon_threads = True


class AgentServer:
    """Binds a socket and serves an agent on it."""

    def __init__(
        self,
        agent: BenchAgent,
        *,
        host: str = "0.0.0.0",
        port: int = 9737,
    ) -> None:
        self.agent = agent
        handler = type("_BoundHandler", (_Handler,), {"agent": agent})
        self._server = _ThreadedTCPServer((host, port), handler)
        self._thread: Optional[threading.Thread] = None

    @property
    def address(self) -> tuple[str, int]:
        return self._server.server_address[:2]

    @property
    def port(self) -> int:
        return self.address[1]

    def start(self) -> AgentServer:
        self.agent.start_deadman()
        self._thread = threading.Thread(
            target=self._server.serve_forever, name="agent-server", daemon=True
        )
        self._thread.start()
        log.info("agent: listening on %s:%d", *self.address)
        return self

    def stop(self) -> None:
        self._server.shutdown()
        self._server.server_close()
        if self._thread is not None:
            self._thread.join(timeout=5.0)
            self._thread = None
        self.agent.shutdown()

    def __enter__(self) -> AgentServer:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

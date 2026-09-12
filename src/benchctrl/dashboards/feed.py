"""The dashboard's connection to a bench agent.

Owns an observer :py:class:`~benchctrl.net.client.RemoteClient`, folds what
arrives into a :py:class:`~benchctrl.dashboards.state.BenchStatus`, and
reconnects on its own when the agent restarts.

Why a thread and not the render loop
------------------------------------

A display redraws on a timer, so the naive design is "connect, read, render,
exit". That would open a socket per frame, and the agent would see a connection
storm from the one client least entitled to bother it. Instead the feed runs
once in a background thread and the renderer reads a snapshot.

What makes this safe for the bench
----------------------------------

Two properties, both inherited rather than reimplemented:

- The session is an **observer** (``RemoteClient(..., observer=True)``), so its
  traffic does not count as operator contact and it cannot call anything that
  changes state. A crashed, wedged, or hostile dashboard cannot arm anything or
  keep an armed bench alive. See
  :py:data:`benchctrl.agent.server.OBSERVER_METHODS`.
- Fan-out to it is already non-blocking on the agent side
  (:py:mod:`benchctrl.agent.eventbus`), with a shallow droppable queue. If this
  process stops reading, the agent sheds events to it and carries on.

So the failure modes here cost the panel its freshness and cost the bench
nothing. The remaining job is to make sure they cost the panel its *credibility*
too — a feed that cannot see the agent must not leave a stale-but-plausible
screen up. That is why every exit path calls ``apply_disconnected``.

The status poll
---------------

Events alone cannot establish initial state: a panel that starts mid-run has
missed every event that produced the current state. So the feed also polls
``agent.status`` — slowly, and only because it is the authoritative source that
lets :py:meth:`BenchStatus.apply_status` clear staleness. Polling is safe here
*only* because this is an observer session; on a normal session it would starve
the deadman.

The bus inventory
-----------------

``agent.status`` reports what devices are *doing*, which is not the same as what
is *attached*. The safety governor creates a device's state lazily — on the first
call that could arm it — so on a freshly-started agent ``safety.devices`` is
``{}``, and a panel driven from it alone shows NO LINK for an instrument that is
plugged in and ready.

``agent.discover`` answers the other question, so the feed polls it too, on a much
slower clock (:py:data:`DEFAULT_INVENTORY_S`). It has to be a separate cadence
rather than another field on the status poll: measured on the bench board it costs
~1.65 s against ~5 ms for ``agent.status``, since identifying a USB-TMC instrument
means reading its string descriptors over libusb. Both calls are in
``OBSERVER_METHODS`` already, so none of this widens what a display may do.

The generator's screen
----------------------

The one read this feed makes of an *instrument* rather than of the agent: while
the FUI page is showing the function generator's own screen (``/sdg/screen``),
the session loop grabs :py:data:`SCREEN_DEVICE`'s ``SCDP`` bitmap every
:py:data:`SCREEN_POLL_S`. It is opt-in and self-cancelling — each page fetch arms
it for :py:data:`SCREEN_WATCH_S`, so a closed browser stops the grabs — and it
is a claim-free ``device.read`` of a non-mutating method, never an
``agent.claim``. The agent's observer allowlist governs whether the call is
permitted at all; if it is refused the pane reads NO SCREEN and nothing else on
the panel is affected.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Callable, Optional

from benchctrl.config import EndpointConfig
from benchctrl.dashboards.state import BenchStatus

log = logging.getLogger("benchctrl.dashboards.feed")

#: How often to fetch an authoritative status snapshot. Slow on purpose: events
#: carry the interesting transitions, and this only has to correct drift and
#: establish state at startup.
DEFAULT_POLL_S = 5.0

#: How often to re-take the bus inventory (``agent.discover``). Two orders of
#: magnitude slower than the status poll, and measured rather than guessed: on
#: the bench board ``agent.discover`` takes ~1.65 s against ~5 ms for
#: ``agent.status``, because identifying a USB-TMC instrument means reading its
#: string descriptors over libusb. Instruments are also plugged in by hand on a
#: timescale of minutes, so a fast scan would buy nothing for 300x the cost.
DEFAULT_INVENTORY_S = 30.0

#: Backoff bounds for reconnecting. Capped so a display left on overnight
#: rejoins within a minute of the agent coming back, rather than an hour.
RECONNECT_MIN_S = 1.0
RECONNECT_MAX_S = 30.0

#: The one device whose own screen the FUI shows: the function generator's
#: ``SCDP`` bitmap fills the DMM pane while the generator is served, so a test
#: can be watched from across the bench. See :py:meth:`AgentFeed.want_screen`.
SCREEN_DEVICE = "siglent_sdg1032x"

#: How often to re-grab the screen while someone is watching it. ~0.5 s per
#: grab on the bench (a 392 KB BMP over USB-TMC), so 2 s keeps the generator's
#: worker mostly free for the run that is actually driving it.
SCREEN_POLL_S = 2.0

#: How long one ``/sdg/screen`` hit keeps the poll armed. The page re-fetches
#: every :py:data:`SCREEN_POLL_S` while the pane is up, so this only has to
#: outlast a couple of missed fetches; a closed browser stops the grabs within
#: this window rather than polling the instrument forever for nobody.
SCREEN_WATCH_S = 10.0


class AgentFeed:
    """A self-healing read-only feed from one agent into one ``BenchStatus``.

    Thread-safe for the render loop's purposes: :py:meth:`snapshot` takes the
    lock, and every mutation of the status happens under it.
    """

    def __init__(
        self,
        endpoint: EndpointConfig,
        *,
        poll_s: float = DEFAULT_POLL_S,
        inventory_s: float = DEFAULT_INVENTORY_S,
        connect: Optional[Callable[[], object]] = None,
        screen_reader: Optional[Callable[[object], bytes]] = None,
    ) -> None:
        self.endpoint = endpoint
        self.poll_s = poll_s
        self.inventory_s = inventory_s
        self.status = BenchStatus()
        # Injectable so tests can drive the whole loop against a fake client;
        # the default builds a real observer session.
        self._connect = connect or self._default_connect
        # How one screen grab reaches the generator, given the session's client.
        # Injectable for the same reason as ``connect``; the default is the
        # claim-free ``device.read`` in :py:meth:`_default_screen_reader`.
        self._screen_reader = screen_reader or self._default_screen_reader
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: object = None
        self._reconnects = 0
        #: The last screen grab, as ``(bmp_bytes, monotonic_ts)``; None when
        #: there is no *current* picture — never a stale one dressed as live.
        self.latest_screen: Optional[tuple[bytes, float]] = None
        self._screen_wanted_until = 0.0
        self._screen_grabbed_at = 0.0

    def _default_connect(self):
        from benchctrl.net.client import RemoteClient

        # observer=True is the load-bearing argument in this whole module.
        return RemoteClient(self.endpoint, observer=True).connect()

    # --- lifecycle ------------------------------------------------------

    def start(self) -> AgentFeed:
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="benchctrl-dashboard-feed", daemon=True
        )
        self._thread.start()
        return self

    def stop(self, *, timeout: float = 3.0) -> None:
        self._stop.set()
        client = self._client
        if client is not None:
            # Shutting down anyway: a close that fails has nothing left to tell us.
            with contextlib.suppress(Exception):
                client.close()  # type: ignore[attr-defined]
        thread = self._thread
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout=timeout)
        self._thread = None

    def __enter__(self) -> AgentFeed:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()

    # --- the loop -------------------------------------------------------

    def _run(self) -> None:
        backoff = RECONNECT_MIN_S
        while not self._stop.is_set():
            try:
                client = self._connect()
            except Exception as exc:  # noqa: BLE001 - the agent may just be down
                with self._lock:
                    self.status.apply_disconnected(f"cannot reach the agent: {exc}")
                # A display that cannot reach the bench is a normal state on a
                # board that boots before the agent, so this is not an error.
                log.info("dashboard: agent unreachable (%s); retrying", exc)
                if self._stop.wait(backoff):
                    return
                backoff = min(backoff * 2, RECONNECT_MAX_S)
                continue

            backoff = RECONNECT_MIN_S
            self._client = client
            try:
                self._session(client)
            except Exception as exc:  # noqa: BLE001 - never kill the feed thread
                log.info("dashboard: session ended (%s)", exc)
                with self._lock:
                    self.status.apply_disconnected(f"session ended: {exc}")
            finally:
                self._client = None
                with contextlib.suppress(Exception):
                    client.close()  # type: ignore[union-attr]
                self._reconnects += 1
            if self._stop.wait(RECONNECT_MIN_S):
                return

    def _session(self, client) -> None:
        """Run one connected session until it drops."""
        with self._lock:
            self.status.apply_connected(getattr(client, "welcome", {}) or {})

        # Events arrive on the client's own rx thread. Handing them straight
        # into the status under the lock is fine — folding an event is pure
        # dict work and cannot block.
        client.on_event = self._on_event

        # Due immediately: until the first inventory lands, every slot's presence
        # is unknown, and "unknown" is the one thing the rail cannot render as a
        # fact. Monotonic deadline rather than a countdown, so a slow status poll
        # does not push the inventory back indefinitely.
        next_inventory = 0.0
        # The status poll gets the same treatment once the screen poll exists:
        # the loop now wakes on whichever clock is due next, and a 2 s screen
        # cadence must not drag the 5 s status poll along with it.
        next_status = 0.0
        while not self._stop.is_set():
            if not getattr(client, "is_connected", False):
                with self._lock:
                    self.status.apply_disconnected("the agent closed the connection")
                return
            if time.monotonic() >= next_status:
                snapshot = client.status()
                with self._lock:
                    self.status.apply_status(snapshot)
                next_status = time.monotonic() + self.poll_s

            now = time.monotonic()
            if now >= next_inventory:
                # Deliberately inline on this thread rather than a second one.
                # The agent handles one request at a time per session, so a
                # parallel scan would not overlap with the status poll anyway —
                # it would just make the ordering unpredictable and let two
                # in-flight calls both hit the same session's writer.
                self._take_inventory(client)
                next_inventory = time.monotonic() + self.inventory_s
            # Same reasoning: inline, after the reads that matter more.
            screen_due = self._poll_screen(client)

            delay = next_status - time.monotonic()
            if screen_due is not None:
                delay = min(delay, screen_due)
            if self._stop.wait(max(delay, 0.0)):
                return

    def _take_inventory(self, client) -> None:
        """Re-scan the bus. Never fatal: a failed scan is missing data, not a
        broken session.

        A scan that raises leaves the previous inventory in place rather than
        blanking it. That is the right way round for this one field: discovery
        walks USB and can fail transiently under contention, and flapping a slot
        between ATTACHED and NOT FOUND would make the rail unreadable. A scan
        that stops succeeding for good is covered by the staleness machinery,
        which already governs everything else on the panel.
        """
        try:
            inventory = client.discover()
        except Exception as exc:  # noqa: BLE001 - an optional enrichment, not the feed
            log.info("dashboard: bus inventory failed (%s); keeping the last one", exc)
            return
        with self._lock:
            self.status.apply_inventory(inventory)

    # --- the generator's screen -----------------------------------------

    def want_screen(self) -> None:
        """Arm the screen poll for :py:data:`SCREEN_WATCH_S` from now.

        Called by the HTTP handler on every ``/sdg/screen`` hit, so the
        instrument is only ever read while a page is actually showing it. The
        first hit arms the poll and gets a 404; the picture is there by the
        page's next fetch.
        """
        with self._lock:
            self._screen_wanted_until = time.monotonic() + SCREEN_WATCH_S

    def screen_snapshot(self) -> dict:
        """``{"present", "age_s", "bytes"}`` for the view, under the lock."""
        with self._lock:
            latest = self.latest_screen
        if latest is None:
            return {"present": False, "age_s": None, "bytes": None}
        data, taken = latest
        return {
            "present": True,
            "age_s": round(max(time.monotonic() - taken, 0.0), 2),
            "bytes": len(data),
        }

    def _screen_served(self) -> bool:
        """Whether the agent's registry lists the generator at all. Learned from
        WELCOME's device table, so it is known before any inventory lands."""
        with self._lock:
            slot = self.status.slots.get(SCREEN_DEVICE)
            return slot is not None and bool(slot.served)

    def _poll_screen(self, client) -> Optional[float]:
        """One step of the screen poll. Returns how long until the next grab is
        due, or None when nothing is due (nobody watching, or not served).

        Never fatal, and never stale: a grab that raises clears
        :py:attr:`latest_screen` so the pane drops to NO SCREEN rather than
        keeping the last bitmap up as if it were live. Logged at debug because on
        a bench without the generator this is the ordinary idle case, not news.
        """
        now = time.monotonic()
        with self._lock:
            wanted = now < self._screen_wanted_until
        if not wanted or not self._screen_served():
            with self._lock:
                self.latest_screen = None
            return None
        remaining = SCREEN_POLL_S - (now - self._screen_grabbed_at)
        if remaining > 0:
            return remaining
        # Stamp before the read, not after: a slow or failing grab must not
        # shorten the gap to the next one.
        self._screen_grabbed_at = now
        try:
            data = self._screen_reader(client)
        except Exception as exc:  # noqa: BLE001 - a picture, not the feed
            log.debug("dashboard: screen grab of %s failed (%s)", SCREEN_DEVICE, exc)
            with self._lock:
                self.latest_screen = None
            return SCREEN_POLL_S
        with self._lock:
            self.latest_screen = (bytes(data), time.monotonic())
        return SCREEN_POLL_S

    @staticmethod
    def _default_screen_reader(client) -> bytes:
        """Read the generator's screen through the feed's own session.

        Deliberately *not* ``client.attach()``: that helper takes the writer claim
        as a side effect, and a dashboard must never hold one — while a run is
        driving the generator the claim is the run's. ``read_screen`` is not a
        mutator, so it goes through ``device.read`` — the one device verb an
        observer session has: refused unless the device is already open (a
        display never powers a session up) and never a mutator, by the
        agent's own check as well as the name-prefix rule.
        """
        data = client.call(
            "device.read",
            {
                "device": SCREEN_DEVICE,
                "method": "read_screen",
                "args": [],
                "kwargs": {},
                "want_props": False,
            },
        )
        if not isinstance(data, (bytes, bytearray, memoryview)):
            raise TypeError(f"read_screen returned {type(data).__name__}, not bytes")
        return bytes(data)

    def _on_event(self, event: dict) -> None:
        try:
            with self._lock:
                self.status.apply_event(event)
        except Exception:  # noqa: BLE001 - a malformed event must not kill the rx thread
            log.exception("dashboard: could not fold event %r", event)

    # --- reading --------------------------------------------------------

    def snapshot(self) -> dict:
        """A flat, render-ready view. Re-checks silence as a side effect.

        The silence check lives here rather than on a timer so there is no
        second thread to leak, and so a render loop that has itself stalled
        reports staleness the moment it wakes up.
        """
        with self._lock:
            # Bounds the STARTING window even if the feed thread never reports.
            # Here rather than on a timer, for the same reason as check_silence.
            self.status.expire_startup_grace()
            self.status.check_silence()
            data = self.status.to_dict()
            data["reconnects"] = self._reconnects
            data["sdg_screen"] = self.screen_snapshot()
            return data

    @property
    def bench(self) -> BenchStatus:
        """The live status object. Read under :py:meth:`snapshot` in a UI."""
        return self.status

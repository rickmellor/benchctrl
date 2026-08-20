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
"""

from __future__ import annotations

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
    ) -> None:
        self.endpoint = endpoint
        self.poll_s = poll_s
        self.inventory_s = inventory_s
        self.status = BenchStatus()
        # Injectable so tests can drive the whole loop against a fake client;
        # the default builds a real observer session.
        self._connect = connect or self._default_connect
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: Optional[threading.Thread] = None
        self._client: object = None
        self._reconnects = 0

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
            try:
                client.close()  # type: ignore[attr-defined]
            except Exception:  # noqa: BLE001 - shutting down anyway
                pass
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
                try:
                    client.close()  # type: ignore[attr-defined]
                except Exception:  # noqa: BLE001
                    pass
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
        while not self._stop.is_set():
            if not getattr(client, "is_connected", False):
                with self._lock:
                    self.status.apply_disconnected("the agent closed the connection")
                return
            snapshot = client.status()
            with self._lock:
                self.status.apply_status(snapshot)

            now = time.monotonic()
            if now >= next_inventory:
                # Deliberately inline on this thread rather than a second one.
                # The agent handles one request at a time per session, so a
                # parallel scan would not overlap with the status poll anyway —
                # it would just make the ordering unpredictable and let two
                # in-flight calls both hit the same session's writer.
                self._take_inventory(client)
                next_inventory = time.monotonic() + self.inventory_s
            if self._stop.wait(self.poll_s):
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
            return data

    @property
    def bench(self) -> BenchStatus:
        """The live status object. Read under :py:meth:`snapshot` in a UI."""
        return self.status

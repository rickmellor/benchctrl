"""A client whose agent dies must fail its calls now, not in 30 seconds.

Found on the bench board, not in CI: an observer feed reported ``STALE`` for
16 s after the agent was ``SIGKILL``ed, where it should have said ``NO AGENT``
immediately. The cause was not dashboard-specific.

``_rx_loop`` used to sweep :py:attr:`RemoteClient._pending` and only *then*,
in its ``finally``, set ``_connected = False``. Meanwhile
``call_with_props`` checked ``_connected`` outside the lock it used to
register. Interleave those and a request lands in ``_pending`` after the sweep
has run: no thread will ever fail it, so it waits out the full
``DEFAULT_CALL_TIMEOUT`` (30 s) against an agent that no longer exists.

That is a bug for every client, not just a panel. ``systemctl restart
benchctrl-agent`` kills the process under whatever is connected, so an
operator's next call — plausibly the one driving an instrument to a safe
state — could block for half a minute before it admitted the agent was gone.

These tests drive the interleaving deterministically instead of racing for it.
The natural race reproduced 2 times in 6 on the board, which is exactly the
flakiness that lets a bug like this live in a suite.
"""

from __future__ import annotations

import threading
import time

import pytest

from benchctrl.net.client import DEFAULT_CALL_TIMEOUT, RemoteClient
from benchctrl.net.errors import BenchConnectionError
from benchctrl.net.frames import FrameType


class _DeadWriter:
    """A writer whose send succeeds and whose response never comes."""

    def __init__(self):
        self.sent = []

    def send(self, frame_type, payload):
        self.sent.append((frame_type, payload))

    def close(self):
        pass


@pytest.fixture()
def client():
    """A client wired up as if connected, with no socket behind it.

    Built by hand rather than against a real agent because the point is the
    ordering inside the client, and a real socket makes the window a race.
    """
    c = RemoteClient.__new__(RemoteClient)
    RemoteClient.__init__(c, _endpoint())
    c._writer = _DeadWriter()
    c._connected = True
    return c


def _endpoint():
    from benchctrl.config import EndpointConfig

    return EndpointConfig(host="127.0.0.1", port=1, token="t")


# --------------------------------------------------------------------------
# The bug
# --------------------------------------------------------------------------


def test_a_call_filed_while_the_connection_dies_fails_immediately(client):
    """The regression. Registers a call concurrently with the death sweep.

    Fails on the old ordering by taking ``DEFAULT_CALL_TIMEOUT`` seconds; the
    assertion is on elapsed time because that *is* the defect. The bound is
    generous (2 s against a 30 s timeout) so it cannot flake on a loaded board
    while still being nowhere near the broken behaviour.
    """
    started = threading.Event()
    outcome = {}

    def caller():
        started.set()
        t0 = time.monotonic()
        try:
            client.call("agent.status", timeout=DEFAULT_CALL_TIMEOUT)
            outcome["result"] = "returned"
        except BenchConnectionError as exc:
            outcome["result"] = "BenchConnectionError"
            outcome["message"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            outcome["result"] = type(exc).__name__
        outcome["elapsed"] = time.monotonic() - t0

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    started.wait(timeout=5.0)
    # Give the caller time to get its request into _pending, so the sweep has
    # something to find and we are testing the interesting interleaving.
    time.sleep(0.05)

    client._die(BenchConnectionError("connection lost: recv failed"))

    thread.join(timeout=DEFAULT_CALL_TIMEOUT + 5.0)
    assert not thread.is_alive(), "the call never returned at all"
    assert outcome["result"] == "BenchConnectionError", outcome
    assert outcome["elapsed"] < 2.0, (
        f"the call took {outcome['elapsed']:.1f}s to notice a dead agent; it "
        f"was waiting out the {DEFAULT_CALL_TIMEOUT:.0f}s call timeout, which "
        f"is the bug this test exists for"
    )


def test_a_call_filed_after_the_connection_died_fails_immediately(client):
    """The simpler half: no concurrency, the client is already dead."""
    client._die(BenchConnectionError("connection lost: recv failed"))
    t0 = time.monotonic()
    with pytest.raises(BenchConnectionError):
        client.call("agent.status", timeout=DEFAULT_CALL_TIMEOUT)
    assert time.monotonic() - t0 < 2.0


def test_the_failure_names_the_real_reason(client):
    """"not connected" hides what happened; a panel would show the wrong cause.

    The dashboard renders this string, so a generic message costs an operator
    the difference between "the agent crashed" and "I typed the wrong port".
    """
    client._die(BenchConnectionError("connection lost: recv failed: [Errno 104]"))
    with pytest.raises(BenchConnectionError, match="Errno 104"):
        client.call("agent.status")


def test_the_first_cause_of_death_wins(client):
    """Later cleanup must not overwrite the diagnosis.

    ``close()`` also calls ``_die``, and it runs after the rx loop has already
    recorded why the connection dropped. Reporting "connection closed" then
    would replace the real cause with a description of the tidying up.
    """
    client._die(BenchConnectionError("connection lost: recv failed: [Errno 104]"))
    client._die(BenchConnectionError("connection closed"))
    with pytest.raises(BenchConnectionError, match="Errno 104"):
        client.call("agent.status")


def test_death_marks_the_client_disconnected(client):
    assert client.is_connected
    client._die(BenchConnectionError("gone"))
    assert not client.is_connected


def test_death_fails_calls_already_waiting(client):
    """The half that already worked, kept so a fix cannot regress it."""
    outcome = {}

    def caller():
        try:
            client.call("agent.status", timeout=DEFAULT_CALL_TIMEOUT)
        except BenchConnectionError as exc:
            outcome["error"] = str(exc)

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert client._writer.sent, "the request was never sent"
    client._die(BenchConnectionError("connection lost: peer reset"))
    thread.join(timeout=5.0)
    assert not thread.is_alive()
    assert "peer reset" in outcome.get("error", "")


def test_a_reconnect_clears_the_previous_deaths_reason(client):
    """A reused client must not report the last session's failure."""
    client._die(BenchConnectionError("connection lost: recv failed"))
    assert client._death is not None
    # connect() clears the latch before doing anything else; it then fails to
    # reach a nonexistent agent, which is fine — the latch is what we check.
    with pytest.raises(BenchConnectionError, match="could not reach agent"):
        client.connect()
    assert client._death is None, (
        "a reconnect attempt left the old session's death latched, so the next "
        "failure would be reported with a stale reason"
    )


class _EofReader:
    """A reader that reports one clean EOF, then blocks.

    ``read_frame`` returning None is the orderly-shutdown path: the agent
    closed the socket without an error. Distinct from a reset, and it used to
    be handled worse — ``_rx_loop`` broke out of the loop and set
    ``_connected = False`` in its ``finally``, but never swept ``_pending``.
    An in-flight call therefore waited out the full timeout even though the
    client already knew the agent had gone.
    """

    def __init__(self):
        self.eof_sent = threading.Event()
        self._release = threading.Event()

    def read_frame(self, timeout=None):
        if not self.eof_sent.is_set():
            self.eof_sent.set()
            return None
        self._release.wait(timeout=5.0)
        return None


def test_a_clean_eof_fails_calls_in_flight():
    """Exercises the real ``_rx_loop``, not a hand-driven ``_die``.

    ``systemctl stop`` gives a clean close where ``systemctl kill`` gives a
    reset, so both paths reach an operator in practice.
    """
    c = RemoteClient.__new__(RemoteClient)
    RemoteClient.__init__(c, _endpoint())
    c._writer = _DeadWriter()
    c._reader = _EofReader()
    c._connected = True

    outcome = {}

    def caller():
        t0 = time.monotonic()
        try:
            c.call("agent.status", timeout=DEFAULT_CALL_TIMEOUT)
        except BenchConnectionError as exc:
            outcome["error"] = str(exc)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = f"{type(exc).__name__}: {exc}"
        outcome["elapsed"] = time.monotonic() - t0

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    time.sleep(0.1)
    assert c._writer.sent, "the request was never sent"

    rx = threading.Thread(target=c._rx_loop, daemon=True)
    rx.start()

    thread.join(timeout=DEFAULT_CALL_TIMEOUT + 5.0)
    assert not thread.is_alive(), "the call never returned"
    assert outcome["elapsed"] < 2.0, (
        f"a clean EOF took {outcome['elapsed']:.1f}s to fail an in-flight "
        f"call; it waited out the {DEFAULT_CALL_TIMEOUT:.0f}s call timeout"
    )
    assert "closed the connection" in outcome.get("error", ""), outcome
    assert not c.is_connected


def test_a_live_client_still_sends(client):
    """The complement: this must not have made every call fail.

    A fix that reported "dead" too eagerly would be worse than the bug — it
    would break every working client instead of slowing down a dying one.
    """
    outcome = {}

    def caller():
        try:
            client.call("agent.status", timeout=1.0)
        except Exception as exc:  # noqa: BLE001
            outcome["error"] = type(exc).__name__

    thread = threading.Thread(target=caller, daemon=True)
    thread.start()
    thread.join(timeout=5.0)
    assert client._writer.sent, "a healthy client did not send its request"
    frame_type, _ = client._writer.sent[0]
    assert frame_type == FrameType.REQ
    # It times out because nothing answers a hand-built writer; the point is
    # that it got as far as sending rather than being refused up front.
    assert outcome.get("error") == "BenchTimeoutError", outcome

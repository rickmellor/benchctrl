"""The bench host's own LAN identity, for the display's top-left panel.

Not bench data. Everything else on the panel arrives from the agent over the
observer session; this is read from the kernel of the machine *serving the page*.
It is here rather than in :py:mod:`benchctrl.dashboards.fui.view` because that
module is pure by contract — no sockets, no clock — and answering "what is my
address" is unavoidably both.

Why the panel wants it
----------------------

The board boots headless into the kiosk, and its address comes from DHCP. When
that address moves, every remote route to the bench breaks at once: ssh, the
tunnel the display is meant to be tested over, and the host entry in the
operator's ``~/.ssh/config``. The screen on the bench is then the only place the
new address exists, and until now it was the one thing the screen did not say.

The states are kept distinct for the same reason the instrument rail
distinguishes its several kinds of dark: the operator's next move differs.

======================  ==================================================
:py:data:`LAN_OK`       an address on a real interface — the normal state
:py:data:`NO_ADDRESS`   the link is up but nothing has been assigned, so
                        the DHCP server is the thing to look at
:py:data:`NO_CARRIER`   nothing is plugged in, or the other end is dark
:py:data:`LAN_UNKNOWN`  we have not probed, or the probe itself failed;
                        never shown as an absence of network
======================  ==================================================

The one rule
------------

**A displayed address is one this kernel holds right now, or there is no address
shown.** A wrong-but-plausible IP on a bench display is worse than a blank: it
sends an operator to ssh at a host that stopped being the bench, and the timeout
they get looks like the bench being down. So a failed probe reports
:py:data:`LAN_UNKNOWN` and drops the address — it does not keep the last one.
"""

from __future__ import annotations

import socket
import time
from pathlib import Path
from typing import Callable, Optional

#: An address on a real interface. The ordinary state.
LAN_OK = "LAN"

#: Carrier, but no address assigned — a DHCP failure looks like this.
NO_ADDRESS = "NO ADDRESS"

#: No carrier: unplugged, or the switch port is dark.
NO_CARRIER = "NO CARRIER"

#: We do not know. Either nothing has probed yet or the probe failed. Explicitly
#: not an assertion that the network is down.
LAN_UNKNOWN = "UNKNOWN"

#: How long a reading is reused. The kiosk fetches the view twice a second and
#: this costs a socket plus two small reads from ``/sys``; an address does not
#: change on that timescale. Kept short so that when one *does* change, the
#: screen an operator is staring at catches up within a frame or two.
CACHE_TTL_S = 2.0

#: Where the default route is read from. A module constant so a test can point
#: the reader elsewhere without monkeypatching ``open``.
PROC_ROUTE = Path("/proc/net/route")

SYS_NET = Path("/sys/class/net")


def classify(
    ip: Optional[str],
    *,
    operstate: Optional[str],
    carrier: Optional[bool],
) -> str:
    """Decide which of the four states a reading is. Pure, and the whole point.

    Split out from the probing so the interesting half is testable without a
    network: every branch here is a claim the display will make on a bench.
    """
    # An address settles it, and it is checked first on purpose. A working
    # interface whose `operstate` this kernel reports as "unknown" — which is
    # what a bridge or a WSL interface does — must not be downgraded to a fault
    # when we are demonstrably holding an address on it.
    if ip:
        return LAN_OK
    # No address. Now the distinction worth drawing is *why*, and carrier is the
    # only thing that separates "nobody assigned me one" from "there is no cable".
    if carrier is False or operstate == "down":
        return NO_CARRIER
    if operstate is None and carrier is None:
        # Nothing readable to base a claim on. Saying NO CARRIER here would
        # invent a diagnosis; the honest report is that we cannot tell.
        return LAN_UNKNOWN
    return NO_ADDRESS


def _default_iface(read_text: Callable[[Path], str]) -> Optional[str]:
    """The interface carrying the default route, or None.

    ``/proc/net/route`` rather than parsing ``ip route``: no subprocess on a
    board that paints this panel twice a second, and no dependency on iproute2
    being installed in the kiosk's PATH.
    """
    try:
        lines = read_text(PROC_ROUTE).splitlines()
    except OSError:
        return None
    best: Optional[tuple[int, str]] = None
    for line in lines[1:]:  # first line is the header
        fields = line.split()
        if len(fields) < 8:
            continue
        iface, destination, _gw, _flags, _refcnt, _use, metric = fields[:7]
        # Destination 00000000 is the default route. Hex, little-endian, and
        # compared as a string because that is exactly how the kernel writes it.
        if destination != "00000000":
            continue
        try:
            rank = int(metric)
        except ValueError:
            continue
        # Lowest metric wins, matching what the kernel will actually choose. A
        # box with both wired and wireless up has two default routes, and picking
        # the wrong one would report carrier for an interface no traffic uses.
        if best is None or rank < best[0]:
            best = (rank, iface)
    return best[1] if best else None


def _probe_ip() -> Optional[str]:
    """The source address the kernel would use to reach off-box.

    A connected UDP socket, which sends nothing: no packet leaves, no name is
    resolved, nothing blocks, and it works on a board with no DNS. The
    alternative — ``gethostbyname(gethostname())`` — returns 127.0.1.1 on Debian
    and would put a loopback address on a bench display as though it were the
    bench's address.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        # 192.0.2.0/24 is TEST-NET-1: reserved by RFC 5737 for documentation and
        # guaranteed never to be a real host, so this cannot accidentally name a
        # machine somebody actually has.
        sock.connect(("192.0.2.1", 9))
        ip = sock.getsockname()[0]
    except OSError:
        # No route at all. Not an error worth logging every two seconds on a
        # board whose journal is on the same 2 GB filesystem as everything else.
        return None
    finally:
        sock.close()
    return str(ip) if ip else None


def _routable(ip: Optional[str]) -> Optional[str]:
    """``ip`` if it is an address a workstation could reach, else None.

    Deliberately here and not inside :py:func:`_probe_ip`. It was written there
    first, which meant the rule was a property of one collaborator rather than of
    this module: a test injecting its own ``probe_ip`` — and any future
    alternative probe — bypassed it and the panel would happily print
    ``127.0.1.1`` as the bench's address. The invariant belongs at the point the
    address is decided, where every path goes through it.

    ``127.0.1.1`` specifically is what Debian puts in ``/etc/hosts`` for the
    hostname, so it is the value a naive implementation lands on.
    """
    if not ip or ip.startswith("127.") or ip == "0.0.0.0":  # noqa: S104
        return None
    return ip


def _read_text(path: Path) -> str:
    return path.read_text()


def lan_status(
    *,
    probe_ip: Callable[[], Optional[str]] = _probe_ip,
    read_text: Callable[[Path], str] = _read_text,
) -> dict:
    """One reading of this host's LAN identity.

    Returns the block the view carries: ``state`` (one of the four constants),
    ``ip``, ``iface`` and ``hostname``. ``ip`` is an empty string rather than
    ``None`` whenever there is nothing to show, so the renderer has one falsy
    thing to test and cannot print the word "None" on a bench display.
    """
    iface = _default_iface(read_text)
    operstate: Optional[str] = None
    carrier: Optional[bool] = None
    if iface:
        try:
            operstate = read_text(SYS_NET / iface / "operstate").strip()
        except OSError:
            operstate = None
        try:
            carrier = read_text(SYS_NET / iface / "carrier").strip() == "1"
        except OSError:
            # ENOENT when the interface has just gone; EINVAL from the kernel
            # when it is administratively down. Neither is a carrier claim.
            carrier = None

    # Filtered before classify, so a loopback address is "no address" for the
    # purpose of every downstream decision — not just blanked in the output.
    ip = _routable(probe_ip())
    state = classify(ip, operstate=operstate, carrier=carrier)

    try:
        hostname = socket.gethostname()
    except OSError:  # pragma: no cover - gethostname does not fail in practice
        hostname = ""

    return {
        "state": state,
        # Only ever populated when the state says there is an address. Belt and
        # braces against a future edit that returns an address alongside a
        # not-OK state: the panel would then be showing an address it has just
        # said it does not have.
        "ip": ip if (ip and state == LAN_OK) else "",
        "iface": iface or "",
        "hostname": hostname,
    }


class LanProbe:
    """:py:func:`lan_status` with a short TTL, for the request path.

    The server builds a view per request; without this, a kiosk left running for
    a week does a few million socket setups to answer a question whose answer
    changes about never.
    """

    def __init__(
        self,
        *,
        ttl_s: float = CACHE_TTL_S,
        clock: Callable[[], float] = time.monotonic,
        status: Callable[..., dict] = lan_status,
    ) -> None:
        self._ttl_s = ttl_s
        self._clock = clock
        self._status = status
        self._cached: Optional[dict] = None
        self._read_at = 0.0

    def __call__(self) -> dict:
        now = self._clock()
        if self._cached is None or (now - self._read_at) >= self._ttl_s:
            # A failure replaces the cache rather than leaving the old reading in
            # it. Holding a good address across a network drop is precisely the
            # stale-but-plausible readout the panel may not show.
            self._cached = self._status()
            self._read_at = now
        return self._cached

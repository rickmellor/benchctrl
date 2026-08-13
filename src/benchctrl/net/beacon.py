"""UDP discovery beacon — zeroconf-like, without the dependency.

The agent broadcasts a small JSON beacon; hosts listen and build a list of
benches. Roughly 40 lines each way, stdlib only, and it works on the offline
kit as shipped — python-zeroconf is not in it for either architecture, and
mDNS from inside a container fights Docker's bridge network besides.

The beacon deliberately carries **no inventory**. Broadcasting instrument
models and serial numbers to an entire subnet tells anyone listening exactly
what hardware is in the room and what it is worth. What it does carry is a
token *fingerprint* — a hash prefix — so a client can recognise its own
bench without the beacon being useful to anyone else, plus a device count so
"is anything attached" is answerable before connecting.

For standards-compliant DNS-SD, install a static Avahi service file
alongside this (see :py:func:`avahi_service_xml`). The board already runs
avahi-daemon with exactly that pattern for Arduino's own service, so it is a
proven path — and it means ``avahi-browse`` and other DNS-SD tools find the
bench too.
"""

from __future__ import annotations

import json
import logging
import socket
import threading
import time
from dataclasses import dataclass
from typing import Callable, Optional

from benchctrl.config import DEFAULT_PORT, SERVICE_TYPE
from benchctrl.net.auth import token_fingerprint

log = logging.getLogger("benchctrl.net.beacon")

BEACON_PORT = 9738
BEACON_INTERVAL_S = 2.0
SERVICE_MAGIC = "benchctrl"
BEACON_VERSION = 1


@dataclass(frozen=True)
class BeaconRecord:
    """One advertised agent."""

    host: str
    port: int
    address: str
    agent_version: str = ""
    fingerprint: str = ""
    device_count: int = 0
    received_at: float = 0.0

    @property
    def endpoint(self) -> str:
        return f"{self.address}:{self.port}"

    def matches_token(self, token: Optional[str]) -> bool:
        """Whether this bench was configured with ``token``."""
        if not token or not self.fingerprint:
            return False
        return token_fingerprint(token) == self.fingerprint

    def __str__(self) -> str:
        who = self.host or self.address
        devices = f"{self.device_count} device(s)" if self.device_count else "no devices"
        return f"{who} at {self.endpoint} — {devices}, agent {self.agent_version or '?'}"


def build_payload(
    *,
    host: str,
    port: int = DEFAULT_PORT,
    token: Optional[str] = None,
    agent_version: str = "",
    device_count: int = 0,
) -> bytes:
    return json.dumps(
        {
            "svc": SERVICE_MAGIC,
            "v": BEACON_VERSION,
            "host": host,
            "port": port,
            "agent": agent_version,
            "fp": token_fingerprint(token) if token else "",
            "ndev": device_count,
        },
        separators=(",", ":"),
    ).encode("utf-8")


def parse_payload(data: bytes, address: str) -> Optional[BeaconRecord]:
    """Decode a beacon, or None if it isn't one of ours."""
    try:
        payload = json.loads(data.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError):
        return None
    if payload.get("svc") != SERVICE_MAGIC or payload.get("v") != BEACON_VERSION:
        return None
    try:
        port = int(payload.get("port", DEFAULT_PORT))
    except (TypeError, ValueError):
        return None
    return BeaconRecord(
        host=str(payload.get("host", "")),
        port=port,
        address=address,
        agent_version=str(payload.get("agent", "")),
        fingerprint=str(payload.get("fp", "")),
        device_count=int(payload.get("ndev", 0) or 0),
        received_at=time.monotonic(),
    )


class BeaconTransmitter:
    """Broadcasts the agent's presence."""

    def __init__(
        self,
        *,
        host: str,
        port: int = DEFAULT_PORT,
        token: Optional[str] = None,
        agent_version: str = "",
        device_count_fn: Optional[Callable[[], int]] = None,
        beacon_port: int = BEACON_PORT,
        interval_s: float = BEACON_INTERVAL_S,
    ) -> None:
        self.host = host
        self.port = port
        self.token = token
        self.agent_version = agent_version
        self.device_count_fn = device_count_fn
        self.beacon_port = beacon_port
        self.interval_s = interval_s
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.sent = 0

    def start(self) -> BeaconTransmitter:
        if self._thread is not None:
            return self
        self._stop.clear()
        self._thread = threading.Thread(
            target=self._run, name="benchctrl-beacon-tx", daemon=True
        )
        self._thread.start()
        return self

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2.0)
            self._thread = None

    def send_once(self, sock: Optional[socket.socket] = None) -> None:
        owned = sock is None
        if sock is None:
            sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            payload = build_payload(
                host=self.host,
                port=self.port,
                token=self.token,
                agent_version=self.agent_version,
                device_count=self.device_count_fn() if self.device_count_fn else 0,
            )
            sock.sendto(payload, ("255.255.255.255", self.beacon_port))
            self.sent += 1
        except OSError as exc:
            log.debug("beacon: send failed (%s) — is the network up?", exc)
        finally:
            if owned:
                sock.close()

    def _run(self) -> None:
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
        try:
            while not self._stop.is_set():
                self.send_once(sock)
                self._stop.wait(self.interval_s)
        finally:
            sock.close()

    def __enter__(self) -> BeaconTransmitter:
        return self.start()

    def __exit__(self, *exc_info) -> None:
        self.stop()


def listen(
    timeout: float = 5.0,
    *,
    beacon_port: int = BEACON_PORT,
    token: Optional[str] = None,
    stop_after: int = 0,
) -> list[BeaconRecord]:
    """Collect beacons for ``timeout`` seconds.

    Args:
        token: when given, only benches whose fingerprint matches are
            returned — "find *my* bench" rather than "find any bench".
        stop_after: return as soon as this many benches are seen.
    """
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    try:
        sock.bind(("", beacon_port))
    except OSError as exc:
        sock.close()
        raise OSError(
            f"could not listen for beacons on UDP {beacon_port}: {exc}"
        ) from exc

    found: dict[str, BeaconRecord] = {}
    deadline = time.monotonic() + timeout
    try:
        while time.monotonic() < deadline:
            sock.settimeout(max(0.05, deadline - time.monotonic()))
            try:
                data, addr = sock.recvfrom(2048)
            except socket.timeout:
                break
            except OSError:
                break
            record = parse_payload(data, addr[0])
            if record is None:
                continue
            if token and not record.matches_token(token):
                continue
            found[record.endpoint] = record
            if stop_after and len(found) >= stop_after:
                break
    finally:
        sock.close()
    return sorted(found.values(), key=lambda r: r.endpoint)


def avahi_service_xml(
    *,
    port: int = DEFAULT_PORT,
    agent_version: str = "",
    fingerprint: str = "",
) -> str:
    """A static Avahi service file advertising the agent over DNS-SD.

    Install at ``/etc/avahi/services/benchctrl.service``. avahi-daemon picks
    up files in that directory automatically, so this needs no Python
    dependency and no daemon of our own — the same mechanism Arduino's own
    ``arduino.service`` already uses on the board.
    """
    txt = [f"<txt-record>v={BEACON_VERSION}</txt-record>"]
    if agent_version:
        txt.append(f"<txt-record>agent={agent_version}</txt-record>")
    if fingerprint:
        txt.append(f"<txt-record>fp={fingerprint}</txt-record>")
    records = "\n    ".join(txt)
    return f"""<?xml version="1.0" standalone='no'?>
<!DOCTYPE service-group SYSTEM "avahi-service.dtd">
<!-- Installed by benchctrl. Advertises the bench agent over DNS-SD. -->
<service-group>
  <name replace-wildcards="yes">benchctrl on %h</name>
  <service>
    <type>{SERVICE_TYPE}</type>
    <port>{port}</port>
    {records}
  </service>
</service-group>
"""

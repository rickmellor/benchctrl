"""HMAC challenge-response over a pre-shared token.

The token never crosses the wire. Both sides prove knowledge of it by
MACing a pair of nonces, so a passive listener on shared wifi — a hotel, a
conference, a lab network — cannot lift the secret by watching a login. The
server nonce bounds replay to a single handshake.

Stdlib only: ``hmac``, ``hashlib``, ``secrets``.

What this does **not** give you: confidentiality. Everything after the
handshake is plaintext on the LAN. Anyone who can sniff the link can read
setpoints and measurements, and anyone who can inject packets can interfere.
For a hostile network the documented answer is an SSH tunnel —
``ssh -L 9737:localhost:9737 arduino@<board>.local`` — since sshd is already
listening. What it *does* give you is that a stray host on the same subnet
cannot drive your instruments, which is the threat that matters when the
thing on the other end can push current into a DUT.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import secrets
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("benchctrl.net.auth")

PROTOCOL_VERSION = 1
AUTH_METHOD = "hmac-sha256"

#: Domain separator — stops a MAC computed for one purpose being replayed
#: for another if this token is ever reused elsewhere.
CONTEXT = b"benchctrl-v1"

NONCE_BYTES = 16

#: Consecutive failures from one address before it is tarpitted.
MAX_FAILURES = 3
TARPIT_SECONDS = 30.0

#: A peer that connects and then says nothing holds a slot open.
HANDSHAKE_TIMEOUT = 5.0


def new_nonce() -> str:
    return secrets.token_urlsafe(NONCE_BYTES)


def new_token() -> str:
    """Generate a shared secret for a fresh install."""
    return secrets.token_urlsafe(32)


def compute_mac(token: str, nonce_client: str, nonce_server: str) -> str:
    """The MAC both sides compute independently."""
    message = b"|".join(
        (nonce_client.encode("utf-8"), nonce_server.encode("utf-8"), CONTEXT)
    )
    return hmac.new(token.encode("utf-8"), message, hashlib.sha256).hexdigest()


def verify_mac(token: str, nonce_client: str, nonce_server: str, presented: str) -> bool:
    """Constant-time comparison — never ``==`` on a secret-derived value."""
    expected = compute_mac(token, nonce_client, nonce_server)
    return hmac.compare_digest(expected, presented or "")


def token_fingerprint(token: str) -> str:
    """A short public identifier for a token.

    Broadcast in the discovery beacon so a client can recognise "this is my
    bench" without the beacon carrying anything that helps an attacker. It
    is a hash prefix, not a credential.
    """
    if not token:
        return ""
    return hashlib.sha256(token.encode("utf-8")).hexdigest()[:8]


@dataclass
class FailureTracker:
    """Per-address failure counting and tarpitting.

    Keyed by address only, not address+port: an attacker gets a new source
    port for free on every reconnect, so counting per-port would make the
    limit meaningless.
    """

    max_failures: int = MAX_FAILURES
    tarpit_seconds: float = TARPIT_SECONDS
    _failures: dict[str, int] = field(default_factory=dict)
    _blocked_until: dict[str, float] = field(default_factory=dict)

    def is_blocked(self, address: str, now: Optional[float] = None) -> bool:
        now = time.monotonic() if now is None else now
        until = self._blocked_until.get(address)
        if until is None:
            return False
        if now >= until:
            self._blocked_until.pop(address, None)
            self._failures.pop(address, None)
            return False
        return True

    def seconds_remaining(self, address: str, now: Optional[float] = None) -> float:
        now = time.monotonic() if now is None else now
        return max(0.0, self._blocked_until.get(address, 0.0) - now)

    def record_failure(self, address: str, now: Optional[float] = None) -> bool:
        """Count a failure. Returns True if the address is now tarpitted."""
        now = time.monotonic() if now is None else now
        count = self._failures.get(address, 0) + 1
        self._failures[address] = count
        if count >= self.max_failures:
            self._blocked_until[address] = now + self.tarpit_seconds
            log.warning(
                "auth: %s tarpitted for %.0fs after %d failures",
                address,
                self.tarpit_seconds,
                count,
            )
            return True
        return False

    def record_success(self, address: str) -> None:
        self._failures.pop(address, None)
        self._blocked_until.pop(address, None)


def build_hello(client_name: str = "benchctrl") -> tuple[dict, str]:
    """Client step 1. Returns ``(payload, nonce_client)``."""
    nonce = new_nonce()
    return {"v": PROTOCOL_VERSION, "nonce_c": nonce, "client": client_name}, nonce


def build_challenge(agent_name: str = "benchctrl-agent") -> tuple[dict, str]:
    """Server step 2. Returns ``(payload, nonce_server)``."""
    nonce = new_nonce()
    return (
        {
            "v": PROTOCOL_VERSION,
            "nonce_s": nonce,
            "agent": agent_name,
            "auth": AUTH_METHOD,
        },
        nonce,
    )


def build_auth(token: str, nonce_client: str, nonce_server: str) -> dict:
    """Client step 3."""
    return {"mac": compute_mac(token, nonce_client, nonce_server)}


def check_version(payload: dict) -> Optional[str]:
    """Return an error string if the peer speaks a version we don't."""
    version = payload.get("v")
    if version != PROTOCOL_VERSION:
        return (
            f"protocol version mismatch: peer speaks v{version}, "
            f"this build speaks v{PROTOCOL_VERSION}"
        )
    return None

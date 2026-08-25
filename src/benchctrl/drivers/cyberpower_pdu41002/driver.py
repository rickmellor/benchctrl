"""CyberPower PDU41002 switched PDU driver.

An 8-outlet, 1U, 120 V/20 A rackmount switched PDU. This is benchctrl's first
*switching* device and its first with a network interface: the bench could
already source, sink, measure and load, but not cut and restore mains power.

Two transports, **one** CLI. The device speaks the same line-oriented command
language on its serial console and over SSH — verified byte-for-byte against
hardware, so a single grammar, parser and simulator serve both paths. SNMP is
deliberately not used: the CLI covers switching, state, metering and identity,
and one protocol means one parser and one simulator instead of two.

    from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

    # serial (the bootstrap/recovery transport — no key, no negotiation)
    with CyberPowerPDU41002.open(port="/dev/ttyUSB0",
                                 allowed_outlets=(7, 8)) as pdu:
        print(pdu.read_identity())
        print(pdu.outlet_states())

    # ssh — same surface, same parsers
    with CyberPowerPDU41002.open(host="pdu-benchctrl",
                                 allowed_outlets=(7, 8)) as pdu:
        print(pdu.read_device_status())

Credentials
-----------
The password is **never** a positional argument in normal use and never belongs
in a config file. Left as ``None`` it is read from ``BENCHCTRL_PDU_PASSWORD``
in the environment of whichever host actually runs the driver, following the
``BENCHCTRL_TOKEN`` precedent in :py:mod:`benchctrl.config`.

That is not a style preference. ``DeviceConfig.open`` is round-tripped verbatim
by ``DeviceConfig.to_dict()`` (unlike ``EndpointConfig.token``, which is masked
to ``"***"``), and it is forwarded across the agent RPC wire — which is
HMAC-authenticated but **not encrypted**. A password placed there would be both
written back into any saved config and sent in clear over the LAN.

Safety
------
For every other instrument on the bench, "safe" means *output off*. For a PDU,
cutting mains is itself the disruptive act: it can de-power a DUT mid-
measurement. So the default is **do not move the contactors**, and every
switching call is opt-in per outlet via a mandatory ``allowed_outlets``
allowlist.

Two device behaviours drive the design and are easy to get wrong:

- **``oltctrl`` reports nothing.** A switch command answers with a blank line
  and a re-prompt, byte-identical whether or not the outlet moved. Read-back
  verification is therefore mandatory, not prudent — there is no other way to
  learn what happened.
- **The CLI is single-session across all transports**, and closing the port
  does *not* end the device session. So :py:meth:`close` must send ``exit``, or
  the other transport is left locked out in a way that looks exactly like bad
  credentials.

See ``tests/fixtures/pdu41002/`` for the hardware captures all of this is
derived from, and ``KNOWN_LIMITATIONS.md`` for the SSH firmware defects.
"""

from __future__ import annotations

import logging
import os
import re
import time
from dataclasses import dataclass
from typing import Iterable, Optional

from benchctrl.drivers.cyberpower_pdu41002.links import (
    BAUDRATE,
    LinkError,
    SerialLink,
    SshLink,
)

log = logging.getLogger("benchctrl.drivers.cyberpower_pdu41002.driver")

#: The device's ready prompt. The trailing space is significant: it is the
#: read-until sentinel for every command.
PROMPT = "CyberPower > "

#: Environment variable holding the CLI password. Read on the host that runs
#: the driver, so the secret never enters config or the agent RPC wire.
PASSWORD_ENV = "BENCHCTRL_PDU_PASSWORD"

#: Login prompts, used both to authenticate and — critically — to detect that
#: an idle timeout has dropped the session mid-sequence.
_LOGIN_NAME = "Login Name :"
_LOGIN_PASSWORD = "Login Password :"

#: The *ssh client's* password prompt, not the device's. Over SSH the username
#: rides in the argv, so the device never shows ``Login Name :`` at all — ssh
#: asks for the password and the device goes straight to its banner. Matched
#: case-insensitively on this fragment because the surrounding text varies with
#: OpenSSH version (``admin@host's password:`` vs ``(admin@host) password:``).
_SSH_PASSWORD_PROMPT = "assword:"

#: ssh's own refusal, distinct from the device rejecting a credential.
_SSH_DENIED_MARKERS = (
    "Permission denied",
    "Too many authentication failures",
    "No supported authentication methods",
)

#: Emitted by the device (or by ssh) when the single CLI session is already held.
#:
#: **Only meaningful during login.** ``Connection to `` is what ssh prints when a
#: session ends for *any* reason, so mid-session it says nothing about
#: contention — see ``_LINK_GONE_MARKERS``.
_HANGUP_MARKERS = ("closed by remote host", "Connection to ")

#: The ssh client's notice that the *device* ended the session — which is what
#: an idle timeout looks like over the network.
#:
#: Measured on firmware 1.3.4: after ~180 s idle the PDU disconnects and ssh
#: prints, in one 130-byte burst,
#: "Received disconnect from … :11: user close and disconnect!" followed by
#: "Disconnected from …". Without this the symptom was a bare
#: "no prompt within 12.0s" — a timeout, implying the device was slow, when in
#: fact the link was gone.
#:
#: The distinction is not cosmetic: **an idle logout is recoverable over serial
#: and not over ssh.** On serial the session drops to ``Login Name :`` on the
#: same open port and the driver re-authenticates in place. Over ssh the client
#: process has exited, so there is nothing left to re-authenticate *on* — the
#: caller has to open a new session. A timeout invites a retry that can never
#: work; a connection error tells the truth.
_SSH_DISCONNECT_MARKERS = ("Received disconnect from", "Disconnected from")

#: Everything that means "the link is gone" **after** a successful login.
#:
#: Deliberately a superset of ``_SSH_DISCONNECT_MARKERS`` plus the
#: ``_HANGUP_MARKERS``, and the reason is a bug this cost:
#: ``_HANGUP_MARKERS`` is only diagnostic *during login*, where the device
#: hanging up straight after the banner really does mean another session holds
#: the CLI. ``Connection to `` is simply what ssh prints when a session ends for
#: **any** reason, and measurement showed the idle logout produces *either*
#: wording depending on how ssh notices — so a mid-session match on it was
#: reported as ``PDU41002SessionError``: "another session is logged in — send
#: 'exit' on it". Which is doubly unhelpful, because nothing else *was* logged
#: in and the advice cannot work on a link that no longer exists.
#:
#: Past login the cause is knowable from position rather than from wording: the
#: session was established, so anything that ends it now is a dead link and the
#: recovery is to reopen. Hence one set used in one place
#: (:py:meth:`_raise_if_disconnected`, called only from :py:meth:`_round_trip`)
#: rather than a wording contest that a future OpenSSH release would silently
#: win.
_LINK_GONE_MARKERS = (*_SSH_DISCONNECT_MARKERS, *_HANGUP_MARKERS)

#: The serial console's fourth login outcome, and the awkward one: it means
#: **either** a wrong credential **or** a correct one the device cannot yet
#: service, with no way to tell which.
#:
#: Measured on firmware 1.3.4. A wrong password and a correct password submitted
#: within ~15 s of a previous session closing produce *byte-identical* output —
#: "Please wait for authentication....", a line of dots for ~15 s, then
#: "Login Failed", and then silence: no re-prompt follows, so a read waiting for
#: one waits forever.
#:
#: That silence is why this needs its own marker rather than falling through to
#: the timeout: without it the driver reports "the device did not answer" when
#: the device answered clearly, and the retry that would have worked never
#: happens.
#:
#: The ambiguity is not resolvable from the bytes, so the driver retries within
#: its budget instead of classifying: a wrong password fails the same way every
#: time and ends as an auth error when the budget runs out, while a busy device
#: succeeds on a later attempt. It also means ``close()`` sending ``exit`` gets
#: the session released *eventually* rather than immediately — the device holds
#: it for a few seconds more.
_LOGIN_FAILED = "Login Failed"

#: Outlet indices are plain ints and nothing else. The alias exists to make the
#: signatures self-documenting, *not* to widen them: see ``_coerce_outlet`` for
#: why ``"all"``, ``"b1"``, ``"b2"`` and collections are rejected structurally.
OutletIndex = int

#: The **only** shape of switching command this driver is permitted to emit.
#:
#: This is a whitelist applied to the rendered bytes, immediately before the
#: write, and it is the second of two independent guards — the first being
#: :py:meth:`CyberPowerPDU41002._coerce_outlet`, which rejects anything that is
#: not a plain in-range ``int``. Two guards rather than one because they fail
#: differently: the coercion catches a bad *argument*, this catches a bad
#: *rendering*, and a format-string bug is exactly the kind of defect that
#: turns a validated ``int`` back into a dangerous string.
#:
#: ``\d+`` is not a laxity here: the index has already been range-checked, and
#: the point of this pattern is to reject the aggregate spellings the device
#: accepts — ``index all``, ``index b1``, ``index b2`` — each of which is one
#: line that moves every contactor on the unit. It also excludes ``guest``
#: (which addresses a *different physical box* in a daisy chain) and
#: ``menumode`` (a one-way trap: the manual states returning to the CLI needs a
#: full logout/login, so every later parse would fail against menu output while
#: the link still looked healthy).
_SAFE_OLTCTRL_RE = re.compile(
    r"^oltctrl index \d+ act "
    r"(on|off|reboot|delayon|delayoff|delayreboot|cancel)$"
)

#: Actions accepted by :py:meth:`CyberPowerPDU41002.set_outlet_state`, keyed by
#: ``(on, delayed)``. Spelled out rather than assembled from fragments so the
#: emitted verb is greppable and the mapping is auditable at a glance.
_SWITCH_ACTIONS = {
    (True, False): "on",
    (False, False): "off",
    (True, True): "delayon",
    (False, True): "delayoff",
}


# ---------------------------------------------------------------------------
# Exceptions — kept independent of benchctrl.exceptions, as the other drivers
# do, so `benchctrl.bench` need not import benchctrl's whole surface.
# ---------------------------------------------------------------------------


class PDU41002Error(RuntimeError):
    """Base class for PDU41002 driver errors."""


class PDU41002ConnectionError(PDU41002Error):
    """Could not open the transport, or lost it mid-stream."""


class PDU41002ProtocolError(PDU41002Error):
    """Unexpected response shape from the device."""


class PDU41002TimeoutError(PDU41002Error, TimeoutError):
    """No prompt within the timeout."""


class PDU41002ValueError(PDU41002Error, ValueError):
    """Client-side check failed before anything was sent."""


class PDU41002AuthError(PDU41002Error):
    """Login was refused, or no password was available."""


class PDU41002SessionError(PDU41002Error):
    """The device's single CLI session is held by someone else.

    Raised specifically so this is not misread as an authentication failure.
    The device accepts the password, prints its banner, *and then* hangs up
    when another session (on either transport) is already logged in. Send
    ``exit`` on the other session — or note that merely closing its port does
    not release it.
    """


class PDU41002PolicyError(PDU41002Error):
    """A switching call named an outlet outside ``allowed_outlets``."""


class PDU41002CommandError(PDU41002Error):
    """The device rejected a command.

    Attributes:
        command: what was sent.
        message: the device's own error text.
        marker: zero-based column of the ``^`` caret, when the device supplied
            one. ``None`` for the out-of-range-index shape, which has no caret.
    """

    def __init__(
        self, command: str, message: str, marker: Optional[int] = None
    ) -> None:
        super().__init__(
            f"device rejected {command!r}: {message}"
            + (f" (at column {marker})" if marker is not None else "")
        )
        self.command = command
        self.message = message
        self.marker = marker


# ---------------------------------------------------------------------------
# Value types
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class PDU41002Info:
    """Identity, from ``sys show`` — the closest thing this device has to *IDN?."""

    name: str
    location: str
    contact: str
    model: str
    hardware_version: str
    firmware_version: str
    mac_address: str

    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "location": self.location,
            "contact": self.contact,
            "model": self.model,
            "hardware_version": self.hardware_version,
            "firmware_version": self.firmware_version,
            "mac_address": self.mac_address,
        }


@dataclass(frozen=True)
class PDU41002Status:
    """Device metering, from ``devsta show``.

    ``power_factor`` is ``None`` when the device prints ``----`` (it does so at
    zero load, which is not an error).
    """

    load_A: float
    load_W: float
    load_VA: float
    power_factor: Optional[float]
    voltage_V: float
    frequency_Hz: float
    peak_load_A: Optional[float] = None
    energy_kWh: Optional[float] = None

    def to_dict(self) -> dict:
        return {
            "load_A": self.load_A,
            "load_W": self.load_W,
            "load_VA": self.load_VA,
            "power_factor": self.power_factor,
            "voltage_V": self.voltage_V,
            "frequency_Hz": self.frequency_Hz,
            "peak_load_A": self.peak_load_A,
            "energy_kWh": self.energy_kWh,
        }


@dataclass(frozen=True)
class OutletConfig:
    """Per-outlet delays, from ``oltcfg``.

    These matter for correctness, not just information: a read-back after a
    switch must wait longer than ``on_delay_s``/``off_delay_s``, and those are
    operator-configurable. Hardcoding a retry window instead of deriving it
    from these values produces a driver that flakes when someone changes a
    delay.
    """

    index: int
    name: str
    on_delay_s: int
    off_delay_s: int
    reboot_duration_s: int

    def to_dict(self) -> dict:
        return {
            "index": self.index,
            "name": self.name,
            "on_delay_s": self.on_delay_s,
            "off_delay_s": self.off_delay_s,
            "reboot_duration_s": self.reboot_duration_s,
        }


# ---------------------------------------------------------------------------
# Driver
# ---------------------------------------------------------------------------


class CyberPowerPDU41002:
    """A connected CyberPower PDU41002.

    Construct via :py:meth:`open`, which takes **exactly one** of ``port``
    (serial) or ``host`` (SSH). Supplying both is an error rather than a
    silent preference: a run log must be able to answer "which wire did that
    mains switch travel on", and a driver that quietly picks one cannot.

    This class implements no :py:mod:`benchctrl.interfaces` Protocol. Per
    ``CONTRIBUTING.md`` convention 3 a Protocol is defined when the *second*
    instance of a device class lands, and a ``Switch`` abstraction generalised
    from a mains PDU would be a poor fit for, say, a signal multiplexer.
    """

    #: Retry margin added on top of the device's configured switching delay.
    _VERIFY_MARGIN_S = 3.0
    #: Gap between read-back polls while waiting for a contactor to settle.
    _VERIFY_POLL_S = 0.25
    #: How long to wait for a prompt on an ordinary command.
    _CMD_TIMEOUT_S = 12.0
    #: Total login budget. Generous because it has to cover *several* attempts:
    #: a serial login within ~15 s of a previous session closing answers
    #: "Login Failed" (see ``_LOGIN_FAILED``) and only succeeds on a retry, and
    #: each refusal costs the device's full ~15 s authentication attempt. The
    #: cost of the headroom is that a genuinely wrong password takes this long
    #: to be reported; the cost of less is a bench that cannot reopen its own
    #: PDU after switching transports.
    _LOGIN_TIMEOUT_S = 75.0
    #: Minimum window for the reply to a submitted password, regardless of how
    #: much of ``_LOGIN_TIMEOUT_S`` earlier attempts consumed. The device prints
    #: "Please wait for authentication...." and takes up to ~15 s over it. A
    #: floor rather than a share of the remaining budget because the
    #: alternative — a read too short to see the verdict — is reported as a
    #: rejected password, and hunting a correct credential is the most expensive
    #: wrong answer this driver can give.
    _AUTH_WAIT_S = 20.0
    #: Pause after a refusal before trying again. The device is mid-teardown of
    #: someone else's session; hammering it just burns the budget on refusals.
    _LOGIN_RETRY_S = 3.0

    def __init__(
        self,
        link,
        *,
        allowed_outlets: Iterable[int],
        username: str = "admin",
        password: Optional[str] = None,
        transport: str = "serial",
        outlet_count: int = 8,
        panic_outlets: Iterable[int] = (),
    ) -> None:
        self._link = link
        self._transport = transport
        self._username = username
        # Held only in memory, never logged, never in __repr__, never returned
        # by any method. See the module docstring.
        self.__password = password
        self._outlet_count = outlet_count
        self._allowed = frozenset(_coerce_outlet_set(allowed_outlets, "allowed_outlets"))
        self._panic = frozenset(_coerce_outlet_set(panic_outlets, "panic_outlets"))
        if not self._panic <= self._allowed:
            raise PDU41002ValueError(
                f"panic_outlets {sorted(self._panic - self._allowed)} are not in "
                f"allowed_outlets {sorted(self._allowed)}; the governor must "
                f"never be able to cut an outlet the driver itself may not touch"
            )
        self._buf = ""
        self._authed = False
        self._echoes = transport == "serial"
        self._config_cache: dict[int, OutletConfig] = {}

    # -- construction -------------------------------------------------------

    @classmethod
    def open(
        cls,
        *,
        port: Optional[str] = None,
        host: Optional[str] = None,
        allowed_outlets: Iterable[int],
        username: str = "admin",
        password: Optional[str] = None,
        baudrate: int = BAUDRATE,
        ssh_port: int = 22,
        panic_outlets: Iterable[int] = (),
        outlet_count: int = 8,
        env: Optional[dict] = None,
    ) -> "CyberPowerPDU41002":
        """Open a session over serial (``port``) or SSH (``host``).

        Args:
            port: serial device path, e.g. ``/dev/ttyUSB0``.
            host: hostname or IP for the SSH transport.
            allowed_outlets: **required.** Outlets this instance may switch.
                An allowlist, not a denylist: a config typo fails closed, and a
                daisy-chained unit cannot silently widen scope.
            password: leave ``None`` to read ``BENCHCTRL_PDU_PASSWORD`` from
                the environment. Passing it explicitly is supported for tests
                and simulators but should not appear in a config file.
            panic_outlets: outlets the safety governor may cut on trip. Must be
                a subset of ``allowed_outlets``. Empty by default.

        Raises:
            PDU41002ValueError: neither or both of ``port``/``host``.
            PDU41002AuthError: no password available, or login refused.
            PDU41002SessionError: the device's single CLI session is held.
        """
        if (port is None) == (host is None):
            raise PDU41002ValueError(
                "open() needs exactly one of port= (serial) or host= (ssh); "
                f"got port={port!r}, host={host!r}. Which transport carried a "
                f"mains switch must be unambiguous in the run log."
            )

        resolved = _resolve_password(password, env)

        if port is not None:
            link = SerialLink(port, baudrate=baudrate)
            transport = "serial"
        else:
            link = SshLink(str(host), username=username, port=ssh_port)
            transport = "ssh"

        pdu = cls(
            link,
            allowed_outlets=allowed_outlets,
            username=username,
            password=resolved,
            transport=transport,
            outlet_count=outlet_count,
            panic_outlets=panic_outlets,
        )
        try:
            link.open()
        except LinkError as e:
            raise PDU41002ConnectionError(str(e)) from e
        try:
            pdu._login()
        except Exception:
            pdu._close_link()
            raise
        return pdu

    # -- properties ---------------------------------------------------------

    @property
    def transport(self) -> str:
        """``"serial"`` or ``"ssh"`` — recorded so run logs are unambiguous."""
        return self._transport

    @property
    def is_open(self) -> bool:
        return bool(self._link is not None and self._link.is_open)

    @property
    def outlet_count(self) -> int:
        return self._outlet_count

    @property
    def allowed_outlets(self) -> frozenset:
        return self._allowed

    @property
    def panic_outlets(self) -> frozenset:
        return self._panic

    # -- lifecycle ----------------------------------------------------------

    def close(self) -> None:
        """Release the device session, then close the transport.

        **Sending ``exit`` is required, not courteous.** The device permits one
        CLI session across all transports and does *not* drop it when the port
        closes, so skipping this leaves the PDU unreachable from the other
        transport — and the resulting failure arrives *after* a successful
        password exchange, so it reads as bad credentials rather than a stale
        session.

        Never changes outlet state. Safe to call repeatedly.
        """
        if self._link is not None and self._link.is_open and self._authed:
            try:
                self._link.write(b"exit\r")
                # Give the device a moment to process it; the reply is
                # irrelevant and its absence must not raise from close().
                self._read_until([PROMPT, "Logout"], timeout=3.0)
            except (LinkError, PDU41002Error, OSError):
                log.debug("exit on close failed; session may remain held", exc_info=True)
        self._authed = False
        self._close_link()

    def _close_link(self) -> None:
        if self._link is not None:
            try:
                self._link.close()
            except Exception:  # pragma: no cover - best-effort
                pass

    def __enter__(self) -> "CyberPowerPDU41002":
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def __repr__(self) -> str:
        # No credential material, by construction and by test.
        return (
            f"CyberPowerPDU41002(transport={self._transport!r}, "
            f"open={self.is_open}, allowed_outlets={sorted(self._allowed)})"
        )

    # -- authentication -----------------------------------------------------

    def _login(self, *, timeout: Optional[float] = None) -> None:
        """Authenticate, by whichever route this transport actually uses.

        The two transports authenticate in genuinely different ways, and this is
        the one place the "one CLI over two pipes" abstraction does *not* hold:

        - **Serial** talks to the device's own two-step prompts
          (``Login Name :`` then ``Login Password :``).
        - **SSH** never shows those. The *ssh client* prompts for the password
          (``admin@host's password:``) because the username is already in the
          argv, and the device goes straight to its banner and prompt.

        So they get separate methods rather than one loop with branches. Sharing
        the loop was tried and is actively wrong: the serial path opens by
        poking with a bare CR to discover an unknown session state, and doing
        that on the SSH path submits an **empty password** to the client's
        prompt, burning an auth attempt.
        """
        budget = self._LOGIN_TIMEOUT_S if timeout is None else timeout
        if self._transport == "ssh":
            self._login_ssh(budget)
        else:
            self._login_serial(budget)

    def _login_ssh(self, budget: float) -> None:
        """Answer the ssh client's password prompt, then wait for the banner.

        Authentication is slow — ~7.5 s on firmware 1.3.4 between submitting the
        password and the banner appearing — so the waits here look generous on
        purpose.
        """
        deadline = time.monotonic() + budget
        text = self._read_until(
            [_SSH_PASSWORD_PROMPT, PROMPT, *_SSH_DENIED_MARKERS],
            timeout=min(budget, 20.0),
        )
        self._raise_if_hungup(text)
        self._raise_if_ssh_denied(text)

        if PROMPT in text:
            # Some paths reach the shell without a prompt (e.g. a key the device
            # did accept). Nothing to answer.
            self._authed = True
            return

        if _SSH_PASSWORD_PROMPT not in text:
            raise PDU41002TimeoutError(
                f"ssh never asked for a password and never reached "
                f"{PROMPT!r} (got {len(text)} bytes); the host may be "
                f"unreachable or the KEX workaround may have stopped working"
            )

        self._link.write(self.__password.encode("ascii") + b"\r")
        after = self._read_until(
            [PROMPT, _SSH_PASSWORD_PROMPT, *_SSH_DENIED_MARKERS],
            timeout=max(5.0, deadline - time.monotonic()),
        )
        # Order matters: the device prints its whole banner and *then* hangs up
        # when the single session is held, so the hangup check must come before
        # concluding anything from the presence or absence of the prompt.
        self._raise_if_hungup(after)
        self._raise_if_ssh_denied(after)
        if PROMPT in after:
            self._authed = True
            return
        if _SSH_PASSWORD_PROMPT in after:
            # A re-prompt is the client saying the password was wrong.
            raise PDU41002AuthError(
                f"ssh re-prompted for the {self._username!r} password: it was "
                f"rejected. Check {PASSWORD_ENV}."
            )
        raise PDU41002TimeoutError(
            f"password accepted but no {PROMPT!r} appeared over ssh within "
            f"{budget:.0f}s"
        )

    def _raise_if_ssh_denied(self, text: str) -> None:
        if any(m in text for m in _SSH_DENIED_MARKERS):
            raise PDU41002AuthError(
                f"ssh refused the {self._username!r} login to "
                f"{getattr(self._link, 'host', '?')}; check {PASSWORD_ENV}. "
                f"Note the device offers only keyboard-interactive auth, so a "
                f"key will never work here."
            )

    def _login_serial(self, budget: float) -> None:
        """Drive the device's own two-step login from an unknown starting state.

        The CLI keeps session state across port opens, so this cannot assume it
        is greeted by a login prompt: it may already be authenticated, or
        sitting mid-prompt from a previous half-finished attempt. A bare CR
        either re-prompts or answers with the ready prompt, which is enough to
        tell those apart.
        """
        deadline = time.monotonic() + budget
        password = self.__password
        #: Set when the device answered "Login Failed" at least once, so the
        #: error raised on exhaustion can say which of the two things happened.
        refusals = 0

        while time.monotonic() < deadline:
            self._link.write(b"\r")
            text = self._read_until(
                [PROMPT, _LOGIN_NAME, _LOGIN_PASSWORD], timeout=6.0
            )
            self._raise_if_hungup(text)

            if PROMPT in text:
                self._authed = True
                return

            if _LOGIN_PASSWORD in text:
                # Mid-login from a previous attempt: an empty password gets us
                # back to the name prompt rather than guessing.
                self._link.write(b"\r")
                self._read_until([_LOGIN_NAME], timeout=6.0)
                continue

            if _LOGIN_NAME in text:
                self._link.write(self._username.encode("ascii") + b"\r")
                got = self._read_until([_LOGIN_PASSWORD, _LOGIN_NAME], timeout=6.0)
                if _LOGIN_PASSWORD not in got:
                    continue
                self._link.write(password.encode("ascii") + b"\r")
                # Floored at _AUTH_WAIT_S, not at a token 2s: the device prints
                # "Please wait for authentication...." and takes seconds over it.
                # Deriving this window purely from the remaining budget means a
                # login that started late gets a read too short to ever see the
                # prompt — and the only two outcomes below are "authenticated"
                # and "refused", so a truncated read is indistinguishable from a
                # wrong password.
                after = self._read_until(
                    [PROMPT, _LOGIN_NAME, _LOGIN_FAILED],
                    timeout=max(self._AUTH_WAIT_S, deadline - time.monotonic()),
                )
                self._raise_if_hungup(after)
                if PROMPT in after:
                    self._authed = True
                    return
                if _LOGIN_FAILED in after:
                    # Ambiguous by construction — see _LOGIN_FAILED. Retry
                    # rather than classify: a busy device succeeds on a later
                    # attempt, a wrong password does this every time and falls
                    # out of the loop below. No re-prompt follows, so start the
                    # next attempt from scratch rather than waiting for one.
                    refusals += 1
                    log.debug(
                        "serial login refused (attempt %d); retrying within "
                        "the remaining budget",
                        refusals,
                    )
                    time.sleep(self._LOGIN_RETRY_S)
                    continue
                if _LOGIN_NAME in after:
                    # Back at the name prompt is the device's way of saying no.
                    raise PDU41002AuthError(
                        f"device refused the {self._username!r} login over "
                        f"{self._transport}; check {PASSWORD_ENV}"
                    )
                # Neither marker: the device said something, or nothing, that is
                # not a verdict. Reporting that as an auth failure sends the
                # operator hunting for a password that was in fact correct —
                # which is the same misdiagnosis the single-session hangup
                # causes, and it has to be kept distinct for the same reason.
                raise PDU41002TimeoutError(
                    f"submitted the {self._username!r} password over "
                    f"{self._transport} and got neither {PROMPT!r} nor a "
                    f"re-prompt within {self._AUTH_WAIT_S:.0f}s. The password "
                    f"was not rejected — the device did not answer. Got "
                    f"{after[-200:]!r}"
                )

        if refusals:
            # Every attempt got a definite refusal, so this is a credential
            # problem far more likely than a slow device — but say both, because
            # the device emits the same bytes for each and a bench that just
            # closed a session on the other transport hits this legitimately.
            raise PDU41002AuthError(
                f"the device answered {_LOGIN_FAILED!r} to all {refusals} "
                f"{self._username!r} login attempts over {self._transport} in "
                f"{budget:.0f}s. Most likely {PASSWORD_ENV} is wrong; the same "
                f"response also means 'another CLI session is still closing', "
                f"which clears on its own within ~15s."
            )
        raise PDU41002TimeoutError(
            f"could not reach a {PROMPT!r} prompt over {self._transport} "
            f"within {budget:.0f}s"
        )

    def _raise_if_hungup(self, text: str) -> None:
        """Distinguish the single-session hangup from an auth failure.

        The device prints its full banner and *then* disconnects, so without
        this the symptom is indistinguishable from a wrong password.
        """
        if any(m in text for m in _HANGUP_MARKERS):
            raise PDU41002SessionError(
                "the PDU accepted the login and then closed the session: its "
                "CLI allows only one session at a time, across serial and SSH "
                "together. Another session is logged in — send 'exit' on it "
                "(closing its port alone does NOT release the session)."
            )

    def _raise_if_disconnected(self, text: str) -> None:
        """Report a vanished ssh session as a connection error, not a timeout.

        This is what an **idle logout over the network** looks like: the device
        drops the connection after ~180 s and the ssh client says so. The
        asymmetry with serial is the point and it is not something the driver can
        paper over — serial keeps the port and lands on ``Login Name :``, so
        ``_cmd`` re-authenticates in place, whereas here the ssh process is gone
        and there is nothing to re-authenticate on. Raising a timeout invited a
        retry that could never succeed; ``PDU41002ConnectionError`` tells the
        caller the only thing that works, which is to open again.

        Matches ``_LINK_GONE_MARKERS`` rather than only ssh's disconnect notice,
        because ssh has more than one wording for the same event and the other
        one collided with the single-session check: an idled-out session was
        reported as "another session is logged in — send 'exit' on it", advice
        that cannot work on a link that no longer exists. Called only from
        :py:meth:`_round_trip`, i.e. only *after* a successful login, which is
        what makes the broader match safe — see ``_LINK_GONE_MARKERS``.
        """
        if any(m in text for m in _LINK_GONE_MARKERS):
            # Mark the session dead so a later call fails fast rather than
            # writing into a pty nobody is reading.
            self._authed = False
            first = next(iter(text.strip().splitlines()), "")
            raise PDU41002ConnectionError(
                f"the PDU closed the {self._transport} session, reporting "
                f"{first!r}. Most likely the device's idle timeout, which is "
                f"5 minutes as shipped and *not* recoverable in place over "
                f"ssh: reopen the connection. (Over serial the same timeout "
                f"is recovered automatically, because the session drops to a "
                f"login prompt rather than dropping the link.)"
            )

    # -- CLI engine ---------------------------------------------------------

    def _read_until(
        self, needles: Iterable[str], *, timeout: float
    ) -> str:
        """Accumulate bytes until one of ``needles`` appears, or time out.

        Reads to the **prompt**, never to a blank line: the unknown-verb error
        carries a ~30 line verb dump, and the number of blank lines before the
        prompt differs between the three error shapes (2, 3 and 0). A
        blank-line-terminated read would truncate mid-error and desync the
        session for every later command.
        """
        wanted = list(needles)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                chunk = self._link.read(4096, timeout=0.2)
            except LinkError as e:
                raise PDU41002ConnectionError(str(e)) from e
            if chunk:
                self._buf += chunk.decode("ascii", errors="replace")
                if any(n in self._buf for n in wanted):
                    break
            elif any(n in self._buf for n in wanted):
                break
        out, self._buf = self._buf, ""
        return out

    def _cmd(self, command: str, *, timeout: Optional[float] = None) -> str:
        """Send one command and return its response, minus echo and prompt.

        Re-authenticates if the device has idled out. That is a safety
        requirement rather than a convenience: a logged-out session consumes
        ``oltctrl index 1 act off`` as a *username*, so the switch is silently
        swallowed while the caller believes it happened.
        """
        if self._link is None or not self._link.is_open:
            raise PDU41002ConnectionError("not open")

        budget = self._CMD_TIMEOUT_S if timeout is None else timeout
        text = self._round_trip(command, budget)

        if _LOGIN_NAME in text or _LOGIN_PASSWORD in text:
            log.info("session had timed out; re-authenticating before %r", command)
            self._authed = False
            self._buf = ""
            self._login()
            text = self._round_trip(command, budget)
            if _LOGIN_NAME in text or _LOGIN_PASSWORD in text:
                raise PDU41002AuthError(
                    f"session kept dropping to the login prompt around {command!r}"
                )

        # Deliberately not _raise_if_hungup: post-login, the hangup markers mean
        # "the link died", not "someone else holds the CLI", and _round_trip has
        # already raised PDU41002ConnectionError for them. Calling it here is
        # what reported an idle logout as a session conflict.
        body = _strip_echo(text, command, echoes=self._echoes)
        _raise_for_error(command, body, self._outlet_count)
        return body

    def _round_trip(self, command: str, timeout: float) -> str:
        """Write one command and read to a prompt, without pre-clearing.

        Deliberately sends *nothing* but the command. An earlier version
        prefixed ``\\x03`` to clear a possibly-dirty input line; measurement on
        firmware 1.3.4 showed that to be wrong twice over:

        - The device does not treat ``\\x03`` as an interrupt. It echoes the
          byte literally and takes it as part of the command, so every command
          became ``\\x03sys show`` and drew ``Command not found`` with the caret
          at a constant column 13.
        - It does not clear a dirty line either: a partial line followed by
          ``\\x03`` then a command still failed. A **bare CR** is what resyncs
          this CLI, which is what :py:meth:`_resync` sends.

        Back-to-back commands need no clearing at all — six consecutive reads
        were verified clean, with the echo exactly equal to the command.
        """
        try:
            self._buf = ""
            self._link.write(command.encode("ascii") + b"\r")
        except LinkError as e:
            raise PDU41002ConnectionError(str(e)) from e
        text = self._read_until(
            [PROMPT, _LOGIN_NAME, _LOGIN_PASSWORD, *_LINK_GONE_MARKERS],
            timeout=timeout,
        )
        # Before the missing-prompt check below, so a dead link is reported as a
        # dead link rather than as a slow device.
        self._raise_if_disconnected(text)
        # No hangup markers here: _raise_if_disconnected covers all of them and
        # has already raised. Only the login prompts remain as legitimate
        # non-prompt endings, and _cmd re-authenticates on those.
        if PROMPT not in text and not any(
            m in text for m in (_LOGIN_NAME, _LOGIN_PASSWORD)
        ):
            # Leave the session usable: a half-consumed line would otherwise
            # prepend itself to the *next* command, turning one timeout into a
            # run of bogus "Command not found" errors that look unrelated.
            self._resync()
            raise PDU41002TimeoutError(
                f"no prompt after {command!r} within {timeout:.1f}s "
                f"(got {len(text)} bytes)"
            )
        return text

    def _resync(self) -> None:
        """Recover the input line with a bare CR.

        Measured on firmware 1.3.4: a partial line followed by CR then a command
        succeeds, whereas the same sequence using ``\\x03`` does not — this CLI
        has no interrupt character. Best-effort; a failure here must not mask
        whatever error prompted the resync.
        """
        try:
            reset = getattr(self._link, "reset_input", None)
            if callable(reset):
                reset()
            self._link.write(b"\r")
            self._buf = ""
            self._read_until([PROMPT, _LOGIN_NAME], timeout=3.0)
            self._buf = ""
        except (LinkError, OSError, PDU41002Error):
            log.debug("resync failed; session may be desynced", exc_info=True)

    # -- reads --------------------------------------------------------------

    def read_identity(self) -> PDU41002Info:
        """Identity and static details, from ``sys show``."""
        fields = _parse_colon_fields(self._cmd("sys show"))
        try:
            return PDU41002Info(
                name=fields["Name"],
                location=fields.get("Location", ""),
                contact=fields.get("Contact", ""),
                model=fields["Model"],
                hardware_version=fields.get("Hardware Version", ""),
                firmware_version=fields.get("Firmware Version", ""),
                mac_address=fields.get("MAC Address", ""),
            )
        except KeyError as e:
            raise PDU41002ProtocolError(
                f"sys show did not report {e.args[0]!r}; got {sorted(fields)}"
            ) from e

    def read_device_status(self) -> PDU41002Status:
        """Metering, from ``devsta show``."""
        body = self._cmd("devsta show")
        fields = _parse_colon_fields(body)

        load = fields.get("Device Load", "")
        amps, watts, va = _parse_load_triplet(load)

        pf_raw = fields.get("Power Factor", "").strip()
        # The device prints "----" at zero load. That is not an error.
        pf = None if not pf_raw or set(pf_raw) <= {"-"} else _to_float(pf_raw, "power factor")

        return PDU41002Status(
            load_A=amps,
            load_W=watts,
            load_VA=va,
            power_factor=pf,
            voltage_V=_parse_suffixed(fields, "Voltage", "V"),
            frequency_Hz=_parse_suffixed(fields, "Frequency", "Hz"),
            peak_load_A=_parse_optional_suffixed(fields, "Peak Load", "A"),
            energy_kWh=_parse_optional_suffixed(fields, "Energy", "kWh"),
        )

    def measure_load_A(self) -> float:
        """Total device load in amps."""
        return self.read_device_status().load_A

    def measure_voltage_V(self) -> float:
        """Input mains voltage."""
        return self.read_device_status().voltage_V

    def measure_frequency_Hz(self) -> float:
        """Input mains frequency."""
        return self.read_device_status().frequency_Hz

    def outlet_state(self, index: OutletIndex) -> bool:
        """Whether one outlet is energised. ``True`` == On."""
        idx = self._coerce_outlet(index)
        body = self._cmd(f"oltsta index {idx} show")
        fields = _parse_colon_fields(body)
        raw = fields.get("Status")
        if raw is None:
            raise PDU41002ProtocolError(
                f"oltsta index {idx} show reported no Status; got {sorted(fields)}"
            )
        return _parse_on_off(raw, f"outlet {idx} status")

    def outlet_states(self) -> dict:
        """Every outlet's state in one round trip, from ``oltsta show``."""
        body = self._cmd("oltsta show")
        states: dict[int, bool] = {}
        for idx, _name, status in _parse_outlet_table(body):
            states[idx] = _parse_on_off(status, f"outlet {idx} status")
        if not states:
            raise PDU41002ProtocolError(
                f"could not parse any outlet rows from oltsta show: {body!r}"
            )
        return states

    def outlet_name(self, index: OutletIndex) -> str:
        """The operator-assigned label for one outlet."""
        idx = self._coerce_outlet(index)
        fields = _parse_colon_fields(self._cmd(f"oltsta index {idx} show"))
        name = fields.get("Outlet Name")
        if name is None:
            raise PDU41002ProtocolError(
                f"oltsta index {idx} show reported no Outlet Name"
            )
        return name

    def read_outlet_config(self, *, refresh: bool = False) -> dict:
        """Per-outlet delays, from ``oltcfg index all show``.

        Cached, because :py:meth:`set_outlet_state`'s read-back budget is
        derived from it and re-reading on every switch would double the traffic
        for a value that rarely changes. ``refresh=True`` re-reads.

        This is an aggregate *read*. Reads are safe; only aggregate **writes**
        are dangerous, and none are ever emitted — see ``_assert_safe_command``.
        """
        if self._config_cache and not refresh:
            return dict(self._config_cache)
        body = self._cmd("oltcfg index all show")
        out: dict[int, OutletConfig] = {}
        for idx, name, on_s, off_s, reboot_s in _parse_config_table(body):
            out[idx] = OutletConfig(
                index=idx,
                name=name,
                on_delay_s=on_s,
                off_delay_s=off_s,
                reboot_duration_s=reboot_s,
            )
        if not out:
            raise PDU41002ProtocolError(
                f"could not parse any rows from oltcfg index all show: {body!r}"
            )
        self._config_cache = out
        return dict(out)

    # -- writes -------------------------------------------------------------
    #
    # Every method here is prefixed `set_`, `reset` or `clear_` — not for
    # style, but because `agent/dispatch.py` derives which calls require a
    # writer claim *purely* from the method name, with no driver-declared
    # override. A switching method named `outlet_on()` would be remotely
    # callable by any observer: mains switching that bypasses the claim gate.
    # Renaming anything below silently removes that protection.

    def set_outlet_state(
        self,
        index: OutletIndex,
        on: bool,
        *,
        delayed: bool = False,
        verify: bool = True,
    ) -> bool:
        """Switch one outlet and return its **verified** state.

        Args:
            index: outlet number. Must be a plain int in ``allowed_outlets``.
            on: ``True`` energises, ``False`` cuts.
            delayed: use the device's ``delayon``/``delayoff`` actions, which
                honour the per-outlet delay as a *scheduled* switch.
            verify: re-read the outlet until it agrees, and raise if it never
                does. Defaults on, and turning it off means accepting that the
                return value is a guess — see below.

        Returns:
            The state read back from the device, not the state requested. With
            ``verify=False`` the device is not asked, and the requested state is
            returned unconfirmed.

        Raises:
            PDU41002PolicyError: ``index`` is not in ``allowed_outlets``.
            PDU41002ValueError: ``index`` is not a plain in-range int, or ``on``
                is not a bool.
            PDU41002ProtocolError: the outlet never reached the requested state
                within the budget derived from its configured delay.

        This returns the read-back state rather than ``None`` because
        ``oltctrl`` reports **nothing**: its response is a blank line and a
        re-prompt, byte-identical whether or not the contactor moved (captured
        in ``tests/fixtures/pdu41002/outlet_switch.txt``). There is no other way
        to learn what happened, so mains switching here is never
        fire-and-forget.
        """
        idx = self._coerce_outlet(index)
        self._require_allowed(idx)
        if not isinstance(on, bool):
            # Not pedantry: `set_outlet_state(3, "off")` would otherwise be
            # truthy and energise an outlet the caller was trying to cut.
            raise PDU41002ValueError(
                f"`on` must be a bool, got {on!r}. A truthy string would "
                f"energise an outlet a caller meant to cut."
            )

        action = _SWITCH_ACTIONS[(on, bool(delayed))]
        self._switch(idx, action)

        if not verify:
            log.warning(
                "outlet %d switched to %s without read-back; the device does "
                "not acknowledge oltctrl, so this result is unconfirmed",
                idx,
                "on" if on else "off",
            )
            return on
        return self._verify_outlet(idx, on)

    def reset_outlet(self, index: OutletIndex, *, delayed: bool = False) -> None:
        """Power-cycle one outlet via the device's own ``reboot`` action.

        Returns ``None``, unlike :py:meth:`set_outlet_state`, because the
        end state is the *same* as the start state — a read-back cannot
        distinguish "cycled" from "never moved". The transient off is what the
        caller wants and it is not observable after the fact, so this method
        makes no claim about it. Callers needing proof of the cut should drive
        ``set_outlet_state(index, False)`` then ``True`` and verify each.

        The device holds the outlet off for its configured reboot duration
        (5 s as shipped) and restores it without further instruction.
        """
        idx = self._coerce_outlet(index)
        self._require_allowed(idx)
        self._switch(idx, "delayreboot" if delayed else "reboot")

    def clear_outlet_command(self, index: OutletIndex) -> None:
        """Cancel a pending delayed switch on one outlet (``act cancel``).

        Only affects a *scheduled* action; an outlet that has already moved
        stays where it is. Requires the outlet to be in ``allowed_outlets``:
        cancelling a scheduled cut leaves mains on when an operator expected it
        off, which is a state change in every sense that matters.
        """
        idx = self._coerce_outlet(index)
        self._require_allowed(idx)
        self._switch(idx, "cancel")

    # -- switching internals ------------------------------------------------

    def _switch(self, idx: int, action: str) -> None:
        """Render, guard and emit one ``oltctrl`` command."""
        command = f"oltctrl index {idx} act {action}"
        _assert_safe_command(command)
        self._cmd(command)

    def _verify_budget_s(self, idx: int, on: bool) -> float:
        """How long to allow for an outlet to reach ``on``.

        Derived from the device's own ``oltcfg`` delay rather than hardcoded,
        because that delay is **operator-configurable per outlet**. The capture
        notes make the failure concrete: the ``oltctrl`` round trip is ~0.62 s
        and the contactor itself settles in ~1.5 s, so a budget sized from
        either of those measurements looks generous and then flakes on a unit
        whose ``td_on`` an operator raised. Falls back to the margin alone if
        the config cannot be read — a failed read of an advisory value must not
        block a switch that has already been emitted.
        """
        try:
            config = self.read_outlet_config()
        except PDU41002Error:
            log.debug("could not read oltcfg for the verify budget", exc_info=True)
            return self._VERIFY_MARGIN_S
        cfg = config.get(idx)
        if cfg is None:
            return self._VERIFY_MARGIN_S
        return float(cfg.on_delay_s if on else cfg.off_delay_s) + self._VERIFY_MARGIN_S

    def _verify_outlet(self, idx: int, want: bool) -> bool:
        """Poll one outlet until it reads ``want``, or raise.

        Polls rather than sleeping the full budget so the common case (already
        settled) returns immediately.
        """
        budget = self._verify_budget_s(idx, want)
        deadline = time.monotonic() + budget
        last: Optional[bool] = None
        while True:
            last = self.outlet_state(idx)
            if last == want:
                return last
            if time.monotonic() >= deadline:
                break
            time.sleep(self._VERIFY_POLL_S)
        raise PDU41002ProtocolError(
            f"outlet {idx} still reads "
            f"{'on' if last else 'off'} {budget:.1f}s after being switched "
            f"{'on' if want else 'off'}. The command was accepted — oltctrl "
            f"never reports failure — so either the outlet did not move or the "
            f"delay configured for it exceeds this budget."
        )

    # -- validation ---------------------------------------------------------

    def _coerce_outlet(self, index: OutletIndex) -> int:
        """Validate an outlet index, rejecting aggregates structurally.

        Only a plain ``int`` in range is accepted. No signature in this driver
        takes ``"all"``, ``"b1"``, ``"b2"`` or a collection, because
        ``oltctrl index all act off`` is a single line that de-powers
        everything. ``bool`` is rejected before ``int`` so
        ``outlet_state(True)`` cannot silently mean outlet 1.
        """
        if isinstance(index, bool):
            raise PDU41002ValueError(
                f"outlet index must be an int 1-{self._outlet_count}; "
                f"got bool {index!r}"
            )
        if not isinstance(index, int):
            raise PDU41002ValueError(
                f"outlet index must be an int 1-{self._outlet_count}; got "
                f"{index!r}. Aggregate targets ('all', 'b1', 'b2') are "
                f"deliberately unsupported: one such command cuts every outlet."
            )
        if not 1 <= index <= self._outlet_count:
            raise PDU41002ValueError(
                f"outlet index must be 1-{self._outlet_count}; got {index}"
            )
        return index

    def _require_allowed(self, index: int) -> None:
        if index not in self._allowed:
            raise PDU41002PolicyError(
                f"outlet {index} is not in allowed_outlets "
                f"{sorted(self._allowed)}; refusing to switch it"
            )


# ---------------------------------------------------------------------------
# Parsing helpers — every format here is pinned by a hardware capture in
# tests/fixtures/pdu41002/, not by the manual (which omits most of them).
# ---------------------------------------------------------------------------

#: A ``^`` caret line, as used by two of the three error shapes.
_CARET_RE = re.compile(r"^\s*(\^)\s*$")
#: The out-of-range shape, which has neither a caret nor an "Error :" prefix.
_INDEX_ERR_RE = re.compile(r"Index number must be (\d+) to (\d+)")
#: "Device Load : 0.00 A/ 0 W/ 0 VA"
_LOAD_RE = re.compile(
    r"([-+]?\d+(?:\.\d+)?)\s*A\s*/\s*([-+]?\d+(?:\.\d+)?)\s*W\s*/\s*"
    r"([-+]?\d+(?:\.\d+)?)\s*VA"
)
#: "    1  Outlet1                              On"
_OUTLET_ROW_RE = re.compile(r"^\s*(\d+)\s+(.*?)\s{2,}(On|Off)\s*$", re.IGNORECASE)
#: "    1  Outlet1     3 s        3 s       5 s"
_CONFIG_ROW_RE = re.compile(
    r"^\s*(\d+)\s+(.*?)\s{2,}(\d+)\s*s\s+(\d+)\s*s\s+(\d+)\s*s\s*$",
    re.IGNORECASE,
)


def _lines(text: str) -> list[str]:
    """Split a device response into lines.

    The firmware mixes terminators: ``\\r\\n`` mostly, a bare ``\\n\\r`` before
    caret lines and between some table rows. Normalising both orderings before
    splitting is what stops ``snmpv3 show``-style output collapsing into one
    giant line.
    """
    return text.replace("\r\n", "\n").replace("\n\r", "\n").replace("\r", "\n").split("\n")


def _strip_echo(text: str, command: str, *, echoes: bool) -> str:
    """Remove the command echo and the trailing prompt.

    The serial console echoes the submitted command as the first line of the
    response; SSH does not. Stripping is therefore *conditional* — a driver
    that always strips eats a real line of SSH output, and one that never
    strips leaves the command text in every serial parse.

    ``echoes`` selects the expected behaviour, but the echo is only removed if
    it is actually present, so a transport that surprises us degrades to
    "leave it alone" rather than corrupting the body.
    """
    body = text
    idx = body.find(PROMPT)
    if idx >= 0:
        body = body[:idx]
    stripped = body.lstrip("\r\n")
    if echoes and stripped.startswith(command):
        stripped = stripped[len(command):]
    elif not echoes and stripped.startswith(command):
        # Harmless, but worth knowing about: it means the echo assumption for
        # this transport is wrong.
        log.debug("unexpected echo of %r on a non-echoing transport", command)
        stripped = stripped[len(command):]
    return stripped.lstrip("\r\n")


def _raise_for_error(command: str, body: str, outlet_count: int) -> None:
    """Raise :py:class:`PDU41002CommandError` if the body is an error.

    Detection keys on ``"Error :"`` **or** the index-range sentence. Either
    alone is insufficient: the out-of-range shape carries no ``Error :`` prefix
    and no caret, so a caret- or prefix-only check silently treats it as a
    successful response.

    ``outlet_count`` is used only to notice a disagreement: the range sentence
    is the *device's own* statement of how many outlets it has, which beats our
    configured value. Reaching this path at all means the client-side range
    check was bypassed, so warn rather than silently accept a stale count.
    """
    m = _INDEX_ERR_RE.search(body)
    if m:
        device_max = int(m.group(2))
        if device_max != outlet_count:
            log.warning(
                "device reports %d outlets but this driver is configured for "
                "%d; pass outlet_count=%d to open()",
                device_max,
                outlet_count,
                device_max,
            )
        raise PDU41002CommandError(
            command,
            f"index out of range: device accepts {m.group(1)} to {device_max}",
        )

    if "Error :" not in body:
        return

    message = ""
    marker: Optional[int] = None
    for line in _lines(body):
        if _CARET_RE.match(line):
            marker = line.index("^")
        elif line.strip().startswith("Error :"):
            message = line.split("Error :", 1)[1].strip()
    raise PDU41002CommandError(command, message or "unspecified error", marker)


def _parse_colon_fields(body: str) -> dict:
    """Collect ``Key : Value`` lines into a dict.

    Stops at the first ``:`` so values containing colons (times, MACs written
    with them) survive. Table rows without a colon are ignored.
    """
    out: dict[str, str] = {}
    for line in _lines(body):
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        key = key.strip()
        if key and not key.startswith("-"):
            out[key] = value.strip()
    return out


def _parse_outlet_table(body: str) -> list:
    """Rows of ``oltsta show`` as ``(index, name, status)``."""
    rows = []
    for line in _lines(body):
        m = _OUTLET_ROW_RE.match(line)
        if m:
            rows.append((int(m.group(1)), m.group(2).strip(), m.group(3)))
    return rows


def _parse_config_table(body: str) -> list:
    """Rows of ``oltcfg index all show``.

    The header wraps onto two physical lines ("On/Off/Reboot" then
    "Delay/Delay/Duration"), so matching data rows by shape rather than by
    position under a header is what keeps this robust.
    """
    rows = []
    for line in _lines(body):
        m = _CONFIG_ROW_RE.match(line)
        if m:
            rows.append(
                (
                    int(m.group(1)),
                    m.group(2).strip(),
                    int(m.group(3)),
                    int(m.group(4)),
                    int(m.group(5)),
                )
            )
    return rows


def _parse_on_off(raw: str, label: str) -> bool:
    value = raw.strip().lower()
    if value.startswith("on"):
        return True
    if value.startswith("off"):
        return False
    raise PDU41002ProtocolError(f"{label}: expected On/Off, got {raw!r}")


def _parse_load_triplet(raw: str) -> tuple:
    m = _LOAD_RE.search(raw or "")
    if not m:
        raise PDU41002ProtocolError(
            f"could not parse 'Device Load' from {raw!r}"
        )
    return float(m.group(1)), float(m.group(2)), float(m.group(3))


def _to_float(raw: str, label: str) -> float:
    try:
        return float(raw)
    except (TypeError, ValueError) as e:
        raise PDU41002ProtocolError(f"{label}: expected a number, got {raw!r}") from e


def _parse_suffixed(fields: dict, key: str, suffix: str) -> float:
    raw = fields.get(key)
    if raw is None:
        raise PDU41002ProtocolError(f"devsta show reported no {key!r}")
    return _to_float(raw.strip().rstrip(suffix).strip(), key)


def _parse_optional_suffixed(fields: dict, key: str, suffix: str):
    """Like :py:func:`_parse_suffixed` but tolerant.

    ``Peak Load`` and ``Energy`` carry a parenthesised timestamp and are
    informational, so a device that formats them unexpectedly should not fail
    a status read.
    """
    raw = fields.get(key)
    if not raw:
        return None
    head = raw.split("(")[0].strip()
    head = head.rstrip(suffix).strip() if head.endswith(suffix) else head
    try:
        return float(head)
    except ValueError:
        return None


def _assert_safe_command(command: str) -> None:
    """Refuse to emit any switching command outside :py:data:`_SAFE_OLTCTRL_RE`.

    A module-level function, not a method, so a test can exercise it against
    hand-written strings the driver's own methods cannot construct — which is
    the only way to prove the guard catches a *rendering* bug rather than
    merely restating the argument validation that already happened.

    Deliberately raises :py:class:`PDU41002ValueError` before any byte reaches
    the wire. The failure mode this prevents is not theoretical: ``oltctrl
    index all act off`` is a single well-formed line that de-powers every
    outlet on the unit, and the device answers it with the same blank
    re-prompt it gives a legitimate switch.
    """
    if not _SAFE_OLTCTRL_RE.match(command):
        raise PDU41002ValueError(
            f"refusing to emit {command!r}: not a single-outlet oltctrl "
            f"command. Aggregate targets (all, b1, b2), other units (guest) "
            f"and menumode are structurally unreachable by design."
        )


def _coerce_outlet_set(values: Iterable[int], label: str) -> set:
    """Validate an outlet collection, rejecting bool and non-ints."""
    out = set()
    for v in values:
        if isinstance(v, bool) or not isinstance(v, int):
            raise PDU41002ValueError(
                f"{label} must contain plain ints; got {v!r}"
            )
        if v < 1:
            raise PDU41002ValueError(f"{label} entries must be >= 1; got {v}")
        out.add(v)
    return out


def _resolve_password(password: Optional[str], env: Optional[dict]) -> str:
    """Resolve the CLI password, preferring the environment over config.

    Fails at ``open()`` naming the variable, rather than letting a missing
    password surface later as a login timeout — which is a far harder symptom
    to diagnose.
    """
    if password:
        return password
    source = os.environ if env is None else env
    from_env = source.get(PASSWORD_ENV)
    if from_env:
        return from_env
    raise PDU41002AuthError(
        f"no PDU password available: set {PASSWORD_ENV} in the environment of "
        f"the host running this driver. Do not put it in a benchctrl config "
        f"file — DeviceConfig.open is round-tripped verbatim by to_dict() and "
        f"is forwarded over the agent's unencrypted RPC wire."
    )

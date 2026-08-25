"""A simulated CyberPower PDU41002 switched PDU.

Line-oriented menu-free CLI over the same pty loopback the other simulators
use, so the real driver drives it unmodified — including the login handshake
and the ``CyberPower > `` prompt sentinel.

**Every response format here is copied from a capture of the real device**
(firmware 1.3.4, 2026-08-24), checked in under
``tests/fixtures/pdu41002/``. This matters more than usual: ``AGENTS.md`` warns
that a simulator written from the same reading of the manual as the driver
agrees with the driver and is still wrong, and ``sim/qr10x.py``'s docstring
records what that cost last time. Several formats below are things the manual
does not show at all — the ``oltcfg`` two-line wrapped header, the bare
``\\n\\r`` row separators, the caret-free out-of-range error, and the fact that
``oltctrl`` answers with nothing whatsoever.

Modelled deliberately, because each one is a driver bug if unhandled:

- **Command echo.** The real serial console echoes the submitted command back
  as the first line of the response; SSH does not. ``echo=True`` (the default)
  reproduces serial. A driver that unconditionally strips or keeps the echo
  passes against one transport and fails against the other.
- **Single session.** The device permits one CLI session across all
  transports, and hangs up on a *successfully authenticated* newcomer while an
  incumbent holds the session. See :py:meth:`hold_session`.
- **Session persistence.** Closing the port does not log the session out; only
  ``exit`` (or an idle timeout) does. This is why the driver must send ``exit``
  on close.
- **Three error shapes**, not the two the manual documents.
- **Configurable per-outlet on/off delay**, so a read-back that assumes an
  instant switch flakes exactly as it does on hardware.
"""

from __future__ import annotations

import logging
import threading
from typing import Optional

from benchctrl.sim.base import SimDevice
from benchctrl.sim.loopback import SerialLoopback

log = logging.getLogger("benchctrl.sim.pdu41002")

#: The prompt the device emits when ready. Trailing space is significant — it
#: is the driver's read-until sentinel.
PROMPT = "CyberPower > "

#: Placeholder for the bare ``\n\r`` the real firmware emits between some table
#: rows. Response bodies are written with ``\n`` and converted to ``\r\n``, so a
#: literal ``\n\r`` cannot survive that pass; this token does. Chosen to be a
#: byte sequence no real response contains.
RAW_NL_CR = "\x00NLCR\x00"

#: Shaped after the real unit but with obviously synthetic identity: a sim that
#: claims a real device's MAC makes captured logs impossible to attribute.
#: Model/HW/FW are kept real so format parsing is exercised faithfully.
DEFAULT_IDENTITY = {
    "Name": "SIM-PDU41002",
    "Location": "Simulator",
    "Contact": "benchctrl",
    "Model": "PDU41002",
    "Hardware Version": "1.2",
    "Firmware Version": "1.3.4",
    "MAC Address": "00-0C-15-00-00-01",
}

#: Verb list dumped by the real firmware on an unrecognised command, in order.
#: Part of the error response, so the driver's read-until must survive it.
VERBS = (
    "devsta", "devcfg", "oltsta", "oltcfg", "oltctrl", "schedule", "date",
    "ntp", "sys", "dst", "login", "admin", "device", "oltuser", "radius",
    "ldap", "tcpip", "tcpip6", "snmpv1", "snmpv3", "trap", "web", "console",
    "ftp", "eventlog", "syslog", "menumode", "clear", "exit",
)

_USAGE_OLTCTRL = (
    "    oltctrl index <outlet index> act <on | off| reboot| delayon| "
    "delayoff| delayreboot| cancel>"
)


class SimulatedPDU41002(SimDevice):
    """A PDU41002 that answers CLI commands from a pty.

    Args:
        outlets: number of outlets. The real unit has 8 and says so in its
            out-of-range error, which is how the driver learns the count.
        username / password: credentials for the login handshake. The password
            is accepted as given; tests never need the real device's.
        echo: reproduce the serial console's command echo. ``False`` models
            the SSH transport, which does not echo.
        on_delay_s / off_delay_s: per-outlet switching delay, as reported by
            ``oltcfg``. Non-zero makes read-back verification actually wait,
            which is the behaviour that caught a too-short retry budget.
        require_login: when ``False`` the session starts authenticated, which
            keeps tests that are not about auth shorter.
    """

    def __init__(
        self,
        *,
        loopback: Optional[SerialLoopback] = None,
        outlets: int = 8,
        username: str = "admin",
        password: str = "simpass",
        echo: bool = True,
        on_delay_s: int = 3,
        off_delay_s: int = 3,
        reboot_duration_s: int = 5,
        voltage_v: float = 121.7,
        frequency_hz: float = 60.0,
        identity: Optional[dict[str, str]] = None,
        require_login: bool = True,
        free_run: bool = True,
    ) -> None:
        super().__init__(loopback=loopback, tick_hz=100.0, free_run=free_run)
        self._lock = threading.RLock()
        self._rx = bytearray()

        self.outlets = outlets
        self.username = username
        self.password = password
        self.echo = echo
        self.on_delay_s = on_delay_s
        self.off_delay_s = off_delay_s
        self.reboot_duration_s = reboot_duration_s
        self.voltage_v = voltage_v
        self.frequency_hz = frequency_hz
        self.identity = dict(DEFAULT_IDENTITY)
        if identity:
            self.identity.update(identity)

        #: True == energised. The real unit ships with everything on.
        self.outlet_state: dict[int, bool] = {
            i: True for i in range(1, outlets + 1)
        }
        self.outlet_name: dict[int, str] = {
            i: f"Outlet{i}" for i in range(1, outlets + 1)
        }
        #: Pending switch actions: index -> [(apply_at_elapsed_s, target), ...].
        #:
        #: A **list** per outlet, not a single slot. ``act reboot`` schedules two
        #: transitions on one outlet — off now, on after the reboot duration —
        #: and a one-slot-per-outlet model silently drops the first, so the
        #: transient cut would be unobservable and a reboot would look
        #: indistinguishable from doing nothing. A test asserting "the outlet
        #: went off then came back" would then pass against a simulator that
        #: never cut it.
        self._pending: dict[int, list[tuple[float, bool]]] = {}

        #: Every command line the client sent, post-login. Tests assert against
        #: this to prove the driver never emits `all`, `b1`, `b2` or `guest`.
        self.command_log: list[str] = []
        #: Login attempts as (username, accepted). Passwords are NOT recorded —
        #: a simulator that logs credentials would leak them into test output.
        self.login_log: list[tuple[str, bool]] = []

        self._authed = not require_login
        self._await_password_for: Optional[str] = None
        #: Set while another session "holds" the device; a newcomer that
        #: authenticates successfully is then hung up on.
        self._held = False
        self.hangup_count = 0
        #: Set by :py:meth:`drop_ssh_session`: the link is gone and nothing this
        #: device would have said ever arrives again. Distinct from
        #: ``_authed = False``, which is the *serial* logout — there the port is
        #: still live and a login prompt is waiting.
        self._gone = False
        #: When set, the next command answers with a forced error shape.
        self.force_next_error: Optional[str] = None
        #: How many more logins answer ``Login Failed`` before one succeeds.
        #:
        #: Models the device's fourth login outcome, measured on firmware 1.3.4:
        #: a *correct* password submitted within ~15 s of a previous session
        #: closing is refused with output byte-identical to a wrong one — dots
        #: for ~15 s, then "Login Failed", then **silence**, with no re-prompt.
        #: It clears on its own.
        #:
        #: Non-zero here is the busy case: the credential is *right* and the
        #: refusal still happens. A wrong password takes the same path
        #: unconditionally, and emits the same bytes on purpose — a simulator
        #: that made the two distinguishable would let the driver "classify"
        #: something the hardware does not let it classify.
        self.refuse_next_logins = 0
        #: Every login refused with the ``Login Failed`` shape, so a test can
        #: prove a retry happened rather than one lucky attempt.
        self.refusal_count = 0
        #: When set, the next submitted password gets **no answer at all** — no
        #: prompt, no re-prompt, nothing.
        #:
        #: Models a device that is slow or wedged mid-authentication rather than
        #: one that rejects a credential. The two are worth keeping apart
        #: because they look identical from the driver's side (the prompt does
        #: not arrive) and lead an operator to opposite places: one to a
        #: credential that was correct all along, the other to the device. The
        #: driver reported the first as the second until this hook existed.
        self.stall_next_auth = False
        #: When set, ``oltctrl`` is accepted and acknowledged but **no outlet
        #: moves** — the device lying by omission.
        #:
        #: This models the failure that makes read-back verification mandatory
        #: rather than prudent. On hardware ``oltctrl`` answers with a blank line
        #: and a re-prompt whether or not the contactor moved, so a driver that
        #: trusted the response could not tell this state from success. There is
        #: no way to test that guarantee without a device that can lie.
        self.ignore_switches = False

        if require_login:
            # The real device greets a fresh serial console with a login prompt
            # only after receiving something, so nothing is emitted here.
            pass

    # --- session control (test hooks) -----------------------------------

    def hold_session(self) -> None:
        """Model another transport holding the single CLI session.

        A client that logs in while this is set gets the full banner and is
        then disconnected, exactly as the hardware behaves. That ordering is
        the point: the failure arrives *after* successful authentication, so a
        driver that reports it as an auth error is misdiagnosing it.
        """
        self._held = True

    def release_session(self) -> None:
        """Release the held session (the incumbent sent ``exit``)."""
        self._held = False

    def drop_ssh_session(self, *, wording: str = "received_disconnect") -> None:
        """Model an idle logout **over ssh**, which kills the link.

        ``wording`` selects which of the client's two notices is emitted, and
        having both is the point rather than thoroughness for its own sake: ssh
        prints either depending on how it notices the close, and the second
        collided with the driver's single-session check, so an idle logout was
        reported as "another session is logged in — send 'exit' on it". A hook
        that only ever emitted the first left that path untested and the
        hardware suite found it instead.

        - ``"received_disconnect"`` — the device sent a disconnect message, so
          ssh names it and then says it disconnected.
        - ``"connection_to"`` — ssh noticed the socket close itself.

        The serial and network cases are genuinely different and the driver has
        to treat them differently, so the simulator needs both:
        :py:meth:`force_logout` drops to a login prompt on a live port (serial,
        recoverable in place), while this emits what the *ssh client* prints when
        the device hangs up at ~180 s idle — after which no prompt ever arrives.

        As with :py:meth:`hold_session`, the loopback is left open rather than
        closed: closing it would destroy the pty the driver still holds. The
        silence is modelled explicitly instead, via :py:attr:`_gone`, because it
        is the half that matters — a simulator that emitted the notice and then
        carried on answering would let a driver *appear* to recover from a
        session that is really dead, which is exactly the false pass this hook
        exists to prevent.
        """
        notices = {
            # Both captured from the board against firmware 1.3.4.
            "received_disconnect": (
                "Received disconnect from 192.168.1.246 port 22:11: "
                "user close and disconnect!\r\n"
                "Disconnected from 192.168.1.246 port 22\r\n"
            ),
            "connection_to": "Connection to pdu-benchctrl closed by remote host.\r\n",
        }
        if wording not in notices:
            raise ValueError(
                f"unknown wording {wording!r}; expected one of {sorted(notices)}"
            )
        with self._lock:
            self._authed = False
            self._await_password_for = None
            self._gone = True
            self._emit(notices[wording])

    def force_logout(self) -> None:
        """Drop the session to the login prompt without any wall-clock wait.

        Models the device's idle timeout deterministically. The hazard being
        tested: a logged-out session consumes ``oltctrl index 1 act off`` as a
        *username*, so a mains switch is silently swallowed while the caller
        believes it happened.
        """
        with self._lock:
            self._authed = False
            self._await_password_for = None

    @property
    def authenticated(self) -> bool:
        return self._authed

    # --- emit helpers ---------------------------------------------------

    def _emit(self, text: str) -> None:
        self.send(text.encode("ascii"))

    def _prompt(self) -> None:
        self._emit(PROMPT)

    def _reply(self, body: str, *, command: str) -> None:
        """Emit a response body followed by the prompt.

        ``body`` uses ``\\n`` for line breaks and is converted to the device's
        ``\\r\\n``. Where the real device emits the irregular ``\\n\\r`` pair
        instead (``snmpv3 show``'s row separator), callers write
        :py:data:`RAW_NL_CR` — a plain ``"\\n\\r"`` in the body would be
        rewritten to ``"\\r\\n\\r"`` by the conversion below, and the whole
        point of that quirk is that it reaches the driver intact.
        """
        out = ""
        if self.echo:
            out += command + "\r\n"
        out += body.replace("\n", "\r\n").replace(RAW_NL_CR, "\n\r")
        self._emit(out)
        self._prompt()

    # --- inbound --------------------------------------------------------

    def on_frame_bytes(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)
            while True:
                buf = bytes(self._rx)
                idx = min(
                    (i for i in (buf.find(b"\r"), buf.find(b"\n")) if i >= 0),
                    default=-1,
                )
                if idx < 0:
                    break
                line, rest = buf[:idx], buf[idx + 1:]
                # A CRLF pair must not read as two submissions.
                if rest[:1] in (b"\n", b"\r") and buf[idx:idx + 1] != rest[:1]:
                    rest = rest[1:]
                self._rx = bytearray(rest)
                self._handle_line(line.decode("ascii", errors="replace").strip())

    def _handle_line(self, line: str) -> None:
        if self._gone:
            # The ssh client has exited; there is no longer anything on the far
            # end to answer. Swallowing the line rather than replying is what
            # makes the driver's error the *only* possible outcome: with a
            # simulator that kept answering, a driver missing the disconnect
            # check would still get its prompt and the test would pass.
            return

        if not self._authed:
            self._handle_login(line)
            return

        if not line:
            self._prompt()
            return

        self.command_log.append(line)

        if self.force_next_error is not None:
            shape = self.force_next_error
            self.force_next_error = None
            self._error(shape, line)
            return

        self._dispatch(line)

    # --- login ----------------------------------------------------------

    def _handle_login(self, line: str) -> None:
        """Model the two-step login. Empty input re-prompts, as hardware does."""
        if self._await_password_for is not None:
            user = self._await_password_for
            self._await_password_for = None
            ok = user == self.username and line == self.password
            self.login_log.append((user, ok))
            if self.stall_next_auth:
                # Deliberately before the verdict: the point is a device that
                # never answers, not one that answers slowly with "no". The
                # attempt is still logged, so a test can prove the password was
                # correct and *still* got no prompt.
                self.stall_next_auth = False
                self._emit("\r\nPlease wait for authentication....\r\n")
                return
            busy = self.refuse_next_logins > 0
            if busy:
                self.refuse_next_logins -= 1
            if not ok or busy:
                # One shape for both, because the device has one shape for both:
                # dots for ~15 s, "Login Failed", and then **silence** — no
                # re-prompt, so a driver waiting for one waits forever. The
                # earlier model here emitted a tidy "Login Name :" re-prompt,
                # which no firmware does, and that gap hid a real bug (a wrong
                # password and a still-closing session are indistinguishable, so
                # the driver must retry rather than classify).
                self.refusal_count += 1
                self._emit(
                    "\r\nPlease wait for authentication....\r\n"
                    + "." * 37
                    + "\r\n\r\nLogin Failed"
                )
                return
            self._authed = True
            self._emit("\r\nPlease wait for authentication....\r\n")
            self._banner()
            if self._held:
                # Authentication SUCCEEDED and the device still hangs up. This
                # ordering is what makes the real failure so easy to
                # misdiagnose as bad credentials.
                #
                # The real device drops the TCP/serial connection. Closing the
                # loopback here would destroy the pty the client still holds
                # open, so instead emit the hangup notice the SSH client prints
                # and drop back to unauthenticated — the observable behaviour a
                # driver has to cope with (no prompt ever arrives).
                self.hangup_count += 1
                self._authed = False
                self._emit(
                    "Connection to 192.168.1.246 closed by remote host.\r\n"
                )
            return

        if not line:
            # A bare CR at the login prompt just re-prompts.
            self._emit("\r\n\r\nLogin Name : ")
            return

        self._await_password_for = line
        self._emit("\r\nLogin Password : ")

    def _banner(self) -> None:
        self._emit(
            "CyberPowerSystems Inc., Command Shell v1.0\r\n"
            "\n\rWelcome Administrator!\r\n"
            "CyberPower System                        \r\n"
            f"ePDU Firmware Version    {self.identity['Firmware Version']}"
            f"           {self.identity['Model']}                            \r\n"
            "+------- Information ----------------------------------"
            "---------------------+\r\n"
            f"Name     : {self.identity['Name']}                 "
            "Date : 2026/08/24      \r\n"
            f"Contact  : {self.identity['Contact']}                          "
            "Time : 14:13:07        \r\n"
            f"Location : {self.identity['Location']}                        "
            "User : Administrator   \r\n"
            "Up Time  : 0 days 0 hours 35 mins 07 secs.\n\r\n"
            "(c) 2015, CyberPower Systems, Inc. All rights reserved.           \r\n"
            "+------- Console ------------------------------------------"
            "-----------------+\r\n"
        )
        self._prompt()

    def login_prompt(self) -> None:
        """Emit an unsolicited login prompt, as the device does on wake."""
        self._emit("\r\n\r\nLogin Name : ")

    # --- errors ---------------------------------------------------------

    def _error(self, shape: str, command: str) -> None:
        """Emit one of the three real error shapes.

        ``shape`` is ``"command"``, ``"index"`` or ``"parameter"``. They differ
        in more than wording: the index shape has no caret and no ``Error :``
        prefix, and the blank-line count before the prompt differs across all
        three (2, 3, 0). Both facts break naive parsers.
        """
        if shape == "command":
            # Note the leading bare "\n\r" before the caret on real hardware.
            out = (command + "\n\r" if self.echo else "\n\r")
            out += (
                "             ^\r\nError : Command not found\r\n\r\n"
                + "".join(f"    {v}\r\n" for v in VERBS)
                + "\r\n"
            )
            self._emit(out)
            self._prompt()
            return

        if shape == "index":
            out = (command + "\r\n" if self.echo else "\r\n")
            out += f"\r\nIndex number must be 1 to {self.outlets}\r\n\r\n\r\n"
            self._emit(out)
            self._prompt()
            return

        if shape == "parameter":
            out = (command + "\n\r" if self.echo else "\n\r")
            out += (
                "                                 ^\r\n"
                "Error : Parameter Error\r\n"
                + _USAGE_OLTCTRL
                + "\r\n"
            )
            self._emit(out)
            # No blank line before the prompt in this shape.
            self._prompt()
            return

        raise ValueError(f"unknown error shape {shape!r}")

    # --- dispatch -------------------------------------------------------

    def _dispatch(self, line: str) -> None:
        parts = line.split()
        verb = parts[0].lower()

        if verb == "exit":
            self._emit((line + "\n" if self.echo else "") + "\r\n\rLogout")
            self._authed = False
            return

        if verb not in VERBS:
            self._error("command", line)
            return

        if verb == "sys" and parts[1:2] == ["show"]:
            self._reply(self._sys_show(), command=line)
            return
        if verb == "devsta" and parts[1:2] == ["show"]:
            self._reply(self._devsta_show(), command=line)
            return
        if verb == "oltsta":
            self._oltsta(parts, line)
            return
        if verb == "oltcfg":
            self._oltcfg(parts, line)
            return
        if verb == "oltctrl":
            self._oltctrl(parts, line)
            return
        if verb == "console" and parts[1:2] == ["show"]:
            self._reply(
                "    Console Type :  SSH\n    Telnet Port :  23\n"
                "    SSH Port :  22\n\n",
                command=line,
            )
            return
        if verb in ("snmpv1", "snmpv3") and parts[1:2] == ["show"]:
            self._reply(self._snmp_show(verb), command=line)
            return

        # A known verb with arguments this sim does not model. Answering
        # "Parameter Error" is the honest response — it is what the device says
        # for a malformed known verb, and it keeps the session in sync.
        self._error("parameter", line)

    # --- read commands --------------------------------------------------

    def _sys_show(self) -> str:
        keys = (
            "Name", "Location", "Contact", "Model",
            "Hardware Version", "Firmware Version", "MAC Address",
        )
        return "".join(f"    {k} : {self.identity[k]}\n" for k in keys) + "\n"

    def _devsta_show(self) -> str:
        return (
            "\n    Load\n"
            "    -----------------------------------------------\n"
            "    Device Load : 0.00 A/ 0 W/ 0 VA\n"
            "    Power Factor : ----\n"
            "    Peak Load : 0.00A\t(at 08/08/2025 19:05:15) \n"
            "    Energy : 0.0kWh\t(from 08/08/2025 19:05:15) \n"
            "\n    Utility\n"
            "    -----------------------------------------------\n"
            f"    Voltage : {self.voltage_v:.1f}V\n"
            f"    Frequency : {self.frequency_hz:.1f}Hz\n"
            "\n"
        )

    def _parse_index(self, parts: list[str]) -> Optional[int]:
        """Extract ``index N``, or None if absent/unparseable."""
        if "index" not in parts:
            return None
        pos = parts.index("index")
        if pos + 1 >= len(parts):
            return None
        try:
            return int(parts[pos + 1])
        except ValueError:
            return None

    def _oltsta(self, parts: list[str], line: str) -> None:
        if "index" in parts:
            idx = self._parse_index(parts)
            if idx is None:
                self._error("parameter", line)
                return
            if not 1 <= idx <= self.outlets:
                self._error("index", line)
                return
            state = "On" if self.outlet_state[idx] else "Off"
            self._reply(
                f"\n    {self.outlet_name[idx]}\n"
                f"    Outlet Name : {self.outlet_name[idx]}\n"
                f"    Status :      {state}\n\n",
                command=line,
            )
            return

        if parts[1:2] != ["show"]:
            self._error("parameter", line)
            return

        rows = "".join(
            f"    {i}  {self.outlet_name[i]:<37}{'On' if self.outlet_state[i] else 'Off':<8}\n"
            for i in range(1, self.outlets + 1)
        )
        self._reply(
            "\n    #  Name                                 Status     \n"
            "    ----------------------------------------------------"
            "-------------------------\n"
            + rows
            + "\n",
            command=line,
        )

    def _oltcfg(self, parts: list[str], line: str) -> None:
        # `index all show` is the documented aggregate READ. Reads are safe;
        # only aggregate *writes* are dangerous, and the driver never sends one.
        if "index" in parts and "all" not in parts:
            idx = self._parse_index(parts)
            if idx is None:
                self._error("parameter", line)
                return
            if not 1 <= idx <= self.outlets:
                self._error("index", line)
                return

        rows = "".join(
            f"    {i}  {self.outlet_name[i]:<38}{self.on_delay_s} s"
            f"        {self.off_delay_s} s       {self.reboot_duration_s} s   \n"
            for i in range(1, self.outlets + 1)
        )
        # The two-line wrapped header is real and trips line-oriented parsers.
        self._reply(
            "\n    #  Name                                    On       Off"
            "       Reboot \n"
            "                                             Delay     Delay"
            "     Duration  \n"
            "    ----------------------------------------------------"
            "-------------------------\n"
            + rows
            + "\n",
            command=line,
        )

    def _snmp_show(self, verb: str) -> str:
        if verb == "snmpv1":
            return (
                "    SNMPv1 : Disable\n\n"
                "    Community        IP Address                     "
                "         Access Type\n"
                "    ----------------------------------------------"
                "-----------------------\n"
                "    public           0.0.0.0                        "
                "         Read Only\n"
                "    private          0.0.0.0                        "
                "         Read/Write\n"
                "    public2          0.0.0.0                        "
                "         Forbidden\n"
                "    public3          0.0.0.0                        "
                "         Forbidden\n\n"
            )
        # snmpv3's rows are separated by a bare "\n\r" on real hardware, not
        # "\r\n". Emitted via RAW_NL_CR so the quirk survives to the driver.
        rows = (
            "    snmpv3                          Disable 0.0.0.0     "
            "              NONE  NONE" + RAW_NL_CR
            + "    cyber snmpv3 user2              Disable 0.0.0.0     "
            "              NONE  NONE" + RAW_NL_CR
            + "    cyber snmpv3 user3              Disable 0.0.0.0     "
            "              NONE  NONE" + RAW_NL_CR
            + "    cyber snmpv3 user4              Disable 0.0.0.0     "
            "              NONE  NONE" + RAW_NL_CR
        )
        return (
            "    SNMPv3 : Disable\n\n"
            "    User Name                       Status  IP Address  "
            "              Auth  Priv\n"
            "    ----------------------------------------------------"
            "------------------------\n"
            + rows
            + "\n"
        )

    # --- switching ------------------------------------------------------

    def _oltctrl(self, parts: list[str], line: str) -> None:
        idx = self._parse_index(parts)
        if idx is None or "act" not in parts:
            self._error("parameter", line)
            return
        if not 1 <= idx <= self.outlets:
            self._error("index", line)
            return

        pos = parts.index("act")
        action = parts[pos + 1].lower() if pos + 1 < len(parts) else ""

        if action == "cancel":
            self._pending.pop(idx, None)
            self._blank_ack(line)
            return

        if self.ignore_switches:
            # Acknowledged exactly as a successful switch is — same blank line,
            # same prompt — and nothing moves. See `ignore_switches`.
            self._blank_ack(line)
            return

        targets = {
            "on": True, "off": False,
            "delayon": True, "delayoff": False,
        }
        if action in targets:
            delay = self.on_delay_s if targets[action] else self.off_delay_s
            self._schedule(idx, targets[action], delay)
            self._blank_ack(line)
            return

        if action in ("reboot", "delayreboot"):
            # Off, then back on after the reboot duration.
            self._schedule(idx, False, self.off_delay_s)
            self._schedule_after(
                idx, True, self.off_delay_s + self.reboot_duration_s
            )
            self._blank_ack(line)
            return

        self._error("parameter", line)

    def _blank_ack(self, line: str) -> None:
        """``oltctrl`` answers with a blank line and a prompt. Nothing else.

        Identical whether or not the outlet moved, which is precisely why the
        driver must read the state back rather than trust this.
        """
        self._emit((line + "\r\n" if self.echo else "") + "\r\n")
        self._prompt()

    def _schedule(self, idx: int, state: bool, delay_s: float) -> None:
        """Schedule a switch, replacing anything already queued for this outlet.

        A plain ``on``/``off`` supersedes a pending action, matching the device:
        the last instruction wins. Use :py:meth:`_schedule_after` to *add* a
        transition without discarding the queue, as ``reboot`` does.
        """
        self._pending.pop(idx, None)
        self._schedule_after(idx, state, delay_s)

    def _schedule_after(self, idx: int, state: bool, delay_s: float) -> None:
        if delay_s <= 0:
            self.outlet_state[idx] = state
            return
        self._pending.setdefault(idx, []).append((self.elapsed_s + delay_s, state))

    # --- tick -----------------------------------------------------------

    def on_tick(self, elapsed_s: float) -> None:
        if not self._pending:
            return
        with self._lock:
            for idx in list(self._pending):
                queue = self._pending[idx]
                # In scheduled order, so a reboot's off-then-on cannot land
                # backwards if a slow tick makes both due at once.
                due = [(at, state) for at, state in queue if elapsed_s >= at]
                if not due:
                    continue
                due.sort(key=lambda item: item[0])
                self.outlet_state[idx] = due[-1][1]
                remaining = [item for item in queue if elapsed_s < item[0]]
                if remaining:
                    self._pending[idx] = remaining
                else:
                    self._pending.pop(idx, None)

    # --- fault injection ------------------------------------------------

    def inject_error(self, shape: str = "command") -> None:
        """Make the next command answer with one of the three error shapes."""
        if shape not in ("command", "index", "parameter"):
            raise ValueError(f"unknown error shape {shape!r}")
        self.force_next_error = shape

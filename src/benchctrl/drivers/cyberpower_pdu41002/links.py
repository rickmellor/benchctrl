"""Byte pipes for the PDU41002 CLI: one over serial, one over SSH.

The PDU speaks the *same* line-oriented CLI on its serial console and over
SSH — verified byte-for-byte against hardware, see
``tests/fixtures/pdu41002/``. So the grammar, parsers and error handling live
once in :py:mod:`.driver`, above this seam, and these classes carry nothing but
bytes.

Each link offers the repo's informal four-method I/O duck type —
``write(bytes)``, ``read(n, timeout)``, ``is_open``, ``close()`` — matching what
:py:mod:`benchctrl.transports` and the other drivers already assume.

Why SSH needs a pty rather than :py:mod:`subprocess` pipes: the PDU offers only
``keyboard-interactive`` authentication, so the password must be typed at an
interactive prompt. ``BatchMode=yes`` can never work, and pubkey auth is
refused even with a matching private key. Details in :py:class:`SshLink`.
"""

from __future__ import annotations

import errno
import logging
import os
import pty
import select
import shutil
import signal
import time
from typing import Optional

import serial

log = logging.getLogger("benchctrl.drivers.cyberpower_pdu41002.links")

#: Serial console settings, from the manual and confirmed on hardware.
BAUDRATE = 9600


class LinkError(RuntimeError):
    """Transport-level failure. The driver wraps this in its own hierarchy."""


class SerialLink:
    """The PDU's RJ45/DB9 serial console, over pyserial.

    9600 8N1, no flow control. The vendor cable reads CTS and DSR both low, so
    hardware flow control must stay off or writes block forever.
    """

    def __init__(self, port: str, *, baudrate: int = BAUDRATE) -> None:
        self.port = port
        self.baudrate = baudrate
        self._ser: Optional[serial.Serial] = None

    def open(self) -> None:
        try:
            self._ser = serial.Serial(
                port=self.port,
                baudrate=self.baudrate,
                bytesize=serial.EIGHTBITS,
                parity=serial.PARITY_NONE,
                stopbits=serial.STOPBITS_ONE,
                timeout=0.2,
                # The vendor cable leaves CTS/DSR deasserted; any handshaking
                # here turns a working link into a silent hang.
                dsrdtr=False,
                rtscts=False,
                xonxoff=False,
            )
        except (OSError, serial.SerialException) as e:
            raise LinkError(f"could not open serial port {self.port!r}: {e}") from e
        time.sleep(0.15)
        try:
            self._ser.reset_input_buffer()
        except Exception:  # pragma: no cover - best-effort drain
            pass

    @property
    def is_open(self) -> bool:
        return self._ser is not None and self._ser.is_open

    def write(self, data: bytes) -> None:
        if self._ser is None or not self._ser.is_open:
            raise LinkError("serial link is not open")
        try:
            self._ser.write(data)
            self._ser.flush()
        except (OSError, serial.SerialException) as e:
            raise LinkError(f"serial write failed: {e}") from e

    def read(self, max_bytes: int = 4096, timeout: float = 0.2) -> bytes:
        if self._ser is None or not self._ser.is_open:
            raise LinkError("serial link is not open")
        try:
            self._ser.timeout = timeout
            return self._ser.read(max_bytes)
        except (OSError, serial.SerialException) as e:
            raise LinkError(f"serial read failed: {e}") from e

    def reset_input(self) -> None:
        if self._ser is not None and self._ser.is_open:
            try:
                self._ser.reset_input_buffer()
            except Exception:  # pragma: no cover - best-effort
                pass

    def close(self) -> None:
        if self._ser is not None:
            try:
                self._ser.close()
            except Exception:  # pragma: no cover - best-effort
                pass
            self._ser = None

    def __repr__(self) -> str:
        return f"SerialLink(port={self.port!r}, open={self.is_open})"


#: ``ssh`` options that are all forced by measured firmware-1.3.4 defects.
#: Each looks like a careless security default and is not — see the comments.
SSH_OPTIONS: tuple[tuple[str, str], ...] = (
    # The PDU's diffie-hellman-group-exchange-sha256 is broken: the handshake
    # dies with "key exchange failed!". A fixed group works. The defect is in
    # group *exchange* (where the client asks the server to propose a group),
    # not in DH itself.
    ("KexAlgorithms", "diffie-hellman-group14-sha256"),
    # The device refuses publickey auth even when the matching private key is
    # offered, so attempting it only wastes a round trip and muddies the logs.
    ("PubkeyAuthentication", "no"),
    # The device advertises an ed25519 host key whose public bytes are ALL
    # ZEROS, so there is nothing to verify against and no TOFU value to gain.
    ("StrictHostKeyChecking", "no"),
    # ...and given the above, never write that null key into the operator's
    # real known_hosts, where it would linger and confuse later diagnosis.
    ("UserKnownHostsFile", "/dev/null"),
    # Authentication alone takes ~7.5 s on this firmware.
    ("ConnectTimeout", "15"),
)


class SshLink:
    """The PDU's CLI over SSH, driven through a pty.

    A pty rather than :py:mod:`subprocess` pipes for two reasons, both measured
    rather than assumed:

    1. **The device offers only ``keyboard-interactive`` authentication.** The
       password has to be typed at ssh's own interactive prompt, which ssh
       reads from a terminal, not stdin. ``BatchMode=yes`` therefore cannot
       work, and neither can piping the password in.
    2. **The PDU allocates a pty for its CLI**, hence ``-tt``.

    The password is written into the pty and never appears on the command line,
    in the environment, or in any log — a process listing would otherwise
    expose it to every user on the host. ``sshpass`` is deliberately not used
    (it is also absent from the bench agent host).

    Authentication is slow: ~7.5 s from password submission to the banner on
    firmware 1.3.4, so ``login_timeout`` defaults generously.
    """

    def __init__(
        self,
        host: str,
        *,
        username: str = "admin",
        port: int = 22,
        ssh_path: Optional[str] = None,
        connect_timeout: float = 15.0,
    ) -> None:
        self.host = host
        self.username = username
        self.port = port
        self.connect_timeout = connect_timeout
        self._ssh_path = ssh_path
        self._pid: Optional[int] = None
        self._fd: Optional[int] = None

    # -- argv ---------------------------------------------------------------

    def argv(self) -> list[str]:
        """The exact ssh invocation. Public so tests can assert on it."""
        exe = self._ssh_path or shutil.which("ssh") or "ssh"
        args = [exe, "-tt"]
        for key, value in SSH_OPTIONS:
            args += ["-o", f"{key}={value}"]
        if self.port != 22:
            args += ["-p", str(self.port)]
        args.append(f"{self.username}@{self.host}")
        return args

    # -- lifecycle ----------------------------------------------------------

    def open(self) -> None:
        if self._ssh_path is None and shutil.which("ssh") is None:
            raise LinkError(
                "no ssh client found on PATH; the PDU's network transport "
                "needs /usr/bin/ssh (no paramiko dependency is used)"
            )
        argv = self.argv()
        try:
            pid, fd = pty.fork()
        except OSError as e:  # pragma: no cover - fork failure
            raise LinkError(f"could not fork a pty for ssh: {e}") from e
        if pid == 0:  # pragma: no cover - child never returns
            try:
                os.execvp(argv[0], argv)
            finally:
                os._exit(127)
        self._pid = pid
        self._fd = fd

    @property
    def is_open(self) -> bool:
        return self._fd is not None

    def write(self, data: bytes) -> None:
        if self._fd is None:
            raise LinkError("ssh link is not open")
        try:
            os.write(self._fd, data)
        except OSError as e:
            raise LinkError(f"ssh write failed: {e}") from e

    def read(self, max_bytes: int = 4096, timeout: float = 0.2) -> bytes:
        """Read available bytes, returning ``b""`` on timeout.

        A closed pty raises ``EIO`` on Linux rather than returning EOF, so that
        is translated to "no more bytes" — the driver detects a dropped session
        from the absence of a prompt, not from an exception here.
        """
        if self._fd is None:
            raise LinkError("ssh link is not open")
        try:
            ready, _, _ = select.select([self._fd], [], [], timeout)
        except (OSError, ValueError) as e:
            raise LinkError(f"ssh select failed: {e}") from e
        if not ready:
            return b""
        try:
            return os.read(self._fd, max_bytes)
        except OSError as e:
            if e.errno in (errno.EIO, errno.EBADF):
                return b""
            raise LinkError(f"ssh read failed: {e}") from e

    def reset_input(self) -> None:
        """Drain anything already buffered. Cheap and non-blocking."""
        if self._fd is None:
            return
        while True:
            try:
                ready, _, _ = select.select([self._fd], [], [], 0.0)
                if not ready or not os.read(self._fd, 4096):
                    return
            except OSError:
                return

    def close(self) -> None:
        """Close the pty and reap the ssh child."""
        fd, pid = self._fd, self._pid
        self._fd = self._pid = None
        if fd is not None:
            try:
                os.close(fd)
            except OSError:  # pragma: no cover - best-effort
                pass
        if pid is not None:
            for sig in (signal.SIGTERM, signal.SIGKILL):
                try:
                    dead, _ = os.waitpid(pid, os.WNOHANG)
                    if dead == pid:
                        return
                    os.kill(pid, sig)
                    time.sleep(0.1)
                except (ChildProcessError, ProcessLookupError):
                    return
                except OSError:  # pragma: no cover - best-effort
                    return
            try:
                os.waitpid(pid, os.WNOHANG)
            except OSError:  # pragma: no cover - best-effort
                pass

    def __repr__(self) -> str:
        # Deliberately no credential material: this object never holds the
        # password, but a repr is exactly where one would leak if it did.
        return (
            f"SshLink(host={self.host!r}, username={self.username!r}, "
            f"port={self.port}, open={self.is_open})"
        )

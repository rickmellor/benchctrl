"""Pick a serial transport: kernel driver first, userspace bridge as fallback.

Some hosts have a kernel ``ch341`` driver and expose a CH340 bridge as
``/dev/ttyUSB0`` (or ``COM7`` on Windows); Arduino's Uno Q does not, and the
same physical adapter binds nothing at all (see
:py:mod:`benchctrl.transports.ch341`). Both are normal. What is *not* normal is
making the operator know which one they are on before they can name a port.

This module makes that choice, in one place, with a fixed precedence:

1. **An explicit port wins.** If the caller names one, it is opened and nothing
   is probed. An operator overriding the guess is not second-guessed.
2. **A kernel tty wins over userspace.** If the bridge has a driver bound, use
   it: it is battle-tested, survives suspend/resume, and costs no Python thread.
   Ours is a workaround, and a workaround should not win by default.
3. **Userspace only when the kernel offers nothing.** A CH340 enumerated on the
   USB bus with no tty bound is the Uno Q case, and only then do we claim the
   chip over libusb and bridge it to a pty.

**A failed open is not a reason to fall back.** If a kernel tty exists and
opening it fails, that raises. Sliding to userspace would turn "the cable is
unplugged" or "another process holds the port" into a different, working
transport that measures something else — the silent-fallback bug CONTRIBUTING
rule 4 is about. The transport is chosen by what the host *has*, never by what
happened to fail.

Layering: this sits below the drivers, beside pyserial. It imports no driver;
it takes the driver's ``open`` as a callable.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Callable, Optional

from benchctrl.exceptions import BenchConnectionError
from benchctrl.transports.ch341 import CH340_PID, CH340_VID
from benchctrl.transports.ptybridge import PtySerialBridge, open_ch341_pty

log = logging.getLogger("benchctrl.transports.autoserial")

#: Ports that mean "work it out yourself" rather than naming a device.
#:
#: ``"auto"`` matches the convention the USB-TMC drivers already use for their
#: resource strings, so one spelling works across the bench.
AUTO_SENTINELS = frozenset({None, "", "auto", "AUTO", "Auto"})


@dataclass(frozen=True)
class SerialTarget:
    """A port to open, plus the bridge that owns it (if any).

    ``bridge`` is ``None`` whenever the kernel supplied the port. When it is
    not ``None``, its lifetime must match the driver's — see
    :py:func:`open_serial_driver`, which binds the two.
    """

    port: str
    bridge: Optional[PtySerialBridge] = None
    #: How the port was chosen, for logs and for tests to assert on.
    how: str = "explicit"


def _kernel_ttys_for(vid: int, pid: int) -> list[str]:
    """Kernel-bound tty paths for a VID/PID, or [] if the kernel bound none.

    Empty is a normal answer, not an error: it is exactly the Uno Q case.

    Validation gap: the non-empty branch has never been exercised on hardware.
    No host on this bench has a kernel ``ch341`` (the Uno Q is built without
    it; WSL passes no CH340 through), so "prefer the kernel tty" is pinned by
    ``tests/test_autoserial.py`` and not yet observed on silicon. See
    ``ROADMAP.md`` § *Revalidate serial transport selection on a desktop Linux
    host* and ``KNOWN_LIMITATIONS`` § N-6.
    """
    try:
        import serial.tools.list_ports as list_ports
    except ImportError:  # pragma: no cover - pyserial is a hard dependency
        return []
    return [
        p.device
        for p in list_ports.comports()
        if getattr(p, "vid", None) == vid and getattr(p, "pid", None) == pid
    ]


def resolve_ch341_port(
    *,
    port: Optional[str] = None,
    serial_number: Optional[str] = None,
    baudrate: int = 115200,
) -> SerialTarget:
    """Decide how to reach a CH340, per this module's precedence.

    ``serial_number`` only reaches the userspace path, and only helps if the
    adapter actually publishes one — ours reports ``iSerialNumber=0``, i.e. no
    descriptor, so nothing can match it and ``index=`` is the only way to pick
    among several. Untested against multiple adapters on hardware; see
    ``ROADMAP.md``.

    Raises:
        BenchConnectionError: if no CH340 is reachable either way. The message
            names both things that were looked for, because "not found" is
            ambiguous between an unplugged cable and a missing udev rule.
    """
    if port not in AUTO_SENTINELS:
        assert port is not None  # narrowed by the sentinel check
        log.debug("autoserial: using explicitly configured port %s", port)
        return SerialTarget(port=port, how="explicit")

    ttys = _kernel_ttys_for(CH340_VID, CH340_PID)
    if ttys:
        if len(ttys) > 1:
            log.warning(
                "autoserial: %d CH340 ttys present (%s) — using %s. Name a port "
                "explicitly to choose.",
                len(ttys),
                ", ".join(ttys),
                ttys[0],
            )
        log.info("autoserial: kernel ch341 driver present, using %s", ttys[0])
        return SerialTarget(port=ttys[0], how="kernel")

    # No tty. Either there is no adapter, or the kernel has no driver for it.
    # Only libusb can tell those apart.
    log.info(
        "autoserial: no CH340 tty; trying the userspace CH341 driver "
        "(kernel likely built without CONFIG_USB_SERIAL_CH341)"
    )
    try:
        bridge = open_ch341_pty(serial_number=serial_number, baudrate=baudrate)
    except BenchConnectionError as exc:
        raise BenchConnectionError(
            f"no CH340 reachable ({CH340_VID:04x}:{CH340_PID:04x}): no kernel tty "
            f"and the userspace driver could not claim one either ({exc}). Check "
            f"the cable; on a kernel without ch341 also check that the udev rule "
            f"in deploy/udev/60-benchctrl-ch341.rules is installed, since libusb "
            f"needs write access to /dev/bus/usb/*."
        ) from exc
    return SerialTarget(port=bridge.port, bridge=bridge, how="userspace")


def open_serial_driver(
    opener: Callable[..., Any],
    *,
    port: Optional[str] = None,
    serial_number: Optional[str] = None,
    **open_kwargs: Any,
) -> Any:
    """Open a pyserial-based driver on a resolved port, binding bridge lifetime.

    ``opener`` is the driver's ``open`` classmethod, called as
    ``opener(resolved_port, **open_kwargs)``. The driver never learns whether
    its port came from the kernel or from our bridge::

        qr = open_serial_driver(QR10x.open, port=None, baudrate=115200)

    When a bridge is created, the returned object's ``close`` also closes the
    bridge, so ``registry.close()`` and every existing teardown path release
    the USB claim without knowing the bridge exists. Without that the chip
    would stay claimed until the process exited and the next open would fail.
    """
    baudrate = open_kwargs.get("baudrate", 115200)
    target = resolve_ch341_port(
        port=port, serial_number=serial_number, baudrate=baudrate
    )

    try:
        obj = opener(target.port, **open_kwargs)
    except Exception:
        if target.bridge is not None:
            target.bridge.close()
        raise

    if target.bridge is not None:
        _chain_close(obj, target.bridge)
    return obj


def _chain_close(obj: Any, bridge: PtySerialBridge) -> None:
    """Make ``obj.close()`` also close ``bridge``.

    Done per instance rather than by subclassing the driver: the driver class
    is shared with the kernel-tty and simulator paths, which own no bridge, and
    a wrapper class would have to forward the whole instrument surface (and go
    stale as that surface grows).

    The bridge is closed even if the driver's own ``close`` raises. A driver
    that fails to close still has to give the USB device back, or the next open
    finds the chip claimed by a dead handle.
    """
    original = obj.close

    def close_both(*args: Any, **kwargs: Any) -> Any:
        try:
            return original(*args, **kwargs)
        finally:
            bridge.close()

    obj.close = close_both
    # Discoverable for debugging and asserted by the tests; not a public API.
    obj._benchctrl_bridge = bridge

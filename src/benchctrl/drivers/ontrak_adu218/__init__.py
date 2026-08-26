"""Ontrak ADU218 USB relay / digital-input interface driver.

8 PhotoMOS solid-state relays (Panasonic AQZ207, 120 V AC / DC rated) and 8
optically-isolated digital inputs, on one USB device.

Exposes, so far:

- :py:class:`Adu218UsbfsLink` — the transport. A stdlib-only USB link.
- :py:class:`Adu218Device` — one enumerated unit: where it is, which one it is.
- :py:class:`Adu218LinkError` / :py:class:`Adu218LinkTimeout` — transport faults.

The device class itself is not here yet; this package currently ships the link
seam alone. See ``MEGAPLAN_ADU218.md`` for the staging.

**It is USB HID, not a serial device.** There is no tty, no ``/dev/hidraw*``
node, and no vendor driver that runs on this board — so the transport is raw
USBDEVFS ioctls through ``fcntl`` and ``ctypes``, with **zero dependencies**.
That is not a purity exercise: the bench agent host has no pip, so a compiled
wheel would have made the device unreachable there. See
:py:mod:`~benchctrl.drivers.ontrak_adu218.usbfs` for why a "bulk" ioctl on an
interrupt endpoint is contractual rather than a lucky accident, and why the
async URB interface was rejected.

**Every claim in this package is backed by a hardware capture** under
``tests/fixtures/adu218/``, and where the vendor manual disagrees with a
capture, the capture wins — the manual contradicts itself in at least one
place. Two findings from those captures shape the whole design:

*Silence is the only error signal.* An unknown command, a valid command with an
out-of-range argument, and a write-only command are all byte-identical on the
wire: nothing comes back. So there is no such thing as a generic "did that
work?" — only a per-command expectation, which lives in the device class above
the link. And a reply that is not read stays queued on the IN endpoint, so the
*next* query returns the *previous* command's answer. Hence drain-on-open.

*No contact-resistance figure is trustworthy, however repeatable.* The same
closed relay measured 6.14 Ω, then 10.69 Ω, then 10.65 Ω across three
sessions, milliohm-stable within each, with the step traced to re-seated
screw-clamped probes rather than to the relay. Open versus closed therefore
keys on the DMM's over-range sentinel, and this driver reports no resistance at
all. A threshold set from any one of those numbers would have misread the
others.

**Relay state never borrows the word "open".** ``is_open`` means *the link is
connected* — the framework-wide sense, matching ``agent/registry.py`` — which is
the opposite sign to a relay contact being open. Relay state is reported as
energised / de-energised, and ``close()`` closes the *link* and deliberately
leaves the relays exactly where they were.

This is a concrete class implementing no :py:mod:`benchctrl.interfaces`
Protocol. Per ``CONTRIBUTING.md`` convention 3 a Protocol arrives with the
*second* instance of a device class; a ``Switch`` abstraction generalised from
either a mains PDU or an SSR I/O board would fit the other one badly.
"""

from benchctrl.drivers.ontrak_adu218.usbfs import (
    Adu218Device,
    Adu218LinkError,
    Adu218LinkTimeout,
    Adu218UsbfsLink,
    enumerate_devices,
    find_device,
)

__all__ = [
    "Adu218Device",
    "Adu218LinkError",
    "Adu218LinkTimeout",
    "Adu218UsbfsLink",
    "enumerate_devices",
    "find_device",
]

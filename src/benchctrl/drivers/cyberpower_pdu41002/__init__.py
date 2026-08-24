"""CyberPower PDU41002 switched PDU driver.

Exposes:

- :py:class:`CyberPowerPDU41002` — the device class. Identity, device
  metering (load / voltage / frequency), per-outlet state and names, and
  per-outlet switching delays. Outlet *switching* arrives in Stage 2.
- :py:class:`PDU41002Info` — identity / firmware metadata.
- :py:class:`PDU41002Status` — device metering snapshot.
- :py:class:`OutletConfig` — per-outlet on / off / reboot delays.
- :py:class:`PDU41002Error` and subclasses — driver-specific exceptions.

Wire stack: **two transports, one CLI.** A line-oriented command shell,
reached either over the serial console (FTDI FT232R, 9600 8N1) or over SSH.
Command output is byte-identical between them — verified against hardware, see
``tests/fixtures/pdu41002/`` — so one grammar, one parser and one simulator
serve both. SNMP is deliberately unused: the CLI covers every capability, and
one protocol means one parser instead of two.

**The CLI is single-session across all transports.** Serial and SSH cannot be
live at once; the incumbent session wins and the newcomer is hung up on *after*
its password is accepted. So "network alongside serial" means alternating, not
concurrent — and :py:meth:`CyberPowerPDU41002.close` must send ``exit``,
because closing the port alone does not release the device's session.

The password comes from ``BENCHCTRL_PDU_PASSWORD`` in the environment of the
host running the driver, never from a config file — see the
:py:mod:`~benchctrl.drivers.cyberpower_pdu41002.driver` module docstring for
why config is the wrong place for it.

This is a concrete class implementing no :py:mod:`benchctrl.interfaces`
Protocol. Per ``CONTRIBUTING.md`` convention 3, a Protocol is defined when the
*second* instance of a device class lands; a ``Switch`` abstraction generalised
from a mains PDU would fit a signal multiplexer poorly. Callers depend on this
surface directly, as they do for the QR10x and DP2031.
"""

from benchctrl.drivers.cyberpower_pdu41002.driver import (
    CyberPowerPDU41002,
    OutletConfig,
    PDU41002AuthError,
    PDU41002CommandError,
    PDU41002ConnectionError,
    PDU41002Error,
    PDU41002Info,
    PDU41002PolicyError,
    PDU41002ProtocolError,
    PDU41002SessionError,
    PDU41002Status,
    PDU41002TimeoutError,
    PDU41002ValueError,
)

__all__ = [
    "CyberPowerPDU41002",
    "PDU41002Info",
    "PDU41002Status",
    "OutletConfig",
    "PDU41002Error",
    "PDU41002AuthError",
    "PDU41002CommandError",
    "PDU41002ConnectionError",
    "PDU41002PolicyError",
    "PDU41002ProtocolError",
    "PDU41002SessionError",
    "PDU41002TimeoutError",
    "PDU41002ValueError",
]

"""Simulated instruments backed by real serial loopbacks.

The simulators here exist so the whole stack — drivers, transport, framing,
recording, the remote agent, and the MCP tool surface — can be exercised end
to end with no hardware attached. They are *device* simulators, not mocks of
benchctrl classes: each one speaks its instrument's real wire protocol over a
pty, and the production driver connects to it unmodified.

    from benchctrl.sim import SimulatedOtiiArc
    from benchctrl.drivers.otii_arc import OtiiArc

    with SimulatedOtiiArc() as sim:
        smu = OtiiArc.open(sim.port)
        smu.set_voltage(3.3)
        smu.set_output(True)
        with smu.record("mc", "mv") as rec:
            time.sleep(1.0)
        print(rec.statistics("mc").mean)

Nothing in this package is imported by the driver or MCP layers; it is a
development and test dependency only, and costs nothing at runtime.
"""

from __future__ import annotations

from benchctrl.sim.base import SimDevice
from benchctrl.sim.loopback import SerialLoopback, open_loopback
from benchctrl.sim.otii_arc import SimulatedOtiiArc
from benchctrl.sim.qr10x import SimulatedQR10x
from benchctrl.sim.waveforms import (
    Constant,
    OhmicLoad,
    Ramp,
    Sine,
    Square,
    Steps,
    Waveform,
)

__all__ = [
    "SimDevice",
    "SerialLoopback",
    "open_loopback",
    "SimulatedOtiiArc",
    "SimulatedQR10x",
    "Constant",
    "OhmicLoad",
    "Ramp",
    "Sine",
    "Square",
    "Steps",
    "Waveform",
]

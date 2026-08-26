"""Build a real driver bound to a simulated instrument.

``session.resolve(key, ...)`` in ``mode="sim"`` calls one of these. Each
returns the *production driver class* connected over a pty to a simulator —
not a mock of the driver. Sim mode therefore exercises the same code path as
hardware: driver, transport, framing, and pyvisa where applicable.

The simulator's lifetime is tied to the driver: closing the driver closes the
simulator and releases the pty, so a test that forgets to clean up leaks a
file descriptor rather than a thread.
"""

from __future__ import annotations

import logging
from typing import Any, Callable

from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.sim.factories")


def _bind_lifetime(driver: Any, sim: Any) -> Any:
    """Close ``sim`` when ``driver`` closes, and keep it referenced."""
    driver._benchctrl_sim = sim  # also prevents GC of the simulator
    original_close = driver.close

    def close_both(*args, **kwargs):
        try:
            return original_close(*args, **kwargs)
        finally:
            sim.close()

    driver.close = close_both  # type: ignore[method-assign]
    return driver


def make_otii_arc(**kwargs) -> Any:
    """A real ``OtiiArc`` driving a :py:class:`SimulatedOtiiArc`."""
    from benchctrl.drivers.otii_arc import OtiiArc
    from benchctrl.sim.otii_arc import SimulatedOtiiArc

    sim_kwargs = kwargs.pop("sim", {})
    kwargs.pop("port", None)  # the caller's port is meaningless here
    sim = SimulatedOtiiArc(**sim_kwargs)
    sim.start()
    try:
        driver = OtiiArc.open(sim.port, **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def make_qr10x(**kwargs) -> Any:
    """A real ``QR10x`` driving a :py:class:`SimulatedQR10x`."""
    from benchctrl.drivers.eastwood_qr10x import QR10x
    from benchctrl.sim.qr10x import SimulatedQR10x

    sim_kwargs = kwargs.pop("sim", {})
    kwargs.pop("port", None)
    sim = SimulatedQR10x(**sim_kwargs)
    sim.start()
    try:
        driver = QR10x.open(sim.port, **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def _asrl(port: str) -> str:
    """VISA resource string for a serial device, via the pyvisa-py backend."""
    return f"ASRL{port}::INSTR"


def make_dl3031a(**kwargs) -> Any:
    """A real ``RigolDL3031A`` over pyvisa-py's serial backend."""
    from benchctrl.drivers.rigol_dl3031a import RigolDL3031A
    from benchctrl.sim.scpi import SimulatedRigolDL3031A

    sim_kwargs = kwargs.pop("sim", {})
    kwargs.pop("resource", None)
    sim = SimulatedRigolDL3031A(**sim_kwargs)
    sim.start()
    try:
        driver = RigolDL3031A.open(_asrl(sim.port), **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def make_dp2031(**kwargs) -> Any:
    """A real ``RigolDP2031`` over pyvisa-py's serial backend."""
    from benchctrl.drivers.rigol_dp2031 import RigolDP2031
    from benchctrl.sim.scpi import SimulatedRigolDP2031

    sim_kwargs = kwargs.pop("sim", {})
    kwargs.pop("resource", None)
    sim = SimulatedRigolDP2031(**sim_kwargs)
    sim.start()
    try:
        driver = RigolDP2031.open(_asrl(sim.port), **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def make_sdm4065a(**kwargs) -> Any:
    """A real ``SiglentSDM4065A`` over pyvisa-py's serial backend."""
    from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A
    from benchctrl.sim.sdm4065a import SimulatedSDM4065A

    sim_kwargs = kwargs.pop("sim", {})
    kwargs.pop("resource", None)
    sim = SimulatedSDM4065A(**sim_kwargs)
    sim.start()
    try:
        driver = SiglentSDM4065A.open(_asrl(sim.port), **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def make_pdu41002(**kwargs) -> Any:
    """A real ``CyberPowerPDU41002`` driving a :py:class:`SimulatedPDU41002`.

    Differs from the other factories in two ways, both because this is the
    first device with a credential and the first that switches mains:

    - **The password is supplied here**, matching the simulator's, so sim mode
      never needs ``BENCHCTRL_PDU_PASSWORD`` set. A test that had to export a
      password to run offline would be a bad trade.
    - **``allowed_outlets`` defaults to every outlet — in sim mode only.** On
      hardware it is mandatory and has no default, precisely so a config typo
      cannot silently widen switching scope. Against a simulator there are no
      contactors to move, and requiring it would make every sim caller repeat
      boilerplate. Callers can still pass a narrower set to exercise the
      allowlist itself.
    """
    from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002
    from benchctrl.sim.pdu41002 import SimulatedPDU41002

    sim_kwargs = dict(kwargs.pop("sim", {}))
    kwargs.pop("port", None)
    kwargs.pop("host", None)  # the sim is always reached over its pty
    sim = SimulatedPDU41002(**sim_kwargs)
    sim.start()
    kwargs.setdefault("password", sim.password)
    kwargs.setdefault("allowed_outlets", tuple(range(1, sim.outlets + 1)))
    try:
        driver = CyberPowerPDU41002.open(port=sim.port, **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


def make_adu218(**kwargs) -> Any:
    """A real ``OntrakADU218`` driving a :py:class:`SimulatedAdu218Link`.

    The only factory that does not create a pty, because the ADU218 is USB HID
    and has no byte stream to loop back. The simulator subclasses the
    *production* USBDEVFS link and overrides only the ioctl, so framing, the
    mandatory ``0x01`` report id, the desync check and ``drain()`` are all still
    shipping code — see :py:mod:`benchctrl.sim.adu218`.

    ``allowed_relays`` is left at its permissive default rather than being
    narrowed here: unlike the PDU there are no mains contactors, and the
    operator's stated policy is that these 1 A SSRs toggle freely with the
    hardware watchdog as the per-test interlock. A caller can still pass a
    narrower set to exercise the allowlist itself.
    """
    from benchctrl.drivers.ontrak_adu218 import OntrakADU218
    from benchctrl.sim.adu218 import SimulatedAdu218Link

    sim_kwargs = dict(kwargs.pop("sim", {}))
    kwargs.pop("port", None)  # there is no port; identity is the descriptor
    kwargs.pop("path", None)
    sim = SimulatedAdu218Link(**sim_kwargs)
    try:
        driver = OntrakADU218.open(link=sim, **kwargs)
    except Exception:
        sim.close()
        raise
    return _bind_lifetime(driver, sim)


FACTORIES: dict[str, Callable[..., Any]] = {
    "otii_arc": make_otii_arc,
    "eastwood_qr10x": make_qr10x,
    "rigol_dl3031a": make_dl3031a,
    "rigol_dp2031": make_dp2031,
    "siglent_sdm4065a": make_sdm4065a,
    "cyberpower_pdu41002": make_pdu41002,
    "ontrak_adu218": make_adu218,
}


def factory_for(device_key: str) -> Callable[..., Any]:
    try:
        return FACTORIES[device_key]
    except KeyError:
        raise BenchValueError(
            f"no simulator for device key {device_key!r}; "
            f"available: {sorted(FACTORIES)}"
        ) from None

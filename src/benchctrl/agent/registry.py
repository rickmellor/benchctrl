"""Which devices the agent owns, and what each exposes.

Registration is where the remote surface is decided: the agent introspects
the live driver object once, and that snapshot is the allowlist for the rest
of the session. Nothing the client sends can widen it.

Devices open lazily. A bench where the Rigol is powered off should still
serve the Arc, so a failure to open one device is reported on that device's
first use rather than at startup.
"""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

from benchctrl.agent.dispatch import DeviceSurface, introspect
from benchctrl.exceptions import BenchConnectionError, BenchValueError

log = logging.getLogger("benchctrl.agent.registry")


@dataclass
class DeviceEntry:
    """One device the agent can serve."""

    device_key: str
    opener: Callable[..., Any]
    open_kwargs: dict = field(default_factory=dict)
    obj: Optional[Any] = None
    surface: Optional[DeviceSurface] = None
    open_error: Optional[str] = None

    @property
    def is_open(self) -> bool:
        return self.obj is not None

    def to_dict(self) -> dict:
        d: dict = {"key": self.device_key, "open": self.is_open}
        if self.surface is not None:
            d.update(self.surface.to_dict())
        if self.open_error:
            d["open_error"] = self.open_error
        return d


class DeviceRegistry:
    """The agent's device table."""

    def __init__(self) -> None:
        self._entries: dict[str, DeviceEntry] = {}
        self._lock = threading.RLock()

    # --- registration ---------------------------------------------------

    def register(
        self,
        device_key: str,
        opener: Callable[..., Any],
        *,
        open_kwargs: Optional[dict] = None,
    ) -> None:
        """Declare a device the agent may open. Does not open it."""
        with self._lock:
            self._entries[device_key] = DeviceEntry(
                device_key=device_key,
                opener=opener,
                open_kwargs=dict(open_kwargs or {}),
            )
            log.info("registry: declared %s", device_key)

    def register_open(self, device_key: str, obj: Any) -> DeviceEntry:
        """Register an already-open object (tests, and sim mode)."""
        with self._lock:
            entry = DeviceEntry(
                device_key=device_key,
                opener=lambda **kw: obj,
                obj=obj,
                surface=introspect(obj, device_key),
            )
            self._entries[device_key] = entry
            return entry

    @property
    def keys(self) -> list[str]:
        with self._lock:
            return sorted(self._entries)

    def entry(self, device_key: str) -> DeviceEntry:
        with self._lock:
            entry = self._entries.get(device_key)
        if entry is None:
            raise BenchValueError(
                f"this agent does not serve {device_key!r}; it serves: "
                f"{self.keys}"
            )
        return entry

    # --- lifecycle ------------------------------------------------------

    def open(self, device_key: str, **override_kwargs) -> DeviceEntry:
        """Open a device if it isn't already. Idempotent."""
        entry = self.entry(device_key)
        with self._lock:
            if entry.is_open:
                return entry
            kwargs = {**entry.open_kwargs, **override_kwargs}
            # Drop Nones so an explicit "auto-discover" from the client
            # doesn't override a configured port on the agent.
            kwargs = {k: v for k, v in kwargs.items() if v is not None}
            try:
                obj = entry.opener(**kwargs)
            except Exception as exc:  # noqa: BLE001
                entry.open_error = repr(exc)
                log.warning("registry: opening %s failed: %r", device_key, exc)
                raise
            entry.obj = obj
            entry.surface = introspect(obj, device_key)
            entry.open_error = None
            log.info(
                "registry: opened %s (%s) — %d methods, %d properties",
                device_key,
                entry.surface.class_name,
                len(entry.surface.methods),
                len(entry.surface.properties),
            )
            return entry

    def get(self, device_key: str) -> Any:
        """The live object, opening it on first use."""
        entry = self.entry(device_key)
        if not entry.is_open:
            entry = self.open(device_key)
        obj = entry.obj
        if obj is None:  # pragma: no cover - open() raises instead
            raise BenchConnectionError(f"{device_key} is not open")
        return obj

    def surface_of(self, device_key: str) -> DeviceSurface:
        entry = self.entry(device_key)
        if entry.surface is None:
            entry = self.open(device_key)
        assert entry.surface is not None
        return entry.surface

    def close(self, device_key: str) -> bool:
        """Close one device. Returns whether anything was closed."""
        entry = self.entry(device_key)
        with self._lock:
            obj, entry.obj, entry.surface = entry.obj, None, None
        if obj is None:
            return False
        try:
            obj.close()
        except Exception as exc:  # noqa: BLE001
            log.warning("registry: closing %s raised: %r", device_key, exc)
        return True

    def close_all(self) -> None:
        for key in self.keys:
            try:
                self.close(key)
            except Exception:  # noqa: BLE001 - teardown is best effort
                log.exception("registry: closing %s failed", key)

    def open_devices(self) -> dict[str, Any]:
        with self._lock:
            return {k: e.obj for k, e in self._entries.items() if e.obj is not None}

    def describe(self) -> list[dict]:
        with self._lock:
            return [e.to_dict() for e in self._entries.values()]


def build_default_registry(
    device_keys: Optional[list[str]] = None,
    *,
    simulate: bool = False,
    open_kwargs: Optional[dict[str, dict]] = None,
) -> DeviceRegistry:
    """Registry for the standard four drivers.

    With ``simulate=True`` every device is backed by a simulator behind a
    pty — the production driver still runs, so the agent is exercised
    end to end with no instruments attached. That is what makes the whole
    remote stack developable without hardware.
    """
    from benchctrl.config import DEVICE_KEYS

    keys = device_keys or list(DEVICE_KEYS)
    registry = DeviceRegistry()
    kwargs_by_key = open_kwargs or {}

    if simulate:
        from benchctrl.sim import factories

        for key in keys:
            registry.register(
                key, factories.factory_for(key), open_kwargs=kwargs_by_key.get(key, {})
            )
        return registry

    openers: dict[str, Callable[..., Any]] = {}

    def _arc(**kw):
        from benchctrl.drivers.otii_arc import OtiiArc

        return OtiiArc.open(**kw)

    def _qr(**kw):
        from benchctrl.drivers.eastwood_qr10x import QR10x
        from benchctrl.transports.autoserial import open_serial_driver

        # Goes through autoserial rather than QR10x.open directly: the bench may
        # or may not have a kernel ch341 driver, and the agent must not need a
        # different config per board. An explicit configured port still wins.
        return open_serial_driver(QR10x.open, **kw)

    def _dl(**kw):
        from benchctrl.drivers.rigol_dl3031a import RigolDL3031A

        return RigolDL3031A.open(**kw)

    def _dp(**kw):
        from benchctrl.drivers.rigol_dp2031 import RigolDP2031

        return RigolDP2031.open(**kw)

    def _dmm(**kw):
        from benchctrl.drivers.siglent_sdm4065a import SiglentSDM4065A

        return SiglentSDM4065A.open(**kw)

    def _pdu(**kw):
        from benchctrl.drivers.cyberpower_pdu41002 import CyberPowerPDU41002

        # Deliberately NOT via transports.autoserial: that exists for the CH340
        # bridge, whose kernel driver may be absent. This PDU's FT232R has a
        # kernel driver, and autoserial's probing would open unrelated ports on
        # a bench where one of them switches mains.
        #
        # No password is injected here. It is resolved inside open() from
        # BENCHCTRL_PDU_PASSWORD in *this agent's* environment, which is the
        # whole point: open_kwargs arrive over the unencrypted RPC wire, so a
        # password routed through here would have crossed the LAN in clear.
        return CyberPowerPDU41002.open(**kw)

    openers = {
        "otii_arc": _arc,
        "eastwood_qr10x": _qr,
        "rigol_dl3031a": _dl,
        "rigol_dp2031": _dp,
        "siglent_sdm4065a": _dmm,
        "cyberpower_pdu41002": _pdu,
    }

    for key in keys:
        opener = openers.get(key)
        if opener is None:
            raise BenchValueError(f"no opener for device key {key!r}")
        registry.register(key, opener, open_kwargs=kwargs_by_key.get(key, {}))
    return registry

"""The local / remote / simulated seam.

Every driver's MCP tool module owns a connection singleton and a
``_get_<device>()`` that opens it on demand. Those functions are the single
place in the codebase that decides *which object* the 226 tools operate on,
which makes them the only place remote mode has to touch.

The contract each ``_get_*`` follows after this change:

    def _get_smu() -> OtiiArc:
        global _smu
        with _lock:
            if _smu is None or not _smu.is_connected:
                _smu = session.resolve("otii_arc", opener=OtiiArc.open)
            return _smu

The module global stays the cache; :py:func:`resolve` is only the populator.
That matters for two reasons: the existing tests inject fakes by assigning
those globals directly and must keep working, and the same hole is how a
``RemoteDevice`` or a simulated instrument gets injected.

Mode is resolved **per device key, not per process**, so a single MCP server
can drive an Arc on a remote bench while talking to a Rigol plugged into the
laptop it's running on.

With nothing configured, :py:func:`resolve` is exactly ``opener()``.
"""

from __future__ import annotations

import logging
import threading
from typing import Any, Callable, Optional

from benchctrl.config import Config, DEVICE_KEYS
from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.session")

_LOCK = threading.RLock()
_CONFIG: Optional[Config] = None
_CLIENTS: dict[str, Any] = {}  # endpoint address -> RemoteClient
_SIM_FACTORIES: dict[str, Callable[..., Any]] = {}


# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------


def configure(config: Optional[Config]) -> None:
    """Install ``config`` as the active binding. ``None`` resets to all-local."""
    global _CONFIG
    with _LOCK:
        _shutdown_clients_locked()
        _CONFIG = config
        if config is not None and not config.is_all_local:
            for key in DEVICE_KEYS:
                mode = config.mode_for(key)
                if mode != "local":
                    log.info("session: %s -> %s", key, mode)


def configure_from_environment(**kwargs) -> Config:
    """Resolve config from CLI/env/file and install it. Returns what was set."""
    from benchctrl import config as _config

    cfg = _config.resolve(**kwargs)
    configure(cfg)
    return cfg


def current_config() -> Config:
    """The active Config, or an empty all-local one."""
    with _LOCK:
        return _CONFIG if _CONFIG is not None else Config()


def mode_for(device_key: str) -> str:
    """``"local"``, ``"remote"``, or ``"sim"`` for ``device_key``."""
    return current_config().mode_for(device_key)


def is_remote(device_key: str) -> bool:
    return mode_for(device_key) == "remote"


# ---------------------------------------------------------------------------
# Simulator registration
# ---------------------------------------------------------------------------


def register_sim_factory(device_key: str, factory: Callable[..., Any]) -> None:
    """Register the callable that builds a simulated ``device_key``.

    Kept as a registry rather than an import so that ``benchctrl.sim`` — a
    test/development dependency — is never imported by the driver or MCP
    layers unless someone actually asks for ``mode="sim"``.
    """
    if device_key not in DEVICE_KEYS:
        raise BenchValueError(f"unknown device key {device_key!r}")
    with _LOCK:
        _SIM_FACTORIES[device_key] = factory


def _default_sim_factory(device_key: str) -> Callable[..., Any]:
    from benchctrl.sim import factories

    return factories.factory_for(device_key)


# ---------------------------------------------------------------------------
# Resolution
# ---------------------------------------------------------------------------


def resolve(
    device_key: str,
    *,
    opener: Callable[..., Any],
    open_kwargs: Optional[dict] = None,
) -> Any:
    """Return a live object for ``device_key`` honouring the active mode.

    Args:
        device_key: one of :py:data:`benchctrl.config.DEVICE_KEYS`.
        opener: how to open the device locally — normally the driver's
            ``open`` classmethod. Called with ``open_kwargs``.
        open_kwargs: arguments forwarded to the opener (or to the agent, for
            a remote device, so the *bench* opens it the same way).

    Returns:
        A real driver instance, a simulated instrument, or a remote proxy.
        All three satisfy the same duck type; the tools cannot tell them
        apart.
    """
    kwargs = dict(open_kwargs or {})
    cfg = current_config()
    mode = cfg.mode_for(device_key)

    if mode == "local":
        return opener(**kwargs)

    if mode == "sim":
        with _LOCK:
            factory = _SIM_FACTORIES.get(device_key)
        if factory is None:
            factory = _default_sim_factory(device_key)
        log.info("session: opening simulated %s", device_key)
        return factory(**kwargs)

    if mode == "remote":
        endpoint = cfg.endpoint_for(device_key)
        client = _client_for(endpoint)
        log.info(
            "session: attaching remote %s via %s:%d",
            device_key,
            endpoint.host,
            endpoint.port,
        )
        return client.attach(device_key, kwargs)

    raise BenchValueError(f"unreachable mode {mode!r} for {device_key!r}")


def _client_for(endpoint) -> Any:
    """One shared client per endpoint address, opened lazily."""
    key = f"{endpoint.host}:{endpoint.port}"
    with _LOCK:
        client = _CLIENTS.get(key)
        if client is not None and client.is_connected:
            return client
        from benchctrl.net.client import RemoteClient

        client = RemoteClient(endpoint)
        client.connect()
        _CLIENTS[key] = client
        return client


def client_for_device(device_key: str) -> Any:
    """The connected client backing ``device_key``.

    Raises:
        BenchValueError: if the device isn't in remote mode.
    """
    cfg = current_config()
    if cfg.mode_for(device_key) != "remote":
        raise BenchValueError(f"{device_key!r} is not in remote mode")
    return _client_for(cfg.endpoint_for(device_key))


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


def shutdown() -> None:
    """Release every remote client.

    The agent treats a clean disconnect as consent to drop the writer claim
    and, if this was the last session holding an armed device, to drive it to
    its safe state. Call this from any process-exit path that can arm
    hardware — see ``benchctrl.mcp.main``.
    """
    with _LOCK:
        _shutdown_clients_locked()


def _shutdown_clients_locked() -> None:
    for key, client in list(_CLIENTS.items()):
        try:
            client.close()
        except Exception as exc:  # pragma: no cover - teardown best effort
            log.warning("session: closing client %s raised: %r", key, exc)
        _CLIENTS.pop(key, None)


def reset_for_tests() -> None:
    """Drop all state. Tests use this so one module can't leak into another."""
    global _CONFIG
    with _LOCK:
        _shutdown_clients_locked()
        _CONFIG = None
        _SIM_FACTORIES.clear()

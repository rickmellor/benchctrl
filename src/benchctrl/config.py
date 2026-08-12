"""Layered configuration for local / remote / simulated device binding.

benchctrl had no configuration mechanism before remote mode: every address
was a function argument or a CLI flag. That stays true — this module is
inert unless something explicitly configures it, and with no config file, no
environment variables, and no CLI flags, every device resolves to ``local``
and behaviour is byte-for-byte what it was.

Precedence, highest first:

1. :py:func:`benchctrl.session.configure` called directly
2. CLI flags (``--remote``, ``--local``, ``--sim``)
3. ``BENCHCTRL_*`` environment variables
4. ``~/.config/benchctrl/config.json``
5. everything local

JSON rather than TOML deliberately: ``tomllib`` is stdlib only from 3.11 and
``pyproject.toml`` declares ``requires-python = ">=3.9"``. JSON is also the
encoder the wire protocol already uses, so this adds no new dependency and
no new parser.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from benchctrl.exceptions import BenchValueError

log = logging.getLogger("benchctrl.config")

#: Canonical device keys — one per driver package. These are the names used
#: in config files, CLI flags, and the remote wire protocol.
DEVICE_KEYS: tuple[str, ...] = (
    "otii_arc",
    "eastwood_qr10x",
    "rigol_dl3031a",
    "rigol_dp2031",
)

MODES: tuple[str, ...] = ("local", "remote", "sim")

DEFAULT_PORT = 9737
DEFAULT_CONFIG_PATH = Path.home() / ".config" / "benchctrl" / "config.json"

#: Advertised over UDP and DNS-SD. Not a registered IANA service type.
SERVICE_TYPE = "_benchctrl._tcp"


@dataclass(frozen=True)
class EndpointConfig:
    """How to reach one benchctrl agent."""

    host: str
    port: int = DEFAULT_PORT
    token: Optional[str] = None
    heartbeat_s: float = 5.0
    deadman_s: float = 15.0
    connect_timeout_s: float = 5.0

    def __post_init__(self) -> None:
        if not self.host:
            raise BenchValueError("endpoint host must not be empty")
        if not (0 < self.port < 65536):
            raise BenchValueError(f"endpoint port out of range: {self.port}")
        if self.deadman_s <= self.heartbeat_s:
            raise BenchValueError(
                f"deadman_s ({self.deadman_s}) must exceed heartbeat_s "
                f"({self.heartbeat_s}) — otherwise a healthy link trips the "
                f"safety governor"
            )

    @property
    def address(self) -> tuple[str, int]:
        return (self.host, self.port)

    def to_dict(self) -> dict:
        d = {
            "host": self.host,
            "port": self.port,
            "heartbeat_s": self.heartbeat_s,
            "deadman_s": self.deadman_s,
            "connect_timeout_s": self.connect_timeout_s,
        }
        if self.token:
            d["token"] = "***"  # never round-trip a secret through to_dict
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> EndpointConfig:
        return cls(
            host=str(d["host"]),
            port=int(d.get("port", DEFAULT_PORT)),
            token=d.get("token"),
            heartbeat_s=float(d.get("heartbeat_s", 5.0)),
            deadman_s=float(d.get("deadman_s", 15.0)),
            connect_timeout_s=float(d.get("connect_timeout_s", 5.0)),
        )


@dataclass(frozen=True)
class DeviceConfig:
    """Where one device lives."""

    mode: str = "local"
    endpoint: Optional[str] = None
    open: dict = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.mode not in MODES:
            raise BenchValueError(
                f"unknown device mode {self.mode!r}; valid: {list(MODES)}"
            )

    def to_dict(self) -> dict:
        d: dict = {"mode": self.mode}
        if self.endpoint:
            d["endpoint"] = self.endpoint
        if self.open:
            d["open"] = dict(self.open)
        return d

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> DeviceConfig:
        return cls(
            mode=str(d.get("mode", "local")),
            endpoint=d.get("endpoint"),
            open=dict(d.get("open", {})),
        )


@dataclass(frozen=True)
class Config:
    """Resolved binding for every device key."""

    endpoints: dict[str, EndpointConfig] = field(default_factory=dict)
    devices: dict[str, DeviceConfig] = field(default_factory=dict)

    def device(self, key: str) -> DeviceConfig:
        """Binding for ``key``, defaulting to local."""
        return self.devices.get(key, DeviceConfig())

    def mode_for(self, key: str) -> str:
        return self.device(key).mode

    def endpoint_for(self, key: str) -> EndpointConfig:
        """Resolve the endpoint a remote device should use.

        Raises:
            BenchValueError: if the device is remote but names no reachable
                endpoint — a misconfiguration worth failing loudly on, since
                the alternative is silently falling back to local and driving
                the wrong hardware.
        """
        dc = self.device(key)
        name = dc.endpoint
        if name is None:
            if len(self.endpoints) == 1:
                return next(iter(self.endpoints.values()))
            raise BenchValueError(
                f"device {key!r} is mode={dc.mode!r} but names no endpoint, "
                f"and there are {len(self.endpoints)} endpoints to choose from"
            )
        try:
            return self.endpoints[name]
        except KeyError:
            raise BenchValueError(
                f"device {key!r} references undefined endpoint {name!r}; "
                f"defined: {sorted(self.endpoints)}"
            ) from None

    @property
    def is_all_local(self) -> bool:
        """True when nothing is configured — the default, untouched path."""
        return all(dc.mode == "local" for dc in self.devices.values())

    def to_dict(self) -> dict:
        return {
            "endpoints": {k: v.to_dict() for k, v in self.endpoints.items()},
            "devices": {k: v.to_dict() for k, v in self.devices.items()},
        }

    @classmethod
    def from_dict(cls, d: Mapping[str, Any]) -> Config:
        endpoints = {
            name: EndpointConfig.from_dict(v)
            for name, v in (d.get("endpoints") or {}).items()
        }
        devices = {}
        for name, v in (d.get("devices") or {}).items():
            if name not in DEVICE_KEYS:
                log.warning(
                    "config: ignoring unknown device key %r (known: %s)",
                    name,
                    ", ".join(DEVICE_KEYS),
                )
                continue
            devices[name] = DeviceConfig.from_dict(v)
        return cls(endpoints=endpoints, devices=devices)


# ---------------------------------------------------------------------------
# Loading
# ---------------------------------------------------------------------------


def load_file(path: Optional[Path] = None) -> Config:
    """Load a config file, or return an all-local Config if absent."""
    p = Path(path) if path else DEFAULT_CONFIG_PATH
    if not p.is_file():
        return Config()
    try:
        raw = json.loads(p.read_text())
    except (OSError, json.JSONDecodeError) as exc:
        raise BenchValueError(f"{p}: could not read config: {exc}") from exc

    mode = p.stat().st_mode & 0o077
    if mode and any(e.token for e in _endpoints_of(raw)):
        log.warning(
            "%s holds a token but is group/world-accessible (mode %o). "
            "Run: chmod 600 %s",
            p,
            p.stat().st_mode & 0o777,
            p,
        )
    return Config.from_dict(raw)


def _endpoints_of(raw: Mapping[str, Any]) -> Iterable[EndpointConfig]:
    for v in (raw.get("endpoints") or {}).values():
        try:
            yield EndpointConfig.from_dict(v)
        except Exception:  # pragma: no cover - warning path only
            continue


def load_env(env: Optional[Mapping[str, str]] = None) -> Optional[Config]:
    """Build a Config from ``BENCHCTRL_*`` variables, or None if unset.

    Recognised:
        BENCHCTRL_REMOTE=host[:port]   bind every device to that agent
        BENCHCTRL_TOKEN=...            shared secret for the handshake
        BENCHCTRL_LOCAL_DEVICES=a,b    force these keys back to local
        BENCHCTRL_SIM_DEVICES=a,b      force these keys to simulated
        BENCHCTRL_CONFIG=/path.json    alternate config file
    """
    env = os.environ if env is None else env
    remote = env.get("BENCHCTRL_REMOTE")
    local_keys = _split_keys(env.get("BENCHCTRL_LOCAL_DEVICES"))
    sim_keys = _split_keys(env.get("BENCHCTRL_SIM_DEVICES"))
    if not remote and not local_keys and not sim_keys:
        return None
    return build(
        remote=remote,
        token=env.get("BENCHCTRL_TOKEN"),
        local_devices=local_keys,
        sim_devices=sim_keys,
    )


def build(
    *,
    remote: Optional[str] = None,
    token: Optional[str] = None,
    local_devices: Iterable[str] = (),
    sim_devices: Iterable[str] = (),
    endpoint_name: str = "bench",
    base: Optional[Config] = None,
) -> Config:
    """Assemble a Config from flag-shaped inputs.

    ``remote`` binds every device key to one agent; ``local_devices`` and
    ``sim_devices`` then carve exceptions out of that. This ordering is what
    makes "Arc on the bench, Rigols on this laptop" expressible in two flags.
    """
    endpoints = dict(base.endpoints) if base else {}
    devices = dict(base.devices) if base else {}

    if remote:
        host, port = parse_address(remote)
        endpoints[endpoint_name] = EndpointConfig(host=host, port=port, token=token)
        for key in DEVICE_KEYS:
            prev = devices.get(key, DeviceConfig())
            devices[key] = replace(prev, mode="remote", endpoint=endpoint_name)
    elif token and endpoints:
        endpoints = {k: replace(v, token=token) for k, v in endpoints.items()}

    for key in _validated(local_devices):
        devices[key] = replace(devices.get(key, DeviceConfig()), mode="local")
    for key in _validated(sim_devices):
        devices[key] = replace(devices.get(key, DeviceConfig()), mode="sim")

    return Config(endpoints=endpoints, devices=devices)


def resolve(
    *,
    cli: Optional[Config] = None,
    env: Optional[Mapping[str, str]] = None,
    path: Optional[Path] = None,
) -> Config:
    """Apply the documented precedence and return the effective Config."""
    environ = os.environ if env is None else env
    cfg_path = path or (
        Path(environ["BENCHCTRL_CONFIG"]) if environ.get("BENCHCTRL_CONFIG") else None
    )
    cfg = load_file(cfg_path)

    from_env = load_env(environ)
    if from_env is not None:
        cfg = _merge(cfg, from_env)
    if cli is not None:
        cfg = _merge(cfg, cli)
    return cfg


def _merge(base: Config, overlay: Config) -> Config:
    endpoints = dict(base.endpoints)
    for name, ep in overlay.endpoints.items():
        if name in endpoints and ep.token is None:
            # Don't let a flag-provided endpoint erase a file-provided token.
            endpoints[name] = replace(ep, token=endpoints[name].token)
        else:
            endpoints[name] = ep
    devices = dict(base.devices)
    devices.update(overlay.devices)
    return Config(endpoints=endpoints, devices=devices)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def parse_address(text: str, default_port: int = DEFAULT_PORT) -> tuple[str, int]:
    """Split ``host``, ``host:port``, or a bracketed IPv6 literal."""
    text = text.strip()
    if not text:
        raise BenchValueError("empty address")
    if text.startswith("["):  # [::1]:9737
        close = text.find("]")
        if close < 0:
            raise BenchValueError(f"malformed IPv6 address: {text!r}")
        host = text[1:close]
        rest = text[close + 1 :]
        if rest.startswith(":"):
            return host, _port(rest[1:])
        return host, default_port
    if text.count(":") == 1:
        host, _, raw = text.partition(":")
        if not host:
            raise BenchValueError(f"missing host in {text!r}")
        return host, _port(raw)
    return text, default_port


def _port(raw: str) -> int:
    try:
        value = int(raw)
    except ValueError:
        raise BenchValueError(f"port must be an integer, got {raw!r}") from None
    if not (0 < value < 65536):
        raise BenchValueError(f"port out of range: {value}")
    return value


def _split_keys(raw: Optional[str]) -> list[str]:
    if not raw:
        return []
    return [part.strip() for part in raw.split(",") if part.strip()]


def _validated(keys: Iterable[str]) -> list[str]:
    out = []
    for key in keys:
        if key not in DEVICE_KEYS:
            raise BenchValueError(
                f"unknown device key {key!r}; valid: {list(DEVICE_KEYS)}"
            )
        out.append(key)
    return out

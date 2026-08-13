"""Method dispatch: an allowlist, never a denylist.

The proxy on the client forwards arbitrary method names. The agent decides
what is actually callable, and it decides by enumerating the driver class at
registration time — so a method that does not exist on the real object can
never be invoked, and neither can anything private.

Two rules earn their keep:

**Public, non-dunder, declared on the class.** Not ``getattr(obj, name)`` on
whatever arrives. That closes ``__class__``, ``__reduce__``, ``__init__``,
and the rest of the introspection surface that turns a method proxy into an
arbitrary-object walk.

**An explicit deny set on top.** ``close`` is governor-mediated (an
unmediated close leaves outputs armed with nothing watching). ``calibrate``
and ``firmware_upgrade`` write to NVM and are refused here as well as in the
driver — defence in depth, because a driver that later implements them
should not silently become remotely callable.

Properties are enumerated separately and read through a dedicated path. That
distinction is load-bearing: ``enable_output`` gates on
``smu.current_limit is None``, and a proxy that returned a bound method for
an unknown attribute would make that check always false and **arm an output
with no current limit set**.
"""

from __future__ import annotations

import inspect
import logging
from dataclasses import dataclass, field
from typing import Any, Optional

from benchctrl.net.errors import PolicyError

log = logging.getLogger("benchctrl.agent.dispatch")

#: Never remotely callable, on any device.
GLOBAL_DENY: frozenset[str] = frozenset(
    {
        "close",  # use agent.close — the governor must observe it
        "open",  # classmethod; use agent.open
        "discover",  # classmethod; use agent.discover
        "calibrate",  # writes NVM
        "firmware_upgrade",  # bricking risk
    }
)

#: Handled by dedicated protocol verbs rather than generic forwarding,
#: because their return values are handles rather than values.
SPECIAL: frozenset[str] = frozenset(
    {"record", "start_recording", "stop_recording", "stream", "read_window"}
)


@dataclass
class DeviceSurface:
    """What one registered device exposes."""

    device_key: str
    class_name: str
    methods: frozenset[str] = field(default_factory=frozenset)
    properties: frozenset[str] = field(default_factory=frozenset)
    special: frozenset[str] = field(default_factory=frozenset)
    denied: frozenset[str] = field(default_factory=frozenset)
    #: Properties cheap enough to snapshot on every response.
    snapshot_props: tuple[str, ...] = ()
    #: Methods that mutate device state — refused without the writer claim.
    mutators: frozenset[str] = field(default_factory=frozenset)

    def to_dict(self) -> dict:
        return {
            "key": self.device_key,
            "cls": self.class_name,
            "methods": sorted(self.methods),
            "properties": sorted(self.properties),
            "special": sorted(self.special),
            "snapshot_props": list(self.snapshot_props),
            "mutators": sorted(self.mutators),
        }


#: Prefixes that identify a state-changing method. Anything matching needs
#: the writer claim; everything else is readable by observers.
_MUTATOR_PREFIXES = (
    "set_",
    "enable_",
    "disable_",
    "write_",
    "start_",
    "stop_",
    "reset",
    "clear_",
    "program_",
    "trigger",
    "apply",
    "commit",
    "abort",
    "incr",
    "decr",
    "take_",
)


def is_mutator(name: str) -> bool:
    return name.startswith(_MUTATOR_PREFIXES)


def introspect(obj: Any, device_key: str) -> DeviceSurface:
    """Enumerate ``obj``'s remotely-usable surface.

    Walks the *class*, not the instance, so attributes assigned at runtime
    cannot widen what is callable.
    """
    cls = type(obj)
    methods: set[str] = set()
    properties: set[str] = set()
    special: set[str] = set()
    denied: set[str] = set()

    for name, attr in inspect.getmembers(cls):
        if name.startswith("_"):
            continue
        if isinstance(attr, property):
            properties.add(name)
            continue
        if isinstance(attr, (classmethod, staticmethod)):
            denied.add(name)
            continue
        if isinstance(attr, type):
            denied.add(name)
            continue
        if not callable(attr):
            # A plain class-level constant; readable like a property.
            properties.add(name)
            continue
        if name in GLOBAL_DENY:
            denied.add(name)
            continue
        if name in SPECIAL:
            special.add(name)
            continue
        methods.add(name)

    snapshot = tuple(sorted(properties))
    return DeviceSurface(
        device_key=device_key,
        class_name=cls.__name__,
        methods=frozenset(methods),
        properties=frozenset(properties),
        special=frozenset(special),
        denied=frozenset(denied),
        snapshot_props=snapshot,
        mutators=frozenset(n for n in methods if is_mutator(n)),
    )


def check_callable(surface: DeviceSurface, method: str) -> None:
    """Raise :py:class:`PolicyError` unless ``method`` may be invoked."""
    if not method or method.startswith("_"):
        raise PolicyError(
            f"{surface.device_key}: refusing private or empty method {method!r}"
        )
    if method in surface.denied or method in GLOBAL_DENY:
        reason = {
            "close": "closing is governor-mediated — use agent.close",
            "open": "use agent.open",
            "discover": "use agent.discover",
            "calibrate": "writes to NVM and is not remotely callable",
            "firmware_upgrade": "bricking risk; not remotely callable",
        }.get(method, "denied by policy")
        raise PolicyError(f"{surface.device_key}.{method}: {reason}")
    if method in surface.special:
        raise PolicyError(
            f"{surface.device_key}.{method} has a dedicated protocol verb and "
            f"cannot be called through generic dispatch"
        )
    if method not in surface.methods:
        raise PolicyError(
            f"{surface.device_key} has no remotely-callable method {method!r}"
        )


def check_readable(surface: DeviceSurface, name: str) -> None:
    if not name or name.startswith("_"):
        raise PolicyError(f"{surface.device_key}: refusing private property {name!r}")
    if name not in surface.properties:
        raise PolicyError(f"{surface.device_key} has no remote property {name!r}")


def check_writer(surface: DeviceSurface, method: str, has_claim: bool) -> None:
    """Refuse mutations from a session that does not hold the writer claim."""
    if has_claim:
        return
    if method in surface.mutators:
        raise PolicyError(
            f"{surface.device_key}.{method} mutates device state and this "
            f"session does not hold the writer claim — call agent.claim first"
        )


def bind(obj: Any, surface: DeviceSurface, method: str):
    """Resolve ``method`` to a bound callable after policy checks pass."""
    check_callable(surface, method)
    fn = getattr(obj, method, None)
    if fn is None or not callable(fn):
        raise PolicyError(
            f"{surface.device_key}.{method} is not callable on the live object"
        )
    return fn


def snapshot_properties(
    obj: Any, surface: DeviceSurface, names: Optional[tuple[str, ...]] = None
) -> dict:
    """Read the cheap properties for piggybacking onto a response.

    A property that raises is reported as None rather than failing the whole
    call: the snapshot is a convenience, and losing it must never turn a
    successful ``set_voltage`` into an error.
    """
    out: dict[str, Any] = {}
    for name in names if names is not None else surface.snapshot_props:
        try:
            out[name] = getattr(obj, name)
        except Exception as exc:  # noqa: BLE001
            log.debug("snapshot: %s.%s raised %r", surface.device_key, name, exc)
            out[name] = None
    return out


def classify(obj: Any, device_key: str = "device") -> dict[str, list[str]]:
    """Every public name, bucketed. Used by the completeness test.

    A new driver method that nobody classified shows up here, which is the
    point: it should fail a test rather than silently become remotely
    callable or silently vanish.
    """
    surface = introspect(obj, device_key)
    return {
        "generic": sorted(surface.methods),
        "properties": sorted(surface.properties),
        "special": sorted(surface.special),
        "denied": sorted(surface.denied),
    }

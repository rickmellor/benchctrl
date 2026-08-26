"""Value codec: Python return values <-> JSON-safe wire graphs.

Driver methods return floats, dicts, dataclasses, enums, bytes, generators
and ``Recording`` objects. JSON carries the first two. Everything else gets
a ``__t`` tag resolved through a **closed allowlist**.

The allowlist is the security boundary. A codec that resolved a dotted class
path from the wire would be remote code execution on the client: an agent —
or anything that can impersonate one — could name
``os.system`` and have the client construct it. Only names in
:py:data:`WIRE_TYPES` are ever instantiated, and only with keyword fields
the target dataclass actually declares.

Large payloads do not travel inline. Anything over
:py:data:`INLINE_BYTES_LIMIT` becomes a blob reference the client fetches on
demand, so a 130 MB recording never lands in a single JSON string.
"""

from __future__ import annotations

import base64
import logging
import math
from dataclasses import fields, is_dataclass
from enum import Enum
from typing import Any, Callable, Optional

from benchctrl.exceptions import BenchProtocolError

log = logging.getLogger("benchctrl.net.codec")

#: bytes larger than this become blob references instead of inline base64
INLINE_BYTES_LIMIT = 64 * 1024

TAG = "__t"


def _wire_types() -> dict[str, type]:
    """Name -> class for every type allowed to cross the wire.

    Lazy so importing the codec doesn't drag in every driver.
    """
    global _WIRE_TYPES
    if _WIRE_TYPES is not None:
        return _WIRE_TYPES

    reg: dict[str, type] = {}

    def add(module_path: str, *names: str) -> None:
        try:
            module = __import__(module_path, fromlist=["*"])
        except ImportError as exc:  # pragma: no cover
            log.debug("codec: %s unavailable (%s)", module_path, exc)
            return
        for name in names:
            obj = getattr(module, name, None)
            if isinstance(obj, type):
                reg[name] = obj

    add("benchctrl.channels", "StandardChannel")
    add("benchctrl.samples", "Statistics", "Sample", "ChannelBuffer")
    add("benchctrl.recording", "ChannelInfoResult")
    add("benchctrl.discovery", "DiscoveredDevice")
    add(
        "benchctrl.drivers.otii_arc.channels",
        "OtiiArcChannel",
        "ChannelInfo",
    )
    add("benchctrl.drivers.otii_arc.device", "OtiiArcInfo")
    add("benchctrl.drivers.otii_arc.transport", "PortInfo")
    add("benchctrl.drivers.otii_arc.protocol", "Response", "SampleRecord")
    add("benchctrl.drivers.eastwood_qr10x.driver", "QR10xInfo")
    add("benchctrl.drivers.rigol_dl3031a.driver", "RigolDLInfo")
    add("benchctrl.drivers.rigol_dp2031.driver", "RigolDP2031Info")
    add("benchctrl.drivers.siglent_sdm4065a.driver", "SDM4065AInfo")
    add(
        "benchctrl.drivers.cyberpower_pdu41002.driver",
        "PDU41002Info",
        "PDU41002Status",
        "OutletConfig",
    )
    add("benchctrl.drivers.ontrak_adu218.driver", "ADU218Info")
    add("benchctrl.battery.profile", "BatteryProfile", "DischargeStep")
    add("benchctrl.battery.emulator", "EmulatorState")

    _WIRE_TYPES = reg
    return reg


_WIRE_TYPES: Optional[dict[str, type]] = None


def wire_type_names() -> list[str]:
    return sorted(_wire_types())


class Encoder:
    """Encodes return values, delegating oversized payloads to a blob store.

    Args:
        store_blob: called with ``bytes``; returns a blob id. When None,
            large payloads raise rather than silently inlining megabytes.
        register_iterator: called with a generator; returns an iterator id.
    """

    def __init__(
        self,
        *,
        store_blob: Optional[Callable[[bytes], str]] = None,
        register_iterator: Optional[Callable[[Any], int]] = None,
        register_recording: Optional[Callable[[Any], str]] = None,
        inline_limit: int = INLINE_BYTES_LIMIT,
    ) -> None:
        self._store_blob = store_blob
        self._register_iterator = register_iterator
        self._register_recording = register_recording
        self._inline_limit = inline_limit

    def encode(self, value: Any, _depth: int = 0) -> Any:
        if _depth > 32:
            raise BenchProtocolError("value nested too deeply to encode")

        if value is None or isinstance(value, (bool, int, str)):
            return value

        if isinstance(value, float):
            # JSON has no NaN/Infinity. read_value can return either.
            if math.isnan(value):
                return {TAG: "f", "v": "nan"}
            if math.isinf(value):
                return {TAG: "f", "v": "inf" if value > 0 else "-inf"}
            return value

        if isinstance(value, (bytes, bytearray, memoryview)):
            raw = bytes(value)
            if len(raw) <= self._inline_limit:
                return {TAG: "b64", "v": base64.b64encode(raw).decode("ascii")}
            if self._store_blob is None:
                raise BenchProtocolError(
                    f"{len(raw)} bytes exceeds the inline limit and no blob "
                    f"store is available"
                )
            return {
                TAG: "blob",
                "id": self._store_blob(raw),
                "len": len(raw),
            }

        if isinstance(value, Enum):
            return {TAG: "enum", "c": type(value).__name__, "v": value.name}

        if isinstance(value, (list, tuple)):
            return [self.encode(v, _depth + 1) for v in value]

        if isinstance(value, (set, frozenset)):
            # frozenset is NOT a subclass of set, so listing only `set` here
            # made every PDU41002 call fail — its `allowed_outlets` property is
            # a frozenset, and the property snapshot rides along on *every*
            # response, so the failure was total rather than confined to the
            # one getter. Sorted by repr for a deterministic wire form; both
            # arrive as a list, since JSON has no set.
            return [self.encode(v, _depth + 1) for v in sorted(value, key=repr)]

        if isinstance(value, dict):
            if all(isinstance(k, str) for k in value):
                return {k: self.encode(v, _depth + 1) for k, v in value.items()}
            # read_window() keys its result by channel object.
            return {
                TAG: "map",
                "items": [
                    [self.encode(k, _depth + 1), self.encode(v, _depth + 1)]
                    for k, v in value.items()
                ],
            }

        # Recording is a live handle, not a value — it may still be filling.
        if type(value).__name__ == "Recording" and self._register_recording:
            rec_id = self._register_recording(value)
            return {
                TAG: "rec",
                "id": rec_id,
                "name": getattr(value, "name", "recording"),
                "running": bool(getattr(value, "is_running", False)),
                "channels": [ch.code for ch in getattr(value, "channels", [])],
            }

        if is_dataclass(value) and not isinstance(value, type):
            name = type(value).__name__
            if name not in _wire_types():
                raise BenchProtocolError(
                    f"dataclass {name!r} is not in the wire-type allowlist"
                )
            return {
                TAG: "dc",
                "c": name,
                "f": {
                    f.name: self.encode(getattr(value, f.name), _depth + 1)
                    for f in fields(value)
                },
            }

        if hasattr(value, "__next__") and self._register_iterator:
            return {TAG: "iter", "id": self._register_iterator(value)}

        raise BenchProtocolError(
            f"cannot encode {type(value).__name__} — add it to the wire-type "
            f"allowlist or give the method a special case"
        )


class Decoder:
    """Rebuilds values, constructing only allowlisted types.

    Args:
        fetch_blob: called with a blob id; returns bytes.
        make_recording / make_iterator: build client-side handles.
    """

    def __init__(
        self,
        *,
        fetch_blob: Optional[Callable[[str, int], bytes]] = None,
        make_recording: Optional[Callable[[dict], Any]] = None,
        make_iterator: Optional[Callable[[dict], Any]] = None,
    ) -> None:
        self._fetch_blob = fetch_blob
        self._make_recording = make_recording
        self._make_iterator = make_iterator

    def decode(self, value: Any, _depth: int = 0) -> Any:
        if _depth > 32:
            raise BenchProtocolError("value nested too deeply to decode")

        if value is None or isinstance(value, (bool, int, float, str)):
            return value

        if isinstance(value, list):
            return [self.decode(v, _depth + 1) for v in value]

        if not isinstance(value, dict):
            return value

        tag = value.get(TAG)
        if tag is None:
            return {k: self.decode(v, _depth + 1) for k, v in value.items()}

        if tag == "f":
            return float(value["v"])

        if tag == "b64":
            return base64.b64decode(value["v"])

        if tag == "blob":
            if self._fetch_blob is None:
                raise BenchProtocolError("blob reference with no fetcher configured")
            return self._fetch_blob(value["id"], int(value.get("len", 0)))

        if tag == "map":
            return {
                self._hashable(self.decode(k, _depth + 1)): self.decode(v, _depth + 1)
                for k, v in value["items"]
            }

        if tag == "enum":
            cls = self._lookup(value["c"])
            try:
                return cls[value["v"]]
            except KeyError:
                raise BenchProtocolError(
                    f"{value['c']} has no member {value['v']!r}"
                ) from None

        if tag == "dc":
            cls = self._lookup(value["c"])
            raw = {k: self.decode(v, _depth + 1) for k, v in value["f"].items()}
            declared = {f.name for f in fields(cls)}
            unknown = set(raw) - declared
            if unknown:
                # Newer agent, older client. Drop rather than fail: the
                # fields we do understand are still useful.
                log.debug("dropping unknown %s fields: %s", value["c"], sorted(unknown))
                raw = {k: v for k, v in raw.items() if k in declared}
            missing = declared - set(raw)
            if missing:
                raise BenchProtocolError(
                    f"{value['c']} is missing required fields: {sorted(missing)}"
                )
            return cls(**raw)

        if tag == "rec":
            if self._make_recording is None:
                raise BenchProtocolError("recording handle with no factory configured")
            return self._make_recording(value)

        if tag == "iter":
            if self._make_iterator is None:
                raise BenchProtocolError("iterator handle with no factory configured")
            return self._make_iterator(value)

        raise BenchProtocolError(f"unknown wire tag {tag!r}")

    @staticmethod
    def _hashable(key: Any) -> Any:
        if isinstance(key, list):
            return tuple(key)
        if isinstance(key, dict):
            return tuple(sorted(key.items()))
        return key

    @staticmethod
    def _lookup(name: str) -> type:
        cls = _wire_types().get(name)
        if cls is None:
            raise BenchProtocolError(
                f"type {name!r} is not in the wire-type allowlist — refusing "
                f"to construct it"
            )
        return cls


def encode_value(value: Any, **kwargs) -> Any:
    """One-shot encode. Convenience for tests and simple call sites."""
    return Encoder(**kwargs).encode(value)


def decode_value(value: Any, **kwargs) -> Any:
    """One-shot decode."""
    return Decoder(**kwargs).decode(value)

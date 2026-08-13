"""Client-side device proxies.

``RemoteDevice`` forwards method calls to the agent. The two details that
matter are both about *not* being clever.

**Unknown attributes raise.** A ``__getattr__`` that returns a callable for
any name is the obvious implementation and it is dangerous here.
``enable_output`` gates on ``smu.current_limit is None``; a bound method is
never None, so the gate would silently pass and **arm an output with no
current limit set**. So the proxy knows exactly which names are methods and
which are properties, and raises ``AttributeError`` for anything else —
which is also what a real object does.

**Property reads are piggybacked, not fetched.** ``_smu_state()`` reads 18
properties and every Arc setter tool calls it. One round trip each would
make a single ``set_voltage`` cost 19, turning a 5 ms operation into 100 ms.
Instead every response carries a property snapshot, cached with a short TTL
and invalidated on write. That is the difference between remote mode feeling
native and feeling broken.
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from typing import Any, Iterator, Optional

from benchctrl.exceptions import BenchValueError
from benchctrl.interfaces import SourceMeasurementUnit  # noqa: F401 (documented intent)

log = logging.getLogger("benchctrl.net.proxy")

#: How long a piggybacked property snapshot stays fresh without a new
#: response. Short enough that a second client's write is noticed quickly,
#: long enough that a burst of reads costs nothing.
PROP_TTL_S = 0.05


class RemoteDevice:
    """A device living on a bench agent."""

    def __init__(self, device_key: str, described: dict, client) -> None:
        self._device_key = device_key
        self._client = client
        self._methods = frozenset(described.get("methods", ()))
        self._properties = frozenset(described.get("properties", ()))
        self._special = frozenset(described.get("special", ()))
        self._class_name = described.get("cls", "RemoteDevice")
        self._props: dict[str, Any] = {}
        self._props_at = 0.0
        self._lock = threading.RLock()

    # --- attribute resolution -------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._properties:
            return self._get_property(name)
        if name in self._methods:
            return _BoundRemoteMethod(self, name)
        # Never fall through to a callable. See the module docstring.
        raise AttributeError(
            f"{self._class_name} on remote device {self._device_key!r} has no "
            f"attribute {name!r}"
        )

    def __dir__(self) -> list[str]:
        return sorted(set(super().__dir__()) | self._methods | self._properties)

    def __repr__(self) -> str:
        return f"<RemoteDevice {self._device_key} ({self._class_name})>"

    # --- property cache -------------------------------------------------

    def _get_property(self, name: str) -> Any:
        with self._lock:
            fresh = (time.monotonic() - self._props_at) < PROP_TTL_S
            if fresh and name in self._props:
                return self._props[name]
        snapshot = self._client.call("device.getprops", {"device": self._device_key})
        self._absorb_props(snapshot, decoded=True)
        with self._lock:
            if name not in self._props:
                raise AttributeError(
                    f"remote {self._device_key} did not report property {name!r}"
                )
            return self._props[name]

    def _absorb_props(self, props: Optional[dict], *, decoded: bool = False) -> None:
        """Cache a property snapshot.

        Snapshots ride on responses in *encoded* form (channel enums, sets,
        dataclasses), so they must go through the codec before use — a
        caller doing ``sorted(c.code for c in smu.enabled_channels)`` needs
        real enum members, not wire dicts.
        """
        if not props:
            return
        if not decoded:
            props = self._client._decoder().decode(props)
        with self._lock:
            self._props.update(props)
            self._props_at = time.monotonic()

    def _invalidate(self) -> None:
        with self._lock:
            self._props_at = 0.0

    # --- calls ----------------------------------------------------------

    def _call(self, method: str, args: tuple, kwargs: dict) -> Any:
        from benchctrl.net.codec import Encoder

        encoder = Encoder()
        result, props = self._client.call_with_props(
            "device.call",
            {
                "device": self._device_key,
                "method": method,
                "args": [encoder.encode(a) for a in args],
                "kwargs": {k: encoder.encode(v) for k, v in kwargs.items()},
                "want_props": True,
            },
        )
        # A mutation invalidates, then the piggybacked snapshot repopulates.
        self._invalidate()
        self._absorb_props(props)
        return result

    # --- explicitly unsupported -----------------------------------------

    def close(self):
        raise BenchValueError(
            f"close() is not proxied for {self._device_key} — closing is "
            f"governor-mediated so an armed output is never orphaned. Use "
            f"client.call('agent.close', ...) or session.shutdown()."
        )


class _BoundRemoteMethod:
    """A callable bound to one remote method."""

    __slots__ = ("_device", "_name")

    def __init__(self, device: RemoteDevice, name: str) -> None:
        self._device = device
        self._name = name

    def __call__(self, *args, **kwargs) -> Any:
        return self._device._call(self._name, args, kwargs)

    def __repr__(self) -> str:
        return f"<remote method {self._device._device_key}.{self._name}>"


class RemoteSMU(RemoteDevice):
    """A remote source-measurement unit.

    Satisfies :py:class:`~benchctrl.interfaces.SourceMeasurementUnit`, so
    ``Emulator``, ``Profiler``, and the scenarios harness accept it
    unchanged — though for anything closed-loop they should run *on* the
    bench instead. A 100 Hz emulator loop across the wire needs two round
    trips per tick, which is 0.6–2.0 s of network per second of wall clock.

    Why the Protocol methods are spelled out below instead of being left to
    ``__getattr__``: since Python 3.12, ``isinstance`` against a
    ``runtime_checkable`` Protocol resolves members with
    ``inspect.getattr_static``, which deliberately does not invoke
    ``__getattr__``. A purely dynamic proxy therefore fails the check no
    matter how complete it is at runtime. Declaring the twelve contract
    methods explicitly is what makes ``isinstance(smu,
    SourceMeasurementUnit)`` true — and it documents the contract besides.
    Everything outside the Protocol stays dynamic.
    """

    # ---- Source-side configuration ---------------------------------

    def set_voltage(self, volts: float) -> None:
        """Set the main output voltage."""
        return self._call("set_voltage", (volts,), {})

    def set_main_current(self, amps: float) -> None:
        """Set the CC-mode source/sink current setpoint."""
        return self._call("set_main_current", (amps,), {})

    def set_output(self, enable: bool) -> None:
        """Enable or disable the main output."""
        return self._call("set_output", (enable,), {})

    def set_range(self, range_: str) -> None:
        """Pick the measurement range."""
        return self._call("set_range", (range_,), {})

    def set_power_regulation(self, mode: str) -> None:
        """Pick the regulation mode."""
        return self._call("set_power_regulation", (mode,), {})

    def set_current_limit(self, amps: float) -> None:
        """Set the over-current trip threshold."""
        return self._call("set_current_limit", (amps,), {})

    def set_current_limit_enabled(self, enable: bool) -> None:
        """Arm or disarm the over-current trip."""
        return self._call("set_current_limit_enabled", (enable,), {})

    # ---- Measurement -----------------------------------------------

    def read_value(self, channel, timeout: float = 1.5) -> float:
        """Block up to ``timeout`` for the next sample on ``channel``."""
        return self._call("read_value", (_channel_code(channel), timeout), {})

    # ---- Identity --------------------------------------------------

    def get_fw_version(self) -> str:
        """Query the device firmware version string."""
        return self._call("get_fw_version", (), {})

    def get_device_id(self) -> str:
        """Query the device serial / identifier."""
        return self._call("get_device_id", (), {})

    def read_window(self, channels, duration_s: float) -> dict:
        """Drain samples and rebuild the caller-keyed result dict.

        Special-cased because the local implementation keys its result by
        *the caller's own channel objects*. Object identity cannot cross a
        wire, so the codes travel and the mapping is rebuilt here against
        the arguments the caller actually passed.
        """
        requested = list(channels)
        codes = [_channel_code(ch) for ch in requested]
        raw = self._client.call(
            "device.read_window",
            {
                "device": self._device_key,
                "channels": codes,
                "duration_s": duration_s,
            },
        )
        by_code = {_channel_code(k): v for k, v in (raw or {}).items()}
        return {
            original: by_code.get(code, [])
            for original, code in zip(requested, codes)
        }

    @contextlib.contextmanager
    def record(self, *channels, name: str = "recording") -> Iterator["RemoteRecording"]:
        """Context-managed remote recording.

        The body runs on the caller exactly as it does locally; only the
        samples stay on the bench until the block exits.
        """
        codes = [_channel_code(ch) for ch in channels]
        started = self._client.call(
            "rec.start",
            {"device": self._device_key, "channels": codes, "name": name},
        )
        rec = RemoteRecording({"id": started["rec_id"], **started}, self._client)
        try:
            yield rec
        finally:
            with contextlib.suppress(Exception):
                rec._finish()

    def start_recording(self, *, name: str = "recording", channels=None):
        codes = [_channel_code(ch) for ch in (channels or ())]
        started = self._client.call(
            "rec.start",
            {"device": self._device_key, "channels": codes, "name": name},
        )
        return RemoteRecording({"id": started["rec_id"], **started}, self._client)

    def stop_recording(self):
        handle = self._client.call(
            "rec.stop", {"device": self._device_key, "rec_id": self._active_rec_id()}
        )
        return RemoteRecording({"id": handle["rec_id"], **handle}, self._client)

    def _active_rec_id(self) -> str:
        status = self._client.call("agent.status")
        for rec in status.get("recordings", []):
            if rec.get("device") == self._device_key and rec.get("running"):
                return rec["rec_id"]
        raise BenchValueError(f"no active recording on {self._device_key}")

    def stream(self, seconds: float = float("inf"), chunk_seconds: float = 0.2):
        opened = self._client.call(
            "iter.open",
            {
                "device": self._device_key,
                "kwargs": {"seconds": seconds, "chunk_seconds": chunk_seconds},
            },
        )
        return RemoteIterator(opened["iter_id"], self._client)


class RemoteRecording:
    """A lazy handle that becomes a real ``Recording`` on first data access.

    Nothing transfers while the recording runs. On stop, the agent encodes
    it as ``.opensmu`` and the client fetches it as a blob — after which
    this object delegates to a genuine ``Recording`` and behaves
    identically, including the load-bearing "still usable after the ``with``
    block" contract that ``sensor_profiler`` depends on.
    """

    #: Members that require the full sample data and therefore materialise.
    _DATA_MEMBERS = frozenset(
        {
            "data", "statistics", "timestamps", "buffer", "channels", "count",
            "info", "index_at", "crop", "downsample", "rename", "to_numpy",
            "to_pandas", "save", "save_csv", "save_json", "save_parquet",
            "plot", "save_to_stream", "to_bytes", "_buffers", "start_time",
            "end_time", "device_info", "offset",
        }
    )

    def __init__(self, described: dict, client) -> None:
        self._rec_id = described.get("id") or described.get("rec_id")
        self._client = client
        self._described = dict(described)
        self._real = None
        self._blob_id = described.get("blob")
        self._running = bool(described.get("running", True))
        self.name = described.get("name", "recording")

    # --- lifecycle ------------------------------------------------------

    def _finish(self) -> dict:
        """Stop on the agent and record where the bytes live."""
        if not self._running:
            return self._described
        stopped = self._client.call("rec.stop", {"rec_id": self._rec_id})
        self._described.update(stopped)
        self._blob_id = stopped.get("blob")
        self._running = False
        return stopped

    def _materialise(self):
        if self._real is not None:
            return self._real
        if self._running:
            raise BenchValueError(
                f"recording {self._rec_id} is still running — exit the record() "
                f"block before reading its data"
            )
        if not self._blob_id:
            raise BenchValueError(f"recording {self._rec_id} produced no data blob")
        from benchctrl.recording import Recording

        data = self._client.fetch_blob(
            self._blob_id, int(self._described.get("bytes", 0))
        )
        self._real = Recording.from_bytes(data)
        log.debug(
            "materialised recording %s (%d bytes)", self._rec_id, len(data)
        )
        return self._real

    # --- live queries (no transfer) --------------------------------------

    @property
    def is_running(self) -> bool:
        return self._running

    @property
    def rec_id(self) -> str:
        return self._rec_id

    def counts(self) -> dict:
        """Per-channel sample counts, computed on the bench."""
        return self._client.call("rec.stats", {"rec_id": self._rec_id})["counts"]

    def live_statistics(self, channel) -> dict:
        """Statistics computed board-side, without transferring samples."""
        return self._client.call(
            "rec.stats",
            {"rec_id": self._rec_id, "channel": _channel_code(channel)},
        )["statistics"]

    def preview(self, channel=None, points: int = 40) -> dict:
        return self._client.call(
            "rec.stats",
            {
                "rec_id": self._rec_id,
                "channel": _channel_code(channel) if channel else None,
                "preview": True,
                "points": points,
            },
        ).get("preview", {})

    # --- delegation ------------------------------------------------------

    def __getattr__(self, name: str) -> Any:
        if name.startswith("_"):
            raise AttributeError(name)
        if name in self._DATA_MEMBERS:
            return getattr(self._materialise(), name)
        real = object.__getattribute__(self, "_real")
        if real is not None:
            return getattr(real, name)
        raise AttributeError(
            f"RemoteRecording has no attribute {name!r} (and it is not a "
            f"recognised Recording member that would trigger a fetch)"
        )

    def __repr__(self) -> str:
        state = "running" if self._running else "stopped"
        return f"<RemoteRecording {self._rec_id} {state}>"


class RemoteIterator:
    """Client end of an agent-side generator."""

    def __init__(self, iter_id: int, client, *, batch: int = 64) -> None:
        self._iter_id = iter_id
        self._client = client
        self._batch = batch
        self._buffer: list = []
        self._exhausted = False
        self._closed = False

    def __iter__(self) -> "RemoteIterator":
        return self

    def __next__(self):
        if self._buffer:
            return self._buffer.pop(0)
        if self._exhausted or self._closed:
            raise StopIteration
        page = self._client.call(
            "iter.next", {"iter_id": self._iter_id, "max": self._batch}
        )
        if page.get("error"):
            self._exhausted = True
            raise BenchValueError(f"remote stream failed: {page['error']}")
        self._buffer = list(page.get("items") or [])
        self._exhausted = bool(page.get("exhausted"))
        if not self._buffer:
            raise StopIteration
        return self._buffer.pop(0)

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with contextlib.suppress(Exception):
            self._client.call("iter.close", {"iter_id": self._iter_id})

    def __del__(self) -> None:  # pragma: no cover - GC timing
        with contextlib.suppress(Exception):
            self.close()


#: Device keys whose proxy should be the SMU flavour.
_SMU_KEYS = {"otii_arc"}


def make_proxy(device_key: str, described: dict, client) -> RemoteDevice:
    """Build the right proxy class for ``device_key``."""
    cls = RemoteSMU if device_key in _SMU_KEYS else RemoteDevice
    return cls(device_key, described, client)


def _channel_code(channel) -> str:
    """Reduce any channel reference to its two-letter code."""
    if channel is None:
        return ""
    code = getattr(channel, "code", None)
    if isinstance(code, str):
        return code
    return str(channel)

"""Client connection to a bench agent.

One socket, one reader thread demultiplexing responses, events, and blob
transfers by correlation id. Calls block the caller until their response
arrives, which keeps the whole synchronous driver API intact — the MCP tools
above this layer never learn that anything moved.
"""

from __future__ import annotations

import itertools
import json
import logging
import socket
import threading
import time
from typing import Any, Callable, Optional

from benchctrl.config import EndpointConfig
from benchctrl.exceptions import (
    BenchConnectionError,
    BenchProtocolError,
    BenchTimeoutError,
)
from benchctrl.net import auth as authmod
from benchctrl.net.codec import Decoder
from benchctrl.net.errors import decode_exception
from benchctrl.net.frames import FrameReader, FrameType, FrameWriter

log = logging.getLogger("benchctrl.net.client")

DEFAULT_CALL_TIMEOUT = 30.0


class _Pending:
    __slots__ = ("event", "result", "error", "props")

    def __init__(self) -> None:
        self.event = threading.Event()
        self.result: Any = None
        self.error: Optional[BaseException] = None
        self.props: Optional[dict] = None


class _BlobTransfer:
    def __init__(self, info: dict) -> None:
        self.info = info
        self.chunks: list[bytes] = []
        self.done = threading.Event()
        self.error: Optional[str] = None

    @property
    def data(self) -> bytes:
        return b"".join(self.chunks)


class RemoteClient:
    """A connected session with one agent."""

    def __init__(self, endpoint: EndpointConfig) -> None:
        self.endpoint = endpoint
        self._sock: Optional[socket.socket] = None
        self._reader: Optional[FrameReader] = None
        self._writer: Optional[FrameWriter] = None
        self._ids = itertools.count(1)
        self._pending: dict[int, _Pending] = {}
        self._blobs: dict[str, _BlobTransfer] = {}
        self._active_blob: Optional[_BlobTransfer] = None
        self._lock = threading.RLock()
        self._rx_thread: Optional[threading.Thread] = None
        self._hb_thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self._connected = False
        self.welcome: dict = {}
        self.events: list[dict] = []
        self.on_event: Optional[Callable[[dict], None]] = None
        self._proxies: dict[str, Any] = {}
        self._claims: set[str] = set()

    # --- connection -----------------------------------------------------

    @property
    def is_connected(self) -> bool:
        return self._connected

    def connect(self) -> RemoteClient:
        if self._connected:
            return self
        ep = self.endpoint
        try:
            sock = socket.create_connection(ep.address, timeout=ep.connect_timeout_s)
        except OSError as exc:
            raise BenchConnectionError(
                f"could not reach agent at {ep.host}:{ep.port}: {exc}"
            ) from exc
        sock.setsockopt(socket.IPPROTO_TCP, socket.TCP_NODELAY, 1)
        self._sock = sock
        self._reader = FrameReader(sock)
        self._writer = FrameWriter(sock)

        try:
            self._handshake()
        except Exception:
            self.close()
            raise

        self._connected = True
        self._stop.clear()
        self._rx_thread = threading.Thread(
            target=self._rx_loop, name="benchctrl-client-rx", daemon=True
        )
        self._rx_thread.start()
        self._hb_thread = threading.Thread(
            target=self._heartbeat_loop, name="benchctrl-client-hb", daemon=True
        )
        self._hb_thread.start()
        log.info("connected to agent at %s:%d", ep.host, ep.port)
        return self

    def _handshake(self) -> None:
        assert self._reader and self._writer
        hello, nonce_c = authmod.build_hello()
        self._writer.send(FrameType.HELLO, _json(hello))

        frame = self._reader.read_frame(timeout=self.endpoint.connect_timeout_s)
        if frame is None:
            raise BenchConnectionError("agent closed the connection during handshake")
        frame_type, payload = frame

        if frame_type == FrameType.ERR:
            raise decode_exception(json.loads(payload).get("e", {}))

        if frame_type == FrameType.CHALLENGE:
            if not self.endpoint.token:
                raise BenchConnectionError(
                    "agent requires authentication but no token is configured — "
                    "set it in ~/.config/benchctrl/config.json or BENCHCTRL_TOKEN"
                )
            challenge = json.loads(payload)
            self._writer.send(
                FrameType.AUTH,
                _json(
                    authmod.build_auth(
                        self.endpoint.token, nonce_c, challenge["nonce_s"]
                    )
                ),
            )
            frame = self._reader.read_frame(timeout=self.endpoint.connect_timeout_s)
            if frame is None:
                raise BenchConnectionError("agent closed the connection after AUTH")
            frame_type, payload = frame
            if frame_type == FrameType.ERR:
                raise decode_exception(json.loads(payload).get("e", {}))

        if frame_type != FrameType.WELCOME:
            raise BenchProtocolError(
                f"expected WELCOME, got frame type 0x{frame_type:02X}"
            )
        self.welcome = json.loads(payload)

    def close(self) -> None:
        self._stop.set()
        self._connected = False
        for thread in (self._rx_thread, self._hb_thread):
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2.0)
        self._rx_thread = self._hb_thread = None
        if self._writer is not None:
            self._writer.close()
        if self._sock is not None:
            try:
                self._sock.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                self._sock.close()
            except OSError:
                pass
        self._sock = None
        self._fail_pending(BenchConnectionError("connection closed"))

    def __enter__(self) -> RemoteClient:
        return self.connect()

    def __exit__(self, *exc_info) -> None:
        self.close()

    # --- background threads ---------------------------------------------

    def _rx_loop(self) -> None:
        try:
            while not self._stop.is_set():
                frame = self._reader.read_frame(timeout=60.0)
                if frame is None:
                    break
                self._on_frame(*frame)
        except Exception as exc:  # noqa: BLE001
            if not self._stop.is_set():
                log.warning("client rx loop ended: %r", exc)
                self._fail_pending(BenchConnectionError(f"connection lost: {exc}"))
        finally:
            self._connected = False

    def _on_frame(self, frame_type: int, payload: bytes) -> None:
        if frame_type == FrameType.RSP:
            body = json.loads(payload)
            self._complete(body["id"], result=body.get("r"), props=body.get("props"))
        elif frame_type == FrameType.ERR:
            body = json.loads(payload)
            self._complete(body["id"], error=decode_exception(body.get("e", {})))
        elif frame_type == FrameType.EVT:
            self._on_event(json.loads(payload))
        elif frame_type == FrameType.BLOB_HDR:
            info = json.loads(payload)
            transfer = _BlobTransfer(info)
            with self._lock:
                self._blobs[info["blob"]] = transfer
                self._active_blob = transfer
        elif frame_type == FrameType.BLOB_CHUNK:
            if self._active_blob is not None:
                self._active_blob.chunks.append(payload)
        elif frame_type == FrameType.BLOB_END:
            body = json.loads(payload)
            with self._lock:
                transfer = self._blobs.get(body.get("blob", ""))
                self._active_blob = None
            if transfer is not None:
                if not body.get("ok", False):
                    transfer.error = body.get("error", "transfer failed")
                transfer.done.set()
        elif frame_type == FrameType.PONG:
            pass

    def _on_event(self, event: dict) -> None:
        self.events.append(event)
        if len(self.events) > 1000:
            del self.events[:-500]
        if event.get("severity") in ("critical", "alarm"):
            log.warning("agent event: %s", event)
        if self.on_event is not None:
            try:
                self.on_event(event)
            except Exception:  # noqa: BLE001
                log.exception("client event handler raised")

    def _heartbeat_loop(self) -> None:
        interval = max(1.0, self.endpoint.heartbeat_s)
        while not self._stop.wait(interval):
            try:
                if self._writer is not None and not self._writer.is_closed:
                    self._writer.send(FrameType.PING)
            except Exception:  # noqa: BLE001
                return

    def _complete(self, req_id, *, result=None, error=None, props=None) -> None:
        with self._lock:
            pending = self._pending.pop(req_id, None)
        if pending is None:
            return
        pending.result = result
        pending.error = error
        pending.props = props
        pending.event.set()

    def _fail_pending(self, error: BaseException) -> None:
        with self._lock:
            pending, self._pending = list(self._pending.values()), {}
        for item in pending:
            item.error = error
            item.event.set()

    # --- calls ----------------------------------------------------------

    def call(
        self, method: str, params: Optional[dict] = None, *, timeout: float = DEFAULT_CALL_TIMEOUT
    ) -> Any:
        """Issue a request and return its decoded result."""
        result, _ = self.call_with_props(method, params, timeout=timeout)
        return result

    def call_with_props(
        self, method: str, params: Optional[dict] = None, *, timeout: float = DEFAULT_CALL_TIMEOUT
    ) -> tuple[Any, Optional[dict]]:
        if not self._connected or self._writer is None:
            raise BenchConnectionError("not connected to an agent")
        req_id = next(self._ids)
        pending = _Pending()
        with self._lock:
            self._pending[req_id] = pending
        self._writer.send(
            FrameType.REQ, _json({"id": req_id, "m": method, "p": params or {}})
        )
        if not pending.event.wait(timeout=timeout):
            with self._lock:
                self._pending.pop(req_id, None)
            raise BenchTimeoutError(
                f"no response to {method!r} within {timeout:.1f}s"
            )
        if pending.error is not None:
            raise pending.error
        return self._decoder().decode(pending.result), pending.props

    def _decoder(self) -> Decoder:
        from benchctrl.net.proxy import RemoteIterator, RemoteRecording

        return Decoder(
            fetch_blob=self.fetch_blob,
            make_recording=lambda d: RemoteRecording(d, self),
            make_iterator=lambda d: RemoteIterator(d["id"], self),
        )

    # --- blobs ----------------------------------------------------------

    def fetch_blob(self, blob_id: str, expected_len: int = 0, *, timeout: float = 120.0) -> bytes:
        """Pull a blob, verifying its digest."""
        import hashlib

        self.call("blob.fetch", {"blob": blob_id}, timeout=timeout)
        with self._lock:
            transfer = self._blobs.get(blob_id)
        if transfer is None:
            raise BenchProtocolError(f"agent sent no data for blob {blob_id!r}")
        if not transfer.done.wait(timeout=timeout):
            raise BenchTimeoutError(f"blob {blob_id!r} transfer did not complete")
        if transfer.error:
            raise BenchProtocolError(f"blob {blob_id!r} failed: {transfer.error}")

        data = transfer.data
        with self._lock:
            self._blobs.pop(blob_id, None)

        declared = transfer.info.get("len")
        if declared is not None and len(data) != declared:
            raise BenchProtocolError(
                f"blob {blob_id!r} truncated: got {len(data)} of {declared} bytes"
            )
        digest = transfer.info.get("sha256")
        if digest and hashlib.sha256(data).hexdigest() != digest:
            raise BenchProtocolError(f"blob {blob_id!r} failed its checksum")
        return data

    # --- devices --------------------------------------------------------

    def attach(self, device_key: str, open_kwargs: Optional[dict] = None) -> Any:
        """Open ``device_key`` on the bench and return a proxy for it."""
        from benchctrl.net.proxy import make_proxy

        with self._lock:
            existing = self._proxies.get(device_key)
        if existing is not None:
            return existing

        described = self.call(
            "agent.open", {"device": device_key, "open": open_kwargs or {}}
        )
        self.call("agent.claim", {"device": device_key})
        self._claims.add(device_key)
        proxy = make_proxy(device_key, described, self)
        with self._lock:
            self._proxies[device_key] = proxy
        return proxy

    def detach(self, device_key: str) -> None:
        with self._lock:
            self._proxies.pop(device_key, None)
        if device_key in self._claims:
            try:
                self.call("agent.release", {"device": device_key})
            except Exception:  # noqa: BLE001
                pass
            self._claims.discard(device_key)

    def device_names(self) -> list[str]:
        return [d["key"] for d in self.welcome.get("devices", [])]

    def discover(self) -> dict:
        return self.call("agent.discover")

    def status(self) -> dict:
        return self.call("agent.status")


def _json(payload: dict) -> bytes:
    return json.dumps(payload, separators=(",", ":")).encode("utf-8")

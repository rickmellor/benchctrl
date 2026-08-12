"""A simulated Otii Arc speaking the real binary wire protocol.

This is not a mock of :py:class:`~benchctrl.drivers.otii_arc.device.OtiiArc`.
It is a simulated *device*: it consumes framed command payloads and produces
framed responses and sample streams, byte for byte, using the same
:py:mod:`benchctrl.drivers.otii_arc.protocol` encoders the driver decodes
with. The real driver connects to it over a real pty and cannot tell the
difference short of reading a serial number.

What it models
--------------
- the three-step session-init handshake (and refuses to stream until it runs)
- SET parameter commands, with per-parameter range validation that emits a
  genuine negative-status error frame — so ``BenchCommandError`` and its
  ``error_code`` / ``last_good_value`` attributes can be tested for real
- GET parameter readbacks, including the string identity parameters and the
  268-byte channel inventory blob
- baseline streaming (12-byte records, slow) before a recording starts
- packed high-rate streaming (``69 83 2a ff`` frames, sub-1 and sub-4
  records) once channels are enabled for recording
- a resistive DUT, so enabling the output makes current appear

What it deliberately does not model
-----------------------------------
Calibration tables, firmware upgrade, and the vendor's undocumented banked
parameter space. Those are ``BenchNotImplementedError`` in the driver anyway.
"""

from __future__ import annotations

import logging
import struct
import threading
from typing import Callable, Optional

from benchctrl.drivers.otii_arc import protocol as p
from benchctrl.drivers.otii_arc.channels import OtiiArcChannel
from benchctrl.sim.base import SimDevice
from benchctrl.sim.loopback import SerialLoopback
from benchctrl.sim.waveforms import Constant, OhmicLoad, Waveform

log = logging.getLogger("benchctrl.sim.otii_arc")

#: Response frame header, shared by acks, errors, and GET replies.
RESP_PREFIX = b"\x0e\x03\x99\xff"

#: Device-side rejection codes, matching the real firmware's vocabulary.
STATUS_OK = 0
STATUS_NOT_AVAILABLE = -3
STATUS_OUT_OF_RANGE = -101

#: Inclusive (min, max) in wire units for the SET parameters we validate.
#: Chosen to match the Arc Pro's documented envelope; the driver converts to
#: these units before sending (microvolts, milliamps, microamps, micro-ohms).
SET_RANGES: dict[int, tuple[int, int]] = {
    p.CMD_SET_MAIN_VOLTAGE: (0, 5_000_000),        # 0 .. 5 V in uV
    p.CMD_SET_OC_PROTECTION: (0, 5_000),           # 0 .. 5 A in mA
    p.CMD_SET_MAIN_CURRENT: (0, 5_000_000),        # 0 .. 5 A in uA
    p.CMD_SET_DIGITAL_VOLTAGE: (1_200_000, 5_000_000),
    p.CMD_SET_ADC_RESISTOR: (0, 1_000_000_000),
    p.CMD_SET_RANGE: (0, 1),
    p.CMD_SET_MAIN_OUTPUT: (0, 1),
    p.CMD_SET_4WIRE: (0, 1),
    p.CMD_SET_SRC_CUR_LIMIT_ENABLED: (0, 1),
    p.CMD_SET_UART_ENABLE: (0, 1),
}

#: Identity strings returned by the GET parameters the driver asks for.
DEFAULT_IDENTITY = {
    p.CMD_GET_DEVICE_NAME: "Arc",
    p.CMD_GET_HW_VERSION: "1.3",
    p.CMD_GET_FW_VERSION: "3.1.3",
    p.CMD_GET_DEVICE_ID: "SIM0000000000000000000000000ARC1",
}


class SimulatedOtiiArc(SimDevice):
    """An Arc Pro that lives in a pty instead of on the bench.

    Args:
        channels: waveform per channel code. Any channel without an explicit
            waveform reports 0.0. ``"mc"`` defaults to a resistive load
            driven by the commanded voltage, so the device behaves like
            something is actually attached.
        load_ohm: resistance of the default DUT on the main output.
        baseline_hz: baseline (non-recording) sample emission rate.
        packed_frame_hz: packed-frame rate during a recording. The real
            device emits at the slowest enabled channel's rate (1 kHz);
            tests usually want something gentler.
        require_handshake: if True (default), no samples stream until the
            session-init sequence has been seen — which is what makes the
            handshake actually under test.
    """

    def __init__(
        self,
        *,
        loopback: Optional[SerialLoopback] = None,
        channels: Optional[dict[str, Waveform]] = None,
        load_ohm: float = 330.0,
        baseline_hz: float = 6.0,
        packed_frame_hz: float = 200.0,
        require_handshake: bool = True,
        identity: Optional[dict[int, str]] = None,
        free_run: bool = True,
        tick_hz: float = 400.0,
    ) -> None:
        super().__init__(loopback=loopback, tick_hz=tick_hz, free_run=free_run)
        self._lock = threading.RLock()
        self._rx = bytearray()

        self.load = OhmicLoad(load_ohm)
        self.waveforms: dict[str, Waveform] = {"mc": self.load}
        if channels:
            self.waveforms.update(channels)

        self.baseline_hz = baseline_hz
        self.packed_frame_hz = packed_frame_hz
        self.require_handshake = require_handshake
        self.identity = dict(DEFAULT_IDENTITY)
        if identity:
            self.identity.update(identity)

        # --- device state, in wire units ---
        self.params: dict[int, int] = {
            p.CMD_SET_MAIN_VOLTAGE: 0,
            p.CMD_SET_MAIN_CURRENT: 0,
            p.CMD_SET_OC_PROTECTION: 0,
            p.CMD_SET_MAIN_OUTPUT: 0,
            p.CMD_SET_RANGE: 0,
            p.CMD_SET_4WIRE: 0,
            p.CMD_SET_SRC_CUR_LIMIT_ENABLED: 0,
            p.CMD_SET_POWER_REGULATION: p.POWER_REGULATION_VOLTAGE,
            p.CMD_SET_ADC_RESISTOR: 0,
            p.CMD_SET_UART_ENABLE: 0,
            p.CMD_SET_UART_BAUDRATE: 115200,
            p.CMD_SET_DIGITAL_VOLTAGE: 3_300_000,
            p.CMD_ENABLE_5V: 0,
            p.CMD_SET_GPO: 0,
        }

        self.handshake_seen = False
        self.recording_channels: dict[int, OtiiArcChannel] = {}
        self.uart_tx: list[str] = []
        self.command_log: list[tuple[str, int, int]] = []
        self.rejected: list[tuple[int, int]] = []

        self._packed_seq = 0
        self._next_baseline = 0.0
        self._next_packed = 0.0
        #: Injected fault: when set, the next SET is rejected with this code.
        self.force_next_set_error: Optional[int] = None
        #: Test hook invoked as ``hook(kind, cmd, value)`` for every command.
        self.on_command: Optional[Callable[[str, int, int], None]] = None

    # --- state helpers --------------------------------------------------

    @property
    def output_enabled(self) -> bool:
        return bool(self.params[p.CMD_SET_MAIN_OUTPUT])

    @property
    def voltage_v(self) -> float:
        return self.params[p.CMD_SET_MAIN_VOLTAGE] / 1e6

    @property
    def is_recording(self) -> bool:
        return bool(self.recording_channels)

    def _channel_value(self, ch: OtiiArcChannel, t: float) -> float:
        wf = self.waveforms.get(ch.code)
        if wf is None:
            if ch.code == "mv":
                return self.voltage_v if self.output_enabled else 0.0
            if ch.code == "mp":
                return self._channel_value(OtiiArcChannel.MAIN_CURRENT, t) * (
                    self.voltage_v if self.output_enabled else 0.0
                )
            if ch.code == "tp":
                return 24.5
            return 0.0
        if not self.output_enabled and ch.code in ("mc", "mp"):
            return 0.0
        return wf.value(t)

    # --- inbound --------------------------------------------------------

    def on_frame_bytes(self, data: bytes) -> None:
        with self._lock:
            self._rx.extend(data)
            consumed = 0
            for fr in p.iter_frames(bytes(self._rx)):
                self._handle_payload(fr.payload)
                consumed = fr.offset + 8 + len(fr.payload)
            if consumed:
                del self._rx[:consumed]

    def _handle_payload(self, payload: bytes) -> None:
        if len(payload) < 8:
            return
        seq, type_word = struct.unpack_from("<II", payload, 0)

        # The 8-byte wake payload is [seq=1][type=0x14].
        if type_word == p.TYPE_INIT_WAKE and len(payload) == 8:
            self.handshake_seen = True
            self._log("wake", 0, 0)
            return

        if len(payload) >= 16:
            cmd, value = struct.unpack_from("<II", payload, 8)
        else:
            cmd, value = 0, 0

        if type_word == p.TYPE_SET_PARAMETER:
            self._handle_set(seq, cmd, value)
        elif type_word == p.TYPE_GET_PARAMETER:
            self._handle_get(seq, cmd)
        elif type_word == p.TYPE_CHANNEL_ENABLE:
            self._handle_channel_enable(cmd, bool(value))
        elif type_word == p.TYPE_PREPARE_STOP:
            self._log("prepare_stop", 0, 0)
        elif type_word == p.TYPE_REC_CLEANUP and len(payload) == 8:
            self._log("rec_cleanup", 0, 0)
        elif type_word == p.TYPE_ENABLE_LEGACY_SINK:
            self.params[p.CMD_ENABLE_LEGACY_SINK] = value
            self._log("set", p.CMD_ENABLE_LEGACY_SINK, value)
        elif type_word == p.TYPE_WRITE_TEXT:
            text = payload[12:].decode("utf-8", errors="replace")
            self.uart_tx.append(text)
            self._log("write_tx", 0, len(text))
        elif type_word == p.TYPE_POLL:
            pass
        else:
            log.debug("sim: unhandled type 0x%02X seq=%d", type_word, seq)

    def _log(self, kind: str, cmd: int, value: int) -> None:
        self.command_log.append((kind, cmd, value))
        if self.on_command is not None:
            self.on_command(kind, cmd, value)

    def _handle_set(self, seq: int, cmd: int, value: int) -> None:
        forced = self.force_next_set_error
        if forced is not None:
            self.force_next_set_error = None
            self.rejected.append((cmd, value))
            self._log("set_rejected", cmd, value)
            self.send(self._error_frame(seq, forced, self.params.get(cmd, 0)))
            return

        lo_hi = SET_RANGES.get(cmd)
        if lo_hi is not None:
            lo, hi = lo_hi
            signed = struct.unpack("<i", struct.pack("<I", value))[0]
            candidate = value if value <= 0x7FFFFFFF else signed
            if not (lo <= candidate <= hi):
                self.rejected.append((cmd, value))
                self._log("set_rejected", cmd, value)
                self.send(
                    self._error_frame(seq, STATUS_OUT_OF_RANGE, self.params.get(cmd, 0))
                )
                return

        self.params[cmd] = value
        self._log("set", cmd, value)
        if cmd == p.CMD_SET_MAIN_VOLTAGE:
            self.load.set_voltage(self.voltage_v)
        # The real device acks a successful SET with a 16-byte status-0
        # response. The driver doesn't block on it, but emitting it keeps the
        # inbound stream shaped like the real thing.
        self.send(self._ack_frame(seq, self.params.get(cmd, 0)))

    def _handle_get(self, seq: int, cmd: int) -> None:
        if cmd in self.identity:
            data = self.identity[cmd].encode("utf-8")
            if cmd == p.CMD_GET_DEVICE_ID:
                data = data.ljust(32, b"\x00")[:32]
            self.send(self._response_frame(seq, STATUS_OK, data))
            self._log("get", cmd, 0)
            return

        if cmd == p.CMD_GET_CHANNEL_INVENTORY:
            self.send(self._response_frame(seq, STATUS_OK, self._channel_inventory()))
            self._log("get", cmd, 0)
            return

        if cmd in self.params:
            self.send(
                self._response_frame(seq, STATUS_OK, struct.pack("<I", self.params[cmd]))
            )
            self._log("get", cmd, self.params[cmd])
            return

        self.send(self._response_frame(seq, STATUS_NOT_AVAILABLE, b""))
        self._log("get_na", cmd, 0)

    def _handle_channel_enable(self, wire_id: int, enable: bool) -> None:
        # Init step 3 enables the UART-log channel; that is the handshake's
        # final step, not a recording request.
        if wire_id == OtiiArcChannel.UART_RX.wire_id:
            self.handshake_seen = True
            self._log("init_step3", wire_id, int(enable))
            return
        ch = _WIRE_ID_TO_CHANNEL.get(wire_id)
        if ch is None:
            return
        if enable:
            self.recording_channels[wire_id] = ch
        else:
            self.recording_channels.pop(wire_id, None)
        self._log("channel_enable" if enable else "channel_disable", wire_id, int(enable))

    # --- frame builders -------------------------------------------------

    def _response_frame(self, seq: int, status: int, data: bytes) -> bytes:
        payload = RESP_PREFIX + struct.pack("<Ii", seq, status) + data
        return p.encode_frame(payload)

    def _ack_frame(self, seq: int, value: int) -> bytes:
        # 16-byte success response: [prefix][seq][status=0][value]
        return self._response_frame(seq, STATUS_OK, struct.pack("<I", value))

    def _error_frame(self, seq: int, code: int, last_good: int) -> bytes:
        # parse_error_frame() requires exactly 16 bytes with status < 0.
        return self._response_frame(seq, code, struct.pack("<I", last_good))

    def _channel_inventory(self) -> bytes:
        """The 268-byte inventory blob ``CMD_GET_CHANNEL_INVENTORY`` returns."""
        out = bytearray()
        for ch in OtiiArcChannel:
            out += struct.pack(
                "<HHI", ch.wire_id, ch.subtype, ch.sample_rate
            )
        return bytes(out.ljust(268, b"\x00")[:268])

    # --- outbound streaming ---------------------------------------------

    def on_tick(self, elapsed_s: float) -> None:
        if self.require_handshake and not self.handshake_seen:
            return
        with self._lock:
            if self.is_recording:
                self._maybe_emit_packed(elapsed_s)
            else:
                self._maybe_emit_baseline(elapsed_s)

    def _maybe_emit_baseline(self, t: float) -> None:
        if self.baseline_hz <= 0 or t < self._next_baseline:
            return
        self._next_baseline = t + 1.0 / self.baseline_hz
        self.emit_baseline(t)

    def _maybe_emit_packed(self, t: float) -> None:
        if self.packed_frame_hz <= 0 or t < self._next_packed:
            return
        self._next_packed = t + 1.0 / self.packed_frame_hz
        self.emit_packed(t)

    def emit_baseline(self, t: Optional[float] = None) -> None:
        """Emit one baseline sample record per always-on channel."""
        t = self.elapsed_s if t is None else t
        payload = bytearray()
        for ch in (
            OtiiArcChannel.MAIN_CURRENT,
            OtiiArcChannel.MAIN_VOLTAGE,
            OtiiArcChannel.MAIN_POWER,
            OtiiArcChannel.TEMPERATURE,
        ):
            payload += p.SAMPLE_RECORD_HEADER + struct.pack(
                "<If", ch.wire_id, self._channel_value(ch, t)
            )
        self.send(p.encode_frame(bytes(payload)))

    def emit_packed(self, t: Optional[float] = None) -> None:
        """Emit one packed frame carrying every channel enabled for recording.

        sub-4 channels carry four samples spaced at their native rate, which
        is what makes ``mc`` arrive at 4x the frame rate exactly as it does
        on real hardware.
        """
        t = self.elapsed_s if t is None else t
        self._packed_seq = (self._packed_seq + 1) & 0xFFFFFFFF
        payload = bytearray(p.PACKED_FRAME_MAGIC + struct.pack("<I", self._packed_seq))
        for wire_id, ch in sorted(self.recording_channels.items()):
            if ch.subtype == 4:
                dt = 1.0 / ch.sample_rate if ch.sample_rate else 0.0
                vals = [self._channel_value(ch, t + k * dt) for k in range(4)]
                payload += struct.pack(
                    "<HHI4f", ch.wire_id, 4, ch.sample_rate, *vals
                )
            else:
                payload += struct.pack(
                    "<HHIf", ch.wire_id, 1, ch.sample_rate, self._channel_value(ch, t)
                )
        payload += p.PACKED_FRAME_SENTINEL
        self.send(p.encode_frame(bytes(payload)))

    # --- fault injection ------------------------------------------------

    def inject_error(self, code: int = STATUS_OUT_OF_RANGE) -> None:
        """Make the next SET command fail with ``code``.

        Lets a test drive the real ``BenchCommandError`` path — including the
        asynchronous delivery through the reader thread — without needing a
        device that actually rejects something.
        """
        self.force_next_set_error = code

    def emit_raw(self, data: bytes) -> None:
        """Write arbitrary bytes, framed or not.

        For testing resync: garbage between frames must be skipped silently
        by ``iter_frames``.
        """
        self.send(data)


_WIRE_ID_TO_CHANNEL: dict[int, OtiiArcChannel] = {}
for _ch in OtiiArcChannel:
    _WIRE_ID_TO_CHANNEL.setdefault(_ch.wire_id, _ch)

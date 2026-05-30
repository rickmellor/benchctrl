"""SMU — the public device class.

Wraps a single Arc/Arc Pro for the lifetime of a connection. Owns:

- a :class:`Transport` (the serial port)
- a sequence counter for outbound SET commands
- the host-side state cache (last-known values for setpoints)
- the currently enabled channel set
- an optional background reader thread that drains samples into an
  active :class:`Recording`
"""

from __future__ import annotations

import contextlib
import logging
import threading
import time
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from typing import Optional, Union

from benchctrl.channels import WIRE_ID_TO_CHANNEL, Channel
from benchctrl.exceptions import (
    BenchCommandError,
    BenchConnectionError,
    BenchNotImplementedError,
    BenchTimeoutError,
    BenchValueError,
)
from benchctrl.protocol import (
    CMD_ENABLE_5V,
    CMD_ENABLE_LEGACY_SINK,
    CMD_GET_CHANNEL_INVENTORY,
    CMD_GET_DEVICE_ID,
    CMD_GET_DEVICE_NAME,
    CMD_GET_FW_VERSION,
    CMD_GET_HW_VERSION,
    CMD_SET_4WIRE,
    CMD_SET_ADC_RESISTOR,
    CMD_SET_DIGITAL_VOLTAGE,
    CMD_SET_GPO,
    CMD_SET_MAIN_CURRENT,
    CMD_SET_MAIN_OUTPUT,
    CMD_SET_MAIN_VOLTAGE,
    CMD_SET_OC_PROTECTION,
    CMD_SET_POWER_REGULATION,
    CMD_SET_RANGE,
    CMD_SET_SRC_CUR_LIMIT_ENABLED,
    CMD_SET_UART_BAUDRATE,
    CMD_SET_UART_ENABLE,
    POWER_REGULATION_MAP,
    RANGE_HIGH,
    RANGE_LOW,
    SESSION_INIT_WAKE,
    Response,
    amps_to_microamps,
    amps_to_milliamps,
    encode_channel_enable_for_recording,
    encode_frame,
    encode_get_command,
    encode_gpo,
    encode_prepare_stop,
    encode_recording_cleanup,
    encode_session_init_step2,
    encode_session_init_step3,
    encode_set_command,
    encode_write_tx,
    find_text_metadata,
    iter_frames,
    iter_samples,
    ohms_to_microohms,
    parse_error_frame,
    parse_response,
    power_regulation_value,
    volts_to_microvolts,
)
from benchctrl.recording import Recording
from benchctrl.samples import Sample
from benchctrl.transport import PortInfo, Transport, discover_arc_ports

log = logging.getLogger("benchctrl.device")

ChannelLike = Union[Channel, str]
PowerRegulation = str  # "voltage" | "current" | "inline" | "off"

VALID_POWER_REGULATIONS = ("voltage", "current", "inline", "off")


@dataclass(frozen=True)
class SMUInfo:
    """Discovery-time descriptor for a connected device."""

    port: str
    name: str
    serial: Optional[str]
    description: str

    @classmethod
    def from_port(cls, p: PortInfo) -> SMUInfo:
        return cls(
            port=p.device,
            name=p.name,
            serial=p.serial_number,
            description=p.description,
        )


@dataclass
class _State:
    """Host-side cached state. Only values we have written.

    The Arc has no general-purpose query-current-value command on the wire;
    the device only streams measurements. So cached writes are the source of
    truth for "what did I last set", matching the official client.
    """

    voltage: Optional[float] = None  # V
    main_current: Optional[float] = None  # A (CC mode)
    current_limit: Optional[float] = None  # A (OC protection / max current)
    exp_voltage: Optional[float] = None  # V
    adc_resistor: Optional[float] = None  # Ohm
    uart_baudrate: Optional[int] = None
    uart_enabled: Optional[bool] = None
    output_enabled: Optional[bool] = None
    four_wire_enabled: Optional[bool] = None
    current_limit_enabled: Optional[bool] = None
    range: Optional[str] = None  # "low" | "high"
    exp_5v_enabled: Optional[bool] = None
    legacy_sink_enabled: Optional[bool] = None
    supply_mode: str = "power-box"
    power_regulation: Optional[str] = None
    gpo: dict[int, bool] = field(default_factory=dict)
    # Last seen real-time samples per channel (wire id keyed).
    last_value: dict[Channel, float] = field(default_factory=dict)


class SMU:
    """A connected source-measurement unit."""

    # ----- construction / discovery -------------------------------------

    def __init__(self, transport: Transport, *, info: Optional[SMUInfo] = None):
        self._transport = transport
        self._info = info
        self._seq = 0x1000
        self._state = _State()
        self._enabled_channels: set[Channel] = set()
        self._active_recording: Optional[Recording] = None
        self._active_recording_wire_ids: list[int] = []
        # Background reader for active recording
        self._reader_thread: Optional[threading.Thread] = None
        self._reader_stop = threading.Event()
        self._reader_buffer = bytearray()
        # Track sample arrival times for timestamp synthesis. We use the
        # device's native sample rates rather than wall-clock because the
        # device streams steadily; jitter is small relative to the official
        # client's model.
        self._reader_sample_counts: dict[Channel, int] = {}
        # Asynchronous errors detected during streaming, raised on next call.
        self._pending_error: Optional[BenchCommandError] = None
        self._pending_error_lock = threading.Lock()
        # Raw bytes buffer for read_raw() — only populated when no recording
        # is active (otherwise the reader thread owns the bytes).
        self._raw_window_buffer = bytearray()

    @classmethod
    def discover(cls) -> list[SMUInfo]:
        """List every connected Arc/Arc Pro."""
        return [SMUInfo.from_port(p) for p in discover_arc_ports()]

    @classmethod
    def open(
        cls,
        port: Optional[Union[str, SMUInfo, PortInfo]] = None,
        *,
        baudrate: int = 9600,
    ) -> SMU:
        """Open a connection. If `port` is None, auto-discovers the first device."""
        info: Optional[SMUInfo] = None
        port_name: Optional[str] = None
        if port is None:
            discovered = cls.discover()
            if not discovered:
                raise BenchConnectionError(
                    "no Arc devices found (looking for VID=0x0FCE PID=0xD1E6)"
                )
            info = discovered[0]
            port_name = info.port
        elif isinstance(port, SMUInfo):
            info = port
            port_name = port.port
        elif isinstance(port, PortInfo):
            info = SMUInfo.from_port(port)
            port_name = port.device
        elif isinstance(port, str):
            port_name = port
        else:
            raise BenchValueError(f"unsupported port argument: {type(port).__name__}")

        transport = Transport(port_name, baudrate=baudrate)
        smu = cls(transport, info=info)
        smu._connect()
        return smu

    # ----- internals ----------------------------------------------------

    def _next_seq(self) -> int:
        s = self._seq
        self._seq = (self._seq + 1) & 0xFFFFFFFF
        return s

    def _send_payload(self, payload: bytes) -> int:
        return self._transport.write(encode_frame(payload))

    def _send_set(self, cmd_code: int, value: int) -> None:
        """Send a SET-parameter command, checking for any pending async error first."""
        self._raise_pending_error()
        payload = encode_set_command(self._next_seq(), cmd_code, value)
        log.debug("SET cmd=0x%02X value=%d", cmd_code, value)
        self._send_payload(payload)

    def _raise_pending_error(self) -> None:
        with self._pending_error_lock:
            if self._pending_error is not None:
                err = self._pending_error
                self._pending_error = None
                raise err

    def _connect(self) -> None:
        """Open the transport and run the session-init handshake."""
        self._transport.open()
        # The device requires these three OUTs on a cold session before it
        # will start streaming samples. Observed in every Otii capture.
        self._send_payload(SESSION_INIT_WAKE)
        time.sleep(0.05)
        self._send_payload(encode_session_init_step2(self._next_seq()))
        time.sleep(0.05)
        self._send_payload(encode_session_init_step3(self._next_seq()))
        time.sleep(0.2)

    # ----- context manager ----------------------------------------------

    def __enter__(self) -> SMU:
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def close(self) -> None:
        """Stop any active recording and close the port."""
        if self._active_recording is not None:
            with contextlib.suppress(Exception):
                self.stop_recording()
        self._transport.close()

    # ----- identity & state queries -------------------------------------

    @property
    def info(self) -> Optional[SMUInfo]:
        return self._info

    @property
    def is_connected(self) -> bool:
        return self._transport.is_open

    @property
    def voltage(self) -> Optional[float]:
        """Last-set main output voltage (V), or None if never set."""
        return self._state.voltage

    @property
    def main_current(self) -> Optional[float]:
        return self._state.main_current

    @property
    def current_limit(self) -> Optional[float]:
        return self._state.current_limit

    @property
    def exp_voltage(self) -> Optional[float]:
        return self._state.exp_voltage

    @property
    def adc_resistor(self) -> Optional[float]:
        return self._state.adc_resistor

    @property
    def output_enabled(self) -> Optional[bool]:
        return self._state.output_enabled

    @property
    def four_wire_enabled(self) -> Optional[bool]:
        return self._state.four_wire_enabled

    @property
    def current_limit_enabled(self) -> Optional[bool]:
        return self._state.current_limit_enabled

    @property
    def range(self) -> Optional[str]:
        return self._state.range

    @property
    def uart_enabled(self) -> Optional[bool]:
        return self._state.uart_enabled

    @property
    def uart_baudrate(self) -> Optional[int]:
        return self._state.uart_baudrate

    @property
    def exp_5v_enabled(self) -> Optional[bool]:
        return self._state.exp_5v_enabled

    @property
    def legacy_sink_enabled(self) -> Optional[bool]:
        return self._state.legacy_sink_enabled

    @property
    def supply_mode(self) -> str:
        return self._state.supply_mode

    @property
    def power_regulation(self) -> Optional[str]:
        return self._state.power_regulation

    @property
    def gpo(self) -> dict[int, bool]:
        return dict(self._state.gpo)

    @property
    def enabled_channels(self) -> set[Channel]:
        return set(self._enabled_channels)

    @property
    def four_wire_state(self) -> str:
        """Mirrors the official ``arc.get_4wire`` semantics. Values:
        ``"disabled"`` if 4-wire is off, ``"active"`` if on. We do not
        return ``"cal_invalid"`` / ``"inactive"`` because the wire-side
        sense-mismatch detection has not been reverse-engineered."""
        if self._state.four_wire_enabled is None:
            return "disabled"
        return "active" if self._state.four_wire_enabled else "disabled"

    def version(self) -> dict:
        """Best-effort version info. Returns ``{}`` if the device hasn't
        emitted version metadata since the session opened."""
        # Look for hw/fw strings in already-buffered raw bytes (collected by
        # the reader thread during recording, or by any prior read_raw).
        sources: list[bytes] = []
        if self._raw_window_buffer:
            sources.append(bytes(self._raw_window_buffer))
        if self._reader_buffer:
            sources.append(bytes(self._reader_buffer))
        out: dict = {}
        for src in sources:
            meta = find_text_metadata(src)
            for k, v in meta.items():
                out.setdefault(k, v)
        # Normalise to the official client's key names
        if "firmware_version" in out:
            out["fw_version"] = out.pop("firmware_version")
        return out

    # ----- setters ------------------------------------------------------

    def set_voltage(self, volts: float) -> None:
        """Set the main output voltage (V).

        The device's low range caps around 3.5 V. To go higher call
        :py:meth:`set_range("high")` first.
        """
        if volts < 0:
            raise BenchValueError(f"voltage must be >= 0, got {volts}")
        if volts > 5.5:
            raise BenchValueError(f"voltage must be <= 5.5, got {volts}")
        self._send_set(CMD_SET_MAIN_VOLTAGE, volts_to_microvolts(volts))
        self._state.voltage = volts

    def set_current_limit(self, amps: float) -> None:
        """Set the over-current protection / max current (A)."""
        if amps < 0.001 or amps > 5.0:
            raise BenchValueError(
                f"current_limit must be in [0.001, 5.0] A, got {amps}"
            )
        self._send_set(CMD_SET_OC_PROTECTION, amps_to_milliamps(amps))
        self._state.current_limit = amps

    set_max_current = set_current_limit

    def set_main_current(self, amps: float) -> None:
        """Set the CC-mode source/sink current (A)."""
        if amps < -5.0 or amps > 5.0:
            raise BenchValueError(
                f"main_current must be in [-5.0, 5.0] A, got {amps}"
            )
        self._send_set(CMD_SET_MAIN_CURRENT, amps_to_microamps(amps))
        self._state.main_current = amps

    def set_output(self, enable: bool) -> None:
        """Enable / disable the main output."""
        self._send_set(CMD_SET_MAIN_OUTPUT, 1 if enable else 0)
        self._state.output_enabled = bool(enable)

    def set_range(self, range_: str) -> None:
        """Set main-output measurement range — ``"low"`` or ``"high"``."""
        if range_ == "low":
            self._send_set(CMD_SET_RANGE, RANGE_LOW)
        elif range_ == "high":
            self._send_set(CMD_SET_RANGE, RANGE_HIGH)
        else:
            raise BenchValueError(f"range must be 'low' or 'high', got {range_!r}")
        self._state.range = range_

    def set_four_wire(self, enable: bool) -> None:
        """Enable / disable 4-wire (sense) measurement mode."""
        self._send_set(CMD_SET_4WIRE, 1 if enable else 0)
        self._state.four_wire_enabled = bool(enable)

    def set_current_limit_enabled(self, enable: bool) -> None:
        """Enable / disable source-current-limit (CC) operation.

        True = constant current; False = cut-off when limit is exceeded.
        """
        self._send_set(CMD_SET_SRC_CUR_LIMIT_ENABLED, 1 if enable else 0)
        self._state.current_limit_enabled = bool(enable)

    def set_exp_voltage(self, volts: float) -> None:
        """Set the expansion-port digital voltage (V, 1.2-5.0)."""
        if volts < 1.2 or volts > 5.0:
            raise BenchValueError(
                f"exp_voltage must be in [1.2, 5.0] V, got {volts}"
            )
        self._send_set(CMD_SET_DIGITAL_VOLTAGE, volts_to_microvolts(volts))
        self._state.exp_voltage = volts

    def set_exp_5v(self, enable: bool) -> None:
        """Enable / disable the EXP-port 5V output."""
        value = 5_000_000 if enable else 0
        self._send_set(CMD_ENABLE_5V, value)
        self._state.exp_5v_enabled = bool(enable)

    enable_5v = set_exp_5v  # alias for parity with official API

    def set_exp_port(self, enable: bool) -> None:
        """Enable the expansion port (umbrella enable)."""
        # No distinct wire command observed for the umbrella enable. The
        # closest official-API equivalent is the same as set_exp_5v.
        # Treat this as a convenience alias; precise semantics differ from
        # the official client and are documented in ROADMAP.md.
        self.set_exp_5v(enable)

    def set_adc_resistor(self, ohms: float) -> None:
        """Set the ADC shunt resistor value (Ohm, 0.001-22)."""
        if ohms < 0.001 or ohms > 22.0:
            raise BenchValueError(
                f"adc_resistor must be in [0.001, 22.0] Ohm, got {ohms}"
            )
        self._send_set(CMD_SET_ADC_RESISTOR, ohms_to_microohms(ohms))
        self._state.adc_resistor = ohms

    def set_uart(self, enable: bool, *, baudrate: Optional[int] = None) -> None:
        """Enable / disable the UART decoder, optionally setting baud first."""
        if baudrate is not None:
            self.set_uart_baudrate(baudrate)
        self._send_set(CMD_SET_UART_ENABLE, 1 if enable else 0)
        self._state.uart_enabled = bool(enable)

    def enable_uart(self, enable: bool) -> None:
        """Alias of :py:meth:`set_uart` for parity with the official API."""
        self.set_uart(enable)

    def set_uart_baudrate(self, baud: int) -> None:
        """Set the UART decoder baud rate (0 disables)."""
        if baud < 0:
            raise BenchValueError(f"baudrate must be >= 0, got {baud}")
        self._send_set(CMD_SET_UART_BAUDRATE, baud)
        self._state.uart_baudrate = baud

    def write_tx(self, value: str | bytes) -> None:
        """Write a UART TX frame (text).

        Wire form: ``[seq][0x82][0x19][utf-8 bytes…]`` — a variable-length
        payload distinct from the SET/GET vocabulary. Decoded in cap #43.
        """
        self._raise_pending_error()
        payload = encode_write_tx(self._next_seq(), value)
        log.debug("write_tx %r", value)
        self._send_payload(payload)

    def set_tx(self, value: bool) -> None:
        """Drive the TX pin as a GPO.

        Decoded in cap #43: ``Arc.set_tx`` is exactly equivalent to
        ``set_gpo(3, state)`` — the TX pin occupies the third GPO slot
        (bits 6-8) in the SET_GPO bit pattern.
        """
        self.set_gpo(3, value)

    def get_rx(self) -> bool:
        """Read the RX pin as a GPI.

        Not yet decoded as a distinct wire command. The vendor API may
        report this from a GPI bitmap channel, but the bit-position has not
        been verified. Defers cleanly for now."""
        raise BenchNotImplementedError(
            "get_rx() is a deferred feature — see ROADMAP.md"
        )

    def set_gpo(self, pin: int, state: bool) -> None:
        """Set GPO pin (1, 2, or 3) to state.

        Pin 3 is the TX pin used as a GPO when UART is disabled — same
        wire encoding the vendor's ``Arc.set_tx`` produces (cap #43).
        """
        if pin not in (1, 2, 3):
            raise BenchValueError(f"GPO pin must be 1, 2, or 3, got {pin}")
        self._send_set(CMD_SET_GPO, encode_gpo(pin, state))
        self._state.gpo[pin] = bool(state)

    def get_gpi(self, pin: int) -> bool:
        """Get the state of one of the GPI pins (1 or 2).

        Returned from the streamed GPI bitmap (wire id 0x16). Bit `pin-1`
        of the latest bitmap sample is the pin state. Returns False if
        no GPI sample has been observed yet on this connection.
        """
        if pin not in (1, 2):
            raise BenchValueError(f"GPI pin must be 1 or 2, got {pin}")
        last = self._state.last_value.get(Channel.GPI1) or self._state.last_value.get(
            Channel.GPI2
        )
        if last is None:
            return False
        bitmap = int(last)
        return bool(bitmap & (1 << (pin - 1)))

    def set_legacy_sink(self, enable: bool) -> None:
        """Enable / disable legacy current-sink mode."""
        self._send_set(CMD_ENABLE_LEGACY_SINK, 1 if enable else 0)
        self._state.legacy_sink_enabled = bool(enable)

    enable_legacy_sink = set_legacy_sink

    def set_power_regulation(self, mode: str) -> None:
        """Set power regulation mode.

        Wire form: ``type=0x66 cmd=0x0A val=<mode>`` where
        ``voltage=0, current=1, inline=10, off=100`` (decoded in cap #43).
        """
        if mode not in POWER_REGULATION_MAP:
            raise BenchValueError(
                f"mode must be one of {sorted(POWER_REGULATION_MAP)}, got {mode!r}"
            )
        self._send_set(CMD_SET_POWER_REGULATION, power_regulation_value(mode))
        self._state.power_regulation = mode

    def set_supply_power_box(self) -> None:
        """Switch the supply mode back to the default 'power-box'."""
        self._state.supply_mode = "power-box"

    # ----- channels -----------------------------------------------------

    def enable_channel(self, channel: ChannelLike) -> None:
        """Mark a channel as enabled for the next :py:meth:`start_recording`.

        Auto-co-enables: ``MAIN_CURRENT`` enables ``MAIN_POWER``;
        ``ADC_CURRENT`` enables ``ADC_POWER`` — matching the device's own
        behaviour.
        """
        ch = Channel.coerce(channel)
        if not ch.toggleable:
            log.debug("channel %s is always-on; enable_channel is a no-op", ch.code)
            return
        self._enabled_channels.add(ch)
        for co in ch.co_enables:
            self._enabled_channels.add(Channel.from_code(co))

    def disable_channel(self, channel: ChannelLike) -> None:
        ch = Channel.coerce(channel)
        self._enabled_channels.discard(ch)
        # Note: we do NOT auto-disable co-enables; users explicitly opted
        # into the pair, leave it that way for predictability.

    def enable_channels(self, *channels: ChannelLike) -> None:
        for c in channels:
            self.enable_channel(c)

    def disable_all_channels(self) -> None:
        self._enabled_channels.clear()

    def get_channel_samplerate(self, channel: ChannelLike) -> int:
        """Return the channel's documented native sample rate."""
        return Channel.coerce(channel).sample_rate

    def set_channel_samplerate(self, channel: ChannelLike, value: int) -> None:
        """Deferred — see ROADMAP.md (server-side bug blocked capture)."""
        raise BenchNotImplementedError(
            "set_channel_samplerate() is a deferred feature — see ROADMAP.md"
        )

    def read_value(self, channel: ChannelLike, timeout: float = 1.5) -> float:
        """Block up to `timeout` seconds for the next sample on `channel`.

        Uses a brief raw read window when no recording is active.
        """
        ch = Channel.coerce(channel)
        if self._active_recording is not None:
            # Recording is running; wait for the reader thread to populate it
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if ch in self._active_recording._buffers:
                    channel_buf = self._active_recording._buffers[ch]
                    if channel_buf.values:
                        return channel_buf.values[-1]
                time.sleep(0.02)
            raise BenchTimeoutError(
                f"no sample for {ch.code} within {timeout:.2f} s"
            )

        # No recording: drain a window of raw bytes and parse
        raw = self._transport.read_for(min(timeout, 0.5))
        self._raw_window_buffer.extend(raw)
        # Check for error frames first
        for fr in iter_frames(raw):
            err = parse_error_frame(fr.payload)
            if err is not None:
                raise BenchCommandError(err.error_code, err.last_good_value)
            for rec in iter_samples(fr.payload):
                if rec.channel_id == ch.wire_id:
                    self._state.last_value[ch] = rec.value
                    return rec.value
        raise BenchTimeoutError(f"no sample for {ch.code} within {timeout:.2f} s")

    def read_window(
        self,
        channels: Iterable[ChannelLike],
        duration_s: float,
    ) -> dict[Channel, list[float]]:
        """Drain ``duration_s`` of inbound samples, return a list per channel.

        Like :py:meth:`read_raw` but parses sample records and groups them
        by channel. Useful when you need a recent-history window without
        starting a full recording — e.g. for the battery profiler's
        step-response measurement.

        Args:
            channels: channels of interest (others are ignored).
            duration_s: how long to drain the inbound stream.

        Returns:
            ``{channel: [sample, sample, ...]}``, one entry per requested
            channel (empty list if no samples arrived on that channel).

        Raises:
            BenchValueError: if a recording is active. Stop the recording
                first; the reader thread owns the byte stream during
                recording.
        """
        if self._active_recording is not None:
            raise BenchValueError(
                "read_window() requires no active recording — stop_recording() first"
            )
        target = {Channel.coerce(c) for c in channels}
        raw = self._transport.read_for(duration_s)
        self._raw_window_buffer.extend(raw)
        out: dict[Channel, list[float]] = {c: [] for c in target}
        for fr in iter_frames(raw):
            err = parse_error_frame(fr.payload)
            if err is not None:
                # Queue the error to be raised on the next SET (matches
                # read_value's behaviour).
                with self._pending_error_lock:
                    if self._pending_error is None:
                        self._pending_error = BenchCommandError(
                            err.error_code, err.last_good_value
                        )
                continue
            for rec in iter_samples(fr.payload):
                ch = WIRE_ID_TO_CHANNEL.get(rec.channel_id)
                if ch in target:
                    out[ch].append(rec.value)
                    self._state.last_value[ch] = rec.value
        return out

    # ----- recording lifecycle ------------------------------------------

    def start_recording(
        self,
        *,
        name: str = "recording",
        channels: Optional[Iterable[ChannelLike]] = None,
    ) -> Recording:
        """Start a new recording. Returns the :class:`Recording` object."""
        if self._active_recording is not None:
            raise BenchValueError("a recording is already active")

        if channels is None:
            channel_set: set[Channel] = set(self._enabled_channels)
        else:
            channel_set = {Channel.coerce(c) for c in channels}
        if not channel_set:
            raise BenchValueError(
                "no channels selected — call enable_channels(...) first"
            )

        # Auto-pair derived channels
        for src_code, derived_code in (("mc", "mp"), ("ac", "ap")):
            src = Channel.from_code(src_code)
            der = Channel.from_code(derived_code)
            if src in channel_set:
                channel_set.add(der)

        # Send one type=0x78 channel-enable per requested channel, in
        # wire-id ascending order to match the Otii server's behaviour
        # captured in phase33. Each enable is `[seq][0x78][wire_id][1]`.
        # Follow with a single 8-byte cleanup payload to flush.
        ordered_wire_ids = sorted({c.wire_id for c in channel_set})
        for wire_id in ordered_wire_ids:
            self._send_payload(
                encode_channel_enable_for_recording(self._next_seq(), wire_id, True)
            )
        self._send_payload(encode_recording_cleanup(self._next_seq()))
        self._active_recording_wire_ids = ordered_wire_ids

        # Pre-create channel buffers so reader thread doesn't have to lock
        rec = Recording(
            name=name,
            device_info={"port": self._transport.port},
        )
        for ch in channel_set:
            rec._ensure_buffer(ch, ch.sample_rate)
        rec._begin(self)
        self._active_recording = rec
        self._reader_sample_counts = {ch: 0 for ch in channel_set}
        self._reader_stop.clear()
        self._reader_thread = threading.Thread(
            target=self._reader_loop, name="benchctrl-reader", daemon=True
        )
        self._reader_thread.start()
        return rec

    def stop_recording(self) -> Recording:
        """Stop the active recording. Returns it for convenience."""
        rec = self._active_recording
        if rec is None:
            raise BenchValueError("no recording is active")

        # Signal reader thread to stop and wait for it.
        self._reader_stop.set()
        if self._reader_thread is not None:
            self._reader_thread.join(timeout=2.0)
        self._reader_thread = None

        # Prepare-stop (0x7E) flushes the packed-streaming buffer before we
        # send per-channel disables. The vendor sends this — cap #04 frame
        # 3182 + cap #18 frame 2310 both show 0x7E immediately before the
        # disable burst. Without it the device sometimes lags in switching
        # streaming modes.
        self._send_payload(encode_prepare_stop(self._next_seq()))

        # Send a per-channel disable for every wire id we enabled at start.
        # Symmetric to the start-of-recording enable burst.
        for wire_id in self._active_recording_wire_ids:
            self._send_payload(
                encode_channel_enable_for_recording(self._next_seq(), wire_id, False)
            )
        self._send_payload(encode_recording_cleanup(self._next_seq()))
        self._active_recording_wire_ids = []

        rec._end()
        self._active_recording = None
        return rec

    # ----- GET-parameter interface (type=0x64) --------------------------

    def get_param(
        self,
        cmd_code: int,
        *,
        timeout: float = 1.0,
    ) -> Response:
        """Send a GET-parameter command and return the device's response.

        Wire form: ``[seq][0x64][cmd][0]`` outbound; device replies with a
        unified response frame ``[0e 03 99 ff][seq][status:i32][data]`` —
        see :py:meth:`benchctrl.protocol.parse_response`.

        Args:
            cmd_code: parameter to query. The same code as the matching
                ``CMD_SET_*`` reads back that parameter (e.g. cmd=0x0B
                returns the cached main voltage). Banked variants
                (0x040B, 0x080B, …) expose per-range / per-calibration-set
                readbacks; see ``docs/protocol.md``.
            timeout: seconds to wait for the response.

        Returns:
            The decoded :class:`Response`. Check ``.ok`` / ``.not_available``
            / ``.rejected`` to discriminate outcomes; use ``.as_u32()``,
            ``.as_int()``, ``.as_float()``, ``.as_text()``, or
            ``.as_u32_array()`` for typed data extraction.

        Raises:
            BenchValueError: if a recording is active. Stop the recording
                first; the reader thread owns the inbound byte stream
                during a recording.
            BenchTimeoutError: if no response arrives within ``timeout``.
        """
        if self._active_recording is not None:
            raise BenchValueError(
                "get_param() requires no active recording — stop_recording() first"
            )
        self._raise_pending_error()
        seq = self._next_seq()
        self._send_payload(encode_get_command(seq, cmd_code))
        # Drain incoming bytes in short windows and look for a matching seq
        pending = bytearray()
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            chunk = self._transport.read_chunk(8192)
            if chunk:
                pending.extend(chunk)
            # Try to find a response for our seq
            for fr in iter_frames(bytes(pending)):
                r = parse_response(fr.payload)
                if r is not None and r.response_seq == seq:
                    return r
        raise BenchTimeoutError(
            f"no response to GET cmd=0x{cmd_code:04X} within {timeout:.2f}s"
        )

    def get_device_name(self) -> str:
        """Query the device's name string (typically ``"Arc"``)."""
        return self.get_param(CMD_GET_DEVICE_NAME).as_text()

    def get_hw_version(self) -> str:
        """Query the hardware version string."""
        return self.get_param(CMD_GET_HW_VERSION).as_text()

    def get_fw_version(self) -> str:
        """Query the firmware version string."""
        return self.get_param(CMD_GET_FW_VERSION).as_text()

    def get_device_id(self) -> str:
        """Query the 32-character device serial id."""
        return self.get_param(CMD_GET_DEVICE_ID).as_text()

    def get_main_voltage_setpoint(self) -> float | None:
        """Read back the device's cached main voltage (V).

        Wire form: ``GET cmd=0x0B``. Returns the cached setpoint in volts,
        or ``None`` if the device reports the parameter as not available.
        """
        r = self.get_param(0x0B)
        v = r.as_u32()
        return None if not r.ok or v is None else v / 1_000_000

    def get_max_current_setpoint(self) -> float | None:
        """Read back the device's cached current limit (A).

        Wire form: ``GET cmd=0x0C``. Value is milliamps.
        """
        r = self.get_param(0x0C)
        v = r.as_u32()
        return None if not r.ok or v is None else v / 1_000

    def get_exp_voltage_setpoint(self) -> float | None:
        """Read back the device's cached expansion-port voltage (V).

        Wire form: ``GET cmd=0x33``. Value is microvolts.
        """
        r = self.get_param(0x33)
        v = r.as_u32()
        return None if not r.ok or v is None else v / 1_000_000

    def get_uart_baudrate_setpoint(self) -> int | None:
        """Read back the device's cached UART baud rate (baud).

        Wire form: ``GET cmd=0x29``. Value is the baud rate directly.
        """
        r = self.get_param(0x29)
        v = r.as_u32()
        return None if not r.ok else v

    def get_channel_inventory(self) -> bytes:
        """Query the device's channel inventory table.

        Wire form: ``GET cmd=0x8D``. Returns the raw 268-byte response
        data. The decoded structure is documented in ``docs/protocol.md`` —
        a repeating record of
        ``[wire_id:u16][flags:u16][padding:u32][rate:u32][padding:u32]``
        per supported channel.
        """
        return self.get_param(CMD_GET_CHANNEL_INVENTORY).data

    @contextlib.contextmanager
    def record(
        self,
        *channels: ChannelLike,
        name: str = "recording",
    ) -> Iterator[Recording]:
        """Context-managed recording.

        Either pass channels explicitly:

            with smu.record(Channel.MAIN_CURRENT, Channel.MAIN_VOLTAGE) as rec:
                ...

        or pre-enable them via :py:meth:`enable_channels` and call with no
        arguments. Stop happens automatically on context exit.
        """
        rec = self.start_recording(
            name=name,
            channels=channels if channels else None,
        )
        try:
            yield rec
        finally:
            with contextlib.suppress(Exception):
                self.stop_recording()

    # ----- low-level streaming + raw access ----------------------------

    def stream(
        self,
        seconds: float = float("inf"),
        chunk_seconds: float = 0.2,
    ) -> Iterator[Sample]:
        """Yield real-time samples (host-driven).

        Equivalent to the legacy ``ArcDirect.stream_samples`` but yields
        typed :class:`Sample` objects with sample-rate-derived timestamps.
        Cannot be used while a recording is active.
        """
        if self._active_recording is not None:
            raise BenchValueError("cannot stream while a recording is active")

        t0 = time.monotonic()
        pending = bytearray()
        sample_counts: dict[Channel, int] = {}
        while time.monotonic() - t0 < seconds:
            window_end = time.monotonic() + chunk_seconds
            while time.monotonic() < window_end:
                chunk = self._transport.read_chunk(8192)
                if chunk:
                    pending.extend(chunk)
            consumed = 0
            for fr in iter_frames(bytes(pending)):
                err = parse_error_frame(fr.payload)
                if err is not None:
                    raise BenchCommandError(err.error_code, err.last_good_value)
                for rec in iter_samples(fr.payload):
                    ch = WIRE_ID_TO_CHANNEL.get(rec.channel_id)
                    if ch is None:
                        continue
                    self._state.last_value[ch] = rec.value
                    n = sample_counts.get(ch, 0)
                    sample_counts[ch] = n + 1
                    timestamp = n / ch.sample_rate if ch.sample_rate else 0.0
                    yield Sample(timestamp=timestamp, value=rec.value, channel=ch)
                consumed = fr.offset + 8 + len(fr.payload)
            if consumed > 0:
                del pending[:consumed]

    def read_raw(self, seconds: float) -> bytes:
        """Drain raw bulk-IN bytes for `seconds` — escape hatch."""
        if self._active_recording is not None:
            raise BenchValueError("cannot read raw while a recording is active")
        buf = self._transport.read_for(seconds)
        self._raw_window_buffer.extend(buf)
        return buf

    # ----- background reader for active recording -----------------------

    def _reader_loop(self) -> None:
        """Drain the bulk-IN stream into `self._active_recording`."""
        rec = self._active_recording
        if rec is None:
            return

        pending = bytearray()
        last_log = time.monotonic()
        while not self._reader_stop.is_set():
            try:
                chunk = self._transport.read_chunk(8192)
            except BenchConnectionError as exc:
                log.warning("reader: connection error, stopping: %s", exc)
                break
            if chunk:
                pending.extend(chunk)
                self._reader_buffer.extend(chunk)
            consumed = 0
            for fr in iter_frames(bytes(pending)):
                err = parse_error_frame(fr.payload)
                if err is not None:
                    with self._pending_error_lock:
                        self._pending_error = BenchCommandError(
                            err.error_code, err.last_good_value
                        )
                for rec_smpl in iter_samples(fr.payload):
                    ch = WIRE_ID_TO_CHANNEL.get(rec_smpl.channel_id)
                    if ch is None:
                        continue
                    self._state.last_value[ch] = rec_smpl.value
                    # Append to buffer; the buffer is one-thread-owned
                    # (this thread) so no lock needed for v0.1.
                    buf = rec._buffers.get(ch)
                    if buf is None:
                        # Out-of-recording channels (e.g. always-on
                        # temperature, GPI bitmap) — auto-add a buffer.
                        buf = rec._ensure_buffer(ch, ch.sample_rate)
                    buf.append(rec_smpl.value)
                consumed = fr.offset + 8 + len(fr.payload)
            if consumed > 0:
                del pending[:consumed]
            # Cap the raw replay buffer at ~1 MB so long recordings don't
            # grow unboundedly. We only need it for opportunistic version
            # parsing on close.
            if len(self._reader_buffer) > 1_000_000:
                self._reader_buffer = self._reader_buffer[-200_000:]
            # Periodic debug log
            if log.isEnabledFor(logging.DEBUG):
                now = time.monotonic()
                if now - last_log > 5.0:
                    counts = {ch.code: len(b.values) for ch, b in rec._buffers.items()}
                    log.debug("reader: counts=%s", counts)
                    last_log = now

    # ----- deferred features -------------------------------------------

    def calibrate(self) -> None:
        """Internal calibration — deferred for safety. See ROADMAP.md."""
        raise BenchNotImplementedError(
            "calibrate() is deferred — see ROADMAP.md (writes to NVM)"
        )

    def firmware_upgrade(self, filename: Optional[str] = None) -> None:
        """Firmware upgrade — deferred indefinitely (bricking risk).

        Use the vendor's Otii application for firmware updates.
        """
        raise BenchNotImplementedError(
            "firmware_upgrade() is intentionally deferred — see ROADMAP.md"
        )

    def enable_battery_profiling(self, enable: bool) -> None:
        raise BenchNotImplementedError(
            "battery profiling is deferred — see ROADMAP.md"
        )

    def set_battery_profile(self, value: str) -> None:
        raise BenchNotImplementedError(
            "battery profile selection is deferred — see ROADMAP.md"
        )

    def set_supply_battery_emulator(
        self,
        battery_profile_id: str,
        *,
        series: int = 1,
        parallel: int = 1,
        used_capacity: Optional[float] = None,
        soc: Optional[float] = None,
        soc_tracking: bool = True,
    ):
        raise BenchNotImplementedError(
            "battery emulation is deferred — see ROADMAP.md"
        )

    def wait_for_battery_data(self, timeout: float) -> float:
        raise BenchNotImplementedError(
            "battery data wait is deferred — see ROADMAP.md"
        )

    def iter_uart_log(self) -> Iterator[str]:
        raise BenchNotImplementedError(
            "UART log channel iteration is deferred — see ROADMAP.md"
        )

    # ----- generic property passthrough --------------------------------

    def get_property(self, name: str):
        """Mirror official ``arc.get_property``. v0.1 reads from host cache only."""
        return getattr(self._state, name, None)

    def set_property(self, name: str, value) -> None:
        """Mirror official ``arc.set_property``. v0.1 writes to host cache only."""
        if hasattr(self._state, name):
            setattr(self._state, name, value)
        else:
            # Allow arbitrary keys, stash in a dict on _State for parity
            extras = getattr(self._state, "_extras", None)
            if extras is None:
                extras = {}
                self._state._extras = extras  # type: ignore[attr-defined]
            extras[name] = value

    def commit(self) -> None:
        """No-op in benchctrl — every SET is sent immediately."""
        return None

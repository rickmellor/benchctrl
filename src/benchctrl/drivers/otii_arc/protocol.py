"""On-wire protocol — pure framing and command encoding.

Lives below `transport`. Knows nothing about pyserial; takes and returns
bytes. This keeps it 100% unit-testable without hardware.

Reference framing (host to device AND device to host):

    [A3 2C B5 7F]     4-byte device wire magic
    [L:u16 LE]        payload length
    [S:u16 LE]        checksum: sum(payload bytes) & 0xFFFF
    [payload]         L bytes

See ``docs/protocol.md`` for the full reverse-engineering notes.
"""

from __future__ import annotations

import struct
from collections.abc import Iterator
from dataclasses import dataclass

from benchctrl.exceptions import BenchProtocolError

# --- USB identity --------------------------------------------------------

VID = 0x0FCE
PID = 0xD1E6


# --- wire magic ----------------------------------------------------------

WIRE_MAGIC = b"\xa3\x2c\xb5\x7f"


# --- payload-level type tags --------------------------------------------

TYPE_POLL = 0x0A  # host heartbeat with monotonic ~1 MHz timestamp counter (optional)
TYPE_INIT_WAKE = 0x14  # short init payload
TYPE_GET_PARAMETER = 0x64  # GET cached parameter value
TYPE_INIT_STEP2 = 0x64  # legacy alias — init step 2 uses TYPE_GET_PARAMETER w/ cmd=0x29
TYPE_SET_PARAMETER = 0x66
TYPE_CHANNEL_ENABLE = 0x78  # cmd=wire_id, val=1 enables/0 disables that channel for streaming
TYPE_STOP_RECORDING = 0x78  # legacy alias of TYPE_CHANNEL_ENABLE
TYPE_PREPARE_STOP = 0x7E  # 8B host -> device "flush packed streaming buffer" before disable burst
TYPE_REC_CLEANUP = 0x7C  # 8B housekeeping payload after enable/disable burst
TYPE_ENABLE_LEGACY_SINK = 0x7C  # SET command (same code, different shape)
TYPE_WRITE_TEXT = 0x82  # variable-length: [seq][0x82][cmd][text bytes...]


# --- SET command codes (type 0x66) --------------------------------------

CMD_SET_4WIRE = 0x05
CMD_SET_SRC_CUR_LIMIT_ENABLED = 0x06
CMD_SET_RANGE = 0x08  # 0=low, 1=high
CMD_SET_MAIN_OUTPUT = 0x09  # output enable
CMD_SET_POWER_REGULATION = 0x0A  # voltage=0, current=1, inline=10, off=100
CMD_SET_MAIN_VOLTAGE = 0x0B  # microvolts
CMD_SET_OC_PROTECTION = 0x0C  # milliamps (also "max current")
CMD_SET_MAIN_CURRENT = 0x0D  # microamps (CC source/sink)
CMD_SET_ADC_RESISTOR = 0x1E  # micro-ohms
CMD_SET_UART_ENABLE = 0x28
CMD_SET_UART_BAUDRATE = 0x29
CMD_SET_GPO = 0x32  # encoded bit pattern, see encode_gpo (supports pins 1/2/3, TX pin = pin 3)
CMD_SET_DIGITAL_VOLTAGE = 0x33  # microvolts (expansion-port digital)
CMD_ENABLE_5V = 0x34  # 5_000_000 enables, 0 disables
CMD_ENABLE_LEGACY_SINK = 0x7C  # 1/0

# type=0x82 cmd code(s)
CMD_WRITE_TX = 0x19  # write UART TX text — payload is [seq][0x82][0x19][utf-8 bytes]

# Well-known GET cmd codes (type=0x64) for read-back / device identification
CMD_GET_DEVICE_NAME = 0x01      # response data: ASCII device name (e.g. "Arc")
CMD_GET_HW_VERSION = 0x80       # response data: ASCII hardware version (e.g. "1.3")
CMD_GET_FW_VERSION = 0x81       # response data: ASCII firmware version (e.g. "3.1.3")
CMD_GET_DEVICE_ID = 0x82        # response data: 32-char ASCII serial id
CMD_GET_CHANNEL_INVENTORY = 0x8D  # response data: 268 B channel inventory table

# Symbolic power regulation modes (value semantics for cmd=0x0A)
POWER_REGULATION_VOLTAGE = 0
POWER_REGULATION_CURRENT = 1
POWER_REGULATION_INLINE = 10
POWER_REGULATION_OFF = 100
POWER_REGULATION_MAP = {
    "voltage": POWER_REGULATION_VOLTAGE,
    "current": POWER_REGULATION_CURRENT,
    "inline": POWER_REGULATION_INLINE,
    "off": POWER_REGULATION_OFF,
}


# --- range values --------------------------------------------------------

RANGE_LOW = 0
RANGE_HIGH = 1


# --- legacy recording payload constants ---------------------------------
#
# The byte sequence below was historically used by `arc_direct.start_recording`,
# but reverse-engineering capture #33 showed it is actually a mis-read of an
# *inbound* packed-sample frame (the device's high-rate streaming envelope).
# We keep these constants for backwards reference but do NOT send them on the
# wire — see `encode_channel_enable_for_recording` for the correct command.

START_REC_HEADER = b"\x69\x83\x2a\xff\x17\x02\x00\x00"  # legacy / artifact
START_REC_SENTINEL = b"\x17\x00\x00\x00\x00\x00\x00\x00"  # also tail of packed sample frames


# --- inbound frame signatures -------------------------------------------

# Baseline sample record (within a device->host frame payload):
#   [02 00 08 00] [chan:u32 LE] [value:f32 LE]   — 12 B
# Emitted when streaming in the slow baseline mode (no channels explicitly
# enabled for recording).
SAMPLE_RECORD_HEADER = b"\x02\x00\x08\x00"
SAMPLE_RECORD_LEN = 12

# Packed sample frame (full payload, device -> host):
#   [69 83 2a ff] [seq:u32 LE] then per-channel records:
#       subtype=1: [id:u16][1:u16][rate:u32][value:f32 LE]                 — 12 B  (1 sample)
#       subtype=4: [id:u16][4:u16][rate:u32][value0..3:f32 LE x4]          — 24 B  (4 samples)
#   then 8-byte sentinel [17 00 00 00 00 00 00 00]
#
# Frames arrive at the slowest enabled channel's rate (1 kHz when any sub-1
# channel is enabled). High-rate (sub-4) channels carry 4 samples per frame.
PACKED_FRAME_MAGIC = b"\x69\x83\x2a\xff"
PACKED_FRAME_SENTINEL = b"\x17\x00\x00\x00\x00\x00\x00\x00"

# Error response frame (whole 16-B payload):
#   [0e 03 99 ff 04 10 00 00] [err:i32 LE] [last_good:u32 LE]
ERROR_PAYLOAD_HEADER = b"\x0e\x03\x99\xff\x04\x10\x00\x00"
ERROR_PAYLOAD_LEN = 16

# SET-ack frame (whole 16-B payload):
#   [0e 03 99 ff] [0x1000 | cmd:u32 LE] [field:u32 LE] [value:u32 LE]
SET_ACK_PREFIX = b"\x0e\x03\x99\xff"
SET_ACK_PAYLOAD_LEN = 16
SET_ACK_CMD_MASK = 0x1000


# --- session init payloads ----------------------------------------------

# These three payloads are sent on every fresh open() so the device starts
# streaming. They were observed in every captured session.
SESSION_INIT_WAKE = b"\x01\x00\x00\x00\x14\x00\x00\x00"

# The other two depend on the per-connection sequence number; see
# `build_session_init_step2()` and `build_session_init_step3()`.


# ============================================================================
# Encoding (host -> device)
# ============================================================================


def checksum(payload: bytes) -> int:
    """Wire checksum: sum of payload bytes, masked to 16 bits."""
    return sum(payload) & 0xFFFF


def encode_frame(payload: bytes) -> bytes:
    """Wrap a payload in the device's wire framing.

    Args:
        payload: raw payload bytes.

    Returns:
        Framed bytes ready to write to the serial port.
    """
    if len(payload) > 0xFFFF:
        raise BenchProtocolError(f"payload too long: {len(payload)} > 65535")
    return WIRE_MAGIC + struct.pack("<HH", len(payload), checksum(payload)) + payload


def encode_set_command(seq: int, cmd_code: int, value: int) -> bytes:
    """Build the 16-byte SET-parameter payload.

    Args:
        seq: monotonic per-connection sequence number.
        cmd_code: one of the ``CMD_SET_*`` constants.
        value: command-specific 32-bit unsigned value.
    """
    return struct.pack("<IIII", seq & 0xFFFFFFFF, TYPE_SET_PARAMETER, cmd_code, value & 0xFFFFFFFF)


def encode_get_command(seq: int, cmd_code: int) -> bytes:
    """Build the 16-byte GET-parameter payload (type=0x64).

    Args:
        seq: monotonic per-connection sequence number.
        cmd_code: parameter to query — usually the same as the corresponding
            ``CMD_SET_*`` (e.g. cmd=0x0B queries main voltage).
            Banked variants (0x040B, 0x080B, ...) expose per-range or
            calibration-set readbacks; see ``docs/protocol.md``.
    """
    return struct.pack("<IIII", seq & 0xFFFFFFFF, TYPE_GET_PARAMETER, cmd_code, 0)


def encode_write_tx(seq: int, text: str | bytes) -> bytes:
    """Build the variable-length UART TX text payload (type=0x82 cmd=0x19).

    The wire form is ``[seq:u32][0x82:u32][0x19:u32][utf-8 bytes…]`` — no
    trailing null and no length prefix; the device reads to end-of-frame.
    """
    data = text.encode("utf-8") if isinstance(text, str) else bytes(text)
    return struct.pack("<III", seq & 0xFFFFFFFF, TYPE_WRITE_TEXT, CMD_WRITE_TX) + data


def encode_prepare_stop(seq: int) -> bytes:
    """8-byte 'flush packed streaming buffer' payload (type=0x7E).

    Otii sends this immediately before a per-channel disable burst when
    stopping a recording. Without it, the device sometimes lags in switching
    streaming modes. benchctrl's stop_recording sends it for vendor parity."""
    return struct.pack("<II", seq & 0xFFFFFFFF, TYPE_PREPARE_STOP)


def encode_poll(seq: int, timestamp_us: int) -> bytes:
    """16-byte host heartbeat (type=0x0A) with a monotonic timestamp.

    Optional — the device functions without it. Otii sends one every ~1 s
    with a microsecond-resolution counter at the cmd offset and constant
    ``value=4``.
    """
    return struct.pack("<IIII", seq & 0xFFFFFFFF, TYPE_POLL, timestamp_us & 0xFFFFFFFF, 4)


def power_regulation_value(mode: str) -> int:
    """Map a power-regulation mode string to its wire value."""
    if mode not in POWER_REGULATION_MAP:
        raise BenchProtocolError(
            f"unknown power_regulation mode {mode!r}; "
            f"valid: {sorted(POWER_REGULATION_MAP)}"
        )
    return POWER_REGULATION_MAP[mode]


def encode_session_init_step2(seq: int) -> bytes:
    """Init step 2 payload (16 B). Sent after the wake payload."""
    return struct.pack("<IIII", seq & 0xFFFFFFFF, TYPE_INIT_STEP2, 0x29, 0)


def encode_session_init_step3(seq: int) -> bytes:
    """Init step 3 payload (16 B). Sent after step 2.

    Mechanically identical to ``encode_channel_enable_for_recording(seq, 0x17, True)``
    — the historical "init step 3" simply enables the rx (UART log) channel for
    streaming, which is also what triggers the device to start delivering its
    baseline 12-channel stream.
    """
    return encode_channel_enable_for_recording(seq, 0x17, True)


def encode_channel_enable_for_recording(seq: int, wire_id: int, enable: bool) -> bytes:
    """Enable / disable one channel for recording-mode streaming (16 B).

    Sent once per channel to be recorded. After the burst (plus an optional
    recording cleanup payload), the device switches from baseline streaming
    (12-byte ``02 00 08 00``-prefixed records at ~6 Hz) to packed high-rate
    streaming (76-byte ``69 83 2a ff``-prefixed frames at the slowest enabled
    channel's native rate, e.g. 1 kHz, with 4 samples packed per frame for
    sub-4 channels like main current / main power).

    Args:
        seq: monotonic per-connection sequence number.
        wire_id: device-side channel id (e.g. 0x00 for ``mc``).
        enable: True to start streaming this channel, False to stop.
    """
    return struct.pack(
        "<IIII",
        seq & 0xFFFFFFFF,
        TYPE_CHANNEL_ENABLE,
        wire_id,
        1 if enable else 0,
    )


def encode_stop_recording(seq: int, target_wire_id: int) -> bytes:
    """Legacy single-channel stop. Equivalent to disabling one channel.

    Kept for backwards compatibility; new code should call
    :func:`encode_channel_enable_for_recording` with ``enable=False`` directly.
    """
    return encode_channel_enable_for_recording(seq, target_wire_id, False)


def encode_recording_cleanup(seq: int) -> bytes:
    """8-byte cleanup payload sent after a burst of channel enable/disable
    commands. Without it the device sometimes lags in switching streaming
    modes."""
    return struct.pack("<II", seq & 0xFFFFFFFF, TYPE_REC_CLEANUP)


@dataclass(frozen=True)
class RecordingChannel:
    """One entry in a START_RECORDING channel-config payload."""

    wire_id: int
    subtype: int  # 1 (low-rate) or 4 (high-rate)
    sample_rate: int
    initial_value: bytes = b""  # 16-B blob for subtype=4, ignored otherwise

    def encode(self) -> bytes:
        if self.subtype == 1:
            return struct.pack("<HHII", self.wire_id, self.subtype, self.sample_rate, 0)
        if self.subtype == 4:
            blob = (self.initial_value + b"\x00" * 16)[:16]
            return struct.pack("<HHI", self.wire_id, self.subtype, self.sample_rate) + blob
        raise BenchProtocolError(f"recording subtype {self.subtype} not supported")


def encode_start_recording(channels: list[RecordingChannel]) -> bytes:
    """Build the full START_RECORDING payload.

    Layout:
        [START_REC_HEADER (8 B)] [N channel records] [START_REC_SENTINEL (8 B)]

    The order of `channels` is preserved in the payload. The caller is
    responsible for any auto-co-enable logic (mc => mp, ac => ap).
    """
    return START_REC_HEADER + b"".join(c.encode() for c in channels) + START_REC_SENTINEL


def encode_gpo(pin: int, state: bool) -> int:
    """Encode a GPO state as the SET-command value.

    Each pin reserves 3 bits in the value field. bit 0 = OFF command,
    bit 1 = ON command (bit 2 unused, possibly tri-state).

    Verified against captures (cap #29 for pins 1-2; cap #43 for pin 3 via
    `Arc.set_tx`):
        gpo(1, True)  -> 2     (bit 1)
        gpo(1, False) -> 1     (bit 0)
        gpo(2, True)  -> 16    (bit 4)
        gpo(2, False) -> 8     (bit 3)
        gpo(3, True)  -> 128   (bit 7) — same wire encoding as Arc.set_tx(True)
        gpo(3, False) -> 64    (bit 6) — same wire encoding as Arc.set_tx(False)
    """
    if pin < 1:
        raise BenchProtocolError(f"GPO pin must be >= 1, got {pin}")
    bit = (pin - 1) * 3 + (1 if state else 0)
    return 1 << bit


def volts_to_microvolts(volts: float) -> int:
    return int(round(volts * 1_000_000))


def amps_to_milliamps(amps: float) -> int:
    return int(round(amps * 1_000))


def amps_to_microamps(amps: float) -> int:
    return int(round(amps * 1_000_000))


def ohms_to_microohms(ohms: float) -> int:
    return int(round(ohms * 1_000_000))


# ============================================================================
# Decoding (device -> host)
# ============================================================================


@dataclass(frozen=True)
class Frame:
    """A single validated wire frame.

    Attributes:
        offset: starting byte index in the source buffer.
        payload: the raw payload (length, checksum already validated).
    """

    offset: int
    payload: bytes


def iter_frames(buf: bytes) -> Iterator[Frame]:
    """Yield every validly-framed packet in `buf`.

    Walks by `WIRE_MAGIC`, validates L and S, yields validated payloads only.
    Garbage between frames (corrupted bytes, leading partials, unframed text)
    is skipped silently.

    Truncated frames at the tail terminate iteration cleanly without raising.
    """
    i = 0
    n = len(buf)
    while i + 8 <= n:
        if buf[i : i + 4] != WIRE_MAGIC:
            i += 1
            continue
        length = struct.unpack_from("<H", buf, i + 4)[0]
        expected_sum = struct.unpack_from("<H", buf, i + 6)[0]
        payload_end = i + 8 + length
        if payload_end > n:
            break  # incomplete frame at tail
        payload = bytes(buf[i + 8 : payload_end])
        if checksum(payload) == expected_sum:
            yield Frame(offset=i, payload=payload)
            i = payload_end
        else:
            # Bad checksum: skip past the magic and resync
            i += 4


@dataclass(frozen=True)
class SampleRecord:
    """A single channel-sample within a frame payload."""

    channel_id: int
    value: float


def iter_samples(payload: bytes) -> Iterator[SampleRecord]:
    """Walk a frame payload and yield every sample record found.

    Handles both wire formats:

      - **Baseline** records: 12-byte ``[02 00 08 00][chan:u32][value:f32]``
        (one sample per record, used when no recording is active).
      - **Packed** frames: full payload starts with ``[69 83 2a ff][seq:u32]``
        and carries per-channel sub-1 (1 sample) or sub-4 (4 samples) records,
        terminated by an 8-byte sentinel. Used during a recording at the
        device's native rates.

    For packed frames, sub-4 records yield four consecutive samples for the
    same channel; sub-1 records yield one. Other record types
    (errors, acks, metadata) are skipped.
    """
    # Packed frame path — payload starts with the packed magic
    if payload[:4] == PACKED_FRAME_MAGIC and len(payload) >= 16:
        yield from _iter_packed_samples(payload)
        return

    # Baseline byte-scan for `02 00 08 00`-prefixed records
    k = 0
    m = len(payload)
    while k + SAMPLE_RECORD_LEN <= m:
        if payload[k : k + 4] == SAMPLE_RECORD_HEADER:
            chan = struct.unpack_from("<I", payload, k + 4)[0]
            if chan < 0x80:
                val = struct.unpack_from("<f", payload, k + 8)[0]
                yield SampleRecord(channel_id=chan, value=val)
                k += SAMPLE_RECORD_LEN
                continue
        k += 1


def _iter_packed_samples(payload: bytes) -> Iterator[SampleRecord]:
    """Walk a packed sample frame and yield each contained float sample.

    Layout (after 8-byte ``[magic][seq]`` header):
        per channel:
            [wire_id:u16][subtype:u16][rate:u32]
            then either a 4-byte f32 (subtype=1) or four 4-byte f32s (subtype=4)
        trailing 8-byte sentinel.
    """
    n = len(payload)
    # Skip 8-byte header (magic + seq)
    i = 8
    # Stop before the 8-byte sentinel — we don't strictly require it, but
    # capping at n-8 keeps us out of garbage in malformed frames.
    end = n - 8 if n >= 16 else n
    while i + 8 <= end:
        wire_id, subtype = struct.unpack_from("<HH", payload, i)
        # rate is at offset i+4 but we don't yield it
        if subtype == 1:
            need = 12
            if i + need > end:
                break
            val = struct.unpack_from("<f", payload, i + 8)[0]
            yield SampleRecord(channel_id=wire_id, value=val)
            i += need
        elif subtype == 4:
            need = 24
            if i + need > end:
                break
            # 4 packed floats follow the [id][sub][rate] header
            for k in range(4):
                val = struct.unpack_from("<f", payload, i + 8 + 4 * k)[0]
                yield SampleRecord(channel_id=wire_id, value=val)
            i += need
        else:
            # Unknown subtype — bail rather than guess length
            break


# ---------------------------------------------------------------------------
# Response frame parsing — unified format decoded in cap #16 (phase37)
# ---------------------------------------------------------------------------
#
# All device->host non-sample responses share the shape:
#
#     [0e 03 99 ff] [response_seq:u32 LE] [status:i32 LE] [data: 0..N bytes]
#
# The response_seq echoes the request's seq number for routing/pairing. The
# status field interpretation:
#
#     0           — success, data follows
#     -3 (0xFD..) — parameter not available / not supported
#     -101        — value rejected as out of range (the legacy "error frame")
#     other neg.  — other device-side rejection
#
# Common data shapes by response length:
#
#     12 B (status only)            — short ack / N/A reply
#     16 B (status + 1×u32)         — single-value parameter readback
#     20 B (status + 2×u32 + 1×u32) — composite (seen for GET cmd=0x0C06)
#     28 B (status + 5×u32)         — calibration-table responses (GET 0x51-0x5F)
#     44 B (status + 32 ASCII bytes)— device serial id
#     >44 B (status + bulk data)    — channel inventory (268 B for cmd=0x8D)


@dataclass(frozen=True)
class Response:
    """A parsed device response frame.

    Attributes:
        response_seq: echoed request sequence number
        status: int32 — 0 = OK, negative = error
        data: bytes following the status field (may be empty)
    """

    response_seq: int
    status: int
    data: bytes

    @property
    def ok(self) -> bool:
        return self.status == 0

    @property
    def not_available(self) -> bool:
        return self.status == -3

    @property
    def rejected(self) -> bool:
        return self.status < 0 and self.status != -3

    def as_u32(self) -> int | None:
        """Return the first 32-bit word of data as an unsigned int."""
        if len(self.data) < 4:
            return None
        return struct.unpack_from("<I", self.data, 0)[0]

    def as_int(self) -> int | None:
        """Return the first 32-bit word of data as a signed int."""
        if len(self.data) < 4:
            return None
        return struct.unpack_from("<i", self.data, 0)[0]

    def as_float(self) -> float | None:
        """Return the first 32-bit word of data as float32."""
        if len(self.data) < 4:
            return None
        return struct.unpack_from("<f", self.data, 0)[0]

    def as_text(self) -> str:
        """Return the data as a UTF-8 string (best effort, trim NULs)."""
        return self.data.rstrip(b"\x00").decode("utf-8", errors="replace")

    def as_u32_array(self) -> tuple[int, ...]:
        """Return the data as a tuple of u32 values."""
        n = len(self.data) // 4
        return struct.unpack_from(f"<{n}I", self.data, 0)


def parse_response(payload: bytes) -> Response | None:
    """Return a parsed response, or None if `payload` isn't a device response.

    Accepts any length >= 8 bytes (header + seq). Status is decoded as i32.
    """
    if len(payload) < 8 or payload[:4] != b"\x0e\x03\x99\xff":
        return None
    response_seq = struct.unpack_from("<I", payload, 4)[0]
    status = 0
    data = b""
    if len(payload) >= 12:
        status = struct.unpack_from("<i", payload, 8)[0]
        data = bytes(payload[12:])
    return Response(response_seq=response_seq, status=status, data=data)


# --- legacy convenience APIs --------------------------------------------------
# Kept so callers built against v0.1 still work. New code should use
# `parse_response()` directly.

@dataclass(frozen=True)
class ErrorRecord:
    """Legacy: a device error response to a rejected SET."""

    error_code: int  # signed
    last_good_value: int  # u32


def parse_error_frame(payload: bytes) -> ErrorRecord | None:
    """Return the parsed error, or None.

    Updated semantics: an "error" is any 16-byte response with status < 0.
    The legacy v0.1 implementation used a hard-coded ``04 10 00 00`` prefix
    which was actually ``seq=0x1004`` (a regular seq number). This version
    correctly identifies errors by checking the status word.
    """
    r = parse_response(payload)
    if r is None or len(payload) != 16 or r.status >= 0:
        return None
    last_good = r.as_u32() or 0
    return ErrorRecord(error_code=r.status, last_good_value=last_good)


@dataclass(frozen=True)
class SetAck:
    """Legacy: a device acknowledgement of a SET command."""

    command_code: int
    field: int
    value: int


def parse_set_ack_frame(payload: bytes) -> SetAck | None:
    """Return the parsed SET ack, or None.

    Updated semantics: a SET ack is a 16-byte successful response (status=0)
    with the value at offset 12-15. We synthesise the legacy ``command_code``
    field by extracting the low byte of the response_seq for compatibility,
    though it is no longer a meaningful field in the new protocol model.
    """
    r = parse_response(payload)
    if r is None or len(payload) != 16 or not r.ok:
        return None
    field = r.as_u32() or 0
    value = struct.unpack_from("<I", payload, 12)[0]
    return SetAck(
        command_code=r.response_seq & 0xFF,
        field=field,
        value=value,
    )


def find_text_metadata(buf: bytes) -> dict[str, str]:
    """Opportunistically extract device-id and firmware version strings.

    The Arc Pro emits an ASCII device id (32 hex-ish chars) and a firmware
    version string ("N.N.N") in some early-session responses but not all.
    This is best-effort; callers should not depend on the keys being present.
    """
    import re

    result: dict[str, str] = {}
    m = re.search(rb"[0-9A-F]{32}", buf)
    if m:
        result["device_id"] = m.group(0).decode("ascii")
    m = re.search(rb"(\d{1,2})\.(\d{1,2})\.(\d{1,2})", buf)
    if m:
        result["firmware_version"] = m.group(0).decode("ascii")
    return result

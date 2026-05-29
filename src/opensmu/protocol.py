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

from opensmu.exceptions import SMUProtocolError

# --- USB identity --------------------------------------------------------

VID = 0x0FCE
PID = 0xD1E6


# --- wire magic ----------------------------------------------------------

WIRE_MAGIC = b"\xa3\x2c\xb5\x7f"


# --- payload-level type tags --------------------------------------------

TYPE_POLL = 0x0A
TYPE_INIT_WAKE = 0x14  # short init payload
TYPE_GET_PARAMETER = 0x64  # GET cached parameter value
TYPE_INIT_STEP2 = 0x64  # legacy alias — init step 2 uses TYPE_GET_PARAMETER w/ cmd=0x29
TYPE_SET_PARAMETER = 0x66
TYPE_CHANNEL_ENABLE = 0x78  # cmd=wire_id, val=1 enables/0 disables that channel for streaming
TYPE_STOP_RECORDING = 0x78  # legacy alias of TYPE_CHANNEL_ENABLE
TYPE_REC_CLEANUP = 0x7C  # 8B housekeeping payload after enable/disable burst
TYPE_ENABLE_LEGACY_SINK = 0x7C  # SET command (same code, different shape)


# --- SET command codes (type 0x66) --------------------------------------

CMD_SET_4WIRE = 0x05
CMD_SET_SRC_CUR_LIMIT_ENABLED = 0x06
CMD_SET_RANGE = 0x08  # 0=low, 1=high
CMD_SET_MAIN_OUTPUT = 0x09  # output enable
CMD_SET_MAIN_VOLTAGE = 0x0B  # microvolts
CMD_SET_OC_PROTECTION = 0x0C  # milliamps (also "max current")
CMD_SET_MAIN_CURRENT = 0x0D  # microamps (CC source/sink)
CMD_SET_ADC_RESISTOR = 0x1E  # micro-ohms
CMD_SET_UART_ENABLE = 0x28
CMD_SET_UART_BAUDRATE = 0x29
CMD_SET_GPO = 0x32  # encoded bit pattern, see encode_gpo
CMD_SET_DIGITAL_VOLTAGE = 0x33  # microvolts (expansion-port digital)
CMD_ENABLE_5V = 0x34  # 5_000_000 enables, 0 disables
CMD_ENABLE_LEGACY_SINK = 0x7C  # 1/0


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
        raise SMUProtocolError(f"payload too long: {len(payload)} > 65535")
    return WIRE_MAGIC + struct.pack("<HH", len(payload), checksum(payload)) + payload


def encode_set_command(seq: int, cmd_code: int, value: int) -> bytes:
    """Build the 16-byte SET-parameter payload.

    Args:
        seq: monotonic per-connection sequence number.
        cmd_code: one of the ``CMD_SET_*`` constants.
        value: command-specific 32-bit unsigned value.
    """
    return struct.pack("<IIII", seq & 0xFFFFFFFF, TYPE_SET_PARAMETER, cmd_code, value & 0xFFFFFFFF)


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
        raise SMUProtocolError(f"recording subtype {self.subtype} not supported")


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

    Verified against captures:
        gpo(1, True)  -> 2     (bit 1)
        gpo(1, False) -> 1     (bit 0)
        gpo(2, True)  -> 16    (bit 4)
        gpo(2, False) -> 8     (bit 3)
    """
    if pin < 1:
        raise SMUProtocolError(f"GPO pin must be >= 1, got {pin}")
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


@dataclass(frozen=True)
class ErrorRecord:
    """A device error response to a rejected SET."""

    error_code: int  # signed
    last_good_value: int  # u32


def parse_error_frame(payload: bytes) -> ErrorRecord | None:
    """Return the parsed error, or None if `payload` isn't an error frame."""
    if len(payload) != ERROR_PAYLOAD_LEN:
        return None
    if payload[:8] != ERROR_PAYLOAD_HEADER:
        return None
    err = struct.unpack_from("<i", payload, 8)[0]
    last_good = struct.unpack_from("<I", payload, 12)[0]
    return ErrorRecord(error_code=err, last_good_value=last_good)


@dataclass(frozen=True)
class SetAck:
    """A device acknowledgement of a SET command (echoes cmd_code | 0x1000)."""

    command_code: int
    field: int
    value: int


def parse_set_ack_frame(payload: bytes) -> SetAck | None:
    """Return the parsed SET ack, or None if `payload` isn't one.

    SET-ack and error frames share the `0e 03 99 ff` prefix. The error
    frame's second word is the fixed `04 10 00 00` discriminator; SET-ack
    frames carry `0x1000 | cmd_code` instead.
    """
    if len(payload) != SET_ACK_PAYLOAD_LEN:
        return None
    if payload[:4] != SET_ACK_PREFIX:
        return None
    second_word = struct.unpack_from("<I", payload, 4)[0]
    if (second_word & 0xFF00) != SET_ACK_CMD_MASK:
        # Discriminate from error frame (which has 0x1004 == 4 10 00 00 LE)
        return None
    # The error frame's discriminator (0x00001004) also satisfies the mask;
    # treat it as not-a-set-ack so callers can use parse_error_frame.
    if second_word == 0x00001004:
        return None
    cmd_code = second_word & 0xFF
    field = struct.unpack_from("<I", payload, 8)[0]
    value = struct.unpack_from("<I", payload, 12)[0]
    return SetAck(command_code=cmd_code, field=field, value=value)


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

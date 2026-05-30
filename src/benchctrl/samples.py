"""Sample parsing, per-channel buffering, statistics.

Pure functions and lightweight value objects. Knows nothing about a
serial port; takes raw buffer bytes (or pre-parsed records) and returns
typed Python data.
"""

from __future__ import annotations

import csv
import json
import math
import struct
from collections.abc import Iterable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from benchctrl.drivers.otii_arc.channels import (
    WIRE_ID_TO_CHANNEL,
    OtiiArcChannel as Channel,
)
from benchctrl.drivers.otii_arc.protocol import (
    SAMPLE_RECORD_HEADER,
    SAMPLE_RECORD_LEN,
    SampleRecord,
    iter_frames,
    iter_samples,
)


@dataclass(frozen=True)
class Sample:
    """A single timestamped sample on one channel."""

    timestamp: float  # seconds relative to recording start
    value: float
    channel: Channel


# ---------------------------------------------------------------------------
# Parsing — frame-aware extraction from a raw inbound buffer
# ---------------------------------------------------------------------------


def parse_samples_by_id(buf: bytes) -> dict[int, list[float]]:
    """Group raw float samples by wire channel id from a captured buffer.

    Frame-aware: walks the wire framing, validates checksum on each frame,
    extracts ``02 00 08 00 [chan:u32 LE] [float]`` records. Bytes that
    don't sit inside a validated frame (leading garbage, tail truncation)
    are also byte-scanned as a fallback, matching the legacy
    ``ArcDirect.parse_samples`` behaviour for partial captures.
    """
    out: dict[int, list[float]] = {}

    def scan_region(region: bytes) -> None:
        k = 0
        m = len(region)
        while k + SAMPLE_RECORD_LEN <= m:
            if region[k : k + 4] == SAMPLE_RECORD_HEADER:
                chan = struct.unpack_from("<I", region, k + 4)[0]
                if chan < 0x80:
                    val = struct.unpack_from("<f", region, k + 8)[0]
                    out.setdefault(chan, []).append(val)
                    k += SAMPLE_RECORD_LEN
                    continue
            k += 1

    last_consumed = 0
    for frame in iter_frames(buf):
        if frame.offset > last_consumed:
            scan_region(buf[last_consumed : frame.offset])
        scan_region(frame.payload)
        last_consumed = frame.offset + 8 + len(frame.payload)
    if last_consumed < len(buf):
        scan_region(buf[last_consumed:])
    return out


def parse_samples_by_channel(buf: bytes) -> dict[Channel, list[float]]:
    """Same as :func:`parse_samples_by_id` but keyed by :class:`Channel`."""
    raw = parse_samples_by_id(buf)
    out: dict[Channel, list[float]] = {}
    for wire_id, values in raw.items():
        ch = WIRE_ID_TO_CHANNEL.get(wire_id)
        if ch is not None:
            out[ch] = values
    return out


def iter_records(buf: bytes) -> Iterator[SampleRecord]:
    """Yield every sample record across every valid frame in `buf`.

    Order is preserved (frame by frame, then within-frame). Useful when
    you want a single chronological stream rather than per-channel lists.
    """
    for frame in iter_frames(buf):
        yield from iter_samples(frame.payload)


# ---------------------------------------------------------------------------
# Per-channel buffer with synthesised timestamps
# ---------------------------------------------------------------------------


@dataclass
class ChannelBuffer:
    """Captured samples for one channel.

    Timestamps are synthesised from `t0 + index / sample_rate`. This matches
    the official Otii API's ``get_channel_info`` semantics: a `from`/`to`
    span and a single `sample_rate`. Inter-sample jitter is not exposed by
    the device, so the synthesised timeline is the authoritative one.
    """

    channel: Channel
    values: list[float] = field(default_factory=list)
    sample_rate: int = 0
    t0: float = 0.0  # seconds from recording start to sample[0]

    def __len__(self) -> int:
        return len(self.values)

    def append(self, value: float) -> None:
        self.values.append(value)

    def extend(self, values: Iterable[float]) -> None:
        self.values.extend(values)

    @property
    def duration(self) -> float:
        if not self.values or self.sample_rate <= 0:
            return 0.0
        return len(self.values) / self.sample_rate

    @property
    def t_end(self) -> float:
        return self.t0 + self.duration

    def timestamps(self) -> list[float]:
        """Materialise the synthetic time axis for this channel."""
        if self.sample_rate <= 0:
            return [self.t0] * len(self.values)
        dt = 1.0 / self.sample_rate
        return [self.t0 + i * dt for i in range(len(self.values))]

    def iter_samples(self) -> Iterator[Sample]:
        if self.sample_rate <= 0:
            for v in self.values:
                yield Sample(timestamp=self.t0, value=v, channel=self.channel)
            return
        dt = 1.0 / self.sample_rate
        t = self.t0
        for v in self.values:
            yield Sample(timestamp=t, value=v, channel=self.channel)
            t += dt

    def slice_indices(
        self, start: Optional[float], end: Optional[float]
    ) -> tuple[int, int]:
        """Return the (i0, i1) index range covering ``[start, end)``.

        Semantics: include sample ``k`` iff ``start <= t_k < end`` where
        ``t_k = t0 + k / sample_rate``. Bounds are clamped to the buffer's
        actual range. ``start=None`` defaults to ``t0``; ``end=None`` to
        ``t_end``. Returns a half-open interval suitable for slicing.
        """
        if not self.values or self.sample_rate <= 0:
            return (0, len(self.values))
        s = self.t0 if start is None else start
        e = self.t_end if end is None else end
        i0 = max(0, math.ceil((s - self.t0) * self.sample_rate))
        i1 = min(len(self.values), math.ceil((e - self.t0) * self.sample_rate))
        if i1 < i0:
            i1 = i0
        return (i0, i1)

    def view(
        self, start: Optional[float] = None, end: Optional[float] = None
    ) -> list[float]:
        """Sample values inside ``[start, end]``."""
        i0, i1 = self.slice_indices(start, end)
        return list(self.values[i0:i1])


# ---------------------------------------------------------------------------
# Statistics
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Statistics:
    """Summary statistics over a channel slice.

    `energy` (J) and `charge` (C) are only meaningful for power / current
    channels respectively. They are computed by integrating values over
    time and are populated when the channel's unit makes sense.
    """

    channel: Channel
    sample_count: int
    duration: float
    min: float
    max: float
    average: float
    rms: float
    energy: Optional[float] = None
    charge: Optional[float] = None


def compute_statistics(
    buffer: ChannelBuffer, start: Optional[float] = None, end: Optional[float] = None
) -> Statistics:
    """Compute summary stats over `buffer[start:end]`.

    Empty slices return a Statistics with `sample_count=0` and NaN
    aggregates; callers should check `sample_count` before using min/max.
    """
    i0, i1 = buffer.slice_indices(start, end)
    values = buffer.values[i0:i1]
    n = len(values)
    if n == 0:
        return Statistics(
            channel=buffer.channel,
            sample_count=0,
            duration=0.0,
            min=float("nan"),
            max=float("nan"),
            average=float("nan"),
            rms=float("nan"),
            energy=None,
            charge=None,
        )

    sr = buffer.sample_rate if buffer.sample_rate > 0 else 1
    dt = 1.0 / sr
    duration = n * dt

    vmin = vmax = values[0]
    total = 0.0
    sq_total = 0.0
    for v in values:
        if v < vmin:
            vmin = v
        if v > vmax:
            vmax = v
        total += v
        sq_total += v * v
    avg = total / n
    rms = math.sqrt(sq_total / n)

    unit = buffer.channel.unit
    charge = (total * dt) if unit == "A" else None
    energy = (total * dt) if unit == "W" else None

    return Statistics(
        channel=buffer.channel,
        sample_count=n,
        duration=duration,
        min=vmin,
        max=vmax,
        average=avg,
        rms=rms,
        energy=energy,
        charge=charge,
    )


# ---------------------------------------------------------------------------
# Export helpers
# ---------------------------------------------------------------------------


def write_csv_long(
    path: str | Path,
    buffers: dict[Channel, ChannelBuffer],
    *,
    decimals: int = 9,
) -> None:
    """Write a long-form CSV: timestamp,channel,value.

    Long form is robust against channels having different sample rates.
    """
    p = Path(path)
    with p.open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["timestamp_s", "channel", "value", "unit"])
        for ch in sorted(buffers.keys(), key=lambda c: c.wire_id):
            buf = buffers[ch]
            for s in buf.iter_samples():
                w.writerow(
                    [
                        f"{s.timestamp:.{decimals}f}",
                        ch.code,
                        f"{s.value:.{decimals}f}",
                        ch.unit,
                    ]
                )


def write_csv_wide(
    path: str | Path,
    buffers: dict[Channel, ChannelBuffer],
    *,
    decimals: int = 9,
) -> None:
    """Write a wide-form CSV with one column per channel.

    Channels are aligned to the highest sample rate; lower-rate channels
    are repeated to fill. Use long-form (:func:`write_csv_long`) if you
    need lossless preservation of each channel's native rate.
    """
    if not buffers:
        Path(path).write_text("timestamp_s\n", encoding="utf-8")
        return

    max_rate = max(b.sample_rate for b in buffers.values() if b.sample_rate > 0)
    if max_rate <= 0:
        max_rate = 1
    max_dur = max(b.duration for b in buffers.values())
    n_rows = max(1, math.ceil(max_dur * max_rate))
    dt = 1.0 / max_rate

    ordered_channels = sorted(buffers.keys(), key=lambda c: c.wire_id)
    with Path(path).open("w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        header = ["timestamp_s"] + [f"{c.code} ({c.unit})" for c in ordered_channels]
        w.writerow(header)
        for i in range(n_rows):
            t = i * dt
            row = [f"{t:.{decimals}f}"]
            for c in ordered_channels:
                buf = buffers[c]
                if buf.sample_rate <= 0 or not buf.values:
                    row.append("")
                    continue
                # nearest-neighbour sampling at the channel's native rate
                idx = min(
                    int((t - buf.t0) * buf.sample_rate),
                    len(buf.values) - 1,
                )
                if idx < 0:
                    row.append("")
                else:
                    row.append(f"{buf.values[idx]:.{decimals}f}")
            w.writerow(row)


def write_json(
    path: str | Path,
    buffers: dict[Channel, ChannelBuffer],
    stats: Optional[dict[Channel, Statistics]] = None,
    metadata: Optional[dict] = None,
) -> None:
    """Write a JSON sidecar with per-channel samples + optional stats."""
    payload: dict = {"channels": {}}
    if metadata:
        payload["metadata"] = metadata
    for ch, buf in buffers.items():
        block: dict = {
            "code": ch.code,
            "label": ch.label,
            "unit": ch.unit,
            "wire_id": ch.wire_id,
            "sample_rate": buf.sample_rate,
            "t0": buf.t0,
            "duration": buf.duration,
            "values": buf.values,
        }
        if stats and ch in stats:
            s = stats[ch]
            block["statistics"] = {
                "min": s.min,
                "max": s.max,
                "average": s.average,
                "rms": s.rms,
                "sample_count": s.sample_count,
                "duration": s.duration,
                "energy": s.energy,
                "charge": s.charge,
            }
        payload["channels"][ch.code] = block
    Path(path).write_text(json.dumps(payload, indent=2), encoding="utf-8")


def write_raw(path: str | Path, buf: bytes) -> None:
    Path(path).write_bytes(buf)

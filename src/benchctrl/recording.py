"""Recording — captured measurement data for one session.

A `Recording` is a self-contained, immutable-after-stop value object
holding per-channel sample buffers. It supports:

- per-channel info (`ChannelInfoResult`) and statistics (`Statistics`)
- per-channel data access (samples, timestamps, slicing)
- export to CSV (long/wide), JSON, raw bytes, and native binary
- in-memory `crop()`, `downsample()`, and `rename()` operations
- file load (`Recording.load(...)`) for previously-saved files
"""

from __future__ import annotations

import datetime as _dt
import json
import math
import struct
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from benchctrl.channels import Channel
from benchctrl.exceptions import BenchNotImplementedError, BenchValueError
from benchctrl.samples import (
    ChannelBuffer,
    Statistics,
    compute_statistics,
    write_csv_long,
    write_csv_wide,
    write_json,
    write_raw,
)

ChannelLike = Union[Channel, str]


@dataclass(frozen=True)
class ChannelInfoResult:
    """Mirrors the official ``recording.get_channel_info`` shape."""

    offset: float
    from_time: float  # alias of t0 (kept for parity with official `from`)
    to: float  # alias of t_end
    sample_rate: int
    count: int
    channel: Channel

    def as_dict(self) -> dict:
        """Return a dict compatible with the official API's response shape."""
        return {
            "offset": self.offset,
            "from": self.from_time,
            "to": self.to,
            "sample_rate": self.sample_rate,
        }


def _coerce(channel: ChannelLike) -> Channel:
    return Channel.coerce(channel)


class Recording:
    """A captured measurement session.

    Instances are typically created by :py:meth:`SMU.start_recording` or the
    :py:meth:`SMU.record` context manager. They can also be loaded from
    disk with :py:meth:`Recording.load`.

    Attributes:
        name: human-friendly label for the recording.
        offset: time offset (seconds) applied to all channels on access.
        device_info: optional metadata dict from the originating device.
        is_running: True while the underlying SMU is still streaming.
    """

    def __init__(
        self,
        name: str = "recording",
        offset: float = 0.0,
        device_info: Optional[dict] = None,
    ) -> None:
        self.name = name
        self.offset = offset
        self.device_info = device_info or {}
        self._buffers: dict[Channel, ChannelBuffer] = {}
        self._is_running = False
        self._start_time: Optional[_dt.datetime] = None
        self._end_time: Optional[_dt.datetime] = None
        # SMU stores a back-reference here while recording is active.
        # Set to None on stop so the SMU can be garbage-collected.
        self._smu_ref = None

    # --- streaming-side hooks (used by SMU; not part of public API) -----

    def _begin(self, smu) -> None:
        self._smu_ref = smu
        self._is_running = True
        self._start_time = _dt.datetime.now(_dt.timezone.utc)

    def _end(self) -> None:
        self._is_running = False
        self._end_time = _dt.datetime.now(_dt.timezone.utc)
        self._smu_ref = None

    def _ensure_buffer(self, channel: Channel, sample_rate: int) -> ChannelBuffer:
        buf = self._buffers.get(channel)
        if buf is None:
            buf = ChannelBuffer(channel=channel, sample_rate=sample_rate)
            self._buffers[channel] = buf
        return buf

    def _append(self, channel: Channel, value: float, sample_rate: int) -> None:
        self._ensure_buffer(channel, sample_rate).append(value)

    # --- public state ----------------------------------------------------

    @property
    def is_running(self) -> bool:
        return self._is_running

    @property
    def channels(self) -> list[Channel]:
        return list(self._buffers.keys())

    @property
    def start_time(self) -> Optional[_dt.datetime]:
        return self._start_time

    @property
    def end_time(self) -> Optional[_dt.datetime]:
        return self._end_time

    def buffer(self, channel: ChannelLike) -> ChannelBuffer:
        """Return the underlying :class:`ChannelBuffer` for `channel`."""
        ch = _coerce(channel)
        if ch not in self._buffers:
            raise BenchValueError(f"channel {ch.code!r} not present in recording")
        return self._buffers[ch]

    # --- query API (mirrors official client surface) --------------------

    def info(self, channel: ChannelLike) -> ChannelInfoResult:
        """Get info for a channel (offset, from, to, sample_rate, count)."""
        buf = self.buffer(channel)
        return ChannelInfoResult(
            offset=self.offset,
            from_time=buf.t0 + self.offset,
            to=buf.t_end + self.offset,
            sample_rate=buf.sample_rate,
            count=len(buf.values),
            channel=buf.channel,
        )

    def statistics(
        self,
        channel: ChannelLike,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> Statistics:
        """Compute summary statistics over the given time window."""
        buf = self.buffer(channel)
        # Convert from offset-relative time back to buffer-internal time
        s = None if start is None else start - self.offset
        e = None if end is None else end - self.offset
        return compute_statistics(buf, start=s, end=e)

    def data(
        self,
        channel: ChannelLike,
        start: Optional[float] = None,
        end: Optional[float] = None,
    ) -> list[float]:
        """Return raw sample values inside ``[start, end]``."""
        buf = self.buffer(channel)
        s = None if start is None else start - self.offset
        e = None if end is None else end - self.offset
        return buf.view(s, e)

    def timestamps(self, channel: ChannelLike) -> list[float]:
        """Return the synthetic time axis for `channel` (offset-adjusted)."""
        buf = self.buffer(channel)
        return [t + self.offset for t in buf.timestamps()]

    def index_at(self, channel: ChannelLike, timestamp: float) -> int:
        """Index of the sample at or just before `timestamp`."""
        buf = self.buffer(channel)
        if buf.sample_rate <= 0:
            return 0
        t = timestamp - self.offset - buf.t0
        return max(0, min(len(buf.values) - 1, math.floor(t * buf.sample_rate)))

    def count(self, channel: ChannelLike) -> int:
        """Number of samples for `channel`."""
        return len(self.buffer(channel).values)

    # --- mutation --------------------------------------------------------

    def crop(self, start: float, end: float) -> None:
        """In-place crop. Each channel buffer is trimmed to ``[start, end]``."""
        if end < start:
            raise BenchValueError(f"end ({end}) < start ({start})")
        for buf in self._buffers.values():
            s = start - self.offset
            e = end - self.offset
            i0, i1 = buf.slice_indices(s, e)
            buf.values = buf.values[i0:i1]
            # Shift t0 so values[0] still aligns with `start` (after offset)
            dt = 1.0 / buf.sample_rate if buf.sample_rate > 0 else 0.0
            buf.t0 = buf.t0 + i0 * dt

    def downsample(self, channel: ChannelLike, factor: int) -> None:
        """Downsample `channel` by integer `factor` (averaging window)."""
        if factor <= 0:
            raise BenchValueError(f"factor must be >= 1, got {factor}")
        if factor == 1:
            return
        buf = self.buffer(channel)
        out: list[float] = []
        n = len(buf.values)
        for i in range(0, n, factor):
            window = buf.values[i : i + factor]
            if window:
                out.append(sum(window) / len(window))
        buf.values = out
        buf.sample_rate = max(1, buf.sample_rate // factor)

    def rename(self, name: str) -> None:
        self.name = name

    def log(self, text: str, timestamp: float = 0.0) -> None:
        """Append a free-text annotation to the recording's notes."""
        notes = self.device_info.setdefault("notes", [])
        notes.append({"timestamp": timestamp, "text": text})

    # --- export / serialise ---------------------------------------------

    def save_csv(
        self,
        path: str | Path,
        format: str = "long",
        decimals: int = 9,
    ) -> Path:
        """Write samples to CSV. ``format`` is 'long' or 'wide'."""
        p = Path(path)
        if format == "long":
            write_csv_long(p, self._buffers, decimals=decimals)
        elif format == "wide":
            write_csv_wide(p, self._buffers, decimals=decimals)
        else:
            raise BenchValueError(f"format must be 'long' or 'wide', got {format!r}")
        return p

    def save_json(self, path: str | Path) -> Path:
        """Write samples + statistics to a JSON sidecar."""
        stats = {ch: compute_statistics(buf) for ch, buf in self._buffers.items()}
        write_json(
            path,
            self._buffers,
            stats=stats,
            metadata={
                "name": self.name,
                "offset": self.offset,
                "device": self.device_info,
                "start_time": (
                    self._start_time.isoformat() + "Z"
                    if self._start_time
                    else None
                ),
                "end_time": (
                    self._end_time.isoformat() + "Z" if self._end_time else None
                ),
            },
        )
        return Path(path)

    def save_raw(self, path: str | Path, buf: bytes) -> Path:
        """Persist a raw inbound capture buffer."""
        write_raw(path, buf)
        return Path(path)

    # --- data-science conveniences (optional dependencies) --------------
    #
    # Each method below is gated by a lazy import — the optional dep is
    # only loaded when the method is actually called. benchctrl itself
    # imports clean without any of numpy / pandas / pyarrow / matplotlib
    # installed; you only need them if you call the corresponding
    # method. Each lazy import is wrapped to produce a clear, actionable
    # error message pointing to the right extras key.

    def to_numpy(self, channel: ChannelLike):
        """Return this channel's samples as a 1-D ``numpy.ndarray`` (float32).

        **Optional dependency**: ``numpy``. Install with
        ``pip install benchctrl[numpy]``. benchctrl itself imports clean
        without numpy — you only need it if you call this method.

        See :py:meth:`timestamps_numpy` for the matching time axis.
        """
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover - environment-dependent
            raise ImportError(
                "to_numpy() requires numpy. "
                "Install with: pip install 'benchctrl[numpy]'"
            ) from e

        buf = self.buffer(channel)
        return np.asarray(buf.values, dtype=np.float32)

    def timestamps_numpy(self, channel: ChannelLike):
        """Return this channel's synthetic time axis as a 1-D ``numpy.ndarray``.

        **Optional dependency**: ``numpy``. Install with
        ``pip install benchctrl[numpy]``.

        Offset-adjusted to match :py:meth:`timestamps`.
        """
        try:
            import numpy as np
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "timestamps_numpy() requires numpy. "
                "Install with: pip install 'benchctrl[numpy]'"
            ) from e

        buf = self.buffer(channel)
        n = len(buf.values)
        if n == 0 or buf.sample_rate <= 0:
            return np.zeros(n, dtype=np.float64) + buf.t0 + self.offset
        dt = 1.0 / buf.sample_rate
        return (buf.t0 + self.offset) + np.arange(n, dtype=np.float64) * dt

    def to_pandas(self, channel: Optional[ChannelLike] = None):
        """Return a ``pandas`` object holding the recording's samples.

        - If ``channel`` is given, returns a :class:`pandas.Series` named
          for the channel code, indexed by the channel's timestamps.
        - If ``channel`` is None, returns a wide :class:`pandas.DataFrame`
          with one column per channel, indexed at the highest channel's
          rate. Lower-rate channels carry ``NaN`` at timestamps that
          don't align with their native rate — use ``.ffill()`` if you
          want forward-filled values.

        **Optional dependency**: ``pandas`` (which pulls in ``numpy``).
        Install with ``pip install benchctrl[pandas]``.
        """
        try:
            import pandas as pd
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "to_pandas() requires pandas. "
                "Install with: pip install 'benchctrl[pandas]'"
            ) from e

        if channel is not None:
            ch = _coerce(channel)
            buf = self.buffer(ch)
            return pd.Series(
                buf.values,
                index=pd.Index(
                    [t + self.offset for t in buf.timestamps()],
                    name="timestamp_s",
                ),
                name=ch.code,
                dtype="float32",
            )

        # Wide DataFrame across all channels — outer join on the union of timestamps
        if not self._buffers:
            return pd.DataFrame(
                {}, index=pd.Index([], name="timestamp_s", dtype="float64")
            )
        series = [self.to_pandas(ch) for ch in sorted(self._buffers, key=lambda c: c.wire_id)]
        df = pd.concat(series, axis=1)
        df.index.name = "timestamp_s"
        return df

    def save_parquet(
        self,
        path: str | Path,
        *,
        compression: str = "snappy",
    ) -> Path:
        """Write the recording to an Apache Parquet file.

        Layout: wide form (same shape as :py:meth:`to_pandas` with no
        channel argument) — one column per channel, ``timestamp_s`` as
        the index. Stored columnar with the chosen compression
        (``"snappy"`` by default; also accepts ``"zstd"``, ``"gzip"``,
        ``"brotli"``, or ``"none"``).

        Loads cleanly in pandas (``pd.read_parquet``), polars
        (``pl.read_parquet``), duckdb (``read_parquet``), and Apache
        Arrow tooling. Compact and portable — typically 10-20× smaller
        than the equivalent CSV.

        **Optional dependency**: ``pyarrow`` (and ``pandas`` for the
        DataFrame construction). Install with
        ``pip install benchctrl[parquet]``.
        """
        try:
            import pyarrow as pa
            import pyarrow.parquet as pq
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "save_parquet() requires pyarrow. "
                "Install with: pip install 'benchctrl[parquet]'"
            ) from e

        df = self.to_pandas().reset_index()
        # Embed channel units + labels in column metadata so consumers
        # can recover unit context without a sidecar.
        col_meta = {}
        for ch in sorted(self._buffers, key=lambda c: c.wire_id):
            col_meta[ch.code] = {
                "unit": ch.unit,
                "label": ch.label,
                "wire_id": str(ch.wire_id),
                "sample_rate_hz": str(ch.sample_rate),
            }
        table = pa.Table.from_pandas(df, preserve_index=False)
        table = table.replace_schema_metadata(
            {
                **(table.schema.metadata or {}),
                b"benchctrl.recording_name": self.name.encode("utf-8"),
                b"benchctrl.offset_s": str(self.offset).encode("utf-8"),
                b"benchctrl.columns": json.dumps(col_meta).encode("utf-8"),
            }
        )
        p = Path(path)
        pq.write_table(table, p, compression=compression)
        return p

    def plot(
        self,
        channels: Optional[list[ChannelLike]] = None,
        *,
        show: bool = True,
        title: Optional[str] = None,
    ):
        """Quick-look matplotlib plot — one subplot per channel, shared x-axis.

        Returns the :class:`matplotlib.figure.Figure` for further
        customisation (annotations, save to disk, etc.). With
        ``show=True`` (default) the figure is displayed via
        ``plt.show()``; pass ``show=False`` in headless / batch contexts.

        **Optional dependency**: ``matplotlib``. Install with
        ``pip install benchctrl[plot]``.
        """
        try:
            import matplotlib.pyplot as plt
        except ImportError as e:  # pragma: no cover
            raise ImportError(
                "plot() requires matplotlib. "
                "Install with: pip install 'benchctrl[plot]'"
            ) from e

        if channels is None:
            chs = sorted(self._buffers.keys(), key=lambda c: c.wire_id)
        else:
            chs = [_coerce(c) for c in channels]
        if not chs:
            raise BenchValueError("no channels to plot")

        fig, axes = plt.subplots(
            len(chs), 1, sharex=True, figsize=(10, max(2.0, 2.0 * len(chs)))
        )
        if len(chs) == 1:
            axes = [axes]
        for ax, ch in zip(axes, chs):
            buf = self.buffer(ch)
            ts = self.timestamps_numpy(ch)
            ax.plot(ts, buf.values, linewidth=0.8, label=ch.label)
            ax.set_ylabel(f"{ch.label}\n({ch.unit})", rotation=0, ha="right", va="center")
            ax.grid(True, alpha=0.3)
        axes[-1].set_xlabel("time (s)")
        fig.suptitle(title or self.name)
        fig.tight_layout()
        if show:
            plt.show()
        return fig

    # --- native binary format ('.opensmu') ------------------------------

    BENCHCTRL_MAGIC = b"OSMU\x00\x00\x00\x01"  # 8 B — schema version 1

    def save(self, path: str | Path) -> Path:
        """Save to a self-contained `.opensmu` binary file.

        Layout (version 1):
            8 B   magic 'OSMU\\x00\\x00\\x00\\x01'
            4 B   header length (u32 LE)
            N B   JSON-encoded header
            for each channel:
                4 B   block-header length (u32 LE)
                M B   JSON-encoded block header
                K B   block data (count * 4 bytes, float32 LE)
        """
        p = Path(path)
        header = {
            "schema_version": 1,
            "name": self.name,
            "offset": self.offset,
            "device": self.device_info,
            "start_time": (
                self._start_time.isoformat() + "Z" if self._start_time else None
            ),
            "end_time": (
                self._end_time.isoformat() + "Z" if self._end_time else None
            ),
            "channel_order": [ch.code for ch in self._buffers.keys()],
        }
        header_bytes = json.dumps(header).encode("utf-8")
        with p.open("wb") as f:
            f.write(self.BENCHCTRL_MAGIC)
            f.write(struct.pack("<I", len(header_bytes)))
            f.write(header_bytes)
            for ch, buf in self._buffers.items():
                block_header = {
                    "code": ch.code,
                    "wire_id": ch.wire_id,
                    "unit": ch.unit,
                    "label": ch.label,
                    "count": len(buf.values),
                    "sample_rate": buf.sample_rate,
                    "t0": buf.t0,
                    "dtype": "f32",
                }
                bh = json.dumps(block_header).encode("utf-8")
                f.write(struct.pack("<I", len(bh)))
                f.write(bh)
                if buf.values:
                    f.write(struct.pack(f"<{len(buf.values)}f", *buf.values))
        return p

    @classmethod
    def load(cls, path: str | Path) -> Recording:
        """Inverse of :py:meth:`save`."""
        p = Path(path)
        with p.open("rb") as f:
            magic = f.read(8)
            if magic != cls.BENCHCTRL_MAGIC:
                raise BenchValueError(
                    f"{p}: not an .opensmu file (bad magic {magic!r})"
                )
            (hlen,) = struct.unpack("<I", f.read(4))
            header = json.loads(f.read(hlen).decode("utf-8"))
            rec = cls(
                name=header.get("name", "recording"),
                offset=float(header.get("offset", 0.0)),
                device_info=header.get("device", {}),
            )
            if header.get("start_time"):
                rec._start_time = _dt.datetime.fromisoformat(
                    header["start_time"].rstrip("Z")
                )
            if header.get("end_time"):
                rec._end_time = _dt.datetime.fromisoformat(
                    header["end_time"].rstrip("Z")
                )
            while True:
                chunk = f.read(4)
                if not chunk:
                    break
                (bhlen,) = struct.unpack("<I", chunk)
                bh = json.loads(f.read(bhlen).decode("utf-8"))
                ch = Channel.from_code(bh["code"])
                buf = rec._ensure_buffer(ch, int(bh["sample_rate"]))
                buf.t0 = float(bh.get("t0", 0.0))
                count = int(bh["count"])
                if count:
                    raw = f.read(count * 4)
                    buf.values.extend(struct.unpack(f"<{count}f", raw))
        return rec

    # --- placeholders for deferred features -----------------------------

    def get_log_offset(self, channel: ChannelLike) -> float:
        raise BenchNotImplementedError(
            "log offsets are a deferred feature — see ROADMAP.md"
        )

    def set_log_offset(self, channel: ChannelLike, offset: float) -> None:
        raise BenchNotImplementedError(
            "log offsets are a deferred feature — see ROADMAP.md"
        )

    def import_log(self, filename: str | Path, converter: str) -> str:
        raise BenchNotImplementedError(
            "log import is a deferred feature — see ROADMAP.md"
        )

    def append_user_log(self, user_log_id: str, time: float, message: str) -> None:
        raise BenchNotImplementedError(
            "user logs are a deferred feature — see ROADMAP.md"
        )

    # --- ergonomic dunders -----------------------------------------------

    def __repr__(self) -> str:
        return (
            f"Recording(name={self.name!r}, channels={len(self._buffers)}, "
            f"running={self._is_running})"
        )

    def __contains__(self, channel: ChannelLike) -> bool:
        try:
            return _coerce(channel) in self._buffers
        except KeyError:
            return False

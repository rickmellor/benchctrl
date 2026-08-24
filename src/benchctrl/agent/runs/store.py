"""Durable run state: SQLite, an append-only event mirror, and chunk files.

A long run outlives its client, and may outlive the agent process. Anything
that only exists in memory is lost at exactly the moment it mattered most,
so events, metrics and chunk metadata are committed as they happen.

``events.ndjson`` is deliberately redundant with the ``event`` table. SQLite
with WAL survives a crash; it does not reliably survive an SD card losing
power mid-write, which is a real thing on a board someone unplugs. One
fsync'd append per event is cheap — events are rare by design — and it means
the narrative of what happened survives even if the database does not.

Everything lands under ``/home/arduino`` on the board. The root partition
has under 2 GB free and is shared with Docker images and App Lab's models;
filling it takes down the whole board, not just the run.
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
import threading
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterator, Optional

log = logging.getLogger("benchctrl.agent.runs.store")

SCHEMA = """
CREATE TABLE IF NOT EXISTS run(
  run_id TEXT PRIMARY KEY, name TEXT, device TEXT, dut TEXT,
  spec_json TEXT, spec_sha256 TEXT,
  created_utc TEXT, started_utc TEXT, finished_utc TEXT,
  status TEXT, stop_reason TEXT, current_phase INTEGER,
  last_seq INTEGER DEFAULT 0, agent_version TEXT,
  schema_version INTEGER, boot_id TEXT);

CREATE TABLE IF NOT EXISTS phase(
  run_id TEXT, idx INTEGER, name TEXT, mode TEXT,
  started_utc TEXT, ended_utc TEXT, started_mono REAL,
  status TEXT, exit_reason TEXT, setpoints_json TEXT,
  PRIMARY KEY(run_id, idx));

CREATE TABLE IF NOT EXISTS event(
  run_id TEXT, seq INTEGER, ts_mono REAL, ts_utc TEXT, phase_idx INTEGER,
  kind TEXT, severity TEXT, source TEXT, payload_json TEXT,
  PRIMARY KEY(run_id, seq));
CREATE INDEX IF NOT EXISTS ix_event_time ON event(run_id, ts_mono);

CREATE TABLE IF NOT EXISTS metric(
  run_id TEXT, ts_mono REAL, phase_idx INTEGER, ch TEXT,
  n INTEGER, vmin REAL, vmax REAL, vmean REAL, vlast REAL);
CREATE INDEX IF NOT EXISTS ix_metric_time ON metric(run_id, ts_mono, ch);

CREATE TABLE IF NOT EXISTS chunk(
  run_id TEXT, idx INTEGER, path TEXT, phase_idx INTEGER,
  started_utc TEXT, ended_utc TEXT, bytes INTEGER, sha256 TEXT,
  stats_json TEXT, PRIMARY KEY(run_id, idx));

CREATE TABLE IF NOT EXISTS artifact(
  run_id TEXT, name TEXT, path TEXT, kind TEXT, sha256 TEXT,
  PRIMARY KEY(run_id, name));

CREATE TABLE IF NOT EXISTS analysis(
  run_id TEXT PRIMARY KEY, verdict TEXT, evaluated_utc TEXT,
  result_json TEXT);
"""

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_COMPLETE = "complete"
STATUS_ABORTED = "aborted"
STATUS_ERRORED = "errored"
STATUS_SAFE_STOPPED = "safe_stopped"
STATUS_INTERRUPTED = "interrupted"

#: Statuses a run reaches when its spec declared acceptance criteria and the
#: bench evaluated them. They *replace* ``complete`` rather than sitting beside
#: it, and only for a run that ran to the end: every other terminal status
#: describes how the engine exited, which is a different question and the more
#: urgent one. A safe-stopped run that happens to satisfy its checks is still
#: safe-stopped, and burying that under a green PASS would be the worst thing
#: this feature could do.
#:
#: ``inconclusive`` deliberately has no status of its own — a run whose checks
#: could not be evaluated stays ``complete``, because the engine did complete and
#: the verdict is carried separately. Promoting "we could not judge" to a terminal
#: status would make it look like an engine outcome.
STATUS_PASSED = "passed"
STATUS_FAILED = "failed"

TERMINAL = (
    STATUS_COMPLETE,
    STATUS_ABORTED,
    STATUS_ERRORED,
    STATUS_SAFE_STOPPED,
    STATUS_PASSED,
    STATUS_FAILED,
)

#: Terminal statuses meaning the engine ran every phase to the end. The gate on
#: whether acceptance criteria are evaluated at all: judging a DUT on a run that
#: was aborted half way through would produce a verdict about a test that did not
#: happen.
COMPLETED_OK = (STATUS_COMPLETE, STATUS_PASSED, STATUS_FAILED)


def default_runs_dir() -> Path:
    """``/home/arduino/benchctrl/runs`` on the board, else under the cwd."""
    board = Path("/home/arduino")
    if board.is_dir() and os.access(board, os.W_OK):
        return board / "benchctrl" / "runs"
    return Path.cwd() / "benchctrl-runs"


def boot_id() -> str:
    """Identifies this boot, so an interrupted run is distinguishable.

    A run marked ``running`` whose ``boot_id`` differs from the current one
    did not stop cleanly — the machine went down underneath it.
    """
    try:
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()
    except OSError:
        return f"pid-{os.getpid()}"


@dataclass
class Event:
    seq: int
    kind: str
    severity: str = "info"
    source: str = "engine"
    phase_idx: int = -1
    payload: dict = None  # type: ignore[assignment]
    ts_mono: float = 0.0
    ts_utc: str = ""

    def to_dict(self) -> dict:
        return {
            "seq": self.seq,
            "kind": self.kind,
            "severity": self.severity,
            "source": self.source,
            "phase_idx": self.phase_idx,
            "ts_mono": self.ts_mono,
            "ts_utc": self.ts_utc,
            "data": self.payload or {},
        }


class RunStore:
    """One run's durable state, on disk."""

    def __init__(self, run_dir: Path, run_id: str) -> None:
        self.run_dir = Path(run_dir)
        self.run_id = run_id
        self.run_dir.mkdir(parents=True, exist_ok=True)
        self.data_dir = self.run_dir / "data"
        self.data_dir.mkdir(exist_ok=True)
        self.db_path = self.run_dir / "run.db"
        self.events_path = self.run_dir / "events.ndjson"
        self.notes_path = self.run_dir / "notes.md"
        self._lock = threading.RLock()
        self._seq = 0
        self._conn = sqlite3.connect(str(self.db_path), check_same_thread=False)
        self._conn.executescript(SCHEMA)
        self._conn.execute("PRAGMA journal_mode=WAL")
        self._conn.commit()

    # --- run lifecycle --------------------------------------------------

    def create(self, spec, *, agent_version: str = "") -> None:
        now = _utc()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO run(run_id,name,device,dut,spec_json,"
                "spec_sha256,created_utc,status,current_phase,last_seq,"
                "agent_version,schema_version,boot_id) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    spec.name,
                    spec.device,
                    spec.dut,
                    spec.to_json(),
                    spec.sha256,
                    now,
                    STATUS_PENDING,
                    -1,
                    0,
                    agent_version,
                    1,
                    boot_id(),
                ),
            )
            self._conn.commit()
        (self.run_dir / "spec.json").write_text(spec.to_json())

    def set_status(
        self,
        status: str,
        *,
        stop_reason: str = "",
        phase_idx: Optional[int] = None,
    ) -> None:
        with self._lock:
            fields = ["status=?", "stop_reason=?"]
            values: list[Any] = [status, stop_reason]
            if status == STATUS_RUNNING:
                fields.append("started_utc=COALESCE(started_utc,?)")
                values.append(_utc())
            if status in TERMINAL or status == STATUS_INTERRUPTED:
                fields.append("finished_utc=?")
                values.append(_utc())
            if phase_idx is not None:
                fields.append("current_phase=?")
                values.append(phase_idx)
            values.append(self.run_id)
            self._conn.execute(
                f"UPDATE run SET {','.join(fields)} WHERE run_id=?", values
            )
            self._conn.commit()

    def info(self) -> dict:
        with self._lock:
            row = self._conn.execute(
                "SELECT run_id,name,device,dut,spec_sha256,created_utc,started_utc,"
                "finished_utc,status,stop_reason,current_phase,last_seq,boot_id "
                "FROM run WHERE run_id=?",
                (self.run_id,),
            ).fetchone()
        if row is None:
            return {}
        keys = (
            "run_id", "name", "device", "dut", "spec_sha256", "created_utc",
            "started_utc", "finished_utc", "status", "stop_reason",
            "current_phase", "last_seq", "boot_id",
        )
        return dict(zip(keys, row))

    def spec_json(self) -> str:
        with self._lock:
            row = self._conn.execute(
                "SELECT spec_json FROM run WHERE run_id=?", (self.run_id,)
            ).fetchone()
        return row[0] if row else "{}"

    # --- events ---------------------------------------------------------

    def append_event(
        self,
        kind: str,
        *,
        severity: str = "info",
        source: str = "engine",
        phase_idx: int = -1,
        payload: Optional[dict] = None,
    ) -> Event:
        """Persist one event and return it, with its sequence number.

        The sequence is assigned inside the same transaction that writes the
        row, so a reconnecting client using ``since_seq`` can never miss an
        event or see one twice.
        """
        with self._lock:
            self._seq += 1
            event = Event(
                seq=self._seq,
                kind=kind,
                severity=severity,
                source=source,
                phase_idx=phase_idx,
                payload=payload or {},
                ts_mono=round(time.monotonic(), 6),
                ts_utc=_utc(),
            )
            self._conn.execute(
                "INSERT INTO event(run_id,seq,ts_mono,ts_utc,phase_idx,kind,"
                "severity,source,payload_json) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    event.seq,
                    event.ts_mono,
                    event.ts_utc,
                    phase_idx,
                    kind,
                    severity,
                    source,
                    json.dumps(event.payload, separators=(",", ":")),
                ),
            )
            self._conn.execute(
                "UPDATE run SET last_seq=? WHERE run_id=?", (event.seq, self.run_id)
            )
            self._conn.commit()
            self._mirror(event)
        return event

    def _mirror(self, event: Event) -> None:
        """Append to the crash-resistant NDJSON mirror."""
        try:
            with self.events_path.open("a") as fh:
                fh.write(json.dumps(event.to_dict(), separators=(",", ":")) + "\n")
                fh.flush()
                os.fsync(fh.fileno())
        except OSError as exc:  # pragma: no cover
            log.warning("could not mirror event to %s: %s", self.events_path, exc)

    def events_since(self, since_seq: int = 0, limit: int = 1000) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq,ts_mono,ts_utc,phase_idx,kind,severity,source,"
                "payload_json FROM event WHERE run_id=? AND seq>? "
                "ORDER BY seq LIMIT ?",
                (self.run_id, since_seq, limit),
            ).fetchall()
        return [
            {
                "seq": r[0],
                "ts_mono": r[1],
                "ts_utc": r[2],
                "phase_idx": r[3],
                "kind": r[4],
                "severity": r[5],
                "source": r[6],
                "data": json.loads(r[7]),
            }
            for r in rows
        ]

    def recent_events(self, limit: int = 20) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT seq FROM event WHERE run_id=? ORDER BY seq DESC LIMIT ?",
                (self.run_id, limit),
            ).fetchall()
        if not rows:
            return []
        return self.events_since(min(r[0] for r in rows) - 1, limit=limit)

    # --- phases ---------------------------------------------------------

    def start_phase(self, idx: int, phase) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO phase(run_id,idx,name,mode,started_utc,"
                "started_mono,status,setpoints_json) VALUES(?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    idx,
                    phase.name,
                    phase.mode,
                    _utc(),
                    time.monotonic(),
                    STATUS_RUNNING,
                    json.dumps(phase.setpoints),
                ),
            )
            self._conn.execute(
                "UPDATE run SET current_phase=? WHERE run_id=?", (idx, self.run_id)
            )
            self._conn.commit()

    def end_phase(self, idx: int, *, status: str, exit_reason: str = "") -> None:
        with self._lock:
            self._conn.execute(
                "UPDATE phase SET ended_utc=?,status=?,exit_reason=? "
                "WHERE run_id=? AND idx=?",
                (_utc(), status, exit_reason, self.run_id, idx),
            )
            self._conn.commit()

    def phases(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx,name,mode,started_utc,ended_utc,status,exit_reason "
                "FROM phase WHERE run_id=? ORDER BY idx",
                (self.run_id,),
            ).fetchall()
        keys = ("idx", "name", "mode", "started_utc", "ended_utc", "status", "exit_reason")
        return [dict(zip(keys, r)) for r in rows]

    # --- metrics --------------------------------------------------------

    def append_metric(
        self, phase_idx: int, channel: str, stats: dict, *, ts_mono: Optional[float] = None
    ) -> None:
        with self._lock:
            self._conn.execute(
                "INSERT INTO metric(run_id,ts_mono,phase_idx,ch,n,vmin,vmax,"
                "vmean,vlast) VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    ts_mono if ts_mono is not None else time.monotonic(),
                    phase_idx,
                    channel,
                    stats.get("n", 0),
                    stats.get("min"),
                    stats.get("max"),
                    stats.get("mean"),
                    stats.get("last"),
                ),
            )
            self._conn.commit()

    def metric_window(self, channel: str, seconds: float) -> list[dict]:
        cutoff = time.monotonic() - seconds
        with self._lock:
            rows = self._conn.execute(
                "SELECT ts_mono,n,vmin,vmax,vmean,vlast FROM metric "
                "WHERE run_id=? AND ch=? AND ts_mono>=? ORDER BY ts_mono",
                (self.run_id, channel, cutoff),
            ).fetchall()
        keys = ("ts_mono", "n", "min", "max", "mean", "last")
        return [dict(zip(keys, r)) for r in rows]

    def metric_aggregate(
        self, channel: str, agg: str, *, phase_idx: Optional[int] = None
    ) -> Optional[float]:
        """One aggregate of everything recorded on ``channel``, or None.

        None means "no rows", which the caller must keep distinct from a value of
        zero: an acceptance check against an unrecorded channel is inconclusive,
        and ``0.0`` would satisfy most thresholds anyone writes.

        Aggregated in SQL over the ``metric`` rows rather than over the recording
        chunks. The rows are what the engine already wrote while the run was
        happening — one per channel per metric period — so this reads a few hundred
        floats where the chunks are hundreds of megabytes the board cannot hold at
        once. It is a decimated view of the recording and is honest about being
        one: the aggregate is over sampled points, not over every point the Arc
        captured.

        ``min``/``max``/``mean`` aggregate the corresponding per-row column, so an
        aggregate-of-aggregates. With the engine writing ``n=1`` rows whose five
        columns are all the same reading, those coincide with the true sampled
        figures. They would diverge if a caller ever wrote pre-summarised rows with
        ``n>1``, where the mean of means is unweighted — noted rather than
        corrected, because nothing writes such rows today and a weighted mean
        computed from columns that may be NULL is the kind of arithmetic that
        silently produces a plausible wrong number.

        ``last`` is the most recent row by time, not an aggregate: the ordering is
        what makes it meaningful, and ``MAX(vlast)`` would quietly answer a
        different question.
        """
        columns = {"min": "MIN(vmin)", "max": "MAX(vmax)", "mean": "AVG(vmean)"}
        where = "run_id=? AND ch=?"
        params: list[Any] = [self.run_id, channel]
        if phase_idx is not None:
            where += " AND phase_idx=?"
            params.append(phase_idx)
        if agg == "last":
            sql = (
                f"SELECT vlast FROM metric WHERE {where} AND vlast IS NOT NULL "
                f"ORDER BY ts_mono DESC LIMIT 1"
            )
        elif agg in columns:
            sql = f"SELECT {columns[agg]} FROM metric WHERE {where}"
        else:
            raise ValueError(f"unknown metric aggregate {agg!r}")
        with self._lock:
            row = self._conn.execute(sql, params).fetchone()
        if row is None or row[0] is None:
            return None
        return float(row[0])

    def latest_metric(self, channel: str) -> Optional[dict]:
        with self._lock:
            row = self._conn.execute(
                "SELECT ts_mono,n,vmin,vmax,vmean,vlast FROM metric "
                "WHERE run_id=? AND ch=? ORDER BY ts_mono DESC LIMIT 1",
                (self.run_id, channel),
            ).fetchone()
        if row is None:
            return None
        return dict(zip(("ts_mono", "n", "min", "max", "mean", "last"), row))

    # --- chunks ---------------------------------------------------------

    def next_chunk_index(self) -> int:
        """Continue numbering after a resume rather than overwriting."""
        with self._lock:
            row = self._conn.execute(
                "SELECT MAX(idx) FROM chunk WHERE run_id=?", (self.run_id,)
            ).fetchone()
        return 0 if row is None or row[0] is None else int(row[0]) + 1

    def write_chunk(
        self, recording, phase_idx: int, *, stats: Optional[dict] = None
    ) -> dict:
        """Persist a recording chunk as ``.opensmu`` and register it."""
        import hashlib

        idx = self.next_chunk_index()
        path = self.data_dir / f"chunk{idx:03d}.opensmu"
        data = recording.to_bytes()
        path.write_bytes(data)
        digest = hashlib.sha256(data).hexdigest()
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO chunk(run_id,idx,path,phase_idx,"
                "started_utc,ended_utc,bytes,sha256,stats_json) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (
                    self.run_id,
                    idx,
                    str(path),
                    phase_idx,
                    _utc(),
                    _utc(),
                    len(data),
                    digest,
                    json.dumps(stats or {}),
                ),
            )
            self._conn.commit()
        return {"idx": idx, "path": str(path), "bytes": len(data), "sha256": digest}

    def chunks(self) -> list[dict]:
        with self._lock:
            rows = self._conn.execute(
                "SELECT idx,path,phase_idx,bytes,sha256 FROM chunk "
                "WHERE run_id=? ORDER BY idx",
                (self.run_id,),
            ).fetchall()
        return [
            dict(zip(("idx", "path", "phase_idx", "bytes", "sha256"), r)) for r in rows
        ]

    # --- notes / artifacts ----------------------------------------------

    def append_note(self, text: str, *, heading: str = "") -> None:
        with self.notes_path.open("a") as fh:
            if heading:
                fh.write(f"\n## {heading}\n\n")
            fh.write(text.rstrip() + "\n")

    # --- analysis -------------------------------------------------------

    def set_analysis(self, result: dict) -> None:
        """Record the acceptance verdict and every check behind it.

        Stored whole rather than as a column per check: the shape is the spec
        author's, one row per criterion they wrote, and a schema that had to change
        when someone added a check would put a migration between an operator and
        their test. The verdict is lifted out to its own column because that is the
        one field anything queries.
        """
        with self._lock:
            self._conn.execute(
                "INSERT OR REPLACE INTO analysis(run_id,verdict,evaluated_utc,"
                "result_json) VALUES(?,?,?,?)",
                (
                    self.run_id,
                    str(result.get("verdict", "")),
                    _utc(),
                    json.dumps(result, separators=(",", ":")),
                ),
            )
            self._conn.commit()

    def analysis(self) -> Optional[dict]:
        """The recorded verdict, or None if this run was never judged.

        None is the answer for every run whose spec declared no criteria, and it
        must stay distinguishable from a verdict: "nobody said what passing means"
        is not a result, and a display that rendered it as one would put a word
        where there is no judgement.
        """
        with self._lock:
            row = self._conn.execute(
                "SELECT result_json FROM analysis WHERE run_id=?", (self.run_id,)
            ).fetchone()
        if row is None or not row[0]:
            return None
        try:
            loaded = json.loads(row[0])
        except json.JSONDecodeError:  # pragma: no cover
            log.warning("analysis row for %s is not valid JSON", self.run_id)
            return None
        return loaded if isinstance(loaded, dict) else None

    def write_manifest(self) -> Path:
        manifest = {
            "run": self.info(),
            "phases": self.phases(),
            "chunks": self.chunks(),
            "event_count": len(self.events_since(0, limit=100_000)),
        }
        # Only when there is one, for the reason ``analysis()`` returns None: the
        # bundle must not carry a key that reads as a verdict on a run nobody
        # declared criteria for. A reader can then treat presence as the question
        # "was this run judged" without inspecting the value.
        judged = self.analysis()
        if judged is not None:
            manifest["analysis"] = judged
        path = self.run_dir / "manifest.json"
        path.write_text(json.dumps(manifest, indent=2))
        return path

    def close(self) -> None:
        with self._lock:
            try:
                self._conn.close()
            except sqlite3.Error:
                pass


def new_run_id(name: str) -> str:
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime())
    safe = "".join(c if c.isalnum() or c in "-_" else "-" for c in name)[:40]
    return f"{stamp}-{safe}-{uuid.uuid4().hex[:6]}"


def list_runs(runs_dir: Path) -> list[dict]:
    """Summarise every run under ``runs_dir``."""
    runs_dir = Path(runs_dir)
    if not runs_dir.is_dir():
        return []
    out = []
    for child in sorted(runs_dir.iterdir(), reverse=True):
        db = child / "run.db"
        if not db.is_file():
            continue
        try:
            store = RunStore(child, child.name)
            info = store.info()
            store.close()
            if info:
                out.append(info)
        except sqlite3.Error as exc:  # pragma: no cover
            log.warning("could not read run at %s: %s", child, exc)
    return out


def reconcile_interrupted(runs_dir: Path) -> list[str]:
    """Mark runs that were live when the machine went down.

    A run still ``running`` under a previous boot id did not stop cleanly.
    It is never silently resumed: after a power cut the DUT's state is
    unknown, so an operator has to decide.
    """
    current = boot_id()
    touched = []
    for info in list_runs(runs_dir):
        if info.get("status") == STATUS_RUNNING and info.get("boot_id") != current:
            store = RunStore(Path(runs_dir) / info["run_id"], info["run_id"])
            store.set_status(STATUS_INTERRUPTED, stop_reason="agent or board restarted")
            store.append_event(
                "run_interrupted",
                severity="alarm",
                payload={"previous_boot": info.get("boot_id")},
            )
            store.close()
            touched.append(info["run_id"])
            log.warning("run %s was interrupted by a restart", info["run_id"])
    return touched


def _utc() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())

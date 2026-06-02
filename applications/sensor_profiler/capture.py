"""Generic DUT power-profile capture.

CLI:
    python capture.py --dut <slug> --label <label> [options]

Drives the Otii Arc Pro as a CV battery source and records the DUT's
current draw into hourly-rolled ``.opensmu`` chunks. Every capture
produces a self-contained run folder under ``runs/`` with a
``metadata.json`` describing the configuration and current status.

Run id format: ``<UTC>_<dut-slug>_<label>``, e.g.
``2026-05-31T190134Z_room-temp-sensor_assoc-test``.
"""
from __future__ import annotations

import argparse
import datetime as _dt
import json
import re
import signal
import sys
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Optional

from benchctrl.drivers.otii_arc import OtiiArc, OtiiArcChannel

APP_ROOT = Path(__file__).resolve().parent
RUNS_DIR = APP_ROOT / "runs"

_SLUG_RX = re.compile(r"[^A-Za-z0-9._-]+")


def _slug(s: str) -> str:
    """Normalise an arbitrary string into a filename-safe slug."""
    return _SLUG_RX.sub("-", s.strip()).strip("-") or "unnamed"


def _utc_now() -> _dt.datetime:
    return _dt.datetime.now(_dt.timezone.utc)


def _utc_stamp(dt: Optional[_dt.datetime] = None) -> str:
    if dt is None:
        dt = _utc_now()
    return dt.strftime("%Y%m%dT%H%M%SZ")


def _human(seconds: float) -> str:
    seconds = int(seconds)
    h, rem = divmod(seconds, 3600)
    m, s = divmod(rem, 60)
    parts = []
    if h:
        parts.append(f"{h}h")
    if m or h:
        parts.append(f"{m}m")
    parts.append(f"{s}s")
    return " ".join(parts)


# ---------------------------------------------------------------------------
# Run-folder metadata
# ---------------------------------------------------------------------------


@dataclass
class CaptureConfig:
    voltage_V: float
    current_limit_A: float
    ovp_V: float
    chunk_minutes: int
    total_hours: float
    arc_range: str


@dataclass
class RunMetadata:
    dut: str
    label: str
    run_id: str
    started_utc: str
    finished_utc: Optional[str]
    status: str                          # "running" / "complete" / "aborted"
    chunks_written: int
    config: CaptureConfig
    arc_info: dict
    notes: str = ""
    chunk_files: list[str] = field(default_factory=list)

    def to_json(self) -> str:
        d = asdict(self)
        return json.dumps(d, indent=2, sort_keys=False)


def _write_metadata(meta: RunMetadata, path: Path) -> None:
    path.write_text(meta.to_json(), encoding="utf-8")


# ---------------------------------------------------------------------------
# Capture
# ---------------------------------------------------------------------------


def run_capture(
    dut: str,
    label: str,
    voltage_V: float,
    current_limit_A: float,
    ovp_V: float,
    total_hours: float,
    chunk_minutes: int,
    arc_range: str,
    notes: str,
    runs_dir: Path = RUNS_DIR,
    resume_path: Optional[Path] = None,
) -> Path:
    """Run a capture; returns the run folder path.

    If ``resume_path`` is given, append chunks into that existing run
    folder. The capture config (V / I / OVP / chunk minutes / range)
    is loaded from the existing ``metadata.json`` so a resume always
    matches the original session; only ``total_hours`` from the CLI
    is interpreted as *additional* time to capture.
    """
    initial_chunk_idx = 0
    resumed_from_metadata: dict = {}
    if resume_path is not None:
        if not resume_path.exists():
            raise FileNotFoundError(f"resume path {resume_path} not found")
        meta_path = resume_path / "metadata.json"
        if not meta_path.exists():
            raise FileNotFoundError(
                f"resume folder missing metadata.json: {resume_path}"
            )
        resumed_from_metadata = json.loads(
            meta_path.read_text(encoding="utf-8")
        )
        run_id = resumed_from_metadata.get("run_id") or resume_path.name
        run_root = resume_path
        # Load original config — these args take precedence over CLI
        cfg = resumed_from_metadata.get("config", {})
        voltage_V = float(cfg.get("voltage_V", voltage_V))
        current_limit_A = float(cfg.get("current_limit_A", current_limit_A))
        ovp_V = float(cfg.get("ovp_V", ovp_V))
        chunk_minutes = int(cfg.get("chunk_minutes", chunk_minutes))
        arc_range = cfg.get("arc_range", arc_range)
        dut = resumed_from_metadata.get("dut", dut)
        label = resumed_from_metadata.get("label", label)
        initial_chunk_idx = int(
            resumed_from_metadata.get("chunks_written", 0)
        )
        started = _dt.datetime.fromisoformat(
            resumed_from_metadata.get("started_utc")
        ) if resumed_from_metadata.get("started_utc") else _utc_now()
    else:
        dut_slug = _slug(dut)
        label_slug = _slug(label) if label else ""
        started = _utc_now()
        parts = [_utc_stamp(started), dut_slug]
        if label_slug:
            parts.append(label_slug)
        run_id = "_".join(parts)
        run_root = runs_dir / run_id

    data_dir = run_root / "data"
    data_dir.mkdir(parents=True, exist_ok=True)
    metadata_path = run_root / "metadata.json"

    print(f"[run] {run_id} {'(resumed)' if resume_path else ''}")
    print(f"[run] folder: {run_root}")
    if resume_path:
        print(
            f"[resume] continuing from chunk index {initial_chunk_idx}"
        )
    print(
        f"[config] V={voltage_V:.3f} V  I_limit={current_limit_A*1000:.1f} mA  "
        f"OVP={ovp_V:.2f} V  range={arc_range}"
    )
    print(
        f"[window] {total_hours:.2f} h total, {chunk_minutes} min/chunk "
        f"(~{int(total_hours * 60 // chunk_minutes)} chunks)"
    )

    # Manual lifecycle — context manager would always-disable the
    # output on exit; here we control the teardown order so the
    # recording can stop cleanly first.
    smu = OtiiArc.open()
    arc_info = {
        "port": smu.info.port,
        "name": smu.get_device_name(),
        "fw_version": smu.get_fw_version(),
        "hw_version": smu.get_hw_version(),
        "serial": smu.get_device_id(),
    }
    print(f"[arc] connected: {arc_info['port']} fw={arc_info['fw_version']}")

    # On resume, preserve existing notes + chunk_files list; just
    # mark when the resume happened.
    resume_note = (
        f"\n[resumed] {_utc_now().isoformat()} — "
        f"continuing from chunk {initial_chunk_idx}"
        if resume_path else ""
    )
    base_notes = resumed_from_metadata.get("notes", notes) if resume_path \
        else notes
    existing_chunks = resumed_from_metadata.get("chunk_files", []) \
        if resume_path else []

    meta = RunMetadata(
        dut=dut,
        label=label,
        run_id=run_id,
        started_utc=started.isoformat(),
        finished_utc=None,
        status="running",
        chunks_written=initial_chunk_idx,
        config=CaptureConfig(
            voltage_V=voltage_V,
            current_limit_A=current_limit_A,
            ovp_V=ovp_V,
            chunk_minutes=chunk_minutes,
            total_hours=total_hours,
            arc_range=arc_range,
        ),
        arc_info=arc_info,
        notes=(base_notes + resume_note).strip(),
        chunk_files=list(existing_chunks),
    )
    _write_metadata(meta, metadata_path)

    # Ctrl+C handler — raises at next sleep boundary so we save cleanly
    stop = {"requested": False}

    def _on_sigint(signum, frame):
        if stop["requested"]:
            print("\n[stop] second Ctrl+C — forcing exit", flush=True)
            sys.exit(2)
        stop["requested"] = True
        print(
            "\n[stop] Ctrl+C — finishing current chunk before exit",
            flush=True,
        )

    signal.signal(signal.SIGINT, _on_sigint)

    try:
        smu.set_range(arc_range)
        smu.set_power_regulation("voltage")
        smu.set_current_limit(current_limit_A)
        smu.set_current_limit_enabled(True)
        smu.set_voltage(voltage_V)
        smu.enable_channels(
            OtiiArcChannel.MAIN_CURRENT,
            OtiiArcChannel.MAIN_VOLTAGE,
        )

        smu.set_output(True)
        capture_started = time.monotonic()
        total_s = total_hours * 3600.0
        chunk_s = chunk_minutes * 60.0
        print(
            f"[output] ON at {voltage_V:.3f} V — DUT is live",
            flush=True,
        )

        chunk_idx = initial_chunk_idx
        elapsed = 0.0
        while elapsed < total_s and not stop["requested"]:
            chunk_started_clock = time.monotonic()
            chunk_started_utc = _utc_now()
            chunk_name = f"sensor_{_utc_stamp(chunk_started_utc)}_chunk{chunk_idx:03d}"
            chunk_path = data_dir / f"{chunk_name}.opensmu"
            print(
                f"[chunk {chunk_idx}] {chunk_started_utc.isoformat()} → "
                f"{chunk_path.name}",
                flush=True,
            )

            chunk_target_s = min(chunk_s, total_s - elapsed)
            with smu.record(name=chunk_name) as rec:
                slept = 0.0
                while slept < chunk_target_s and not stop["requested"]:
                    step = min(1.0, chunk_target_s - slept)
                    time.sleep(step)
                    slept += step

            saved = rec.save(chunk_path)
            samples = (
                len(rec.data(OtiiArcChannel.MAIN_CURRENT))
                if OtiiArcChannel.MAIN_CURRENT in rec.channels else 0
            )
            runtime = time.monotonic() - chunk_started_clock
            print(
                f"[chunk {chunk_idx}] saved {saved.name} "
                f"({samples:,} mc samples, {_human(runtime)})",
                flush=True,
            )

            chunk_idx += 1
            meta.chunks_written = chunk_idx
            meta.chunk_files.append(saved.name)
            _write_metadata(meta, metadata_path)

            elapsed = time.monotonic() - capture_started
            remaining = total_s - elapsed
            if remaining > 0 and not stop["requested"]:
                print(
                    f"[progress] {_human(elapsed)} elapsed / "
                    f"{_human(remaining)} remaining",
                    flush=True,
                )

        if stop["requested"]:
            meta.status = "aborted"
        else:
            meta.status = "complete"

    except Exception as exc:
        meta.status = "errored"
        meta.notes = (meta.notes + f"\n[error] {exc!r}").strip()
        raise
    finally:
        meta.finished_utc = _utc_now().isoformat()
        _write_metadata(meta, metadata_path)
        try:
            smu.set_output(False)
            print("[arc] output OFF", flush=True)
        except Exception as exc:
            print(f"[arc] WARN — set_output(False) failed: {exc!r}",
                  flush=True)
        smu.close()
        print(f"[arc] released — run: {meta.status}", flush=True)

    return run_root


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--dut", default=None,
                   help="DUT identifier (required for new runs; ignored "
                        "when --resume is given)")
    p.add_argument("--label", default="",
                   help="optional run label (slugged)")
    p.add_argument("--voltage", type=float, default=3.0,
                   help="CV source voltage (V); default 3.0")
    p.add_argument("--current-limit", type=float, default=0.100,
                   help="output current limit (A); default 0.100 (100 mA)")
    p.add_argument("--ovp", type=float, default=3.5,
                   help="OVP / safety_max voltage (V); default 3.5")
    p.add_argument("--hours", type=float, default=24.0,
                   help="total capture window in hours; default 24")
    p.add_argument("--chunk-minutes", type=int, default=60,
                   help="minutes per rolled chunk file; default 60")
    p.add_argument("--range", default="low",
                   choices=("low", "high"),
                   help="Arc Pro range; default 'low' (0–3.5 V envelope)")
    p.add_argument("--notes", default="",
                   help="free-form notes saved in metadata.json")
    p.add_argument("--resume", default=None,
                   help="path to an existing run folder; appends chunks "
                        "into it and ignores --dut/--label/--voltage/etc. "
                        "(those come from the existing metadata.json)")
    args = p.parse_args(list(argv) if argv is not None else None)

    resume_path = Path(args.resume) if args.resume else None
    if not resume_path and not args.dut:
        print("[error] --dut required for new runs (or pass --resume)",
              file=sys.stderr)
        return 2

    if not resume_path:
        if args.voltage > 3.5 and args.range == "low":
            print("[warn] voltage > 3.5 V — switching range to 'high' "
                  "(low range tops out at 3.5 V)")
            args.range = "high"
        if args.voltage > args.ovp:
            print(f"[error] voltage {args.voltage} V > OVP {args.ovp} V",
                  file=sys.stderr)
            return 2

    run_capture(
        dut=args.dut,
        label=args.label,
        voltage_V=args.voltage,
        current_limit_A=args.current_limit,
        ovp_V=args.ovp,
        total_hours=args.hours,
        chunk_minutes=args.chunk_minutes,
        arc_range=args.range,
        notes=args.notes,
        resume_path=resume_path,
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

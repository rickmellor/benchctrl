"""Recording file I/O — CSV/JSON + native binary round-trip."""

from __future__ import annotations

import pytest

from benchctrl.channels import Channel
from benchctrl.exceptions import SMUValueError
from benchctrl.recording import Recording


def _filled_recording():
    rec = Recording(name="run-1")
    mv = rec._ensure_buffer(Channel.MAIN_VOLTAGE, 1000)
    mv.extend([3.30, 3.31, 3.32])
    mc = rec._ensure_buffer(Channel.MAIN_CURRENT, 4000)
    mc.extend([0.010, 0.011, 0.012, 0.013])
    return rec


def test_save_csv_long_format(tmp_path):
    rec = _filled_recording()
    out = rec.save_csv(tmp_path / "rec.csv", format="long")
    assert out.exists()
    lines = out.read_text().splitlines()
    assert lines[0] == "timestamp_s,channel,value,unit"


def test_save_csv_wide_format(tmp_path):
    rec = _filled_recording()
    out = rec.save_csv(tmp_path / "rec.csv", format="wide")
    header = out.read_text().splitlines()[0]
    assert "mv" in header and "mc" in header


def test_save_csv_rejects_unknown_format(tmp_path):
    rec = _filled_recording()
    with pytest.raises(SMUValueError):
        rec.save_csv(tmp_path / "bad.csv", format="something")


def test_save_json_includes_metadata_and_stats(tmp_path):
    import json

    rec = _filled_recording()
    out = rec.save_json(tmp_path / "rec.json")
    blob = json.loads(out.read_text())
    assert blob["metadata"]["name"] == "run-1"
    assert "mv" in blob["channels"]
    assert "statistics" in blob["channels"]["mv"]
    assert blob["channels"]["mv"]["statistics"]["sample_count"] == 3


def test_native_binary_round_trip(tmp_path):
    rec = _filled_recording()
    rec.offset = 0.25
    rec.log("hello", 0.1)
    out = rec.save(tmp_path / "rec.opensmu")
    loaded = Recording.load(out)
    assert loaded.name == "run-1"
    assert loaded.offset == 0.25
    assert Channel.MAIN_VOLTAGE in loaded
    assert Channel.MAIN_CURRENT in loaded
    assert loaded.buffer(Channel.MAIN_VOLTAGE).values == pytest.approx([3.30, 3.31, 3.32])
    assert loaded.buffer(Channel.MAIN_CURRENT).values == pytest.approx(
        [0.010, 0.011, 0.012, 0.013]
    )


def test_native_binary_load_rejects_bad_magic(tmp_path):
    p = tmp_path / "bad.opensmu"
    p.write_bytes(b"WRONGMAG" + b"\x00" * 32)
    with pytest.raises(SMUValueError):
        Recording.load(p)


def test_save_empty_recording_round_trip(tmp_path):
    rec = Recording(name="empty")
    out = rec.save(tmp_path / "empty.opensmu")
    loaded = Recording.load(out)
    assert loaded.name == "empty"
    assert loaded.channels == []

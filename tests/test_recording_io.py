"""Recording file I/O — CSV/JSON + native binary round-trip."""

from __future__ import annotations

import pytest

from benchctrl.drivers.otii_arc.channels import OtiiArcChannel as Channel
from benchctrl.exceptions import BenchValueError
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
    with pytest.raises(BenchValueError):
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
    with pytest.raises(BenchValueError):
        Recording.load(p)


def test_save_empty_recording_round_trip(tmp_path):
    rec = Recording(name="empty")
    out = rec.save(tmp_path / "empty.opensmu")
    loaded = Recording.load(out)
    assert loaded.name == "empty"
    assert loaded.channels == []


# --------------------------------------------------------------------------
# Stream I/O — the .opensmu encoding without a filesystem
# --------------------------------------------------------------------------
#
# Remote mode ships recordings as .opensmu blobs, so these codecs are the
# wire encoder as well as the file writer. They must agree byte-for-byte
# with the path-based versions, which is what makes "one format, four
# consumers" true rather than aspirational.


def test_to_bytes_matches_save(tmp_path):
    import io as _io

    from benchctrl.drivers.otii_arc.channels import OtiiArcChannel
    from benchctrl.recording import Recording

    rec = Recording(name="stream-test")
    for i in range(100):
        rec._append(OtiiArcChannel.MAIN_CURRENT, i * 0.001, 4000)
        rec._append(OtiiArcChannel.MAIN_VOLTAGE, 3.3, 1000)

    path = rec.save(tmp_path / "r.opensmu")
    assert path.read_bytes() == rec.to_bytes()

    buf = _io.BytesIO()
    written = rec.save_to_stream(buf)
    assert written == len(path.read_bytes())


def test_from_bytes_round_trips():
    from benchctrl.drivers.otii_arc.channels import OtiiArcChannel
    from benchctrl.recording import Recording

    rec = Recording(name="round-trip", offset=1.5)
    for i in range(50):
        rec._append(OtiiArcChannel.MAIN_CURRENT, i * 0.01, 4000)

    loaded = Recording.from_bytes(rec.to_bytes())
    assert loaded.name == "round-trip"
    assert loaded.offset == 1.5
    assert loaded.channels == rec.channels
    # .opensmu stores float32 ("dtype": "f32"), so values written from
    # Python float64 come back quantised. Device samples are already f32 and
    # round-trip exactly — see test_sim_loopback's opensmu test.
    import pytest as _pytest

    assert loaded.data(OtiiArcChannel.MAIN_CURRENT) == _pytest.approx(
        rec.data(OtiiArcChannel.MAIN_CURRENT), rel=1e-6
    )


def test_load_from_stream_rejects_bad_magic():
    import io as _io

    import pytest as _pytest

    from benchctrl.exceptions import BenchValueError
    from benchctrl.recording import Recording

    with _pytest.raises(BenchValueError, match="bad magic"):
        Recording.load_from_stream(_io.BytesIO(b"NOTOPENSMU" + b"\x00" * 32))


def test_load_still_names_the_path_on_error(tmp_path):
    import pytest as _pytest

    from benchctrl.exceptions import BenchValueError
    from benchctrl.recording import Recording

    bad = tmp_path / "bad.opensmu"
    bad.write_bytes(b"garbage!" + b"\x00" * 32)
    with _pytest.raises(BenchValueError, match="bad.opensmu"):
        Recording.load(bad)


def test_empty_recording_round_trips():
    from benchctrl.recording import Recording

    assert Recording.from_bytes(Recording(name="empty").to_bytes()).channels == []

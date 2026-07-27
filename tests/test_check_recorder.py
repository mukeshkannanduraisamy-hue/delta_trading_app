import gzip
import json
import shutil
import tempfile
from pathlib import Path

import pytest

from research import check_recorder

# The system temp dir is not writable in this environment, so pytest's
# tmp_path fixture errors out. Use a project-local scratch dir instead.
_SCRATCH = Path(__file__).resolve().parent / ".scratch"


@pytest.fixture
def tmp_path():
    _SCRATCH.mkdir(exist_ok=True)
    d = Path(tempfile.mkdtemp(dir=_SCRATCH))
    try:
        yield d
    finally:
        shutil.rmtree(d, ignore_errors=True)


def _write(tmp_path, name, timestamps, monkeypatch):
    d = tmp_path / "book"
    d.mkdir(exist_ok=True)
    p = d / name
    with gzip.open(p, "wt", encoding="utf-8") as fh:
        for ts in timestamps:
            fh.write(json.dumps({"ts": ts, "symbol": "C-BTC-1-010826"}) + "\n")
    monkeypatch.setattr(check_recorder, "BOOK_DIR", d)
    return p


def test_missing_files_is_not_healthy(tmp_path, monkeypatch):
    monkeypatch.setattr(check_recorder, "BOOK_DIR", tmp_path / "nothing")
    r = check_recorder.report()
    assert r["healthy"] is False and "never written" in r["reason"]


def test_single_snapshot_is_not_a_collection(tmp_path, monkeypatch):
    """The exact failure this tool exists for: one smoke-test snapshot that
    was mistaken for continuous recording."""
    import time
    _write(tmp_path, "book_A.jsonl.gz", [int(time.time())], monkeypatch)
    r = check_recorder.report()
    assert r["healthy"] is False
    assert "smoke test" in r["reason"]


def test_stale_recorder_is_detected(tmp_path, monkeypatch):
    import time
    now = int(time.time())
    _write(tmp_path, "book_A.jsonl.gz", [now - 7200, now - 6900], monkeypatch)
    r = check_recorder.report()
    assert r["healthy"] is False and "stopped" in r["reason"]


def test_live_recorder_is_healthy(tmp_path, monkeypatch):
    import time
    now = int(time.time())
    _write(tmp_path, "book_A.jsonl.gz", [now - 600, now - 300, now], monkeypatch)
    r = check_recorder.report()
    assert r["healthy"] is True and r["reason"] is None


def test_coverage_excludes_outages(tmp_path, monkeypatch):
    """A recorder that ran briefly, died for two days, then restarted must NOT
    report two days of coverage -- span is not coverage."""
    import time
    now = int(time.time())
    ts = [now - 200000, now - 199700, now - 199400]      # ~10 min, long ago
    ts += [now - 600, now - 300, now]                     # ~10 min, now
    _write(tmp_path, "book_A.jsonl.gz", ts, monkeypatch)
    r = check_recorder.report()
    assert r["span_days"] > 2.0, "wall span should look long"
    assert r["covered_days"] < 0.05, (
        f"coverage must exclude the outage, got {r['covered_days']} days")
    assert r["outages"] == 1
    assert r["largest_outage_hours"] > 24


def test_continuous_collection_counts_as_covered(tmp_path, monkeypatch):
    import time
    now = int(time.time())
    ts = [now - i * 300 for i in range(288, -1, -1)]      # 24h at 300s
    _write(tmp_path, "book_A.jsonl.gz", ts, monkeypatch)
    r = check_recorder.report()
    assert r["outages"] == 0
    assert r["covered_days"] == pytest.approx(1.0, abs=0.02)


def test_pct_of_target_uses_coverage_not_span(tmp_path, monkeypatch):
    import time
    now = int(time.time())
    ts = [now - 900000, now - 899700] + [now - 300, now]
    _write(tmp_path, "book_A.jsonl.gz", ts, monkeypatch)
    r = check_recorder.report()
    expected = 100.0 * r["covered_days"] / check_recorder.TARGET_DAYS
    assert r["pct_of_target"] == pytest.approx(expected, abs=0.1)

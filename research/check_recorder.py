"""Is the book recorder actually collecting? Answers with evidence, not a claim.

WHY THIS EXISTS
---------------
This project has now been wrong four times about an assumption it never
checked, and one of those was the recorder itself: a note said it "is running"
when a single smoke-test snapshot had been taken and the process had exited.

A stopped recorder fails SILENTLY. The file still exists, still has data, still
looks fine. The only symptom is that it stopped growing — which nobody notices
until the day they go to use the data.

Run this before trusting the collection:

    .venv/Scripts/python.exe -m research.check_recorder

Exit code 0 = healthy, 1 = stale or missing. Suitable for a scheduled check.
"""

from __future__ import annotations

import gzip
import json
import sys
import time
from pathlib import Path

BOOK_DIR = Path(__file__).resolve().parent / "book"
STALE_AFTER_MIN = 30          # a 300s loop should never be this far behind
TARGET_DAYS = 30              # minimum useful collection for forward validation


def _snapshots(path: Path) -> list[int]:
    """Distinct snapshot timestamps in one daily file."""
    seen = set()
    try:
        with gzip.open(path, "rt", encoding="utf-8") as fh:
            for line in fh:
                try:
                    seen.add(int(json.loads(line)["ts"]))
                except (ValueError, KeyError, json.JSONDecodeError):
                    continue
    except OSError:
        return []
    return sorted(seen)


def report() -> dict:
    files = sorted(BOOK_DIR.glob("book_*.jsonl.gz"))
    if not files:
        return {"healthy": False, "reason": "no book files found — the "
                                            "recorder has never written"}

    all_ts: list[int] = []
    for f in files:
        all_ts.extend(_snapshots(f))
    if not all_ts:
        return {"healthy": False, "reason": "book files exist but contain no "
                                            "parseable snapshots"}

    all_ts.sort()
    now = int(time.time())
    age_min = (now - all_ts[-1]) / 60.0
    span_days = (all_ts[-1] - all_ts[0]) / 86400.0

    # a single snapshot is NOT a collection, however recent it is
    if len(all_ts) < 2:
        return {"healthy": False, "snapshots": 1, "age_min": round(age_min, 1),
                "reason": "only ONE snapshot exists — that is a smoke test, "
                          "not a collection"}

    # Span is first-to-last and says NOTHING about gaps: a recorder that ran
    # one hour, died for two days, then restarted still shows a 2-day "span".
    # Effective coverage counts only intervals where collection was actually
    # live, so a restarted recorder cannot look like a continuous one.
    gaps = [(all_ts[i + 1] - all_ts[i]) for i in range(len(all_ts) - 1)]
    gap_limit = STALE_AFTER_MIN * 60
    covered_s = sum(g for g in gaps if g <= gap_limit)
    outages = [g for g in gaps if g > gap_limit]
    covered_days = covered_s / 86400.0

    healthy = age_min <= STALE_AFTER_MIN
    return {
        "healthy": healthy,
        "files": len(files),
        "snapshots": len(all_ts),
        "span_days": round(span_days, 2),
        "covered_days": round(covered_days, 2),
        "outages": len(outages),
        "largest_outage_hours": round(max(outages) / 3600.0, 1) if outages else 0.0,
        "age_min": round(age_min, 1),
        "bytes": sum(f.stat().st_size for f in files),
        "pct_of_target": round(100.0 * covered_days / TARGET_DAYS, 1),
        "reason": None if healthy else
                  f"last snapshot {age_min:.0f} min ago (stale after "
                  f"{STALE_AFTER_MIN} min) — the recorder has stopped",
    }


def main() -> int:
    r = report()
    print("=" * 62)
    print("  BOOK RECORDER —", "HEALTHY" if r["healthy"] else "NOT COLLECTING")
    print("=" * 62)
    if r.get("snapshots"):
        print(f"  files          {r.get('files', '-')}")
        print(f"  snapshots      {r['snapshots']}")
        print(f"  COVERAGE       {r.get('covered_days', 0)} days "
              f"({r.get('pct_of_target', 0)}% of the {TARGET_DAYS}-day target)")
        print(f"  wall span      {r.get('span_days', 0)} days "
              f"(first to last -- NOT coverage)")
        if r.get("outages"):
            print(f"  outages        {r['outages']}, largest "
                  f"{r['largest_outage_hours']}h  <-- gaps, not collected")
        print(f"  last write     {r['age_min']} min ago")
        print(f"  size           {r.get('bytes', 0):,} bytes")
    if r["reason"]:
        print(f"\n  PROBLEM: {r['reason']}")
        print("\n  restart:")
        print("    .venv/Scripts/python.exe -m research.record_book --loop 300")
    print("=" * 62)
    return 0 if r["healthy"] else 1


if __name__ == "__main__":
    sys.exit(main())

"""Append-only recorder for the live option book.

WHY
---
Delta serves no historical bid/ask, and MARK is a model output that diverges
from executable prices for cheap OTM options. The only way to get a proper
future validation target is to start writing the book down now. Every snapshot
taken today is a contract that becomes real, tradeable history tomorrow.

Deliberately minimal: one JSONL line per quote, append-only, no daemon and no
dependencies beyond what the study already uses. Run it from Task Scheduler,
a cron, or a `while True` shell loop -- the file format does not care about
cadence and gaps are harmless.

    .venv/Scripts/python.exe -m research.record_book            # one snapshot
    .venv/Scripts/python.exe -m research.record_book --loop 300 # every 5 min

Storage is about 456 quotes x ~180 bytes = ~80 KB per snapshot; at 5-minute
cadence that is ~23 MB/day, and the file is gzip-friendly.
"""

from __future__ import annotations

import argparse
import gzip
import json
import time
from pathlib import Path

from research import optdata

OUT_DIR = Path(__file__).resolve().parent / "book"
OUT_DIR.mkdir(parents=True, exist_ok=True)

FIELDS = ("symbol", "kind", "strike", "spot", "moneyness", "dte_hours",
          "bid", "ask", "mid", "spread_pct", "mark_iv")


def snapshot_once(asset: str = "BTC") -> int:
    """Append one full-chain snapshot. Returns the number of quotes written.

    Failures are swallowed and reported rather than raised: this is meant to
    run unattended for weeks, and one bad poll must not end the collection.
    """
    ts = int(time.time())
    try:
        quotes = optdata.snapshot_spreads(asset)
    except Exception as e:  # noqa: BLE001 — an unattended recorder must survive
        print(f"[{ts}] snapshot failed: {type(e).__name__}: {e}")
        return 0

    day = time.strftime("%Y-%m-%d", time.gmtime(ts))
    path = OUT_DIR / f"book_{asset}_{day}.jsonl.gz"
    with gzip.open(path, "at", encoding="utf-8") as fh:
        for q in quotes:
            fh.write(json.dumps({"ts": ts, **{k: q.get(k) for k in FIELDS}}) + "\n")
    print(f"[{ts}] wrote {len(quotes)} quotes -> {path.name}")
    return len(quotes)


def main() -> None:
    ap = argparse.ArgumentParser(description="record the live option book")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between snapshots; 0 = take one and exit")
    args = ap.parse_args()

    if not args.loop:
        snapshot_once(args.asset)
        return
    print(f"recording {args.asset} book every {args.loop}s -> {OUT_DIR}")
    while True:
        snapshot_once(args.asset)
        time.sleep(args.loop)


if __name__ == "__main__":
    main()

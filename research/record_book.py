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
import os
import subprocess
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


LOCK = OUT_DIR / "recorder.lock"


def _claim_lock() -> bool:
    """Refuse to start a second looping recorder.

    Autostart plus a manually-launched instance would both append to the same
    daily file, producing duplicate quotes at near-identical timestamps. That
    is silently corrupting rather than loudly failing, so it is blocked here.

    A stale lock (process gone) is reclaimed: the recorder must survive an
    ungraceful kill without needing manual cleanup.
    """
    if LOCK.exists():
        try:
            pid = int(LOCK.read_text().strip())
        except (ValueError, OSError):
            pid = -1
        if pid > 0 and _pid_alive(pid):
            print(f"another recorder is already running (pid {pid}); exiting")
            return False
        print(f"reclaiming stale lock from pid {pid}")
    LOCK.write_text(str(os.getpid()))
    return True


def _pid_alive(pid: int) -> bool:
    if os.name == "nt":
        out = subprocess.run(["tasklist", "/FI", f"PID eq {pid}", "/NH"],
                             capture_output=True, text=True).stdout
        return str(pid) in out
    try:
        os.kill(pid, 0)
        return True
    except (OSError, ProcessLookupError):
        return False


def main() -> None:
    ap = argparse.ArgumentParser(description="record the live option book")
    ap.add_argument("--asset", default="BTC")
    ap.add_argument("--loop", type=int, default=0,
                    help="seconds between snapshots; 0 = take one and exit")
    args = ap.parse_args()

    if not args.loop:
        snapshot_once(args.asset)
        return

    if not _claim_lock():
        return
    print(f"recording {args.asset} book every {args.loop}s -> {OUT_DIR}")
    try:
        while True:
            snapshot_once(args.asset)
            time.sleep(args.loop)
    finally:
        try:
            if LOCK.exists() and LOCK.read_text().strip() == str(os.getpid()):
                LOCK.unlink()
        except OSError:
            pass


if __name__ == "__main__":
    main()

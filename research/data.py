"""Historical candle fetcher for the retune study.

Delta caps /v2/history/candles at 2000 bars per response, so a 90-day 1m series
(129,600 bars) needs ~65 paginated calls. Results are cached as compressed .npz
so the grid search in Phase 2 never re-fetches.

start/end are Unix SECONDS. Requests are spaced 0.1s apart and honour
X-RATE-LIMIT-RESET on 429, per the Delta rate-limit contract.
"""

from __future__ import annotations

import time
from pathlib import Path

import numpy as np
import requests

PROD = "https://api.india.delta.exchange"
CACHE = Path(__file__).resolve().parent / "cache"
CACHE.mkdir(parents=True, exist_ok=True)

_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
PAGE = 2000
FIELDS = ("time", "open", "high", "low", "close", "volume")


def res_seconds(resolution: str) -> int:
    return int(resolution[:-1]) * _UNIT[resolution[-1]]


def _get(params: dict, attempt: int = 0) -> list[dict]:
    r = requests.get(f"{PROD}/v2/history/candles", params=params, timeout=(3.05, 30))
    if r.status_code == 429:
        raw = r.headers.get("X-RATE-LIMIT-RESET")
        try:
            wait = min(float(raw) / 1000.0, 300.0)
        except (TypeError, ValueError):
            wait = 10.0
        print(f"    [429] sleeping {wait:.1f}s")
        time.sleep(wait + 0.5)
        return _get(params, attempt)
    if 500 <= r.status_code < 600 and attempt < 3:
        time.sleep(2 ** attempt)
        return _get(params, attempt + 1)
    r.raise_for_status()
    body = r.json()
    if body.get("success") is False:
        raise RuntimeError(f"delta error: {body.get('error')}")
    return body.get("result") or []


def fetch(symbol: str, resolution: str, days: float, refresh: bool = False) -> dict:
    """Return {field: np.ndarray} of `days` history, oldest-first.

    Pages BACKWARDS from now: ask for a 2000-bar window, then move the cursor to
    just before the oldest bar received and repeat. Paging backwards (rather than
    forwards from a computed start) means a gap in Delta's history cannot cause
    an infinite loop — the cursor always strictly decreases.
    """
    cache = CACHE / f"{symbol}_{resolution}_{days:g}d.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    secs = res_seconds(resolution)
    end = int(time.time())
    start_target = end - int(days * 86400)
    rows: dict[int, list] = {}
    cursor = end
    calls = 0
    print(f"  fetching {symbol} {resolution} ({days:g}d, ~{int(days*86400/secs)} bars)")
    while cursor > start_target:
        win_start = max(start_target, cursor - secs * PAGE)
        batch = _get({"symbol": symbol, "resolution": resolution,
                      "start": win_start, "end": cursor})
        calls += 1
        if not batch:
            break
        times = [int(c["time"]) for c in batch if c.get("time") is not None]
        if not times:
            break
        for c in batch:
            if c.get("time") is not None:
                rows[int(c["time"])] = c
        oldest = min(times)
        if oldest <= start_target or win_start <= start_target:
            break
        cursor = oldest - secs
        if calls % 10 == 0:
            print(f"    {calls} calls, {len(rows)} bars, back to "
                  f"{time.strftime('%Y-%m-%d %H:%M', time.gmtime(oldest))}")
        time.sleep(0.1)  # stay well inside the 5-minute quota

    ordered = [rows[t] for t in sorted(rows)]
    data = {
        "time": np.array([int(c["time"]) for c in ordered], dtype="int64"),
        "open": np.array([float(c["open"]) for c in ordered], dtype="float64"),
        "high": np.array([float(c["high"]) for c in ordered], dtype="float64"),
        "low": np.array([float(c["low"]) for c in ordered], dtype="float64"),
        "close": np.array([float(c["close"]) for c in ordered], dtype="float64"),
        "volume": np.array([float(c["volume"]) for c in ordered], dtype="float64"),
    }
    np.savez_compressed(cache, **data)
    print(f"  done: {calls} calls, {len(ordered)} bars -> {cache.name}")
    return data


def resample(d: dict, from_res: str, to_res: str) -> dict:
    """Aggregate a finer series into a coarser one.

    Higher timeframes are derived from ONE 1m fetch rather than fetched
    separately: independent fetches can disagree at bar boundaries, which
    silently introduces look-ahead when a strategy reads two timeframes.

    Buckets align to the epoch grid (floor(t / target) * target) so an "1h"
    bar always starts on the hour, matching what the exchange serves. An
    incomplete trailing bucket is dropped -- a partial bar has not closed, and
    the live engine only ever evaluates closed bars.
    """
    src = res_seconds(from_res)
    dst = res_seconds(to_res)
    if dst < src:
        raise ValueError(f"cannot resample {from_res} -> {to_res}: target is finer")
    if dst % src:
        raise ValueError(f"cannot resample {from_res} -> {to_res}: not an integer multiple")
    if dst == src:
        return {k: v.copy() for k, v in d.items()}

    t = d["time"]
    if len(t) == 0:
        return {k: v.copy() for k, v in d.items()}

    bucket = (t // dst) * dst
    # first row index of each bucket, in order
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(t)]                  # exclusive

    # Drop any bucket missing bars. A bucket built from a partial set of
    # source bars is not the bar the exchange would have printed, and letting
    # one through would put a silently wrong high/low into the study.
    complete = (ends - starts) == (dst // src)
    starts, ends = starts[complete], ends[complete]

    return {
        "time": bucket[starts],
        "open": d["open"][starts],
        "close": d["close"][ends - 1],
        "high": np.array([d["high"][a:b].max() for a, b in zip(starts, ends)],
                         dtype="float64"),
        "low": np.array([d["low"][a:b].min() for a, b in zip(starts, ends)],
                        dtype="float64"),
        "volume": np.array([d["volume"][a:b].sum() for a, b in zip(starts, ends)],
                           dtype="float64"),
    }


def validate(d: dict, resolution: str, label: str = "") -> dict:
    """Integrity report: NaNs, duplicates, gaps, OHLC sanity."""
    secs = res_seconds(resolution)
    t = d["time"]
    n = len(t)
    out = {"label": label, "bars": n, "resolution": resolution}
    if n == 0:
        out["fatal"] = "empty series"
        return out

    nan_count = int(sum(np.isnan(d[f]).sum() for f in FIELDS[1:]))
    dup = int(n - len(np.unique(t)))
    diffs = np.diff(t)
    gaps = diffs[diffs > secs]
    bad_ohlc = int(np.sum((d["high"] < d["low"]) |
                          (d["high"] < d["open"]) | (d["high"] < d["close"]) |
                          (d["low"] > d["open"]) | (d["low"] > d["close"])))
    nonpos = int(np.sum(d["close"] <= 0))

    out.update({
        "span_days": round((t[-1] - t[0]) / 86400, 2),
        "first": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t[0])),
        "last": time.strftime("%Y-%m-%d %H:%M", time.gmtime(t[-1])),
        "nans": nan_count,
        "duplicate_ts": dup,
        "gap_count": int(len(gaps)),
        "largest_gap_min": round(float(gaps.max()) / 60, 1) if len(gaps) else 0.0,
        "gaps_over_5min": int(np.sum(gaps > 300)),
        "bad_ohlc_bars": bad_ohlc,
        "nonpositive_close": nonpos,
        "expected_bars": int((t[-1] - t[0]) // secs) + 1,
    })
    out["completeness_pct"] = round(100.0 * n / out["expected_bars"], 2)
    return out


if __name__ == "__main__":
    import json
    for res in ("1m", "5m"):
        d = fetch("BTCUSD", res, 90)
        print(json.dumps(validate(d, res, f"BTCUSD {res}"), indent=2))
        print()

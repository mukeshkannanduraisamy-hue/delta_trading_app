"""Multi-year OHLCV loader for backtesting, with an on-disk cache.

Delta's own history only reaches back ~2.5 years (from 2023-12-29), which is too
short for a 3-5 year study, so historical research uses Binance's public klines
endpoint (no auth, read-only market data). Live trading still executes on Delta.

Cached as compressed .npz per (symbol, interval) so repeated optimizer runs cost
one download, not thousands.
"""

from __future__ import annotations

import time


import numpy as np
import requests

from . import config

CACHE_DIR = config.BASE_DIR / "strategy" / "cache"
CACHE_DIR.mkdir(parents=True, exist_ok=True)

BINANCE = "https://api.binance.com/api/v3/klines"
_MS = {"1m": 60_000, "5m": 300_000, "15m": 900_000, "1h": 3_600_000,
       "4h": 14_400_000, "1d": 86_400_000}


def _fetch_binance(symbol: str, interval: str, start_ms: int, end_ms: int) -> list[list]:
    """Page through Binance klines (1000 bars per request)."""
    out: list[list] = []
    cursor = start_ms
    step = _MS[interval] * 1000
    while cursor < end_ms:
        try:
            r = requests.get(BINANCE, params={
                "symbol": symbol, "interval": interval,
                "startTime": cursor, "endTime": min(cursor + step, end_ms),
                "limit": 1000,
            }, timeout=30)
            r.raise_for_status()
            rows = r.json()
        except Exception as exc:  # noqa: BLE001
            print(f"[datafeed] {symbol} {interval} fetch failed at {cursor}: {exc!r}")
            break
        if not rows:
            cursor += step
            continue
        out.extend(rows)
        last = rows[-1][0]
        if last <= cursor:
            cursor += step
        else:
            cursor = last + _MS[interval]
        time.sleep(0.06)  # stay well inside the public rate limit
    return out


def load(symbol: str, interval: str = "1h", years: float = 5.0,
         refresh: bool = False) -> dict[str, np.ndarray]:
    """Return {time, open, high, low, close, volume} as float64 arrays."""
    cache = CACHE_DIR / f"{symbol}_{interval}_{years:g}y.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    end_ms = int(time.time() * 1000)
    start_ms = end_ms - int(years * 365 * 86400 * 1000)
    rows = _fetch_binance(symbol, interval, start_ms, end_ms)
    if not rows:
        return {k: np.array([]) for k in ("time", "open", "high", "low", "close", "volume")}

    # De-duplicate and sort by open time.
    seen: dict[int, list] = {}
    for r in rows:
        seen[int(r[0])] = r
    ordered = [seen[t] for t in sorted(seen)]

    data = {
        "time": np.array([r[0] / 1000 for r in ordered], dtype="float64"),
        "open": np.array([float(r[1]) for r in ordered]),
        "high": np.array([float(r[2]) for r in ordered]),
        "low": np.array([float(r[3]) for r in ordered]),
        "close": np.array([float(r[4]) for r in ordered]),
        "volume": np.array([float(r[5]) for r in ordered]),
    }
    np.savez_compressed(cache, **data)
    return data


def describe(data: dict) -> str:
    import datetime as dt
    if not len(data.get("time", [])):
        return "empty"
    f = dt.datetime.fromtimestamp(data["time"][0], dt.UTC)
    l = dt.datetime.fromtimestamp(data["time"][-1], dt.UTC)
    return f"{len(data['time'])} bars  {f:%Y-%m-%d} -> {l:%Y-%m-%d}"

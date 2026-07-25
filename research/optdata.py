"""Option chain and option candle access.

RETENTION CONSTRAINT (measured 2026-07-25)
------------------------------------------
Delta keeps option candle history roughly TWO DAYS past expiry. Mapping all 46
expired BTC expiries: 1-day-old -> 1440 bars, 2-day -> 1441, 3-day -> 1-2,
June expiries -> 0. A multi-month backtest on real option data is therefore
impossible; the real window is only ever ~3 days wide. That constraint is what
forces the calibrated-overlay design in calibrate.py.

TRADED VS MARK
--------------
Traded candles (`C-BTC-...`) are single-print and flat: a real response is OHLC
1197/1197/1197/1197 on volume 5. Using them for fills manufactures phantom
edge -- this is the thin-liquidity artifact that inflated the earlier options
study. Only `MARK:` candles are used for premium paths.
"""

from __future__ import annotations

import calendar
import re
import time

import numpy as np
import requests

from research.data import CACHE, PAGE, PROD, _get, res_seconds

SETTLEMENT_HOUR_UTC = 12
_SYM = re.compile(r"^([CP])-([A-Z]+)-(\d+(?:\.\d+)?)-(\d{6})$")


def parse_symbol(sym: str) -> dict:
    """`C-BTC-63000-270726` -> kind/asset/strike/expiry(unix, 12:00 UTC).

    The DDMMYY token is validated as a real calendar date rather than
    slice-and-trust. calendar.timegm() silently normalizes overflow (31 Feb
    becomes 3 Mar), so the round-trip check below is what actually rejects an
    impossible date.
    """
    m = _SYM.match(sym or "")
    if not m:
        raise ValueError(f"not an option symbol: {sym!r}")
    kind, asset, strike, ddmmyy = m.groups()
    dd, mm, yy = int(ddmmyy[0:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ValueError(f"impossible expiry date in {sym!r}: {ddmmyy}")
    expiry = calendar.timegm((2000 + yy, mm, dd, SETTLEMENT_HOUR_UTC, 0, 0, 0, 0, 0))
    got = time.gmtime(expiry)
    if (got.tm_mday, got.tm_mon) != (dd, mm):
        raise ValueError(f"impossible expiry date in {sym!r}: {ddmmyy}")
    return {"kind": kind, "asset": asset, "strike": float(strike), "expiry": expiry}


def _products(contract_type: str, state: str, after: str | None = None) -> tuple[list, str | None]:
    params = {"contract_types": contract_type, "states": state, "page_size": 200}
    if after:
        params["after"] = after
    r = requests.get(f"{PROD}/v2/products", params=params, timeout=(3.05, 30))
    r.raise_for_status()
    body = r.json()
    return (body.get("result") or []), (body.get("meta") or {}).get("after")


def live_chain(asset: str = "BTC") -> list[dict]:
    """Every live option product for `asset`, with strike/cv/tick/fees attached.

    A product missing contract_value is skipped rather than defaulted: sizing
    against a missing multiplier mis-scales every downstream P&L number, and
    the live resolver already refuses these for the same reason.
    """
    out = []
    for ct in ("call_options", "put_options"):
        after = None
        while True:
            res, after = _products(ct, "live", after)
            if not res:
                break
            for p in res:
                if (p.get("underlying_asset") or {}).get("symbol") != asset:
                    continue
                try:
                    meta = parse_symbol(p["symbol"])
                except ValueError:
                    continue
                cv = float(p.get("contract_value") or 0)
                if cv <= 0:
                    continue
                specs = p.get("product_specs") or {}
                out.append({
                    **meta,
                    "symbol": p["symbol"],
                    "contract_value": cv,
                    "tick_size": float(p.get("tick_size") or 0),
                    "taker_rate": float(p.get("taker_commission_rate") or 0) or None,
                    "premium_cap": float(specs.get("premium_commission_rate") or 0) or None,
                })
            if not after:
                break
            time.sleep(0.1)
    return out


def traded_candles(symbol: str, resolution: str, days: float,
                   refresh: bool = False) -> dict:
    """TRADED candles for one option contract -- actual execution prints.

    Delta serves no historical bid/ask (BID:/ASK:/MID: prefixes all return
    empty), so trade prints are the only EXECUTABLE historical reference. A
    print is a price someone actually transacted at, which is exactly the
    thing MARK is not: MARK is a model output that runs +72% to +439% above
    the book mid for cheap OTM options.

    Two caveats callers must respect:
      * a print lands at the bid OR the ask, so a single print carries about
        +-half-spread of noise (~0.75% at ATM). Fine against a 25% tolerance,
        not fine for fills.
      * coverage varies hugely with liquidity. A liquid ATM contract prints
        ~98% of minutes; a thin strike prints a handful of flat bars. Filter
        on volume > 0 and on coverage before trusting a series.
    """
    return _candles(symbol, symbol, resolution, days, refresh)


def mark_candles(symbol: str, resolution: str, days: float,
                 refresh: bool = False) -> dict:
    """MARK candles for one option contract, cached like data.fetch()."""
    return _candles(f"MARK:{symbol}", symbol, resolution, days, refresh)


def _candles(query_symbol: str, symbol: str, resolution: str, days: float,
             refresh: bool = False) -> dict:
    """Paged, cached candle fetch for one option series.

    Pages backwards like data.fetch() does: an option's full life can exceed
    the 2000-bar response cap at 1m, and a truncated series would silently
    drop the contract's early history.
    """
    safe = query_symbol.replace(":", "_").replace("/", "_")
    cache = CACHE / f"{safe}_{resolution}_{days:g}d.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    secs = res_seconds(resolution)
    end = int(time.time())
    start_target = end - int(days * 86400)
    rows: dict[int, dict] = {}
    cursor = end
    while cursor > start_target:
        win_start = max(start_target, cursor - secs * PAGE)
        batch = _get({"symbol": query_symbol, "resolution": resolution,
                      "start": win_start, "end": cursor})
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
        time.sleep(0.1)

    ordered = [rows[t] for t in sorted(rows)]
    d = {
        "time": np.array([int(c["time"]) for c in ordered], dtype="int64"),
        "open": np.array([float(c["open"]) for c in ordered], dtype="float64"),
        "high": np.array([float(c["high"]) for c in ordered], dtype="float64"),
        "low": np.array([float(c["low"]) for c in ordered], dtype="float64"),
        "close": np.array([float(c["close"]) for c in ordered], dtype="float64"),
        # MARK carries no volume (None); traded candles do. Zero-fill the
        # missing case so callers can filter on volume > 0 uniformly.
        "volume": np.array([float(c.get("volume") or 0.0) for c in ordered],
                           dtype="float64"),
    }
    np.savez_compressed(cache, **d)
    return d


def snapshot_spreads(asset: str = "BTC") -> list[dict]:
    """One live bid/ask sample of the whole option chain.

    Spread is the dominant cost in options and the old premium.py model
    omitted it entirely. It is not recoverable from candles -- sampling the
    live book is the only way to measure it.
    """
    r = requests.get(f"{PROD}/v2/tickers",
                     params={"contract_types": "call_options,put_options"},
                     timeout=(3.05, 45))
    r.raise_for_status()
    out = []
    now = time.time()
    for t in (r.json().get("result") or []):
        try:
            meta = parse_symbol(t.get("symbol") or "")
        except ValueError:
            continue
        if meta["asset"] != asset:
            continue
        q = t.get("quotes") or {}
        try:
            bid = float(q.get("best_bid") or 0)
            ask = float(q.get("best_ask") or 0)
            spot = float(t.get("spot_price") or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid or spot <= 0:
            continue
        mid = 0.5 * (bid + ask)
        iv = q.get("mark_iv", t.get("mark_iv"))
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            iv = None
        out.append({
            "symbol": t["symbol"],
            "kind": meta["kind"],
            "strike": meta["strike"],
            "spot": spot,
            "moneyness": meta["strike"] / spot,
            "dte_hours": max(0.0, (meta["expiry"] - now) / 3600.0),
            "bid": bid,
            "ask": ask,
            "mid": mid,
            "spread_pct": (ask - bid) / mid,
            "mark_iv": iv,
        })
    return out


def recent_real_contracts(asset: str = "BTC", min_bars: int = 500,
                          resolution: str = "1m", limit: int = 40,
                          days: float = 5.0) -> list[str]:
    """Live + recently-expired contracts that still carry usable MARK history.

    This is the calibration window. It is small by construction (see the
    retention note at the top of this module) and callers must treat it as
    such rather than assuming it will grow with `days`.

    Candidates are ordered near-the-money first, because those are the
    contracts the engine actually trades and the ones with a populated book.
    """
    live = live_chain(asset)
    spot = 0.0
    if live:
        try:
            snaps = snapshot_spreads(asset)
            spot = float(np.median([s["spot"] for s in snaps])) if snaps else 0.0
        except Exception:  # noqa: BLE001 — a spot probe failure must not abort
            spot = 0.0

    cands = [c["symbol"] for c in live]
    for ct in ("call_options", "put_options"):
        res, _ = _products(ct, "expired")
        for p in res:
            if (p.get("underlying_asset") or {}).get("symbol") == asset:
                cands.append(p["symbol"])
        time.sleep(0.1)

    def _rank(sym: str) -> float:
        try:
            m = parse_symbol(sym)
        except ValueError:
            return float("inf")
        return abs(m["strike"] - spot) if spot > 0 else 0.0

    ordered = sorted(dict.fromkeys(cands), key=_rank)

    keep = []
    for s in ordered:
        if len(keep) >= limit:
            break
        try:
            d = mark_candles(s, resolution, days)
        except Exception:  # noqa: BLE001 — one dead contract must not abort
            continue
        if len(d["time"]) >= min_bars:
            keep.append(s)
        time.sleep(0.05)
    return keep

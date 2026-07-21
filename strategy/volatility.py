"""Variance Risk Premium (VRP) analyzer.

Measures whether Delta BTC option implied volatility systematically exceeds
subsequent/trailing realized volatility. If IV > RV persistently, option SELLERS
are being paid a risk premium — the one structurally documented edge in options
markets, and the only candidate left after directional signals were ruled out.

    VRP = ATM implied vol  -  realized vol (tenor-matched)

Honest caveats, stated up front:
  * A positive VRP is an EXPECTED-VALUE edge, not a free lunch. It is
    compensation for tail risk: selling options loses badly and abruptly when
    volatility spikes. Expectancy alone does not capture that asymmetry.
  * Trailing realized vol is a proxy for future realized vol. The true test is
    IV today vs realized vol over the option's REMAINING life, which needs
    forward data — hence `snapshot()`, which accumulates that series over time.
"""

from __future__ import annotations

import math
import time
from datetime import datetime, timezone
from typing import Optional

from . import config, store
from .delta_client import DeltaError, client

# Annualization factors by candle resolution.
_BARS_PER_YEAR = {"1h": 24 * 365, "15m": 4 * 24 * 365, "1d": 365}


def _f(v) -> Optional[float]:
    try:
        return float(v) if v is not None else None
    except (TypeError, ValueError):
        return None


# --------------------------------------------------------------------------- #
# Realized volatility estimators (annualized %)
# --------------------------------------------------------------------------- #
def close_to_close_vol(candles: list[dict], bars_per_year: int) -> Optional[float]:
    closes = [float(c["close"]) for c in candles if c.get("close")]
    if len(closes) < 3:
        return None
    rets = [math.log(closes[i] / closes[i - 1]) for i in range(1, len(closes)) if closes[i - 1] > 0]
    if len(rets) < 2:
        return None
    mean = sum(rets) / len(rets)
    var = sum((r - mean) ** 2 for r in rets) / (len(rets) - 1)
    return math.sqrt(var) * math.sqrt(bars_per_year) * 100


def parkinson_vol(candles: list[dict], bars_per_year: int) -> Optional[float]:
    """High/low range estimator — ~5x more efficient than close-to-close."""
    vals = []
    for c in candles:
        h, l = _f(c.get("high")), _f(c.get("low"))
        if h and l and l > 0:
            vals.append(math.log(h / l) ** 2)
    if len(vals) < 2:
        return None
    mean_sq = sum(vals) / len(vals)
    return math.sqrt(mean_sq / (4 * math.log(2))) * math.sqrt(bars_per_year) * 100


def realized_vols(symbol: str = "BTCUSD") -> dict:
    """Trailing realized vol over several windows, from 1h candles."""
    bpy = _BARS_PER_YEAR["1h"]
    out: dict = {}
    candles = client.recent_candles(symbol, "1h", count=24 * 31, base=config.PROD_BASE)
    for days, bars in ((7, 24 * 7), (14, 24 * 14), (30, 24 * 30)):
        window = candles[-bars:]
        if len(window) < 24:
            continue
        out[f"{days}d"] = {
            "close_to_close": round(close_to_close_vol(window, bpy) or 0, 2),
            "parkinson": round(parkinson_vol(window, bpy) or 0, 2),
            "bars": len(window),
        }
    return out


# --------------------------------------------------------------------------- #
# Implied volatility term structure
# --------------------------------------------------------------------------- #
def _expiries(asset: str) -> list[str]:
    prods = client.option_products(asset, base=config.EXEC_BASE)
    iso = sorted({p["settlement_time"][:10] for p in prods
                  if p.get("settlement_time") and len(p["settlement_time"]) >= 10})
    return [f"{d[8:10]}-{d[5:7]}-{d[0:4]}" for d in iso]


def _dte(expiry_ddmmyyyy: str) -> Optional[float]:
    try:
        d, m, y = expiry_ddmmyyyy.split("-")
        settle = datetime(int(y), int(m), int(d), 12, 0, tzinfo=timezone.utc)
        return max(0.0, (settle - datetime.now(timezone.utc)).total_seconds() / 86400)
    except (ValueError, AttributeError):
        return None


def _interp_iv_at_delta(pts: list[tuple], target: float) -> Optional[float]:
    """Interpolate IV at a target option delta (e.g. +0.25 call, -0.25 put).

    Using delta rather than a fixed strike offset keeps the measure comparable
    across expiries and spot levels — it's the market-standard convention.
    """
    pts = sorted([p for p in pts if p[0] is not None and p[1] is not None], key=lambda x: x[0])
    if not pts:
        return None
    for i in range(len(pts) - 1):
        d0, iv0 = pts[i]
        d1, iv1 = pts[i + 1]
        if (d0 - target) * (d1 - target) <= 0 and d1 != d0:
            w = (target - d0) / (d1 - d0)
            return iv0 + w * (iv1 - iv0)
    return min(pts, key=lambda p: abs(p[0] - target))[1]  # nearest available


def surface_by_expiry(asset: str, max_expiries: int = 8) -> list[dict]:
    """Full IV surface per expiry: ATM level, skew metrics, and the smile.

    Skew metrics (market convention, in volatility points):
      RR25 = IV(25d call) - IV(25d put)
             >0 calls richer (upside demand), <0 puts richer (crash premium)
      BF25 = mean(IV 25d call, IV 25d put) - IV(ATM)
             how rich the WINGS are versus the body of the smile
    """
    out = []
    for exp in _expiries(asset)[:max_expiries]:
        try:
            tickers = client.option_tickers(asset, exp, base=config.EXEC_BASE)
        except DeltaError:
            continue
        if not tickers:
            continue
        spot = None
        by_strike: dict[float, dict] = {}
        for t in tickers:
            sp = _f(t.get("spot_price"))
            if sp:
                spot = sp
            k = _f(t.get("strike_price"))
            if k is None:
                continue
            entry = by_strike.setdefault(k, {})
            entry["call" if t.get("contract_type") == "call_options" else "put"] = t
        if not by_strike or not spot:
            continue

        points, call_pts, put_pts = [], [], []
        for k in sorted(by_strike):
            row = {"strike": k, "moneyness": round(k / spot, 4),
                   "call_iv": None, "put_iv": None, "call_delta": None, "put_delta": None}
            for side, key in (("call", "call"), ("put", "put")):
                t = by_strike[k].get(key)
                if not t:
                    continue
                iv = _f((t.get("quotes") or {}).get("mark_iv"))
                dlt = _f((t.get("greeks") or {}).get("delta"))
                if iv:
                    row[f"{side}_iv"] = round(iv * 100, 2)  # Delta reports IV as a fraction
                row[f"{side}_delta"] = round(dlt, 4) if dlt is not None else None
                if iv and dlt is not None:
                    (call_pts if side == "call" else put_pts).append((dlt, iv * 100))
            points.append(row)

        atm_k = min(by_strike, key=lambda k: abs(k - spot))
        atm_ivs = []
        for side in ("call", "put"):
            t = by_strike[atm_k].get(side)
            if t:
                iv = _f((t.get("quotes") or {}).get("mark_iv"))
                if iv:
                    atm_ivs.append(iv * 100)
        if not atm_ivs:
            continue
        atm_iv = sum(atm_ivs) / len(atm_ivs)

        iv_25c = _interp_iv_at_delta(call_pts, 0.25)
        iv_25p = _interp_iv_at_delta(put_pts, -0.25)
        rr25 = round(iv_25c - iv_25p, 2) if (iv_25c is not None and iv_25p is not None) else None
        bf25 = (round((iv_25c + iv_25p) / 2 - atm_iv, 2)
                if (iv_25c is not None and iv_25p is not None) else None)

        out.append({
            "expiry": exp,
            "dte": round(_dte(exp) or 0, 2),
            "atm_strike": atm_k,
            "atm_iv": round(atm_iv, 2),
            "iv_25d_call": round(iv_25c, 2) if iv_25c is not None else None,
            "iv_25d_put": round(iv_25p, 2) if iv_25p is not None else None,
            "rr25": rr25,
            "bf25": bf25,
            "spot": spot,
            "smile": points,
        })
    return out


# --------------------------------------------------------------------------- #
def analyze(asset: str = None) -> dict:
    """Full VRP snapshot: realized vol, IV term structure, and the spread."""
    asset = asset or config.ASSET
    rv = realized_vols(f"{asset}USD")
    term = surface_by_expiry(asset)

    def match_rv(dte: float) -> tuple[str, Optional[float]]:
        """Pick the trailing RV window closest to the option's tenor."""
        if not rv:
            return "n/a", None
        best = min(rv.keys(), key=lambda k: abs(int(k[:-1]) - max(dte, 0.5)))
        return best, rv[best]["parkinson"]

    rows = []
    for t in term:
        win, r = match_rv(t["dte"])
        vrp = round(t["atm_iv"] - r, 2) if r is not None else None
        rows.append({**t, "rv_window": win, "realized_vol": r, "vrp": vrp})

    vrps = [r["vrp"] for r in rows if r["vrp"] is not None]
    avg_vrp = round(sum(vrps) / len(vrps), 2) if vrps else None
    positive = sum(1 for v in vrps if v > 0)

    rrs = [r["rr25"] for r in rows if r.get("rr25") is not None]
    bfs = [r["bf25"] for r in rows if r.get("bf25") is not None]
    avg_rr25 = round(sum(rrs) / len(rrs), 2) if rrs else None
    avg_bf25 = round(sum(bfs) / len(bfs), 2) if bfs else None

    return {
        "asset": asset,
        "generated_at": int(time.time()),
        "realized_vol": rv,
        "term_structure": rows,
        "avg_vrp": avg_vrp,
        "expiries_positive": positive,
        "expiries_total": len(vrps),
        "avg_rr25": avg_rr25,
        "avg_bf25": avg_bf25,
        "skew_note": "RR25 = IV(25d call) - IV(25d put): negative means puts are richer "
                     "(the market pays up for crash protection). BF25 = wings vs ATM.",
        "note": "VRP = ATM IV minus tenor-matched trailing realized vol (Parkinson). "
                "Persistently positive VRP means options are priced above subsequent "
                "movement, i.e. sellers are paid a risk premium. This is an "
                "EXPECTED-VALUE edge that compensates for tail risk — selling options "
                "loses abruptly in a vol spike. Trailing RV is a proxy; snapshots "
                "accumulate the forward series needed for a true test.",
    }


def snapshot(asset: str = None) -> dict:
    """Run the analysis and persist it, building the forward IV series.

    Called periodically by the app so the dataset accumulates passively — a
    proper VRP test compares IV recorded TODAY against realized vol measured
    over the option's remaining life, which only forward data can provide.
    """
    a = analyze(asset)
    rv = a.get("realized_vol") or {}
    def _rv(k):
        return (rv.get(k) or {}).get("parkinson")
    rows = [{
        "ts": a["generated_at"], "asset": a["asset"], "expiry": t["expiry"],
        "dte": t["dte"], "atm_iv": t["atm_iv"], "spot": t["spot"],
        "rv7": _rv("7d"), "rv14": _rv("14d"), "rv30": _rv("30d"),
        "iv_25d_call": t.get("iv_25d_call"), "iv_25d_put": t.get("iv_25d_put"),
        "rr25": t.get("rr25"), "bf25": t.get("bf25"),
    } for t in a.get("term_structure", [])]
    written = store.record_iv_snapshot(rows)
    a["snapshot_rows_written"] = written
    return a

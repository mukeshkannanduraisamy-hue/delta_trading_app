"""Simulated ATM option premium series (Section 2.4).

=============================== ASSUMPTION ===================================
Delta does not serve historical option tick data, so the premium series here is
SYNTHESIZED from spot. Every result derived from it is an approximation, not a
measurement. Specifically:

    premium = intrinsic + time_value
    intrinsic   = max(spot - strike, 0)      for CE
                = max(strike - spot, 0)      for PE
    time_value  = spot * IV * sqrt(T) * 0.4  (simplified BS-ATM approximation)
    IV          = trailing realized volatility, annualized from 1m log returns
    T           = days_to_expiry / 365

What this model DOES capture: directional delta response, theta decay as T->0,
and vega response to changing realized vol.

What it does NOT capture: the bid/ask spread (documented at ~15% on Delta's
testnet option book), volatility smile/skew, IV crush after events, the gap
between implied and realized vol (the variance risk premium), and the fact that
real ATM options are quoted in discrete ticks on a thin book.

Every one of those omissions biases results OPTIMISTIC. Treat a positive result
from this series as "not yet disproven", never as an edge.
==============================================================================
"""

from __future__ import annotations

import numpy as np

MIN_IV = 0.15          # floor: crypto ATM IV rarely prints below ~15%
MAX_IV = 3.00
TV_COEF = 0.4          # the 0.4 in spot * IV * sqrt(T)
STRIKE_STEP = 200.0    # Delta lists BTC strikes on a 200-wide grid


def realized_vol(close: np.ndarray, window: int, bars_per_year: float) -> np.ndarray:
    """Trailing annualized realized vol from log returns."""
    lr = np.zeros(len(close))
    lr[1:] = np.log(close[1:] / close[:-1])
    c1 = np.cumsum(np.insert(lr, 0, 0.0))
    c2 = np.cumsum(np.insert(lr * lr, 0, 0.0))
    n = len(close)
    out = np.full(n, np.nan)
    s1 = c1[window:] - c1[:-window]
    s2 = c2[window:] - c2[:-window]
    var = np.maximum(s2 / window - (s1 / window) ** 2, 0.0)
    out[window - 1:] = np.sqrt(var) * np.sqrt(bars_per_year)
    return np.clip(out, MIN_IV, MAX_IV)


def atm_premium_series(d: dict, bar_seconds: int, *, rv_window: int = 1440,
                       roll_hours: float = 24.0, dte_hours: float = 24.0):
    """Build synthetic ATM CE and PE premium OHLC series.

    A fresh ATM contract is struck every `roll_hours` (strike = spot at the roll,
    snapped to the 200-wide grid) and expires `dte_hours` later. Holding the
    strike fixed between rolls is what lets the premium respond to direction —
    a perpetually-ATM strike would have zero delta response and would make the
    series useless for testing a directional signal.
    """
    t, o, h, l, c = d["time"], d["open"], d["high"], d["low"], d["close"]
    n = len(c)
    bars_per_year = 365.0 * 86400.0 / bar_seconds
    iv = realized_vol(c, rv_window, bars_per_year)
    iv = np.where(np.isnan(iv), MIN_IV, iv)

    roll_s = roll_hours * 3600.0
    dte_s = dte_hours * 3600.0
    cycle = ((t - t[0]) // roll_s).astype(np.int64)
    # Strike is set at the FIRST bar of each cycle and held (no look-ahead: the
    # strike is knowable at the moment it is struck).
    first_idx = np.zeros(n, dtype=np.int64)
    _, starts = np.unique(cycle, return_index=True)
    for s_i in range(len(starts)):
        lo = starts[s_i]
        hi = starts[s_i + 1] if s_i + 1 < len(starts) else n
        first_idx[lo:hi] = lo
    strike = np.round(c[first_idx] / STRIKE_STEP) * STRIKE_STEP
    expiry_t = t[first_idx] + dte_s
    T = np.maximum((expiry_t - t) / (365.0 * 86400.0), 1.0 / (365.0 * 24.0 * 60.0))

    def leg(px: np.ndarray, is_call: bool) -> np.ndarray:
        intrinsic = np.maximum(px - strike, 0.0) if is_call else np.maximum(strike - px, 0.0)
        return intrinsic + px * iv * np.sqrt(T) * TV_COEF

    out = {}
    for side, is_call in (("CE", True), ("PE", False)):
        po, pc = leg(o, is_call), leg(c, is_call)
        # A call's premium is monotone increasing in spot, a put's decreasing —
        # so the bar's high maps from spot-high for CE and spot-low for PE.
        ph = leg(h if is_call else l, is_call)
        pl = leg(l if is_call else h, is_call)
        out[side] = {
            "time": t, "open": po, "close": pc,
            "high": np.maximum.reduce([ph, po, pc]),
            "low": np.minimum.reduce([pl, po, pc]),
            "volume": d["volume"],
            "strike": strike, "iv": iv, "T": T,
        }
    return out


def describe(series: dict) -> dict:
    ce = series["CE"]
    return {
        "iv_median": round(float(np.median(ce["iv"])), 4),
        "iv_p5": round(float(np.percentile(ce["iv"], 5)), 4),
        "iv_p95": round(float(np.percentile(ce["iv"], 95)), 4),
        "ce_premium_median": round(float(np.median(ce["close"])), 2),
        "pe_premium_median": round(float(np.median(series["PE"]["close"])), 2),
        "ce_premium_min": round(float(ce["close"].min()), 2),
        "ce_premium_max": round(float(ce["close"].max()), 2),
    }

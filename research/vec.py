"""Vectorized indicators + signal generators + trade simulator.

WHY THIS EXISTS
---------------
Phase 2 needs 274 full backtests (parameter grids) over 90 days of 1m data
(~129k bars). Calling the real `evaluate()` per bar recomputes every indicator
over a 120-bar window each time — roughly 10^8 float ops per strategy per run,
which is hours in pure Python.

These are numpy replicas that compute each indicator ONCE over the full series.
`equivalence.py` proves each replica reproduces the real `evaluate()` signal for
signal on the same data before any of it is trusted. If a replica ever diverges,
the study is invalid — that check is not optional.

INDICATOR SEEDING NOTE
----------------------
The live engine feeds evaluate() a rolling window of `lookback` bars, so its EMA
is seeded from the SMA of the first `period` bars OF THAT WINDOW. Computing over
the full series seeds once at the start. After ~5 * period bars the two converge
to within floating-point noise; the equivalence check quantifies the residual.
"""

from __future__ import annotations

import numpy as np

# --------------------------------------------------------------------------- #
# Indicators — aligned to input length, np.nan during warm-up
# --------------------------------------------------------------------------- #


def sma(v: np.ndarray, period: int) -> np.ndarray:
    out = np.full(len(v), np.nan)
    if period <= 0 or len(v) < period:
        return out
    c = np.cumsum(np.insert(v, 0, 0.0))
    out[period - 1:] = (c[period:] - c[:-period]) / period
    return out


def ema(v: np.ndarray, period: int) -> np.ndarray:
    """EMA seeded with the SMA of the first `period` values (matches indicators.ema)."""
    n = len(v)
    out = np.full(n, np.nan)
    if period <= 0 or n < period:
        return out
    k = 2.0 / (period + 1)
    prev = v[:period].mean()
    out[period - 1] = prev
    one_k = 1.0 - k
    for i in range(period, n):          # recursive: no closed vector form
        prev = v[i] * k + prev * one_k
        out[i] = prev
    return out


def ema_windowed(v: np.ndarray, period: int, lookback: int) -> np.ndarray:
    """EMA computed the way PRODUCTION computes it: independently inside each
    rolling `lookback` window, re-seeded from that window's first `period` bars.

    This matters when lookback is not comfortably larger than period. EMA(50)
    needs roughly 5*period bars to forget its seed, so production's lookback=120
    leaves SwingKing's 50-EMA only partially converged — a full-series EMA does
    NOT reproduce it. (That short lookback is arguably a production weakness in
    its own right; it is reported as a finding rather than silently "fixed",
    because the job here is to backtest the strategy as deployed.)

    Vectorized across windows: the recursion runs along the window axis, so it
    is `lookback` numpy ops on n-length vectors rather than n*lookback scalar
    steps.
    """
    n = len(v)
    out = np.full(n, np.nan)
    if lookback > n or period > lookback:
        return out
    win = np.lib.stride_tricks.sliding_window_view(v, lookback)   # (n-L+1, L)
    k = 2.0 / (period + 1)
    prev = win[:, :period].mean(axis=1)
    for j in range(period, lookback):
        prev = win[:, j] * k + prev * (1.0 - k)
    out[lookback - 1:] = prev
    return out


def true_range(h: np.ndarray, l: np.ndarray, c: np.ndarray) -> np.ndarray:
    tr = np.empty(len(h))
    tr[0] = h[0] - l[0]
    pc = c[:-1]
    tr[1:] = np.maximum(h[1:] - l[1:], np.maximum(np.abs(h[1:] - pc), np.abs(l[1:] - pc)))
    return tr


def atr(h, l, c, period: int = 14) -> np.ndarray:
    """Wilder's ATR (RMA of true range) — matches indicators.atr."""
    n = len(h)
    out = np.full(n, np.nan)
    if n < period:
        return out
    tr = true_range(h, l, c)
    prev = tr[:period].mean()
    out[period - 1] = prev
    for i in range(period, n):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(v: np.ndarray, period: int = 20, k: float = 2.0):
    """Population std (ddof=0) — matches indicators.bollinger."""
    mid = sma(v, period)
    n = len(v)
    upper = np.full(n, np.nan)
    lower = np.full(n, np.nan)
    if n < period:
        return mid, upper, lower
    c1 = np.cumsum(np.insert(v, 0, 0.0))
    c2 = np.cumsum(np.insert(v * v, 0, 0.0))
    s1 = c1[period:] - c1[:-period]
    s2 = c2[period:] - c2[:-period]
    var = np.maximum(s2 / period - (s1 / period) ** 2, 0.0)
    sd = np.sqrt(var)
    upper[period - 1:] = mid[period - 1:] + k * sd
    lower[period - 1:] = mid[period - 1:] - k * sd
    return mid, upper, lower


def supertrend(h, l, c, period: int = 10, mult: float = 3.0):
    """Classic ATR Supertrend. Mirrors indicators.supertrend exactly, including
    the first-valid-bar seeding branch."""
    n = len(h)
    a = atr(h, l, c, period)
    line = np.full(n, np.nan)
    direction = np.zeros(n, dtype=np.int8)
    fu = np.full(n, np.nan)
    fl = np.full(n, np.nan)
    hl2 = (h + l) / 2.0
    seeded = False
    for i in range(n):
        if np.isnan(a[i]):
            continue
        bu = hl2[i] + mult * a[i]
        bl = hl2[i] - mult * a[i]
        pc = c[i - 1] if i > 0 else c[i]
        if not seeded:
            fu[i], fl[i] = bu, bl
            direction[i] = 1 if c[i] >= bl else -1
            line[i] = bl if direction[i] == 1 else bu
            seeded = True
            continue
        fu[i] = bu if (bu < fu[i - 1] or pc > fu[i - 1]) else fu[i - 1]
        fl[i] = bl if (bl > fl[i - 1] or pc < fl[i - 1]) else fl[i - 1]
        pd = direction[i - 1] if direction[i - 1] != 0 else 1
        if pd == 1:
            direction[i] = -1 if c[i] < fl[i] else 1
        else:
            direction[i] = 1 if c[i] > fu[i] else -1
        line[i] = fl[i] if direction[i] == 1 else fu[i]
    return line, direction


def slope(v: np.ndarray, lookback: int = 3) -> np.ndarray:
    """Average per-bar change over `lookback` steps — matches indicators.slope."""
    out = np.full(len(v), np.nan)
    if len(v) > lookback:
        out[lookback:] = (v[lookback:] - v[:-lookback]) / lookback
    return out


# --------------------------------------------------------------------------- #
# Signal generators
#
# Each returns an int8 array: +1 = CE (long/bullish), -1 = PE (short/bearish),
# 0 = no signal, evaluated AT THE CLOSE of that bar. The simulator fills at the
# NEXT bar's open, so there is no look-ahead.
# --------------------------------------------------------------------------- #


def _green(o, c):
    return c >= o


def sig_ema_cross(d, fast=9, slow=21, **_):
    c = d["close"]
    f, s = ema(c, fast), ema(c, slow)
    out = np.zeros(len(c), dtype=np.int8)
    ok = ~np.isnan(f) & ~np.isnan(s)
    ok[1:] &= ok[:-1]
    ok[0] = False
    up = np.zeros(len(c), dtype=bool)
    dn = np.zeros(len(c), dtype=bool)
    up[1:] = (f[:-1] <= s[:-1]) & (f[1:] > s[1:])
    dn[1:] = (f[:-1] >= s[:-1]) & (f[1:] < s[1:])
    out[ok & up] = 1
    out[ok & dn] = -1
    return out


def sig_scalping_pulse(d, fast=9, trend=21, **_):
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    f, s = ema(c, fast), ema(c, trend)
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    # "pullback" = any of the last 3 bars reached the fast EMA
    lo3 = np.full(n, np.nan)
    hi3 = np.full(n, np.nan)
    lo3[2:] = np.minimum(np.minimum(l[2:], l[1:-1]), l[:-2])
    hi3[2:] = np.maximum(np.maximum(h[2:], h[1:-1]), h[:-2])
    g = _green(o, c)
    ok = ~np.isnan(f) & ~np.isnan(s) & ~np.isnan(lo3)
    ok[:3] = False
    prev_h = np.roll(h, 1)
    prev_l = np.roll(l, 1)
    ce = ok & (f > s) & (lo3 <= f) & g & (c > prev_h)
    pe = ok & (f < s) & (hi3 >= f) & ~g & (c < prev_l)
    out[ce] = 1
    out[pe] = -1
    return out


def sig_traffic_light(d, lookback=2, sma_period=15, **_):
    """Break of an N-candle opposite-coloured cluster's range.

    lookback=2 reproduces the original two-candle pair (u[-3], u[-2]).
    """
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    g = _green(o, c)
    s = sma(c, sma_period)
    lb = int(lookback)
    if n < lb + 2:
        return out
    idx = np.arange(lb + 1, n)          # break bar
    # Cluster = the lb bars immediately BEFORE the break bar: [i-lb .. i-1].
    # (Original: a=u[-3], b=u[-2], break=u[-1] -> i-2, i-1 for lb=2.)
    first = g[idx - lb]
    flip = np.zeros(len(idx), dtype=bool)
    for j in range(1, lb):
        flip |= (g[idx - lb + j] != first)
    if lb == 1:
        flip = np.ones(len(idx), dtype=bool)   # single bar: no pair to flip
    upper = np.full(len(idx), -np.inf)
    lower = np.full(len(idx), np.inf)
    for j in range(lb):
        upper = np.maximum(upper, h[idx - lb + j])
        lower = np.minimum(lower, l[idx - lb + j])
    cb = c[idx]
    trend = s[idx]
    tr_ok_up = np.isnan(trend) | (cb >= trend)
    tr_ok_dn = np.isnan(trend) | (cb <= trend)
    out[idx[flip & (cb > upper) & tr_ok_up]] = 1
    out[idx[flip & (cb < lower) & tr_ok_dn]] = -1
    return out


def sig_inside_candle(d, proximity=0.35, **_):
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    if n < 3:
        return out
    i = np.arange(2, n)
    mh, ml = h[i - 2], l[i - 2]          # mother
    bh, bl = h[i - 1], l[i - 1]          # baby
    rng = mh - ml
    inside = (bh <= mh) & (bl >= ml) & (rng > 0)
    cb = c[i]
    ce = inside & (cb > mh) & ((cb - mh) <= proximity * rng)
    pe = inside & (cb < ml) & ((ml - cb) <= proximity * rng)
    out[i[ce]] = 1
    out[i[pe]] = -1
    return out


def sig_mean_rev_bollinger(d, period=20, k=2.0, **_):
    c = d["close"]
    _, up, lo = bollinger(c, period, k)
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    ok = ~np.isnan(up)
    ok[1:] &= ok[:-1]
    ok[0] = False
    cross_up = np.zeros(n, dtype=bool)
    cross_dn = np.zeros(n, dtype=bool)
    cross_up[1:] = (c[1:] > up[1:]) & (c[:-1] <= up[:-1])
    cross_dn[1:] = (c[1:] < lo[1:]) & (c[:-1] >= lo[:-1])
    out[ok & cross_up] = -1              # fade the upside -> PE
    out[ok & cross_dn] = 1               # fade the downside -> CE
    return out


def sig_prime_scalper(d, ema_period=21, atr_period=14, threshold=0.05, **_):
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    e = ema(c, ema_period)
    a = atr(h, l, c, atr_period)
    sl = slope(e, 3)
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    norm = np.full(n, np.nan)
    valid = ~np.isnan(sl) & ~np.isnan(a) & (a != 0)
    norm[valid] = sl[valid] / a[valid]
    prev = np.roll(norm, 1)
    g = _green(o, c)
    ok = ~np.isnan(norm) & ~np.isnan(prev) & ~np.isnan(e)
    ok[0] = False
    ce = ok & (prev <= threshold) & (norm > threshold) & (c > e) & g
    pe = ok & (prev >= -threshold) & (norm < -threshold) & (c < e) & ~g
    out[ce] = 1
    out[pe] = -1
    return out


def sig_swingking(d, fast=20, slow=50, lookback=120, **_):
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    # Windowed EMA: with lookback=120 a 50-period EMA has NOT converged, so the
    # full-series form would diverge from production. See ema_windowed().
    e20 = ema_windowed(c, fast, lookback)
    e50 = ema_windowed(c, slow, lookback)
    n = len(c)
    out = np.zeros(n, dtype=np.int8)
    lo3 = np.full(n, np.nan)
    hi3 = np.full(n, np.nan)
    lo3[2:] = np.minimum(np.minimum(l[2:], l[1:-1]), l[:-2])
    hi3[2:] = np.maximum(np.maximum(h[2:], h[1:-1]), h[:-2])
    e20_prev3 = np.roll(e20, 3)
    rising = e20 > e20_prev3
    falling = e20 < e20_prev3
    g = _green(o, c)
    prev_h, prev_l = np.roll(h, 1), np.roll(l, 1)
    ok = ~np.isnan(e20) & ~np.isnan(e50) & ~np.isnan(e20_prev3) & ~np.isnan(lo3)
    ok[:4] = False
    ce = ok & (e20 > e50) & rising & (lo3 <= e20) & g & (c > prev_h)
    pe = ok & (e20 < e50) & falling & (hi3 >= e20) & ~g & (c < prev_l)
    out[ce] = 1
    out[pe] = -1
    return out


def sig_booming_bulls(d, sma_period=20, fast_p=10, fast_m=2.0,
                      slow_p=20, slow_m=3.0, **_):
    """Structure-becomes-true on the series supplied.

    In production this reads OPTION PREMIUM candles. Here it is applied to the
    simulated premium series (see premium.py) — never to spot, which would be a
    different strategy entirely.
    """
    o, h, l, c = d["open"], d["high"], d["low"], d["close"]
    s = sma(c, sma_period)
    fl, _ = supertrend(h, l, c, fast_p, fast_m)
    sl_, _ = supertrend(h, l, c, slow_p, slow_m)
    above = (c > s) & (c > fl) & (c > sl_)
    above &= ~np.isnan(s) & ~np.isnan(fl) & ~np.isnan(sl_)
    prev = np.roll(above, 1)
    prev[0] = False
    out = np.zeros(len(c), dtype=np.int8)
    out[above & ~prev] = 1               # long the option only
    return out


GENERATORS = {
    "ema_cross": sig_ema_cross,
    "scalping_pulse": sig_scalping_pulse,
    "traffic_light": sig_traffic_light,
    "inside_candle": sig_inside_candle,
    "mean_reversion_bollinger": sig_mean_rev_bollinger,
    "prime_scalper_ema": sig_prime_scalper,
    "swingking_sniper": sig_swingking,
    "booming_bulls_supertrend": sig_booming_bulls,
}

# Native timeframe and default reward:risk, mirroring zing_strategies.py
DEFAULTS = {
    "ema_cross":                {"tf": "1m", "rr": 1.2, "params": {"fast": 9, "slow": 21}},
    "scalping_pulse":           {"tf": "1m", "rr": 1.0, "params": {"fast": 9, "trend": 21}},
    "traffic_light":            {"tf": "1m", "rr": 1.2, "params": {"lookback": 2}},
    "inside_candle":            {"tf": "5m", "rr": 2.0, "params": {"proximity": 0.35}},
    "mean_reversion_bollinger": {"tf": "1m", "rr": 1.5, "params": {"period": 20, "k": 2.0}},
    "prime_scalper_ema":        {"tf": "1m", "rr": 1.2, "params": {"ema_period": 21, "atr_period": 14, "threshold": 0.05}},
    "swingking_sniper":         {"tf": "5m", "rr": 2.0, "params": {"fast": 20, "slow": 50}},
    "booming_bulls_supertrend": {"tf": "1m", "rr": 1.5, "params": {}},
}

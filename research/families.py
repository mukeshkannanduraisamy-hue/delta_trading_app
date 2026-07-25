"""Additional signal families and gates.

WHY MOSTLY GATES
----------------
The existing 8 strategies have ~0 gross edge, which means their entries are
coin flips. Another coin flip does not help. What does help is taking fewer
flips in unfavourable conditions: a gate that cuts trade count 70% while
leaving gross edge flat removes most of the cost bleed, which is a larger
effect than any realistic entry improvement.

NO LOOK-AHEAD
-------------
Every value at index i uses only data at index <= i. Warm-up regions emit
0 (signals) or False (gates) rather than a guess.

PERFORMANCE
-----------
The 1m series is ~525k bars and the search evaluates hundreds of candidates,
so every rolling statistic here is O(n) via cumulative sums or an O(n) deque
scan. A naive per-bar window would be O(n*w) and make the sweep intractable.
"""

from __future__ import annotations

import numpy as np

from research.vec import atr as _atr, ema as _ema


def _shift(a: np.ndarray, k: int) -> np.ndarray:
    """a shifted forward k bars; the first k entries are NaN."""
    out = np.full(len(a), np.nan, dtype="float64")
    if k < len(a):
        out[k:] = a[:len(a) - k]
    return out


def _roll_mean_std(a: np.ndarray, w: int):
    """Rolling mean/std over the w bars ENDING at i-1 (strictly prior).

    O(n) via cumulative sums. Using prior-only windows is what keeps the
    statistic free of the current bar it is being used to judge.
    """
    n = len(a)
    x = np.nan_to_num(a, nan=0.0)
    ok = np.isfinite(a).astype("float64")
    c1 = np.concatenate([[0.0], np.cumsum(x)])
    c2 = np.concatenate([[0.0], np.cumsum(x * x)])
    cn = np.concatenate([[0.0], np.cumsum(ok)])

    mean = np.full(n, np.nan)
    std = np.full(n, np.nan)
    if n <= w:
        return mean, std
    idx = np.arange(w, n)
    cnt = cn[idx] - cn[idx - w]
    s1 = c1[idx] - c1[idx - w]
    s2 = c2[idx] - c2[idx - w]
    good = cnt >= max(2.0, w * 0.5)
    m = np.where(good, s1 / np.maximum(cnt, 1), np.nan)
    v = np.where(good, s2 / np.maximum(cnt, 1) - m * m, np.nan)
    mean[idx] = m
    std[idx] = np.sqrt(np.maximum(v, 0.0))
    return mean, std


def _roll_max(a: np.ndarray, w: int) -> np.ndarray:
    """Rolling max over the w bars ENDING at i-1, O(n) monotonic deque."""
    from collections import deque
    n = len(a)
    out = np.full(n, np.nan)
    dq: deque = deque()
    for i in range(n):
        if i > 0:
            while dq and a[dq[-1]] <= a[i - 1]:
                dq.pop()
            dq.append(i - 1)
            while dq[0] <= i - 1 - w:
                dq.popleft()
            if i >= w:
                out[i] = a[dq[0]]
    return out


def _roll_min(a: np.ndarray, w: int) -> np.ndarray:
    return -_roll_max(-a, w)


# ---------------------------------------------------------------- entries

def momentum_persistence(d: dict, lookback: int = 12,
                         persist: int = 3) -> np.ndarray:
    """Enter with N-bar momentum once its sign has held `persist` bars.

    Trend-following amortizes a fixed per-trade cost over a larger move, which
    is the structural reason to look above 1m. Seven of the existing eight
    strategies are 1m.
    """
    c = d["close"]
    n = len(c)
    sig = np.zeros(n, dtype="int8")
    if n <= lookback + persist + 1:
        return sig

    mom = c - _shift(c, lookback)
    s = np.sign(np.nan_to_num(mom, nan=0.0))

    held = np.ones(n, dtype=bool)
    for k in range(1, persist):
        prev = np.concatenate([np.zeros(k), s[:n - k]])
        held &= (prev == s)
    held &= s != 0

    valid = np.zeros(n, dtype=bool)
    valid[lookback + persist:] = True
    sig[valid & held & (s > 0)] = 1
    sig[valid & held & (s < 0)] = -1
    return sig


def breakout_volume(d: dict, channel: int = 20,
                    vol_z: float = 1.5) -> np.ndarray:
    """Donchian break confirmed by a volume z-score.

    The existing breakout strategies (traffic_light, inside_candle) ignore
    volume entirely, and volume is the standard false-breakout filter.
    """
    c, v = d["close"], d["volume"]
    n = len(c)
    sig = np.zeros(n, dtype="int8")
    if n <= channel + 2:
        return sig

    hi = _roll_max(c, channel)
    lo = _roll_min(c, channel)
    vm, vs = _roll_mean_std(v, channel)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(vs > 0, (v - vm) / np.where(vs > 0, vs, 1.0), 0.0)

    ok = np.isfinite(hi) & np.isfinite(lo) & np.isfinite(z) & (z >= vol_z)
    sig[ok & (c > hi)] = 1
    sig[ok & (c < lo)] = -1
    return sig


# ------------------------------------------------------------------ gates

def vol_regime_gate(d: dict, atr_period: int = 14, window: int = 1440,
                    lo_z: float = -1.0, hi_z: float = 1.0) -> np.ndarray:
    """Admit only bars whose ATR sits in a middle volatility band.

    Uses a rolling z-score of ATR rather than a percentile rank: a true
    per-bar percentile against all prior bars is O(n^2) and intractable on a
    525k-bar series, while the z-score is O(n) and answers the same question
    (is current volatility ordinary for recent history, or extreme).
    """
    a = _atr(d["high"], d["low"], d["close"], atr_period)
    m, s = _roll_mean_std(a, window)
    with np.errstate(invalid="ignore", divide="ignore"):
        z = np.where(s > 0, (a - m) / np.where(s > 0, s, 1.0), np.nan)
    return np.isfinite(z) & (z >= lo_z) & (z <= hi_z)


def session_gate(d: dict, hours: tuple = (13, 14, 15, 16)) -> np.ndarray:
    """Admit only bars whose UTC hour is in `hours`.

    Crypto has real intraday seasonality (Asia/London/US opens) that none of
    the existing strategies use.
    """
    hrs = (np.asarray(d["time"]) // 3600) % 24
    return np.isin(hrs, np.asarray(hours))


def htf_trend_gate(d: dict, htf_close: np.ndarray,
                   fast: int = 20, slow: int = 50) -> np.ndarray:
    """Admit bars where the higher-timeframe trend agrees (classic chop defence).

    `htf_close` must already be forward-filled onto d's bar grid by the caller
    using only CLOSED higher-timeframe bars.
    """
    n = len(d["close"])
    if len(htf_close) != n:
        raise ValueError("htf_close must be aligned to d's bar grid")
    f, s = _ema(htf_close, fast), _ema(htf_close, slow)
    return np.isfinite(f) & np.isfinite(s) & (f > s)


def vrp_gate(iv: np.ndarray, rv: np.ndarray,
             max_ratio: float = 1.5) -> np.ndarray:
    """Block long-premium entries when implied vol is far above realized.

    The one genuinely options-native gate: at high IV/RV you overpay for the
    option regardless of direction. Note the 2026-07-25 snapshot had ATM IV
    BELOW realized vol, so on current data this gate rarely binds -- it is
    retained because the regime it guards against is real and recurring.
    """
    iv = np.asarray(iv, dtype="float64")
    rv = np.asarray(rv, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(rv > 0, iv / rv, np.inf)
    return np.isfinite(ratio) & (ratio <= max_ratio)


def apply_gates(signals: np.ndarray, *gates: np.ndarray) -> np.ndarray:
    """AND every gate onto the signal. Gates can only REMOVE signals."""
    out = np.array(signals, dtype="int8", copy=True)
    for g in gates:
        g = np.asarray(g, dtype=bool)
        if len(g) != len(out):
            raise ValueError("gate length does not match signal length")
        out[~g] = 0
    return out


ENTRY_FAMILIES = {
    "momentum_persistence": momentum_persistence,
    "breakout_volume": breakout_volume,
}

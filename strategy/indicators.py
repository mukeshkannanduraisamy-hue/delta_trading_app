"""Pure-Python technical indicators used by the Zing strategies.

Everything operates on plain lists of floats (or candle dicts) so the engine has
no numpy/pandas dependency. Functions return lists aligned to the input length,
using None for the warm-up region where the indicator is not yet defined.
"""

from __future__ import annotations

import math
from typing import Optional

Candle = dict  # {"time","open","high","low","close","volume"}


def closes(candles: list[Candle]) -> list[float]:
    return [float(c["close"]) for c in candles]


def sma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0:
        return out
    running = 0.0
    for i, v in enumerate(values):
        running += v
        if i >= period:
            running -= values[i - period]
        if i >= period - 1:
            out[i] = running / period
    return out


def ema(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    k = 2.0 / (period + 1)
    # Seed with the SMA of the first `period` values.
    seed = sum(values[:period]) / period
    out[period - 1] = seed
    prev = seed
    for i in range(period, len(values)):
        prev = values[i] * k + prev * (1 - k)
        out[i] = prev
    return out


def true_ranges(candles: list[Candle]) -> list[float]:
    tr: list[float] = []
    prev_close = None
    for c in candles:
        h, l, cl = float(c["high"]), float(c["low"]), float(c["close"])
        if prev_close is None:
            tr.append(h - l)
        else:
            tr.append(max(h - l, abs(h - prev_close), abs(l - prev_close)))
        prev_close = cl
    return tr


def atr(candles: list[Candle], period: int = 14) -> list[Optional[float]]:
    """Wilder's ATR (RMA of true range)."""
    tr = true_ranges(candles)
    out: list[Optional[float]] = [None] * len(candles)
    if len(candles) < period:
        return out
    first = sum(tr[:period]) / period
    out[period - 1] = first
    prev = first
    for i in range(period, len(candles)):
        prev = (prev * (period - 1) + tr[i]) / period
        out[i] = prev
    return out


def bollinger(
    values: list[float], period: int = 20, num_std: float = 2.0
) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    """Return (middle, upper, lower) Bollinger Bands (population std-dev)."""
    mid = sma(values, period)
    upper: list[Optional[float]] = [None] * len(values)
    lower: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i >= period - 1 and mid[i] is not None:
            window = values[i - period + 1 : i + 1]
            m = mid[i]
            var = sum((x - m) ** 2 for x in window) / period
            sd = var ** 0.5
            upper[i] = m + num_std * sd
            lower[i] = m - num_std * sd
    return mid, upper, lower


def supertrend(
    candles: list[Candle], period: int = 10, multiplier: float = 3.0
) -> tuple[list[Optional[float]], list[Optional[int]]]:
    """Classic ATR-based Supertrend.

    Returns (line, direction) where direction is +1 (uptrend, price above the
    line) or -1 (downtrend). Warm-up entries are None.
    """
    n = len(candles)
    a = atr(candles, period)
    line: list[Optional[float]] = [None] * n
    direction: list[Optional[int]] = [None] * n

    final_upper: list[Optional[float]] = [None] * n
    final_lower: list[Optional[float]] = [None] * n

    for i in range(n):
        if a[i] is None:
            continue
        hl2 = (float(candles[i]["high"]) + float(candles[i]["low"])) / 2
        basic_upper = hl2 + multiplier * a[i]
        basic_lower = hl2 - multiplier * a[i]
        close_i = float(candles[i]["close"])
        prev_close = float(candles[i - 1]["close"]) if i > 0 else close_i

        # `i == 0` must be checked explicitly: final_upper[-1] would wrap to the
        # LAST element of the list, which only happens to be None because the
        # list starts empty. That is luck, not logic (audit #19).
        if i == 0 or final_upper[i - 1] is None:
            final_upper[i] = basic_upper
            final_lower[i] = basic_lower
            direction[i] = 1 if close_i >= basic_lower else -1
            line[i] = basic_lower if direction[i] == 1 else basic_upper
            continue

        final_upper[i] = (
            basic_upper
            if (basic_upper < final_upper[i - 1] or prev_close > final_upper[i - 1])
            else final_upper[i - 1]
        )
        final_lower[i] = (
            basic_lower
            if (basic_lower > final_lower[i - 1] or prev_close < final_lower[i - 1])
            else final_lower[i - 1]
        )

        prev_dir = direction[i - 1] if direction[i - 1] is not None else 1
        if prev_dir == 1:
            direction[i] = -1 if close_i < final_lower[i] else 1
        else:
            direction[i] = 1 if close_i > final_upper[i] else -1
        line[i] = final_lower[i] if direction[i] == 1 else final_upper[i]

    return line, direction


def slope(values: list[Optional[float]], i: int, lookback: int = 3) -> Optional[float]:
    """Average per-bar change of `values` over the last `lookback` steps ending at i."""
    if i < lookback or values[i] is None or values[i - lookback] is None:
        return None
    return (values[i] - values[i - lookback]) / lookback


def rsi(values: list[float], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) <= period:
        return out
    
    gains = 0.0
    losses = 0.0
    for i in range(1, period + 1):
        change = values[i] - values[i - 1]
        if change > 0:
            gains += change
        else:
            losses -= change
    
    avg_gain = gains / period
    avg_loss = losses / period
    
    if avg_loss == 0:
        out[period] = 100.0
    else:
        rs = avg_gain / avg_loss
        out[period] = 100.0 - (100.0 / (1.0 + rs))
        
    for i in range(period + 1, len(values)):
        change = values[i] - values[i - 1]
        gain = change if change > 0 else 0.0
        loss = -change if change < 0 else 0.0
        
        avg_gain = (avg_gain * (period - 1) + gain) / period
        avg_loss = (avg_loss * (period - 1) + loss) / period
        
        if avg_loss == 0:
            out[i] = 100.0
        else:
            rs = avg_gain / avg_loss
            out[i] = 100.0 - (100.0 / (1.0 + rs))
            
    return out


def macd(values: list[float], fast: int = 12, slow: int = 26, signal_period: int = 9) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    ema_fast = ema(values, fast)
    ema_slow = ema(values, slow)
    
    macd_line: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if ema_fast[i] is not None and ema_slow[i] is not None:
            macd_line[i] = ema_fast[i] - ema_slow[i]
            
    signal_line: list[Optional[float]] = [None] * len(values)
    hist: list[Optional[float]] = [None] * len(values)
    
    first_valid = 0
    while first_valid < len(macd_line) and macd_line[first_valid] is None:
        first_valid += 1
        
    if first_valid + signal_period <= len(values):
        k = 2.0 / (signal_period + 1)
        seed = sum(macd_line[first_valid:first_valid + signal_period]) / signal_period
        signal_line[first_valid + signal_period - 1] = seed
        hist[first_valid + signal_period - 1] = macd_line[first_valid + signal_period - 1] - seed
        
        prev = seed
        for i in range(first_valid + signal_period, len(values)):
            prev = macd_line[i] * k + prev * (1 - k)
            signal_line[i] = prev
            hist[i] = macd_line[i] - prev
            
    return macd_line, signal_line, hist


def stochastic_rsi(values: list[float], rsi_period: int = 14, stoch_period: int = 14, k_period: int = 3, d_period: int = 3) -> tuple[list[Optional[float]], list[Optional[float]]]:
    rsi_vals = rsi(values, rsi_period)
    
    stoch_rsi: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i >= rsi_period + stoch_period - 1:
            window = rsi_vals[i - stoch_period + 1 : i + 1]
            if None not in window:
                highest = max([x for x in window if x is not None])
                lowest = min([x for x in window if x is not None])
                if highest != lowest:
                    stoch_rsi[i] = 100.0 * (rsi_vals[i] - lowest) / (highest - lowest) # type: ignore
                else:
                    stoch_rsi[i] = 100.0 if rsi_vals[i] == highest else 0.0
                    
    k_line: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i >= rsi_period + stoch_period + k_period - 1:
            window = stoch_rsi[i - k_period + 1 : i + 1]
            if None not in window:
                k_line[i] = sum([x for x in window if x is not None]) / k_period
                
    d_line: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if i >= rsi_period + stoch_period + k_period + d_period - 2:
            window = k_line[i - d_period + 1 : i + 1]
            if None not in window:
                d_line[i] = sum([x for x in window if x is not None]) / d_period
                
    return k_line, d_line


def adx(candles: list[Candle], period: int = 14) -> tuple[list[Optional[float]], list[Optional[float]], list[Optional[float]]]:
    n = len(candles)
    adx_line: list[Optional[float]] = [None] * n
    plus_di: list[Optional[float]] = [None] * n
    minus_di: list[Optional[float]] = [None] * n
    
    if n <= period:
        return adx_line, plus_di, minus_di
        
    tr = true_ranges(candles)
    
    plus_dm = [0.0] * n
    minus_dm = [0.0] * n
    
    for i in range(1, n):
        h1, l1 = float(candles[i]["high"]), float(candles[i]["low"])
        h0, l0 = float(candles[i-1]["high"]), float(candles[i-1]["low"])
        up_move = h1 - h0
        down_move = l0 - l1
        if up_move > down_move and up_move > 0:
            plus_dm[i] = up_move
        if down_move > up_move and down_move > 0:
            minus_dm[i] = down_move
            
    def wilder_smooth(data: list[float], period: int) -> list[Optional[float]]:
        out: list[Optional[float]] = [None] * len(data)
        if len(data) < period:
            return out
        out[period-1] = sum(data[:period])
        prev = out[period-1]
        for i in range(period, len(data)):
            if prev is not None:
                prev = prev - (prev / period) + data[i]
                out[i] = prev
        return out

    smooth_tr = wilder_smooth(tr, period)
    smooth_plus_dm = wilder_smooth(plus_dm, period)
    smooth_minus_dm = wilder_smooth(minus_dm, period)
    
    dx = [0.0] * n
    for i in range(period - 1, n):
        st = smooth_tr[i]
        spd = smooth_plus_dm[i]
        smd = smooth_minus_dm[i]
        if st is not None and st != 0 and spd is not None and smd is not None:
            plus_di[i] = 100.0 * spd / st
            minus_di[i] = 100.0 * smd / st
            diff = abs(plus_di[i] - minus_di[i]) # type: ignore
            summ = plus_di[i] + minus_di[i] # type: ignore
            dx[i] = 100.0 * (diff / summ) if summ != 0 else 0.0
            
    if n >= 2 * period - 1:
        adx_line[2 * period - 2] = sum(dx[period - 1 : 2 * period - 1]) / period
        prev_adx = adx_line[2 * period - 2]
        for i in range(2 * period - 1, n):
            if prev_adx is not None:
                prev_adx = (prev_adx * (period - 1) + dx[i]) / period
                adx_line[i] = prev_adx
            
    return adx_line, plus_di, minus_di


def obv(candles: list[Candle]) -> list[float]:
    out: list[float] = [0.0] * len(candles)
    if not candles:
        return out
    
    out[0] = float(candles[0].get("volume", 0.0))
    for i in range(1, len(candles)):
        close_cur = float(candles[i]["close"])
        close_prev = float(candles[i-1]["close"])
        vol = float(candles[i].get("volume", 0.0))
        
        if close_cur > close_prev:
            out[i] = out[i-1] + vol
        elif close_cur < close_prev:
            out[i] = out[i-1] - vol
        else:
            out[i] = out[i-1]
    return out


def vwap(candles: list[Candle]) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(candles)
    cum_vol = 0.0
    cum_pv = 0.0
    
    for i, c in enumerate(candles):
        h, l, cl = float(c["high"]), float(c["low"]), float(c["close"])
        vol = float(c.get("volume", 0.0))
        typ = (h + l + cl) / 3.0
        
        cum_vol += vol
        cum_pv += typ * vol
        
        if cum_vol > 0:
            out[i] = cum_pv / cum_vol
        else:
            out[i] = typ
    return out


def williams_r(candles: list[Candle], period: int = 14) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(candles)
    for i in range(len(candles)):
        if i >= period - 1:
            window = candles[i - period + 1 : i + 1]
            highest = max(float(c["high"]) for c in window)
            lowest = min(float(c["low"]) for c in window)
            close = float(candles[i]["close"])
            if highest != lowest:
                out[i] = -100.0 * (highest - close) / (highest - lowest)
            else:
                out[i] = 0.0
    return out


def cci(candles: list[Candle], period: int = 20) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(candles)
    typ_prices = [(float(c["high"]) + float(c["low"]) + float(c["close"])) / 3.0 for c in candles]
    
    for i in range(len(candles)):
        if i >= period - 1:
            window = typ_prices[i - period + 1 : i + 1]
            sma_tp = sum(window) / period
            mean_dev = sum(abs(x - sma_tp) for x in window) / period
            if mean_dev > 0:
                out[i] = (typ_prices[i] - sma_tp) / (0.015 * mean_dev)
            else:
                out[i] = 0.0
    return out


def pivot_points(candles: list[Candle]) -> dict:
    if not candles:
        return {"pivot": 0.0, "s1": 0.0, "s2": 0.0, "s3": 0.0, "r1": 0.0, "r2": 0.0, "r3": 0.0}
    last = candles[-1]
    h, l, c = float(last["high"]), float(last["low"]), float(last["close"])
    p = (h + l + c) / 3.0
    return {
        "pivot": p,
        "s1": (p * 2) - h,
        "s2": p - (h - l),
        "s3": l - 2 * (h - p),
        "r1": (p * 2) - l,
        "r2": p + (h - l),
        "r3": h + 2 * (p - l),
    }


def swing_highs_lows(candles: list[Candle], lookback: int = 5) -> tuple[list[float], list[float]]:
    highs = []
    lows = []
    for i in range(lookback, len(candles) - lookback):
        is_high = True
        is_low = True
        h = float(candles[i]["high"])
        l = float(candles[i]["low"])
        for j in range(1, lookback + 1):
            if float(candles[i - j]["high"]) > h or float(candles[i + j]["high"]) > h:
                is_high = False
            if float(candles[i - j]["low"]) < l or float(candles[i + j]["low"]) < l:
                is_low = False
        if is_high:
            highs.append(h)
        if is_low:
            lows.append(l)
    return highs, lows


def fibonacci_levels(high: float, low: float) -> dict:
    diff = high - low
    return {
        0: low,
        0.236: low + diff * 0.236,
        0.382: low + diff * 0.382,
        0.5: low + diff * 0.5,
        0.618: low + diff * 0.618,
        0.786: low + diff * 0.786,
        1.0: high
    }


def wma(values: list[float], period: int) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 0 or len(values) < period:
        return out
    
    denom = period * (period + 1) / 2.0
    for i in range(period - 1, len(values)):
        val = 0.0
        for j in range(period):
            val += values[i - j] * (period - j)
        out[i] = val / denom
    return out


def hma(values: list[float], period: int = 9) -> list[Optional[float]]:
    out: list[Optional[float]] = [None] * len(values)
    if period <= 1 or len(values) < period:
        return out
        
    half_length = int(period / 2)
    sqrt_length = int(math.sqrt(period))
    
    wma1 = wma(values, half_length)
    wma2 = wma(values, period)
    
    diff_wma: list[Optional[float]] = [None] * len(values)
    for i in range(len(values)):
        if wma1[i] is None or wma2[i] is None:
            diff_wma[i] = None
        else:
            diff_wma[i] = 2.0 * wma1[i] - wma2[i]
        
    denom = sqrt_length * (sqrt_length + 1) / 2.0
    start_idx = period - 1 + sqrt_length - 1
    for i in range(start_idx, len(values)):
        window = diff_wma[i - sqrt_length + 1 : i + 1]
        if any(v is None for v in window):
            continue
        val = 0.0
        for j in range(sqrt_length):
            val += diff_wma[i - j] * (sqrt_length - j)  # type: ignore[operator]
        out[i] = val / denom
        
    return out

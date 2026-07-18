"""Pure-Python technical indicators used by the Zing strategies.

Everything operates on plain lists of floats (or candle dicts) so the engine has
no numpy/pandas dependency. Functions return lists aligned to the input length,
using None for the warm-up region where the indicator is not yet defined.
"""

from __future__ import annotations

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

        if final_upper[i - 1] is None:
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

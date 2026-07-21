"""Directional backtest harness for the Zing strategies.

Replays historical BTCUSD perp candles bar-by-bar, runs each strategy's real
`evaluate()` on the rolling window, and measures the DIRECTIONAL outcome of each
signal on the underlying — did price reach a +target before a -stop, sized in
ATR. This answers the make-or-break question: do these signals have directional
edge at all?

Important honesty note: this tests the SIGNAL on the underlying. It deliberately
does NOT model option premium, theta, or the bid/ask spread — so a strategy that
looks positive here can still lose as an actual option trade once those costs
bite. A NEGATIVE expectancy here, though, is decisive: the signal has no edge to
begin with, so the option trade cannot rescue it.
"""

from __future__ import annotations

import time

from . import indicators as ind
from .base import Context
from .delta_client import client
from .zing_strategies import STRATEGY_CLASSES

_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _bar_seconds(resolution: str) -> int:
    return int(resolution[:-1]) * _UNIT.get(resolution[-1], 60)


def fetch_history(symbol: str, resolution: str, days: float) -> list[dict]:
    """Page backwards through /v2/history/candles to accumulate `days` of bars."""
    secs = _bar_seconds(resolution)
    end = int(time.time())
    start_target = end - int(days * 86400)
    out: dict[int, dict] = {}
    cursor = end
    # Delta returns up to ~2000 bars/call; page until we cover the window.
    for _ in range(20):
        win_start = max(start_target, cursor - secs * 2000)
        rows = client.candles(symbol, resolution, win_start, cursor)
        if not rows:
            break
        for r in rows:
            if r.get("time") is not None:
                out[int(r["time"])] = r
        oldest = min(r["time"] for r in rows)
        if oldest <= start_target or win_start <= start_target:
            break
        cursor = oldest - secs
    candles = [out[t] for t in sorted(out)]
    return candles


def backtest_strategy(strat, candles: list[dict], max_hold: int = 30,
                      cost_pct: float = 0.0004) -> dict:
    """Replay one strategy over candles; return aggregate directional stats.

    Trades are sized in ATR: stop = 1 ATR, target = rr x ATR (rr from the
    strategy's own reward:risk). Results are expressed in R multiples.
    """
    n = len(candles)
    highs = [float(c["high"]) for c in candles]
    lows = [float(c["low"]) for c in candles]
    opens = [float(c["open"]) for c in candles]
    closes = ind.closes(candles)
    atr = ind.atr(candles, 14)

    trades: list[dict] = []
    warmup = max(strat.lookback // 2, 55)
    i = warmup
    while i < n - 1:
        a = atr[i]
        if not a or a <= 0:
            i += 1
            continue
        # Bounded window (like the live engine's recent_candles) — keeps the
        # replay O(n * lookback) instead of O(n^2). The strategies only read the
        # last few bars, and indicators converge well within `lookback`.
        window = candles[max(0, i - strat.lookback + 1): i + 1]
        try:
            sigs = strat.evaluate(Context(underlying=window, spot=closes[i]))
        except Exception:  # noqa: BLE001 — a bad bar shouldn't abort the run
            sigs = []
        if not sigs:
            i += 1
            continue

        sig = sigs[0]
        long = sig.direction == "CE"
        # NO LOOK-AHEAD. The signal is only knowable once bar i has CLOSED, so
        # the earliest achievable fill is bar i+1's open — which is what the live
        # engine actually gets. Entering at closes[i] granted the backtest a
        # price that no longer existed by the time an order could be sent, and
        # flattered every breakout strategy in particular (audit #10).
        entry_i = i + 1
        if entry_i >= n:
            break
        entry = opens[entry_i]
        stop_d = a
        tgt_d = a * sig.rr

        result_r = None
        exit_i = min(n - 1, entry_i + max_hold)
        # Start the scan AT the entry bar: a gap straight through the stop on the
        # fill bar is a real outcome and must be counted.
        for j in range(entry_i, exit_i + 1):
            if long:
                if lows[j] <= entry - stop_d:
                    result_r = -1.0
                    exit_i = j
                    break
                if highs[j] >= entry + tgt_d:
                    result_r = sig.rr
                    exit_i = j
                    break
            else:
                if highs[j] >= entry + stop_d:
                    result_r = -1.0
                    exit_i = j
                    break
                if lows[j] <= entry - tgt_d:
                    result_r = sig.rr
                    exit_i = j
                    break
        if result_r is None:  # time exit — mark to market at close
            last = closes[exit_i]
            move = (last - entry) if long else (entry - last)
            result_r = move / stop_d
        result_r -= cost_pct * entry / stop_d  # round-trip cost in R

        trades.append({"dir": sig.direction, "r": result_r, "bar": i,
                       "held": exit_i - entry_i})
        i = exit_i + 1  # flat again only after the trade closes

    rs = [t["r"] for t in trades]
    wins = [r for r in rs if r > 0]
    losses = [r for r in rs if r < 0]
    total = len(rs)
    gross_w = sum(wins)
    gross_l = -sum(losses)
    return {
        "strategy": strat.slug,
        "title": strat.title,
        "timeframe": strat.timeframe,
        "trades": total,
        "wins": len(wins),
        "losses": len(losses),
        "win_rate": round(len(wins) / total * 100, 1) if total else None,
        "expectancy_r": round(sum(rs) / total, 4) if total else None,
        "total_r": round(sum(rs), 3),
        "profit_factor": round(gross_w / gross_l, 2) if gross_l else None,
        "avg_win_r": round(gross_w / len(wins), 3) if wins else None,
        "avg_loss_r": round(-gross_l / len(losses), 3) if losses else None,
        "avg_hold_bars": round(sum(t["held"] for t in trades) / total, 1) if total else None,
    }


def run(days: float = 7.0, symbol: str = "BTCUSD", max_hold: int = 30) -> dict:
    """Backtest every underlying-based strategy over `days` of history."""
    results = []
    skipped = []
    # Group by timeframe so each candle series is fetched once.
    tf_cache: dict[str, list] = {}
    for cls in STRATEGY_CLASSES:
        s = cls()
        if s.basis != "underlying":
            skipped.append({"strategy": s.slug, "reason": "premium-based (no historical premium series)"})
            continue
        if not getattr(s, "backtestable", True):
            skipped.append({"strategy": s.slug,
                            "reason": "queries a live service — replaying it would use "
                                      "today's state at a historical bar (lookahead)"})
            continue
        if s.timeframe not in tf_cache:
            tf_cache[s.timeframe] = fetch_history(symbol, s.timeframe, days)
        candles = tf_cache[s.timeframe]
        if len(candles) < 80:
            skipped.append({"strategy": s.slug, "reason": f"insufficient history ({len(candles)} bars)"})
            continue
        results.append(backtest_strategy(s, candles, max_hold=max_hold))

    results.sort(key=lambda r: (r["expectancy_r"] if r["expectancy_r"] is not None else -99), reverse=True)
    bars_by_tf = {tf: len(c) for tf, c in tf_cache.items()}
    return {
        "symbol": symbol,
        "days": days,
        "max_hold": max_hold,
        "generated_at": int(time.time()),
        "bars_by_timeframe": bars_by_tf,
        "results": results,
        "skipped": skipped,
        "note": "Directional edge on the underlying only — excludes option spread/theta. "
                "Positive expectancy is necessary but NOT sufficient for the option trade to profit.",
    }

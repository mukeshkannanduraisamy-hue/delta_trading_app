"""Walk-forward signal research harness.

Screens a grid of signal families across timeframes, splits each history into a
train half and a test half, and reports only combos that stay positive in BOTH
(the honest bar that defeats in-sample overfitting). Reuses the directional
R-methodology from backtest.py.

Deliberately tests HIGHER timeframes (5m/15m/1h), not 1m — the earlier research
already proved 1m directional signals on BTC have no edge, so re-testing them
would just re-confirm noise. Positive test-half expectancy across a family is
the necessary (not sufficient) signal that something real might be there.
"""

from __future__ import annotations

import time

from . import indicators as ind
from .backtest import backtest_strategy, fetch_history
from .base import Signal, Strategy


# --------------------------------------------------------------------------- #
# Parameterized signal families (fire on the event bar, one signal at a time)
# --------------------------------------------------------------------------- #
class TrendEMA(Strategy):
    basis = "underlying"
    def evaluate(self, ctx):
        f, s = self.params["fast"], self.params["slow"]
        c = ind.closes(ctx.underlying)
        ef, es = ind.ema(c, f), ind.ema(c, s)
        if None in (ef[-1], es[-1], ef[-2], es[-2]):
            return []
        rr = self.params.get("rr", 1.5)
        if ef[-2] <= es[-2] and ef[-1] > es[-1]:
            return [Signal(self.slug, "CE", "ema cross up", 0.05, rr)]
        if ef[-2] >= es[-2] and ef[-1] < es[-1]:
            return [Signal(self.slug, "PE", "ema cross down", 0.05, rr)]
        return []


class Donchian(Strategy):
    basis = "underlying"
    def evaluate(self, ctx):
        p = self.params["period"]
        u = ctx.underlying
        if len(u) < p + 2:
            return []
        highs = [float(x["high"]) for x in u]
        lows = [float(x["low"]) for x in u]
        close = float(u[-1]["close"])
        prev_hi = max(highs[-p - 1:-1])
        prev_lo = min(lows[-p - 1:-1])
        rr = self.params.get("rr", 1.5)
        if close > prev_hi:
            return [Signal(self.slug, "CE", "donchian breakout up", 0.05, rr)]
        if close < prev_lo:
            return [Signal(self.slug, "PE", "donchian breakout down", 0.05, rr)]
        return []


class BollingerFade(Strategy):
    basis = "underlying"
    def evaluate(self, ctx):
        p, k = self.params["period"], self.params["k"]
        c = ind.closes(ctx.underlying)
        mid, up, lo = ind.bollinger(c, p, k)
        if None in (up[-1], lo[-1], up[-2], lo[-2]):
            return []
        rr = self.params.get("rr", 1.0)
        if c[-1] > up[-1] and c[-2] <= up[-2]:
            return [Signal(self.slug, "PE", "fade upper band", 0.05, rr)]
        if c[-1] < lo[-1] and c[-2] >= lo[-2]:
            return [Signal(self.slug, "CE", "fade lower band", 0.05, rr)]
        return []


class ZScoreFade(Strategy):
    basis = "underlying"
    def evaluate(self, ctx):
        p, z = self.params["period"], self.params["z"]
        c = ind.closes(ctx.underlying)
        if len(c) < p + 2:
            return []
        def zscore(idx):
            w = c[idx - p + 1: idx + 1]
            m = sum(w) / p
            sd = (sum((x - m) ** 2 for x in w) / p) ** 0.5
            return (c[idx] - m) / sd if sd else 0.0
        cur, prev = zscore(len(c) - 1), zscore(len(c) - 2)
        rr = self.params.get("rr", 1.0)
        if cur > z and prev <= z:
            return [Signal(self.slug, "PE", "zscore high fade", 0.05, rr)]
        if cur < -z and prev >= -z:
            return [Signal(self.slug, "CE", "zscore low fade", 0.05, rr)]
        return []


class MASlope(Strategy):
    basis = "underlying"
    def evaluate(self, ctx):
        p = self.params["period"]
        c = ind.closes(ctx.underlying)
        m = ind.sma(c, p)
        if None in (m[-1], m[-2], m[-3]):
            return []
        rr = self.params.get("rr", 1.5)
        up_now, up_prev = m[-1] > m[-2], m[-2] > m[-3]
        if up_now and not up_prev:
            return [Signal(self.slug, "CE", "ma slope turns up", 0.05, rr)]
        if not up_now and up_prev:
            return [Signal(self.slug, "PE", "ma slope turns down", 0.05, rr)]
        return []


def _grid() -> list[Strategy]:
    combos: list[Strategy] = []
    for f, s in [(9, 21), (20, 50), (10, 30)]:
        combos.append(_mk(TrendEMA, f"ema_{f}_{s}", f"EMA {f}/{s}", fast=f, slow=s, lookback=s + 60))
    for p in [20, 40, 55]:
        combos.append(_mk(Donchian, f"donchian_{p}", f"Donchian {p}", period=p, lookback=p + 60))
    for p, k in [(20, 2.0), (20, 2.5)]:
        combos.append(_mk(BollingerFade, f"boll_{p}_{k}", f"BollFade {p}/{k}", period=p, k=k, lookback=p + 60))
    for p, z in [(20, 2.0), (50, 2.0)]:
        combos.append(_mk(ZScoreFade, f"zscore_{p}_{z}", f"ZFade {p}/{z}", period=p, z=z, lookback=p + 60))
    for p in [20, 50]:
        combos.append(_mk(MASlope, f"maslope_{p}", f"MASlope {p}", period=p, lookback=p + 60))
    return combos


def _mk(cls, slug, title, **params):
    obj = cls(**params)
    obj.slug = slug
    obj.title = title
    obj.lookback = params.get("lookback", 120)
    return obj


# --------------------------------------------------------------------------- #
def run(days: float = 60.0, timeframes=("5m", "15m", "1h"), max_hold: int = 20,
        min_trades: int = 20) -> dict:
    """Walk-forward screen. Each combo is scored on a train half and a test half."""
    rows = []
    bars_by_tf = {}
    for tf in timeframes:
        candles = fetch_history("BTCUSD", tf, days)
        bars_by_tf[tf] = len(candles)
        if len(candles) < 200:
            continue
        split = int(len(candles) * 0.6)
        train, test = candles[:split], candles[split:]
        for strat in _grid():
            strat.timeframe = tf
            tr = backtest_strategy(strat, train, max_hold=max_hold)
            te = backtest_strategy(strat, test, max_hold=max_hold)
            if tr["trades"] < min_trades or te["trades"] < min_trades:
                continue
            survives = (tr["expectancy_r"] or -1) > 0 and (te["expectancy_r"] or -1) > 0
            rows.append({
                "family": strat.title, "timeframe": tf,
                "train_trades": tr["trades"], "train_exp": tr["expectancy_r"],
                "test_trades": te["trades"], "test_exp": te["expectancy_r"],
                "test_pf": te["profit_factor"], "survives": survives,
            })
    rows.sort(key=lambda r: (r["survives"], r["test_exp"] if r["test_exp"] is not None else -99), reverse=True)
    survivors = [r for r in rows if r["survives"]]
    return {
        "days": days, "timeframes": list(timeframes), "max_hold": max_hold,
        "generated_at": int(time.time()), "bars_by_timeframe": bars_by_tf,
        "combos_tested": len(rows), "survivors": len(survivors), "results": rows,
        "note": "A combo 'survives' only if expectancy is positive in BOTH the train (first 60%) "
                "and test (last 40%) halves. Directional edge on the underlying, excludes option "
                "spread/theta. Survivors are leads to investigate, not proven edges.",
    }

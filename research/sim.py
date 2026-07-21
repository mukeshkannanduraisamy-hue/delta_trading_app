"""Trade simulator + metrics, implementing Section 2.2-2.5 exactly.

FILL RULES (no look-ahead)
  * signal at bar[i] close  ->  entry at bar[i+1] open
  * entry slippage is adverse: long pays open*(1+s), short receives open*(1-s)
  * TP/SL checked against bar OHLC starting AT the entry bar (a gap through the
    stop on the fill bar is a real outcome and must count)
  * if both TP and SL are touched in the same bar -> SL (conservative)
  * time exit fills at the open of the bar after max_hold

COSTS
  * slippage      : SLIP_PCT of fill price, both sides, always adverse
  * taker fee     : FEE_PCT of fill price + GST_RATE on the fee, both sides
  * both are charged on the instrument actually traded (underlying notional for
    the directional test, option premium for the premium test)

R DEFINITION
  R = (net PnL per unit) / stop_distance
  so -1R is a clean stop-out and costs are expressed in the same units, which is
  what makes "cost in R" directly comparable to expectancy.
"""

from __future__ import annotations

import numpy as np

SLIP_PCT = 0.0005          # 0.05% per fill
FEE_PCT = 0.0005           # 0.05% taker per side
GST_RATE = 0.18


def _round_trip_cost(entry: float, exit_: float) -> float:
    """Total per-unit cost of one trade: slippage is already baked into the fill
    prices, so this is the explicit fee leg only."""
    return (entry * FEE_PCT * (1 + GST_RATE)) + (exit_ * FEE_PCT * (1 + GST_RATE))


def simulate(d: dict, signals: np.ndarray, *,
             stop_atr: float = 1.0, rr: float = 1.5,
             atr_period: int = 14, max_hold: int = 30,
             long_only: bool = False,
             atr_override: np.ndarray | None = None) -> dict:
    """Run every signal through the fill model. Returns trades + equity curve.

    One position at a time: after a trade closes the scanner resumes at the exit
    bar, matching the live engine's one-open-position-per-(strategy,direction)
    constraint closely enough for expectancy to be comparable.
    """
    from .vec import atr as _atr

    o, h, l, c, t = d["open"], d["high"], d["low"], d["close"], d["time"]
    n = len(c)
    a = atr_override if atr_override is not None else _atr(h, l, c, atr_period)

    idx = np.flatnonzero(signals != 0)
    trades = []
    i_ptr = 0
    blocked_until = -1

    for i in idx:
        if i <= blocked_until or i + 1 >= n:
            continue
        av = a[i]
        if not np.isfinite(av) or av <= 0:
            continue
        direction = int(signals[i])
        if long_only and direction != 1:
            continue
        long = direction == 1

        raw_entry = o[i + 1]
        entry = raw_entry * (1 + SLIP_PCT) if long else raw_entry * (1 - SLIP_PCT)
        stop_d = stop_atr * av
        tgt_d = stop_d * rr
        sl_px = entry - stop_d if long else entry + stop_d
        tp_px = entry + tgt_d if long else entry - tgt_d

        last = min(n - 1, i + 1 + max_hold)
        exit_px = None
        exit_i = last
        why = "time"
        mfe = 0.0          # max favourable excursion, in price
        mae = 0.0          # max adverse excursion, in price

        for j in range(i + 1, last + 1):
            if long:
                mfe = max(mfe, h[j] - entry)
                mae = max(mae, entry - l[j])
                hit_sl = l[j] <= sl_px
                hit_tp = h[j] >= tp_px
            else:
                mfe = max(mfe, entry - l[j])
                mae = max(mae, h[j] - entry)
                hit_sl = h[j] >= sl_px
                hit_tp = l[j] <= tp_px
            if hit_sl:                      # conservative: SL wins a tie
                exit_px, exit_i, why = sl_px, j, "stop"
                break
            if hit_tp:
                exit_px, exit_i, why = tp_px, j, "target"
                break

        if exit_px is None:                 # time exit at the NEXT bar's open
            k = min(n - 1, last + 1)
            raw = o[k]
            exit_px = raw * (1 - SLIP_PCT) if long else raw * (1 + SLIP_PCT)
            exit_i = k

        gross = (exit_px - entry) if long else (entry - exit_px)
        cost = _round_trip_cost(entry, exit_px)
        net = gross - cost
        trades.append({
            "i": int(i), "entry_i": int(i + 1), "exit_i": int(exit_i),
            "t": int(t[i]), "dir": "CE" if long else "PE",
            "entry": entry, "exit": exit_px, "stop_d": stop_d,
            "gross_r": gross / stop_d, "cost_r": cost / stop_d,
            "r": net / stop_d, "why": why,
            "mfe_r": mfe / stop_d, "mae_r": mae / stop_d,
            "held": int(exit_i - i - 1),
        })
        blocked_until = exit_i

    return {"trades": trades, "atr": a}


def metrics(trades: list[dict], span_days: float) -> dict:
    """Section 2.5 metric set."""
    n = len(trades)
    if n == 0:
        return {"total_trades": 0, "win_trades": 0, "loss_trades": 0,
                "win_rate": None, "loss_rate": None, "avg_win_r": None,
                "avg_loss_r": None, "avg_r_per_trade": None,
                "max_consecutive_loss": 0, "max_drawdown_r": 0.0,
                "max_drawdown_pct": 0.0, "sharpe_ratio": None,
                "profit_factor": None, "signal_frequency": 0.0,
                "avg_hold_bars": None, "total_r": 0.0, "avg_cost_r": None}

    r = np.array([t["r"] for t in trades])
    wins = r[r > 0]
    losses = r[r < 0]

    # Equity curve in R, drawdown as a % of a 100R notional account so the
    # number is comparable across strategies with different trade counts.
    eq = np.concatenate([[0.0], np.cumsum(r)])
    peak = np.maximum.accumulate(eq)
    dd = peak - eq
    max_dd_r = float(dd.max())

    streak = best_streak = 0
    for x in r:
        if x < 0:
            streak += 1
            best_streak = max(best_streak, streak)
        else:
            streak = 0

    sd = float(r.std(ddof=1)) if n > 1 else 0.0
    trades_per_day = n / span_days if span_days > 0 else 0.0
    # Annualize by trades/day * 252 trading-equivalent days.
    sharpe = (float(r.mean()) / sd * np.sqrt(max(trades_per_day, 1e-9) * 252)) if sd > 0 else None

    return {
        "total_trades": n,
        "win_trades": int(len(wins)),
        "loss_trades": int(len(losses)),
        "win_rate": round(100.0 * len(wins) / n, 1),
        "loss_rate": round(100.0 * len(losses) / n, 1),
        "avg_win_r": round(float(wins.mean()), 4) if len(wins) else None,
        "avg_loss_r": round(float(losses.mean()), 4) if len(losses) else None,
        "avg_r_per_trade": round(float(r.mean()), 4),
        "max_consecutive_loss": int(best_streak),
        "max_drawdown_r": round(max_dd_r, 2),
        "max_drawdown_pct": round(max_dd_r, 2),      # 1R == 1% of a 100R account
        "sharpe_ratio": round(sharpe, 2) if sharpe is not None else None,
        "profit_factor": (round(float(wins.sum() / -losses.sum()), 3)
                          if len(losses) and losses.sum() != 0 else None),
        "signal_frequency": round(trades_per_day, 2),
        "avg_hold_bars": round(float(np.mean([t["held"] for t in trades])), 1),
        "total_r": round(float(r.sum()), 2),
        "avg_cost_r": round(float(np.mean([t["cost_r"] for t in trades])), 4),
    }


def status(m: dict) -> str:
    if not m["total_trades"]:
        return "NO-TRADES"
    ar = m["avg_r_per_trade"]
    if ar > 0 and (m["win_rate"] or 0) > 50:
        return "PASS"
    if ar > -0.3:
        return "MARGINAL"
    return "FAIL"


def flagged(m: dict) -> bool:
    """Section 3 Step 5 deep-analysis trigger."""
    if not m["total_trades"]:
        return False
    return ((m["loss_rate"] or 0) > 50
            or (m["avg_r_per_trade"] or 0) < -0.3
            or (m["max_drawdown_pct"] or 0) > 20
            or (m["profit_factor"] is not None and m["profit_factor"] < 0.8))

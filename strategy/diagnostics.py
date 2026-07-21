"""Loss-attribution diagnostics: WHY a strategy loses, not just that it does.

The decisive question is whether the ENTRY has predictive power at all, or
whether the entries are fine and the EXITS throw the edge away. They look
identical in a P&L summary and demand opposite fixes, so this module separates
them with MFE/MAE analysis:

  MFE (Maximum Favourable Excursion) — how far a trade went IN our favour at any
      point before it closed.
  MAE (Maximum Adverse Excursion)    — how far it went against us.

Read it like this:
  * Many trades reach +1R/+2R MFE but still close negative  -> EXIT problem.
    The signal predicts; we give the profit back. Fix targets/trailing.
  * Few trades ever reach +1R MFE                            -> ENTRY problem.
    The signal has no predictive power. No exit tuning can rescue it.

Also attributes losses by market regime (trending vs ranging), flags immediate
false breakouts, and measures overtrading.
"""

from __future__ import annotations

import datetime as dt
from collections import defaultdict

import numpy as np

from .optimizer import FEE, SLIP, Params, adx, atr, ema, supertrend_dir


def collect_trades(d: dict, p: Params) -> list[dict]:
    """Replay the strategy, recording rich per-trade detail (incl. MFE/MAE)."""
    o, h, l, c, v, t = d["open"], d["high"], d["low"], d["close"], d["volume"], d["time"]
    n = len(c)
    if n < 300:
        return []

    ef, es = ema(c, p.ema_fast), ema(c, p.ema_slow)
    a = atr(h, l, c, p.atr_len)
    adx_v = adx(h, l, c, 14)
    st = supertrend_dir(h, l, c, p.st_len, p.st_mult) if p.use_supertrend else None

    trades: list[dict] = []
    i = max(p.ema_slow, p.atr_len, 50) + 2
    cooldown_until = 0

    while i < n - 1:
        if i < cooldown_until or a[i] <= 0:
            i += 1
            continue
        long_sig = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        short_sig = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        if not (long_sig or short_sig):
            i += 1
            continue
        if st is not None:
            if long_sig and st[i] != 1:
                i += 1; continue
            if short_sig and st[i] != -1:
                i += 1; continue

        entry_i = i + 1
        if entry_i >= n:
            break
        is_long = bool(long_sig)
        entry = o[entry_i] * (1 + SLIP if is_long else 1 - SLIP)
        stop_d = p.atr_mult_sl * a[i]
        if stop_d <= 0:
            i += 1; continue
        stop = entry - stop_d if is_long else entry + stop_d

        mfe = mae = 0.0
        outcome = None
        reason = "time"
        exit_i = min(n - 1, entry_i + p.max_hold)

        for j in range(entry_i, exit_i + 1):
            fav = (h[j] - entry) if is_long else (entry - l[j])
            adv = (entry - l[j]) if is_long else (h[j] - entry)
            mfe = max(mfe, fav / stop_d)
            mae = max(mae, adv / stop_d)
            if adv >= stop_d:                      # stop first (conservative)
                outcome, reason, exit_i = -1.0, "stop", j
                break
            if fav >= p.rr * stop_d:
                outcome, reason, exit_i = p.rr, "target", j
                break
        if outcome is None:
            px = c[exit_i]
            mv = (px - entry) if is_long else (entry - px)
            outcome = mv / stop_d

        cost_r = (2 * (FEE + SLIP) * entry) / stop_d
        net = outcome - cost_r
        trades.append({
            "t": float(t[entry_i]), "dir": "long" if is_long else "short",
            "r": net, "reason": reason, "bars": exit_i - entry_i,
            "mfe": mfe, "mae": mae, "adx": float(adx_v[i]),
            "atr_pct": float(a[i] / c[i] * 100),
        })
        cooldown_until = exit_i + 1 + p.cooldown
        i = exit_i + 1
    return trades


# --------------------------------------------------------------------------- #
def analyse(trades: list[dict], rr: float) -> dict:
    if not trades:
        return {"trades": 0}
    r = np.array([x["r"] for x in trades])
    mfe = np.array([x["mfe"] for x in trades])
    mae = np.array([x["mae"] for x in trades])
    adx_v = np.array([x["adx"] for x in trades])

    # --- ENTRY vs EXIT attribution -----------------------------------------
    # What fraction of trades EVER traded far enough in our favour?
    reach = {f"reach_{k}R": round(float((mfe >= k).mean() * 100), 1)
             for k in (0.5, 1.0, 1.5, 2.0, 3.0)}
    # Of the losers, how many had been meaningfully green first?
    losers = [x for x in trades if x["r"] <= 0]
    gave_back = sum(1 for x in losers if x["mfe"] >= 1.0)
    gave_back_pct = round(gave_back / len(losers) * 100, 1) if losers else 0.0

    # A random walk with a stop at -1R and target at +rr R reaches +1R MFE
    # roughly this often; comparing against it shows whether the ENTRY adds
    # anything over noise.
    random_reach_1r = 100 / (1 + 1.0)  # ~50% for a symmetric ±1R barrier
    edge_vs_random = round(reach["reach_1.0R"] - random_reach_1r, 1)

    # --- exit reason mix ----------------------------------------------------
    by_reason: dict[str, list] = defaultdict(list)
    for x in trades:
        by_reason[x["reason"]].append(x["r"])
    reasons = {k: {"n": len(v), "pct": round(len(v) / len(trades) * 100, 1),
                   "avg_r": round(float(np.mean(v)), 4)} for k, v in by_reason.items()}

    # --- regime attribution -------------------------------------------------
    def bucket(mask, label):
        if mask.sum() == 0:
            return None
        rr_ = r[mask]
        return {"label": label, "n": int(mask.sum()),
                "avg_r": round(float(rr_.mean()), 4),
                "win_rate": round(float((rr_ > 0).mean() * 100), 1)}

    regimes = [b for b in (
        bucket(adx_v < 20, "ranging (ADX<20)"),
        bucket((adx_v >= 20) & (adx_v < 25), "transitional (20-25)"),
        bucket(adx_v >= 25, "trending (ADX>=25)"),
    ) if b]

    # --- false breakouts: stopped out almost immediately ---------------------
    quick = [x for x in trades if x["reason"] == "stop" and x["bars"] <= 3]
    false_breakout_pct = round(len(quick) / len(trades) * 100, 1)

    # --- overtrading: does expectancy decay with frequency? ------------------
    gaps = np.diff(np.array([x["t"] for x in trades]))
    median_gap_h = round(float(np.median(gaps)) / 3600, 2) if len(gaps) else None

    # --- monthly returns ----------------------------------------------------
    monthly: dict[str, float] = defaultdict(float)
    mcount: dict[str, int] = defaultdict(int)
    for x in trades:
        key = dt.datetime.fromtimestamp(x["t"], dt.UTC).strftime("%Y-%m")
        monthly[key] += x["r"]
        mcount[key] += 1
    months = sorted(monthly)
    mvals = np.array([monthly[m] for m in months])
    pos_months = int((mvals > 0).sum())

    return {
        "trades": len(trades),
        "avg_r": round(float(r.mean()), 4),
        "win_rate": round(float((r > 0).mean() * 100), 2),
        "mfe_mean": round(float(mfe.mean()), 3),
        "mae_mean": round(float(mae.mean()), 3),
        "mfe_median": round(float(np.median(mfe)), 3),
        **reach,
        "edge_vs_random_pp": edge_vs_random,
        "losers_that_were_1R_green": gave_back_pct,
        "exit_reasons": reasons,
        "regimes": regimes,
        "false_breakout_pct": false_breakout_pct,
        "median_gap_hours": median_gap_h,
        "months": len(months),
        "positive_months": pos_months,
        "positive_month_pct": round(pos_months / len(months) * 100, 1) if months else None,
        "best_month_r": round(float(mvals.max()), 2) if len(mvals) else None,
        "worst_month_r": round(float(mvals.min()), 2) if len(mvals) else None,
        "monthly_series": {m: round(monthly[m], 3) for m in months},
    }


def verdict(a: dict) -> str:
    """Plain-language attribution: entry problem or exit problem?"""
    if not a.get("trades"):
        return "no trades"
    reach1 = a.get("reach_1.0R", 0)
    gave = a.get("losers_that_were_1R_green", 0)
    if reach1 < 45:
        return ("ENTRY PROBLEM — only %.1f%% of trades ever reached +1R in our favour, "
                "which is at or below what a coin flip against the same stop would give. "
                "The signal carries no predictive information; exit tuning cannot fix this."
                % reach1)
    if gave >= 45:
        return ("EXIT PROBLEM — %.1f%% of losing trades had already been +1R green. "
                "The entries predict, but the exits hand the profit back." % gave)
    return ("MIXED — entries show some directional information (%.1f%% reach +1R) but not "
            "enough to clear costs." % reach1)

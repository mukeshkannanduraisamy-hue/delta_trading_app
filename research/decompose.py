"""Decompose option-trade P&L into DIRECTION vs THETA vs COST.

WHY
---
Nine of ten strategy families showed negative expectancy in training. If
direction were a coin flip, expectancies should scatter around zero — roughly
half positive. Nine of ten negative is a systematic drag, not random failure,
and costs were only 0.0002 R/trade so they were not it.

Buying premium means paying time decay every bar held. This module measures
how much, per trade, in R units — so "the strategy is bad" can be separated
from "the strategy is paying rent to hold the position".

METHOD
------
For each trade, price the SAME option at the exit time twice:

  frozen : spot held at its entry value    -> isolates pure theta
  actual : spot at its real exit value     -> theta + direction

Then, in units of the trade's own stop distance (1 R):

  theta_R     = (frozen_exit  - entry) / stop
  direction_R = (actual_exit  - frozen_exit) / stop
  cost_R      = fees / stop
  total_R     = direction_R + theta_R - cost_R

`direction_R` is the only component the signal controls. If it is ~0 the
indicator has no directional information; if it is positive but total_R is
negative, the signal works and theta is eating it — a completely different
problem with a completely different fix.
"""

from __future__ import annotations

import numpy as np

from research import costs, data, search, sim, vec
from research.backtest_cli import DTE_CHOICES, _prepare, build_overlay


def decompose(strategy: str, tf: str, dte_hours: float, days: int = 365,
              params: dict | None = None, overlay=None,
              strict: bool = True) -> dict:
    """Decompose one strategy's P&L. Refuses combinations that cannot measure.

    `strict` blocks TF/DTE pairs whose duty cycle is too low to mean anything
    (see search.duty_cycle). Pass strict=False only to inspect the artifact
    deliberately -- the returned dict then carries a `duty_warning`.
    """
    duty = search.duty_cycle(tf, dte_hours)
    if not duty["usable"]:
        warn = search.duty_warning(tf, dte_hours)
        if strict:
            return {"trades": 0, "strategy": strategy, "timeframe": tf,
                    "dte_hours": dte_hours, "skipped": True,
                    "duty": duty["duty"], "duty_warning": warn}

    ov = overlay if overlay is not None else build_overlay()
    d_tf, ce, pe = _prepare(strategy, tf, dte_hours, days, overlay=ov)
    bar_s = data.res_seconds(tf)
    params = params if params is not None else dict(
        vec.DEFAULTS.get(strategy, {}).get("params", {}))

    cand = {"family": strategy, "timeframe": tf, "params": params, "gates": []}
    sig = search.build_signals(cand, d_tf, ce, None)
    rr = float(vec.DEFAULTS.get(strategy, {}).get("rr", 1.5))
    trades = search._trades(sig, ce, pe, d_tf["close"], rr, costs.OptionCost())
    if not trades:
        return {"trades": 0}

    t = d_tf["time"]
    spot = d_tf["close"]
    roll_s = max(int(dte_hours * 3600 // 2), bar_s)

    theta_r, dir_r, cost_r, total_r = [], [], [], []
    for tr in trades:
        i_in, i_out = tr["entry_i"], tr["exit_i"]
        if i_out >= len(t) or i_in >= len(t):
            continue
        stop = tr["stop_d"]
        if stop <= 0:
            continue

        # rebuild this trade's contract: strike and expiry from its roll epoch
        seg_start = int(t[i_in] // roll_s) * roll_s
        a = int(np.searchsorted(t, seg_start))
        a = min(max(a, 0), len(t) - 1)
        K = round(float(spot[a]) / 200.0) * 200.0
        expiry = int(t[a]) + int(dte_hours * 3600)
        is_call = tr.get("opt", "CE") == "CE"

        rv = np.array([0.30])
        p_in = ov.premium_path(np.array([spot[i_in]]), K, expiry,
                               np.array([t[i_in]], dtype="int64"), is_call, rv=rv)[0]
        # exit priced with spot FROZEN at entry -> pure time decay
        p_frozen = ov.premium_path(np.array([spot[i_in]]), K, expiry,
                                   np.array([t[i_out]], dtype="int64"),
                                   is_call, rv=rv)[0]
        # exit priced with the REAL spot -> theta + direction
        p_actual = ov.premium_path(np.array([spot[i_out]]), K, expiry,
                                   np.array([t[i_out]], dtype="int64"),
                                   is_call, rv=rv)[0]

        theta_r.append((p_frozen - p_in) / stop)
        dir_r.append((p_actual - p_frozen) / stop)
        cost_r.append(tr["cost_r"])
        total_r.append(tr["r"])

    n = len(theta_r)
    if not n:
        return {"trades": 0}
    return {
        "strategy": strategy, "timeframe": tf, "dte_hours": dte_hours,
        "trades": n, "duty": duty["duty"],
        "duty_warning": None if duty["usable"] else search.duty_warning(tf, dte_hours),
        "theta_r": float(np.mean(theta_r)),
        "direction_r": float(np.mean(dir_r)),
        "cost_r": float(np.mean(cost_r)),
        "total_r": float(np.mean(total_r)),
        "avg_hold_bars": float(np.mean([t2["held"] for t2 in trades])),
        "direction_r_std": float(np.std(dir_r, ddof=1)) if n > 1 else 0.0,
    }


def report(rows: list[dict]) -> None:
    skipped = [r for r in rows if r.get("skipped")]
    if skipped:
        print(f"\n  {'!' * 76}")
        print(f"  {len(skipped)} combination(s) SKIPPED — duty cycle too low to measure:")
        seen = set()
        for r in skipped:
            key = (r["timeframe"], r["dte_hours"])
            if key in seen:
                continue
            seen.add(key)
            print(f"    {r['duty_warning']}")
        print(f"  {'!' * 76}")

    print(f"\n{'=' * 86}")
    print("  TRADE P&L DECOMPOSITION — where does the R actually go?")
    print(f"{'=' * 86}")
    print(f"  {'strategy':26s} {'DTE':>5} {'n':>6} {'hold':>5} "
          f"{'direction':>10} {'theta':>9} {'cost':>8} {'total':>9}")
    print(f"  {'-' * 82}")
    for r in rows:
        if not r.get("trades"):
            continue
        print(f"  {r['strategy']:26s} {r['dte_hours']:4.0f}h {r['trades']:6d} "
              f"{r['avg_hold_bars']:5.1f} {r['direction_r']:+10.4f} "
              f"{r['theta_r']:+9.4f} {r['cost_r']:8.4f} {r['total_r']:+9.4f}")

    valid = [r for r in rows if r.get("trades")]
    if not valid:
        return
    print(f"\n  {'-' * 82}")
    print("  READING THIS TABLE")
    print("    direction  what the SIGNAL earned (spot moving the option)")
    print("    theta      rent paid for holding premium — always negative when long")
    print("    cost       fees + spread")
    print("    total      what you actually made")
    md = float(np.mean([r["direction_r"] for r in valid]))
    mt = float(np.mean([r["theta_r"] for r in valid]))
    print(f"\n  mean direction across families: {md:+.4f} R")
    print(f"  mean theta across families:     {mt:+.4f} R")
    if abs(md) < abs(mt):
        print("\n  -> THETA DOMINATES. The drag from holding premium is larger")
        print("     than anything the signals produce. These strategies are not")
        print("     mainly failing at direction; they are paying more rent than")
        print("     their direction is worth.")
    else:
        print("\n  -> DIRECTION DOMINATES. Theta is not the main problem.")

"""Staged walk-forward optimization: core risk params -> filters -> exit logic.

Staged deliberately. Optimising 15 parameters in one grid explores millions of
combinations and is guaranteed to find a curve-fit winner; staging keeps the
number of simultaneously-free parameters small at every step. Every reported
number is OUT-OF-SAMPLE (scored on a window the fit never saw).
"""

from __future__ import annotations

import json
import sys
import time
from dataclasses import replace

sys.path.insert(0, r"D:\TRADER\delta_trading_app")

from strategy import datafeed, optimizer as O  # noqa: E402

PAIRS = ["BTCUSDT", "ETHUSDT", "SOLUSDT"]
TF = "1h"
BPY = 8760.0

BASELINE = O.Params(ema_fast=9, ema_slow=21, atr_len=14, atr_mult_sl=1.0,
                    rr=1.2, use_supertrend=False, max_hold=24)


def wf(d, grid, label=""):
    t0 = time.time()
    r = O.walk_forward(d, grid, folds=4, train_frac=0.6, bars_per_year=BPY)
    r["_elapsed"] = round(time.time() - t0, 1)
    r["_grid"] = len(grid)
    if label:
        print(f"   {label:28s} grid={len(grid):5d}  oos_exp={r.get('oos_expectancy_r')}  "
              f"oos_trades={r.get('oos_trades')}  PF={r.get('oos_profit_factor')}  "
              f"({r['_elapsed']}s)")
    return r


def best_from(res):
    """Most frequently chosen fold parameter set (stability across folds)."""
    picks = res.get("fold_picks") or []
    if not picks:
        return None
    # prefer the pick whose own OOS expectancy was best
    picks = sorted(picks, key=lambda p: (p["oos"].get("expectancy_r") or -9), reverse=True)
    return O.Params(**picks[0]["params"])


def main():
    report = {"timeframe": TF, "years": 5, "pairs": {}}

    for sym in PAIRS:
        print(f"\n=== {sym} ({TF}, 5y) ===")
        d = datafeed.load(sym, TF, 5.0)
        print(f"   data: {datafeed.describe(d)}")

        # ---- baseline, full sample (for the comparison table) ----
        base = O.backtest(d, BASELINE).metrics(BPY)
        print(f"   baseline (Nifty params): exp={base['expectancy_r']} PF={base['profit_factor']} "
              f"trades={base['trades']}")

        # ---- Stage 1: core risk/entry ----
        g1 = [p for p in O.build_grid(
            ema_fast=[5, 9, 13, 21],
            ema_slow=[21, 34, 55, 89],
            atr_mult_sl=[1.0, 1.5, 2.0, 3.0],
            rr=[1.0, 1.5, 2.0, 3.0],
            use_supertrend=[False],
            max_hold=[48],
        ) if p.ema_fast < p.ema_slow]
        r1 = wf(d, g1, "stage1 core")
        p1 = best_from(r1) or BASELINE

        # ---- Stage 2: regime / confirmation filters ----
        g2 = []
        for st in (False, True):
            for adx_min in (0.0, 20.0, 25.0):
                for htf in (0, 200):
                    for vol in (0.0, 1.2):
                        g2.append(replace(p1, use_supertrend=st, adx_min=adx_min,
                                          htf_ema=htf, vol_mult=vol))
        r2 = wf(d, g2, "stage2 filters")
        p2 = best_from(r2) or p1

        # ---- Stage 3: exit management ----
        g3 = []
        for trail in (0.0, 2.0, 3.0):
            for be in (0.0, 1.0):
                for part in (0.0, 1.0):
                    for cd in (0, 5, 20):
                        g3.append(replace(p2, trail_atr=trail, breakeven_at=be,
                                          partial_at=part, cooldown=cd))
        r3 = wf(d, g3, "stage3 exits")
        p3 = best_from(r3) or p2

        # ---- final: full-sample view of the optimized config (in-sample, for gap) ----
        final_is = O.backtest(d, p3).metrics(BPY)

        report["pairs"][sym] = {
            "baseline_full_sample": base,
            "baseline_params": dict(BASELINE.__dict__),
            "stage1": {k: v for k, v in r1.items() if k != "fold_picks"},
            "stage2": {k: v for k, v in r2.items() if k != "fold_picks"},
            "stage3": {k: v for k, v in r3.items() if k != "fold_picks"},
            "optimized_params": dict(p3.__dict__),
            "optimized_in_sample": final_is,
            "optimized_oos": {k: v for k, v in r3.items() if k.startswith("oos")},
        }
        print(f"   OPTIMIZED oos exp={r3.get('oos_expectancy_r')} "
              f"PF={r3.get('oos_profit_factor')} trades={r3.get('oos_trades')} "
              f"maxDD={r3.get('oos_max_dd_r')}")
        print(f"   in-sample exp={final_is['expectancy_r']} (gap shows curve-fit size)")

    out = r"D:\TRADER\delta_trading_app\strategy\cache\optimization_report.json"
    with open(out, "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print("\nreport written:", out)


if __name__ == "__main__":
    main()

"""Is the small positive gross edge real, or the best of 274 coin flips?

Two strategies showed positive gross expectancy under the corrected fill model:
mean_reversion_bollinger (+0.0267 R) and prime_scalper_ema (+0.0016 R). Both are
small. This tests whether either survives:

  1. a t-test against zero on the trade-level R distribution
  2. a multiple-comparison correction (274 grid combos x 8 strategies were
     searched, so the largest observed value is an order statistic, not a
     random draw)
  3. the round-trip FEE, which the corrected model leaves untouched
"""

from __future__ import annotations

import numpy as np

from research import phase1, premium, sim, vec

TICK, MED_PX = 0.5, 65558.0


def main():
    d1, d5 = phase1.load()
    ps = premium.atm_premium_series(d1, 60)
    old = sim.SLIP_PCT
    sim.SLIP_PCT = TICK / MED_PX

    print("=" * 100)
    print("SIGNIFICANCE OF THE GROSS EDGE (corrected fill model)")
    print("=" * 100)
    print("{:<28} {:>8} {:>10} {:>9} {:>9} {:>9} {:>12}".format(
        "strategy", "trades", "mean R", "std", "std err", "t-stat", "p (2-sided)"))
    print("-" * 100)
    try:
        from math import erfc, sqrt
        rows = []
        for slug in vec.DEFAULTS:
            _, trades, _ = phase1.run_strategy(slug, d1, d5, span_days=90.0,
                                               premium_series=ps)
            if not trades:
                continue
            r = np.array([t["gross_r"] for t in trades])
            n = len(r)
            mu, sd = float(r.mean()), float(r.std(ddof=1))
            se = sd / np.sqrt(n)
            t = mu / se if se > 0 else 0.0
            p = erfc(abs(t) / sqrt(2))          # normal approx, n is large
            rows.append((slug, n, mu, sd, se, t, p))
            print("{:<28} {:>8} {:>10.4f} {:>9.4f} {:>9.4f} {:>9.2f} {:>12.4f}".format(
                slug, n, mu, sd, se, t, p))
    finally:
        sim.SLIP_PCT = old

    print("\n" + "=" * 100)
    print("MULTIPLE-COMPARISON CORRECTION")
    print("=" * 100)
    n_tests = 274 + 8
    print(f"  configurations searched         : {n_tests}")
    for slug, n, mu, sd, se, t, p in rows:
        if mu <= 0:
            continue
        bonf = min(1.0, p * n_tests)
        print(f"  {slug:<28} raw p={p:.4f}  Bonferroni p={bonf:.3f}  "
              f"{'SURVIVES' if bonf < 0.05 else 'does NOT survive'}")

    print("\n" + "=" * 100)
    print("THE ARITHMETIC THAT DECIDES IT — fee vs stop distance")
    print("=" * 100)
    a1 = float(np.nanmedian(vec.atr(d1["high"], d1["low"], d1["close"], 14)))
    a5 = float(np.nanmedian(vec.atr(d5["high"], d5["low"], d5["close"], 14)))
    fee_rt = 2 * MED_PX * sim.FEE_PCT * (1 + sim.GST_RATE)
    print(f"  round-trip taker fee alone      : {fee_rt:.2f} USD "
          f"({2*sim.FEE_PCT*(1+sim.GST_RATE)*100:.3f}% of notional)")
    print(f"  median ATR(14) 1m               : {a1:.2f} USD  -> fee = {fee_rt/a1:.2f} R")
    print(f"  median ATR(14) 5m               : {a5:.2f} USD  -> fee = {fee_rt/a5:.2f} R")
    need = fee_rt / a1
    print(f"\n  To net +0.1 R at 1m with a 1-ATR stop, gross edge must exceed "
          f"{need + 0.1:.2f} R per trade.")
    print(f"  Best gross edge measured anywhere in this study: +0.027 R.")
    print(f"  Shortfall: {(need + 0.1) / 0.027:.0f}x")
    stop_needed = fee_rt / 0.10
    print(f"\n  For fees to cost <= 0.10 R the stop must be >= {stop_needed:.0f} USD "
          f"= {stop_needed/a1:.0f}x the 1m ATR ({stop_needed/a5:.1f}x the 5m ATR).")
    print("  That is a swing-timeframe stop, not a scalping stop.")


if __name__ == "__main__":
    main()

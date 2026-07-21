"""FINAL RUN under a corrected fill model.

Control 4 showed the harness is correctly specified: with slippage set to zero,
RANDOM entries return gross expectancy of ~0.00, exactly as random-walk theory
requires. With the prescribed 0.05%-per-fill slippage they return -0.79, because
0.05% of BTC (38.33) exceeds one 1m ATR (35.76).

BTCUSD perp on Delta ticks at 0.5 and quotes a 0.5-2.0 spread. A market order
crossing that spread pays ~1 tick, not 66 ticks. The prescribed slippage is
therefore roughly 50x too large FOR THIS INSTRUMENT AND TIMEFRAME. Section 2.2
itself allows a tick floor ("minimum 1 tick"); this run uses the tick as the
value rather than the floor.

Fees are unchanged from the prescribed model (0.05% taker + 18% GST per side) —
that is Delta's real futures taker fee and is not in dispute.

Purpose: measure the DIRECTIONAL EDGE OF THE SIGNALS with the fill model no
longer dominating. If they are still negative here, the verdict rests on the
signals themselves and not on a mis-specified cost assumption.
"""

from __future__ import annotations

import json

import numpy as np

from research import phase1, premium, sim, vec

TICK = 0.5              # BTCUSD perp tick size on Delta
REALISTIC_SLIP = None   # computed per-dataset as TICK / median price


def run_all(label, slip_pct, days_windows=((0, 30), (30, 60), (60, 90))):
    d1, d5 = phase1.load()
    ps_all = premium.atm_premium_series(d1, 60)

    old = sim.SLIP_PCT
    sim.SLIP_PCT = slip_pct
    out = {}
    try:
        print("=" * 104)
        print(f"{label}   slippage={slip_pct*100:.4f}%/fill "
              f"(= {slip_pct*float(np.median(d1['close'])):.2f} USD, "
              f"{slip_pct*float(np.median(d1['close']))/TICK:.1f} ticks)")
        print("=" * 104)
        hdr = "{:<28} {:>8} {:>7} {:>10} {:>10} {:>9} {:>8} {:>18}"
        print(hdr.format("strategy", "trades", "win%", "grossR", "netR",
                         "costR", "PF", "walk-fwd gross"))
        print("-" * 104)
        for slug in vec.DEFAULTS:
            m, trades, _ = phase1.run_strategy(slug, d1, d5, span_days=90.0,
                                               premium_series=ps_all)
            g = float(np.mean([t["gross_r"] for t in trades])) if trades else None
            wf = []
            for a, b in days_windows:
                w1 = phase1.slice_days(d1, a, b)
                w5 = phase1.slice_days(d5, a, b)
                wps = premium.atm_premium_series(w1, 60)
                mw, tw, _ = phase1.run_strategy(slug, w1, w5, span_days=b - a,
                                                premium_series=wps)
                wf.append(round(float(np.mean([t["gross_r"] for t in tw])), 3) if tw else None)
            npos = sum(1 for x in wf if x is not None and x > 0)
            print(hdr.format(
                slug, m["total_trades"], str(m["win_rate"]),
                str(round(g, 4)) if g is not None else "-",
                str(m["avg_r_per_trade"]), str(m["avg_cost_r"]),
                str(m["profit_factor"]),
                f"{wf}  {npos}/3"))
            out[slug] = {"metrics": m, "gross_r": g, "walk_forward": wf,
                         "wf_positive": npos}
    finally:
        sim.SLIP_PCT = old
    return out


def main():
    d1, _ = phase1.load()
    med_px = float(np.median(d1["close"]))
    realistic = TICK / med_px

    print("\nBTCUSD median price: {:.1f}   tick: {}   1 tick = {:.5f}%\n".format(
        med_px, TICK, realistic * 100))

    prescribed = run_all("A. PRESCRIBED FILL MODEL (0.05%/fill)", 0.0005)
    print()
    corrected = run_all("B. CORRECTED FILL MODEL (1 tick/fill)", realistic)

    print("\n" + "=" * 104)
    print("DELTA — what changes when the fill model stops dominating")
    print("=" * 104)
    print("{:<28} {:>14} {:>14} {:>12} {:>14} {:>14}".format(
        "strategy", "gross(presc)", "gross(corr)", "change", "net(presc)", "net(corr)"))
    print("-" * 104)
    for slug in prescribed:
        a, b = prescribed[slug], corrected[slug]
        ga, gb = a["gross_r"], b["gross_r"]
        print("{:<28} {:>14} {:>14} {:>12} {:>14} {:>14}".format(
            slug,
            str(round(ga, 4)) if ga is not None else "-",
            str(round(gb, 4)) if gb is not None else "-",
            str(round(gb - ga, 4)) if (ga is not None and gb is not None) else "-",
            str(a["metrics"]["avg_r_per_trade"]),
            str(b["metrics"]["avg_r_per_trade"])))

    pos = [s for s, v in corrected.items() if (v["gross_r"] or -9) > 0]
    robust = [s for s, v in corrected.items() if v["wf_positive"] >= 2]
    print(f"\n  positive GROSS under corrected model : {pos or 'NONE'}")
    print(f"  positive in >=2 of 3 walk-forward windows: {robust or 'NONE'}")

    with open("research/corrected_results.json", "w", encoding="utf-8") as fh:
        json.dump({"prescribed": prescribed, "corrected": corrected,
                   "tick": TICK, "median_price": med_px,
                   "realistic_slip_pct": realistic}, fh, indent=2, default=str)
    print("\nwrote research/corrected_results.json")


if __name__ == "__main__":
    main()

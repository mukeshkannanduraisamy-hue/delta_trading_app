"""CONTROL EXPERIMENTS — is the 3% win rate the signal, or the harness?

A 1:1.2 bracket on a random walk should resolve to the stop about 55% of the
time, i.e. a ~45% win rate. Phase 1 reports 2-5%. A discrepancy that large is
not a bad signal; it means something in the fill model dominates the outcome.

Four controls:
  1. Scale check   — how large is the prescribed 0.05% entry slippage relative
                     to ATR(14) at each timeframe?
  2. Random entries— same trade count, random bars. Establishes the harness's
                     null. If real signals match this, they carry no edge.
  3. Direction shuffle — real signal bars, randomized direction. Isolates
                     whether the DIRECTION call carries information.
  4. Stop sweep    — vary stop_atr and slippage to find where the null returns
                     to its theoretical ~0 gross expectancy.
"""

from __future__ import annotations

import numpy as np

from research import phase1, sim, vec


def scale_check(d1, d5):
    print("=" * 96)
    print("CONTROL 1 — cost/slippage scale vs ATR at each timeframe")
    print("=" * 96)
    print("{:<6} {:>12} {:>14} {:>14} {:>16} {:>16}".format(
        "tf", "median ATR", "0.05% of px", "slip/ATR", "roundtrip cost", "cost/ATR (=R)"))
    for tf, d in (("1m", d1), ("5m", d5)):
        a = vec.atr(d["high"], d["low"], d["close"], 14)
        med_atr = float(np.nanmedian(a))
        px = float(np.median(d["close"]))
        slip = px * sim.SLIP_PCT
        rt = (2 * px * sim.SLIP_PCT) + (2 * px * sim.FEE_PCT * (1 + sim.GST_RATE))
        print("{:<6} {:>12.2f} {:>14.2f} {:>14.2f} {:>16.2f} {:>16.2f}".format(
            tf, med_atr, slip, slip / med_atr, rt, rt / med_atr))
    print("\n  slip/ATR >= 1.0 means the entry fill is displaced by a FULL stop-width")
    print("  before the trade even begins — the stop then sits at roughly the")
    print("  prevailing market price and is hit by ordinary intrabar noise.")


def random_entries(d, n_trades, rr, seed=11, stop_atr=1.0, max_hold=30,
                   slip=None, span=60.0):
    rng = np.random.default_rng(seed)
    n = len(d["close"])
    sig = np.zeros(n, dtype=np.int8)
    pick = rng.choice(np.arange(100, n - max_hold - 2), size=min(n_trades, n - 200),
                      replace=False)
    sig[pick] = rng.choice([1, -1], size=len(pick))
    old = sim.SLIP_PCT
    if slip is not None:
        sim.SLIP_PCT = slip
    try:
        tr = sim.simulate(d, sig, stop_atr=stop_atr, rr=rr, max_hold=max_hold)["trades"]
        m = sim.metrics(tr, span)
        g = float(np.mean([t["gross_r"] for t in tr])) if tr else None
    finally:
        sim.SLIP_PCT = old
    return m, g


def main():
    d1, d5 = phase1.load()
    d1_is = phase1.slice_days(d1, 0, 60)
    d5_is = phase1.slice_days(d5, 0, 60)
    scale_check(d1_is, d5_is)

    print("\n" + "=" * 96)
    print("CONTROL 2 — RANDOM entries under the prescribed fill model (the harness null)")
    print("=" * 96)
    print("{:<10} {:>8} {:>8} {:>11} {:>11}".format("tf", "trades", "win%", "grossR", "netR"))
    for tf, d, nt in (("1m", d1_is, 4000), ("5m", d5_is, 1000)):
        m, g = random_entries(d, nt, 1.2)
        print("{:<10} {:>8} {:>8} {:>11} {:>11}".format(
            tf, m["total_trades"], str(m["win_rate"]), str(round(g, 4)),
            str(m["avg_r_per_trade"])))
    print("\n  Real strategies on 1m: win% 2-5, gross -0.78 to -0.84")
    print("  If random matches that, the signals are indistinguishable from noise")
    print("  UNDER THIS FILL MODEL — which is a statement about the model, not the edge.")

    print("\n" + "=" * 96)
    print("CONTROL 3 — direction shuffle on REAL signal bars (does direction inform?)")
    print("=" * 96)
    print("{:<28} {:>10} {:>11} {:>13} {:>11}".format(
        "strategy", "trades", "real gross", "shuffled gross", "delta"))
    rng = np.random.default_rng(5)
    for slug in ("ema_cross", "traffic_light", "mean_reversion_bollinger",
                 "inside_candle", "swingking_sniper"):
        cfg = vec.DEFAULTS[slug]
        d = d5_is if cfg["tf"] == "5m" else d1_is
        s = vec.GENERATORS[slug](d, **cfg["params"])
        real = sim.simulate(d, s, rr=cfg["rr"], max_hold=30)["trades"]
        gr = float(np.mean([t["gross_r"] for t in real])) if real else None
        s2 = s.copy()
        idx = np.flatnonzero(s2 != 0)
        s2[idx] = rng.choice([1, -1], size=len(idx))
        shuf = sim.simulate(d, s2, rr=cfg["rr"], max_hold=30)["trades"]
        gs = float(np.mean([t["gross_r"] for t in shuf])) if shuf else None
        print("{:<28} {:>10} {:>11} {:>13} {:>11}".format(
            slug, len(real), round(gr, 4), round(gs, 4), round(gr - gs, 4)))
    print("\n  delta ~ 0 => the direction call adds nothing over a coin flip on the")
    print("  same bars. delta > 0 => the signal does carry directional information.")

    print("\n" + "=" * 96)
    print("CONTROL 4 — stop/slippage sweep: where does the null return to ~0 gross?")
    print("=" * 96)
    print("{:<10} {:>10} {:>10} {:>10} {:>11}".format(
        "slip%", "stop_atr", "trades", "win%", "grossR"))
    for slip in (0.0005, 0.0001, 0.0):
        for st in (1.0, 3.0, 6.0):
            m, g = random_entries(d1_is, 3000, 1.2, stop_atr=st, slip=slip,
                                  max_hold=60)
            print("{:<10} {:>10} {:>10} {:>10} {:>11}".format(
                f"{slip*100:.3f}", st, m["total_trades"], str(m["win_rate"]),
                str(round(g, 4)) if g is not None else "-"))
    print("\n  A correctly-specified harness should show gross ~0 for RANDOM entries.")
    print("  Rows that do NOT are rows where the fill model, not the market,")
    print("  determines the outcome.")


if __name__ == "__main__":
    main()

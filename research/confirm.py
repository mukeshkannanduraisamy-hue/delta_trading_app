"""Confirmatory test of the pre-registered hypothesis.

Runs EXACTLY the two candidates named in PREREGISTRATION_2026-07-25.md against
data days 365-800, which has never been loaded or inspected in this project.

This file deliberately contains no search, no parameter grid and no candidate
selection. If you find yourself editing it to try another variant, stop: that
converts the confirmatory test into a second exploratory search and consumes
the last untouched data this project has access to.
"""

from __future__ import annotations

import json
import time

import numpy as np

from research import (calibrate, costs, data, drift_control, gauntlet, optdata,
                      premium, search, sim, vec)

TRAIN_DAYS = 365          # the exploratory study's window -- must be EXCLUDED
TOTAL_DAYS = 800          # limit of Delta's BTCUSD history

# The pre-registered candidates. Do not add to this list.
CANDIDATES = [
    {"family": "mean_reversion_bollinger", "timeframe": "5m",
     "dte_band": "1-3d", "params": {}, "gates": []},
    {"family": "mean_reversion_bollinger", "timeframe": "15m",
     "dte_band": "1-3d", "params": {}, "gates": []},
]

MIN_TRADES = 200
DTE_HOURS = search.PRIMARY_DTE["1-3d"]


def untouched_window(refresh: bool = False) -> dict:
    """Days 365-800: everything OLDER than the exploratory study's window."""
    full = data.fetch("BTCUSD", "1m", TOTAL_DAYS, refresh=refresh)
    cutoff = int(time.time()) - TRAIN_DAYS * 86400
    keep = full["time"] < cutoff
    if keep.sum() < 10000:
        raise RuntimeError(
            f"only {int(keep.sum())} untouched bars available; "
            "cannot run a confirmatory test")
    return {k: v[keep] for k, v in full.items()}


def _overlay():
    snaps = optdata.snapshot_spreads("BTC")
    d1 = data.fetch("BTCUSD", "1m", TRAIN_DAYS)
    rvs = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    rv = float(rvs[np.isfinite(rvs)][-1])
    spread = calibrate.fit_spread(snaps)
    carry, _ = calibrate.fit_carry(snaps)
    iv, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=rv)
    smile, _ = calibrate.fit_smile(snaps, carry, iv, rv)
    iv.smile = smile
    return calibrate.Overlay(iv, spread, costs.OptionCost(), carry_rate=carry)


def run_candidate(cand: dict, d_val: dict, ov) -> dict:
    """Evaluate one candidate over the WHOLE untouched window.

    No dev/holdout split: the entire window is the test. Splitting it would
    create room to choose a favourable sub-period, which §5 of the
    pre-registration forbids.
    """
    tf = cand["timeframe"]
    d_tf = data.resample(d_val, "1m", tf)
    bs = data.res_seconds(tf)

    mk = lambda call: search.build_premium_series(d_tf, ov, DTE_HOURS, call, bs)
    ce, pe = mk(True), mk(False)
    rr = float(vec.DEFAULTS.get(cand["family"], {}).get("rr", 1.5))

    sig = search.build_signals(cand, d_tf, ce, None)
    trades = search._trades(sig, ce, pe, d_tf["close"], rr, costs.OptionCost())

    n = len(trades)
    exp = float(np.mean([t["r"] for t in trades])) if trades else None

    # criterion 3: beat the constant-direction control
    bundle = {"spot": d_tf, "ce": ce, "pe": pe, "cost": costs.OptionCost(),
              "idx": {"dev": (0, len(d_tf["time"])),
                      "holdout": (0, len(d_tf["time"]))}}
    drift = drift_control.assess(sig, bundle, rr, "dev")

    passed = (n >= MIN_TRADES and exp is not None and exp > 0
              and bool(drift["beats_constant"]))
    return {
        "candidate": f"{cand['family']} {tf} {cand['dte_band']} ungated",
        "bars": len(d_tf["time"]), "n_trades": n, "expectancy": exp,
        "c1_min_trades": n >= MIN_TRADES,
        "c2_positive": exp is not None and exp > 0,
        "c3_beats_constant": bool(drift["beats_constant"]),
        "always_call": drift["always_call_expectancy"],
        "always_put": drift["always_put_expectancy"],
        "call_pct": drift["direction_mix"]["ce_frac"],
        "passed": passed,
    }


def main() -> dict:
    print("=" * 84)
    print("CONFIRMATORY TEST -- pre-registered, 2 candidates, run once")
    print("=" * 84)

    d_val = untouched_window()
    t0, t1 = int(d_val["time"][0]), int(d_val["time"][-1])
    span = (t1 - t0) / 86400.0
    c = d_val["close"]
    print(f"\nuntouched window: {len(d_val['time']):,} bars, {span:.0f} days")
    print(f"  {time.strftime('%Y-%m-%d', time.gmtime(t0))} -> "
          f"{time.strftime('%Y-%m-%d', time.gmtime(t1))}")
    print(f"  BTC {c[0]:,.0f} -> {c[-1]:,.0f}  ({(c[-1]/c[0]-1)*100:+.1f}%)")

    rep = data.validate(d_val, "1m", "BTCUSD untouched")
    print(f"  completeness {rep['completeness_pct']}%  "
          f"bad OHLC {rep['bad_ohlc_bars']}  gaps>5m {rep['gaps_over_5min']}")

    ov = _overlay()
    results = [run_candidate(c_, d_val, ov) for c_ in CANDIDATES]

    print(f"\n{'candidate':44s} {'trades':>7} {'exp':>9} {'c1':>4} {'c2':>4} {'c3':>4}  verdict")
    print("-" * 84)
    for r in results:
        e = "-" if r["expectancy"] is None else f"{r['expectancy']:+.4f}"
        print(f"{r['candidate']:44s} {r['n_trades']:7d} {e:>9} "
              f"{str(r['c1_min_trades']):>4} {str(r['c2_positive']):>4} "
              f"{str(r['c3_beats_constant']):>4}  "
              f"{'PASS' if r['passed'] else 'FAIL'}")

    print("\ndirection controls:")
    for r in results:
        cp = "-" if r["call_pct"] is None else f"{r['call_pct']:.0%}"
        ac = "-" if r["always_call"] is None else f"{r['always_call']:+.4f}"
        ap = "-" if r["always_put"] is None else f"{r['always_put']:+.4f}"
        print(f"  {r['candidate']:44s} allCE={ac} allPE={ap} calls={cp}")

    confirmed = any(r["passed"] for r in results)
    print("\n" + "=" * 84)
    print(f"  HYPOTHESIS {'CONFIRMED' if confirmed else 'NOT CONFIRMED'}")
    if not confirmed:
        print("  Per pre-registration section 5: recorded as a failed")
        print("  confirmation. No re-slicing, no parameter adjustment, no")
        print("  further candidates against this window.")
    print("=" * 84)

    out = {"preregistered": True, "confirmed": confirmed, "results": results,
           "window": {"bars": len(d_val["time"]), "days": round(span, 1),
                      "start": t0, "end": t1,
                      "btc_change_pct": round((c[-1] / c[0] - 1) * 100, 2)}}
    with open("research/confirmation_results.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote research/confirmation_results.json")
    return out


if __name__ == "__main__":
    main()

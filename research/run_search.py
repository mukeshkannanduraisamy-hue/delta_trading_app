"""Run the strategy search and write the study report.

Primary scope is DTE >= 1 day, set by the overlay's validation record.
Sub-daily is run separately and labelled EXPLORATORY -- it is NOT part of the
acceptance set.

The Strict bar in gauntlet.py is used unchanged. It is not relaxed because the
overlay passed its own credibility gate narrowly; if anything a marginal
overlay argues for keeping it strict.
"""

from __future__ import annotations

import json
import time

import numpy as np

from research import (calibrate, costs, data, families, gauntlet, optdata,
                      premium, search, sizing, vec)


def _overlay():
    snaps = optdata.snapshot_spreads("BTC")
    d1 = data.fetch("BTCUSD", "1m", 365)
    rvs = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    rv = float(rvs[np.isfinite(rvs)][-1])
    spread = calibrate.fit_spread(snaps)
    carry, carry_rep = calibrate.fit_carry(snaps)
    iv, iv_rep = calibrate.fit_iv_term(snaps, rv_at_calibration=rv)
    smile, smile_rep = calibrate.fit_smile(snaps, carry, iv, rv)
    iv.smile = smile
    ov = calibrate.Overlay(iv, spread, costs.OptionCost(), carry_rate=carry)
    return ov, d1, {"carry": carry_rep, "iv": iv_rep, "smile": smile_rep,
                    "rv": rv, "n_quotes": len(snaps)}


def _bundle(d_tf, ov, dte_hours, bar_seconds):
    """Premium series (point estimate + IV band) and splits for one timeframe."""
    mk = lambda call, mult: search.build_premium_series(
        d_tf, ov, dte_hours, call, bar_seconds, iv_mult=mult)
    lo, hi = search.IV_BAND
    return {
        "spot": d_tf,
        "ce": mk(True, 1.0), "pe": mk(False, 1.0),
        "ce_lo": mk(True, lo), "pe_lo": mk(False, lo),
        "ce_hi": mk(True, hi), "pe_hi": mk(False, hi),
        "cost": costs.OptionCost(),
        "idx": search.split_indices(len(d_tf["time"])),
    }


def run(dte_bands, label) -> dict:
    ov, d1, fits = _overlay()
    cands = search.enumerate_candidates(dte_bands)
    budget = search.declared_budget(cands)
    rng = np.random.default_rng(search.SEED)

    print("=" * 88)
    print(f"STRATEGY SEARCH -- {label}")
    print("=" * 88)
    print(f"declared budget: {budget} candidates  (correction applied over ALL of them)")
    print(f"seed: {search.SEED}   holdout: last {search.HOLDOUT_FRAC:.0%}, "
          f"{search.WF_WINDOWS} walk-forward windows")
    print(f"DTE bands: {dte_bands}")

    tfs = {}
    for tf in search.TIMEFRAMES:
        d_tf = data.resample(d1, "1m", tf)
        rep = data.validate(d_tf, tf, f"BTCUSD {tf}")
        print(f"  {tf:4s} {rep['bars']:>8} bars  completeness={rep['completeness_pct']}%")
        tfs[tf] = d_tf

    # 1h close forward-filled onto each grid, for the HTF trend gate
    htf = {}
    d1h = data.resample(d1, "1m", "1h")
    for tf, d_tf in tfs.items():
        pos = np.clip(np.searchsorted(d1h["time"], d_tf["time"], "right") - 1,
                      0, len(d1h["time"]) - 1)
        htf[tf] = d1h["close"][pos]

    bundles = {}
    for tf in search.TIMEFRAMES:
        bs = data.res_seconds(tf)
        for band, dte in dte_bands.items():
            t0 = time.time()
            bundles[(tf, band)] = _bundle(tfs[tf], ov, dte, bs)
            print(f"  built premium series {tf}/{band} "
                  f"({len(tfs[tf]['time'])} bars, {time.time()-t0:.1f}s)")

    results = []
    for i, c in enumerate(cands, 1):
        b = bundles[(c["timeframe"], c["dte_band"])]
        try:
            sig = search.build_signals(c, b["spot"], b["ce"], htf[c["timeframe"]])
            ctx = search.evaluate(c, b, sig, budget, rng)
            g = gauntlet.run(ctx)
        except Exception as e:  # noqa: BLE001 — one bad combo must not abort
            results.append({**c, "error": f"{type(e).__name__}: {e}",
                            "passed": False, "failed_at": "error", "reached": -1})
            continue
        results.append({**c, "passed": g["passed"], "failed_at": g["failed_at"],
                        "reached": g["reached"],
                        "n_trades": len(ctx.get("trades") or []),
                        "expectancy": (float(np.mean([t["r"] for t in ctx["trades"]]))
                                       if ctx.get("trades") else None),
                        "walk_forward": ctx.get("walk_forward"),
                        "shuffle_p": ctx.get("shuffle_p"),
                        "corrected_p": ctx.get("corrected_p"),
                        "holdout": ctx.get("holdout_expectancy"),
                        "details": g["details"]})
        if i % 50 == 0:
            print(f"    {i}/{budget} evaluated, "
                  f"{sum(1 for r in results if r.get('passed'))} passing so far")

    survivors = [r for r in results if r.get("passed")]
    errors = [r for r in results if r.get("failed_at") == "error"]

    print(f"\n  evaluated {len(results)}   survivors {len(survivors)}   errors {len(errors)}")

    from collections import Counter
    stage = Counter(r.get("failed_at") for r in results if not r.get("passed"))
    print("\n  where candidates died:")
    for name in gauntlet.ORDER + ["error"]:
        if stage.get(name):
            print(f"    {name:24s} {stage[name]:5d}")

    return {"label": label, "budget": budget, "seed": search.SEED,
            "dte_bands": dte_bands, "fits": fits, "results": results,
            "survivors": survivors, "n_errors": len(errors),
            "died_at": dict(stage)}


def main():
    primary = run(search.PRIMARY_DTE, "PRIMARY (DTE >= 1 day)")
    exploratory = run(search.EXPLORATORY_DTE, "EXPLORATORY (<1d, NOT in the acceptance set)")

    out = {"primary": primary, "exploratory": exploratory,
           "generated": time.strftime("%Y-%m-%d %H:%M UTC", time.gmtime())}
    with open("research/search_results.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote research/search_results.json")
    return out


if __name__ == "__main__":
    main()

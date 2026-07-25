"""Overlay credibility gate, validated against EXECUTABLE trade prints.

Same overlay, same holdout window, same MAE/RMSE definitions, same R2 on
premium changes, same pass thresholds as run_calibration.py. The ONLY change
is the reference series: real trade prints instead of Delta's MARK model.
"""

from __future__ import annotations

import json

import numpy as np

from research import calibrate, costs, data, optdata, premium


def _fit(snaps, rv_now):
    spread = calibrate.fit_spread(snaps)
    carry, carry_rep = calibrate.fit_carry(snaps)
    iv, iv_rep = calibrate.fit_iv_term(snaps, rv_at_calibration=rv_now)
    smile, smile_rep = calibrate.fit_smile(snaps, carry, iv, rv_now)
    iv.smile = smile
    return (calibrate.Overlay(iv, spread, costs.OptionCost(), carry_rate=carry),
            {"carry": carry_rep, "iv": iv_rep, "smile": smile_rep})


def main() -> dict:
    print("=" * 78)
    print("OVERLAY GATE -- validated against EXECUTABLE TRADE PRINTS")
    print("=" * 78)

    snaps = optdata.snapshot_spreads("BTC")
    d1 = data.fetch("BTCUSD", "1m", 365)
    rvs = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    rv_now = float(rvs[np.isfinite(rvs)][-1])
    overlay, fits = _fit(snaps, rv_now)
    print(f"\nfitted on {len(snaps)} live quotes   "
          f"carry={fits['carry']['carry_rate']:.5f}/yr   RV={rv_now:.4f}")

    contracts = optdata.recent_real_contracts("BTC", min_bars=300, limit=30)
    print(f"evaluating {len(contracts)} contracts\n")

    reports = [calibrate.validate_overlay_traded(overlay, s) for s in contracts]
    scored = [r for r in reports if r.get("reason") is None]
    skipped = [r for r in reports if r.get("reason") is not None]

    hdr = "{:26s} {:>6} {:>8} {:>9} {:>7} {:>8} {:>9}  {}"
    print(hdr.format("contract", "prints", "MAE", "R2(chg)", "K/F", "DTEh",
                     "premium", "verdict"))
    print("-" * 96)
    for r in sorted(scored, key=lambda x: x["mae_pct"]):
        print(hdr.format(r["symbol"], r["n_bars"], f"{r['mae_pct']:.3f}",
                         f"{r['r2_changes']:.3f}", f"{r['median_moneyness']:.4f}",
                         f"{r['median_dte_hours']:.1f}",
                         f"{r['median_premium']:.1f}",
                         "PASS" if r["passed"] else "FAIL"))
    if skipped:
        print(f"\n{len(skipped)} skipped:")
        for r in skipped[:10]:
            print(f"   {r['symbol']:26s} {r['reason']}")

    passed = [r for r in scored if r["passed"]]
    gate = bool(scored) and len(passed) >= max(1, len(scored) // 2)

    # ---- breakdowns requested for the report ----
    def _bd(rows, label, keyfn, buckets):
        print(f"\n  error by {label}:")
        for lo, hi, name in buckets:
            sel = [r for r in rows if lo <= keyfn(r) < hi]
            if not sel:
                continue
            print(f"    {name:14s} n={len(sel):3d}  "
                  f"medMAE={np.median([r['mae_pct'] for r in sel]):.3f}  "
                  f"medR2={np.median([r['r2_changes'] for r in sel]):.3f}  "
                  f"pass={sum(1 for r in sel if r['passed'])}/{len(sel)}")

    if scored:
        _bd(scored, "moneyness (K/F)", lambda r: r["median_moneyness"],
            [(0.0, 0.99, "ITM-ish <0.99"), (0.99, 1.01, "ATM 0.99-1.01"),
             (1.01, 9.9, "OTM-ish >1.01")])
        _bd(scored, "DTE", lambda r: r["median_dte_hours"],
            [(0, 24, "<1d"), (24, 72, "1-3d"), (72, 168, "3-7d"), (168, 1e9, ">7d")])

        for kind, name in (("C", "calls"), ("P", "puts")):
            sel = [r for r in scored if r["kind"] == kind]
            if sel:
                print(f"\n  {name}: pass {sum(1 for r in sel if r['passed'])}/{len(sel)}  "
                      f"medMAE={np.median([r['mae_pct'] for r in sel]):.3f}  "
                      f"medR2={np.median([r['r2_changes'] for r in sel]):.3f}")

        f = [r for r in scored if not r["passed"]]
        lvl = sum(1 for r in f if r["mae_pct"] > calibrate.MAE_PCT_LIMIT
                  and r["r2_changes"] >= calibrate.R2_CHANGES_LIMIT)
        dyn = sum(1 for r in f if r["mae_pct"] <= calibrate.MAE_PCT_LIMIT
                  and r["r2_changes"] < calibrate.R2_CHANGES_LIMIT)
        both = len(f) - lvl - dyn
        print(f"\n  failure type: level-only={lvl}  dynamics-only={dyn}  both={both}")

    print("\n" + "=" * 78)
    print(f"  scored {len(scored)}, {len(passed)} pass "
          f"(MAE<={calibrate.MAE_PCT_LIMIT}, R2(chg)>={calibrate.R2_CHANGES_LIMIT})")
    print(f"  GATE: {'PASSED' if gate else 'FAILED'}")
    print("=" * 78)

    out = {"reference": "traded_prints", "fits": fits, "rv": rv_now,
           "validation": reports, "n_scored": len(scored),
           "n_passed": len(passed), "gate_passed": gate}
    with open("research/validation_traded_results.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote research/validation_traded_results.json")
    return out


if __name__ == "__main__":
    main()

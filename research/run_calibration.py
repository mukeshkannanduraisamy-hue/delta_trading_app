"""Fit the option overlay to real Delta data and report whether it tracks.

This is the credibility gate from the design. If the overlay cannot reproduce
real mark prices on contracts it was not fitted on, the study STOPS here
rather than emitting strategy results built on a broken price model.
"""

from __future__ import annotations

import json

import numpy as np

from research import calibrate, costs, data, optdata, premium


def main() -> dict:
    print("=" * 78)
    print("OPTION OVERLAY CALIBRATION")
    print("=" * 78)

    print("\n[1/4] sampling the live option book ...")
    snaps = optdata.snapshot_spreads("BTC")
    two_sided = len(snaps)
    atm = [s for s in snaps if abs(s["moneyness"] - 1.0) <= calibrate.ATM_BAND]
    print(f"      {two_sided} two-sided quotes, {len(atm)} within the ATM band")

    spread_model = calibrate.fit_spread(snaps)
    print("      spread buckets (median % of mid):")
    for k in sorted(spread_model.buckets, key=str):
        print(f"        {str(k):24s} {spread_model.buckets[k]:.4f}")
    print(f"        fallback{'':16s} {spread_model.fallback:.4f}")

    print("\n[2/4] measuring realized vol at calibration time ...")
    d1 = data.fetch("BTCUSD", "1m", 365)
    rv_series = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    finite = rv_series[np.isfinite(rv_series)]
    rv_now = float(finite[-1])
    print(f"      RV(1d) now = {rv_now:.4f}   365d median = {float(np.median(finite)):.4f}")

    print("\n[3/4] fitting the IV term structure ...")
    iv_model, iv_report = calibrate.fit_iv_term(snaps, rv_at_calibration=rv_now)
    for band, info in sorted(iv_report["bands"].items(), key=lambda kv: kv[1]["dte_hours"]):
        print(f"      {band:8s} n={info['n']:3d}  dte={info['dte_hours']:7.1f}h  "
              f"ATM IV={info['iv']:.4f}")
    print(f"      NOTE: {iv_report['note']}")

    carry, carry_report = calibrate.fit_carry(snaps)
    print(f"\n      forward carry from put-call parity: {carry:.5f}/yr "
          f"({carry_report.get('annualized_pct', 0)}%) from "
          f"{carry_report['n_pairs']} call/put pairs")

    smile, smile_report = calibrate.fit_smile(snaps, carry, iv_model, rv_now)
    iv_model.smile = smile
    print("\n      volatility smile (IV / ATM-IV vs standardized moneyness),"
          " OTM leg only:")
    for rng, info in smile_report.get("knots", {}).items():
        if info.get("dropped"):
            print(f"        x {rng:14s} n={info['n']:3d}  DROPPED (too thin)")
        else:
            print(f"        x {rng:14s} n={info['n']:3d}  IV/atm={info['ratio']:.3f}")
    if smile_report.get("note"):
        print(f"        NOTE: {smile_report['note']}")

    overlay = calibrate.Overlay(iv_model, spread_model, costs.OptionCost(),
                                carry_rate=carry)

    print("\n[4/4] validating against REAL mark prices (held out from the fit) ...")
    contracts = optdata.recent_real_contracts("BTC", min_bars=300, limit=30)
    print(f"      {len(contracts)} contracts carry usable mark history")

    reports = [calibrate.validate_overlay(overlay, s) for s in contracts]
    scored = [r for r in reports if r.get("reason") is None]
    skipped = [r for r in reports if r.get("reason") is not None]

    print(f"\n      {'contract':26s} {'bars':>6} {'MAE%':>8} {'R2(chg)':>9}  verdict")
    print("      " + "-" * 64)
    for r in sorted(scored, key=lambda x: x["mae_pct"]):
        print(f"      {r['symbol']:26s} {r['n_bars']:6d} {r['mae_pct']:8.3f} "
              f"{r['r2_changes']:9.3f}  {'PASS' if r['passed'] else 'FAIL'}")
    if skipped:
        print(f"\n      {len(skipped)} skipped (outside ATM band or no history)")

    passed = [r for r in scored if r["passed"]]
    gate = bool(scored) and len(passed) >= max(1, len(scored) // 2)

    print("\n" + "=" * 78)
    print(f"  scored {len(scored)} contracts, {len(passed)} pass "
          f"(MAE<={calibrate.MAE_PCT_LIMIT}, R2(chg)>={calibrate.R2_CHANGES_LIMIT})")
    print(f"  GATE: {'PASSED' if gate else 'FAILED'}")
    if not gate:
        print("  -> Do NOT proceed to the search. The overlay cannot reproduce")
        print("     real option prices, so any strategy result from it would be")
        print("     a statement about the model, not about the market.")
    print("=" * 78)

    out = {
        "n_quotes": two_sided,
        "n_atm_quotes": len(atm),
        "rv_at_calibration": rv_now,
        "carry_report": carry_report,
        "iv_report": iv_report,
        "smile_report": smile_report,
        "spread_buckets": {str(k): v for k, v in spread_model.buckets.items()},
        "spread_fallback": spread_model.fallback,
        "validation": reports,
        "n_scored": len(scored),
        "n_passed": len(passed),
        "gate_passed": gate,
    }
    with open("research/calibration_results.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote research/calibration_results.json")
    return out


if __name__ == "__main__":
    main()

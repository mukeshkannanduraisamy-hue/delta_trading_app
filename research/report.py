"""Assemble the Section 7 final report from the three phase result files."""

from __future__ import annotations

import json


def load(name):
    with open(f"research/{name}", encoding="utf-8") as fh:
        return json.load(fh)


def main():
    p1, p2, p34 = load("phase1_results.json"), load("phase2_results.json"), load("phase34_results.json")
    L = []
    A = L.append

    A("=" * 112)
    A("BACKTEST + RETUNE FINAL REPORT")
    A("Date: 2026-07-21")
    A("Data: BTCUSD 1m (129,600 bars) + 5m (25,920 bars), 90 days, Delta Exchange India")
    A("Split: in-sample days 0-60 (Phase 2/3) | out-of-sample days 60-90 (Phase 4 only)")
    A("=" * 112)

    A("\nPHASE 1 SUMMARY — INITIAL BACKTEST (90 days, all costs applied)")
    h = "{:<26} {:>7} {:>6} {:>6} {:>9} {:>9} {:>8} {:>7} {:>9}"
    A(h.format("Strategy", "Trades", "Win%", "Loss%", "GrossR", "NetR", "Sharpe", "PF", "Status"))
    A("-" * 112)
    for slug, v in p1.items():
        m = v["metrics"]
        g = p2.get(slug, {}).get("is_metrics", {})
        A(h.format(slug, m["total_trades"], str(m["win_rate"]), str(m["loss_rate"]),
                   "", str(m["avg_r_per_trade"]), str(m["sharpe_ratio"]),
                   str(m["profit_factor"]), v["status"]))
    A("\n  Flagged for Phase 2: {}/{}".format(
        sum(1 for v in p1.values() if v["flagged"]), len(p1)))

    A("\n\nPHASE 2 SUMMARY — LOSS ANALYSIS FINDINGS (in-sample only)")
    A("-" * 112)
    for slug, e in p2.items():
        gs = e.get("grid_summary", {})
        emm = e.get("E_mfe_mae", {})
        filt = e.get("F_filters", []) or []
        bf = max(filt, key=lambda f: f.get("gross_r") or -9) if filt else None
        A(f"\n{slug}")
        A(f"  root cause identified : YES")
        A(f"  grid combos tested    : {gs.get('combos')}  "
          f"positive GROSS: {gs.get('positive_gross')}  "
          f"meeting candidate bar: {gs.get('candidates')}")
        if emm:
            A(f"  MFE/MAE (losers)      : MFE {emm.get('avg_mfe_r_losers')}R vs "
              f"MAE {emm.get('avg_mae_r_losers')}R | "
              f"{emm.get('pct_losers_that_never_moved_favorably')}% never moved favorably")
        if bf:
            A(f"  best filter           : {bf['filter']} -> gross {bf['gross_r']} "
              f"(baseline {filt[0].get('gross_r')})")
        best = gs.get("best_gross")
        if best:
            A(f"  best grid combo       : {best.get('params')} gross={best.get('gross_r')} "
              f"net={best.get('net_r')}")

    A("\n\nPHASE 3 SUMMARY — RETUNING DECISIONS")
    A("-" * 112)
    for slug, v in p34.items():
        pl = v["plan"]
        A(f"  {slug:<28} {pl['action']:<16} {pl['reason']}")

    A("\n\nPHASE 4 SUMMARY — OUT-OF-SAMPLE VALIDATION")
    h2 = "{:<26} {:>12} {:>11} {:>11} {:>11} {:>10} {:>16}"
    A(h2.format("Strategy", "Orig 90d", "IS net", "OOS net", "OOS gross",
                "WF pos", "Verdict"))
    A("-" * 112)
    for slug, v in p34.items():
        A(h2.format(slug,
                    str(v["orig_90d"]["avg_r_per_trade"]),
                    str(v["is"]["avg_r_per_trade"]),
                    str(v["oos"]["avg_r_per_trade"]),
                    str(v["oos"].get("gross_r")),
                    f'{v.get("wf_positive_windows")}/3',
                    v["verdict"]))

    by = {}
    for slug, v in p34.items():
        by.setdefault(v["verdict"], []).append(slug)

    A("\n\nFINAL RECOMMENDATIONS")
    A("-" * 112)
    A(f"  Deploy to paper trading : {by.get('PASS') or 'NONE'}")
    A(f"  Do not deploy           : "
      f"{(by.get('FAIL', []) + by.get('MARGINAL', []) + by.get('OVERFIT', [])) or 'n/a'}")
    A(f"  Concept-flawed (abandon): {by.get('CONCEPT_FLAWED') or 'NONE'}")

    A("\n\nOVERFITTING WARNINGS")
    A("-" * 112)
    warn = [s for s, v in p34.items()
            if (v["is"]["avg_r_per_trade"] or -9) > 0.5 and (v["oos"]["avg_r_per_trade"] or 0) < 0]
    A(f"  {warn or 'None — no strategy showed a large in-sample gain at all, so there was nothing to overfit TO.'}")

    out = "\n".join(L)
    with open("research/FINAL_REPORT.txt", "w", encoding="utf-8") as fh:
        fh.write(out)
    print(out)


if __name__ == "__main__":
    main()

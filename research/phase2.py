"""PHASE 2 — deep historical loss analysis (Analyses A-F).

STRICT: every analysis here runs on the IN-SAMPLE window only (first 60 days).
The final 30 days are never touched until Phase 4.

Analyses report GROSS R alongside net R. That separation is the whole point:
net R conflates "the signal is wrong" with "the cost model is heavy", and only
the first is fixable by retuning. A strategy whose GROSS expectancy is negative
has no edge to rescue at any cost level.
"""

from __future__ import annotations

import json
from collections import defaultdict

import numpy as np

from research import phase1, premium, sim, vec

IS_DAYS = 60.0          # in-sample: days 0-60
OOS_DAYS = 30.0         # out-of-sample: days 60-90 (Phase 4 ONLY)


# --------------------------------------------------------------------------- #
# Regime tagging
# --------------------------------------------------------------------------- #
def regimes(d: dict) -> dict:
    """Regime tags.

    NOTE ON THRESHOLDS. The absolute cutoffs specified for this study
    (ATR/spot > 0.015 = high vol, < 0.005 = low vol, |EMA20-EMA50|/spot < 0.005
    = ranging) are calibrated for daily/hourly bars. On 1m BTC data ATR/spot
    sits around 0.0005, so EVERY bar is tagged "low vol" and "ranging" and the
    tags carry zero information — filter A selects nothing at all.

    Both are therefore computed: the prescribed absolute tags (kept so the
    degeneracy is visible and reportable) and self-calibrating percentile tags
    on the same quantities, which actually discriminate at this timeframe.
    """
    c, h, l = d["close"], d["high"], d["low"]
    e20, e50 = vec.ema(c, 20), vec.ema(c, 50)
    a = vec.atr(h, l, c, 14)
    atr_pct = a / c
    spread = np.abs(e20 - e50) / c

    fin = np.isfinite(atr_pct)
    hi_cut = float(np.percentile(atr_pct[fin], 80)) if fin.any() else np.inf
    lo_cut = float(np.percentile(atr_pct[fin], 20)) if fin.any() else -np.inf
    sfin = np.isfinite(spread)
    rng_cut = float(np.percentile(spread[sfin], 33)) if sfin.any() else np.inf

    return {
        # prescribed absolute tags
        "trend_up": (c > e50) & (e20 > e50),
        "trend_dn": (c < e50) & (e20 < e50),
        "ranging": spread < 0.005,
        "high_vol": atr_pct > 0.015,
        "low_vol": atr_pct < 0.005,
        # self-calibrating equivalents (percentiles of this dataset)
        "p_high_vol": atr_pct > hi_cut,
        "p_low_vol": atr_pct < lo_cut,
        "p_ranging": spread < rng_cut,
        "atr_pct": atr_pct, "e20": e20, "e50": e50,
        "_cuts": {"hi_vol_p80": hi_cut, "lo_vol_p20": lo_cut,
                  "ranging_p33": rng_cut},
    }


def analysis_a(trades, reg) -> list[dict]:
    """A — loss clustering by market regime at entry."""
    names = ("trend_up", "trend_dn", "ranging", "high_vol", "low_vol",
             "p_high_vol", "p_low_vol", "p_ranging")
    total_losses = sum(1 for t in trades if t["r"] < 0)
    rows = []
    for nm in names:
        mask = reg[nm]
        sel = [t for t in trades if mask[t["i"]]]
        if not sel:
            rows.append({"regime": nm, "trades": 0})
            continue
        losses = [t for t in sel if t["r"] < 0]
        rows.append({
            "regime": nm, "trades": len(sel), "losses": len(losses),
            "loss_rate": round(100 * len(losses) / len(sel), 1),
            "pct_of_all_losses": round(100 * len(losses) / total_losses, 1) if total_losses else 0.0,
            "gross_r": round(float(np.mean([t["gross_r"] for t in sel])), 4),
            "net_r": round(float(np.mean([t["r"] for t in sel])), 4),
        })
    return rows


def analysis_b(trades, times) -> list[dict]:
    """B — loss pattern by hour of day (UTC)."""
    by_hour = defaultdict(list)
    for t in trades:
        by_hour[int((times[t["i"]] // 3600) % 24)].append(t)
    rows = []
    for hr in range(24):
        sel = by_hour.get(hr, [])
        if not sel:
            rows.append({"hour": hr, "trades": 0})
            continue
        w = [t for t in sel if t["r"] > 0]
        rows.append({
            "hour": hr, "trades": len(sel),
            "win_rate": round(100 * len(w) / len(sel), 1),
            "loss_rate": round(100 * (len(sel) - len(w)) / len(sel), 1),
            "gross_r": round(float(np.mean([t["gross_r"] for t in sel])), 4),
            "net_r": round(float(np.mean([t["r"] for t in sel])), 4),
        })
    return rows


def analysis_c(trades, d) -> dict:
    """C — signal quality: what distinguishes winners from losers?"""
    o, c, v = d["open"], d["close"], d["volume"]
    vol_ma = vec.sma(v, 20)
    body = np.abs(c - o) / o * 100.0
    win = [t for t in trades if t["r"] > 0]
    los = [t for t in trades if t["r"] <= 0]

    def agg(ts, arr):
        vals = [arr[t["i"]] for t in ts if np.isfinite(arr[t["i"]])]
        return round(float(np.mean(vals)), 4) if vals else None

    vol_ratio = np.divide(v, vol_ma, out=np.full(len(v), np.nan), where=vol_ma > 0)
    out = {
        "winners": len(win), "losers": len(los),
        "body_pct_win": agg(win, body), "body_pct_loss": agg(los, body),
        "vol_ratio_win": agg(win, vol_ratio), "vol_ratio_loss": agg(los, vol_ratio),
    }
    # Is there a threshold that separates them? Test deciles of each feature.
    best = None
    for label, arr in (("body_pct", body), ("vol_ratio", vol_ratio)):
        vals = np.array([arr[t["i"]] for t in trades])
        rs = np.array([t["gross_r"] for t in trades])
        ok = np.isfinite(vals)
        if ok.sum() < 50:
            continue
        for q in (10, 20, 30, 40, 50, 60, 70, 80, 90):
            thr = float(np.percentile(vals[ok], q))
            above = ok & (vals >= thr)
            if above.sum() < 30:
                continue
            gr = float(rs[above].mean())
            if best is None or gr > best["gross_r"]:
                best = {"feature": label, "pctile": q, "threshold": round(thr, 4),
                        "trades_kept": int(above.sum()), "gross_r": round(gr, 4)}
    out["best_discriminator"] = best
    return out


def analysis_e(trades) -> dict:
    """E — MFE/MAE: are TP and SL in the right place?"""
    los = [t for t in trades if t["r"] <= 0]
    if not los:
        return {}
    mfe = np.array([t["mfe_r"] for t in los])
    mae = np.array([t["mae_r"] for t in los])
    allm = np.array([t["mfe_r"] for t in trades])
    alla = np.array([t["mae_r"] for t in trades])
    return {
        "losing_trades": len(los),
        "avg_mfe_r_losers": round(float(mfe.mean()), 4),
        "avg_mae_r_losers": round(float(mae.mean()), 4),
        "mfe_mae_ratio_losers": round(float(mfe.mean() / mae.mean()), 4) if mae.mean() else None,
        "mfe_p75_all_r": round(float(np.percentile(allm, 75)), 4),
        "mae_p25_all_r": round(float(np.percentile(alla, 25)), 4),
        "suggested_tp_atr": round(float(np.percentile(allm, 75)), 3),
        "suggested_sl_atr": round(float(np.percentile(alla, 25)), 3),
        "pct_losers_that_never_moved_favorably": round(
            100.0 * float((mfe < 0.1).mean()), 1),
    }


def analysis_f(slug, d, reg, params, rr, span, max_hold=30) -> list[dict]:
    """F — would a regime filter eliminate the losses?"""
    sig0 = vec.GENERATORS[slug](d, **params)
    c, v, t = d["close"], d["volume"], d["time"]
    vol_ma = vec.sma(v, 20)
    hour = ((t // 3600) % 24).astype(int)
    e50 = reg["e50"]

    filters = {
        "None": np.ones(len(c), dtype=bool),
        # Prescribed absolute cutoff — selects nothing on 1m data, kept so the
        # degeneracy is measured rather than assumed.
        "A_minvol":   reg["atr_pct"] > 0.008,
        # Self-calibrating equivalent: top-20% volatility bars on THIS dataset.
        "A2_volp80":  reg["p_high_vol"],
        "B_volume":   np.nan_to_num(v / np.where(vol_ma > 0, vol_ma, np.nan), nan=0) > 1.2,
        "C_trend":    np.ones(len(c), dtype=bool),   # direction-aware, applied below
        "D_hours":    (hour >= 8) & (hour < 20),
    }
    rows = []
    base = None
    for name, mask in filters.items():
        s = sig0.copy()
        if name == "C_trend":
            with np.errstate(invalid="ignore"):
                s[(s == 1) & ~(c > e50)] = 0
                s[(s == -1) & ~(c < e50)] = 0
        else:
            s[~mask] = 0
        res = sim.simulate(d, s, rr=rr, max_hold=max_hold)
        m = sim.metrics(res["trades"], span)
        g = (round(float(np.mean([x["gross_r"] for x in res["trades"]])), 4)
             if res["trades"] else None)
        if base is None:
            base = g
        rows.append({"filter": name, "trades": m["total_trades"],
                     "win_rate": m["win_rate"], "gross_r": g,
                     "net_r": m["avg_r_per_trade"], "max_dd": m["max_drawdown_pct"],
                     "improvement_gross": (round(g - base, 4)
                                           if (g is not None and base is not None) else None)})
    # Combined uses the self-calibrating volatility tag; the prescribed absolute
    # one selects zero bars at this timeframe, which would make the combination
    # trivially empty and uninformative.
    s = sig0.copy()
    comb = filters["A2_volp80"] & filters["B_volume"] & filters["D_hours"]
    s[~comb] = 0
    with np.errstate(invalid="ignore"):
        s[(s == 1) & ~(c > e50)] = 0
        s[(s == -1) & ~(c < e50)] = 0
    res = sim.simulate(d, s, rr=rr, max_hold=max_hold)
    m = sim.metrics(res["trades"], span)
    g = round(float(np.mean([x["gross_r"] for x in res["trades"]])), 4) if res["trades"] else None
    rows.append({"filter": "A+B+C+D", "trades": m["total_trades"], "win_rate": m["win_rate"],
                 "gross_r": g, "net_r": m["avg_r_per_trade"], "max_dd": m["max_drawdown_pct"],
                 "improvement_gross": (round(g - base, 4)
                                       if (g is not None and base is not None) else None)})
    return rows


# --------------------------------------------------------------------------- #
# D — parameter grids
# --------------------------------------------------------------------------- #
GRIDS = {
    "ema_cross": [{"fast": f, "slow": s}
                  for f in (5, 7, 9, 11, 13) for s in (15, 18, 21, 25, 30)],
    "scalping_pulse": [{"fast": f, "trend": t}
                       for f in (5, 7, 9) for t in (20, 25, 30)],
    "traffic_light": [{"lookback": lb} for lb in (1, 2, 3)],
    "inside_candle": [{"proximity": p, "_rr": r}
                      for p in (0.25, 0.30, 0.35, 0.40, 0.45)
                      for r in (1.5, 2.0, 2.5, 3.0)],
    "mean_reversion_bollinger": [{"period": p, "k": k}
                                 for p in (15, 20, 25, 30) for k in (1.5, 2.0, 2.5, 3.0)],
    "prime_scalper_ema": [{"ema_period": e, "atr_period": a, "threshold": th}
                          for e in (7, 9, 12, 15) for a in (10, 14, 20)
                          for th in (0.1, 0.2, 0.3, 0.5)],
    "swingking_sniper": [{"fast": f, "slow": s}
                         for f in (15, 20, 25) for s in (40, 50, 60)],
    "booming_bulls_supertrend": [{"fast_p": ap, "fast_m": m, "_tp": tp, "_sl": sl}
                                 for ap in (7, 10, 14) for m in (2.0, 2.5, 3.0, 3.5)
                                 for tp in (0.05, 0.075, 0.10, 0.125)
                                 for sl in (0.03, 0.05, 0.07)],
}


def grid_search(slug, d1_is, d5_is, ps_is, span):
    cfg = vec.DEFAULTS[slug]
    d = d5_is if cfg["tf"] == "5m" else d1_is
    out = []
    for combo in GRIDS[slug]:
        p = dict(cfg["params"])
        rr = cfg["rr"]
        stop_atr = 1.0
        cp = {k: v for k, v in combo.items() if not k.startswith("_")}
        p.update(cp)
        if "_rr" in combo:
            rr = combo["_rr"]
        if "_tp" in combo:                # premium strategy: tp/sl as % of premium
            rr = combo["_tp"] / combo["_sl"]
        try:
            if slug == "booming_bulls_supertrend":
                trades = []
                for side in ("CE", "PE"):
                    s = ps_is[side]
                    sig = vec.sig_booming_bulls(s, **p)
                    r = sim.simulate(s, sig, stop_atr=stop_atr, rr=rr,
                                     max_hold=30, long_only=True)
                    trades.extend(r["trades"])
            else:
                sig = vec.GENERATORS[slug](d, **p)
                trades = sim.simulate(d, sig, stop_atr=stop_atr, rr=rr,
                                      max_hold=30)["trades"]
        except Exception as exc:  # noqa: BLE001
            out.append({"params": combo, "error": repr(exc)})
            continue
        m = sim.metrics(trades, span)
        g = float(np.mean([t["gross_r"] for t in trades])) if trades else None
        out.append({
            "params": combo, "trades": m["total_trades"], "win_rate": m["win_rate"],
            "gross_r": round(g, 4) if g is not None else None,
            "net_r": m["avg_r_per_trade"], "max_dd": m["max_drawdown_pct"],
            "sharpe": m["sharpe_ratio"], "pf": m["profit_factor"],
            "candidate": bool(m["total_trades"] and (m["avg_r_per_trade"] or -9) > 0
                              and (m["win_rate"] or 0) > 50
                              and (m["max_drawdown_pct"] or 1e9) < 15),
        })
    return out


def main():
    d1, d5 = phase1.load()
    d1_is = phase1.slice_days(d1, 0, IS_DAYS)
    d5_is = phase1.slice_days(d5, 0, IS_DAYS)
    ps_is = premium.atm_premium_series(d1_is, 60)

    print("=" * 100)
    print(f"PHASE 2 — DEEP LOSS ANALYSIS · IN-SAMPLE ONLY (days 0-{IS_DAYS:.0f})")
    print(f"1m bars {len(d1_is['close'])}   5m bars {len(d5_is['close'])}")
    print("=" * 100)

    report = {}
    for slug in vec.DEFAULTS:
        cfg = vec.DEFAULTS[slug]
        d = d5_is if cfg["tf"] == "5m" else d1_is
        m, trades, _ = phase1.run_strategy(slug, d1_is, d5_is, span_days=IS_DAYS,
                                           premium_series=ps_is)
        print(f"\n{'='*100}\n{slug.upper()}   IS trades={m['total_trades']}  "
              f"net_r={m['avg_r_per_trade']}  win%={m['win_rate']}")
        entry = {"is_metrics": m}

        if slug != "booming_bulls_supertrend" and trades:
            reg = regimes(d)
            entry["A_regime"] = analysis_a(trades, reg)
            print("\n  A. LOSS CLUSTERING BY REGIME")
            print("    {:<12} {:>8} {:>8} {:>10} {:>12} {:>10} {:>10}".format(
                "regime", "trades", "losses", "loss%", "%allLosses", "grossR", "netR"))
            for r in entry["A_regime"]:
                if not r.get("trades"):
                    continue
                print("    {:<12} {:>8} {:>8} {:>9}% {:>11}% {:>10} {:>10}".format(
                    r["regime"], r["trades"], r["losses"], r["loss_rate"],
                    r["pct_of_all_losses"], r["gross_r"], r["net_r"]))

            entry["B_hour"] = analysis_b(trades, d["time"])
            good = [r for r in entry["B_hour"] if r.get("trades", 0) > 20
                    and (r.get("gross_r") or -9) > 0]
            print(f"\n  B. TIME-OF-DAY — hours with POSITIVE gross R: "
                  f"{[r['hour'] for r in good] or 'NONE'}")

            entry["C_quality"] = analysis_c(trades, d)
            cq = entry["C_quality"]
            print(f"\n  C. SIGNAL QUALITY  body% win/loss={cq['body_pct_win']}/"
                  f"{cq['body_pct_loss']}  volRatio win/loss={cq['vol_ratio_win']}/"
                  f"{cq['vol_ratio_loss']}")
            print(f"     best discriminator: {cq['best_discriminator']}")

            entry["E_mfe_mae"] = analysis_e(trades)
            e = entry["E_mfe_mae"]
            print(f"\n  E. MFE/MAE  losers avg MFE={e['avg_mfe_r_losers']}R "
                  f"MAE={e['avg_mae_r_losers']}R | suggested TP={e['suggested_tp_atr']}R "
                  f"SL={e['suggested_sl_atr']}R")
            print(f"     losers that NEVER moved favorably: "
                  f"{e['pct_losers_that_never_moved_favorably']}%")

            entry["F_filters"] = analysis_f(slug, d, reg, cfg["params"], cfg["rr"], IS_DAYS)
            print("\n  F. REGIME FILTERS")
            print("    {:<10} {:>8} {:>7} {:>10} {:>10} {:>12}".format(
                "filter", "trades", "win%", "grossR", "netR", "improvement"))
            for r in entry["F_filters"]:
                print("    {:<10} {:>8} {:>7} {:>10} {:>10} {:>12}".format(
                    r["filter"], r["trades"], str(r["win_rate"]), str(r["gross_r"]),
                    str(r["net_r"]), str(r["improvement_gross"])))

        print(f"\n  D. PARAMETER GRID ({len(GRIDS[slug])} combos)")
        g = grid_search(slug, d1_is, d5_is, ps_is, IS_DAYS)
        entry["D_grid"] = g
        valid = [x for x in g if x.get("gross_r") is not None]
        valid.sort(key=lambda x: x["gross_r"], reverse=True)
        cands = [x for x in g if x.get("candidate")]
        pos_gross = [x for x in valid if x["gross_r"] > 0]
        print("    best 3 by GROSS R:")
        for x in valid[:3]:
            print(f"      {x['params']}  trades={x['trades']} win%={x['win_rate']} "
                  f"grossR={x['gross_r']} netR={x['net_r']}")
        print(f"    combos with POSITIVE gross R : {len(pos_gross)}/{len(g)}")
        print(f"    combos meeting CANDIDATE bar  : {len(cands)}/{len(g)}")
        entry["grid_summary"] = {
            "combos": len(g), "positive_gross": len(pos_gross),
            "candidates": len(cands),
            "best_gross": valid[0] if valid else None,
        }
        report[slug] = entry

    with open("research/phase2_results.json", "w", encoding="utf-8") as fh:
        json.dump(report, fh, indent=2, default=str)
    print("\nwrote research/phase2_results.json")
    return report


if __name__ == "__main__":
    main()

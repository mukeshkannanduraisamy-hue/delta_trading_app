"""PHASE 3 (retune) + PHASE 4 (out-of-sample validation + walk-forward).

Retuning is driven ONLY by Phase 2 findings on the in-sample window. Phase 4
then scores the retuned configurations on the untouched final 30 days, plus a
3-window walk-forward.

A retune is proposed for a strategy only if Phase 2 found a parameter set or
filter with POSITIVE in-sample gross expectancy. Where nothing in the grid or
the filter set produced positive gross R, the strategy is classified
CONCEPT_FLAWED and deliberately left alone — tuning a signal with no measurable
edge is curve-fitting by definition.
"""

from __future__ import annotations

import json

import numpy as np

from research import phase1, phase2, premium, sim, vec

IS_DAYS, OOS_START, TOTAL = 60.0, 60.0, 90.0


def score(slug, d1, d5, ps, params, rr, span, filt=None, stop_atr=1.0, max_hold=30):
    """Backtest one configuration; returns metrics + gross R."""
    cfg = vec.DEFAULTS[slug]
    p = dict(cfg["params"])
    p.update({k: v for k, v in (params or {}).items() if not k.startswith("_")})
    d = d5 if cfg["tf"] == "5m" else d1

    if slug == "booming_bulls_supertrend":
        trades = []
        for side in ("CE", "PE"):
            s = ps[side]
            sig = vec.sig_booming_bulls(s, **p)
            trades.extend(sim.simulate(s, sig, stop_atr=stop_atr, rr=rr,
                                       max_hold=max_hold, long_only=True)["trades"])
    else:
        sig = vec.GENERATORS[slug](d, **p)
        if filt is not None:
            sig = filt(sig, d)
        trades = sim.simulate(d, sig, stop_atr=stop_atr, rr=rr,
                              max_hold=max_hold)["trades"]
    m = sim.metrics(trades, span)
    m["gross_r"] = (round(float(np.mean([t["gross_r"] for t in trades])), 4)
                    if trades else None)
    return m


def make_filter(use_vol_p80=False, use_trend=False, use_hours=False,
                use_body=None, ref=None):
    """Build a signal filter from the Phase 2 findings that were actually tested."""
    def _f(sig, d):
        s = sig.copy()
        c, v, t = d["close"], d["volume"], d["time"]
        keep = np.ones(len(c), dtype=bool)
        if use_vol_p80:
            a = vec.atr(d["high"], d["low"], c, 14)
            ap = a / c
            fin = np.isfinite(ap)
            cut = float(np.percentile(ap[fin], 80)) if fin.any() else np.inf
            keep &= ap > cut
        if use_hours:
            hour = ((t // 3600) % 24).astype(int)
            keep &= (hour >= 8) & (hour < 20)
        if use_body is not None:
            body = np.abs(c - d["open"]) / d["open"] * 100.0
            keep &= body >= use_body
        s[~keep] = 0
        if use_trend:
            e50 = vec.ema(c, 50)
            with np.errstate(invalid="ignore"):
                s[(s == 1) & ~(c > e50)] = 0
                s[(s == -1) & ~(c < e50)] = 0
        return s
    return _f


def build_plan(p2: dict, p1: dict) -> dict:
    """Decide, per strategy, whether a data-backed retune exists.

    Section 8 Rule 8 is applied first and is decisive: if Phase 1 net expectancy
    is below -1.0 R AND no grid combination reached positive net expectancy,
    the strategy is CONCEPT_FLAWED and is not retuned. Tuning a signal that
    shows no edge under any tested parameterization is curve-fitting; the only
    thing more searching can find is noise that fits this particular 60 days.

    Where the rule does not bite, the best grid combination is adopted, or
    failing that the best filter that produced positive gross expectancy.
    """
    plan = {}
    for slug, e in p2.items():
        gs = e.get("grid_summary", {})
        best = gs.get("best_gross")
        grid = e.get("D_grid", []) or []
        pos_net = [x for x in grid if (x.get("net_r") or -9) > 0]
        pos_gross = gs.get("positive_gross", 0)
        filters = e.get("F_filters", []) or []
        fpos = [f for f in filters
                if f.get("gross_r") is not None and f["gross_r"] > 0]

        p1_net = ((p1.get(slug) or {}).get("metrics") or {}).get("avg_r_per_trade")
        rule8 = (p1_net is not None and p1_net < -1.0 and not pos_net)

        # Best filter by gross, even if still negative — used to quantify the
        # ceiling of what filtering can achieve, not to justify a retune.
        best_filter = (max(filters, key=lambda f: f.get("gross_r") or -9)
                       if filters else None)

        common = {"is_gross": (best or {}).get("gross_r"),
                  "is_net": (best or {}).get("net_r"),
                  "phase1_net": p1_net,
                  "grid_positive_net": len(pos_net),
                  "grid_positive_gross": pos_gross,
                  "best_filter_gross": (best_filter or {}).get("gross_r"),
                  "best_filter_name": (best_filter or {}).get("filter")}

        if rule8:
            plan[slug] = {**common, "action": "CONCEPT_FLAWED",
                          "params": (best or {}).get("params", {}),
                          "reason": (f"Rule 8: Phase-1 net {p1_net} < -1.0 R and 0 of "
                                     f"{len(grid)} grid combos reached positive net "
                                     f"expectancy in-sample")}
        elif pos_net:
            b = max(pos_net, key=lambda x: x["net_r"])
            plan[slug] = {**common, "action": "RETUNE", "params": b["params"],
                          "reason": "grid combo with positive in-sample NET expectancy",
                          "is_gross": b.get("gross_r"), "is_net": b.get("net_r")}
        elif pos_gross and best and (best.get("gross_r") or -9) > 0:
            plan[slug] = {**common, "action": "RETUNE", "params": best["params"],
                          "reason": "grid combo with positive in-sample gross expectancy"}
        elif fpos:
            bf = max(fpos, key=lambda f: f["gross_r"])
            plan[slug] = {**common, "action": "RETUNE", "params": {},
                          "filter": bf["filter"],
                          "reason": f'filter {bf["filter"]} produced positive gross R'}
        else:
            plan[slug] = {**common, "action": "NO_EDGE_FOUND",
                          "params": (best or {}).get("params", {}),
                          "reason": ("no parameter combination and no filter produced "
                                     "positive expectancy in-sample")}
    return plan


def main():
    with open("research/phase2_results.json", encoding="utf-8") as fh:
        p2 = json.load(fh)
    with open("research/phase1_results.json", encoding="utf-8") as fh:
        p1 = json.load(fh)

    d1, d5 = phase1.load()
    d1_is, d5_is = phase1.slice_days(d1, 0, IS_DAYS), phase1.slice_days(d5, 0, IS_DAYS)
    d1_oos, d5_oos = phase1.slice_days(d1, OOS_START, TOTAL), phase1.slice_days(d5, OOS_START, TOTAL)
    ps_is = premium.atm_premium_series(d1_is, 60)
    ps_oos = premium.atm_premium_series(d1_oos, 60)
    ps_all = premium.atm_premium_series(d1, 60)

    plan = build_plan(p2, p1)

    print("=" * 108)
    print("PHASE 3 — RETUNE PLAN (derived only from in-sample Phase 2 findings)")
    print("=" * 108)
    for slug, pl in plan.items():
        print(f"\n{slug}")
        print(f"  action        : {pl['action']}")
        print(f"  reason        : {pl['reason']}")
        print(f"  best IS gross : {pl['is_gross']}   best IS net: {pl['is_net']}")
        print(f"  grid combos with positive net/gross : "
              f"{pl['grid_positive_net']}/{pl['grid_positive_gross']}")
        print(f"  best filter   : {pl['best_filter_name']} (gross {pl['best_filter_gross']})")
        if pl["action"] == "RETUNE":
            print(f"  params        : {pl.get('params')}  filter={pl.get('filter')}")

    print("\n" + "=" * 108)
    print(f"PHASE 4 — OUT-OF-SAMPLE VALIDATION (days {OOS_START:.0f}-{TOTAL:.0f}, never used before)")
    print("=" * 108)
    hdr = "{:<26} {:>12} {:>12} {:>12} {:>12} {:>10} {:>14}"
    print(hdr.format("strategy", "orig90d net", "IS net", "OOS net", "OOS gross",
                     "OOS win%", "verdict"))
    print("-" * 108)

    results = {}
    for slug, pl in plan.items():
        cfg = vec.DEFAULTS[slug]
        rr = cfg["rr"]
        params = pl.get("params") or {}
        if "_rr" in params:
            rr = params["_rr"]
        if "_tp" in params and "_sl" in params:
            rr = params["_tp"] / params["_sl"]
        filt = None
        if pl.get("filter"):
            fname = pl["filter"]
            filt = make_filter(
                use_vol_p80=("A2" in fname or "A+" in fname),
                use_trend=("C" in fname),
                use_hours=("D" in fname))

        m_orig = phase1.run_strategy(slug, d1, d5, span_days=TOTAL,
                                     premium_series=ps_all)[0]
        m_is = score(slug, d1_is, d5_is, ps_is, params, rr, IS_DAYS, filt)
        m_oos = score(slug, d1_oos, d5_oos, ps_oos, params, rr, TOTAL - OOS_START, filt)

        oos_net = m_oos["avg_r_per_trade"]
        is_net = m_is["avg_r_per_trade"]
        if m_oos["total_trades"] == 0:
            verdict = "NO-TRADES"
        elif oos_net > 0 and (m_oos["win_rate"] or 0) > 50:
            verdict = "PASS"
        elif (is_net or -9) > 0.3 and (oos_net or 0) < -0.3:
            verdict = "OVERFIT"
        elif (oos_net or -9) > -0.2:
            verdict = "MARGINAL"
        else:
            verdict = "FAIL"
        if pl["action"] == "CONCEPT_FLAWED":
            verdict = "CONCEPT_FLAWED"

        print(hdr.format(slug,
                         str(m_orig["avg_r_per_trade"]), str(is_net), str(oos_net),
                         str(m_oos["gross_r"]), str(m_oos["win_rate"]), verdict))
        results[slug] = {"plan": pl, "orig_90d": m_orig, "is": m_is,
                         "oos": m_oos, "verdict": verdict}

    # ---------------- walk-forward, 3 windows ---------------- #
    print("\n" + "=" * 108)
    print("WALK-FORWARD — 3 x 30-day windows (gross R, cost-independent signal edge)")
    print("=" * 108)
    print("{:<26} {:>14} {:>14} {:>14} {:>16}".format(
        "strategy", "w1 d0-30", "w2 d30-60", "w3 d60-90", "positive windows"))
    print("-" * 108)
    for slug, pl in plan.items():
        cfg = vec.DEFAULTS[slug]
        rr = cfg["rr"]
        params = pl.get("params") or {}
        if "_rr" in params:
            rr = params["_rr"]
        if "_tp" in params and "_sl" in params:
            rr = params["_tp"] / params["_sl"]
        gs = []
        for a, b in ((0, 30), (30, 60), (60, 90)):
            w1 = phase1.slice_days(d1, a, b)
            w5 = phase1.slice_days(d5, a, b)
            wps = premium.atm_premium_series(w1, 60)
            m = score(slug, w1, w5, wps, params, rr, 30.0)
            gs.append(m["gross_r"])
        npos = sum(1 for g in gs if g is not None and g > 0)
        print("{:<26} {:>14} {:>14} {:>14} {:>16}".format(
            slug, str(gs[0]), str(gs[1]), str(gs[2]), f"{npos}/3"))
        results[slug]["walk_forward_gross"] = gs
        results[slug]["wf_positive_windows"] = npos

    with open("research/phase34_results.json", "w", encoding="utf-8") as fh:
        json.dump(results, fh, indent=2, default=str)
    print("\nwrote research/phase34_results.json")
    return results


if __name__ == "__main__":
    main()

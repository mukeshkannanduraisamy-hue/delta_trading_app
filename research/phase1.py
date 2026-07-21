"""PHASE 1 — initial backtest of all 8 strategies on 90 days of BTCUSD."""

from __future__ import annotations

import json

import numpy as np

from research import data as dl, premium, sim, vec

SPAN_DAYS = 90.0
MAX_HOLD = 30


def load():
    return dl.fetch("BTCUSD", "1m", 90), dl.fetch("BTCUSD", "5m", 90)


def slice_days(d: dict, start_day: float, end_day: float) -> dict:
    """Sub-slice by day offset from the series start (for IS/OOS splits)."""
    t0 = d["time"][0]
    lo = t0 + int(start_day * 86400)
    hi = t0 + int(end_day * 86400)
    m = (d["time"] >= lo) & (d["time"] < hi)
    return {k: v[m] for k, v in d.items() if isinstance(v, np.ndarray)}


def run_strategy(slug: str, d1: dict, d5: dict, *, params: dict | None = None,
                 stop_atr: float = 1.0, rr: float | None = None,
                 max_hold: int = MAX_HOLD, span_days: float = SPAN_DAYS,
                 premium_series: dict | None = None):
    """Run one strategy and return (metrics, trades, extras)."""
    cfg = vec.DEFAULTS[slug]
    p = dict(cfg["params"])
    if params:
        p.update(params)
    rr = cfg["rr"] if rr is None else rr
    d = d5 if cfg["tf"] == "5m" else d1

    if slug == "booming_bulls_supertrend":
        # Trades the OPTION PREMIUM, so it runs on the synthesized series.
        ps = premium_series or premium.atm_premium_series(d1, 60)
        all_trades = []
        for side in ("CE", "PE"):
            s = ps[side]
            sig = vec.sig_booming_bulls(s, **p)
            res = sim.simulate(s, sig, stop_atr=stop_atr, rr=rr,
                               max_hold=max_hold, long_only=True)
            for tr in res["trades"]:
                tr["dir"] = side
            all_trades.extend(res["trades"])
        all_trades.sort(key=lambda x: x["i"])
        return sim.metrics(all_trades, span_days), all_trades, {"basis": "premium(sim)"}

    sig = vec.GENERATORS[slug](d, **p)
    res = sim.simulate(d, sig, stop_atr=stop_atr, rr=rr, max_hold=max_hold)
    return sim.metrics(res["trades"], span_days), res["trades"], {"basis": "underlying"}


HDR = ("{:<26} {:>6} {:>6} {:>6} {:>8} {:>8} {:>7} {:>7} {:>6} {:>8} {:>9}")


def table(rows: list[tuple[str, dict]]) -> str:
    out = [HDR.format("Strategy", "Trades", "Win%", "Loss%", "AvgR",
                      "MaxDD%", "Sharpe", "PF", "Sig/d", "AvgCostR", "Status")]
    out.append("-" * len(out[0]))
    for slug, m in rows:
        out.append(HDR.format(
            slug, m["total_trades"],
            f'{m["win_rate"]}' if m["win_rate"] is not None else "-",
            f'{m["loss_rate"]}' if m["loss_rate"] is not None else "-",
            f'{m["avg_r_per_trade"]}' if m["avg_r_per_trade"] is not None else "-",
            f'{m["max_drawdown_pct"]}',
            f'{m["sharpe_ratio"]}' if m["sharpe_ratio"] is not None else "-",
            f'{m["profit_factor"]}' if m["profit_factor"] is not None else "-",
            f'{m["signal_frequency"]}',
            f'{m["avg_cost_r"]}' if m["avg_cost_r"] is not None else "-",
            sim.status(m)))
    return "\n".join(out)


def main():
    d1, d5 = load()
    print("=" * 118)
    print("PHASE 1 — INITIAL BACKTEST · BTCUSD · 90 days · Delta Exchange India")
    print("=" * 118)
    print(f"1m bars: {len(d1['close'])}   5m bars: {len(d5['close'])}")

    ps = premium.atm_premium_series(d1, 60)
    print("premium model:", json.dumps(premium.describe(ps)))
    print(f"costs: slippage {sim.SLIP_PCT*100:.3f}%/fill, taker {sim.FEE_PCT*100:.3f}%/side "
          f"+ {sim.GST_RATE*100:.0f}% GST | stop=1.0*ATR(14) | max_hold={MAX_HOLD} bars")
    print()

    rows, store = [], {}
    for slug in vec.DEFAULTS:
        m, trades, extra = run_strategy(slug, d1, d5, premium_series=ps)
        rows.append((slug, m))
        store[slug] = {"metrics": m, "extra": extra,
                       "flagged": sim.flagged(m), "status": sim.status(m)}
        store[slug]["n_trades"] = len(trades)
    print(table(rows))
    print()

    flag = [s for s, v in store.items() if v["flagged"]]
    print("Flagged for Phase 2 deep analysis ({}/{}):".format(len(flag), len(store)))
    for s in flag:
        m = store[s]["metrics"]
        why = []
        if (m["loss_rate"] or 0) > 50:
            why.append(f'loss_rate {m["loss_rate"]}%>50')
        if (m["avg_r_per_trade"] or 0) < -0.3:
            why.append(f'avgR {m["avg_r_per_trade"]}<-0.3')
        if (m["max_drawdown_pct"] or 0) > 20:
            why.append(f'maxDD {m["max_drawdown_pct"]}>20')
        if m["profit_factor"] is not None and m["profit_factor"] < 0.8:
            why.append(f'PF {m["profit_factor"]}<0.8')
        print(f"  - {s:<28} {'; '.join(why)}")

    # Cost is the headline number: if average round-trip cost exceeds average
    # gross edge, no parameter choice can save the strategy.
    print("\nCOST DIAGNOSTIC (why these numbers look the way they do)")
    print("{:<28} {:>10} {:>10} {:>10}".format("strategy", "grossR", "costR", "netR"))
    for slug in vec.DEFAULTS:
        m, trades, _ = run_strategy(slug, d1, d5, premium_series=ps)
        if not trades:
            continue
        g = float(np.mean([t["gross_r"] for t in trades]))
        cst = float(np.mean([t["cost_r"] for t in trades]))
        print("{:<28} {:>10.4f} {:>10.4f} {:>10.4f}".format(slug, g, -cst, g - cst))

    with open("research/phase1_results.json", "w", encoding="utf-8") as fh:
        json.dump(store, fh, indent=2, default=str)
    print("\nwrote research/phase1_results.json")
    return store


if __name__ == "__main__":
    main()

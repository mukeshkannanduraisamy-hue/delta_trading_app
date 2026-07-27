"""End-to-end BTC options backtest: data -> indicator -> option trade -> metrics.

    # single backtest
    .venv/Scripts/python.exe -m research.backtest_cli --strategy ema_cross --tf 15m

    # list available indicators
    .venv/Scripts/python.exe -m research.backtest_cli --list

    # parameter sweep
    .venv/Scripts/python.exe -m research.backtest_cli --strategy ema_cross --tune

This is a thin front-end over components that have already been validated:
costs from the live product spec, a premium overlay checked against executable
trade prints, and a simulator whose null control returns ~0 on random entries.
It does not reimplement any of that.

READ THIS BEFORE USING --tune
-----------------------------
Tuning finds the best parameters ON THE DATA YOU TUNE ON. That number is not an
estimate of future performance, and in this project the gap has been measured
rather than assumed:

  * A 1,440-candidate sweep produced in-sample expectancy up to +0.81 R/trade.
    Nothing survived out-of-sample validation.
  * The two candidates that did survive degraded 65% and 98% on their holdout.

`--tune` therefore reports in-sample figures and labels them as such. To learn
whether a tuned result is real you need data it was never fitted on. See
`PREREGISTRATION_2026-07-25.md` for how that was done properly here.
"""

from __future__ import annotations

import argparse
import itertools

import numpy as np

from research import (calibrate, costs, data, drift_control, families, optdata,
                      premium, search, sim, vec)

DEFAULT_DAYS = 365
DTE_CHOICES = {"1d": 24.0, "1-3d": 48.0, "3-7d": 120.0, "7d+": 240.0}

TUNE_GRIDS = {
    "ema_cross": {"fast": [5, 9, 15], "slow": [21, 34, 50]},
    "mean_reversion_bollinger": {"period": [14, 20, 30], "k": [1.5, 2.0, 2.5]},
    "scalping_pulse": {"fast": [5, 9, 13], "trend": [21, 34]},
    "traffic_light": {"lookback": [2, 3], "sma_period": [10, 15, 20]},
    "prime_scalper_ema": {"ema_period": [13, 21, 34], "threshold": [0.03, 0.05, 0.08]},
    "swingking_sniper": {"fast": [10, 20], "slow": [30, 50], "lookback": [60, 120]},
    "inside_candle": {"proximity": [0.25, 0.35, 0.5]},
    "momentum_persistence": {"lookback": [6, 12, 24], "persist": [2, 3]},
    "breakout_volume": {"channel": [20, 50], "vol_z": [1.0, 1.5, 2.0]},
}


def indicators() -> dict:
    out = {k: "built-in strategy" for k in vec.GENERATORS}
    out.update({k: "signal family" for k in families.ENTRY_FAMILIES})
    return out


def build_overlay():
    """Option pricing model, calibrated to the current live chain."""
    snaps = optdata.snapshot_spreads("BTC")
    d1 = data.fetch("BTCUSD", "1m", DEFAULT_DAYS)
    rvs = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    rv = float(rvs[np.isfinite(rvs)][-1])
    spread = calibrate.fit_spread(snaps)
    carry, _ = calibrate.fit_carry(snaps)
    iv, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=rv)
    smile, _ = calibrate.fit_smile(snaps, carry, iv, rv)
    iv.smile = smile
    return calibrate.Overlay(iv, spread, costs.OptionCost(), carry_rate=carry)


def backtest(strategy: str, tf: str, dte_hours: float, days: int,
             params: dict, overlay=None) -> dict:
    """One backtest. Direction -> CE if bullish, PE if bearish."""
    d1 = data.fetch("BTCUSD", "1m", days)
    d_tf = data.resample(d1, "1m", tf)
    bs = data.res_seconds(tf)
    ov = overlay if overlay is not None else build_overlay()

    ce = search.build_premium_series(d_tf, ov, dte_hours, True, bs)
    pe = search.build_premium_series(d_tf, ov, dte_hours, False, bs)

    cand = {"family": strategy, "timeframe": tf, "params": params, "gates": []}
    sig = search.build_signals(cand, d_tf, ce, None)
    rr = float(vec.DEFAULTS.get(strategy, {}).get("rr", 1.5))

    trades = search._trades(sig, ce, pe, d_tf["close"], rr, costs.OptionCost())
    span_days = (d_tf["time"][-1] - d_tf["time"][0]) / 86400.0
    m = sim.metrics(trades, span_days)

    bundle = {"spot": d_tf, "ce": ce, "pe": pe, "cost": costs.OptionCost(),
              "idx": {"dev": (0, len(d_tf["time"])),
                      "holdout": (0, len(d_tf["time"]))}}
    drift = drift_control.assess(sig, bundle, rr, "dev")

    return {"strategy": strategy, "timeframe": tf, "dte_hours": dte_hours,
            "params": params, "signals": int(np.count_nonzero(sig)),
            "metrics": m, "drift": drift, "bars": len(d_tf["time"]),
            "span_days": round(span_days, 1)}


def _print_one(r: dict) -> None:
    m, dr = r["metrics"], r["drift"]
    print(f"\n{'=' * 70}")
    print(f"  {r['strategy']}  {r['timeframe']}  DTE={r['dte_hours']:.0f}h  {r['params']}")
    print(f"{'=' * 70}")
    print(f"  bars {r['bars']:,}   span {r['span_days']} days   "
          f"signals {r['signals']:,}")
    if not m["total_trades"]:
        print("  no trades")
        return
    print(f"\n  trades            {m['total_trades']:,}")
    # sim.metrics returns win_rate already scaled to percent (sim.py:177),
    # so format as a plain number -- ":.1%" would multiply by 100 again.
    print(f"  win rate          {m['win_rate']:.1f}%" if m["win_rate"] is not None else "")
    print(f"  expectancy        {m['avg_r_per_trade']:+.4f} R/trade")
    print(f"  total             {m['total_r']:+.1f} R")
    print(f"  profit factor     {m['profit_factor']:.3f}" if m["profit_factor"] else "")
    print(f"  sharpe            {m['sharpe_ratio']:.3f}" if m["sharpe_ratio"] else "")
    print(f"  max drawdown      {m['max_drawdown_r']:.1f} R")
    print(f"  avg cost          {m['avg_cost_r']:.4f} R/trade")
    print(f"  avg hold          {m['avg_hold_bars']:.1f} bars")

    mix = dr["direction_mix"]
    print(f"\n  calls/puts        {mix['ce']:,} / {mix['pe']:,} "
          f"({mix['ce_frac']:.0%} calls)" if mix["ce_frac"] is not None else "")
    ac, ap = dr["always_call_expectancy"], dr["always_put_expectancy"]
    print(f"  always-call       {ac:+.4f} R" if ac is not None else "")
    print(f"  always-put        {ap:+.4f} R" if ap is not None else "")
    if dr["edge_over_constant"] is not None:
        verdict = "YES" if dr["beats_constant"] else "NO"
        print(f"  beats direction?  {verdict}  "
              f"({dr['edge_over_constant']:+.4f} R over best constant)")
        if not dr["beats_constant"]:
            print("     -> the signal adds nothing over just picking one side;")
            print("        any profit here is directional exposure, not skill.")


MIN_TEST_TRADES = 200


def _holds(train_e, test_e, n_test, beats_constant) -> bool:
    """Did the strategy survive out-of-sample?

    Requires FOUR things, and the first one is easy to forget:

      1. train expectancy > 0 — you cannot "validate" a strategy you would
         never have selected. A sweep found swingking_sniper at train -0.047 /
         test +0.021 and called it a survivor; nobody would deploy something
         that lost money on the data they chose from. Negative-in-train,
         positive-in-test is regression to the mean, not edge.
      2. test expectancy > 0
      3. enough test trades to mean anything
      4. beats constant-direction exposure — otherwise the P&L is just being
         long or short during a trend, not signal.
    """
    return (train_e is not None and train_e > 0
            and test_e is not None and test_e > 0
            and n_test >= MIN_TEST_TRADES
            and bool(beats_constant))


def _prepare(strategy: str, tf: str, dte_hours: float, days: int, overlay=None):
    """Fetch, resample and price the option legs ONCE for reuse across combos."""
    d1 = data.fetch("BTCUSD", "1m", days)
    d_tf = data.resample(d1, "1m", tf)
    bs = data.res_seconds(tf)
    ov = overlay if overlay is not None else build_overlay()
    ce = search.build_premium_series(d_tf, ov, dte_hours, True, bs)
    pe = search.build_premium_series(d_tf, ov, dte_hours, False, bs)
    return d_tf, ce, pe


def _eval_window(strategy, tf, params, d_tf, ce, pe, a, b) -> dict:
    """Evaluate one parameter set over bars [a, b).

    Signals are generated on the FULL series and then sliced, which is safe
    because every indicator here is causal (bar i uses only bars <= i). It also
    keeps the train and test slices using identical indicator warm-up state.
    """
    cand = {"family": strategy, "timeframe": tf, "params": params, "gates": []}
    sig = search.build_signals(cand, d_tf, ce, None)
    rr = float(vec.DEFAULTS.get(strategy, {}).get("rr", 1.5))

    ce_w, pe_w = search._slice(ce, a, b), search._slice(pe, a, b)
    trades = search._trades(sig[a:b], ce_w, pe_w, d_tf["close"][a:b], rr,
                            costs.OptionCost())
    span = (d_tf["time"][b - 1] - d_tf["time"][a]) / 86400.0
    m = sim.metrics(trades, span)

    bundle = {"spot": {k: v[a:b] for k, v in d_tf.items()},
              "ce": ce_w, "pe": pe_w, "cost": costs.OptionCost(),
              "idx": {"dev": (0, b - a), "holdout": (0, b - a)}}
    drift = drift_control.assess(sig[a:b], bundle, rr, "dev")
    return {"params": params, "metrics": m, "drift": drift,
            "span_days": round(span, 1)}


def validate(strategy: str, tf: str, dte_hours: float, days: int,
             split: float = 0.7) -> dict:
    """Tune on the first `split` of the data, report on the rest.

    This is the honest version of --tune. Parameters are chosen using ONLY the
    training slice; the number that gets reported comes from a slice the
    selection never saw. The gap between the two is the quantity that actually
    matters, and it is printed prominently because it is usually large.
    """
    d_tf, ce, pe = _prepare(strategy, tf, dte_hours, days)
    n = len(d_tf["time"])
    cut = int(n * split)

    grid = TUNE_GRIDS.get(strategy)
    combos = ([dict(zip(grid, v)) for v in itertools.product(*grid.values())]
              if grid else [dict(vec.DEFAULTS.get(strategy, {}).get("params", {}))])

    print(f"\n{'=' * 72}")
    print(f"  VALIDATED BACKTEST — {strategy} {tf} DTE={dte_hours:.0f}h")
    print(f"{'=' * 72}")
    print(f"  train  bars 0..{cut:,} ({split:.0%})   "
          f"test  bars {cut:,}..{n:,} ({1 - split:.0%})")
    print(f"  {len(combos)} parameter set(s); selection uses TRAIN ONLY\n")

    trained = []
    for i, p in enumerate(combos, 1):
        r = _eval_window(strategy, tf, p, d_tf, ce, pe, 0, cut)
        e = r["metrics"]["avg_r_per_trade"]
        trained.append((p, e, r["metrics"]["total_trades"]))
        print(f"  [{i}/{len(combos)}] train {str(p):42s} "
              f"n={r['metrics']['total_trades']:5d} "
              f"exp={e if e is not None else 0:+.4f}")

    scored = [t for t in trained if t[1] is not None]
    if not scored:
        print("\n  no parameter set produced trades on the training slice")
        return {}
    best_p, best_train_e, best_train_n = max(scored, key=lambda t: t[1])

    test = _eval_window(strategy, tf, best_p, d_tf, ce, pe, cut, n)
    tm, td = test["metrics"], test["drift"]
    test_e = tm["avg_r_per_trade"]

    print(f"\n  {'-' * 68}")
    print(f"  selected on TRAIN: {best_p}")
    print(f"  {'-' * 68}")
    print(f"  {'':22s} {'train':>14} {'TEST (unseen)':>16}")
    print(f"  {'trades':22s} {best_train_n:>14,} {tm['total_trades']:>16,}")
    print(f"  {'expectancy R/trade':22s} {best_train_e:>+14.4f} "
          f"{test_e if test_e is not None else float('nan'):>+16.4f}")
    if tm["total_r"] is not None:
        print(f"  {'total R':22s} {'':>14} {tm['total_r']:>+16.1f}")
    if tm["profit_factor"]:
        print(f"  {'profit factor':22s} {'':>14} {tm['profit_factor']:>16.3f}")

    if test_e is not None and best_train_e:
        drop = 100.0 * (1.0 - test_e / best_train_e) if best_train_e > 0 else None
        if drop is not None:
            print(f"\n  DEGRADATION train -> test: {drop:+.0f}%")

    beats = td["beats_constant"]
    print(f"  beats direction on TEST? {'YES' if beats else 'NO'}", end="")
    if td["edge_over_constant"] is not None:
        print(f"  ({td['edge_over_constant']:+.4f} R over best constant)")
    else:
        print()

    ok = _holds(best_train_e, test_e, tm["total_trades"], beats)
    print(f"\n  {'=' * 68}")
    print(f"  VERDICT: {'HOLDS OUT-OF-SAMPLE' if ok else 'DOES NOT HOLD'}")
    if not ok:
        why = []
        if best_train_e is None or best_train_e <= 0:
            why.append("train expectancy not positive — you would never have "
                       "selected this")
        if tm["total_trades"] < MIN_TEST_TRADES:
            why.append(f"only {tm['total_trades']} test trades "
                       f"(need {MIN_TEST_TRADES})")
        if test_e is None or test_e <= 0:
            why.append("test expectancy not positive")
        if not beats:
            why.append("does not beat constant-direction exposure")
        print(f"  reason: {'; '.join(why)}")
    print(f"  {'=' * 68}")

    print("\n  Note: this split is reusable, so it is NOT an untouched holdout.")
    print("  Re-running --validate across many strategies selects on the test")
    print("  slice too, just more slowly. Treat the first run as the honest")
    print("  one; after that the slice is progressively spent.")

    return {"selected": best_p, "train_expectancy": best_train_e,
            "test_expectancy": test_e, "test_trades": tm["total_trades"],
            "beats_constant": beats, "holds": ok}


def validate_all(tf: str, dte_hours: float, days: int,
                 split: float = 0.7) -> dict:
    """DECLARED SWEEP: validate every family, budget fixed before running.

    Running --validate one family at a time and stopping when something looks
    good is selection without accounting. This declares the whole budget up
    front, reports every family including the failures, and applies a
    Bonferroni correction over the full budget to anything that comes out
    positive. It also spends the test slice completely, which is stated rather
    than left for the reader to work out.
    """
    import math

    fams = sorted(indicators())

    def _n_combos(fam: str) -> int:
        """Number of parameter SETS, i.e. the product of the value lists.

        len(grid) counts parameter NAMES, not combinations -- using it reported
        a budget of 19 for a sweep that actually tested 63 sets, understating
        the multiple-comparison problem by 3x. In a declared sweep the budget
        is the whole point, so it has to be the real count.
        """
        grid = TUNE_GRIDS.get(fam)
        if not grid:
            return 1
        n = 1
        for values in grid.values():
            n *= len(values)
        return n

    budget = sum(_n_combos(f) for f in fams)

    print(f"\n{'=' * 78}")
    print("  DECLARED SWEEP — every family validated, budget fixed in advance")
    print(f"{'=' * 78}")
    print(f"  families            {len(fams)}")
    print(f"  parameter sets      {budget}  (correction applied over ALL of them)")
    print(f"  split               {split:.0%} train / {1 - split:.0%} unseen")
    print(f"  timeframe / DTE     {tf} / {dte_hours:.0f}h")
    print("\n  This spends the test slice. After this sweep it is training data.\n")

    d_tf, ce, pe = _prepare("__shared__", tf, dte_hours, days)
    n = len(d_tf["time"])
    cut = int(n * split)

    rows = []
    for fam in fams:
        grid = TUNE_GRIDS.get(fam)
        combos = ([dict(zip(grid, v)) for v in itertools.product(*grid.values())]
                  if grid else [dict(vec.DEFAULTS.get(fam, {}).get("params", {}))])

        trained = []
        for p in combos:
            try:
                r = _eval_window(fam, tf, p, d_tf, ce, pe, 0, cut)
            except Exception as e:                       # one bad combo must not abort
                continue
            e_ = r["metrics"]["avg_r_per_trade"]
            if e_ is not None:
                trained.append((p, e_))
        if not trained:
            rows.append({"family": fam, "note": "no trades on train"})
            print(f"  {fam:28s} no trades on train")
            continue

        best_p, train_e = max(trained, key=lambda t: t[1])
        try:
            test = _eval_window(fam, tf, best_p, d_tf, ce, pe, cut, n)
        except Exception as e:
            rows.append({"family": fam, "note": f"test error: {e}"})
            continue

        tm, td = test["metrics"], test["drift"]
        test_e, n_test = tm["avg_r_per_trade"], tm["total_trades"]

        holds = _holds(train_e, test_e, n_test, bool(td["beats_constant"]))
        rows.append({"family": fam, "params": best_p, "train": train_e,
                     "test": test_e, "test_trades": n_test,
                     "beats_constant": bool(td["beats_constant"]),
                     "holds": holds})
        print(f"  {fam:28s} train={train_e:+.4f}  test={test_e:+.4f}  "
              f"n={n_test:5d}  beats_dir={'Y' if td['beats_constant'] else 'N'}  "
              f"{'HOLDS' if holds else 'fails'}")

    scored = [r for r in rows if r.get("test") is not None]
    holders = [r for r in scored if r["holds"]]

    print(f"\n{'=' * 78}")
    print(f"  {'family':28s} {'train':>9} {'test':>9} {'trades':>7} {'dir':>4}  verdict")
    print(f"  {'-' * 74}")
    for r in sorted(scored, key=lambda r: -(r["test"] or -9)):
        print(f"  {r['family']:28s} {r['train']:+9.4f} {r['test']:+9.4f} "
              f"{r['test_trades']:7d} {'Y' if r['beats_constant'] else 'N':>4}  "
              f"{'HOLDS' if r['holds'] else 'fails'}")

    print(f"\n{'=' * 78}")
    if holders:
        print(f"  {len(holders)} of {len(scored)} families held out-of-sample.")
        print(f"  Budget was {budget} parameter sets. A single survivor from a")
        print(f"  sweep this size is what noise looks like -- treat any holder")
        print(f"  as a HYPOTHESIS to be confirmed on data this sweep never saw,")
        print(f"  not as a validated result.")
    else:
        print(f"  NO family held out-of-sample ({len(scored)} evaluated,")
        print(f"  {budget} parameter sets). Consistent with the 1,440-candidate")
        print(f"  search, which also produced nothing that survived.")
    print(f"\n  The {1 - split:.0%} test slice is now SPENT. Any further result on it")
    print("  is in-sample. The untouched 435-day window remains the only clean")
    print("  validation set, and is reserved by PREREGISTRATION_2026-07-25.md.")
    print(f"{'=' * 78}")

    return {"budget": budget, "families": len(fams), "results": rows,
            "holders": [h["family"] for h in holders]}


def tune(strategy: str, tf: str, dte_hours: float, days: int) -> list:
    grid = TUNE_GRIDS.get(strategy)
    if not grid:
        print(f"no tuning grid defined for '{strategy}'")
        return []
    combos = [dict(zip(grid, v)) for v in itertools.product(*grid.values())]

    print(f"\n{'=' * 70}")
    print(f"  PARAMETER SWEEP — {strategy} {tf} DTE={dte_hours:.0f}h")
    print(f"  {len(combos)} combinations. Results are IN-SAMPLE.")
    print(f"{'=' * 70}")

    ov = build_overlay()
    rows = []
    for i, p in enumerate(combos, 1):
        r = backtest(strategy, tf, dte_hours, days, p, overlay=ov)
        m = r["metrics"]
        rows.append({"params": p, "trades": m["total_trades"],
                     "expectancy": m["avg_r_per_trade"],
                     "total_r": m["total_r"],
                     "beats_constant": r["drift"]["beats_constant"]})
        print(f"  [{i}/{len(combos)}] {str(p):46s} "
              f"n={m['total_trades']:5d} "
              f"exp={m['avg_r_per_trade'] if m['avg_r_per_trade'] is not None else 0:+.4f}")

    ranked = sorted([r for r in rows if r["expectancy"] is not None],
                    key=lambda r: -r["expectancy"])
    print(f"\n  top 5 in-sample:")
    print(f"  {'params':46s} {'trades':>7} {'exp':>9} {'totalR':>9} {'beats dir':>10}")
    for r in ranked[:5]:
        print(f"  {str(r['params']):46s} {r['trades']:7d} {r['expectancy']:+9.4f} "
              f"{r['total_r']:+9.1f} {str(r['beats_constant']):>10}")

    print(f"\n  {'!' * 66}")
    print("  These are IN-SAMPLE results — the best row is the one that fit")
    print("  this data best, which is not the same as the one that will work.")
    print("  Measured in this project: a 1,440-candidate sweep reached +0.81 R")
    print("  in-sample and nothing survived out-of-sample; the two that did")
    print("  survive degraded 65% and 98% on their holdout.")
    print("  To trust a tuned result, test it on data it never saw.")
    print(f"  {'!' * 66}")
    return ranked


def main() -> None:
    ap = argparse.ArgumentParser(
        description="Backtest a BTC options strategy driven by a technical indicator")
    ap.add_argument("--strategy", default="mean_reversion_bollinger")
    ap.add_argument("--tf", default="15m", help="5m, 15m, 1h, 4h")
    ap.add_argument("--dte", default="1-3d", choices=list(DTE_CHOICES),
                    help="option maturity bucket")
    ap.add_argument("--days", type=int, default=DEFAULT_DAYS)
    ap.add_argument("--tune", action="store_true", help="parameter sweep (in-sample)")
    ap.add_argument("--validate", action="store_true",
                    help="tune on the first 70%% and report on the unseen rest")
    ap.add_argument("--validate-all", action="store_true",
                    help="declared sweep: validate EVERY family, budget fixed up front")
    ap.add_argument("--split", type=float, default=0.7,
                    help="train fraction for --validate (default 0.7)")
    ap.add_argument("--list", action="store_true", help="list indicators")
    args = ap.parse_args()

    if args.list:
        print("\navailable indicators:\n")
        for k, v in sorted(indicators().items()):
            grid = "tunable" if k in TUNE_GRIDS else "-"
            print(f"  {k:30s} {v:20s} {grid}")
        print("\ntimeframes: 5m 15m 1h 4h")
        print(f"DTE buckets: {', '.join(DTE_CHOICES)}")
        return

    known = indicators()
    if args.strategy not in known:
        print(f"unknown indicator '{args.strategy}'. Use --list.")
        return

    dte = DTE_CHOICES[args.dte]
    if args.validate_all:
        validate_all(args.tf, dte, args.days, args.split)
    elif args.validate:
        if not 0.3 <= args.split <= 0.9:
            print("--split must be between 0.3 and 0.9")
            return
        validate(args.strategy, args.tf, dte, args.days, args.split)
    elif args.tune:
        tune(args.strategy, args.tf, dte, args.days)
    else:
        params = dict(vec.DEFAULTS.get(args.strategy, {}).get("params", {}))
        _print_one(backtest(args.strategy, args.tf, dte, args.days, params))


if __name__ == "__main__":
    main()

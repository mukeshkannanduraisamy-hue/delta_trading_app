"""Forward validation on RECORDED BOOK DATA. No overlay, no model.

Protocol is fixed in `PREREGISTRATION_FORWARD.md`, written 2026-07-27 while
only 0.14 days of data existed. This file implements it and nothing else.

WHAT MAKES THIS DIFFERENT FROM EVERY OTHER TEST IN THIS PROJECT
---------------------------------------------------------------
Every historical result here priced options with a Black-76 overlay calibrated
to a single 2026-07-25 chain snapshot. `STUDY_2026-07-25.md` §14.6 records the
consequence, and it is asymmetric AGAINST the result: these strategies BUY
premium, so if real options were dearer than the model implies, expectancy is
overstated. Delta purges option history ~2 days after expiry, so no amount of
historical work can settle it.

Here there is no model at all. Premiums are the bid/ask actually quoted, read
from `research/book/*.jsonl.gz`. Fills are taker-side throughout: **enter at
the ask, exit at the bid**. If the edge survives that, the overlay caveat is
answered. If it does not, the caveat was the edge.

RUN ONCE. The data gate in `check_gate()` blocks execution until the recorded
window is large enough; do not lower it to get an earlier answer.
"""

from __future__ import annotations

import gzip
import json
import math
from collections import defaultdict
from pathlib import Path

import numpy as np

from research import costs, data, drift_control, search, sim, vec
from research.check_recorder import BOOK_DIR, report as recorder_report

# --- pre-registered constants. Changing these voids the pre-registration. ---
MIN_COVERAGE_DAYS = 30.0
MAX_OUTAGE_HOURS = 24.0
MAX_OUTAGE_FRACTION = 0.15
MIN_SNAPSHOTS = 6000
MIN_TRADES = 200
DTE_HOURS = 48.0                       # the 1-3d band
CANDIDATES = [
    {"family": "mean_reversion_bollinger", "timeframe": "5m",
     "params": {}, "gates": []},
    {"family": "mean_reversion_bollinger", "timeframe": "15m",
     "params": {}, "gates": []},
]

SNAP_TOLERANCE_S = 400                 # a 300s cadence, with slack for a miss


# ------------------------------------------------------------------ loading

def load_book(asset: str = "BTC") -> dict:
    """{ts: {symbol: quote}} from every recorded daily file."""
    snaps: dict[int, dict] = defaultdict(dict)
    for path in sorted(BOOK_DIR.glob(f"book_{asset}_*.jsonl.gz")):
        try:
            with gzip.open(path, "rt", encoding="utf-8") as fh:
                for line in fh:
                    try:
                        q = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    ts, sym = q.get("ts"), q.get("symbol")
                    if ts is None or not sym:
                        continue
                    if q.get("bid") is None or q.get("ask") is None:
                        continue          # one-sided quotes are not tradeable
                    snaps[int(ts)][sym] = q
        except OSError:
            continue
    return dict(snaps)


def check_gate(asset: str = "BTC") -> dict:
    """May the test run at all? Thresholds are pre-registered, not tunable."""
    r = recorder_report()
    if not r.get("snapshots"):
        return {"ok": False, "reason": r.get("reason", "no data")}

    span_s = max(r.get("span_days", 0.0) * 86400.0, 1.0)
    outage_fraction = 1.0 - (r.get("covered_days", 0.0) * 86400.0 / span_s)

    checks = [
        ("coverage >= 30d", r.get("covered_days", 0.0) >= MIN_COVERAGE_DAYS,
         f"{r.get('covered_days', 0.0):.2f} / {MIN_COVERAGE_DAYS:.0f} days"),
        ("largest outage <= 24h",
         r.get("largest_outage_hours", 0.0) <= MAX_OUTAGE_HOURS,
         f"{r.get('largest_outage_hours', 0.0):.1f}h"),
        ("total outage <= 15%", outage_fraction <= MAX_OUTAGE_FRACTION,
         f"{outage_fraction:.0%}"),
        ("snapshots >= 6000", r.get("snapshots", 0) >= MIN_SNAPSHOTS,
         f"{r.get('snapshots', 0):,}"),
    ]
    return {"ok": all(c[1] for c in checks), "checks": checks, "recorder": r}


# ------------------------------------------------- contract selection

def _parse_expiry_hours(q: dict, ts: int) -> float:
    return float(q.get("dte_hours") or 0.0)


def pick_contract(snap: dict, spot: float, target_dte_h: float, is_call: bool):
    """The ATM contract at the requested maturity, as actually quoted.

    Chooses the nearest available expiry first, then the strike nearest spot
    within it -- the same order the live OptionResolver uses. Returns None when
    the chain has nothing usable, which is a real condition (thin book), not an
    error to paper over.
    """
    kind = "C" if is_call else "P"
    best, best_key = None, None
    for sym, q in snap.items():
        if q.get("kind") != kind:
            continue
        dte = _parse_expiry_hours(q, 0)
        strike = q.get("strike")
        if not strike or dte <= 0:
            continue
        key = (abs(dte - target_dte_h), abs(float(strike) - spot))
        if best_key is None or key < best_key:
            best, best_key = sym, key
    return best


# ------------------------------------------------- series construction

def build_book_series(snaps: dict, bar_times: np.ndarray, spot: np.ndarray,
                      dte_hours: float, is_call: bool) -> dict:
    """Bid/ask/mid series for a rolling ATM contract, from recorded quotes.

    A contract is chosen at each roll boundary and then TRACKED by symbol, so
    the series follows one real instrument between rolls exactly as a held
    position would. Bars with no quote for that symbol are marked invalid
    rather than interpolated -- inventing a price is how phantom fills happen.
    """
    ts_sorted = np.array(sorted(snaps), dtype="int64")
    n = len(bar_times)
    bid = np.full(n, np.nan)
    ask = np.full(n, np.nan)
    roll_s = max(int(dte_hours * 3600 // 2), 1)

    cur_sym, cur_epoch = None, None
    roll_starts = []
    for i, t in enumerate(bar_times):
        if len(ts_sorted) == 0:
            break
        j = int(np.searchsorted(ts_sorted, t))
        cands = [k for k in (j - 1, j) if 0 <= k < len(ts_sorted)]
        if not cands:
            continue
        k = min(cands, key=lambda x: abs(int(ts_sorted[x]) - int(t)))
        snap_ts = int(ts_sorted[k])
        if abs(snap_ts - int(t)) > SNAP_TOLERANCE_S:
            continue                       # no contemporaneous book
        snap = snaps[snap_ts]

        epoch = int(t) // roll_s
        if epoch != cur_epoch:
            cur_epoch = epoch
            cur_sym = pick_contract(snap, float(spot[i]), dte_hours, is_call)
            roll_starts.append(i)
        if cur_sym is None or cur_sym not in snap:
            continue
        q = snap[cur_sym]
        b, a = q.get("bid"), q.get("ask")
        if b is None or a is None or a <= 0 or b <= 0 or a < b:
            continue
        bid[i], ask[i] = float(b), float(a)

    mid = (bid + ask) / 2.0
    return {"time": bar_times, "bid": bid, "ask": ask, "mid": mid,
            "roll_starts": np.array(roll_starts or [0], dtype="int64"),
            "valid": np.isfinite(mid)}


def _ohlc_from_mid(series: dict) -> dict:
    """sim works on OHLC. Mid drives stop/target; the spread is charged
    separately at fill time so it cannot be double counted."""
    mid = np.nan_to_num(series["mid"], nan=0.0)
    return {"time": series["time"], "open": mid, "high": mid, "low": mid,
            "close": mid, "volume": np.zeros(len(mid)),
            "roll_starts": series["roll_starts"]}


# ------------------------------------------------------------ evaluation

def _apply_taker_fills(trades: list, ce: dict, pe: dict) -> list:
    """Charge the real spread: buy at the ask, sell at the bid.

    sim simulated on mid, so each trade owes (ask-mid) entering and (mid-bid)
    exiting, both taken from the quotes recorded at those bars. Anything
    friendlier than this is a modelling favour and is forbidden by §5 of the
    pre-registration.
    """
    out = []
    for t in trades:
        src = ce if t.get("opt", "CE") == "CE" else pe
        i_in, i_out = t["entry_i"], t["exit_i"]
        if i_in >= len(src["ask"]) or i_out >= len(src["bid"]):
            continue
        a_in, m_in = src["ask"][i_in], src["mid"][i_in]
        b_out, m_out = src["bid"][i_out], src["mid"][i_out]
        if not all(np.isfinite(x) for x in (a_in, m_in, b_out, m_out)):
            continue
        slip = (a_in - m_in) + (m_out - b_out)
        stop = t["stop_d"]
        if stop <= 0:
            continue
        t = dict(t)
        t["spread_r"] = slip / stop
        t["r"] = t["r"] - t["spread_r"]
        out.append(t)
    return out


def run_candidate(cand: dict, snaps: dict) -> dict:
    tf = cand["timeframe"]
    d1 = data.fetch("BTCUSD", "1m", 365)
    d_tf = data.resample(d1, "1m", tf)

    ts_all = np.array(sorted(snaps), dtype="int64")
    if len(ts_all) < 2:
        return {"candidate": tf, "error": "no snapshots"}
    keep = (d_tf["time"] >= ts_all[0] - SNAP_TOLERANCE_S) & \
           (d_tf["time"] <= ts_all[-1] + SNAP_TOLERANCE_S)
    d_tf = {k: v[keep] for k, v in d_tf.items()}
    if len(d_tf["time"]) < 50:
        return {"candidate": tf, "error": "spot and book windows do not overlap"}

    ce = build_book_series(snaps, d_tf["time"], d_tf["close"], DTE_HOURS, True)
    pe = build_book_series(snaps, d_tf["time"], d_tf["close"], DTE_HOURS, False)

    ce_o, pe_o = _ohlc_from_mid(ce), _ohlc_from_mid(pe)
    params = dict(vec.DEFAULTS.get(cand["family"], {}).get("params", {}))
    params.update(cand["params"])
    c = {"family": cand["family"], "timeframe": tf, "params": params, "gates": []}
    sig = search.build_signals(c, d_tf, ce_o, None)
    sig = np.where(ce["valid"] & pe["valid"], sig, 0).astype("int8")

    rr = float(vec.DEFAULTS.get(cand["family"], {}).get("rr", 1.5))
    raw = search._trades(sig, ce_o, pe_o, d_tf["close"], rr, costs.OptionCost())
    trades = _apply_taker_fills(raw, ce, pe)

    n = len(trades)
    exp = float(np.mean([t["r"] for t in trades])) if trades else None
    bundle = {"spot": d_tf, "ce": ce_o, "pe": pe_o, "cost": costs.OptionCost(),
              "idx": {"dev": (0, len(d_tf["time"])),
                      "holdout": (0, len(d_tf["time"]))}}
    drift = drift_control.assess(sig, bundle, rr, "dev")

    passed = (n >= MIN_TRADES and exp is not None and exp > 0
              and bool(drift["beats_constant"]))
    return {"candidate": f"{cand['family']} {tf} 1-3d ungated",
            "bars": len(d_tf["time"]), "quoted_bars": int(ce["valid"].sum()),
            "n_trades": n, "expectancy": exp,
            "mean_spread_r": (float(np.mean([t["spread_r"] for t in trades]))
                              if trades else None),
            "c1_min_trades": n >= MIN_TRADES,
            "c2_positive": exp is not None and exp > 0,
            "c3_beats_constant": bool(drift["beats_constant"]),
            "passed": passed}


def main() -> dict:
    print("=" * 78)
    print("  FORWARD VALIDATION — recorded book quotes, no pricing model")
    print("=" * 78)

    gate = check_gate()
    print("\n  data gate (thresholds pre-registered, not tunable):")
    for name, ok, detail in gate.get("checks", []):
        print(f"    [{'PASS' if ok else 'FAIL'}] {name:24s} {detail}")

    if not gate["ok"]:
        r = gate.get("recorder", {})
        need = MIN_COVERAGE_DAYS - r.get("covered_days", 0.0)
        print(f"\n  GATE CLOSED — the test may not run yet.")
        if need > 0:
            print(f"  Need {need:.1f} more days of coverage. Keep the recorder")
            print("  running; check with `python -m research.check_recorder`.")
        print("\n  Do NOT lower these thresholds to get an earlier answer —")
        print("  they are fixed in PREREGISTRATION_FORWARD.md §4.")
        print("=" * 78)
        return {"ran": False, "gate": gate}

    snaps = load_book()
    print(f"\n  loaded {len(snaps):,} snapshots")
    results = [run_candidate(c, snaps) for c in CANDIDATES]

    print(f"\n  {'candidate':42s} {'trades':>7} {'exp':>9} {'spread':>8}  verdict")
    print("  " + "-" * 74)
    for r in results:
        if r.get("error"):
            print(f"  {r['candidate']:42s} {r['error']}")
            continue
        e = "-" if r["expectancy"] is None else f"{r['expectancy']:+.4f}"
        s = "-" if r["mean_spread_r"] is None else f"{r['mean_spread_r']:.4f}"
        print(f"  {r['candidate']:42s} {r['n_trades']:7d} {e:>9} {s:>8}  "
              f"{'PASS' if r['passed'] else 'FAIL'}")

    confirmed = any(r.get("passed") for r in results)
    print("\n" + "=" * 78)
    print(f"  FORWARD VALIDATION {'PASSED' if confirmed else 'FAILED'}")
    if not confirmed:
        print("  Per PREREGISTRATION_FORWARD.md §6: size remains zero")
        print("  permanently for this hypothesis. No re-slicing, no parameter")
        print("  adjustment, no 'collect more and retry'.")
    else:
        print("  Per §7 this is evidence, NOT a mandate. Next steps are a")
        print("  quarter of quarter-Kelly, then 30 days live paper through the")
        print("  real execution path, and only then capital.")
    print("=" * 78)
    return {"ran": True, "confirmed": confirmed, "results": results}


if __name__ == "__main__":
    main()

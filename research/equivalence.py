"""Prove the vectorized signal generators reproduce the REAL evaluate().

The grid search cannot call evaluate() 274 times over 129k bars, so it uses the
numpy replicas in vec.py. Those replicas are only trustworthy if they emit the
same signal, on the same bar, as the production strategy classes.

This walks a random sample of bars, builds the exact rolling window the live
engine would pass (`lookback` closed candles), calls the real evaluate(), and
compares against the replica's value at that index. Any mismatch invalidates the
study and is reported loudly rather than swallowed.
"""

from __future__ import annotations

import sys

import numpy as np

sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent.parent))

from strategy.base import Context                      # noqa: E402
from strategy import zing_strategies as zs             # noqa: E402
from research import vec                               # noqa: E402

CLASSES = {
    "ema_cross": zs.EMACross,
    "scalping_pulse": zs.ScalpingPulse,
    "traffic_light": zs.TrafficLight,
    "inside_candle": zs.InsideCandle,
    "mean_reversion_bollinger": zs.MeanReversionBollinger,
    "prime_scalper_ema": zs.PrimeScalperEMA,
    "swingking_sniper": zs.SwingKingSniper,
}


def to_candles(d: dict, lo: int, hi: int) -> list[dict]:
    return [{"time": int(d["time"][k]), "open": float(d["open"][k]),
             "high": float(d["high"][k]), "low": float(d["low"][k]),
             "close": float(d["close"][k]), "volume": float(d["volume"][k])}
            for k in range(lo, hi)]


def check(slug: str, d: dict, samples: int = 1200, seed: int = 7) -> dict:
    cls = CLASSES[slug]
    strat = cls()
    lookback = strat.lookback
    gen = vec.GENERATORS[slug]
    params = vec.DEFAULTS[slug]["params"]
    vsig = gen(d, **params)

    n = len(d["close"])
    rng = np.random.default_rng(seed)

    # Sample every bar the replica fires on (up to `samples`), plus an equal
    # number of quiet bars — testing only firing bars would miss false positives.
    fire = np.flatnonzero(vsig != 0)
    fire = fire[fire > lookback + 5]
    quiet = np.flatnonzero(vsig == 0)
    quiet = quiet[quiet > lookback + 5]
    take_f = rng.choice(fire, size=min(samples // 2, len(fire)), replace=False) if len(fire) else np.array([], int)
    take_q = rng.choice(quiet, size=min(samples // 2, len(quiet)), replace=False) if len(quiet) else np.array([], int)
    idx = np.sort(np.concatenate([take_f, take_q]).astype(int))

    agree = 0
    mismatches = []
    for i in idx:
        window = to_candles(d, max(0, i - lookback + 1), i + 1)
        try:
            sigs = strat.evaluate(Context(underlying=window, spot=window[-1]["close"]))
        except Exception as exc:  # noqa: BLE001
            mismatches.append({"i": int(i), "error": repr(exc)})
            continue
        real = 0
        if sigs:
            real = 1 if sigs[0].direction == "CE" else -1
        if real == int(vsig[i]):
            agree += 1
        elif len(mismatches) < 12:
            mismatches.append({"i": int(i), "real": real, "vec": int(vsig[i])})

    return {
        "slug": slug,
        "sampled": len(idx),
        "fire_bars_sampled": int(len(take_f)),
        "agree": agree,
        "agreement_pct": round(100.0 * agree / len(idx), 3) if len(idx) else None,
        "vec_signal_count": int((vsig != 0).sum()),
        "mismatches": mismatches,
    }


if __name__ == "__main__":
    from research import data as dl

    d1 = dl.fetch("BTCUSD", "1m", 90)
    d5 = dl.fetch("BTCUSD", "5m", 90)

    print("EQUIVALENCE: vectorized replica vs production evaluate()")
    print("-" * 78)
    print("{:<28} {:>8} {:>8} {:>10} {:>10}".format(
        "strategy", "sampled", "agree", "agree%", "vec sigs"))
    print("-" * 78)
    worst = 100.0
    for slug in CLASSES:
        d = d5 if vec.DEFAULTS[slug]["tf"] == "5m" else d1
        r = check(slug, d)
        worst = min(worst, r["agreement_pct"] if r["agreement_pct"] is not None else 0)
        print("{:<28} {:>8} {:>8} {:>9.2f}% {:>10}".format(
            r["slug"], r["sampled"], r["agree"], r["agreement_pct"], r["vec_signal_count"]))
        for m in r["mismatches"][:4]:
            print("      mismatch:", m)
    print("-" * 78)
    print("worst agreement: {:.2f}%".format(worst))
    print("VERDICT:", "REPLICAS VALID" if worst >= 99.0 else "REPLICAS INVALID - DO NOT PROCEED")

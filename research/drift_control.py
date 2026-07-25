"""Constant-direction control: does the signal beat plain directional exposure?

WHY THIS EXISTS
---------------
BTC fell 44.5% across the study window (dev -29.7%, holdout -21.0%). Both
option directions are LONG premium -- a buy signal buys a call, a sell signal
buys a put -- so a strategy with a persistent PUT bias makes money from the
decline whether or not its signal carries information.

The direction-shuffle null does NOT control for this. Shuffling directions at
the same bars yields roughly 50/50 calls and puts, so in a strongly trending
market ANY consistent directional bias beats the shuffle. Passing that test
can mean "you were short while BTC dropped", not "your entries are skillful".

This control answers the question the shuffle cannot: take the SAME entry bars
and force every trade to the strategy's own majority direction. If the real
signal cannot beat that, the signal is adding nothing to plain directional
exposure and the apparent edge is beta, not alpha.
"""

from __future__ import annotations

import numpy as np

from research import search


def direction_mix(trades: list) -> dict:
    """Call/put split of a trade list. 'CE' is a long call, 'PE' a long put.

    Reads `opt`, NOT sim's `dir`. Both legs are simulated as long premium, so
    `dir` is "CE" for every trade including long puts -- using it reported a
    100% call mix for strategies that were in fact trading both sides.
    """
    if not trades:
        return {"n": 0, "ce": 0, "pe": 0, "ce_frac": None, "majority": None}
    ce = sum(1 for t in trades if t.get("opt") == "CE")
    pe = len(trades) - ce
    return {"n": len(trades), "ce": ce, "pe": pe,
            "ce_frac": ce / len(trades),
            "majority": "CE" if ce >= pe else "PE"}


def constant_direction_expectancy(sig, bundle, a, b, rr, direction: int):
    """Expectancy when every entry bar is forced to one direction."""
    forced = np.where(sig != 0, direction, 0).astype("int8")
    tr = search._trades(forced,
                        search._slice(bundle["ce"], a, b),
                        search._slice(bundle["pe"], a, b),
                        bundle["spot"]["close"][a:b], rr, bundle["cost"])
    return search._exp(tr), len(tr)


def assess(sig, bundle, rr, region="dev") -> dict:
    """Compare the real signal against forced-long and forced-short variants.

    `beats_constant` is the question that matters: does choosing direction
    per-bar beat committing to one direction on the same bars?
    """
    a, b = bundle["idx"]["dev"] if region == "dev" else bundle["idx"]["holdout"]
    real_tr = search._trades(sig[a:b],
                             search._slice(bundle["ce"], a, b),
                             search._slice(bundle["pe"], a, b),
                             bundle["spot"]["close"][a:b], rr, bundle["cost"])
    real = search._exp(real_tr)
    mix = direction_mix(real_tr)

    long_e, long_n = constant_direction_expectancy(sig[a:b], bundle, a, b, rr, 1)
    short_e, short_n = constant_direction_expectancy(sig[a:b], bundle, a, b, rr, -1)

    best_const = max([x for x in (long_e, short_e) if x is not None], default=None)
    return {
        "region": region,
        "real_expectancy": real,
        "always_call_expectancy": long_e, "always_call_trades": long_n,
        "always_put_expectancy": short_e, "always_put_trades": short_n,
        "best_constant": best_const,
        "direction_mix": mix,
        "beats_constant": (real is not None and best_const is not None
                           and real > best_const),
        "edge_over_constant": (None if (real is None or best_const is None)
                               else real - best_const),
    }

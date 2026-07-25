"""Position sizing derived from MEASURED edge.

The app currently sizes from a hand-set confidence table: >80 confidence maps
to 100% of max lot at 20x leverage. Confidence is a guess at edge, so that
table is a guess compounded by leverage.

This replaces it with fractional Kelly on the strategy's own validated
out-of-sample trade distribution, hard-capped by a drawdown-derived limit.

STATED LIMIT: Kelly on an ESTIMATED edge is fragile -- the estimate carries all
the uncertainty of the backtest, and in this project it additionally carries
the uncertainty of an option overlay that passed its credibility gate at
exactly the threshold. Quarter-Kelly is the standard defensive discount, not a
safety guarantee.
"""

from __future__ import annotations

import numpy as np

MIN_TRADES_FOR_KELLY = 30
R_KEY = "r"


def kelly_fraction(trades: list, fraction: float = 0.25) -> float:
    """f* = mean / variance of per-trade R, scaled by `fraction`.

    Returns 0.0 for a non-positive edge or too few trades: there is no
    fraction of a losing edge worth betting.
    """
    if len(trades) < MIN_TRADES_FOR_KELLY:
        return 0.0
    r = np.array([t[R_KEY] for t in trades], dtype="float64")
    r = r[np.isfinite(r)]
    if len(r) < MIN_TRADES_FOR_KELLY:
        return 0.0
    mu, var = float(r.mean()), float(r.var(ddof=1))
    if mu <= 0 or var <= 0:
        return 0.0
    return float(max(0.0, (mu / var) * fraction))


def drawdown_derived_cap(equity_curve, pct_of_equity: float = 0.20,
                         quantile: float = 0.95) -> float:
    """Largest position fraction whose q-quantile drawdown stays within
    `pct_of_equity` of the account."""
    e = np.asarray(equity_curve, dtype="float64")
    if len(e) < 2:
        return 0.0
    peak = np.maximum.accumulate(e)
    dd = np.where(peak > 0, (peak - e) / peak, 0.0)
    q = float(np.quantile(dd, quantile))
    if q <= 0:
        return 1.0
    return float(min(1.0, max(0.0, pct_of_equity / q)))


def equity_curve(trades: list, start: float = 1.0) -> np.ndarray:
    """Cumulative R curve, for feeding drawdown_derived_cap()."""
    if not trades:
        return np.array([start])
    r = np.array([t[R_KEY] for t in trades], dtype="float64")
    r = r[np.isfinite(r)]
    return start + np.concatenate([[0.0], np.cumsum(r)])


def recommend(trades: list, equity, validated: bool) -> dict:
    """Sizing recommendation.

    An unvalidated strategy gets ZERO by construction -- the guarantee is
    structural, not a matter of discipline.
    """
    if not validated:
        return {"kelly_f": 0.0, "applied_f": 0.0, "max_leverage": 0.0,
                "max_lot_pct": 0.0,
                "rationale": "Strategy did not clear the gauntlet; it is not "
                             "validated, so size is zero by construction."}

    kf = kelly_fraction(trades, fraction=1.0)
    quarter = kf * 0.25
    cap = drawdown_derived_cap(equity)
    applied = min(quarter, cap)
    return {
        "kelly_f": kf,
        "quarter_kelly": quarter,
        "drawdown_cap": cap,
        "applied_f": applied,
        "max_leverage": round(min(5.0, 1.0 + applied * 10.0), 2),
        "max_lot_pct": round(applied * 100.0, 2),
        "rationale": (f"quarter-Kelly {quarter:.4f} capped by the 95th-percentile "
                      f"drawdown limit {cap:.4f}; applied {applied:.4f}"),
    }

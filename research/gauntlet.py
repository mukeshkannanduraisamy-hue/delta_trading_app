"""The acceptance bar, fixed BEFORE the search.

Criteria are objects in an ordered list rather than an if-chain, so a new
criterion is added by appending a class and the order (cheapest-first) can
change without touching the driver.

Evaluation SHORT-CIRCUITS at the first failure. That is both a compute saving
and what makes the near-miss report meaningful: every candidate records exactly
which criterion killed it.

The bar is NOT relaxed because the overlay passed its own gate narrowly. If
anything a marginal overlay argues for keeping this strict, since overlay error
is one more reason a borderline result could be noise.
"""

from __future__ import annotations

import numpy as np

# Trade-level R is stored under "r" by sim.simulate().
R_KEY = "r"


def _expectancy(trades) -> float | None:
    if not trades:
        return None
    vals = [t[R_KEY] for t in trades if np.isfinite(t.get(R_KEY, np.nan))]
    return float(np.mean(vals)) if vals else None


class MinTrades:
    """Statistical-validity precondition, not an extra hurdle: below this the
    t-test has no power, so a 'pass' would be noise whatever it said."""
    name = "min_trades"

    def __init__(self, n: int = 200):
        self.n = n

    def check(self, ctx: dict):
        got = len(ctx.get("trades") or [])
        return got >= self.n, {"n": got, "required": self.n}


class PositiveExpectancy:
    name = "positive_expectancy"

    def check(self, ctx: dict):
        e = _expectancy(ctx.get("trades") or [])
        return (e is not None and e > 0), {"expectancy": e}


class WalkForward:
    name = "walk_forward"

    def __init__(self, min_positive: int = 3, windows: int = 4):
        self.min_positive = min_positive
        self.windows = windows

    def check(self, ctx: dict):
        wf = ctx.get("walk_forward") or []
        pos = sum(1 for x in wf if x is not None and x > 0)
        return pos >= self.min_positive, {
            "positive": pos, "windows": len(wf),
            "required": self.min_positive, "values": wf}


class ShuffleControl:
    """Must beat its own direction-shuffled null."""
    name = "shuffle_control"

    def __init__(self, p: float = 0.05):
        self.p = p

    def check(self, ctx: dict):
        got = ctx.get("shuffle_p")
        return (got is not None and got < self.p), {
            "shuffle_p": got, "threshold": self.p}


class CorrectedSignificance:
    """t-test corrected for the FULL declared search budget.

    The correction must cover every combo tried, including those discarded.
    Searching thousands and correcting as if a handful were searched is the
    easiest way to manufacture a winner.
    """
    name = "corrected_significance"

    def __init__(self, p: float = 0.05):
        self.p = p

    def check(self, ctx: dict):
        got = ctx.get("corrected_p")
        return (got is not None and got < self.p), {
            "corrected_p": got, "threshold": self.p,
            "search_budget": ctx.get("search_budget")}


class OverlayBand:
    """Must stay positive across the overlay's measured error band, not just
    at the point estimate. Matters more here because the overlay passed its
    own credibility gate at exactly the threshold."""
    name = "overlay_band"

    def check(self, ctx: dict):
        vals = [v for v in (ctx.get("band_results") or []) if v is not None]
        if not vals:
            return False, {"band_results": []}
        return all(v > 0 for v in vals), {
            "band_results": vals, "min": float(min(vals))}


class FinalHoldout:
    """The untouched tail of the data, evaluated exactly once."""
    name = "final_holdout"

    def check(self, ctx: dict):
        e = ctx.get("holdout_expectancy")
        return (e is not None and e > 0), {"holdout_expectancy": e}


DEFAULT_CRITERIA = [
    MinTrades(200),
    PositiveExpectancy(),
    WalkForward(min_positive=3, windows=4),
    ShuffleControl(p=0.05),
    CorrectedSignificance(p=0.05),
    OverlayBand(),
    FinalHoldout(),
]

ORDER = [c.name for c in DEFAULT_CRITERIA]


def run(ctx: dict, criteria: list | None = None) -> dict:
    """Short-circuit at the first failure; record which one it was."""
    crits = DEFAULT_CRITERIA if criteria is None else criteria
    details = {}
    for c in crits:
        ok, info = c.check(ctx)
        details[c.name] = {"passed": ok, **info}
        if not ok:
            return {"passed": False, "failed_at": c.name,
                    "reached": ORDER.index(c.name) if c.name in ORDER else -1,
                    "details": details}
    return {"passed": True, "failed_at": None, "reached": len(crits),
            "details": details}

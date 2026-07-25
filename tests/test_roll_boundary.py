import numpy as np
import pytest

from research import costs, search


def _series(n, roll_starts, level=100.0):
    """A premium series that JUMPS at every roll boundary."""
    c = np.full(n, level, dtype="float64")
    for j, s in enumerate(roll_starts):
        if s > 0:
            c[s:] = level * (10.0 ** (j + 1))      # violent splice
    return {"time": np.arange(0, n * 3600, 3600, dtype="int64"),
            "open": c, "high": c * 1.01, "low": c * 0.99, "close": c,
            "volume": np.zeros(n),
            "roll_starts": np.array(roll_starts, dtype="int64")}


def test_segment_bounds_splits_at_every_roll():
    s = _series(100, [0, 25, 50, 75])
    assert search._segment_bounds(s, 100) == [(0, 25), (25, 50), (50, 75), (75, 100)]


def test_segment_bounds_handles_missing_roll_starts():
    assert search._segment_bounds({"close": np.zeros(10)}, 10) == [(0, 10)]


def test_segment_bounds_always_starts_at_zero():
    s = {"roll_starts": np.array([30, 60])}
    assert search._segment_bounds(s, 90)[0][0] == 0


def test_no_trade_crosses_a_roll_boundary():
    """The bug that manufactured +1.07 R/trade at p=1e-83: a trade held
    across a contract roll books the splice as if it were a price move."""
    n = 120
    rolls = [0, 30, 60, 90]
    ce = _series(n, rolls)
    pe = _series(n, rolls)
    sig = np.zeros(n, dtype="int8")
    sig[[5, 20, 28, 35, 58, 85]] = 1          # some entries sit right before a roll
    spot = np.full(n, 64000.0)

    trades = search._trades(sig, ce, pe, spot, 1.5, costs.OptionCost())
    assert trades, "fixture must produce trades"
    for t in trades:
        seg = max(r for r in rolls if r <= t["entry_i"])
        nxt = min([r for r in rolls if r > t["entry_i"]] + [n])
        assert seg <= t["exit_i"] <= nxt, (
            f"trade entered at {t['entry_i']} exited at {t['exit_i']}, "
            f"crossing the roll at {nxt}")


def test_splice_jump_cannot_produce_profit():
    """With a violent 10x splice at each roll, confining trades to one
    contract must keep returns sane."""
    n = 120
    rolls = [0, 30, 60, 90]
    ce = _series(n, rolls)
    sig = np.zeros(n, dtype="int8")
    sig[[25, 28, 55, 58, 85]] = 1             # deliberately just before rolls
    spot = np.full(n, 64000.0)
    trades = search._trades(sig, ce, ce, spot, 1.5, costs.OptionCost())
    for t in trades:
        assert t["r"] < 5.0, f"splice leaked into P&L: r={t['r']}"


def test_slice_rebases_roll_starts_instead_of_slicing_them():
    """roll_starts holds INDICES; slicing it positionally would corrupt the
    segment boundaries."""
    s = _series(100, [0, 25, 50, 75])
    out = search._slice(s, 20, 80)
    assert len(out["close"]) == 60
    # rolls at 25, 50 and 75 all fall inside [20, 80) -> 5, 30, 55
    assert list(out["roll_starts"]) == [0, 5, 30, 55], \
        "boundaries must be re-based to the slice origin"


def test_slice_keeps_a_leading_zero_boundary():
    s = _series(100, [0, 25, 50, 75])
    assert search._slice(s, 30, 45)["roll_starts"][0] == 0

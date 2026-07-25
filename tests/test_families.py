import numpy as np
import pytest

from research import families


def _d(close, volume=None, start=0):
    close = np.asarray(close, dtype="float64")
    n = len(close)
    return {"time": np.arange(start, start + n * 60, 60, dtype="int64"),
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": (np.ones(n) if volume is None
                                       else np.asarray(volume, dtype="float64"))}


# ---------------- momentum persistence ----------------

def test_momentum_persistence_fires_long_on_sustained_uptrend():
    d = _d(np.arange(100, dtype="float64") + 1000.0)
    assert families.momentum_persistence(d, 12, 3)[-1] == 1


def test_momentum_persistence_fires_short_on_sustained_downtrend():
    d = _d(2000.0 - np.arange(100, dtype="float64"))
    assert families.momentum_persistence(d, 12, 3)[-1] == -1


def test_momentum_persistence_silent_on_flat():
    d = _d(np.full(100, 1000.0))
    assert np.all(families.momentum_persistence(d, 12, 3) == 0)


def test_momentum_persistence_has_no_lookahead():
    """Changing a FUTURE bar must not change a PAST signal."""
    c = np.r_[np.arange(60, dtype="float64"),
              np.arange(60, 120)[::-1].astype("float64")]
    base = families.momentum_persistence(_d(c), 12, 3)
    c2 = c.copy()
    c2[80:] += 500.0
    assert np.array_equal(base[:80], families.momentum_persistence(_d(c2), 12, 3)[:80])


def test_momentum_persistence_warmup_is_silent():
    d = _d(np.arange(100, dtype="float64") + 1000.0)
    assert np.all(families.momentum_persistence(d, 12, 3)[:15] == 0)


# ---------------- breakout + volume ----------------

def test_breakout_requires_volume_confirmation():
    c = np.r_[np.full(40, 1000.0), np.full(10, 1050.0)]
    rng = np.random.default_rng(11)
    # baseline volume must vary, or the rolling std is exactly 0 and no
    # z-score is defined for any bar
    base = rng.uniform(0.9, 1.1, 50)

    quiet = families.breakout_volume(_d(c, volume=base.copy()), 20, 1.5)
    loud_v = base.copy()
    loud_v[40:] = 50.0
    loud = families.breakout_volume(_d(c, volume=loud_v), 20, 1.5)

    assert np.count_nonzero(loud) > 0, "a price break on huge volume must fire"
    assert np.count_nonzero(loud) > np.count_nonzero(quiet)


def test_breakout_has_no_lookahead():
    rng = np.random.default_rng(2)
    c = 1000.0 + np.cumsum(rng.normal(0, 2, 300))
    v = rng.uniform(1, 10, 300)
    base = families.breakout_volume(_d(c, v), 20, 1.0)
    c2, v2 = c.copy(), v.copy()
    c2[200:] += 300.0
    v2[200:] *= 10
    mod = families.breakout_volume(_d(c2, v2), 20, 1.0)
    assert np.array_equal(base[:200], mod[:200])


def test_rolling_max_uses_strictly_prior_bars():
    """The channel must not include the bar being tested, or every new high
    trivially fails to exceed its own window."""
    a = np.array([1.0, 2, 3, 10, 4, 5], dtype="float64")
    got = families._roll_max(a, 3)
    assert got[3] == 3.0, "window at i=3 must cover bars 0..2, excluding bar 3"


# ---------------- gates ----------------

def test_vol_regime_gate_excludes_extremes():
    rng = np.random.default_rng(3)
    c = 1000.0 + np.cumsum(rng.normal(0, 1, 4000))
    g = families.vol_regime_gate(_d(c), 14, window=500)
    frac = g[600:].mean()
    assert 0.3 < frac < 0.95, f"middle band should admit most but not all, got {frac:.2f}"


def test_vol_regime_gate_is_false_during_warmup():
    rng = np.random.default_rng(3)
    c = 1000.0 + np.cumsum(rng.normal(0, 1, 2000))
    assert not families.vol_regime_gate(_d(c), 14, window=500)[:100].any()


def test_session_gate_matches_utc_hours():
    d = _d(np.full(1440, 1000.0), start=0)
    g = families.session_gate(d, hours=(13, 14))
    hrs = (d["time"] // 3600) % 24
    assert np.array_equal(g, np.isin(hrs, (13, 14)))


def test_vrp_gate_blocks_when_iv_far_above_rv():
    g = families.vrp_gate(np.array([1.0, 1.0, 1.0]),
                          np.array([0.9, 0.5, 0.2]), max_ratio=1.5)
    assert g[0] and not g[2]


def test_htf_trend_gate_requires_alignment():
    up = np.arange(400, dtype="float64") + 1000.0
    d = _d(up)
    assert families.htf_trend_gate(d, up, 20, 50)[-1]
    assert not families.htf_trend_gate(d, up[::-1].copy(), 20, 50)[-1]


def test_htf_trend_gate_rejects_misaligned_input():
    d = _d(np.arange(100, dtype="float64"))
    with pytest.raises(ValueError):
        families.htf_trend_gate(d, np.arange(50, dtype="float64"))


# ---------------- gate composition ----------------

def test_apply_gates_is_an_and_of_all_masks():
    sig = np.array([1, -1, 1, -1], dtype="int8")
    out = families.apply_gates(sig,
                               np.array([True, True, False, False]),
                               np.array([True, False, True, False]))
    assert np.array_equal(out, np.array([1, 0, 0, 0], dtype="int8"))


def test_apply_gates_never_creates_a_signal():
    out = families.apply_gates(np.zeros(4, dtype="int8"), np.ones(4, dtype=bool))
    assert np.all(out == 0)


def test_apply_gates_rejects_length_mismatch():
    with pytest.raises(ValueError):
        families.apply_gates(np.zeros(4, dtype="int8"), np.ones(3, dtype=bool))


def test_gates_only_reduce_trade_count():
    rng = np.random.default_rng(9)
    c = 1000.0 + np.cumsum(rng.normal(0, 2, 3000))
    d = _d(c)
    sig = families.momentum_persistence(d, 12, 3)
    gated = families.apply_gates(sig, families.vol_regime_gate(d, 14, window=500))
    assert np.count_nonzero(gated) <= np.count_nonzero(sig)

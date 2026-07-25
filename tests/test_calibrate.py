import numpy as np
import pytest

from research import calibrate, costs


def _snap(dte_hours, iv, moneyness=1.0, spread=0.02):
    return {"dte_hours": dte_hours, "mark_iv": iv, "moneyness": moneyness,
            "spread_pct": spread, "mid": 100.0}


# ---------------- IV term structure ----------------

def test_fit_iv_recovers_term_structure_from_snapshot():
    """The one thing a single snapshot DOES vary in is DTE, so that is what
    the model may fit. Measured 2026-07-25: ~0.14 at 1-8h rising to ~0.34 at
    7d+."""
    snaps = ([_snap(4, 0.14)] * 6 + [_snap(40, 0.18)] * 12
             + [_snap(120, 0.32)] * 6 + [_snap(300, 0.34)] * 8)
    m, report = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40)
    assert m.iv(dte_hours=4, rv=0.40) == pytest.approx(0.14, abs=0.02)
    assert m.iv(dte_hours=300, rv=0.40) == pytest.approx(0.34, abs=0.02)
    assert report["n"] == 32


def test_regime_scaling_is_off_by_default():
    """Scaling IV with realized vol was an assumption, never a measurement.
    Validation against trade prints found it was the dominant residual error
    (pass 8/22 -> 11/22 with it off), so it must not be on unless asked for."""
    snaps = [_snap(24, 0.20)] * 40
    m, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40)
    assert m.regime_scaling is False
    assert m.iv(dte_hours=24, rv=0.40) == pytest.approx(m.iv(dte_hours=24, rv=1.60))


def test_iv_scales_with_rv_regime_when_explicitly_enabled():
    snaps = [_snap(24, 0.20)] * 40
    m, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40)
    m.regime_scaling = True
    base = m.iv(dte_hours=24, rv=0.40)
    hot = m.iv(dte_hours=24, rv=0.80)
    assert hot > base
    assert hot == pytest.approx(base * 2.0, rel=0.01)


def test_iv_is_clamped_to_floor_and_cap():
    snaps = [_snap(24, 0.20)] * 40
    m, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40,
                                 floor=0.10, cap=1.50)
    m.regime_scaling = True
    assert m.iv(dte_hours=24, rv=0.0001) == 0.10
    assert m.iv(dte_hours=24, rv=99.0) == 1.50


def test_fit_iv_term_refuses_insufficient_data():
    """Parsimony: do not fit a model the data cannot support."""
    with pytest.raises(ValueError):
        calibrate.fit_iv_term([_snap(24, 0.2)] * 3, rv_at_calibration=0.4)


def test_fit_iv_term_interpolates_between_observed_buckets():
    snaps = [_snap(4, 0.10)] * 20 + [_snap(300, 0.30)] * 20
    m, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40)
    mid = m.iv(dte_hours=100, rv=0.40)
    assert 0.10 < mid < 0.30, "an unobserved DTE must interpolate, not jump"


def test_iv_array_matches_scalar_iv():
    snaps = [_snap(4, 0.10)] * 20 + [_snap(300, 0.30)] * 20
    m, _ = calibrate.fit_iv_term(snaps, rv_at_calibration=0.40)
    dte = np.array([4.0, 50.0, 300.0])
    rv = np.array([0.40, 0.40, 0.40])
    got = m.iv_array(dte, rv)
    want = [m.iv(d, r) for d, r in zip(dte, rv)]
    assert np.allclose(got, want)


# ---------------- spread ----------------

def test_spread_model_uses_bucket_median():
    snaps = [_snap(24, 0.2, spread=0.01) for _ in range(20)]
    m = calibrate.fit_spread(snaps)
    assert m.spread_pct(1.0, 24.0) == pytest.approx(0.01)


def test_spread_model_falls_back_for_unseen_bucket():
    m = calibrate.fit_spread([_snap(24, 0.2, spread=0.02)] * 20)
    assert m.spread_pct(moneyness=0.5, dte_hours=9999.0) > 0


def test_spread_model_is_pessimistic_on_thin_evidence():
    """A bucket with few samples must not look CHEAPER than the book overall."""
    snaps = [_snap(24, 0.2, spread=0.30)] * 50 + [_snap(24, 0.2, 2.0, spread=0.001)]
    m = calibrate.fit_spread(snaps)
    assert m.spread_pct(2.0, 24.0) >= 0.5 * 0.30


# ---------------- overlay ----------------

def test_overlay_reproduces_black_scholes():
    m = calibrate.FlatIV(0.55)
    ov = calibrate.Overlay(m, calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    expiry = 1_800_000_000
    times = np.array([expiry - 86400], dtype="int64")
    got = ov.premium_path(np.array([64000.0]), 64000.0, expiry, times, True,
                          rv=np.array([0.55]))
    want = costs.bs_price(64000.0, 64000.0, 1.0 / 365.0, 0.55, True)
    # vectorized pricing uses an inlined erf approximation; the residual is
    # ~0.004 here, well inside the 0.1 tick these options quote in
    assert got[0] == pytest.approx(want, abs=0.02)


def test_overlay_decays_to_intrinsic_at_expiry():
    ov = calibrate.Overlay(calibrate.FlatIV(0.55),
                           calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    expiry = 1_800_000_000
    got = ov.premium_path(np.array([65000.0]), 64000.0, expiry,
                          np.array([expiry], dtype="int64"), True,
                          rv=np.array([0.55]))
    assert got[0] == pytest.approx(1000.0, abs=1e-6)


def test_overlay_bid_is_below_ask():
    ov = calibrate.Overlay(calibrate.FlatIV(0.55),
                           calibrate.SpreadModel({}, 0.10), costs.OptionCost())
    expiry = 1_800_000_000
    times = np.array([expiry - 86400], dtype="int64")
    mid = np.array([100.0])
    bid, ask = ov.bid_ask(mid, 64000.0, expiry, times, np.array([64000.0]))
    assert bid[0] < mid[0] < ask[0]
    assert ask[0] - bid[0] == pytest.approx(10.0)


# ---------------- the credibility gate ----------------

def _fixture(premium_level):
    n = 600
    expiry = 1_800_000_000
    times = np.arange(expiry - n * 60, expiry, 60, dtype="int64")
    spot = np.full(n, 64000.0)
    mark = np.full(n, premium_level)
    return spot, mark, times, 64000.0, expiry, True


def test_validate_overlay_reports_failure_rather_than_raising():
    ov = calibrate.Overlay(calibrate.FlatIV(0.01),
                           calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    rep = calibrate.validate_overlay(ov, symbol=None, _fake=_fixture(5000.0))
    assert rep["passed"] is False
    assert "mae_pct" in rep


def test_error_metric_is_stable_as_premium_decays_to_zero():
    """A fixed absolute error must not blow up just because the option is
    cheap. Dividing by the per-bar premium made near-expiry contracts score
    5x worse (0.338 vs 0.069) while their R2 on changes stayed above 0.85 --
    the metric was wrong, not the model."""
    n = 400
    expiry = 1_800_000_000
    times = np.arange(expiry - n * 60, expiry, 60, dtype="int64")
    spot = np.full(n, 64000.0)

    # a contract whose premium decays from 200 toward ~0
    mark = np.linspace(200.0, 0.5, n)
    pred = mark + 2.0                      # constant 2.0 absolute error

    rep_decay = calibrate._verdict("decaying", mark, pred)
    # a contract that stays expensive, same absolute error
    flat = np.full(n, 200.0)
    rep_flat = calibrate._verdict("flat", flat, flat + 2.0)

    assert rep_decay["mae_pct"] < 0.10, (
        "a constant 2.0 error on a decaying option must stay a small "
        f"fraction of its typical premium, got {rep_decay['mae_pct']:.3f}")
    assert rep_decay["mae_pct"] < 5 * rep_flat["mae_pct"]


def test_validate_overlay_reports_the_limits_it_applied():
    ov = calibrate.Overlay(calibrate.FlatIV(0.55),
                           calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    rep = calibrate.validate_overlay(ov, symbol=None, _fake=_fixture(300.0))
    assert "mae_limit" in rep and "r2_limit" in rep


# ---------------- forward carry ----------------

def _pair(K, spot, dte_hours, carry, expiry_tok="270726"):
    """A call/put pair consistent with F = S*exp(carry*T), via parity."""
    import math as _m
    from research import costs as _c
    T = dte_hours / 24.0 / 365.0
    F = spot * _m.exp(carry * T)
    call = _c.black76_price(F, K, T, 0.5, True)
    put = _c.black76_price(F, K, T, 0.5, False)
    common = {"strike": K, "spot": spot, "dte_hours": dte_hours,
              "moneyness": K / spot, "spread_pct": 0.02}
    return [{**common, "kind": "C", "mid": call, "symbol": f"C-BTC-{K:.0f}-{expiry_tok}"},
            {**common, "kind": "P", "mid": put, "symbol": f"P-BTC-{K:.0f}-{expiry_tok}"}]


def test_fit_carry_recovers_a_known_rate():
    snaps = []
    for i, dte in enumerate((24.0, 120.0, 480.0, 1400.0)):
        for K in (63800.0, 64000.0, 64200.0):
            snaps += _pair(K, 64000.0, dte, 0.05, expiry_tok=f"{10+i:02d}0826")
    carry, rep = calibrate.fit_carry(snaps)
    assert carry == pytest.approx(0.05, abs=0.005)
    assert rep["n_pairs"] == 12


def test_fit_carry_ignores_unreliable_far_otm_pairs():
    """Deep OTM legs are worth a few dollars and quote unreliably; letting
    them set the forward returned 0.687/yr where the truth was ~0.05."""
    good = []
    for i, dte in enumerate((24.0, 120.0, 480.0, 1400.0)):
        for K in (63800.0, 63900.0, 64000.0, 64100.0):
            good += _pair(K, 64000.0, dte, 0.05, expiry_tok=f"{10+i:02d}0826")
    junk = []
    for K in (40000.0, 90000.0, 120000.0):
        junk += _pair(K, 64000.0, 24.0, 5.0, expiry_tok="990826")  # absurd rate
    carry, _ = calibrate.fit_carry(good + junk)
    assert carry == pytest.approx(0.05, abs=0.01), "far-OTM junk must not move the forward"


def test_fit_carry_returns_zero_without_enough_pairs():
    carry, rep = calibrate.fit_carry(_pair(64000.0, 64000.0, 24.0, 0.05))
    assert carry == 0.0
    assert "too few" in rep["note"]


# ---------------- volatility smile ----------------

def _otm_snap(K, spot, dte_hours, iv, carry=0.05):
    """A quote whose OTM leg is the one carrying vol information."""
    import math as _m
    T = dte_hours / 24.0 / 365.0
    F = spot * _m.exp(carry * T)
    kind = "P" if K < F else "C"
    return {"symbol": f"{kind}-BTC-{K:.0f}-010826", "kind": kind, "strike": K,
            "spot": spot, "dte_hours": dte_hours, "moneyness": K / spot,
            "mark_iv": iv, "spread_pct": 0.02, "mid": 100.0}


def test_otm_leg_filter_keeps_only_the_out_of_the_money_side():
    """Deep ITM quotes carry no vol information -- their vega is ~0 and their
    quoted IV is numerically unstable."""
    spot = 64000.0
    snaps = [_otm_snap(60000.0, spot, 24.0, 0.30),   # K<F -> put is OTM
             _otm_snap(68000.0, spot, 24.0, 0.30)]   # K>F -> call is OTM
    # add the WRONG legs too; they must be filtered out
    snaps.append({**snaps[0], "kind": "C", "symbol": "C-BTC-60000-010826"})
    snaps.append({**snaps[1], "kind": "P", "symbol": "P-BTC-68000-010826"})
    kept = calibrate._otm_leg_quotes(snaps, carry=0.05)
    assert len(kept) == 2
    kinds = {(q["strike"], q["kind"]) for q in kept}
    assert kinds == {(60000.0, "P"), (68000.0, "C")}


def test_fit_smile_recovers_a_convex_curve():
    spot = 64000.0
    term = calibrate.IVTermModel({24.0: 0.20}, rv_at_calibration=0.40)
    snaps = []
    # build a V-shaped smile: higher IV away from the money
    for K, iv in ((59000.0, 0.30), (61500.0, 0.24), (63500.0, 0.205),
                  (64500.0, 0.205), (66500.0, 0.24), (69000.0, 0.30)):
        snaps += [_otm_snap(K, spot, 24.0, iv) for _ in range(12)]
    smile, rep = calibrate.fit_smile(snaps, 0.05, term, 0.40)
    assert rep["n_knots"] >= 3
    atm_ratio = float(smile.ratio(0.0))
    wing_ratio = float(smile.ratio(3.0))
    assert wing_ratio > atm_ratio, "wings must price above the money"


def test_fit_smile_drops_thin_buckets():
    spot = 64000.0
    term = calibrate.IVTermModel({24.0: 0.20}, rv_at_calibration=0.40)
    snaps = [_otm_snap(63500.0, spot, 24.0, 0.21) for _ in range(40)]
    snaps += [_otm_snap(59000.0, spot, 24.0, 0.30)]      # single thin quote
    _, rep = calibrate.fit_smile(snaps, 0.05, term, 0.40)
    thin = [v for v in rep["knots"].values() if v.get("dropped")]
    assert thin, "a bucket below the sample floor must be dropped, not fitted"


def test_fit_smile_falls_back_to_flat_on_insufficient_data():
    term = calibrate.IVTermModel({24.0: 0.20}, rv_at_calibration=0.40)
    smile, rep = calibrate.fit_smile([], 0.05, term, 0.40)
    assert float(smile.ratio(0.0)) == pytest.approx(1.0)
    assert float(smile.ratio(5.0)) == pytest.approx(1.0)
    assert "flat" in rep["note"]


def test_smile_clamps_outside_its_fitted_range():
    """Beyond the observed wings the curve must hold, not extrapolate upward
    on no evidence."""
    smile = calibrate.SmileModel(np.array([-2.0, 0.0, 2.0]),
                                 np.array([1.3, 1.0, 1.3]))
    assert float(smile.ratio(-99.0)) == pytest.approx(1.3)
    assert float(smile.ratio(99.0)) == pytest.approx(1.3)


def test_iv_surface_applies_smile_by_moneyness():
    smile = calibrate.SmileModel(np.array([-2.0, 0.0, 2.0]),
                                 np.array([1.5, 1.0, 1.5]))
    m = calibrate.IVTermModel({24.0: 0.20}, rv_at_calibration=0.40, smile=smile)
    T = np.array([24.0 / 24.0 / 365.0])
    atm = m.iv_array(np.array([24.0]), np.array([0.40]), np.array([0.0]), T)
    # move 2 standardized units OTM
    off = 2.0 * 0.20 * float(np.sqrt(T[0]))
    wing = m.iv_array(np.array([24.0]), np.array([0.40]), np.array([off]), T)
    assert wing[0] > atm[0]
    assert wing[0] == pytest.approx(atm[0] * 1.5, rel=0.05)


def test_iv_surface_without_smile_is_flat_across_strikes():
    m = calibrate.IVTermModel({24.0: 0.20}, rv_at_calibration=0.40)
    T = np.array([1.0 / 365.0])
    a = m.iv_array(np.array([24.0]), np.array([0.40]), np.array([0.0]), T)
    b = m.iv_array(np.array([24.0]), np.array([0.40]), np.array([0.05]), T)
    assert a[0] == pytest.approx(b[0])


def test_realized_vol_must_not_be_computed_on_a_gappy_series():
    """Sampling a sparse series and computing RV on it annualizes the gaps.

    A series sampled every 60 minutes is not a 1-minute series; treating it as
    one inflated RV, IV and premium, and produced validation MAE that grew
    with DTE purely because longer-dated contracts print less often.
    """
    from research import premium as _p
    rng = np.random.default_rng(5)
    # step size chosen so the dense series sits well clear of realized_vol's
    # MIN_IV floor; otherwise both sides clamp and the test proves nothing
    contiguous = 64000.0 + np.cumsum(rng.normal(0, 30.0, 20000))
    bpy = 365 * 24 * 60

    dense_rv = _p.realized_vol(contiguous, 1440, bpy)
    dense_rv = float(np.median(dense_rv[np.isfinite(dense_rv)]))

    sparse = contiguous[::60]                    # one sample per hour
    sparse_rv = _p.realized_vol(sparse, min(1440, len(sparse) - 1), bpy)
    sparse_rv = float(np.median(sparse_rv[np.isfinite(sparse_rv)]))

    assert sparse_rv > 3 * dense_rv, (
        "hourly samples treated as 1m returns must inflate RV -- this is the "
        "failure mode the fix guards against")

import math

import pytest

from research import costs

# Real Delta India rates, verified live 2026-07-24 and recorded in
# strategy/config.py:115-124. taker_commission_rate is 0.01% (NOT the 0.03%
# that older project docs quote -- that was a stale default already corrected
# in the app, and it overstated the notional leg of every fee by 3x).
TAKER = 0.0001
PREMIUM_CAP = 0.035
GST = 0.18


# ---------- Delta options fee ----------

def test_option_fee_takes_the_cheaper_of_notional_and_premium_cap():
    c = costs.OptionCost()
    fee = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=1)
    notional_leg = TAKER * 64000.0 * 0.001 * 1
    premium_leg = PREMIUM_CAP * 10.0 * 0.001 * 1
    assert fee == pytest.approx(min(notional_leg, premium_leg) * (1 + GST))


def test_option_fee_uses_notional_leg_when_premium_is_large():
    c = costs.OptionCost()
    fee = c.fee(premium=100000.0, spot=64000.0, contract_value=0.001, contracts=1)
    assert fee == pytest.approx(TAKER * 64000.0 * 0.001 * 1 * (1 + GST))


def test_option_fee_scales_linearly_with_contracts():
    c = costs.OptionCost()
    one = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=1)
    ten = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=10)
    assert ten == pytest.approx(one * 10)


def test_option_fee_matches_live_app_config():
    """The study's fee must equal what the app charges, or the backtest is not
    measuring the app that will trade."""
    from strategy import config
    c = costs.OptionCost(notional_rate=config.FEE_NOTIONAL_RATE,
                         premium_cap=config.FEE_PREMIUM_CAP,
                         gst=config.GST_RATE)
    fee = c.fee(premium=250.0, spot=64000.0, contract_value=0.001, contracts=3)
    expected = min(config.FEE_NOTIONAL_RATE * 64000.0 * 0.001 * 3,
                   config.FEE_PREMIUM_CAP * 250.0 * 0.001 * 3) * (1 + config.GST_RATE)
    assert fee == pytest.approx(expected)


def test_option_fee_default_matches_config_default():
    """Guards against the 0.0003 regression: the module default must track the
    app's verified rate, not an older doc's figure."""
    from strategy import config
    assert costs.OptionCost().notional_rate == config.FEE_NOTIONAL_RATE


def test_option_fee_accepts_per_product_rates():
    """Rates differ per contract and are read from /v2/products."""
    c = costs.OptionCost(notional_rate=0.0005, premium_cap=0.01, gst=0.0)
    fee = c.fee(premium=1000.0, spot=64000.0, contract_value=0.001, contracts=1)
    assert fee == pytest.approx(min(0.0005 * 64000.0 * 0.001, 0.01 * 1000.0 * 0.001))


def test_round_trip_charges_both_legs():
    c = costs.OptionCost()
    rt = c.round_trip_cost(100.0, 150.0, spot_in=64000.0, spot_out=64500.0,
                           contract_value=0.001, contracts=2)
    expected = (c.fee(100.0, 64000.0, 0.001, 2) + c.fee(150.0, 64500.0, 0.001, 2))
    assert rt == pytest.approx(expected)


# ---------- Black-Scholes ----------

def test_bs_atm_call_known_value():
    # S=K=100, T=1, sigma=20%, r=0 -> 7.9656
    assert costs.bs_price(100.0, 100.0, 1.0, 0.20, is_call=True) == pytest.approx(7.9656, abs=1e-3)


def test_bs_put_call_parity():
    S, K, T, sig = 64000.0, 63000.0, 0.05, 0.55
    call = costs.bs_price(S, K, T, sig, is_call=True)
    put = costs.bs_price(S, K, T, sig, is_call=False)
    assert call - put == pytest.approx(S - K, abs=1e-6)


def test_bs_intrinsic_at_expiry():
    assert costs.bs_price(64000.0, 63000.0, 0.0, 0.5, True) == pytest.approx(1000.0)
    assert costs.bs_price(62000.0, 63000.0, 0.0, 0.5, True) == pytest.approx(0.0)
    assert costs.bs_price(62000.0, 63000.0, 0.0, 0.5, False) == pytest.approx(1000.0)


def test_bs_price_is_monotonic_in_spot_for_a_call():
    prev = -1.0
    for S in (60000.0, 62000.0, 64000.0, 66000.0):
        p = costs.bs_price(S, 64000.0, 0.05, 0.55, True)
        assert p > prev
        prev = p


def test_bs_delta_matches_finite_difference():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    g = costs.bs_greeks(S, K, T, sig, is_call=True)
    h = 1.0
    fd = (costs.bs_price(S + h, K, T, sig, True) - costs.bs_price(S - h, K, T, sig, True)) / (2 * h)
    assert g["delta"] == pytest.approx(fd, abs=1e-4)


def test_bs_gamma_matches_finite_difference():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    g = costs.bs_greeks(S, K, T, sig, is_call=True)
    h = 10.0
    d_up = costs.bs_greeks(S + h, K, T, sig, True)["delta"]
    d_dn = costs.bs_greeks(S - h, K, T, sig, True)["delta"]
    assert g["gamma"] == pytest.approx((d_up - d_dn) / (2 * h), rel=1e-3)


def test_bs_theta_is_negative_for_long_options():
    assert costs.bs_greeks(64000.0, 64000.0, 0.05, 0.55, True)["theta"] < 0
    assert costs.bs_greeks(64000.0, 64000.0, 0.05, 0.55, False)["theta"] < 0


def test_bs_vega_matches_finite_difference():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    g = costs.bs_greeks(S, K, T, sig, is_call=True)
    h = 1e-4
    fd = (costs.bs_price(S, K, T, sig + h, True) - costs.bs_price(S, K, T, sig - h, True)) / (2 * h)
    assert g["vega"] == pytest.approx(fd / 100.0, rel=1e-3)


def test_bs_greeks_degrade_safely_at_expiry():
    g = costs.bs_greeks(65000.0, 64000.0, 0.0, 0.55, True)
    assert g["gamma"] == 0.0 and g["theta"] == 0.0 and g["vega"] == 0.0
    assert g["delta"] == 1.0, "an ITM call at expiry has delta 1"


def test_black76_satisfies_parity_against_the_forward():
    """C - P = F - K. Pricing off spot instead forces C - P = S - K, which
    systematically misprices puts whenever the forward is in contango."""
    F, K, T, sig = 64500.0, 64000.0, 0.05, 0.55
    c = costs.black76_price(F, K, T, sig, True)
    p = costs.black76_price(F, K, T, sig, False)
    assert c - p == pytest.approx(F - K, abs=1e-6)


def test_black76_equals_bs_when_forward_equals_spot():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    assert costs.black76_price(S, K, T, sig, True) == pytest.approx(
        costs.bs_price(S, K, T, sig, True, r=0.0), rel=1e-9)


def test_black76_intrinsic_at_expiry():
    assert costs.black76_price(65000.0, 64000.0, 0.0, 0.5, True) == pytest.approx(1000.0)
    assert costs.black76_price(63000.0, 64000.0, 0.0, 0.5, False) == pytest.approx(1000.0)


def test_contango_forward_lowers_put_value_relative_to_spot_pricing():
    """The mechanism behind the put mispricing this fixed."""
    S, K, T, sig = 64000.0, 64000.0, 0.17, 0.55
    F = costs.forward(S, T, 0.049)
    assert F > S
    assert costs.black76_price(F, K, T, sig, False) < costs.bs_price(S, K, T, sig, False)


def test_forward_grows_with_maturity_and_carry():
    assert costs.forward(64000.0, 0.0, 0.05) == pytest.approx(64000.0)
    assert costs.forward(64000.0, 1.0, 0.05) > costs.forward(64000.0, 0.5, 0.05)
    assert costs.forward(64000.0, 1.0, 0.0) == pytest.approx(64000.0)


def test_perp_cost_charges_both_sides_with_gst():
    c = costs.PerpCost(fee_pct=0.0005, gst=0.18)
    assert c.round_trip_cost(100.0, 200.0) == pytest.approx(
        (100.0 + 200.0) * 0.0005 * 1.18)


def test_black76_array_matches_scalar():
    """The vectorized path must agree with the scalar one it replaced."""
    import numpy as np
    F = np.array([60000.0, 64000.0, 64000.0, 70000.0])
    T = np.array([0.05, 0.05, 0.0, 0.2])
    sig = np.array([0.55, 0.55, 0.55, 0.30])
    for is_call in (True, False):
        got = costs.black76_price_array(F, 64000.0, T, sig, is_call)
        want = [costs.black76_price(f, 64000.0, t, s, is_call)
                for f, t, s in zip(F, T, sig)]
        # The vectorized path uses an inlined erf approximation (numpy has no
        # erf ufunc, scipy is not a dependency). Its ~1e-7 CDF error maps to
        # ~0.01 in price -- an order of magnitude finer than the 0.1 tick
        # these options quote in, so it cannot affect a fill or a P&L figure.
        assert np.allclose(got, want, rtol=1e-5, atol=0.02)


def test_black76_array_handles_expiry_and_zero_vol():
    import numpy as np
    got = costs.black76_price_array(np.array([65000.0, 63000.0]), 64000.0,
                                    np.array([0.0, 0.0]), np.array([0.5, 0.5]), True)
    assert np.allclose(got, [1000.0, 0.0])


def test_black76_array_preserves_parity():
    import numpy as np
    F = np.array([62000.0, 64000.0, 66000.0])
    T = np.full(3, 0.05)
    sig = np.full(3, 0.55)
    c = costs.black76_price_array(F, 64000.0, T, sig, True)
    p = costs.black76_price_array(F, 64000.0, T, sig, False)
    assert np.allclose(c - p, F - 64000.0, atol=1e-5)

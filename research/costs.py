"""Cost models and the Black-Scholes pricing core.

WHY A PROTOCOL
--------------
The study prices two instruments with different fee structures (perp flat
taker vs. Delta options min-of-two-legs). Making the cost model injectable
keeps sim.py as one fill model instead of growing an `if instrument == ...`
branch, and a new structure (maker rebates, another venue) is added as a class
rather than by editing existing code.

FEE RATES
---------
Delta India options: min(taker_rate * spot notional, premium_cap * premium)
plus 18% GST. The defaults here track strategy/config.py, which records the
rates verified live on 2026-07-24: taker_commission_rate = 0.0001 (0.01%) and
premium_commission_rate = 0.035 (3.5%).

Older project documents quote 0.03% for the taker leg. That figure is stale --
it was a fallback default already found to be 3x the real rate and corrected
in config.py. Using it would overstate the notional leg of every fee, and
overstating simulated fees inverts an edge verdict just as surely as
understating them. Rates are per-contract on Delta and are threaded in from
/v2/products by callers that have them.

BLACK-SCHOLES
-------------
Replaces the `spot * IV * sqrt(T) * 0.4` approximation in premium.py, which
yields no usable greeks. r = 0: there is no risk-free leg in a BTC-margined
inverse-settled option.
"""

from __future__ import annotations

import math
from typing import Protocol

try:  # keep the study's defaults tied to the app's verified rates
    from strategy import config as _app_config

    _TAKER = _app_config.FEE_NOTIONAL_RATE
    _PREMIUM_CAP = _app_config.FEE_PREMIUM_CAP
    _GST = _app_config.GST_RATE
except Exception:  # noqa: BLE001 — research must run without the app importable
    _TAKER, _PREMIUM_CAP, _GST = 0.0001, 0.035, 0.18


# --------------------------------------------------------------------------
# Black-Scholes
# --------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, sigma: float, r: float):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def bs_price(S: float, K: float, T: float, sigma: float,
             is_call: bool, r: float = 0.0) -> float:
    """European option price. T in YEARS. Returns intrinsic when T<=0."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    disc = math.exp(-r * T)
    if is_call:
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def black76_price(F: float, K: float, T: float, sigma: float,
                  is_call: bool) -> float:
    """Option price off the FORWARD, undiscounted (Black-76).

    Delta's option book prices off the forward, not spot. Backed out of live
    quotes via put-call parity (F = C - P + K) on 2026-07-25, the forward runs
    in contango that scales with maturity: F/S - 1 was +0.00002 at 4.7h rising
    to +0.00833 at 62 days (~4.9%/yr), and was consistent across strikes within
    each expiry, which is what a real forward looks like rather than noise.

    Pricing off spot instead forces C - P = S - K when the market is quoting
    C - P = F - K. With F > S that systematically OVERPRICES puts, which is
    exactly the failure this replaced: puts tracked mark changes at R2 0.94-0.96
    while their levels were off by 30-90%.

    Discounting is omitted (T is short and the premium settles in the quote
    asset), so parity holds exactly as C - P = F - K.
    """
    if F <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(F - K, 0.0) if is_call else max(K - F, 0.0)
    v = sigma * math.sqrt(T)
    d1 = (math.log(F / K) + 0.5 * sigma * sigma * T) / v
    d2 = d1 - v
    if is_call:
        return F * _norm_cdf(d1) - K * _norm_cdf(d2)
    return K * _norm_cdf(-d2) - F * _norm_cdf(-d1)


def black76_price_array(F, K, T, sigma, is_call: bool):
    """Vectorized Black-76. Same maths as black76_price, over numpy arrays.

    The scalar version in a Python loop costs ~1s per 500k-bar series, and the
    search builds a premium series per (timeframe, DTE band) and reuses it
    across every candidate -- so this is the difference between a sweep that
    finishes and one that does not.
    """
    import numpy as _np

    F = _np.asarray(F, dtype="float64")
    T = _np.asarray(T, dtype="float64")
    sigma = _np.asarray(sigma, dtype="float64")

    intrinsic = _np.maximum(F - K, 0.0) if is_call else _np.maximum(K - F, 0.0)
    live = (T > 0) & (sigma > 0) & (F > 0)
    if not _np.any(live):
        return intrinsic

    v = _np.where(live, sigma * _np.sqrt(_np.maximum(T, 1e-300)), 1.0)
    d1 = _np.where(live,
                   (_np.log(_np.maximum(F, 1e-300) / K) + 0.5 * sigma ** 2 * T) / v,
                   0.0)
    d2 = d1 - v
    def ncdf(x):
        return 0.5 * (1.0 + _erf_vec(x / _np.sqrt(2.0)))

    if is_call:
        priced = F * ncdf(d1) - K * ncdf(d2)
    else:
        priced = K * ncdf(-d2) - F * ncdf(-d1)
    return _np.where(live, priced, intrinsic)


def _erf_vec(x):
    """Vectorized erf via Abramowitz & Stegun 7.1.26 (|error| < 1.5e-7).

    numpy has no erf ufunc and scipy is deliberately not a dependency of this
    project, so the approximation is inlined. Accuracy is far finer than the
    option prices it feeds, which are quoted to 0.1.
    """
    import numpy as _np

    x = _np.asarray(x, dtype="float64")
    sign = _np.sign(x)
    ax = _np.abs(x)
    t = 1.0 / (1.0 + 0.3275911 * ax)
    y = 1.0 - (((((1.061405429 * t - 1.453152027) * t) + 1.421413741) * t
                - 0.284496736) * t + 0.254829592) * t * _np.exp(-ax * ax)
    return sign * y


def forward(S: float, T: float, carry_rate: float) -> float:
    """F = S * exp(carry * T). carry_rate is annualized, T in years."""
    return float(S) * math.exp(float(carry_rate) * float(T))


def bs_greeks(S: float, K: float, T: float, sigma: float,
              is_call: bool, r: float = 0.0) -> dict:
    """delta (per 1 unit spot), gamma, theta (per DAY), vega (per 1 vol POINT).

    theta and vega are scaled to the units the rest of the codebase uses:
    options_calc.py reasons in per-day theta, and IV is quoted in points.
    """
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        if is_call:
            d = 1.0 if S > K else 0.0
        else:
            d = -1.0 if S < K else 0.0
        return {"delta": d, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1, d2 = _d1_d2(S, K, T, sigma, r)
    disc = math.exp(-r * T)
    pdf = _norm_pdf(d1)
    sqrtT = math.sqrt(T)

    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf / (S * sigma * sqrtT)
    vega = S * pdf * sqrtT / 100.0
    term = -(S * pdf * sigma) / (2.0 * sqrtT)
    if is_call:
        theta = (term - r * K * disc * _norm_cdf(d2)) / 365.0
    else:
        theta = (term + r * K * disc * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# --------------------------------------------------------------------------
# Cost models
# --------------------------------------------------------------------------

class CostModel(Protocol):
    """Round-trip cost of one trade in price units of the traded instrument.

    Slippage is baked into fill prices by the caller; this is the explicit
    fee leg only.
    """

    def round_trip_cost(self, entry: float, exit_: float, **kw) -> float:
        ...


class PerpCost:
    """BTCUSD perpetual: flat taker fee plus GST on each side."""

    def __init__(self, fee_pct: float = 0.0005, gst: float = 0.18):
        self.fee_pct = fee_pct
        self.gst = gst

    def round_trip_cost(self, entry: float, exit_: float, **kw) -> float:
        k = self.fee_pct * (1.0 + self.gst)
        return entry * k + exit_ * k


class OptionCost:
    """Delta India options: min(notional leg, premium leg), plus GST.

    Both legs scale by contract_value and contract count, so the min() is
    taken on the ALREADY-SCALED legs. Taking it on per-unit rates and scaling
    afterwards happens to agree only when both legs share the same multiplier,
    which is a coincidence rather than a guarantee -- keep it explicit.
    """

    def __init__(self, notional_rate: float = _TAKER,
                 premium_cap: float = _PREMIUM_CAP, gst: float = _GST):
        self.notional_rate = notional_rate
        self.premium_cap = premium_cap
        self.gst = gst

    def fee(self, premium: float, spot: float,
            contract_value: float, contracts: int) -> float:
        notional_leg = self.notional_rate * spot * contract_value * contracts
        premium_leg = self.premium_cap * premium * contract_value * contracts
        return min(notional_leg, premium_leg) * (1.0 + self.gst)

    def round_trip_cost(self, entry: float, exit_: float, *,
                        spot_in: float, spot_out: float,
                        contract_value: float = 1.0, contracts: int = 1,
                        **kw) -> float:
        return (self.fee(entry, spot_in, contract_value, contracts)
                + self.fee(exit_, spot_out, contract_value, contracts))

"""Fit the option overlay to real option data, and prove it tracks.

WHY THIS EXISTS
---------------
Delta retains option candles ~2 days past expiry (see optdata.py), so a
365-day option backtest cannot use real option prices. The overlay maps a real
spot path to a premium path instead. That is only legitimate if the mapping is
shown to reproduce real option prices on data it was not fitted on, which is
what validate_overlay() is for.

WHAT A SINGLE SNAPSHOT CAN AND CANNOT SUPPORT
---------------------------------------------
The calibration window is one live chain snapshot plus ~3 days of mark
candles. That constrains the model sharply:

  * IV vs RV CANNOT be regressed. Every quote in one snapshot shares the same
    trailing RV, so `iv ~ a + b*rv` has zero variance in x and is degenerate.
    Fitting it anyway would produce meaningless coefficients that look like a
    model.
  * IV vs DTE CAN be fitted -- a snapshot spans many expiries, so the term
    structure is genuinely observed. Measured 2026-07-25: ATM IV ~0.14 at
    1-8h rising to ~0.34 at 7d+.
  * Vol-regime response is ASSUMED, not fitted: IV is scaled proportionally
    with RV relative to RV at calibration time. Backtesting a period whose RV
    is double today's therefore doubles IV. This is an assumption stated
    plainly rather than a measurement dressed up as one.

SCOPE: NEAR-THE-MONEY ONLY
--------------------------
Checked against the live book on 2026-07-25, Black-Scholes at the quoted
mark_iv reproduces ATM mids to within 0.3% (mid 72.00 vs BS 71.81) but
diverges badly out of the money (ratio 1.4x at 1% OTM, 5x at 3% OTM) because
the smile is steep and a single IV cannot span it. The overlay is therefore
only trusted near ATM -- which is what the live OptionResolver trades anyway.
validate_overlay() enforces this by refusing contracts outside the band.
"""

from __future__ import annotations

import math

import numpy as np

from research import costs, premium

MIN_IV_SAMPLES = 20
MIN_BUCKET_SAMPLES = 10
MAE_PCT_LIMIT = 0.25        # overlay must track real marks within 25%
R2_CHANGES_LIMIT = 0.50     # and explain half the variance of premium CHANGES
ATM_BAND = 0.03             # |K/S - 1| beyond this is outside the trusted scope
CARRY_ATM_BAND = 0.02       # tighter: only liquid pairs may set the forward

# Match the live engine: it refuses any expiry closer than this, so the
# overlay is neither fitted nor graded inside that window.
try:
    from strategy.config import MIN_HOURS_TO_EXPIRY
except Exception:  # noqa: BLE001 — research must run without the app importable
    MIN_HOURS_TO_EXPIRY = 2.0


def _dte_band(h: float) -> str:
    if h <= 8:
        return "1-8h"
    if h <= 24:
        return "8-24h"
    if h <= 72:
        return "1-3d"
    if h <= 168:
        return "3-7d"
    return "7d+"


def _money_band(m: float) -> str:
    if m < 0.97:
        return "itm"
    if m <= 1.03:
        return "atm"
    return "otm"


class FlatIV:
    """Constant IV. Used by tests and as the null model in comparisons."""

    def __init__(self, value: float):
        self.value = float(value)

    def iv(self, dte_hours: float, rv: float, log_moneyness: float = 0.0,
           T_years: float | None = None) -> float:
        return self.value

    def iv_array(self, dte_hours: np.ndarray, rv: np.ndarray,
                 log_moneyness: np.ndarray | None = None,
                 T_years: np.ndarray | None = None) -> np.ndarray:
        return np.full(len(np.asarray(dte_hours)), self.value, dtype="float64")


class SmileModel:
    """IV / ATM-IV as a function of STANDARDIZED moneyness.

    x = ln(K/F) / (atm_iv * sqrt(T)) puts every maturity on one axis, so a
    single 1-D curve describes the whole surface instead of a separate smile
    per expiry. That is the parsimonious choice here: the calibration window
    is one snapshot, and pooling gives each knot n=14-46 instead of a handful.

    Measured 2026-07-25 (237 OTM-leg quotes): 1.47 at x=-3, 1.21 at x=-1.5,
    1.03 at the money, 1.08 at x=+1.5, 1.30 at x=+3 -- a clean convex smile.
    """

    def __init__(self, xs: np.ndarray, ratios: np.ndarray):
        order = np.argsort(np.asarray(xs, dtype="float64"))
        self.xs = np.asarray(xs, dtype="float64")[order]
        self.ratios = np.asarray(ratios, dtype="float64")[order]

    def ratio(self, x) -> np.ndarray:
        # np.interp clamps outside the fitted range rather than extrapolating
        # a curve upward without evidence.
        return np.interp(np.asarray(x, dtype="float64"), self.xs, self.ratios)

    @staticmethod
    def flat() -> "SmileModel":
        return SmileModel(np.array([-1.0, 1.0]), np.array([1.0, 1.0]))


class IVTermModel:
    """IV as a function of DTE and moneyness, scaled by the vol regime.

    `knots` is {dte_hours: atm_iv} observed at calibration. Lookups interpolate
    in log(DTE), the axis the term structure is smooth in. An optional
    SmileModel supplies the moneyness dimension; without one the surface is
    flat across strikes.
    """

    def __init__(self, knots: dict, rv_at_calibration: float,
                 floor: float = 0.05, cap: float = 3.0,
                 smile: SmileModel | None = None,
                 regime_scaling: bool = False):
        pts = sorted((float(k), float(v)) for k, v in knots.items())
        self._x = np.log(np.array([max(p[0], 1e-6) for p in pts]))
        self._y = np.array([p[1] for p in pts])
        self.rv_ref = max(float(rv_at_calibration), 1e-6)
        self.floor, self.cap = floor, cap
        self.smile = smile
        self.regime_scaling = regime_scaling

    def _base(self, dte_hours):
        x = np.log(np.maximum(np.asarray(dte_hours, dtype="float64"), 1e-6))
        return np.interp(x, self._x, self._y)

    def atm_iv(self, dte_hours, rv):
        """ATM IV at a given DTE.

        regime_scaling defaults OFF. Scaling IV proportionally with realized
        vol was an ASSUMPTION, never a measurement -- one IV snapshot has zero
        variance in RV, so the relationship could not be fitted. Validation
        against executable trade prints identified it as the dominant residual
        error: it produced a level error worth roughly 7 vol points, and
        turning it off improved BOTH metrics (pass 8/22 -> 11/22, median MAE
        0.273 -> 0.209, median R2 0.673 -> 0.782).

        It is retained as an option because the assumption is economically
        reasonable and may prove correct once recorded book history spans
        more than one vol regime -- at which point it can be fitted rather
        than assumed.
        """
        base = self._base(dte_hours)
        if not self.regime_scaling:
            return base
        return base * (np.maximum(np.asarray(rv, dtype="float64"), 0.0) / self.rv_ref)

    def iv(self, dte_hours: float, rv: float, log_moneyness: float = 0.0,
           T_years: float | None = None) -> float:
        return float(self.iv_array(np.array([dte_hours]), np.array([rv]),
                                   np.array([log_moneyness]),
                                   None if T_years is None else np.array([T_years]))[0])

    def iv_array(self, dte_hours: np.ndarray, rv: np.ndarray,
                 log_moneyness: np.ndarray | None = None,
                 T_years: np.ndarray | None = None) -> np.ndarray:
        """log_moneyness is ln(K/F). Passing None prices at the money."""
        atm = self.atm_iv(dte_hours, rv)
        if self.smile is None or log_moneyness is None:
            return np.clip(atm, self.floor, self.cap)

        if T_years is None:
            T_years = np.asarray(dte_hours, dtype="float64") / 24.0 / 365.0
        denom = np.maximum(atm * np.sqrt(np.maximum(T_years, 1e-12)), 1e-9)
        x = np.asarray(log_moneyness, dtype="float64") / denom
        return np.clip(atm * self.smile.ratio(x), self.floor, self.cap)


def fit_iv_term(snapshots: list[dict], rv_at_calibration: float,
                floor: float = 0.05, cap: float = 3.0) -> tuple[IVTermModel, dict]:
    """Fit the IV term structure from a live chain snapshot.

    Only near-the-money quotes are used: the smile makes OTM mark_iv a
    different quantity, and mixing them inflates the fitted level (an early
    pass that bucketed 0.97-1.03 as "ATM" reported 0.247 where the true ATM
    reading was 0.131).
    """
    usable = [s for s in snapshots
              if s.get("mark_iv") and 0 < float(s["mark_iv"]) < 5
              and abs(float(s.get("moneyness", 1.0)) - 1.0) <= ATM_BAND
              and float(s.get("dte_hours", 0)) > 0]
    if len(usable) < MIN_IV_SAMPLES:
        raise ValueError(
            f"need >= {MIN_IV_SAMPLES} near-ATM IV samples to fit a term "
            f"structure, got {len(usable)}. Fitting fewer would produce a "
            "model the data cannot support.")

    grouped: dict = {}
    for s in usable:
        grouped.setdefault(_dte_band(float(s["dte_hours"])), []).append(s)

    knots = {}
    per_band = {}
    for band, rows in grouped.items():
        h = float(np.median([float(r["dte_hours"]) for r in rows]))
        v = float(np.median([float(r["mark_iv"]) for r in rows]))
        knots[h] = v
        per_band[band] = {"n": len(rows), "dte_hours": h, "iv": v}

    if len(knots) == 1:
        # A single band cannot describe a curve; hold it flat rather than
        # extrapolating a slope out of one point.
        only = next(iter(knots.items()))
        knots = {max(only[0] * 0.5, 1e-3): only[1], only[0] * 2.0: only[1]}

    model = IVTermModel(knots, rv_at_calibration, floor=floor, cap=cap)
    report = {"n": len(usable), "bands": per_band,
              "rv_at_calibration": float(rv_at_calibration),
              "atm_band": ATM_BAND,
              "note": "IV-vs-RV was NOT regressed: one snapshot has zero "
                      "variance in RV. Regime response is an assumed "
                      "proportional scaling, not a measurement."}
    return model, report


def fit_carry(snapshots: list[dict]) -> tuple[float, dict]:
    """Annualized carry rate, backed out of live quotes via put-call parity.

    For each (strike, expiry) with both legs quoted, F = C - P + K. Regressing
    ln(F/S) on T through the origin gives the carry. Measured 2026-07-25:
    ~4.9%/yr, with F/S-1 rising monotonically from +0.00002 at 4.7h to
    +0.00833 at 62 days and agreeing across strikes within each expiry.
    """
    by: dict = {}
    for x in snapshots:
        key = (x.get("strike"), x.get("symbol", "").rsplit("-", 1)[-1])
        by.setdefault(key, {})[x.get("kind")] = x

    # Only near-ATM pairs, and a MEDIAN estimator rather than least squares.
    #
    # Both guards exist because the first version had neither and returned
    # 0.687/yr where the true rate is ~0.05/yr. Deep OTM legs are worth a few
    # dollars and their quotes are unreliable, so F = C - P + K built from one
    # of them is noise; least squares then chases that noise. Restricting to
    # liquid near-ATM pairs and taking a median of per-pair rates is robust to
    # the handful that remain bad.
    rates, n_pairs = [], 0
    for (K, _exp), legs in by.items():
        c, p = legs.get("C"), legs.get("P")
        if not c or not p or not K:
            continue
        S = float(c.get("spot") or 0)
        T = float(c.get("dte_hours") or 0) / 24.0 / 365.0
        if S <= 0 or T <= 0:
            continue
        if abs(float(K) / S - 1.0) > CARRY_ATM_BAND:
            continue
        F = float(c["mid"]) - float(p["mid"]) + float(K)
        if F <= 0:
            continue
        rates.append(math.log(F / S) / T)
        n_pairs += 1

    if n_pairs < 8:
        return 0.0, {"carry_rate": 0.0, "n_pairs": n_pairs,
                     "note": "too few near-ATM call/put pairs to back out a "
                             "forward; carry held at zero (prices off spot)"}

    carry = float(np.median(rates))
    return carry, {"carry_rate": carry, "n_pairs": n_pairs,
                   "annualized_pct": round(carry * 100, 3),
                   "estimator": "median of per-pair ln(F/S)/T, near-ATM only",
                   "p25": float(np.percentile(rates, 25)),
                   "p75": float(np.percentile(rates, 75))}


def _otm_leg_quotes(snapshots: list[dict], carry: float) -> list[dict]:
    """Keep only the OUT-OF-THE-MONEY leg at each strike.

    Quoted IV is only meaningful where vega is. Deep in-the-money options are
    almost entirely intrinsic, so their quoted IV is numerically unstable and
    carries no vol information. Measured 2026-07-25, mixing both legs produced
    an incoherent curve: put IV fell monotonically to 0.073 as K/S rose into
    deep-ITM territory while call IV rose to 0.316 over the same range. Taking
    the OTM leg at each strike -- the market-standard construction -- yields a
    clean convex smile from the same data.
    """
    out = []
    for x in snapshots:
        iv = x.get("mark_iv")
        h = float(x.get("dte_hours") or 0.0)
        S = float(x.get("spot") or 0.0)
        K = float(x.get("strike") or 0.0)
        if not iv or h <= 0 or S <= 0 or K <= 0:
            continue
        iv = float(iv)
        if not (0 < iv < 5):
            continue
        T = h / 24.0 / 365.0
        F = S * math.exp(carry * T)
        want = "P" if K < F else "C"
        if x.get("kind") != want:
            continue
        out.append({**x, "T": T, "F": F, "log_moneyness": math.log(K / F)})
    return out


def fit_smile(snapshots: list[dict], carry: float, term: IVTermModel,
              rv_at_calibration: float,
              edges=(-4.0, -2.0, -1.0, -0.5, 0.5, 1.0, 2.0, 4.0)) -> tuple[SmileModel, dict]:
    """Fit IV/ATM-IV against standardized moneyness, pooled across maturities.

    Parsimony: one 1-D curve for the whole surface, and a bucket is only kept
    as a knot if it holds at least MIN_BUCKET_SAMPLES quotes. Buckets thinner
    than that are dropped rather than fitted, and the curve clamps to its
    fitted range instead of extrapolating.
    """
    rows = _otm_leg_quotes(snapshots, carry)
    if len(rows) < MIN_IV_SAMPLES:
        return SmileModel.flat(), {"n": len(rows), "knots": {},
                                   "note": "too few OTM-leg quotes; smile held flat"}

    xs, ratios = [], []
    for r in rows:
        atm = float(term.atm_iv(np.array([r["dte_hours"]]),
                                np.array([rv_at_calibration]))[0])
        if atm <= 0 or r["T"] <= 0:
            continue
        x = r["log_moneyness"] / (atm * math.sqrt(r["T"]))
        xs.append(x)
        ratios.append(float(r["mark_iv"]) / atm)

    xs = np.array(xs)
    ratios = np.array(ratios)

    knot_x, knot_y, report = [], [], {}
    for lo, hi in zip(edges, edges[1:]):
        sel = (xs >= lo) & (xs < hi)
        n = int(sel.sum())
        if n < MIN_BUCKET_SAMPLES:
            report[f"{lo:+.1f}..{hi:+.1f}"] = {"n": n, "dropped": True}
            continue
        knot_x.append(0.5 * (lo + hi))
        knot_y.append(float(np.median(ratios[sel])))
        report[f"{lo:+.1f}..{hi:+.1f}"] = {"n": n, "ratio": knot_y[-1]}

    if len(knot_x) < 3:
        return SmileModel.flat(), {"n": len(rows), "knots": report,
                                   "note": "fewer than 3 populated buckets; "
                                           "smile held flat"}
    return (SmileModel(np.array(knot_x), np.array(knot_y)),
            {"n": len(rows), "knots": report, "n_knots": len(knot_x)})


class SpreadModel:
    def __init__(self, buckets: dict, fallback: float):
        self.buckets = buckets
        self.fallback = float(fallback)

    def spread_pct(self, moneyness: float, dte_hours: float) -> float:
        return float(self.buckets.get((_money_band(moneyness), _dte_band(dte_hours)),
                                      self.fallback))


def fit_spread(snapshots: list[dict]) -> SpreadModel:
    """Median spread per (moneyness, DTE) bucket.

    A bucket with thin evidence must not look CHEAP, so any bucket under
    MIN_BUCKET_SAMPLES is floored at half the global median. Under-charging
    spread is the single easiest way to invent an edge that does not exist.
    """
    rows = [s for s in snapshots
            if np.isfinite(s.get("spread_pct", np.nan)) and s["spread_pct"] >= 0]
    if not rows:
        return SpreadModel({}, 0.15)
    global_med = float(np.median([s["spread_pct"] for s in rows]))

    grouped: dict = {}
    for s in rows:
        key = (_money_band(float(s.get("moneyness", 1.0))),
               _dte_band(float(s.get("dte_hours", 0.0))))
        grouped.setdefault(key, []).append(float(s["spread_pct"]))

    buckets = {}
    for key, vals in grouped.items():
        med = float(np.median(vals))
        buckets[key] = med if len(vals) >= MIN_BUCKET_SAMPLES else max(med, 0.5 * global_med)
    return SpreadModel(buckets, global_med)


class Overlay:
    """Maps a real spot path to a premium path."""

    def __init__(self, iv_model, spread_model: SpreadModel,
                 cost_model: costs.OptionCost, rv_window: int = 1440,
                 bar_seconds: int = 60, carry_rate: float = 0.0):
        self.iv_model = iv_model
        self.spread_model = spread_model
        self.cost_model = cost_model
        self.rv_window = rv_window
        self.bar_seconds = bar_seconds
        self.carry_rate = float(carry_rate)

    def _rv(self, spot: np.ndarray) -> np.ndarray:
        bars_per_year = 365.0 * 86400.0 / self.bar_seconds
        w = min(self.rv_window, max(2, len(spot) - 1))
        return premium.realized_vol(spot, w, bars_per_year)

    def premium_path(self, spot: np.ndarray, K: float, expiry_ts: int,
                     times: np.ndarray, is_call: bool,
                     rv: np.ndarray | None = None) -> np.ndarray:
        spot = np.asarray(spot, dtype="float64")
        times = np.asarray(times, dtype="int64")
        rv = self._rv(spot) if rv is None else np.asarray(rv, dtype="float64")
        rv = np.nan_to_num(rv, nan=float(np.nanmedian(rv)) if np.any(np.isfinite(rv)) else 0.5)

        secs_left = np.maximum(expiry_ts - times, 0).astype("float64")
        T = secs_left / (365.0 * 86400.0)

        # Price off the FORWARD, which is what the book quotes against.
        # Using spot forces C - P = S - K and systematically misprices puts.
        fwd = spot * np.exp(self.carry_rate * T)

        # IV at THIS bar's moneyness, not at the money. A contract struck ATM
        # drifts along the smile as spot moves, and pricing that drift at a
        # flat ATM vol was the residual error the smile fixes.
        log_m = np.log(np.maximum(K / np.maximum(fwd, 1e-9), 1e-12))
        sigma = self.iv_model.iv_array(secs_left / 3600.0, rv, log_m, T)
        return costs.black76_price_array(fwd, K, T, sigma, is_call)

    def bid_ask(self, premium_mid: np.ndarray, K: float, expiry_ts: int,
                times: np.ndarray, spot: np.ndarray):
        """Entries pay the ask, exits receive the bid. Never mid."""
        secs_left = np.maximum(expiry_ts - np.asarray(times), 0).astype("float64")
        half = np.array([
            0.5 * self.spread_model.spread_pct(K / max(float(s), 1e-9), float(h) / 3600.0)
            for s, h in zip(np.asarray(spot), secs_left)
        ])
        mid = np.asarray(premium_mid, dtype="float64")
        return mid * (1.0 - half), mid * (1.0 + half)


def _verdict(symbol, mark, pred, extra=None) -> dict:
    # Normalize by a STABLE per-contract scale, not the per-bar premium.
    #
    # Dividing by |mark| at each bar looked reasonable but is unbounded: as an
    # option approaches expiry its premium collapses toward zero (or toward
    # intrinsic), so a fixed absolute error becomes an arbitrarily large
    # percentage. Measured on the first calibration run, near-expiry contracts
    # scored 5x worse on that metric (median 0.338 vs 0.069) while their
    # R2 on premium CHANGES stayed at 0.85-0.96 -- i.e. the model was tracking
    # those contracts well and the metric was reporting otherwise.
    #
    # The median premium over the window is a scale that does not collapse,
    # and it answers the question actually being asked: how large is a typical
    # pricing error relative to this contract's typical premium.
    scale = float(np.median(np.abs(mark)))
    if not np.isfinite(scale) or scale <= 0:
        scale = float(np.mean(np.abs(mark))) or 1e-9
    mae_pct = float(np.mean(np.abs(pred - mark)) / scale)
    rmse_pct = float(math.sqrt(np.mean((pred - mark) ** 2)) / scale)
    dm, dp = np.diff(mark), np.diff(pred)
    if len(dm) > 2 and float(np.std(dm)) > 0:
        ss_res = float(np.sum((dm - dp) ** 2))
        ss_tot = float(np.sum((dm - dm.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    else:
        r2 = float("nan")
    passed = bool(mae_pct <= MAE_PCT_LIMIT
                  and (r2 >= R2_CHANGES_LIMIT if np.isfinite(r2) else False))
    out = {"symbol": symbol, "n_bars": int(len(mark)), "mae_pct": mae_pct,
           "rmse_pct": rmse_pct, "r2_changes": float(r2), "passed": passed,
           "mae_limit": MAE_PCT_LIMIT, "r2_limit": R2_CHANGES_LIMIT}
    if extra:
        out.update(extra)
    return out


def _reject(symbol, reason) -> dict:
    return {"symbol": symbol, "n_bars": 0, "mae_pct": float("nan"),
            "rmse_pct": float("nan"), "r2_changes": float("nan"),
            "passed": False, "reason": reason,
            "mae_limit": MAE_PCT_LIMIT, "r2_limit": R2_CHANGES_LIMIT}


MIN_TRADED_BARS = 100      # executable prints needed before a verdict is given


def validate_overlay_traded(overlay: Overlay, symbol: str) -> dict:
    """Validate against EXECUTABLE trade prints rather than MARK.

    MARK is a model output: it tracks the book mid within +-1% at ATM but runs
    +72% to +439% above it for cheap OTM options, which is precisely the
    regime where MARK-based validation was failing. Delta serves no historical
    bid/ask (BID:/ASK:/MID: all return empty), so trade prints are the only
    historical reference that represents money actually changing hands.

    Methodology is deliberately IDENTICAL to validate_overlay(): same expiry
    window, same ATM band, same MAE/RMSE normalization, same R2 on premium
    changes, same thresholds. Only the reference series differs.
    """
    from research import data, optdata
    try:
        meta = optdata.parse_symbol(symbol)
    except ValueError as e:
        return _reject(symbol, str(e))

    tr = optdata.traded_candles(symbol, "1m", 5)
    if len(tr["time"]) < MIN_TRADED_BARS:
        return _reject(symbol, f"insufficient trade prints ({len(tr['time'])} bars)")

    # Only bars where a trade actually happened. A zero-volume bar carries no
    # execution information and would just repeat a stale print.
    traded = tr["volume"] > 0
    if int(traded.sum()) < MIN_TRADED_BARS:
        return _reject(symbol, f"only {int(traded.sum())} bars with volume > 0")

    t_all = tr["time"][traded]
    px_all = tr["close"][traded]

    spot_series = data.fetch("BTCUSD", "1m", 365)
    idx = np.searchsorted(spot_series["time"], t_all)
    idx = np.clip(idx, 0, len(spot_series["time"]) - 1)
    ok = spot_series["time"][idx] == t_all
    if int(ok.sum()) < MIN_TRADED_BARS:
        return _reject(symbol, "spot/trade timestamps do not align")

    times = t_all[ok]
    ref = px_all[ok]
    spot = spot_series["close"][idx[ok]]
    K, expiry, is_call = meta["strike"], meta["expiry"], meta["kind"] == "C"

    live = (expiry - times) >= MIN_HOURS_TO_EXPIRY * 3600
    near = np.abs(K / np.maximum(spot, 1e-9) - 1.0) <= ATM_BAND
    keep = live & near
    if int(keep.sum()) < MIN_TRADED_BARS:
        return _reject(
            symbol,
            f"only {int(keep.sum())} tradeable near-ATM prints "
            f"(need {MIN_TRADED_BARS}; band +-{ATM_BAND:.0%})")

    # Realized vol MUST come from the contiguous minute series, then be
    # sampled at the kept timestamps.
    #
    # Trade prints are sparse and irregular, so computing RV on the filtered
    # array treats an hour-long gap between prints as a one-minute return and
    # annualizes it. That inflated RV, which inflated IV, which inflated the
    # predicted premium -- producing MAE that grew with DTE (0.91 at <1d to
    # 5.87 at >7d) purely because longer-dated contracts print less often.
    bpy = 365.0 * 24.0 * 60.0
    rv_full = premium.realized_vol(spot_series["close"], 1440, bpy)
    rv_at = rv_full[idx[ok]][keep]
    med = float(np.nanmedian(rv_full[np.isfinite(rv_full)])) if np.any(np.isfinite(rv_full)) else 0.5
    rv_at = np.nan_to_num(rv_at, nan=med)

    times, ref, spot = times[keep], ref[keep], spot[keep]
    pred = overlay.premium_path(spot, K, expiry, times, is_call, rv=rv_at)

    fwd = spot * np.exp(overlay.carry_rate *
                        np.maximum(expiry - times, 0) / (365.0 * 86400.0))
    return _verdict(symbol, ref, pred, extra={
        "kind": meta["kind"],
        "median_moneyness": float(np.median(K / np.maximum(fwd, 1e-9))),
        "median_dte_hours": float(np.median((expiry - times) / 3600.0)),
        "median_premium": float(np.median(ref)),
        "reference": "traded_prints",
    })


def validate_overlay(overlay: Overlay, symbol: str | None,
                     _fake: tuple | None = None) -> dict:
    """Compare the overlay against REAL mark prices it was not fitted on.

    Returns a verdict dict rather than raising, so a caller can report a
    failed gate and stop cleanly instead of crashing mid-study.
    """
    if _fake is not None:
        spot, mark, times, K, expiry, is_call = _fake
    else:
        from research import data, optdata
        try:
            meta = optdata.parse_symbol(symbol)
        except ValueError as e:
            return _reject(symbol, str(e))
        m = optdata.mark_candles(symbol, "1m", 5)
        if len(m["time"]) < 100:
            return _reject(symbol, f"insufficient real mark history ({len(m['time'])} bars)")

        spot_series = data.fetch("BTCUSD", "1m", 365)
        idx = np.searchsorted(spot_series["time"], m["time"])
        idx = np.clip(idx, 0, len(spot_series["time"]) - 1)
        ok = spot_series["time"][idx] == m["time"]
        if int(ok.sum()) < 100:
            return _reject(symbol, "spot/mark timestamps do not align")

        times = m["time"][ok]
        mark = m["close"][ok]
        spot = spot_series["close"][idx[ok]]
        K, expiry, is_call = meta["strike"], meta["expiry"], meta["kind"] == "C"

        # Score only the region the live engine would actually trade. It
        # refuses any expiry inside MIN_HOURS_TO_EXPIRY, where the remaining
        # premium is almost pure theta on a thinning book, so measuring the
        # overlay there would grade it on bars it will never be asked to price.
        live = (expiry - times) >= MIN_HOURS_TO_EXPIRY * 3600
        if int(live.sum()) < 100:
            return _reject(symbol, "too few bars outside the no-trade expiry window")

        # Score only bars where the contract is actually near ATM.
        #
        # Filtering by the contract's MEDIAN moneyness was wrong: a contract
        # can sit inside the band on median while spending most of its life
        # far from it. That matters because Delta's MARK price is itself a
        # model output -- measured 2026-07-25, MARK tracks the book mid within
        # +-1% at ATM but runs +72% to +439% above it for cheap OTM contracts,
        # where the mark carries a vol floor the real book does not. Grading
        # the overlay on those bars compares two models to each other rather
        # than comparing this model to a tradeable price.
        #
        # Per-bar filtering also matches the live engine, whose OptionResolver
        # selects the ATM strike at entry.
        near = np.abs(K / np.maximum(spot, 1e-9) - 1.0) <= ATM_BAND
        keep = live & near
        if int(keep.sum()) < 100:
            return _reject(
                symbol,
                f"only {int(keep.sum())} tradeable near-ATM bars "
                f"(need 100; band +-{ATM_BAND:.0%})")

        # Same reason as validate_overlay_traded: the live/ATM filters leave a
        # gappy series, and computing RV on it annualizes the gaps.
        bpy = 365.0 * 24.0 * 60.0
        rv_full = premium.realized_vol(spot_series["close"], 1440, bpy)
        rv_at = rv_full[idx[ok]][keep]
        finite = rv_full[np.isfinite(rv_full)]
        rv_at = np.nan_to_num(rv_at, nan=float(np.median(finite)) if len(finite) else 0.5)

        times, mark, spot = times[keep], mark[keep], spot[keep]
        pred = overlay.premium_path(spot, K, expiry, times, is_call, rv=rv_at)
        return _verdict(symbol, mark, pred)

    pred = overlay.premium_path(spot, K, expiry, times, is_call)
    return _verdict(symbol, mark, pred)

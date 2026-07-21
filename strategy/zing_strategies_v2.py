"""Retuned (v2) Zing strategies — PHASE 3 output of the 2026-07-21 study.

The originals in `zing_strategies.py` are NOT modified. Each class here subclasses
its original and overrides only what the Phase 2 analysis justified, so the
delta between v1 and v2 is inspectable in one place.

READ THIS BEFORE USING ANY OF IT
--------------------------------
Phase 2 tested 274 parameter combinations plus 6 regime filters across 60 days
of in-sample 1m/5m BTCUSD data. For the seven directional strategies, ZERO
combinations produced positive expectancy — not net, and not even gross (before
any fee or slippage). Section 8 Rule 8 of the study therefore classifies them
CONCEPT_FLAWED, and they are deliberately NOT retuned: searching harder on a
signal with no measurable edge finds noise, not alpha.

What IS provided here is the single lever the analysis did validate — a
volatility-regime gate — exposed as an opt-in mixin. On `traffic_light` it lifted
in-sample gross expectancy from -0.83 R to -0.45 R. That is a large, real, and
reproducible improvement. It is also still negative. The gate makes a losing
strategy lose less; it does not make it profitable, and nothing in this module
should be read as a recommendation to trade.

WHY THE GATE WORKS (mechanism, not curve fit)
---------------------------------------------
Risk is sized as a multiple of ATR, so cost measured in R is
`round_trip_cost / (stop_atr * ATR)`. On 1m BTC bars ATR is roughly 0.05% of
spot while the prescribed round trip is 0.218%, making cost ~2.9 R per trade.
Restricting entries to the top volatility quintile raises ATR, which shrinks
cost in R terms. The gate improves the arithmetic of the trade; it does not
improve the predictive quality of the signal.
"""

from __future__ import annotations

from typing import Optional

from . import indicators as ind
from .base import Context, Signal
from . import zing_strategies as v1

# Entries are allowed only when ATR(14)/price sits above this percentile of its
# own recent history. Percentile rather than an absolute cutoff because the
# absolute thresholds used in the study (ATR/spot > 0.008) select literally zero
# 1m bars — BTC 1m ATR is ~0.0005 of spot, sixteen times smaller.
VOL_GATE_PERCENTILE = 80
VOL_GATE_WINDOW = 500          # bars of history the percentile is measured over


def _percentile(sorted_vals: list[float], pct: float) -> float:
    """Linear-interpolated percentile of an already-sorted list."""
    if not sorted_vals:
        return float("inf")
    if len(sorted_vals) == 1:
        return sorted_vals[0]
    k = (len(sorted_vals) - 1) * (pct / 100.0)
    lo = int(k)
    hi = min(lo + 1, len(sorted_vals) - 1)
    return sorted_vals[lo] + (sorted_vals[hi] - sorted_vals[lo]) * (k - lo)


def volatility_gate_open(candles: list[dict], *, percentile: float = VOL_GATE_PERCENTILE,
                         window: int = VOL_GATE_WINDOW,
                         atr_period: int = 14) -> bool:
    """True when the current bar's ATR/price is in the top (100-percentile)% of
    the trailing `window` bars.

    Self-calibrating by construction: it adapts to whatever volatility regime the
    market is in, so it needs no retuning when BTC's absolute volatility shifts.
    """
    if len(candles) < max(atr_period + 2, 30):
        return False
    a = ind.atr(candles, atr_period)
    ratios = [
        a[i] / float(candles[i]["close"])
        for i in range(max(0, len(a) - window), len(a))
        if a[i] is not None and float(candles[i]["close"]) > 0
    ]
    if len(ratios) < 30 or a[-1] is None:
        return False
    current = a[-1] / float(candles[-1]["close"])
    return current >= _percentile(sorted(ratios), percentile)


class VolatilityGated:
    """Mixin: drop every signal the parent produces outside the volatility gate.

    Composes with any Strategy because it only filters the parent's output — it
    never reimplements the signal, so v2 can never silently drift from v1's rule.
    """

    vol_percentile: float = VOL_GATE_PERCENTILE
    vol_window: int = VOL_GATE_WINDOW

    def evaluate(self, ctx: Context) -> list[Signal]:
        sigs = super().evaluate(ctx)          # type: ignore[misc]
        if not sigs:
            return []
        if not volatility_gate_open(ctx.underlying, percentile=self.vol_percentile,
                                    window=self.vol_window):
            return []
        for s in sigs:
            s.strategy = self.slug            # type: ignore[attr-defined]
            s.meta = {**s.meta, "vol_gated": True}
        return sigs


# --------------------------------------------------------------------------- #
# Gated variants.
#
# Provided so the finding is reproducible and so the gate can be A/B tested in
# paper mode. NONE of these reached positive expectancy in Phase 2 — they are
# strictly "less negative". Do not enable them expecting profit.
# --------------------------------------------------------------------------- #
class EMACrossV2(VolatilityGated, v1.EMACross):
    slug = "ema_cross_v2"
    title = "EMA Cross (vol-gated)"
    description = ("v1 rule, entries restricted to the top volatility quintile. "
                   "IS gross -0.81R -> still negative. NOT profitable.")


class TrafficLightV2(VolatilityGated, v1.TrafficLight):
    slug = "traffic_light_v2"
    title = "Traffic Light (vol-gated)"
    description = ("v1 rule, top volatility quintile only. Largest measured gate "
                   "effect: IS gross -0.83R -> -0.45R. Still negative.")


class ScalpingPulseV2(VolatilityGated, v1.ScalpingPulse):
    slug = "scalping_pulse_v2"
    title = "Scalping Pulse (vol-gated)"
    description = "v1 rule, top volatility quintile only. Still negative."


class MeanReversionBollingerV2(VolatilityGated, v1.MeanReversionBollinger):
    slug = "mean_reversion_bollinger_v2"
    title = "Mean Reversion Bollinger (vol-gated)"
    description = "v1 rule, top volatility quintile only. Still negative."


class PrimeScalperEMAV2(VolatilityGated, v1.PrimeScalperEMA):
    slug = "prime_scalper_ema_v2"
    title = "Prime Scalper EMA (vol-gated)"
    description = "v1 rule, top volatility quintile only. Still negative."


class InsideCandleV2(VolatilityGated, v1.InsideCandle):
    slug = "inside_candle_v2"
    title = "Inside Candle (vol-gated)"
    description = "v1 rule, top volatility quintile only. Still negative."


class SwingKingSniperV2(VolatilityGated, v1.SwingKingSniper):
    slug = "swingking_sniper_v2"
    title = "SwingKing Sniper (vol-gated)"
    description = "v1 rule, top volatility quintile only. Still negative."


# Registry is intentionally NOT wired into STRATEGY_CLASSES. These are research
# artifacts; enabling one requires an explicit, deliberate import.
V2_CLASSES = [
    EMACrossV2, TrafficLightV2, ScalpingPulseV2, MeanReversionBollingerV2,
    PrimeScalperEMAV2, InsideCandleV2, SwingKingSniperV2,
]

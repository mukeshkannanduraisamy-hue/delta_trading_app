"""The eight Zing Trade strategies, adapted to Delta BTC/ETH options.

Each class implements the entry logic exactly as described on
https://zing.trade/blog/category/strategies/ . Directional strategies read the
underlying perpetual candles and buy the ATM call (CE) or put (PE); Booming
Bulls reads the option premium candles directly.

Risk is returned as a % of the option premium (sl_pct) with the strategy's
published reward:reward ratio (rr); the engine turns that into target/stop
premium levels at fill time.
"""

from __future__ import annotations

from . import indicators as ind
from .base import Context, Signal, Strategy


def _green(c: dict) -> bool:
    return float(c["close"]) >= float(c["open"])


# --------------------------------------------------------------------------- #
# 1. EMA Cross — EMA9/EMA21 crossover on 1m underlying candles.
# --------------------------------------------------------------------------- #
class EMACross(Strategy):
    slug = "ema_cross"
    title = "EMA Cross"
    timeframe = "1m"
    basis = "underlying"
    description = (
        "9/21 EMA crossover on 1m candles. Fires only on the crossover candle: "
        "cross up -> buy ATM call, cross down -> buy ATM put. RR 1.2:1."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        # Self-guard the length before any negative indexing: a strategy must be
        # safe for any caller, not only for an engine that happens to pre-check
        # (audit #18).
        if len(ctx.underlying) < 22:
            return []
        c = ind.closes(ctx.underlying)
        fast, slow = ind.ema(c, 9), ind.ema(c, 21)
        if fast[-1] is None or slow[-1] is None or fast[-2] is None or slow[-2] is None:
            return []
        sl, rr = self.params.get("sl_pct", 0.06), self.params.get("rr", 1.2)
        if fast[-2] <= slow[-2] and fast[-1] > slow[-1]:
            return [Signal(self.slug, "CE", "9EMA crossed above 21EMA", sl, rr, confidence=65)]
        if fast[-2] >= slow[-2] and fast[-1] < slow[-1]:
            return [Signal(self.slug, "PE", "9EMA crossed below 21EMA", sl, rr, confidence=65)]
        return []


# --------------------------------------------------------------------------- #
# 2. Scalping Pulse — fast/slow MA trend + pullback to fast MA + confirmation.
# --------------------------------------------------------------------------- #
class ScalpingPulse(Strategy):
    slug = "scalping_pulse"
    title = "Scalping Pulse"
    timeframe = "1m"
    basis = "underlying"
    description = (
        "1m trend (9/21 EMA) + pullback to the fast EMA + a confirmation candle "
        "breaking the prior candle. Buy ATM CE/PE. RR 1:1."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        u = ctx.underlying
        if len(u) < 22:
            return []
        c = ind.closes(u)
        fast, slow = ind.ema(c, 9), ind.ema(c, 21)
        if any(x[-1] is None for x in (fast, slow)):
            return []
        sl, rr = self.params.get("sl_pct", 0.06), self.params.get("rr", 1.0)
        last, prev = u[-1], u[-2]
        f = fast[-1]
        # Pullback = one of the last 3 candles reached the fast EMA.
        touched_low = min(float(x["low"]) for x in u[-3:]) <= f
        touched_high = max(float(x["high"]) for x in u[-3:]) >= f
        if fast[-1] > slow[-1] and touched_low and _green(last) and float(last["close"]) > float(prev["high"]):
            return [Signal(self.slug, "CE", "Bull trend, pullback to 9EMA, breakout candle", sl, rr, confidence=75)]
        if fast[-1] < slow[-1] and touched_high and not _green(last) and float(last["close"]) < float(prev["low"]):
            return [Signal(self.slug, "PE", "Bear trend, pullback to 9EMA, breakdown candle", sl, rr, confidence=75)]
        return []


# --------------------------------------------------------------------------- #
# 3. Traffic Light — two-candle colour flip breakout (OCO).
# --------------------------------------------------------------------------- #
class TrafficLight(Strategy):
    slug = "traffic_light"
    title = "Traffic Light"
    timeframe = "1m"
    basis = "underlying"
    description = (
        "Green->red or red->green two-candle pair; break above the pair's high "
        "-> CE, break below its low -> PE (only one side). 15SMA+EMA context. RR 1.2."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        u = ctx.underlying
        if len(u) < 4:
            return []
        a, b, brk = u[-3], u[-2], u[-1]
        # Opposite-coloured pair.
        if _green(a) == _green(b):
            return []
        upper = max(float(a["high"]), float(b["high"]))
        lower = min(float(a["low"]), float(b["low"]))
        c = ind.closes(u)
        s = ind.sma(c, 15)
        trend = s[-1]
        sl, rr = self.params.get("sl_pct", 0.08), self.params.get("rr", 1.2)
        close = float(brk["close"])
        if close > upper and (trend is None or close >= trend):
            return [Signal(self.slug, "CE", "Break above 2-candle pair high", sl, rr, confidence=50)]
        if close < lower and (trend is None or close <= trend):
            return [Signal(self.slug, "PE", "Break below 2-candle pair low", sl, rr, confidence=50)]
        return []


# --------------------------------------------------------------------------- #
# 4. Inside Candle — 5m inside bar breakout with a 35% proximity filter.
# --------------------------------------------------------------------------- #
class InsideCandle(Strategy):
    slug = "inside_candle"
    title = "Inside Candle"
    timeframe = "5m"
    basis = "underlying"
    description = (
        "5m mother/baby inside bar; enter on a close outside the mother range "
        "(<=35% beyond it). Bull -> CE, bear -> PE. SL at opposite end, target 2:1."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        u = ctx.underlying
        if len(u) < 3:
            return []
        mother, baby, brk = u[-3], u[-2], u[-1]
        mh, ml = float(mother["high"]), float(mother["low"])
        rng = mh - ml
        if rng <= 0:
            return []
        # baby fully inside mother.
        if not (float(baby["high"]) <= mh and float(baby["low"]) >= ml):
            return []
        close = float(brk["close"])
        sl, rr = self.params.get("sl_pct", 0.08), self.params.get("rr", 2.0)
        if close > mh and (close - mh) <= 0.35 * rng:
            return [Signal(self.slug, "CE", "Inside-bar breakout above mother", sl, rr, confidence=55)]
        if close < ml and (ml - close) <= 0.35 * rng:
            return [Signal(self.slug, "PE", "Inside-bar breakdown below mother", sl, rr, confidence=55)]
        return []


# --------------------------------------------------------------------------- #
# 5. Mean Reversion Bollinger — fade Bollinger-band extremes.
# --------------------------------------------------------------------------- #
class MeanReversionBollinger(Strategy):
    slug = "mean_reversion_bollinger"
    title = "Mean Reversion Bollinger"
    timeframe = "1m"
    basis = "underlying"
    description = (
        "Bollinger(20,2). Close above upper band -> fade with a PUT; close below "
        "lower band -> fade with a CALL. Best in range-bound markets. RR 1.5."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        if len(ctx.underlying) < 21:
            return []
        c = ind.closes(ctx.underlying)
        mid, up, lo = ind.bollinger(c, 20, 2.0)
        if up[-1] is None or lo[-1] is None or up[-2] is None or lo[-2] is None:
            return []
        sl, rr = self.params.get("sl_pct", 0.08), self.params.get("rr", 1.5)
        prev, close = c[-2], c[-1]
        # Fire on the CROSS beyond a band (the extreme event), not on every
        # candle that remains outside it.
        if close > up[-1] and prev <= up[-2]:
            return [Signal(self.slug, "PE", "Close crossed above upper Bollinger band (fade)", sl, rr, confidence=85)]
        if close < lo[-1] and prev >= lo[-2]:
            return [Signal(self.slug, "CE", "Close crossed below lower Bollinger band (fade)", sl, rr, confidence=85)]
        return []


# --------------------------------------------------------------------------- #
# 6. Prime Scalper EMA — ATR-normalized EMA slope momentum.
# --------------------------------------------------------------------------- #
class PrimeScalperEMA(Strategy):
    slug = "prime_scalper_ema"
    title = "Prime Scalper EMA"
    timeframe = "1m"
    basis = "underlying"
    description = (
        "EMA(21) slope over 3 candles normalized by ATR(14) vs a momentum "
        "threshold, price on the right side of the EMA, plus a confirm candle. "
        "SL 1.0xATR / target 1.2xATR (managed as premium %)."
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        u = ctx.underlying
        if len(u) < 25:
            return []
        c = ind.closes(u)
        e = ind.ema(c, 21)
        a = ind.atr(u, 14)
        if e[-1] is None or a[-1] is None or a[-1] == 0:
            return []
        raw_slope = ind.slope(e, len(e) - 1, 3)
        prev_slope = ind.slope(e, len(e) - 2, 3)
        if raw_slope is None or prev_slope is None or not a[-2]:
            return []
        norm = raw_slope / a[-1]
        prev_norm = prev_slope / a[-2]
        thr = self.params.get("threshold", 0.05)
        sl, rr = self.params.get("sl_pct", 0.06), self.params.get("rr", 1.2)
        last = u[-1]
        # Blog wording: the slope CROSSES the threshold — fire on the crossing
        # candle only, so persisting momentum doesn't re-signal every bar.
        if prev_norm <= thr < norm and c[-1] > e[-1] and _green(last):
            return [Signal(self.slug, "CE", f"Bull momentum cross (norm slope {norm:.3f})", sl, rr, confidence=65)]
        if prev_norm >= -thr > norm and c[-1] < e[-1] and not _green(last):
            return [Signal(self.slug, "PE", f"Bear momentum cross (norm slope {norm:.3f})", sl, rr, confidence=65)]
        return []


# --------------------------------------------------------------------------- #
# 7. SwingKing Sniper — higher-conviction 5m trend + pullback (longer hold).
# --------------------------------------------------------------------------- #
class SwingKingSniper(Strategy):
    slug = "swingking_sniper"
    title = "SwingKing Sniper"
    timeframe = "5m"
    basis = "underlying"
    description = (
        "High-conviction 5m trend (20/50 EMA) with a pullback entry, held longer "
        "for a bigger move. Fewer signals, wider target. RR 2.0. (Zing under-"
        "specifies the exact rule; this is a faithful trend-pullback adaptation.)"
    )

    def evaluate(self, ctx: Context) -> list[Signal]:
        u = ctx.underlying
        # Length check MUST come first: `e20[-4]` was evaluated before the
        # `len(u) < 5` clause in the same or-chain, so a short series raised
        # IndexError instead of returning no signal (audit #17).
        if len(u) < 51:
            return []
        c = ind.closes(u)
        e20, e50 = ind.ema(c, 20), ind.ema(c, 50)
        if e20[-1] is None or e50[-1] is None or e20[-4] is None:
            return []
        sl = self.params.get("sl_pct", 0.12)
        rr = self.params.get("rr", 2.0)
        rising = e20[-1] > e20[-4]
        falling = e20[-1] < e20[-4]
        last, prev = u[-1], u[-2]
        touched_low = min(float(x["low"]) for x in u[-3:]) <= e20[-1]
        touched_high = max(float(x["high"]) for x in u[-3:]) >= e20[-1]
        if e20[-1] > e50[-1] and rising and touched_low and _green(last) and float(last["close"]) > float(prev["high"]):
            return [Signal(self.slug, "CE", "Strong uptrend pullback (20>50 EMA rising)", sl, rr, confidence=95,
                           max_hold_bars=self.params.get("max_hold_bars", 48))]
        if e20[-1] < e50[-1] and falling and touched_high and not _green(last) and float(last["close"]) < float(prev["low"]):
            return [Signal(self.slug, "PE", "Strong downtrend pullback (20<50 EMA falling)", sl, rr, confidence=95,
                           max_hold_bars=self.params.get("max_hold_bars", 48))]
        return []


# --------------------------------------------------------------------------- #
# 8. Booming Bulls Supertrend — on the OPTION PREMIUM candles (CE & PE variants).
# --------------------------------------------------------------------------- #
class BoomingBullsSupertrend(Strategy):
    slug = "booming_bulls_supertrend"
    title = "Booming Bulls Supertrend"
    timeframe = "1m"
    basis = "premium"
    description = (
        "Reads the ATM option premium directly: SMA(20) + fast Supertrend(10,2) "
        "+ slow Supertrend(20,3). When the premium trades above the SMA and both "
        "Supertrend lines, buy that option. Limit entry at fast-ST +0.5%. "
        "Target +7.5%, stop -5% on the premium. Independent CE and PE variants."
    )

    def _variant(self, side: str, candles: list[dict]) -> list[Signal]:
        if not candles or len(candles) < 26:
            return []
        c = ind.closes(candles)
        s = ind.sma(c, 20)
        fast_line, _ = ind.supertrend(candles, 10, 2.0)
        slow_line, _ = ind.supertrend(candles, 20, 3.0)
        if any(x[-1] is None or x[-2] is None for x in (s, fast_line, slow_line)):
            return []

        def _above(i: int) -> bool:
            return c[i] > s[i] and c[i] > fast_line[i] and c[i] > slow_line[i]

        # Blog: the ALERT stage "triggers" — fire when the bullish structure
        # becomes newly true, not on every bar it stays true.
        if _above(-1) and not _above(-2):
            return [
                Signal(
                    self.slug,
                    side,
                    f"{side} premium above SMA and both Supertrends",
                    sl_pct=self.params.get("sl_pct", 0.05),
                    rr=self.params.get("rr", 1.5),  # 7.5% target / 5% stop
                    basis="premium",
                    limit_offset_pct=0.005,
                    meta={"variant": side},
                )
            ]
        return []

    def evaluate(self, ctx: Context) -> list[Signal]:
        signals: list[Signal] = []
        for side in ("CE", "PE"):
            signals.extend(self._variant(side, ctx.premium.get(side, [])))
        return signals


# --------------------------------------------------------------------------- #
# Registry
# --------------------------------------------------------------------------- #
from .ai_strategy import AIStrategy  # noqa: E402 — avoids a circular import at module top

STRATEGY_CLASSES = [
    EMACross,
    ScalpingPulse,
    TrafficLight,
    InsideCandle,
    MeanReversionBollinger,
    PrimeScalperEMA,
    SwingKingSniper,
    BoomingBullsSupertrend,
    AIStrategy,
]

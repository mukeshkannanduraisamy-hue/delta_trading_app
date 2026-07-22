"""LLM-driven trading strategy.

Sends a compact, numeric market briefing to an LLM and asks for a directional
call as strict JSON. Deliberately conservative in three ways:

  * NON-BLOCKING. An LLM round-trip takes seconds; the trading loop must not
    stall (stop-losses depend on it). The call runs on a background thread and
    `evaluate()` only ever reads the last cached decision.
  * RATE-LIMITED. One call per AI_REFRESH_SECONDS at most — LLM calls cost money
    and add nothing at tick frequency.
  * ONE-SHOT. Each decision is consumed exactly once, so a stale opinion cannot
    re-fire every bar.

Honest note: an LLM reading OHLC has no established predictive edge. It is held
to exactly the same bar as every other strategy here — run it through
`/backtest` and `/research` before trusting it with anything.
"""

from __future__ import annotations

import json
import re
import threading
import time
from dataclasses import dataclass
from typing import Optional

from . import config
from . import indicators as ind
from .base import Context, Signal, Strategy
from .journal import journal

SYSTEM_PROMPT = (
    "You are a disciplined quantitative options trader analysing BTC. "
    "You will receive a numeric market briefing. Decide whether to buy an "
    "at-the-money CALL (CE), an at-the-money PUT (PE), or to stay flat (NONE).\n\n"
    "Rules:\n"
    "1. Reply with STRICT JSON only, no prose, no markdown fences.\n"
    '2. Schema: {"direction":"CE"|"PE"|"NONE","confidence":0.0-1.0,'
    '"reason":"<=140 chars"}\n'
    "3. Prefer NONE. Most market conditions do not offer an edge, and a "
    "long option bleeds theta while you wait. Only choose CE or PE when the "
    "evidence is genuinely one-sided.\n"
    "4. confidence is your calibrated probability that the move follows through "
    "far enough to cover the option spread. Be honest, not agreeable."
)


@dataclass
class Decision:
    direction: str          # CE | PE | NONE
    confidence: float
    reason: str
    at: float
    consumed: bool = False
    error: Optional[str] = None

    def as_dict(self) -> dict:
        return {"direction": self.direction, "confidence": self.confidence,
                "reason": self.reason, "at": self.at, "consumed": self.consumed,
                "error": self.error, "age_sec": round(time.time() - self.at, 1)}


def _client():
    """Lazily construct the OpenAI-compatible client (import cost is real).

    max_retries=0 is essential: the SDK retries timeouts twice by default, so an
    unresponsive model would burn timeout x3 before the fallback model is even
    tried. We want to fail fast and move down the chain.
    """
    from openai import OpenAI
    return OpenAI(base_url=config.AI_BASE_URL, api_key=config.AI_API_KEY,
                  timeout=config.AI_TIMEOUT_SECONDS, max_retries=0)


def _parse_decision(text: str) -> dict:
    """Extract the JSON object from a model reply, tolerating fences/prose."""
    text = (text or "").strip()
    text = re.sub(r"^```(?:json)?|```$", "", text, flags=re.MULTILINE).strip()
    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        pass
    m = re.search(r"\{.*\}", text, re.DOTALL)  # first JSON object anywhere
    if m:
        try:
            return json.loads(m.group(0))
        except (json.JSONDecodeError, ValueError):
            pass
    raise ValueError(f"unparseable model reply: {text[:200]}")


def build_briefing(candles: list[dict], spot: float) -> str:
    """Compact numeric snapshot — no future data, only closed candles."""
    c = ind.closes(candles)
    e9, e21, e50 = ind.ema(c, 9), ind.ema(c, 21), ind.ema(c, 50)
    atr = ind.atr(candles, 14)
    mid, up, lo = ind.bollinger(c, 20, 2.0)
    recent = candles[-12:]

    def pct(a, b):
        return round((a / b - 1) * 100, 2) if (a and b) else None

    lines = [
        f"asset=BTC spot={spot:.1f}",
        f"ema9={e9[-1]:.1f} ema21={e21[-1]:.1f} ema50={e50[-1]:.1f}"
        if all(x[-1] is not None for x in (e9, e21, e50)) else "ema=warmup",
        f"atr14={atr[-1]:.1f} ({(atr[-1]/spot*100):.2f}% of spot)" if atr[-1] else "atr=warmup",
        f"bollinger_upper={up[-1]:.1f} mid={mid[-1]:.1f} lower={lo[-1]:.1f}"
        if up[-1] is not None else "bollinger=warmup",
        f"close_vs_ema21={pct(c[-1], e21[-1])}%" if e21[-1] else "",
        f"change_12bars={pct(c[-1], c[-12])}%" if len(c) >= 12 else "",
        f"range_high_50={max(float(x['high']) for x in candles[-50:]):.1f}"
        f" low_50={min(float(x['low']) for x in candles[-50:]):.1f}" if len(candles) >= 50 else "",
        "last_12_closed_candles (o,h,l,c,v):",
    ]
    for x in recent:
        lines.append(
            f"  {float(x['open']):.1f},{float(x['high']):.1f},"
            f"{float(x['low']):.1f},{float(x['close']):.1f},{float(x.get('volume') or 0):.0f}"
        )
    return "\n".join(l for l in lines if l)


class AIStrategy(Strategy):
    slug = "ai_llm"
    title = "AI (LLM) Signal"
    timeframe = "15m"
    basis = "underlying"
    lookback = 120
    # The LLM is queried live, so a historical replay would ask it about TODAY's
    # market while pretending to stand at a past bar. Excluded from backtests.
    backtestable = False
    BLACKLIST_SECONDS = 1800  # skip a hanging model for 30 min
    description = (
        "Sends a numeric market briefing (EMAs, ATR, Bollinger, recent candles) "
        "to an LLM and asks for a CE/PE/NONE call with a calibrated confidence. "
        "Runs off the trading loop on a background thread; one decision is acted "
        "on at most once, and only above the confidence threshold."
    )

    def __init__(self, enabled: bool = False, **params) -> None:
        super().__init__(enabled, **params)
        self._decision: Optional[Decision] = None
        self._lock = threading.Lock()
        self._inflight = False
        self._last_attempt = 0.0
        self.calls = 0
        self.errors = 0
        self.last_model: Optional[str] = None
        # model -> unix time until which it is skipped (circuit breaker)
        self._blacklist: dict[str, float] = {}

    # ------------------------------------------------------------------ #
    def _call(self, model: str, briefing: str) -> str:
        resp = _client().chat.completions.create(
            model=model,
            messages=[{"role": "system", "content": SYSTEM_PROMPT},
                      {"role": "user", "content": briefing}],
            temperature=config.AI_TEMPERATURE,
            top_p=1,
            max_tokens=400,
            seed=42,
        )
        return (resp.choices[0].message.content or "") if resp.choices else ""

    def _refresh(self, briefing: str) -> None:
        """Background LLM call — never runs on the trading loop.

        Walks the model chain: several hosted models accept the connection and
        then never respond, so a timeout on the primary must not kill the signal.
        """
        try:
            text, used, errs = "", None, []
            now = time.time()
            chain = [config.AI_MODEL, *config.AI_FALLBACK_MODELS]
            live = [m for m in chain if self._blacklist.get(m, 0) < now]
            if not live:  # every model is cooling down — retry the whole chain
                live = chain
            for model in live:
                try:
                    text = self._call(model, briefing)
                    if text.strip():
                        used = model
                        self._blacklist.pop(model, None)
                        break
                    errs.append(f"{model}: empty reply")
                    self._blacklist[model] = now + self.BLACKLIST_SECONDS
                except Exception as exc:  # noqa: BLE001 — try the next model
                    errs.append(f"{model}: {type(exc).__name__}")
                    # Circuit breaker: a model that hangs costs a full timeout
                    # every refresh, so stop asking it for a while.
                    self._blacklist[model] = now + self.BLACKLIST_SECONDS
            if used is None:
                raise RuntimeError("all models failed -> " + "; ".join(errs[:4]))
            self.last_model = used
            data = _parse_decision(text)
            direction = str(data.get("direction", "NONE")).upper()
            if direction not in ("CE", "PE", "NONE"):
                direction = "NONE"
            conf = float(data.get("confidence", 0) or 0)
            d = Decision(direction=direction, confidence=max(0.0, min(conf, 1.0)),
                         reason=str(data.get("reason", ""))[:140], at=time.time())
            with self._lock:
                self._decision = d
                self.calls += 1
            journal.record("ai_decision", {"strategy": self.slug, "direction": d.direction,
                                           "confidence": d.confidence, "reason": d.reason,
                                           "model": used})
        except Exception as exc:  # noqa: BLE001 — the loop must survive any AI failure
            with self._lock:
                self._decision = Decision("NONE", 0.0, "", time.time(), True, str(exc)[:200])
                self.errors += 1
            journal.record("ai_error", {"strategy": self.slug, "error": str(exc)[:200]})
        finally:
            with self._lock:
                self._inflight = False

    def _maybe_refresh(self, ctx: Context) -> None:
        now = time.time()
        with self._lock:
            if self._inflight:
                return
            if now - self._last_attempt < config.AI_REFRESH_SECONDS:
                return
            if not config.AI_API_KEY:
                return
            self._inflight = True
            self._last_attempt = now
        briefing = build_briefing(ctx.underlying, ctx.spot)
        threading.Thread(target=self._refresh, args=(briefing,), daemon=True).start()

    # ------------------------------------------------------------------ #
    def evaluate(self, ctx: Context) -> list[Signal]:
        if len(ctx.underlying) < 55:
            return []
        self._maybe_refresh(ctx)

        with self._lock:
            d = self._decision
            if (not d or d.consumed or d.error
                    or d.direction not in ("CE", "PE")
                    or d.confidence < config.AI_MIN_CONFIDENCE):
                return []
            # Ignore an opinion older than two refresh windows — the market has
            # moved on and a stale call is worse than no call.
            if time.time() - d.at > config.AI_REFRESH_SECONDS * 2:
                return []
            d.consumed = True
            direction, conf, reason = d.direction, d.confidence, d.reason

        return [Signal(
            self.slug, direction,
            f"AI {conf:.0%}: {reason}"[:160],
            confidence=max(1, min(100, int(conf * 100))),
            sl_pct=self.params.get("sl_pct", 0.10),
            rr=self.params.get("rr", 1.5),
            meta={"confidence": conf, "model": config.AI_MODEL},
        )]

    def info(self) -> dict:
        base = super().info()
        with self._lock:
            base["ai"] = {
                "model": config.AI_MODEL,
                "model_used": self.last_model,
                "fallbacks": config.AI_FALLBACK_MODELS,
                "has_key": bool(config.AI_API_KEY),
                "calls": self.calls,
                "errors": self.errors,
                "last_decision": self._decision.as_dict() if self._decision else None,
            }
        return base

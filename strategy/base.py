"""Strategy framework: the Signal contract and the Strategy base class."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .indicators import Candle


@dataclass
class Signal:
    """A request to open ONE option position.

    Risk is expressed as a percentage of the option premium so it executes
    uniformly across strategies (Zing's Nifty point targets don't translate to
    crypto premiums; the risk:reward ratio is preserved instead).
    """

    strategy: str
    direction: str          # "CE" (buy call) | "PE" (buy put)
    reason: str
    sl_pct: float = 0.05     # stop loss as a fraction of entry premium
    rr: float = 1.5          # reward:risk -> target_pct = sl_pct * rr
    basis: str = "underlying"  # "underlying" | "premium"
    limit_offset_pct: Optional[float] = None  # limit-entry offset above premium
    max_hold_bars: Optional[int] = None
    meta: dict = field(default_factory=dict)

    @property
    def target_pct(self) -> float:
        return self.sl_pct * self.rr


@dataclass
class Context:
    """Everything a strategy needs to evaluate one poll cycle."""

    underlying: list[Candle]                 # perp candles at the strategy tf
    spot: float
    premium: dict[str, list[Candle]] = field(default_factory=dict)  # {"CE":[],"PE":[]}


class Strategy:
    """Base class. Subclasses set metadata and implement `evaluate`."""

    slug: str = "base"
    title: str = "Base Strategy"
    timeframe: str = "1m"          # candle resolution driving the signal
    basis: str = "underlying"      # "underlying" | "premium"
    lookback: int = 120            # candles to fetch
    description: str = ""
    # False for strategies that consult a live external service: replaying them
    # would query that service with TODAY's state while pretending to be at a
    # historical bar (lookahead), and cost real money per bar.
    backtestable: bool = True

    def __init__(self, enabled: bool = False, **params) -> None:
        self.enabled = enabled
        self.params = params

    def evaluate(self, ctx: Context) -> list[Signal]:  # pragma: no cover - abstract
        raise NotImplementedError

    def info(self) -> dict:
        return {
            "slug": self.slug,
            "title": self.title,
            "timeframe": self.timeframe,
            "basis": self.basis,
            "enabled": self.enabled,
            "description": self.description,
        }

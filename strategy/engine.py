"""The strategy engine: polls market data, evaluates every enabled strategy,
routes signals to the executor, and manages open positions.

Designed to run as a background asyncio task inside the FastAPI app. All the
blocking REST work happens in a worker thread so the event loop stays free.
"""

from __future__ import annotations

import asyncio
import time
import traceback
from typing import Optional

from . import config
from .base import Context
from .delta_client import client
from .executor import Executor
from .journal import journal
from .market_data import OptionResolver
from .zing_strategies import STRATEGY_CLASSES

_UNIT = {"m": 60, "h": 3600, "d": 86400, "w": 604800}


def _bar_seconds(resolution: str) -> int:
    return int(resolution[:-1]) * _UNIT.get(resolution[-1], 60)


class Engine:
    def __init__(self) -> None:
        self.asset = config.ASSET
        self.underlying = config.UNDERLYING_SYMBOL
        self.resolver = OptionResolver(self.asset)
        self.executor = Executor(self.resolver)
        self.strategies = [cls() for cls in STRATEGY_CLASSES]
        self.by_slug = {s.slug: s for s in self.strategies}

        self.running = False
        self._task: Optional[asyncio.Task] = None
        self.last_cycle_at: float = 0.0
        self.last_error: Optional[str] = None
        self.last_spot: Optional[float] = None
        self.last_snapshots: list[dict] = []
        self.cycles = 0
        self.signals_generated = 0

    # ------------------------------------------------------------------ #
    # Strategy toggles
    # ------------------------------------------------------------------ #
    def set_enabled(self, slug: str, enabled: bool) -> bool:
        s = self.by_slug.get(slug)
        if not s:
            return False
        s.enabled = enabled
        journal.record("config", {"strategy": slug, "enabled": enabled})
        return True

    def enabled_strategies(self):
        return [s for s in self.strategies if s.enabled]

    # ------------------------------------------------------------------ #
    # Market data helpers (blocking; called inside a worker thread)
    # ------------------------------------------------------------------ #
    def _get_spot(self) -> Optional[float]:
        try:
            t = client.ticker(self.underlying, base=config.PROD_BASE)
            sp = t.get("spot_price") or t.get("mark_price") or t.get("close")
            return float(sp) if sp is not None else None
        except Exception:  # noqa: BLE001
            return None

    def _cycle(self) -> None:
        spot = self._get_spot()
        if spot is None:
            self.last_error = "could not fetch spot price"
            return
        self.last_spot = spot

        enabled = self.enabled_strategies()
        if not enabled:
            self.last_snapshots = self.executor.manage()
            return

        atm = self.resolver.atm(spot)  # {"CE": OptionQuote, "PE": OptionQuote}

        # Fetch underlying candles once per distinct timeframe this cycle.
        tf_cache: dict[str, list] = {}
        for s in enabled:
            if s.basis == "underlying" and s.timeframe not in tf_cache:
                tf_cache[s.timeframe] = client.recent_candles(
                    self.underlying, s.timeframe, count=s.lookback, base=config.PROD_BASE
                )

        # Premium candles for premium-basis strategies (ATM CE & PE).
        premium_cache: dict[str, dict[str, list]] = {}
        for s in enabled:
            if s.basis == "premium":
                pc: dict[str, list] = {}
                for side in ("CE", "PE"):
                    q = atm.get(side)
                    if q:
                        pc[side] = self.resolver.premium_candles(q.symbol, s.timeframe, s.lookback)
                premium_cache[s.slug] = pc

        for s in enabled:
            try:
                if s.basis == "underlying":
                    candles = tf_cache.get(s.timeframe) or []
                    if len(candles) < 5:
                        continue
                    ctx = Context(underlying=candles, spot=spot)
                else:
                    ctx = Context(underlying=[], spot=spot, premium=premium_cache.get(s.slug, {}))

                for sig in s.evaluate(ctx):
                    self.signals_generated += 1
                    quote = atm.get(sig.direction)
                    if not quote:
                        journal.record("skip", {"strategy": sig.strategy,
                                                "direction": sig.direction,
                                                "reason": "no ATM contract"})
                        continue
                    self.executor.open(sig, quote, _bar_seconds(s.timeframe))
            except Exception as exc:  # noqa: BLE001 — isolate a bad strategy
                journal.record("error", {"strategy": s.slug, "error": repr(exc)})

        # Manage exits after processing entries.
        self.last_snapshots = self.executor.manage()

    async def _run(self) -> None:
        journal.record("engine", {"event": "start", **config.summary()})
        while self.running:
            try:
                await asyncio.to_thread(self._cycle)
                self.last_error = None
            except Exception:  # noqa: BLE001
                self.last_error = traceback.format_exc(limit=3)
                journal.record("error", {"scope": "cycle", "error": self.last_error})
            self.cycles += 1
            self.last_cycle_at = time.time()
            await asyncio.sleep(config.POLL_SECONDS)
        journal.record("engine", {"event": "stop"})

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self.running:
            return False
        self.running = True
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self, flatten: bool = False) -> None:
        self.running = False
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if flatten:
            self.executor.flatten_all("engine_stop")

    # ------------------------------------------------------------------ #
    # Status for the UI
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        return {
            "running": self.running,
            "config": config.summary(),
            "last_cycle_at": self.last_cycle_at,
            "cycles": self.cycles,
            "signals_generated": self.signals_generated,
            "last_error": self.last_error,
            "spot": self.last_spot,
            "stats": self.executor.stats(),
            "positions": self.last_snapshots,
            "strategies": [s.info() for s in self.strategies],
        }


engine = Engine()

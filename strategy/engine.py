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
from .delta_client import DeltaAuthError, DeltaError, DeltaRateLimited, client
from .eventbus import bus as bus_events
from .executor import Executor
from .journal import journal
from .market_data import OptionResolver
from .pricebus import bus
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

        # Boot-time enablement from ENGINE_ENABLED_STRATEGIES ("all", "none",
        # or a comma-separated slug list).
        wanted = config.ENABLED_STRATEGIES
        if wanted == "all":
            for s in self.strategies:
                s.enabled = True
        elif wanted not in ("", "none"):
            slugs = {w.strip() for w in wanted.split(",") if w.strip()}
            for s in self.strategies:
                s.enabled = s.slug in slugs

        self.running = False
        self._task: Optional[asyncio.Task] = None
        self.last_cycle_at: float = 0.0
        self.last_error: Optional[str] = None
        self.last_spot: Optional[float] = None
        self.last_snapshots: list[dict] = []
        self.cycles = 0
        self.signals_generated = 0
        # Set when the exchange rate-limits us; the poll loop sleeps until then.
        self._rate_limited_until: float = 0.0
        # Per-strategy gate: the closed-candle time (or wall-clock bar index for
        # premium strategies) last evaluated, so each strategy fires at most
        # once per closed candle instead of once per poll.
        self._last_eval: dict[str, int] = {}
        # Per-timeframe bar clock, so a cycle can skip all fetching when no new
        # candle has closed since the last evaluation.
        self._last_bar_seen: dict[str, int] = {}

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
        # Live WS tick first (in-memory, no network); REST only as a fallback.
        live = bus.spot(self.underlying)
        if live is not None:
            return live
        try:
            t = client.ticker(self.underlying, base=config.PROD_BASE)
            sp = t.get("spot_price") or t.get("mark_price") or t.get("close")
            return float(sp) if sp is not None else None
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _closed_only(candles: list, bar_secs: int) -> list:
        """Drop the still-forming bar so strategies only ever see closed candles."""
        if candles and candles[-1].get("time", 0) + bar_secs > time.time():
            return candles[:-1]
        return candles

    def _cycle(self) -> None:
        # Manage exits FIRST: TP/SL/expiry protection must run even when the
        # signal-side (production) API is down — the execution venue is
        # independent of it.
        self.last_snapshots = self.executor.manage()
        if self.cycles % 20 == 0:
            self.executor.reconcile()

        spot = self._get_spot()
        if spot is None:
            self.last_error = "could not fetch spot price (exits still managed)"
            return
        self.last_spot = spot

        enabled = self.enabled_strategies()
        if not enabled:
            return

        now = time.time()

        # Bar-clock gate BEFORE any fetching. Strategies evaluate at most once
        # per closed candle, so on a 1m timeframe with a 5s poll, 11 of every 12
        # cycles have nothing new to look at — fetching candles or the option
        # chain in those cycles is pure waste.
        due_tfs: set[str] = set()
        for s in enabled:
            bar = int(now // _bar_seconds(s.timeframe))
            if self._last_bar_seen.get(s.timeframe) != bar:
                due_tfs.add(s.timeframe)
        if not due_tfs:
            return  # nothing has closed since the last evaluation

        # Fetch FIRST, then consume the gate — and only for timeframes whose
        # data actually arrived. Marking the gate before fetching meant a single
        # transient error (429, 5xx, timeout) silently discarded that entire
        # bar's signals for every strategy on the timeframe: the next poll saw
        # the same wall-clock bar and skipped, and by the time the gate advanced
        # the bar was gone (audit #8).
        tf_cache: dict[str, list] = {}
        fetched_tfs: set[str] = set()
        for tf in sorted(due_tfs):
            underlying_strats = [s for s in enabled
                                 if s.basis == "underlying" and s.timeframe == tf]
            if not underlying_strats:
                fetched_tfs.add(tf)      # premium-only timeframe: nothing to fetch
                continue
            # Use the LARGEST lookback on this timeframe. Previously whichever
            # strategy happened to be enumerated first set it, so a 50-EMA
            # strategy could be starved by a 9-EMA one sharing the timeframe.
            lookback = max(s.lookback for s in underlying_strats)
            try:
                raw = client.recent_candles(
                    self.underlying, tf, count=lookback, base=config.PROD_BASE
                )
            except Exception as exc:  # noqa: BLE001
                journal.record("error", {
                    "scope": f"candles:{tf}", "error": repr(exc),
                    "note": "bar gate NOT consumed — will retry next poll"})
                continue
            tf_cache[tf] = self._closed_only(raw, _bar_seconds(tf))
            fetched_tfs.add(tf)

        if not fetched_tfs:
            return
        for tf in fetched_tfs:
            self._last_bar_seen[tf] = int(now // _bar_seconds(tf))
        due_tfs = fetched_tfs

        # ATM chain is resolved lazily — only if a due strategy could act on it.
        atm: dict = {}
        def _atm() -> dict:
            nonlocal atm
            if not atm:
                atm = self.resolver.atm(spot)
            return atm

        # Premium candles for premium-basis strategies (ATM CE & PE).
        premium_cache: dict[str, dict[str, list]] = {}
        for s in enabled:
            if s.basis == "premium" and s.timeframe in due_tfs:
                bar_secs = _bar_seconds(s.timeframe)
                if self._last_eval.get(s.slug) == int(now // bar_secs):
                    continue  # same bar as last evaluation — skip the fetch too
                pc: dict[str, list] = {}
                for side in ("CE", "PE"):
                    q = _atm().get(side)
                    if q:
                        raw = self.resolver.premium_candles(q.symbol, s.timeframe, s.lookback)
                        pc[side] = self._closed_only(raw, bar_secs)
                premium_cache[s.slug] = pc

        for s in enabled:
            try:
                bar_secs = _bar_seconds(s.timeframe)
                if s.basis == "underlying":
                    candles = tf_cache.get(s.timeframe) or []
                    if len(candles) < 5:
                        continue
                    # Gate: evaluate at most once per closed candle.
                    gate = int(candles[-1].get("time", 0))
                    if self._last_eval.get(s.slug) == gate:
                        continue
                    self._last_eval[s.slug] = gate
                    ctx = Context(underlying=candles, spot=spot)
                else:
                    if not premium_cache.get(s.slug):
                        continue  # bar clock hasn't advanced or no ATM premium
                    # data — leave the gate unconsumed so the bar can retry.
                    gate = int(now // bar_secs)
                    if self._last_eval.get(s.slug) == gate:
                        continue
                    self._last_eval[s.slug] = gate
                    ctx = Context(underlying=[], spot=spot, premium=premium_cache.get(s.slug, {}))

                for sig in s.evaluate(ctx):
                    self.signals_generated += 1
                    quote = _atm().get(sig.direction)
                    if not quote:
                        journal.record("skip", {"strategy": sig.strategy,
                                                "direction": sig.direction,
                                                "reason": "no ATM contract"})
                        continue
                    opened = self.executor.open(sig, quote, bar_secs)
                    if opened:
                        self.last_snapshots = self.last_snapshots + [opened]
            except Exception as exc:  # noqa: BLE001 — isolate a bad strategy
                journal.record("error", {"strategy": s.slug, "error": repr(exc)})

    async def _run(self) -> None:
        journal.record("engine", {"event": "start", **config.summary()})
        while self.running:
            try:
                await asyncio.to_thread(self._cycle)
                self.last_error = None
                self._rate_limited_until = 0.0
            except DeltaRateLimited as exc:
                # Honour the exchange's own reset window. Retrying sooner just
                # extends the ban and burns more quota.
                self._rate_limited_until = time.time() + exc.retry_after
                self.last_error = f"rate limited, backing off {exc.retry_after:.0f}s"
                journal.record("rate_limit", {"retry_after": exc.retry_after})
            except DeltaAuthError as exc:
                # Permanent: a bad key or an un-whitelisted IP cannot be retried
                # into working. Surface it loudly rather than spinning.
                self.last_error = f"AUTH FAILURE: {exc}"
                journal.record("error", {"scope": "auth", "error": str(exc)})
            except Exception:  # noqa: BLE001
                self.last_error = traceback.format_exc(limit=3)
                journal.record("error", {"scope": "cycle", "error": self.last_error})
            self.cycles += 1
            self.last_cycle_at = time.time()
            # Push live state to dashboards. Coalesced downstream, so a fast
            # poll cannot flood the UI.
            try:
                bus_events.publish("engine", {
                    "running": self.running, "cycles": self.cycles,
                    "signals_generated": self.signals_generated,
                    "spot": self.last_spot, "last_error": self.last_error,
                    "stats": self.executor.stats(),
                    "positions": self.last_snapshots,
                })
            except Exception:  # noqa: BLE001
                pass
            delay = max(config.POLL_SECONDS, self._rate_limited_until - time.time())
            await asyncio.sleep(delay)
        journal.record("engine", {"event": "stop"})

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #
    def preflight(self) -> Optional[str]:
        """Reasons the engine must NOT start, or None if it is safe to."""
        if config.EXECUTION_MODE == "paper":
            return None
            
        if not (config.API_KEY and config.API_SECRET):
            return ("DELTA_API_KEY / DELTA_API_SECRET are not set. This engine "
                    "places real testnet orders and has no simulated fallback.")
        try:
            client.profile(retries=1)
        except DeltaAuthError as exc:
            return f"API credentials rejected by the exchange: {exc}"
        except DeltaError as exc:
            # Reachability problem rather than a credential problem. Allow the
            # start — exits must be able to run even during an API wobble — but
            # surface it so it is not mistaken for a healthy engine.
            self.last_error = f"preflight warning: {exc}"
            journal.record("engine", {"event": "preflight_warning", "error": str(exc)})
        return None

    def start(self) -> bool:
        # Guard both flags: a start() racing a still-draining stop() must not
        # spawn a second engine loop (the old loop would see running=True again
        # and never exit).
        if self.running or (self._task and not self._task.done()):
            return False
        blocker = self.preflight()
        if blocker:
            self.last_error = f"REFUSED TO START: {blocker}"
            journal.record("engine", {"event": "start_refused", "reason": blocker})
            print(f"[engine] REFUSED TO START — {blocker}")
            return False
        journal.record("engine", {"event": "start_armed", "mode": config.EXECUTION_MODE,
                                  "contracts": config.CONTRACTS,
                                  "note": "PAPER TRADING — simulating orders internally"})
        self.running = True
        self._task = asyncio.create_task(self._run())
        return True

    async def stop(self, flatten: bool = False) -> None:
        self.running = False
        if self._task:
            await asyncio.gather(self._task, return_exceptions=True)
            self._task = None
        if flatten:
            # flatten_all does blocking REST order flow — keep it off the loop.
            await asyncio.to_thread(self.executor.flatten_all, "engine_stop")

    # ------------------------------------------------------------------ #
    # Status for the UI
    # ------------------------------------------------------------------ #
    def status(self) -> dict:
        # A stopped engine's last_snapshots would go stale (e.g. after a manual
        # flatten via the API) — serve the executor's live view instead.
        positions = self.last_snapshots if self.running else self.executor.snapshots()
        return {
            "running": self.running,
            # Exits (stop / target / time / expiry) keep running via the app's
            # background reconciler even when the engine is stopped — "stopped"
            # means "opens nothing new", not "abandons open positions".
            "exits_active": True,
            "config": config.summary(),
            "last_cycle_at": self.last_cycle_at,
            "cycles": self.cycles,
            "signals_generated": self.signals_generated,
            "last_error": self.last_error,
            "spot": self.last_spot,
            "stats": self.executor.stats(),
            "positions": positions,
            "strategies": [s.info() for s in self.strategies],
        }


engine = Engine()

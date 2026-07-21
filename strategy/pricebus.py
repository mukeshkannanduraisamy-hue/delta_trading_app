"""In-memory price bus shared between the WebSocket feed and the trading engine.

The engine used to REST-poll for spot and for every open position's premium,
which added hundreds of milliseconds of latency per read and burned rate limit.
The app already holds ONE upstream WebSocket to Delta, so instead the consumer
publishes every tick here and the engine reads it from memory — effectively zero
latency, and stop-losses react on the tick rather than on the next poll.

Lives in `strategy/` (not main.py) so the engine can import it without a
circular dependency on the web app.
"""

from __future__ import annotations

import threading
import time
from typing import Optional


class PriceBus:
    # A tick older than this is treated as stale and the caller falls back to
    # REST — better a slow correct price than a fast wrong one.
    MAX_AGE = 15.0

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._prices: dict[str, dict] = {}
        self._wanted: set[str] = set()
        self._subscribed: set[str] = set()
        self.updates = 0

    # -- publish (called from the WS consumer) -------------------------- #
    def update(self, symbol: str, **fields) -> None:
        if not symbol:
            return
        with self._lock:
            row = self._prices.setdefault(symbol, {})
            row.update({k: v for k, v in fields.items() if v is not None})
            row["ts"] = time.time()
            self.updates += 1

    # -- read (called from the engine / executor) ----------------------- #
    def get(self, symbol: str) -> Optional[dict]:
        with self._lock:
            row = self._prices.get(symbol)
            if not row:
                return None
            if time.time() - row.get("ts", 0) > self.MAX_AGE:
                return None
            return dict(row)

    def bid(self, symbol: str) -> Optional[float]:
        """Best bid ONLY — what a seller actually receives.

        Deliberately does NOT fall back to mark price. Mark is a mid-like
        reference; using it as an exit price hands paper trading roughly half the
        bid/ask spread as phantom profit, and on a book this wide that bias is
        large enough to invert an edge verdict (audit #13). No bid means the
        contract is genuinely unsellable — callers must handle None, not be
        handed a price that does not exist.
        """
        row = self.get(symbol)
        if not row:
            return None
        b = row.get("best_bid")
        return b if (b is not None and b > 0) else None

    def ask(self, symbol: str) -> Optional[float]:
        """Best ask ONLY — what a buyer actually pays."""
        row = self.get(symbol)
        if not row:
            return None
        a = row.get("best_ask")
        return a if (a is not None and a > 0) else None

    def mark(self, symbol: str) -> Optional[float]:
        """Mark price. Valid for display and risk; NEVER for simulating a fill."""
        row = self.get(symbol)
        return row.get("mark_price") if row else None

    def spot(self, symbol: str) -> Optional[float]:
        row = self.get(symbol)
        if not row:
            return None
        return row.get("spot_price") or row.get("mark_price") or row.get("close")

    # -- dynamic subscription ------------------------------------------ #
    def want(self, *symbols: str) -> None:
        """Register interest in a symbol so the WS consumer subscribes to it."""
        with self._lock:
            for s in symbols:
                if s:
                    self._wanted.add(s)

    def pending(self) -> list[str]:
        with self._lock:
            return sorted(self._wanted - self._subscribed)

    def mark_subscribed(self, symbols: list[str]) -> None:
        with self._lock:
            self._subscribed.update(symbols)

    def reset_subscriptions(self) -> None:
        """Called when the upstream socket reconnects — re-subscribe everything."""
        with self._lock:
            self._subscribed.clear()

    def stats(self) -> dict:
        with self._lock:
            fresh = sum(1 for r in self._prices.values()
                        if time.time() - r.get("ts", 0) <= self.MAX_AGE)
            return {"symbols": len(self._prices), "fresh": fresh,
                    "wanted": len(self._wanted), "subscribed": len(self._subscribed),
                    "updates": self.updates}


bus = PriceBus()

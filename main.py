"""
Delta Exchange India — Live Trading Web App
===========================================
PHASE 1: Project setup + live price ticker (BTCUSD, ETHUSD, SOLUSD)

Backend responsibilities in this phase:
  * Serve the dashboard page.
  * Maintain ONE upstream WebSocket connection to Delta Exchange and
    subscribe to the public `v2/ticker` channel.
  * Fan out every ticker update to all connected browser clients.
  * Auto-reconnect to Delta if the upstream connection drops.
  * Expose a REST fallback for a single ticker.

Public market data needs no authentication. No API keys are used in Phase 1.
"""

import asyncio
import contextlib
import json
import random
import time
from collections import deque
from contextlib import asynccontextmanager
from pathlib import Path

import requests
import websockets
from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates

# Phase 4 — Zing strategy engine (paper / testnet-demo options trading).
from strategy import backtest as strat_backtest
from strategy import config as strat_config
from strategy import research as strat_research
from strategy import store as strat_store
from strategy import volatility as strat_vol
from strategy.delta_client import DeltaError, client as delta_client
from strategy.account import sync as account_sync
from strategy.engine import engine
from strategy.eventbus import bus as event_bus
from strategy.journal import journal
from strategy.pricebus import bus as price_bus

# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
BASE_DIR = Path(__file__).resolve().parent
DELTA_REST = "https://api.india.delta.exchange"
DELTA_WS = "wss://socket.india.delta.exchange"

# Symbols shown as live ticker cards on the dashboard.
SYMBOLS = ["BTCUSD", "ETHUSD", "SOLUSD"]

# Phase 2 — the dashboard chart focuses on BTCUSD across these timeframes.
CHART_SYMBOL = "BTCUSD"
CHART_RESOLUTIONS = ["1m", "3m", "5m", "15m", "30m", "1h", "4h", "1d"]

# Delta candlestick resolutions accepted by /v2/history/candles and the WS
# candlestick channels (superset of the UI timeframes above).
VALID_RESOLUTIONS = {
    "1m", "3m", "5m", "15m", "30m", "1h", "2h", "4h", "6h",
    "1d", "7d", "1w", "2w", "30d",
}

MAX_RECENT_TRADES = 30  # cached per chart symbol for late-joining browsers

templates = Jinja2Templates(directory=str(BASE_DIR / "templates"))


# --------------------------------------------------------------------------- #
# Client hub — tracks connected browsers and broadcasts to them
# --------------------------------------------------------------------------- #
class ClientHub:
    """Keeps the set of connected browser WebSockets and broadcasts messages.

    Also caches the latest normalized ticker per symbol so a freshly opened
    browser gets an immediate snapshot instead of waiting for the next tick.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.last_ticker: dict[str, dict] = {}
        self.recent_trades: deque = deque(maxlen=MAX_RECENT_TRADES)
        self.upstream_connected: bool = False

    async def register(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, message: dict) -> None:
        payload = json.dumps(message)
        async with self.lock:
            targets = list(self.clients)
        dead: list[WebSocket] = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)


hub = ClientHub()


# --------------------------------------------------------------------------- #
# Ticker normalization
# --------------------------------------------------------------------------- #
def _fnum(value):
    """Best-effort float conversion; Delta returns many numbers as strings."""
    try:
        if value is None:
            return None
        return float(value)
    except (TypeError, ValueError):
        return None


def normalize_ticker(raw: dict) -> dict:
    """Convert a Delta `v2/ticker` payload (WS or REST) into a clean shape.

    The live WS `v2/ticker` channel emits the same verbose object as the REST
    `/v2/tickers/{symbol}` endpoint (verified against the live API), so one
    normalizer serves both paths.
    """
    quotes = raw.get("quotes") or {}
    band = raw.get("price_band") or {}
    return {
        "type": "ticker",
        "symbol": raw.get("symbol"),
        "spot_price": _fnum(raw.get("spot_price")),
        "mark_price": _fnum(raw.get("mark_price")),
        "best_bid": _fnum(quotes.get("best_bid")),
        "best_ask": _fnum(quotes.get("best_ask")),
        "bid_size": _fnum(quotes.get("bid_size")),
        "ask_size": _fnum(quotes.get("ask_size")),
        "high": _fnum(raw.get("high")),
        "low": _fnum(raw.get("low")),
        "open": _fnum(raw.get("open")),
        "close": _fnum(raw.get("close")),
        "volume": _fnum(raw.get("volume")),
        "turnover_usd": _fnum(raw.get("turnover_usd")),
        "oi": _fnum(raw.get("oi_contracts")),
        "oi_value_usd": _fnum(raw.get("oi_value_usd")),
        "change_24h": _fnum(raw.get("ltp_change_24h")),  # already a percent
        "funding_rate": _fnum(raw.get("funding_rate")),
        "price_band_low": _fnum(band.get("lower_limit")),
        "price_band_high": _fnum(band.get("upper_limit")),
        "timestamp": raw.get("timestamp"),  # microseconds since epoch
    }


def normalize_candle(raw: dict) -> dict:
    """Convert a WS `candlestick_{res}` payload into a chart-ready update.

    Delta sends `candle_start_time` in microseconds; TradingView Lightweight
    Charts expects the bar `time` in whole seconds.
    """
    start_us = raw.get("candle_start_time")
    time_s = int(start_us / 1_000_000) if start_us else None
    return {
        "type": "candle",
        "symbol": raw.get("symbol"),
        "resolution": raw.get("resolution"),
        "time": time_s,
        "open": _fnum(raw.get("open")),
        "high": _fnum(raw.get("high")),
        "low": _fnum(raw.get("low")),
        "close": _fnum(raw.get("close")),
        "volume": _fnum(raw.get("volume")),
    }


def normalize_trade(raw: dict, symbol: str | None = None) -> dict:
    """Convert an `all_trades` tick (or a snapshot entry) into a clean trade.

    The market aggressor is the taker: buyer_role == 'taker' -> BUY (green),
    seller_role == 'taker' -> SELL (red).
    """
    side = "buy" if raw.get("buyer_role") == "taker" else "sell"
    return {
        "symbol": raw.get("symbol") or symbol,
        "price": _fnum(raw.get("price")),
        "size": _fnum(raw.get("size")),
        "side": side,
        "timestamp": raw.get("timestamp"),  # microseconds
    }


# --------------------------------------------------------------------------- #
# Option chain helpers (Phase 3)
# --------------------------------------------------------------------------- #
def _iv_pct(value):
    """Delta reports IV as a fraction (0.2495); show it as a percent (24.95)."""
    n = _fnum(value)
    return n * 100 if n is not None else None


def build_option_side(t: dict) -> dict:
    """Extract one side (call or put) of a strike from an option ticker."""
    q = t.get("quotes") or {}
    g = t.get("greeks") or {}
    return {
        "symbol": t.get("symbol"),
        "mark_price": _fnum(t.get("mark_price")),
        "bid": _fnum(q.get("best_bid")),
        "ask": _fnum(q.get("best_ask")),
        "bid_size": _fnum(q.get("bid_size")),
        "ask_size": _fnum(q.get("ask_size")),
        "iv": _iv_pct(q.get("mark_iv")),
        "bid_iv": _iv_pct(q.get("bid_iv")),
        "ask_iv": _iv_pct(q.get("ask_iv")),
        "delta": _fnum(g.get("delta")),
        "gamma": _fnum(g.get("gamma")),
        "theta": _fnum(g.get("theta")),
        "vega": _fnum(g.get("vega")),
        "rho": _fnum(g.get("rho")),
        "oi": _fnum(t.get("oi_contracts")),
        "volume": _fnum(t.get("volume")),
        "ltp_change_24h": _fnum(t.get("ltp_change_24h")),
    }


def compute_max_pain(oi_by_strike: dict) -> float | None:
    """Strike at which total option-holder payout is minimized (max pain).

    For each candidate settlement price P (over the strike set):
        pain(P) = Σ call_oi(K)·max(0, P−K) + Σ put_oi(K)·max(0, K−P)
    Max pain is the P that minimizes pain.
    """
    strikes = sorted(oi_by_strike.keys())
    if not strikes:
        return None
    best_k, best_pain = None, None
    for p in strikes:
        pain = 0.0
        for k in strikes:
            if p > k:
                pain += (oi_by_strike[k]["call_oi"] or 0) * (p - k)
            elif p < k:
                pain += (oi_by_strike[k]["put_oi"] or 0) * (k - p)
        if best_pain is None or pain < best_pain:
            best_pain, best_k = pain, p
    return best_k


# --------------------------------------------------------------------------- #
# Upstream Delta WebSocket consumer (single connection, auto-reconnect)
# --------------------------------------------------------------------------- #
# No frame from Delta for this long means the socket is dead, even if TCP has
# not noticed. Without this the consumer blocked forever on a half-open
# connection: prices froze, nothing reconnected, and nothing reported it
# (audit #3).
UPSTREAM_STALL_S = 60.0


async def _drain_pending_subscriptions(ws) -> None:
    """Subscribe to any option contracts the engine has started tracking, so
    their premiums stream instead of being REST-polled."""
    pending = price_bus.pending()
    if not pending:
        return
    batch = pending[:40]
    await ws.send(json.dumps({
        "type": "subscribe",
        "payload": {"channels": [{"name": "v2/ticker", "symbols": batch}]},
    }))
    price_bus.mark_subscribed(batch)


async def _handle_upstream_message(message) -> None:
    """Route one upstream frame: price bus first, then browser fan-out."""
    try:
        raw = json.loads(message)
    except (json.JSONDecodeError, TypeError):
        return
    mtype = raw.get("type", "")

    if mtype == "v2/ticker":
        # Publish EVERY ticker (underlyings and options) to the in-memory bus —
        # this is what makes engine reads instant and what the option chain
        # streams from. It happens before any broadcast so a slow browser can
        # never delay the trading engine's view of price.
        q = raw.get("quotes") or {}
        g = raw.get("greeks") or {}
        price_bus.update(
            raw.get("symbol"),
            best_bid=_fnum(q.get("best_bid")),
            best_ask=_fnum(q.get("best_ask")),
            bid_size=_fnum(q.get("bid_size")),
            ask_size=_fnum(q.get("ask_size")),
            mark_price=_fnum(raw.get("mark_price")),
            spot_price=_fnum(raw.get("spot_price")),
            close=_fnum(raw.get("close")),
            iv=_iv_pct(q.get("mark_iv")),
            delta=_fnum(g.get("delta")),
            gamma=_fnum(g.get("gamma")),
            theta=_fnum(g.get("theta")),
            vega=_fnum(g.get("vega")),
            oi=_fnum(raw.get("oi_contracts")),
            volume=_fnum(raw.get("volume")),
        )

    if mtype == "v2/ticker" and raw.get("symbol") in SYMBOLS:
        norm = normalize_ticker(raw)
        hub.last_ticker[norm["symbol"]] = norm
        await hub.broadcast(norm)

    elif mtype.startswith("candlestick_"):
        norm = normalize_candle(raw)
        if norm["time"] is not None:
            await hub.broadcast(norm)

    elif mtype == "all_trades":
        trade = normalize_trade(raw)
        hub.recent_trades.appendleft(trade)
        await hub.broadcast({"type": "trade", **trade})

    elif mtype == "all_trades_snapshot":
        sym = raw.get("symbol")
        trades = [normalize_trade(t, sym) for t in (raw.get("trades") or [])]
        # Snapshot arrives newest-first; seed the cache in that order.
        hub.recent_trades.clear()
        for t in trades[:MAX_RECENT_TRADES]:
            hub.recent_trades.append(t)
        await hub.broadcast(
            {"type": "trade_snapshot", "trades": trades[:MAX_RECENT_TRADES]}
        )


async def delta_ws_consumer() -> None:
    """Single upstream connection to Delta, with three independent liveness
    guarantees so a dead socket can never go unnoticed:

      1. ping_interval/ping_timeout — an unanswered pong raises ConnectionClosed.
      2. UPSTREAM_STALL_S — bounds any silent gap, even if a proxy answers pings.
      3. Jittered exponential backoff to 60s on reconnect, per Delta's guidance.

    On reconnect the dynamic option subscriptions are re-registered, because a
    fresh socket carries none of them.
    """
    channels = [{"name": "v2/ticker", "symbols": SYMBOLS}]
    # Phase 2: live candles (all UI timeframes) + trade tape for the chart symbol.
    for res in CHART_RESOLUTIONS:
        channels.append({"name": f"candlestick_{res}", "symbols": [CHART_SYMBOL]})
    channels.append({"name": "all_trades", "symbols": [CHART_SYMBOL]})

    subscribe_msg = json.dumps(
        {"type": "subscribe", "payload": {"channels": channels}}
    )
    backoff = 1.0
    while True:
        try:
            async with websockets.connect(
                DELTA_WS,
                ping_interval=20,      # protocol keepalive
                ping_timeout=10,       # unanswered pong => ConnectionClosed
                close_timeout=5,
                max_size=None,
            ) as ws:
                await ws.send(subscribe_msg)
                hub.upstream_connected = True
                # A fresh socket has none of our dynamic option subscriptions.
                price_bus.reset_subscriptions()
                await hub.broadcast({"type": "status", "upstream": "connected"})
                backoff = 1.0  # reset after a successful connect

                while True:
                    try:
                        message = await asyncio.wait_for(
                            ws.recv(), timeout=UPSTREAM_STALL_S
                        )
                    except asyncio.TimeoutError:
                        print(f"[delta-ws] no frame in {UPSTREAM_STALL_S:.0f}s "
                              f"— treating socket as dead, reconnecting")
                        break
                    await _drain_pending_subscriptions(ws)
                    await _handle_upstream_message(message)

        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log and retry any failure
            print(f"[delta-ws] disconnected: {exc!r} — retry in ~{backoff:.1f}s")
        finally:
            hub.upstream_connected = False

        with contextlib.suppress(asyncio.CancelledError):
            await hub.broadcast({"type": "status", "upstream": "disconnected"})
        # Jitter stops every client (and every restart) reconnecting in lockstep.
        await asyncio.sleep(backoff * (0.5 + random.random()))
        backoff = min(backoff * 2, 60.0)


# --------------------------------------------------------------------------- #
# App + lifespan
# --------------------------------------------------------------------------- #
# --------------------------------------------------------------------------- #
# App-state WebSocket: pushes engine/journal/health state so no page polls.
# --------------------------------------------------------------------------- #
class AppHub:
    """Fan-out for application state, with per-topic coalescing.

    Coalescing is the rate-limit protection: if the engine ticks twice per
    second, browsers still receive at most one `engine` frame per flush, so a
    fast trading loop can never flood a slow client.
    """

    def __init__(self) -> None:
        self.clients: set[WebSocket] = set()
        self.lock = asyncio.Lock()
        self.last_state: dict = {}      # topic -> newest payload (snapshot on connect)

    async def register(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.add(ws)

    async def unregister(self, ws: WebSocket) -> None:
        async with self.lock:
            self.clients.discard(ws)

    async def broadcast(self, frames: list[dict]) -> None:
        if not frames:
            return
        async with self.lock:
            targets = list(self.clients)
        if not targets:
            return
        payload = json.dumps({"type": "batch", "frames": frames})
        dead = []
        for ws in targets:
            try:
                await ws.send_text(payload)
            except Exception:  # noqa: BLE001
                dead.append(ws)
        if dead:
            async with self.lock:
                for ws in dead:
                    self.clients.discard(ws)


app_hub = AppHub()


class ChainStreamer:
    """Streams a live option chain over WebSocket.

    Structure (strikes, symbols, expiry) is fetched once over REST because it
    changes only when contracts are listed; the QUOTES are then overlaid from
    the in-memory price bus on every flush, so the table updates at tick speed
    instead of on a 5-second REST poll.
    """

    REFRESH_STRUCTURE = 120.0   # seconds between structure re-fetches

    def __init__(self) -> None:
        self.asset = "BTC"
        self.expiry = ""
        self.subscribers = 0
        self._structure: dict | None = None
        self._structure_at = 0.0
        self._key: tuple | None = None

    def select(self, asset: str, expiry: str) -> None:
        key = (asset.upper(), expiry)
        if key != self._key:
            self._key = key
            self.asset, self.expiry = key
            self._structure = None      # force a refetch for the new selection

    async def _ensure_structure(self) -> dict | None:
        if self._structure and (time.time() - self._structure_at) < self.REFRESH_STRUCTURE:
            return self._structure
        try:
            data = await api_option_chain(asset=self.asset, expiry=self.expiry)
        except Exception:  # noqa: BLE001
            return self._structure
        if isinstance(data, JSONResponse):
            return self._structure
        self._structure = data
        self._structure_at = time.time()
        # Ask the upstream socket to stream every contract in this chain.
        syms = []
        for row in data.get("strikes", []):
            for side in ("call", "put"):
                s = (row.get(side) or {}).get("symbol")
                if s:
                    syms.append(s)
        if syms:
            price_bus.want(*syms)
        return self._structure

    async def snapshot(self) -> dict | None:
        base = await self._ensure_structure()
        if not base:
            return None
        # Overlay live quotes; deep-copying the whole chain each flush is
        # wasteful, so mutate a shallow per-row copy.
        strikes = []
        live_count = 0
        for row in base.get("strikes", []):
            out = {"strike": row.get("strike"), "call": None, "put": None}
            for side in ("call", "put"):
                s = row.get(side)
                if not s:
                    continue
                merged = dict(s)
                tick = price_bus.get(s.get("symbol")) if s.get("symbol") else None
                if tick:
                    live_count += 1
                    for src, dst in (("best_bid", "bid"), ("best_ask", "ask"),
                                     ("bid_size", "bid_size"), ("ask_size", "ask_size"),
                                     ("mark_price", "mark_price"), ("iv", "iv"),
                                     ("delta", "delta"), ("gamma", "gamma"),
                                     ("theta", "theta"), ("vega", "vega"),
                                     ("oi", "oi"), ("volume", "volume")):
                        if tick.get(src) is not None:
                            merged[dst] = tick[src]
                out[side] = merged
            strikes.append(out)
        spot = price_bus.spot(f"{self.asset}USD") or base.get("spot_price")
        return {**base, "spot_price": spot, "strikes": strikes,
                "live_contracts": live_count, "streamed": True}


class ChainStreamerRegistry:
    """One ChainStreamer per (asset, expiry), reference-counted by viewer.

    A single global streamer was shared by every client, and `select()` mutated
    its asset/expiry — so a second tab choosing ETH silently switched the first
    tab's BTC chain to ETH strikes under a BTC header, with no error (audit #11).
    Keying by selection means concurrent viewers of different chains never
    collide, and a chain's structure cache is freed when its last viewer leaves.
    """

    def __init__(self) -> None:
        self._streamers: dict[tuple[str, str], ChainStreamer] = {}
        self._lock = asyncio.Lock()

    async def acquire(self, asset: str, expiry: str) -> ChainStreamer:
        key = ((asset or "BTC").upper(), expiry or "")
        async with self._lock:
            s = self._streamers.get(key)
            if s is None:
                s = ChainStreamer()
                s.select(*key)
                self._streamers[key] = s
            s.subscribers += 1
            return s

    async def release(self, streamer) -> None:
        if streamer is None:
            return
        async with self._lock:
            streamer.subscribers = max(0, streamer.subscribers - 1)
            if streamer.subscribers == 0:
                self._streamers.pop(streamer._key, None)

    async def active(self) -> list[ChainStreamer]:
        async with self._lock:
            return [s for s in self._streamers.values() if s.subscribers > 0]


chain_registry = ChainStreamerRegistry()


async def chain_publisher() -> None:
    """Push every actively-watched option chain ~2x/sec.

    Each frame carries its `key` so a client can discard chains it did not ask
    for — the `chain` topic is coalesced downstream, so with two different chains
    live only the last one published per flush survives the fan-out.
    """
    while True:
        try:
            for streamer in await chain_registry.active():
                snap = await streamer.snapshot()
                if snap:
                    event_bus.publish("chain", {**snap, "key": list(streamer._key)})
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[chain-publisher] {exc!r}")
        await asyncio.sleep(0.5)


async def state_broadcaster() -> None:
    """Drain the engine's event bus and fan out ~10x/sec, coalesced by topic."""
    while True:
        try:
            events = event_bus.drain()
            if events:
                latest: dict[str, dict] = {}
                journal_batch: list[dict] = []
                for e in events:
                    if e["topic"] == "journal":
                        journal_batch.append(e["data"])       # keep every event
                    else:
                        latest[e["topic"]] = e["data"]        # coalesce state
                frames = [{"topic": t, "data": d} for t, d in latest.items()]
                if journal_batch:
                    frames.append({"topic": "journal", "data": journal_batch[-40:]})
                for f in frames:
                    if f["topic"] != "journal":
                        app_hub.last_state[f["topic"]] = f["data"]
                await app_hub.broadcast(frames)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — broadcasting must never die
            print(f"[state-broadcaster] {exc!r}")
        await asyncio.sleep(0.1)


async def engine_publisher() -> None:
    """Push engine state ~1x/sec REGARDLESS of whether the engine is running.

    The engine only emits a frame when it completes a cycle, so a STOPPED engine
    produced no frames at all and the Strategy page fell back to the 5s health
    tick. This keeps that page live either way.
    """
    while True:
        try:
            event_bus.publish("engine", {
                "running": engine.running,
                "cycles": engine.cycles,
                "signals_generated": engine.signals_generated,
                "spot": engine.last_spot or price_bus.spot(strat_config.UNDERLYING_SYMBOL),
                "last_error": engine.last_error,
                "stats": engine.executor.stats(),
                "positions": engine.executor.snapshots(),
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[engine-publisher] {exc!r}")
        await asyncio.sleep(1.0)


def _account_health() -> dict:
    """Account-sync freshness plus engine-book-vs-exchange drift."""
    snap = account_sync.snapshot()
    exch = account_sync.positions_by_product()
    tracked: dict[int, int] = {}
    for p in engine.executor.snapshots():
        if p.get("state") != "open":
            continue
        pid = p.get("product_id")
        if pid:
            tracked[int(pid)] = tracked.get(int(pid), 0) + (p.get("contracts") or 0)
    
    if strat_config.EXECUTION_MODE == "paper":
        exch = dict(tracked)
        
    drift = sorted(set(exch) | set(tracked))
    mismatched = [pid for pid in drift if exch.get(pid, 0) != tracked.get(pid, 0)]
    shorts = {pid: sz for pid, sz in exch.items() if sz < 0}
    return {
        # Shorts are called out separately: the engine cannot manage them, so
        # one sitting on the account is unmanaged risk with no stop.
        "short_positions": shorts,
        "has_unmanaged_shorts": bool(shorts),
        "synced_at": snap["synced_at"], "age_sec": snap["age_sec"],
        "stale": snap["stale"], "ok": snap["ok"],
        "equity_usd": snap["equity_usd"], "available_usd": snap["available_usd"],
        "exchange_positions": snap.get("position_count", 0),
        "exchange_open_orders": snap.get("open_order_count", 0),
        "engine_tracked_positions": engine.executor.open_count(),
        "in_sync": not mismatched,
        "mismatched_products": mismatched,
        "orphan_policy": strat_config.ORPHAN_POLICY,
        "errors": snap["errors"],
    }


async def account_publisher() -> None:
    """Keep the Delta demo account synced and pushed to every dashboard.

    Runs independently of the engine: a stopped engine used to mean nothing
    reconciled at all, so the UI could show a book that had been wrong for
    hours. The blocking REST work goes to a worker thread — never the loop.
    """
    while True:
        try:
            snap = await asyncio.to_thread(account_sync.refresh)
            event_bus.publish("account", {
                "synced_at": snap["synced_at"], "ok": snap["ok"],
                "equity_usd": snap["equity_usd"],
                "available_usd": snap["available_usd"],
                "balances": snap["balances"],
                "positions": snap["positions"],
                "open_orders": snap["open_orders"],
                "position_count": snap.get("position_count", 0),
                "open_order_count": snap.get("open_order_count", 0),
                "errors": snap["errors"],
                "syncs": snap["syncs"], "failures": snap["failures"],
            })
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[account-sync] {exc!r}")
        await asyncio.sleep(strat_config.ACCOUNT_SYNC_SECONDS)


async def engine_reconciler() -> None:
    """Keep the book synced AND keep exits running while the engine is STOPPED.

    "Stopped" must mean "opens no new positions" — not "abandons the ones it
    holds". Two things follow, and both matter:

      * reconcile() adopts untracked exchange positions. Adopting one while
        nothing enforces its stop would be worse than not adopting it at all:
        the UI would show a target and a stop that no code is watching.
      * an open position still needs its stop, target, time and expiry exits.
        Settlement in particular does not wait for the operator to press Start.

    So this runs manage() before reconcile(), mirroring the engine's own cycle
    order (exits first, always). It never opens anything.
    """
    while True:
        await asyncio.sleep(strat_config.ACCOUNT_SYNC_SECONDS * 2)
        try:
            if engine.running:
                continue          # the engine's own cycle is already doing it

            def _tick():
                # Exits FIRST — same invariant as Engine._cycle.
                engine.executor.manage()
                engine.executor.reconcile()

            await asyncio.to_thread(_tick)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[reconciler] {exc!r}")


async def health_publisher() -> None:
    """Push a health frame every few seconds so the strip needs no polling."""
    while True:
        try:
            snap = {
                "iv_collection": await asyncio.to_thread(strat_store.iv_snapshot_stats),
                "trades_recorded": await asyncio.to_thread(strat_store.count_trades),
                "price_bus": price_bus.stats(),
                "event_bus": event_bus.stats(),
                "engine": {
                    "running": engine.running, "cycles": engine.cycles,
                    "mode": strat_config.EXECUTION_MODE,
                    "open_positions": engine.executor.open_count(),
                    "pending_orders": len(engine.executor.pending),
                    "last_error": engine.last_error,
                    "enabled_strategies": len(engine.enabled_strategies()),
                },
                # Account sync: drift between the engine's book and the
                # exchange is the thing most worth seeing at a glance.
                "account": _account_health(),
            }
            event_bus.publish("health", snap)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            print(f"[health-publisher] {exc!r}")
        await asyncio.sleep(5)


async def iv_collector() -> None:
    """Passively record a VRP snapshot every 30 min, building the forward IV
    series needed to test the variance risk premium properly. Runs whether or
    not the trading engine is active; never places orders."""
    while True:
        try:
            await asyncio.to_thread(strat_vol.snapshot, strat_config.ASSET)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — collection must never crash the app
            print(f"[iv-collector] snapshot failed: {exc!r}")
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(app: FastAPI):
    consumer = asyncio.create_task(delta_ws_consumer())
    collector = asyncio.create_task(iv_collector())
    broadcaster = asyncio.create_task(state_broadcaster())
    healthpub = asyncio.create_task(health_publisher())
    enginepub = asyncio.create_task(engine_publisher())
    chainpub = asyncio.create_task(chain_publisher())
    acctpub = asyncio.create_task(account_publisher())
    reconciler = asyncio.create_task(engine_reconciler())
    # Phase 5: durable trade store — backfill any legacy JSONL closes once.
    strat_store.backfill_from_jsonl()
    if strat_config.AUTOSTART:
        engine.start()
    try:
        yield
    finally:
        await engine.stop()
        # A process exit used to leave real testnet positions with no manager
        # watching their stops, and said nothing about it (audit #12).
        open_now = await asyncio.to_thread(engine.executor.snapshots_blocking)
        if open_now:
            # Live-only: every open position is a real exchange position, so
            # leaving one behind means it rides with no manager watching its stop.
            if strat_config.FLATTEN_ON_SHUTDOWN:
                n = await asyncio.to_thread(engine.executor.flatten_all, "app_shutdown")
                print(f"[shutdown] flattened {n} live position(s)")
            else:
                journal.record("shutdown_open_positions", {
                    "count": len(open_now),
                    "positions": [{"id": p.get("id"), "symbol": p.get("symbol"),
                                   "contracts": p.get("contracts")} for p in open_now],
                    "note": "app stopped with REAL positions still open on the exchange"})
                print(f"[shutdown] WARNING: {len(open_now)} REAL position(s) left open "
                      f"— they have no stop-loss manager until the app restarts")
        for task in (consumer, collector, broadcaster, healthpub, enginepub,
                     chainpub, acctpub, reconciler):
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task


app = FastAPI(title="Delta Trading App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


@app.websocket("/ws/app")
async def ws_app(ws: WebSocket):
    """Single application-state socket: engine, journal, health, alerts."""
    await ws.accept()
    await app_hub.register(ws)
    current_chain: ChainStreamer | None = None
    try:
        # Immediate snapshot so a fresh page is populated without a REST call.
        snapshot = [{"topic": t, "data": d} for t, d in app_hub.last_state.items()]
        snapshot.append({"topic": "engine", "data": engine.status()})
        snapshot.append({"topic": "journal", "data": journal.recent(40)})
        await ws.send_text(json.dumps({"type": "batch", "frames": snapshot}))

        while True:
            msg = await ws.receive_text()
            # Heartbeat: the client pings, we pong. Detects half-open sockets
            # that TCP alone would leave hanging.
            if msg == "ping":
                await ws.send_text(json.dumps({"type": "pong", "ts": time.time()}))
                continue
            try:
                cmd = json.loads(msg)
            except (json.JSONDecodeError, TypeError):
                continue
            action = cmd.get("action")
            if action == "sub_chain":
                nxt = await chain_registry.acquire(
                    cmd.get("asset", "BTC"), cmd.get("expiry", ""))
                if nxt is not current_chain:
                    await chain_registry.release(current_chain)
                    current_chain = nxt
                else:
                    # acquire() already incremented; this client only holds one.
                    await chain_registry.release(nxt)
                snap = await current_chain.snapshot()
                if snap:
                    await ws.send_text(json.dumps({
                        "type": "batch",
                        "frames": [{"topic": "chain", "data": snap,
                                    "key": list(current_chain._key)}]}))
            elif action == "unsub_chain":
                await chain_registry.release(current_chain)
                current_chain = None
    except WebSocketDisconnect:
        pass
    except Exception as exc:  # noqa: BLE001
        print(f"[ws/app] client error: {exc!r}")
    finally:
        await chain_registry.release(current_chain)
        await app_hub.unregister(ws)


# --------------------------------------------------------------------------- #
# Routes
# --------------------------------------------------------------------------- #
@app.get("/")
async def dashboard(request: Request):
    return templates.TemplateResponse(
        "dashboard.html", {"request": request, "symbols": SYMBOLS}
    )


@app.get("/api/ticker/{symbol}")
async def api_ticker(symbol: str):
    """Return the latest ticker for a symbol (cached tick, else REST fallback)."""
    symbol = symbol.upper()
    if symbol in hub.last_ticker:
        return hub.last_ticker[symbol]

    def _fetch():
        resp = requests.get(f"{DELTA_REST}/v2/tickers/{symbol}", timeout=10)
        resp.raise_for_status()
        return resp.json().get("result", {})

    try:
        result = await asyncio.to_thread(_fetch)
        if not result:
            return JSONResponse(status_code=404, content={"error": "symbol not found"})
        return normalize_ticker(result)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/candles")
async def api_candles(symbol: str = CHART_SYMBOL, resolution: str = "5m", limit: int = 300):
    """Return historical OHLC candles (ascending time) for the chart.

    Delta's /v2/history/candles returns newest-first; we reverse to the
    ascending order TradingView Lightweight Charts requires and hand back
    `time` in whole seconds.
    """
    symbol = symbol.upper()
    if resolution not in VALID_RESOLUTIONS:
        return JSONResponse(
            status_code=400, content={"error": f"invalid resolution: {resolution}"}
        )

    # Approximate seconds-per-bar to size the lookback window.
    unit = {"m": 60, "h": 3600, "d": 86400, "w": 604800}
    seconds = int(resolution[:-1]) * unit.get(resolution[-1], 60)
    limit = max(1, min(limit, 2000))

    import time as _time

    end = int(_time.time())
    start = end - seconds * (limit + 5)

    def _fetch():
        resp = requests.get(
            f"{DELTA_REST}/v2/history/candles",
            params={"symbol": symbol, "resolution": resolution, "start": start, "end": end},
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("result", []) or []

    try:
        rows = await asyncio.to_thread(_fetch)
        rows.sort(key=lambda c: c.get("time", 0))  # ascending for the chart
        candles = [
            {
                "time": int(c["time"]),
                "open": _fnum(c.get("open")),
                "high": _fnum(c.get("high")),
                "low": _fnum(c.get("low")),
                "close": _fnum(c.get("close")),
                "volume": _fnum(c.get("volume")),
            }
            for c in rows
            if c.get("time") is not None
        ]
        return {"symbol": symbol, "resolution": resolution, "candles": candles[-limit:]}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/option-chain")
async def option_chain_page(request: Request):
    return templates.TemplateResponse("option_chain.html", {"request": request})


@app.get("/api/expiries")
async def api_expiries(asset: str = "BTC"):
    """Return live expiries for an asset's options, sorted ascending.

    Delta gives `settlement_time` as an ISO instant; the chain endpoint wants
    `expiry_date` as DD-MM-YYYY, so we return both the sortable ISO date and
    the DD-MM-YYYY form.
    """
    asset = asset.upper()

    def _fetch():
        resp = requests.get(
            f"{DELTA_REST}/v2/products",
            params={
                "contract_types": "call_options",
                "underlying_asset_symbol": asset,
                "states": "live",
            },
            timeout=15,
        )
        resp.raise_for_status()
        return resp.json().get("result", []) or []

    try:
        rows = await asyncio.to_thread(_fetch)
        iso_dates = {
            p["settlement_time"][:10]
            for p in rows
            if p.get("settlement_time") and len(p["settlement_time"]) >= 10
        }
        expiries = [
            {"date": d, "expiry": f"{d[8:10]}-{d[5:7]}-{d[0:4]}"}
            for d in sorted(iso_dates)
        ]
        return {"asset": asset, "expiries": expiries}
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})


@app.get("/api/option-chain")
async def api_option_chain(asset: str = "BTC", expiry: str = ""):
    """Return a structured option chain with PCR, Max Pain, ATM, and ATM IV."""
    asset = asset.upper()

    def _fetch():
        params = {
            "contract_types": "call_options,put_options",
            "underlying_asset_symbols": asset,
        }
        if expiry:
            params["expiry_date"] = expiry
        # Use the execution base: in live_demo the UI must show the chain from
        # the venue orders actually route to, or a displayed contract may not
        # exist where we trade (audit #22).
        resp = requests.get(f"{strat_config.EXEC_BASE}/v2/tickers",
                            params=params, timeout=(3.05, 20))
        resp.raise_for_status()
        return resp.json().get("result", []) or []

    try:
        rows = await asyncio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})

    strikes: dict[float, dict] = {}
    spot = None
    for t in rows:
        sp = _fnum(t.get("spot_price"))
        if sp:
            spot = sp
        k = _fnum(t.get("strike_price"))
        if k is None:
            continue
        entry = strikes.setdefault(k, {"strike": k, "call": None, "put": None})
        side = build_option_side(t)
        if t.get("contract_type") == "call_options":
            entry["call"] = side
        else:
            entry["put"] = side

    strike_list = sorted(strikes.values(), key=lambda e: e["strike"])

    def _oi(entry, key):
        s = entry.get(key)
        return (s["oi"] or 0) if s else 0

    def _vol(entry, key):
        s = entry.get(key)
        return (s["volume"] or 0) if s else 0

    total_call_oi = sum(_oi(e, "call") for e in strike_list)
    total_put_oi = sum(_oi(e, "put") for e in strike_list)
    total_call_vol = sum(_vol(e, "call") for e in strike_list)
    total_put_vol = sum(_vol(e, "put") for e in strike_list)

    pcr_oi = (total_put_oi / total_call_oi) if total_call_oi else None
    pcr_vol = (total_put_vol / total_call_vol) if total_call_vol else None

    atm_strike = (
        min(strikes.keys(), key=lambda k: abs(k - spot))
        if strikes and spot else None
    )

    oi_by_strike = {
        e["strike"]: {"call_oi": _oi(e, "call"), "put_oi": _oi(e, "put")}
        for e in strike_list
    }
    max_pain = compute_max_pain(oi_by_strike)

    atm_iv = None
    if atm_strike is not None:
        e = strikes[atm_strike]
        ivs = [s["iv"] for s in (e["call"], e["put"]) if s and s["iv"] is not None]
        if ivs:
            atm_iv = sum(ivs) / len(ivs)

    return {
        "asset": asset,
        "expiry": expiry,
        "spot_price": spot,
        "atm_strike": atm_strike,
        "pcr_oi": pcr_oi,
        "pcr_volume": pcr_vol,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "max_pain": max_pain,
        "atm_iv": atm_iv,
        "strikes": strike_list,
    }


# --------------------------------------------------------------------------- #
# Phase 4 — strategy engine + paper/testnet-demo trading
# --------------------------------------------------------------------------- #
@app.get("/strategy")
async def strategies_page(request: Request):
    return templates.TemplateResponse("strategies.html", {"request": request})


@app.get("/api/strategy/status")
async def api_strategy_status():
    return engine.status()


@app.post("/api/strategy/start")
async def api_strategy_start():
    """Arm the engine. LIVE ONLY — this places real orders on the testnet book.

    start() runs a credential preflight and refuses if it cannot trade, so a
    False here with a `blocked_reason` means nothing was armed.
    """
    started = engine.start()
    return {"ok": True, "started": started, "running": engine.running,
            "mode": strat_config.EXECUTION_MODE, "live_only": True,
            "contracts": strat_config.CONTRACTS,
            "blocked_reason": None if started else engine.last_error}


@app.post("/api/strategy/stop")
async def api_strategy_stop(flatten: bool = False):
    await engine.stop(flatten=flatten)
    return {"ok": True, "running": engine.running}


@app.post("/api/strategy/toggle")
async def api_strategy_toggle(request: Request):
    body = await request.json()
    slug = body.get("slug", "")
    enabled = bool(body.get("enabled", False))
    ok = engine.set_enabled(slug, enabled)
    return {"ok": ok, "slug": slug, "enabled": enabled}


@app.post("/api/strategy/flatten")
async def api_strategy_flatten():
    n = await asyncio.to_thread(engine.executor.flatten_all, "manual_flatten")
    return {"ok": True, "closed": n}


@app.post("/api/strategy/flatten-shorts")
async def api_flatten_shorts():
    """Close every SHORT position with a reduce_only BUY.

    Separate from /flatten because the engine cannot manage shorts and never
    opens them deliberately — one appearing means something went wrong, so
    closing it is an explicit operator decision, not an automatic one.
    """
    res = await asyncio.to_thread(engine.executor.flatten_shorts)
    return res


@app.get("/api/strategy/journal")
async def api_strategy_journal(limit: int = 100):
    # Clamp: `limit` is user-supplied and was unbounded (audit #21).
    limit = max(1, min(int(limit), 500))
    return {"events": journal.recent(limit)}


# --------------------------------------------------------------------------- #
# Phase 5 — journal (durable trade history) + performance analytics
# --------------------------------------------------------------------------- #
@app.get("/journal")
async def journal_page(request: Request):
    return templates.TemplateResponse("journal.html", {"request": request})


@app.get("/performance")
async def performance_page(request: Request):
    return templates.TemplateResponse("performance.html", {"request": request})


@app.get("/api/trades")
async def api_trades(
    limit: int = 200, offset: int = 0,
    strategy: str = "all", outcome: str = "all", mode: str = "all",
):
    limit = max(1, min(limit, 1000))
    rows = await asyncio.to_thread(
        strat_store.fetch_trades, limit, offset,
        strategy, outcome if outcome != "all" else None, mode,
    )
    total = await asyncio.to_thread(strat_store.count_trades)
    strategies = await asyncio.to_thread(strat_store.strategies_seen)
    return {"trades": rows, "total": total, "strategies": strategies}


@app.get("/api/performance")
async def api_performance():
    return await asyncio.to_thread(strat_store.performance)


@app.get("/backtest")
async def backtest_page(request: Request):
    return templates.TemplateResponse("backtest.html", {"request": request})


@app.get("/api/backtest")
async def api_backtest(days: float = 7.0, max_hold: int = 30):
    days = max(0.5, min(days, 30.0))
    max_hold = max(3, min(max_hold, 200))
    return await asyncio.to_thread(strat_backtest.run, days, "BTCUSD", max_hold)


@app.get("/volatility")
async def volatility_page(request: Request):
    return templates.TemplateResponse("volatility.html", {"request": request})


@app.get("/api/volatility")
async def api_volatility(record: bool = False):
    """Live VRP snapshot. `record=true` also persists it to the forward series."""
    fn = strat_vol.snapshot if record else strat_vol.analyze
    try:
        data = await asyncio.to_thread(fn, strat_config.ASSET)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})
    data["collection"] = await asyncio.to_thread(strat_store.iv_snapshot_stats)
    return data


@app.get("/research")
async def research_page(request: Request):
    return templates.TemplateResponse("research.html", {"request": request})


@app.get("/api/research")
async def api_research(days: float = 60.0, max_hold: int = 20):
    days = max(7.0, min(days, 120.0))
    max_hold = max(3, min(max_hold, 200))
    return await asyncio.to_thread(
        strat_research.run, days, ("5m", "15m", "1h"), max_hold, 20
    )


@app.get("/api/health")
async def api_health():
    """Phase 7 — one-call system health: engine, auth, data collection, errors.

    Everything here is read-only and safe to poll; it never places an order.
    """
    def _probe():
        out: dict = {}
        if strat_config.EXECUTION_MODE == "paper":
            out["auth"] = {"ok": True}
        else:
            try:
                delta_client.profile(retries=1)
                out["auth"] = {"ok": True}
            except DeltaError as exc:
                msg = str(exc)
                ip = None
                if "client_ip" in msg:
                    try:
                        ip = msg.split('"client_ip":')[1].split('"')[1]
                    except (IndexError, ValueError):
                        ip = None
                out["auth"] = {
                    "ok": False,
                    "error": msg[:200],
                    "client_ip": ip,
                    "hint": ("This IP is not whitelisted on the demo API key. The IP rotates on "
                             "mobile/CGNAT networks — remove the IP restriction on the key entirely.")
                    if ip else None,
                }
            except Exception as exc:  # noqa: BLE001
                out["auth"] = {"ok": False, "error": str(exc)[:200]}
        out["iv_collection"] = strat_store.iv_snapshot_stats()
        out["trades_recorded"] = strat_store.count_trades()
        return out

    data = await asyncio.to_thread(_probe)
    st = engine.status()
    recent = journal.recent(40)
    problems = [e for e in recent if e.get("kind") in ("order_error", "error", "reconcile_mismatch")]
    data["engine"] = {
        "running": st["running"],
        "cycles": st["cycles"],
        "signals": st["signals_generated"],
        "open_positions": st["stats"]["open_positions"],
        "pending_orders": st["stats"].get("pending_orders", 0),
        "last_error": st["last_error"],
        "last_cycle_at": st["last_cycle_at"],
        "mode": st["config"]["execution_mode"],
        "autostart": st["config"]["autostart"],
        "enabled_strategies": sum(1 for s in st["strategies"] if s["enabled"]),
        # Exits keep running via the background reconciler even when stopped.
        "exits_active": st.get("exits_active", False),
    }
    data["price_bus"] = price_bus.stats()
    data["account"] = _account_health()
    data["recent_problems"] = problems[:8]
    data["problem_count"] = len(problems)
    ok = data["auth"]["ok"] and not st["last_error"]
    # An unmanaged short is a standing risk with no stop — it must degrade the
    # overall status, not sit quietly inside a sub-object nobody reads.
    if data["account"].get("has_unmanaged_shorts"):
        ok = False
        data["status"] = "unmanaged_short"
    elif not data["account"].get("in_sync", True):
        data["status"] = "out_of_sync" if ok else "degraded"
    else:
        data["status"] = "ok" if ok else ("degraded" if data["auth"]["ok"] else "auth_failed")
    return data


@app.get("/api/account")
async def api_account(refresh: bool = False):
    """Delta demo account snapshot: balances, positions and resting orders.

    Serves the continuously-synced snapshot (refreshed every
    ACCOUNT_SYNC_SECONDS by `account_publisher`), so this route is instant and
    cannot block the event loop. Pass `?refresh=true` to force a fresh pull.
    """
    if refresh:
        try:
            await asyncio.to_thread(account_sync.refresh)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse(status_code=502, content={"error": str(exc)})
    snap = account_sync.snapshot()
    # Back-compat keys for the existing UI.
    if snap.get("errors", {}).get("balances"):
        snap["balances_error"] = snap["errors"]["balances"]
    if snap.get("errors", {}).get("positions"):
        snap["positions_error"] = snap["errors"]["positions"]
    return snap


@app.websocket("/ws/market")
async def ws_market(ws: WebSocket):
    await ws.accept()
    await hub.register(ws)
    try:
        # Immediate snapshot so cards populate without waiting for a fresh tick.
        await ws.send_text(
            json.dumps(
                {
                    "type": "status",
                    "upstream": "connected" if hub.upstream_connected else "disconnected",
                }
            )
        )
        for sym in SYMBOLS:
            if sym in hub.last_ticker:
                await ws.send_text(json.dumps(hub.last_ticker[sym]))

        # Seed the trade tape from cache so it isn't empty on first load.
        if hub.recent_trades:
            await ws.send_text(
                json.dumps(
                    {"type": "trade_snapshot", "trades": list(hub.recent_trades)}
                )
            )

        # Block on receive purely to detect client disconnect. The browser is
        # not expected to send anything; broadcasts come from the consumer task.
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        pass
    except Exception:  # noqa: BLE001
        pass
    finally:
        await hub.unregister(ws)

@app.get("/api/settings")
async def api_get_settings():
    from strategy.settings import manager as settings_manager
    return settings_manager.all()

@app.post("/api/settings")
async def api_post_settings(request: Request):
    from strategy.settings import manager as settings_manager
    data = await request.json()
    settings_manager.update(data)
    return {"ok": True, "settings": settings_manager.all()}

@app.get("/api/auto-confidence/preview")
async def api_preview_confidence(symbol: str, side: str, confidence: int = 50):
    from strategy.executor import _confidence_params
    from strategy.pricebus import bus
    from strategy.options_calc import calculate_options_order_params
    
    contracts, leverage = _confidence_params(confidence)
    
    spot = bus.spot(symbol) or engine.last_spot or 0.0
    q_dict = engine.executor.resolver.atm(spot)
    quote = q_dict.get(side.upper())
    
    if not quote:
        return JSONResponse(status_code=400, content={"error": "No tradable option found for current spot price."})
        
    virtual_balance = account_sync.snapshot().get("equity_usd", 10000.0)
    
    try:
        adv_params = calculate_options_order_params(quote.symbol, side.upper(), confidence, virtual_balance)
    except Exception as e:
        return JSONResponse(status_code=400, content={"error": f"Failed to calculate advanced order params: {e}"})

    margin = (adv_params["smart_entry"] * contracts * quote.contract_value) / leverage if leverage else 0.0
    
    conf = max(1, min(100, confidence))
    if conf <= 20: tier = "Very Low"
    elif conf <= 40: tier = "Low"
    elif conf <= 60: tier = "Medium"
    elif conf <= 80: tier = "High"
    else: tier = "Very High"
    
    return {
        "symbol": quote.symbol,
        "entry_price": adv_params["smart_entry"],
        "contracts": contracts,
        "leverage": leverage,
        "margin": margin,
        "tier": tier,
        "contract_value": quote.contract_value,
        "available_usd": account_sync.snapshot().get("available_usd", 0),
        "adv_params": adv_params
    }

@app.post("/api/order/place")
async def api_order_place(request: Request):
    data = await request.json()
    symbol = data.get("symbol", "BTCUSD")
    side = data.get("side", "CE")
    confidence = int(data.get("confidence", 50))
    
    def do_trade():
        from strategy.pricebus import bus
        from strategy.base import Signal
        spot = bus.spot(symbol) or engine.last_spot or 0.0
        q_dict = engine.executor.resolver.atm(spot)
        quote = q_dict.get(side.upper())
            
        if not quote:
            return {"error": "Could not resolve ATM option for entry."}
            
        sig = Signal(
            strategy="manual",
            direction=side.upper(),
            reason=f"Manual trade with {confidence} confidence",
            confidence=confidence
        )
        
        # Open market will do the pre-trade calculation again and execute
        res = engine.executor.open(sig, quote, bar_seconds=60)
        if not res:
            return {"error": "Order rejected by executor (check journal for reasons)."}
        return {"ok": True, "position": res}
        
    try:
        res = await asyncio.to_thread(do_trade)
        if "error" in res:
            return JSONResponse(status_code=400, content=res)
        return res
    except Exception as e:
        import traceback
        traceback.print_exc()
        return JSONResponse(status_code=500, content={"error": str(e)})

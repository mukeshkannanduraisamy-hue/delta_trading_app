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
from strategy import config as strat_config
from strategy.delta_client import DeltaError, client as delta_client
from strategy.engine import engine
from strategy.journal import journal

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
async def delta_ws_consumer() -> None:
    channels = [{"name": "v2/ticker", "symbols": SYMBOLS}]
    # Phase 2: live candles (all UI timeframes) + trade tape for the chart symbol.
    for res in CHART_RESOLUTIONS:
        channels.append({"name": f"candlestick_{res}", "symbols": [CHART_SYMBOL]})
    channels.append({"name": "all_trades", "symbols": [CHART_SYMBOL]})

    subscribe_msg = json.dumps(
        {"type": "subscribe", "payload": {"channels": channels}}
    )
    backoff = 1
    while True:
        try:
            # ping_interval=None: the ticker stream is continuous (a message
            # every ~2s), so we rely on message flow for liveness instead of
            # protocol pings, which Delta does not always answer.
            async with websockets.connect(
                DELTA_WS, ping_interval=None, max_size=None
            ) as ws:
                await ws.send(subscribe_msg)
                hub.upstream_connected = True
                await hub.broadcast({"type": "status", "upstream": "connected"})
                backoff = 1  # reset after a successful connect

                async for message in ws:
                    try:
                        raw = json.loads(message)
                    except (json.JSONDecodeError, TypeError):
                        continue
                    mtype = raw.get("type", "")

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
                        trades = [
                            normalize_trade(t, sym) for t in (raw.get("trades") or [])
                        ]
                        # Snapshot arrives newest-first; seed the cache in that order.
                        hub.recent_trades.clear()
                        for t in trades[:MAX_RECENT_TRADES]:
                            hub.recent_trades.append(t)
                        await hub.broadcast(
                            {"type": "trade_snapshot", "trades": trades[:MAX_RECENT_TRADES]}
                        )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — log and retry any failure
            print(f"[delta-ws] disconnected: {exc!r} — retry in {backoff}s")
        finally:
            hub.upstream_connected = False

        with contextlib.suppress(asyncio.CancelledError):
            await hub.broadcast({"type": "status", "upstream": "disconnected"})
        await asyncio.sleep(backoff)
        backoff = min(backoff * 2, 15)  # exponential backoff, capped at 15s


# --------------------------------------------------------------------------- #
# App + lifespan
# --------------------------------------------------------------------------- #
@asynccontextmanager
async def lifespan(app: FastAPI):
    consumer = asyncio.create_task(delta_ws_consumer())
    if strat_config.AUTOSTART:
        engine.start()
    try:
        yield
    finally:
        await engine.stop()
        consumer.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await consumer


app = FastAPI(title="Delta Trading App", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=str(BASE_DIR / "static")), name="static")


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
        resp = requests.get(f"{DELTA_REST}/v2/tickers", params=params, timeout=20)
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
    started = engine.start()
    return {"ok": True, "started": started, "running": engine.running}


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


@app.get("/api/strategy/journal")
async def api_strategy_journal(limit: int = 100):
    return {"events": journal.recent(limit)}


@app.get("/api/account")
async def api_account():
    """Live testnet demo account snapshot (profile + balances + positions)."""
    def _fetch():
        out = {}
        try:
            bals = delta_client.balances()
            out["balances"] = [
                {
                    "asset": b.get("asset_symbol"),
                    "balance": b.get("balance"),
                    "available": b.get("available_balance"),
                }
                for b in (bals or [])
                if float(b.get("balance") or 0) != 0
            ]
        except DeltaError as exc:
            out["balances_error"] = str(exc)
        try:
            out["positions"] = delta_client.positions()
        except DeltaError as exc:
            out["positions_error"] = str(exc)
        return out

    try:
        return await asyncio.to_thread(_fetch)
    except Exception as exc:  # noqa: BLE001
        return JSONResponse(status_code=502, content={"error": str(exc)})


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

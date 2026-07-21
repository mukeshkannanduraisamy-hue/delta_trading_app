# Delta Trading App — Complete Project Documentation

**Status:** Phase 4 Complete · Testnet-Only · No Validated Edge  
**Generated:** 2026-07-21  
**For:** Incoming AI / Next Developer

---

# TABLE OF CONTENTS

1. [TL;DR & Quick Start](#tldr--quick-start)
2. [What This Project Does](#what-this-project-does)
3. [Critical Context (Must Know)](#critical-context-must-know)
4. [Project Structure](#project-structure)
5. [Core Systems Explained](#core-systems-explained)
6. [API Routes & WebSocket](#api-routes--websocket)
7. [Data Flows](#data-flows)
8. [Configuration (`.env`)](#configuration-env)
9. [Recent Fixes (2026-07-21)](#recent-fixes-2026-07-21)
10. [Common Tasks with Examples](#common-tasks-with-examples)
11. [Testing Workflow](#testing-workflow)
12. [Known Issues & Gotchas](#known-issues--gotchas)
13. [Debug Checklist](#debug-checklist)
14. [File Reference Guide](#file-reference-guide)
15. [Running the Project](#running-the-project)

---

# TL;DR & Quick Start

## What
A **FastAPI-based live cryptocurrency options trading engine** with 8 quantitative strategies, running on Delta Exchange India testnet.

## Status
✅ All phases complete  
❌ No validated edge (all strategies tested negative)  
🧪 Testnet-only (no real money)

## In 30 Seconds
```
Delta WebSocket (market data)
  ↓
Engine evaluates 8 strategies every 5–15s
  ↓
Executor fills (paper or live_demo)
  ↓
Browser dashboards show real-time state
```

## Start Now (5 Minutes)
```powershell
cd D:\TRADER\delta_trading_app
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open: http://localhost:8000/strategy → Enable a strategy → Click Start

---

# What This Project Does

### Primary Goal
Test whether 8 published option strategies (from Zing Trade blog, adapted to crypto) have tradeable edge on Delta Exchange India.

### Verdict
❌ **No validated edge.** All backtests negative (−0.37 to −1.34 R/trade).

### What It Can Do
- ✅ Live options chain explorer (OI, IV, Greeks, bid/ask)
- ✅ Real-time strategy signal generation (8 rules)
- ✅ Paper fills (simulated, realistic with fees)
- ✅ Live testnet orders (real, one-line config)
- ✅ Trade journal (JSONL + SQLite)
- ✅ Performance analytics (equity curve, Sharpe, max DD)
- ✅ Offline backtesting (pure function replay)
- ✅ Walk-forward research (train/test/holdout validation)
- ✅ IV surface + VRP analysis lab

### What It Can't Do
- ❌ Generate profitable signals
- ❌ Accept real capital
- ❌ Scale beyond single process
- ❌ Predict market direction

---

# Critical Context (Must Know)

## #1: Testnet Only
- **No real money.** All trading on Delta's demo book.
- Credentials: `DELTA_API_KEY` and `DELTA_API_SECRET` for testnet only.
- Paper fills simulate bid/ask spread + 18% GST fee (realistic cost).

## #2: No Validated Edge
- Backtests: All 8 strategies negative (−0.37 to −1.34 R/trade)
- Walk-forward: 0 survivors across 30-combo parameter screen
- VRP test: Inconclusive (≈0)
- **Do not deploy capital. Full stop.**

## #3: Paper Mode is Default
- `.env` → `EXECUTION_MODE=paper` (default)
- Simulates realistic fills (bid/ask + fees)
- Safe for iteration and testing

## #4: One-Line Switch to Live Demo
- `.env` → `EXECUTION_MODE=live_demo`
- Places real testnet orders via Delta API
- Requires API credentials on testnet
- Deliberate choice; never auto-arms

## #5: WebSocket Real-Time Data
- Chain updates: 0.5s latency (~8 frames per 4 seconds)
- Engine state: 1.0s latency (~4 frames per 4 seconds)
- Spot price: Live (via PriceBus in-memory cache)

---

# Project Structure

## Directory Layout

```
D:\TRADER\delta_trading_app/
├── main.py                          # FastAPI app + WebSocket
├── requirements.txt                 # Python deps
├── .env                             # Config (EXECUTION_MODE, ENGINE_ASSET, etc.)
├── PHASE4_STRATEGY_ENGINE.md        # Full strategy documentation
│
├── strategy/                        # Core trading engine
│   ├── __init__.py
│   ├── base.py                      # Signal + Strategy contracts
│   ├── config.py                    # Constants, env vars, fees
│   ├── delta_client.py              # REST client (HMAC auth)
│   ├── market_data.py               # Expiry/ATM CE/PE resolution
│   ├── indicators.py                # SMA, EMA, ATR, Bollinger, Supertrend
│   ├── zing_strategies.py           # 8 strategies (all stateless)
│   ├── engine.py                    # Main poll loop
│   ├── executor.py                  # Paper + live_demo fills
│   ├── eventbus.py                  # Thread-safe queue (engine → asyncio)
│   ├── pricebus.py                  # In-memory price cache
│   ├── journal.py                   # JSONL + SQLite trade log
│   ├── backtest.py                  # Offline replay
│   ├── research.py                  # Walk-forward screen
│   ├── store.py                     # SQLite persistence
│   ├── volatility.py                # IV surface + VRP collector
│   ├── datafeed.py                  # Binance klines loader + cache
│   ├── diagnostics.py               # Health check
│   ├── optimizer.py                 # Parameter search
│   ├── ai_strategy.py               # Placeholder
│   ├── cache/                       # Cached OHLCV (.npz files)
│   └── journal/                     # Trade logs
│       ├── trades.db                # SQLite
│       └── trades.jsonl             # Human-readable
│
├── static/                          # Frontend assets
│   ├── css/
│   │   └── style.css                # Unified dark theme
│   └── js/
│       ├── app.js                   # App init
│       ├── wsclient.js              # WebSocket singleton
│       ├── chart.js                 # Lightweight-charts wrapper
│       ├── option_chain.js          # OI + IV charts
│       ├── strategies.js            # Engine control
│       ├── backtest.js              # Backtest UI
│       ├── research.js              # Walk-forward UI
│       ├── journal.js               # Trade table
│       ├── performance.js           # Analytics
│       ├── volatility.js            # IV lab
│       ├── health.js                # Dashboard
│       └── lib/
│           └── lightweight-charts.standalone.production.js
│
├── templates/                       # Jinja2 HTML
│   ├── base.html                    # Layout
│   ├── dashboard.html               # /
│   ├── option_chain.html            # /option-chain
│   ├── strategies.html              # /strategy
│   ├── backtest.html                # /backtest
│   ├── research.html                # /research
│   ├── journal.html                 # /journal
│   ├── performance.html             # /performance
│   └── volatility.html              # /volatility
│
└── pine/                            # Pine Script reference
    └── crypto_optimized_strategy.pine
```

---

# Core Systems Explained

## System 1: Data Acquisition

### Delta REST Client (`delta_client.py`)
- Signed HMAC authentication for private endpoints
- Fetches perp candles (BTCUSD, ETHUSDT, SOLUSDT; 1m, 5m, 1h, 4h)
- Retrieves option chain (all expirations, strikes, bid/ask/IV)
- Places/cancels orders, reads balances (testnet only)

### PriceBus (`pricebus.py`)
- Thread-safe in-memory price store
- Populated by Delta WebSocket ticker updates (<100ms)
- Engine reads without REST: `bus.bid(symbol)`, `bus.spot(symbol)`
- Zero-latency access (no network calls in hot path)

## System 2: Trading Engine

### Engine Loop (`engine.py`)
Runs as background asyncio task, polls every 5–15s:
1. **Manage exits:** TP/SL/time/expiry per position
2. **Bar-clock check:** Skip if no new candle (saves REST calls)
3. **Fetch candles:** Only if bar-clock says new bar
4. **Fetch option chain:** Lazy, cached ~10s
5. **Evaluate strategies:** All enabled, pure functions
6. **Route signals:** To executor
7. **Emit state:** To EventBus → WebSocket clients

### Executor (`executor.py`)
Handles fills and position tracking:
- **Paper mode:** Simulates buy-at-ask, exit-at-bid; applies taker fee + 18% GST
- **Live demo:** Places real market orders via Delta API
- TP/SL logic: % of premium entered
- Position reconciliation every 20 cycles (live mode)
- No double-sells: network-ambiguous orders are marked + reconciled

### The 8 Strategies (`zing_strategies.py`)

| Strategy | TF | Rule |
|----------|----|----|
| EMA Cross | 1m | 9/21 EMA crossover (fires once per cross) |
| Scalping Pulse | 1m | Trend + pullback to fast EMA + confirm candle |
| Traffic Light | 1m | Two-candle colour-flip; break high→CE, low→PE |
| Inside Candle | 5m | Inside-bar breakout, ≤35% proximity; 2:1 R:R |
| Mean Reversion Bollinger | 1m | Fade extremes: above upper→PE, below lower→CE |
| Prime Scalper EMA | 1m | ATR-normalized EMA slope vs threshold |
| SwingKing Sniper | 5m | High-conviction 20/50 EMA trend + pullback |
| Booming Bulls Supertrend | 1m | SMA + Supertrend on option premium; 7.5%/5% |

**Key:** All stateless, pure functions. Input: candles + chain + spot. Output: Signal or None.

### Journal & Storage
- **JSONL:** Every event (signal, fill, exit, error)
- **SQLite:** Closed trades only (what traders see)
- Filterable by strategy, symbol, date, P&L

## System 3: Research & Backtesting

### Backtest (`backtest.py`)
- Replays exact same `evaluate()` logic over historical OHLCV
- Pure function: no I/O
- Returns directional accuracy, R/trade, signal count
- Input: historical data from Binance (cached .npz)

### Walk-Forward Screen (`research.py`)
- Train/test/holdout split: 60% train, 20% test, 20% holdout
- Scores: # profitable signals, avg R/trade, Sharpe, max drawdown
- Rejects strategies that don't survive both train AND test
- Gate for new ideas before live deployment

### Volatility Lab (`volatility.py`)
- Collects 30-minute IV surface snapshots
- Computes variance risk premium (implied vol vs realized vol)
- Stores history for forward series analysis
- Runs even when engine is stopped

## System 4: Real-Time Communication

### WebSocket Server (`main.py:/ws/app`)
- Topic-based subscriptions: `sub_chain`, `sub_engine`, etc.
- Broadcasts to connected clients
- No authentication required (public market data only)

### AppHub Multiplexer (`main.py`)
- **ChainStreamer:** Fetches structure once, overlays live prices from PriceBus
- **chain_publisher():** Emits `chain` topic every 0.5s
- **engine_publisher():** Emits `engine` topic every 1.0s (even when stopped)
- **state_broadcaster():** Drains EventBus every 100ms → broadcasts

### EventBus (`eventbus.py`)
- Bounded queue (maxsize=2000)
- Bridges engine thread → asyncio WebSocket layer
- Drops oldest on overflow (doesn't block engine)

### Client-Side WebSocket (`static/js/wsclient.js`)
- Singleton `window.AppWS`
- Exponential backoff + jitter on disconnect
- 15s heartbeat, 45s stall detector
- Outbound queue (messages sent even if down, drained on reconnect)
- Visibility-change + online event listeners (fast reconnect)

## System 5: Health & Diagnostics

### Health Endpoint (`diagnostics.py`)
- Auth status: API key valid? IP whitelisted?
- Engine state: running? how many positions?
- Data freshness: last candle? last chain? last price?
- Recent errors: auth fail? order reject? reconciliation mismatch?

---

# API Routes & WebSocket

## Pages (HTML Routes)
```
GET /                    → dashboard.html
GET /option-chain        → option_chain.html
GET /strategy            → strategies.html
GET /journal             → journal.html
GET /performance         → performance.html
GET /backtest            → backtest.html
GET /research            → research.html
GET /volatility          → volatility.html
```

## WebSocket
```
WebSocket /ws/app
├── Actions:
│   ├── sub_chain        → subscribe to chain updates
│   ├── unsub_chain      → unsubscribe from chain
│   ├── sub_engine       → subscribe to engine state
│   └── unsub_engine     → unsubscribe from engine
└── Topics:
    ├── chain            → option chain data (~0.5s)
    ├── engine           → engine state (~1.0s)
    └── health           → broadcast errors
```

## Strategy Control (JSON REST)
```
GET  /api/strategy/status           → engine state, positions, stats, strategy list
POST /api/strategy/start            → start engine
POST /api/strategy/stop?flatten=... → stop + optionally close positions
POST /api/strategy/toggle           → {slug, enabled}
POST /api/strategy/flatten          → close all positions
GET  /api/strategy/journal?limit=50 → trade feed (JSONL)
```

## Live Data (REST Fallback)
```
GET /api/chain?asset=BTC&expiry=LATEST&strikes=20  → option chain
GET /api/spot?symbol=BTCUSD                        → spot price
```

## Account (Signed)
```
GET /api/account                    → testnet balances + positions (Delta API)
```

## Analytics (Expensive Compute)
```
GET /api/performance                → closed trades, equity curve, stats
GET /api/backtest?strategy=...      → replay scores
GET /api/research/families          → walk-forward results
GET /api/volatility/surface?expiry  → live IV heatmap
GET /api/volatility/vrp             → VRP time series
```

## Health
```
GET /api/health                     → auth, engine, data freshness, errors
```

---

# Data Flows

## Flow 1: Live Price Update (Target: 50ms Roundtrip)
```
Delta WebSocket v2/ticker
  ↓ [<100ms per tick]
PriceBus.update(ticker)  [thread-safe in-memory]
  ↓
Engine cycle: PriceBus.bid(symbol)  [zero-latency]
  ↓
Executor uses live bid/ask
  ↓
EventBus.emit({"engine": state})
  ↓
state_broadcaster() drains every 100ms
  ↓
AppWS.broadcast(topic="engine")
  ↓
Browser: window.AppWS receives frame
  ↓
strategies.js updates DOM (position feed, live P&L)
```

## Flow 2: Strategy Signal → Order (5s Poll + Gate Logic)
```
Engine.poll() [every 5–15s]:
  1. Manage exits (TP/SL/time/expiry) [per position]
  2. Bar clock: skip if no new candle [saves REST]
  3. Fetch option chain [lazy, cached ~10s]
  4. For each enabled strategy:
       - Evaluate(candles, chain, spot)  [pure function]
       - If Signal → Executor.execute(signal)
  5. Executor:
     - Paper: simulated fill (bid/ask), fee calc, P&L
     - Live demo: POST /v2/orders (real testnet order)
  6. Journal → SQLite + JSONL
  7. Emit state → EventBus → WS clients
```

## Flow 3: Backtest Path (Minutes)
```
Backtest harness:
  1. Load historical OHLCV [cached .npz from Binance]
  2. For each bar:
     - Evaluate(candles, chain, spot)  [pure, no I/O]
     - Collect signal + score
  3. Results: directional accuracy, R/trade, Sharpe
```

## Flow 4: Walk-Forward Research (5–30 min)
```
Research harness:
  1. Define train/test/holdout split [60/20/20]
  2. For each strategy:
     - Train: replay on train bars, score
     - Test: replay on test bars, score
     - Holdout: reserve for final check
  3. Keep only strategies with both train AND test positive
  4. Store results → JSON
```

---

# Configuration (`.env`)

```ini
# Delta Exchange (testnet credentials)
DELTA_API_BASE = https://api.india.delta.exchange
DELTA_API_KEY = <your-testnet-key>
DELTA_API_SECRET = <your-testnet-secret>

# Execution Mode
EXECUTION_MODE = paper              # or live_demo

# Engine Configuration
ENGINE_ASSET = BTC                  # or ETH
ENGINE_CONTRACTS = 1                # contracts per trade
ENGINE_POLL_SECONDS = 15            # evaluation interval (5s floor)
ENGINE_AUTOSTART = false            # start on boot?
ENGINE_MAX_OPEN = 8                 # position cap
ENGINE_MAX_HOLD_BARS = 60           # time-based exit (candles)
ENGINE_COOLDOWN_BARS = 5            # post-exit strategy cooldown
ENGINE_ENABLED_STRATEGIES = []      # [] = all; ["ema-cross", ...] = subset
```

---

# Recent Fixes (2026-07-21)

## Bug #1: Canvas Height Growth on Option Chain Charts

### Symptom
OI bar chart and IV Smile chart on `/option-chain` grew taller by ~10% on every WebSocket data update (every 0.5s), causing unbounded UI expansion over 10–20 minutes.

### Root Cause (Technical)
- **Pattern:** `const h = canvas.clientHeight; canvas.height = h * dpr;`
- Canvas element's `height` attribute (intrinsic pixel size) overwrites the HTML attribute
- Next frame reads `getAttribute("height")` → returns inflated pixel value, not original CSS height
- On DPR=1.25: each frame 300 → 375 → 469 → 586 → ... (exponential growth)

### Solution

**`static/js/option_chain.js:234`** — `setupCanvas()` function:
```js
// BEFORE (buggy)
var cssH = canvas.getAttribute("height") ? parseInt(canvas.getAttribute("height"), 10) : 300;
canvas.width = cssW * dpr;
canvas.height = cssH * dpr;  // overwrites height attribute; next read is corrupted

// AFTER (fixed)
var cssH = parseInt(canvas.dataset.h || "300", 10);  // read from data-h, never overwritten
canvas.style.width = cssW + "px";
canvas.style.height = cssH + "px";  // lock CSS height
canvas.width = cssW * dpr;
canvas.height = cssH * dpr;  // pixel buffer for DPR
```

**`templates/option_chain.html:77,81`** — Canvas element:
```html
<!-- BEFORE -->
<canvas id="oi-chart" height="300"></canvas>

<!-- AFTER -->
<canvas id="oi-chart" data-h="300"></canvas>
```

### Verification
After 4 seconds of 0.5s updates (8 frames):
- `document.getElementById('oi-chart').clientHeight` → still 300px (no growth)
- Rendering crisp on high-DPR screens

### Lesson Learned
- Canvas intrinsic size (`canvas.width` / `canvas.height` attributes) ≠ CSS size
- DPR scaling requires careful separation: lock CSS, scale only pixel buffer
- Never read from `canvas.clientHeight` after writing to `canvas.height` in same cycle

---

## Bug #2: 5-Second Delay on Option Chain & Strategy Pages

### Symptom
`/option-chain` and `/strategy` pages felt sluggish; data updated only every 5+ seconds despite advertised 0.5s push.

### Root Causes (Multiple)

#### Part A: Auto-Refresh Flag Defaulted to False
- `option_chain.js` initialized `state.autoRefresh = false`
- WebSocket frames arrived (verified: 7 frames per 2.5 seconds)
- But handler bailed: `if (!state.autoRefresh) return;` — all frames discarded
- HTML checkbox was not `checked` by default

**Solution:**
- `templates/option_chain.html:28`: Added `checked` to input
- `static/js/option_chain.js`: Changed initial value to `true`

#### Part B: Engine Only Published on Cycle Completion
- Engine ran every 15 seconds (default `ENGINE_POLL_SECONDS`)
- State emitted to EventBus only on cycle end
- If engine stopped: zero frames, fell back to 5s health tick
- Frontend fell back to polling, felt like "5s lag"

**Solution:**
- Added background task `engine_publisher()` in `main.py`:
  - Asyncio task publishing `engine` state every 1.0s regardless of run state
  - Runs independently from engine loop
  - Ensures steady updates even when engine stopped

- Added background task `chain_publisher()` in `main.py`:
  - Fetches option chain + broadcasts `chain` topic every 0.5s
  - Uses ChainStreamer: fetches structure once (REST), overlays live prices from PriceBus (zero-latency)
  - Reduces REST overhead

#### Part C: Routing Error in WebSocket Decorator
- `@app.websocket` decorator placed **before** `app = FastAPI()`
- Reference to undefined `app` → server wouldn't boot

**Solution:**
- Moved route definition to after `app = FastAPI(...)`

### Verification
- `/option-chain` now updates ~0.5s (8 frames per 4 seconds)
- `/strategy` now updates ~1.0s (4 frames per 4 seconds)
- Status line shows "Live · 50+ contracts streaming"

---

# Common Tasks with Examples

## Task 1: Add a New Strategy

**File:** `strategy/zing_strategies.py`

```python
from strategy.base import Signal, Strategy

def my_strategy_rule(candles, chain, spot, context):
    """
    Pure function: candles + chain + spot → Signal or None
    - candles: list of bar dicts (o, h, l, c, volume, time)
    - chain: dict with ATM CE/PE contract details
    - spot: current spot price
    - context: execution context
    """
    # Calculate indicators
    closes = [bar['c'] for bar in candles]
    ema9 = ema(closes, 9)
    ema21 = ema(closes, 21)
    
    # Check conditions
    if ema9[-1] > ema21[-1] and ema9[-2] <= ema21[-2]:
        return Signal(direction='BUY', reason='9/21 EMA cross up', strength=0.8)
    elif ema9[-1] < ema21[-1] and ema9[-2] >= ema21[-2]:
        return Signal(direction='SELL', reason='9/21 EMA cross down', strength=0.8)
    
    return None

# Register in STRATEGIES dict
STRATEGIES = {
    "my-strategy": Strategy(
        slug="my-strategy",
        name="My Strategy",
        rule=my_strategy_rule,
        description="My custom strategy rule",
    ),
    ...
}
```

**Test it:**
1. Backtest: `GET /api/backtest?strategy=my-strategy`
2. Check directional score
3. If positive, consider walk-forward: `GET http://localhost:8000/research`

---

## Task 2: Debug Why Engine Won't Start

**Checklist:**

1. **Check auth:**
   ```powershell
   curl http://localhost:8000/api/health
   ```
   - Look for `auth_status.api_key_valid` (true?)
   - Look for `auth_status.ip_whitelisted` (true?)
   - If false: Log shows your IP; add it to demo.delta.exchange

2. **Check data freshness:**
   - `last_chain_update` should be recent (<10s)
   - `last_candle_update` should be recent (<10s)
   - If old: Delta WebSocket might be down

3. **Check funds:**
   ```powershell
   curl http://localhost:8000/api/account
   ```
   - Testnet balance > 0?

4. **Check config:**
   - `.env` → `ENGINE_AUTOSTART=false` by default (manually click Start)
   - Strategy enabled? (POST `/api/strategy/toggle {slug: "...", enabled: true}`)

---

## Task 3: View Live Trades

**Real-time:** `http://localhost:8000/strategy` → "Signal Feed" panel (WebSocket live)  
**History:** `GET /api/strategy/journal?limit=50` → JSON array

```json
{
  "timestamp": "2026-07-21T12:34:56Z",
  "event": "fill",
  "strategy": "ema-cross",
  "direction": "BUY",
  "symbol": "BTC",
  "price": 42500.0,
  "quantity": 1,
  "fee": 80.5,
  "pnl": null,
  "status": "open"
}
```

**Filtered table:** `http://localhost:8000/journal`

---

## Task 4: Backtest a Strategy Change

1. Edit `strategy/zing_strategies.py` → modify rule function
2. Navigate to `http://localhost:8000/backtest`
3. Select your strategy from dropdown
4. Click "Run Backtest" (offline replay)
5. Check results: directional accuracy %, avg R/trade, signal count
6. If negative: Don't go live

---

## Task 5: Run Walk-Forward Research

1. Navigate to `http://localhost:8000/research`
2. Select symbol + timeframe + parameter range
3. Click "Run Screen" (can take 5–30 min)
4. Results: train accuracy, test accuracy, holdout accuracy
   - Green = both train AND test positive → maybe real edge
   - Red = train-only or negative test → overfitting

---

## Task 6: Check IV Surface / VRP

1. Navigate to `http://localhost:8000/volatility`
2. Heatmap: IV% by strike (left = far put, right = far call)
3. Line chart: VRP trend (30-min snapshots collected automatically)
   - Green bar = implied > realized (unusual)
   - Red bar = implied < realized (common)

---

## Task 7: Deploy to Live Demo (Real Testnet Orders)

⚠️ **WARNING: This sends real orders to testnet.**

1. **Prep:** Backtest passes? Walk-forward pass? (They won't.)
2. **Config:** `.env` → `EXECUTION_MODE=live_demo`
3. **Check:** `GET /api/account` → testnet balance > 0
4. **Run:** Restart server
5. **Go:** Navigate to `/strategy` → enable strategy → click Start
6. **Monitor:** Watch signal feed; check fills in `/journal`
7. **Exit:** Click "Stop" (closes all positions)

---

# Testing Workflow

## For Strategy Changes

```
1. Edit zing_strategies.py
2. GET http://localhost:8000/backtest?strategy=<slug>
   → Verify directional score
3. GET http://localhost:8000/strategy
   → Enable strategy, click Start (paper mode)
   → Watch signal feed for 30 seconds
   → Check fills in /journal
   → Click Stop
```

## For UI Changes

```
1. Edit static/js/*.js or templates/*.html
2. Refresh browser (Ctrl+F5 to bypass cache)
3. Check browser console (F12 → Console) for errors
4. Test responsive layout on mobile (F12 → toggle device mode)
```

## For Backend/API Changes

```
1. Edit strategy/*.py or main.py
2. Restart server (Ctrl+C, re-run uvicorn)
3. Test via curl:
   curl -X POST http://localhost:8000/api/strategy/start
4. Check main.py stdout for logs + exceptions
```

## Verify Canvas Fix

```
1. Navigate to http://localhost:8000/option-chain
2. DevTools: F12 → Console
3. Run: document.getElementById('oi-chart').clientHeight
   → Should be 300
4. Wait 30 seconds (60+ updates at 0.5s)
5. Run again: document.getElementById('oi-chart').clientHeight
   → Still 300, not growing ✓
```

## Verify WebSocket Latency

```
1. Navigate to http://localhost:8000/strategy
2. Watch timestamps in "Signal Feed" panel
3. Every frame ~1.0s apart (engine topic) ✓
4. Navigate to `/option-chain` table
5. Timestamps update ~0.5s apart (chain topic) ✓
```

---

# Known Issues & Gotchas

## Gotcha 1: IP Whitelisting
**Problem:** Dynamic ISP reassigns your IP; orders fail with `ip_not_whitelisted_for_api_key`  
**Solution:** Check `/api/health` for your logged IP; re-whitelist on demo.delta.exchange

## Gotcha 2: Option Expiry
**Problem:** Options expire 12:00 UTC; engine fails to resolve ATM if <5 min to close  
**Solution:** Hardened to reconcile vs. exchange every 20 cycles (live mode)

## Gotcha 3: Candle Lag
**Problem:** Engine polls every 5–15s, but candles close on :00s; may miss bars  
**Solution:** Lower poll interval or add bar-clock backfill

## Gotcha 4: Paper Fills Assume No Slippage
**Problem:** Testnet liquidity thin; real fills may differ from simulated  
**Solution:** Verify empirically on live_demo; monitor `/journal` for orphaned fills

## Gotcha 5: Cooldown Prevents Re-Entry Too Fast
**Problem:** After exit, same (strategy, direction) blocked for 5 bars  
**Solution:** Edit `ENGINE_COOLDOWN_BARS` in `.env`; lower = faster re-entry, higher = safer

---

# Debug Checklist

## App Won't Start
- [ ] Python 3.10+? Run `python --version`
- [ ] Venv activated? Run `.\.venv\Scripts\python`
- [ ] Port 8000 free? Run `netstat -an | findstr 8000`
- [ ] Check `main.py` imports; any circular dependencies?
- [ ] Check `.env` → valid API credentials?

## WebSocket Not Updating
- [ ] Browser console: `window.AppWS.state()` → active connection?
- [ ] Network tab (F12): any 401/403/500 errors?
- [ ] Stall detector active? (45s auto-reconnect)
- [ ] Subscribed to right topic? (`sub_chain`, `sub_engine`)

## Engine Won't Start
- [ ] `/api/health` → auth status valid?
- [ ] `/api/health` → IP whitelisted?
- [ ] `/api/health` → data fresh (<10s)?
- [ ] `.env` → `ENGINE_AUTOSTART=false` by default (click Start manually)
- [ ] Strategy enabled? (POST `/api/strategy/toggle`)

## Trades Orphaned (Position Open, No Exchange Order)
- [ ] Network glitch during order placement?
- [ ] Check `/api/account` (reads testnet positions)
- [ ] Engine reconciles every 20 cycles; check `/journal` for `reconcile_mismatch`
- [ ] If reconcile fails: manually close via `/api/strategy/flatten`

## Charts Not Rendering
- [ ] Canvas element exists? Check source → `<canvas id="oi-chart">`
- [ ] JS error? F12 → Console tab
- [ ] No data? Check WS connection + `/api/chain` response

---

# File Reference Guide

| Change | File | Type |
|--------|------|------|
| Strategy logic | `strategy/zing_strategies.py` | Python |
| Execution (fills, TP/SL) | `strategy/executor.py` | Python |
| Indicators (SMA, EMA, ATR) | `strategy/indicators.py` | Python |
| WebSocket events | `main.py` | Python |
| Option chain UI | `templates/option_chain.html` + `static/js/option_chain.js` | HTML + JS |
| Strategy UI (engine control) | `templates/strategies.html` + `static/js/strategies.js` | HTML + JS |
| Dashboard | `templates/dashboard.html` + `static/js/health.js` | HTML + JS |
| Performance analytics | `templates/performance.html` + `static/js/performance.js` | HTML + JS |
| Trade table | `templates/journal.html` + `static/js/journal.js` | HTML + JS |
| Backtest results | `templates/backtest.html` + `static/js/backtest.js` | HTML + JS |
| IV surface | `templates/volatility.html` + `static/js/volatility.js` | HTML + JS |
| All API routes | `main.py` | Python |
| Trade persistence | `strategy/store.py` | Python |
| Health check | `strategy/diagnostics.py` | Python |

---

# Running the Project

## 1. Setup (5 min)

```powershell
cd D:\TRADER\delta_trading_app
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

## 2. Configure (2 min)

Edit `.env`:
```ini
DELTA_API_KEY=<your-testnet-key>
DELTA_API_SECRET=<your-testnet-secret>
EXECUTION_MODE=paper
ENGINE_AUTOSTART=false
```

## 3. Run (1 min)

```powershell
.\.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

## 4. Browse (2 min)

- **Dashboard:** http://localhost:8000
- **Option Chain:** http://localhost:8000/option-chain
- **Strategy Engine:** http://localhost:8000/strategy
- **Performance:** http://localhost:8000/performance
- **Backtest:** http://localhost:8000/backtest
- **Research:** http://localhost:8000/research
- **Journal:** http://localhost:8000/journal
- **Volatility:** http://localhost:8000/volatility

## 5. Test (5 min)

1. Navigate to `http://localhost:8000/strategy`
2. Toggle a strategy (checkbox)
3. Click "Start"
4. Watch "Signal Feed" for 30 seconds
5. Check `/journal` for trade events
6. Click "Stop" to close all positions

---

# Mental Models (Copy These)

## Data Flow: Live Price → Browser
```
Delta WebSocket (ticker, <100ms)
  ↓
PriceBus (in-memory cache)
  ↓
Engine reads (zero-latency)
  ↓
Signals routed to Executor
  ↓
EventBus (bounded queue)
  ↓
state_broadcaster (drains every 100ms)
  ↓
AppWS (WebSocket)
  ↓
Browser (JavaScript updates DOM)
```

## Data Flow: Strategy Signal → Trade
```
Engine polls every 5–15s
  ↓ [bar-clock check]
Fetch candles + chain (lazy, cached)
  ↓
Evaluate all strategies (pure functions)
  ↓
Executor receives signal
  ↓ [paper: simulated; live_demo: real order]
Position tracked + journal logged
  ↓
EventBus emits state
  ↓
WebSocket clients receive update
  ↓
Browser updates (P&L, positions, signal feed)
```

## Data Flow: Backtest/Research
```
Load historical OHLCV (cached .npz)
  ↓
Replay strategy.evaluate() per bar (pure)
  ↓
Collect signals + scores
  ↓
Train/test/holdout split (60/20/20)
  ↓
Report results
```

---

# Performance Notes

- **Bar-clock gating:** If poll is 5s and bars are 1m, 11/12 cycles skip candle fetch (no new bar)
- **Chain lazy-load:** Only fetched if strategy could act (time-of-day, data freshness)
- **Price reads:** PriceBus is in-memory; zero-latency (no REST per tick)
- **Backtest:** Pure function replay; single-threaded; fast for small datasets
- **Walk-forward:** 60/20/20 split × combos × 8 strategies; can take 5–30 min
- **Option chain fetches:** 32→7 across 8 cycles (bar-clock + cache)
- **Candle fetches:** 8→1 across 8 cycles (bar-clock optimization)

---

# Key Technical Decisions Explained

## Decision 1: PriceBus (In-Memory, Not Redis)
**Trade-off:** Single-process (no network latency) vs. scalability  
**Chosen:** In-memory (engine in same process; zero I/O in hot path)

## Decision 2: Paper vs. Live Demo
**Trade-off:** Speed/iteration vs. real order flow  
**Chosen:** Paper default (safe); one-line `.env` switch to live_demo

## Decision 3: Canvas DPR Scaling (Now Fixed)
**Trade-off:** High-DPI crisp rendering vs. feedback loop bug  
**Chosen:** Lock CSS height, derive pixel math from container (eliminated growth)

## Decision 4: WebSocket Frame Rate
**Trade-off:** Responsiveness (0.5s) vs. bandwidth  
**Chosen:** 0.5s for chain, 1.0s for engine (feels live, not bandwidth-heavy)

## Decision 5: Stateless Strategy Evaluation
**Trade-off:** Simplicity vs. context awareness  
**Chosen:** Pure functions (easier to backtest, debug, parallelize)

---

# Next Steps for Incoming AI

1. ✅ Read this entire document (you're doing it!)
2. ✅ Run `uvicorn main:app --host 127.0.0.1 --port 8000`
3. ✅ Navigate to `/strategy` and enable a strategy
4. ✅ Click "Start" and watch signal feed for 1 minute
5. ✅ Check `/journal` for trade events
6. ✅ Click "Stop" to close positions
7. ✅ Review `/api/health` to understand engine state
8. ✅ Trace one full cycle: `engine.py:run_engine()` → strategy rule → executor → journal
9. ✅ Run a backtest: `/backtest?strategy=ema-cross`
10. ✅ Make a small change and re-backtest

---

# Summary

### What You Have
✅ Complete trading engine (8 strategies)  
✅ Live options chain explorer  
✅ Paper + live_demo execution  
✅ Trade journal (SQLite + JSONL)  
✅ Performance analytics (equity curve, Sharpe, max DD)  
✅ Backtesting harness (pure replay)  
✅ Walk-forward research (train/test validation)  
✅ IV surface + VRP lab  

### What You Should Know
❌ No validated edge (all strategies tested negative)  
🧪 Testnet-only (no real money)  
📊 Paper is default (safe for iteration)  
🔄 One-line `.env` switch to live_demo  
⚠️ Do not deploy capital  

### How to Start
1. Read this document
2. `uvicorn main:app --host 127.0.0.1 --port 8000`
3. Navigate to `/strategy`
4. Enable a strategy, click Start
5. Watch trades flow

**Welcome to the project! 🚀**

---

**Generated:** 2026-07-21  
**Status:** Phase 4 Complete · All documentation consolidated  
**For:** Incoming AI / Next Developer  
**Questions?** Review the relevant section above or check `/api/health` for diagnostics

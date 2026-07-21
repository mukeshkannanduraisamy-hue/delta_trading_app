# Delta Trading App — Project Structure & Architecture

**Status:** Phase 4 Complete (Strategy Engine) · Phases 5–7 Complete (Journal, Backtest, Health)
**Verdict:** No validated edge found; testnet-only; paper mode default

---

## Project Overview

A **FastAPI-based live cryptocurrency options trading web application** that:
- Connects to Delta Exchange India (testnet via REST/WebSocket)
- Implements 8 quantitative option strategies from Zing Trade blog
- Executes trades as **paper trades** (simulation) or **real testnet orders**
- Provides real-time UI dashboards, backtesting, walk-forward research, and risk analysis

**Architecture:** Python FastAPI backend + Jinja2 templates + vanilla JS frontend · WebSocket async duplex communication · SQLite trade journal · Redis-like in-memory price bus

---

## Directory Structure

```
D:\TRADER\delta_trading_app/
├── main.py                          # FastAPI app entry point
├── requirements.txt                 # Python dependencies
├── .env                             # Configuration (EXECUTION_MODE, ENGINE_ASSET, etc.)
├── PHASE4_STRATEGY_ENGINE.md        # Full system documentation
│
├── strategy/                        # Core trading engine & research package
│   ├── __init__.py
│   ├── base.py                      # Signal + Strategy abstract contracts
│   ├── config.py                    # Constants, env vars, fee models, endpoints
│   ├── delta_client.py              # REST client (HMAC auth) for signed candle/order/balance calls
│   ├── market_data.py               # Expiry/ATM CE/PE resolution + live bid pricing
│   ├── indicators.py                # SMA, EMA, ATR, Bollinger Bands, Supertrend (no numpy)
│   ├── zing_strategies.py           # 8 strategies + signal registry
│   ├── engine.py                    # Main poll loop: fetch → evaluate → route → manage
│   ├── executor.py                  # Paper + live_demo fills, position management, TP/SL exits
│   ├── eventbus.py                  # Bounded queue (asyncio ↔ worker thread bridge)
│   ├── pricebus.py                  # Thread-safe in-memory price store (no DB calls in hot path)
│   ├── journal.py                   # Append-only JSONL + SQLite trade log
│   ├── backtest.py                  # Offline strategy evaluation (same evaluate() logic, pure)
│   ├── research.py                  # Walk-forward screen (train/test split validation)
│   ├── store.py                     # SQLite trade persistence (journal/trades.db)
│   ├── volatility.py                # IV surface + VRP collector (30min snapshots)
│   ├── ai_strategy.py               # Placeholder for AI-based strategies
│   ├── diagnostics.py               # Health check, auth validation, data freshness
│   ├── optimizer.py                 # Backtesting search harness
│   ├── datafeed.py                  # Historical Binance klines loader + cache (.npz)
│   ├── run_optimization.py          # Offline optimization entry point
│   │
│   ├── cache/                       # Cached OHLCV data (compressed .npz files)
│   │   └── *.npz                    # BTCUSDT_1h_5y.npz, etc. (one per symbol/timeframe/period)
│   │
│   └── journal/                     # Trade persistence
│       ├── trades.db                # SQLite: closures only (real journal of law)
│       └── trades.jsonl             # Human-readable JSONL log (all events)
│
├── static/                          # Frontend assets
│   ├── css/
│   │   └── style.css                # Unified dark-theme stylesheet
│   │
│   └── js/
│       ├── app.js                   # Common app init + navbar/connection status
│       ├── wsclient.js              # Window.AppWS singleton; reconnect + heartbeat + topics
│       ├── chart.js                 # Lightweight-charts wrapper
│       │
│       ├── (per-page controllers)
│       ├── option_chain.js          # /option-chain: chain table + OI chart + IV smile chart
│       ├── strategies.js            # /strategy: engine toggle + live signal feed
│       ├── backtest.js              # /backtest: replay scoring UI
│       ├── research.js              # /research: walk-forward harness UI
│       ├── journal.js               # /journal: trade table + filtering
│       ├── performance.js           # /performance: P&L, equity curve, stats
│       ├── volatility.js            # /volatility: IV surface lab
│       ├── health.js                # /: dashboard + health strip
│       │
│       └── lib/
│           └── lightweight-charts.standalone.production.js
│
├── templates/                       # Jinja2 HTML pages
│   ├── base.html                    # Layout: nav, connection status, sidebar
│   ├── dashboard.html               # /: home + health strip
│   ├── option_chain.html            # /option-chain: chain table + charts
│   ├── strategies.html              # /strategy: engine control
│   ├── backtest.html                # /backtest: result display
│   ├── research.html                # /research: walk-forward results
│   ├── journal.html                 # /journal: trade log
│   ├── performance.html             # /performance: analytics
│   ├── volatility.html              # /volatility: IV lab
│   └── (missing: doesn't exist yet)
│
└── pine/                            # Pine Script strategy definitions (reference only)
    └── crypto_optimized_strategy.pine
```

---

## Core Systems

### 1. **Backend (FastAPI, Python)**

#### Data Acquisition
- **Delta REST client** (`delta_client.py`): Signed HMAC authentication
  - Fetches perp candles (BTCUSD, ETHUSDT, SOLUSDT; 1m, 5m, 1h, 4h)
  - Retrieves option chain (all expirations, strikes, bid/ask/IV)
  - Places / cancels orders, reads balances (testnet only)
  
- **PriceBus** (`pricebus.py`): Thread-safe in-memory store populated every WebSocket tick
  - Live bid/ask for options + underlyings
  - Zero-latency reads from trading engine (no REST in hot path)
  - `bus.bid(symbol)`, `bus.spot(symbol)`, `bus.update(ticker)`

#### Trading Engine
- **Engine** (`engine.py`): Main loop (5–15s poll via background asyncio task)
  1. Manage exits: TP/SL/time/expiry
  2. Fetch candles (bar-clock gating; skips if no new bar)
  3. Fetch option chain (if a strategy could act)
  4. Evaluate all enabled strategies
  5. Route signals → executor
  6. Persist to journal

- **Executor** (`executor.py`): Fills + position tracking
  - **Paper mode (default):** Simulates buy-at-ask, exit-at-bid; applies 18% GST + taker fee
  - **Live demo:** Places real market orders on testnet via Delta API
  - TP/SL logic: % of premium entered (Nifty rules adapted to crypto)
  - Position reconciliation every 20 cycles (live mode)

- **Strategies** (`zing_strategies.py`): 8 rules, all stateless per cycle
  1. EMA Cross (9/21, 1m)
  2. Scalping Pulse (trend + pullback, 1m)
  3. Traffic Light (candle colour-flip, 1m)
  4. Inside Candle (breakout, 5m, proximity filter)
  5. Mean Reversion Bollinger (band extremes, 1m)
  6. Prime Scalper EMA (ATR slope, 1m)
  7. SwingKing Sniper (20/50 EMA trend, 5m)
  8. Booming Bulls Supertrend (option premium SMA + Supertrend, 1m)

  Each produces `Signal(direction, reason, strength)` → executor acts or skips.

- **Journal** (`journal.py` + `store.py`): Trade log
  - JSONL: every event (signal, fill, exit, error)
  - SQLite: closed trades only (what traders see)
  - Filterable by strategy, symbol, date, P&L

#### Real-Time Communication
- **WebSocket Server** (`main.py:/ws/app`)
  - `sub_chain` / `unsub_chain`: Option chain updates (~0.5s)
  - `sub_engine` / `unsub_engine`: Strategy engine state (~1.0s when running)
  - Topic-based routing; AppHub fan-out
  
- **EventBus** (`eventbus.py`): Thread-safe queue from engine → asyncio WS layer
  - Bounded (maxsize=2000), drops oldest on overflow
  - Bridged by `state_broadcaster()` task (drains every 100ms)

- **AppHub** (`main.py`): Multiplexer for live data streams
  - `ChainStreamer`: Fetches structure once, overlays live prices from PriceBus
  - `chain_publisher()`: Emits `chain` topic every 0.5s
  - `engine_publisher()`: Emits `engine` topic every 1.0s (regardless of run state)
  - `state_broadcaster()`: Drains EventBus → WebSocket clients

#### Research & Backtesting
- **Backtest** (`backtest.py`): Offline walk-through
  - Replays exact same `evaluate()` logic over historical data
  - Pure function: no I/O, returns signal score per bar
  - Input: historical OHLCV (cached from Binance via `datafeed.py`)
  
- **Research** (`research.py`): Walk-forward validation
  - Train/test split: (60% train, 20% test, 20% holdout)
  - Scores: # profitable signals, avg R/trade, Sharpe, max drawdown
  - Rejects strategies that don't survive both train AND test
  
- **Volatility** (`volatility.py`): IV surface lab
  - Collects 30-minute snapshots of full chain (IV, delta, expiry, strike)
  - Computes variance risk premium (implied vol vs realized vol)
  - Stores history for forward series analysis

- **Optimizer** (`optimizer.py`): Parameter search harness (run_optimization.py entry)
  - Genetic algorithm / grid search over strategy parameter ranges
  - Outputs optimization_report.json with Pareto frontier

#### Configuration & Health
- **Config** (`config.py`): `.env`-driven
  - `EXECUTION_MODE`: `paper` or `live_demo`
  - `ENGINE_ASSET`: BTC / ETH
  - `ENGINE_CONTRACTS`: contracts per trade
  - `ENGINE_POLL_SECONDS`: 5–900 (gate: floored at 5s)
  - `ENGINE_AUTOSTART`: boot the engine?
  - `ENGINE_MAX_OPEN`: position cap
  - `ENGINE_MAX_HOLD_BARS`: time-based exit
  
- **Diagnostics** (`diagnostics.py`): Health endpoint
  - Auth status (IP whitelisted? API key valid?)
  - Engine state + position count
  - Data freshness (last candle, last chain, last price)
  - Recent errors (auth fail, order reject, reconciliation mismatch)

---

### 2. **Frontend (Vanilla JS + HTML/CSS)**

#### Architecture
- **WebSocket client** (`wsclient.js`): `window.AppWS` singleton
  - Reconnect with exponential backoff + jitter
  - 15s heartbeat (empty frame)
  - 45s stall detector (auto-reconnect if no data)
  - Topic subscriptions: `sub_chain`, `sub_engine`, etc.
  - Outbound queue (messages sent even if connection is down, drained on reconnect)
  - Visibility-change + online event listeners (fast re-connect on tab focus / internet return)

#### Pages

| Route | Purpose | Key UI | Updates |
|-------|---------|--------|---------|
| `/` | Dashboard | Health strip, status, connection | Live |
| `/option-chain` | Chain explorer | Table (OI, IV, Greeks) + OI bar chart + IV smile | 0.5s WS |
| `/strategy` | Engine control | Toggle strategies, Start/Stop, signal feed, live P&L | 1.0s WS |
| `/journal` | Trade log | Filterable table (date, strategy, symbol, P&L) | REST + load-more |
| `/performance` | Analytics | Equity curve (canvas), stats (Sharpe, max DD, win %), P&L histogram | REST |
| `/backtest` | Scoring UI | Strategy selector, results table (edge score, R/trade) | REST |
| `/research` | Walk-forward | Family selector, train/test/holdout results | REST (slow compute) |
| `/volatility` | IV lab | IV surface heatmap, VRP trend, live expiry chain | REST |

#### Key Components

- **option_chain.js** (FIXED BUG: canvas height growth)
  - Fetches `/api/chain` or subscribes to `chain` topic
  - `renderOIChart()`: Bars (call/put OI by strike, ATM marker)
  - `renderIVSmile()`: Line chart (call/put IV by strike)
  - **Fix applied:** Canvas CSS height locked via `data-h` attribute; height derivation reads from fixed container, not inflated canvas

- **strategies.js**
  - POST `/api/strategy/start`, `/api/strategy/stop?flatten=true`
  - POST `/api/strategy/toggle` `{slug: "...", enabled: true/false}`
  - Subscribe to `engine` topic; render position feed, live spot
  - "Demo Wallet" panel: calls `/api/account` to fetch testnet balance (signed)

- **journal.js**
  - GET `/api/strategy/journal?limit=50&offset=...`
  - Filter by strategy, symbol, date range
  - Render with P&L color-coding (green = win, red = loss)

- **performance.js**
  - GET `/api/performance` (expensive: computes equity curve from all closed trades)
  - Canvas equity curve, stats row (total P&L, win rate, profit factor, max drawdown, Sharpe)

- **backtest.js**
  - GET `/api/backtest?strategy=...` (triggers offline replay if not cached)
  - Results: directional accuracy, avg R/trade, signal count

- **research.js**
  - GET `/api/research/families?limit=30` (walk-forward screen results)
  - Results table: train accuracy, test accuracy, holdout accuracy
  - Green = both train AND test positive; red = train-only or negative-test

- **volatility.js**
  - GET `/api/volatility/surface?expiry=...` (live IV surface)
  - Heatmap (strike vs IV%)
  - VRP line chart (last 7 days of 30-min snapshots)

- **health.js** (dashboard strip)
  - GET `/api/health` every 5–10s
  - Renders: auth status, engine state, last update time, error count, connection status

---

## API Routes (FastAPI)

### Pages
- `GET /` → `dashboard.html`
- `GET /option-chain` → `option_chain.html`
- `GET /strategy` → `strategies.html`
- `GET /journal` → `journal.html`
- `GET /performance` → `performance.html`
- `GET /backtest` → `backtest.html`
- `GET /research` → `research.html`
- `GET /volatility` → `volatility.html`

### WebSocket
- `WebSocket /ws/app` (query: `client_id`, `auth`)
  - Actions: `sub_chain`, `unsub_chain`, `sub_engine`, `unsub_engine`
  - Topics: `chain` (~0.5s), `engine` (~1.0s), `health` (broadcast errors)

### Strategy Control (JSON REST)
- `GET /api/strategy/status` → engine state, positions, stats, strategy list
- `POST /api/strategy/start` → start engine
- `POST /api/strategy/stop?flatten=true` → stop + close all positions
- `POST /api/strategy/toggle` `{slug, enabled}` → toggle one strategy
- `POST /api/strategy/flatten` → close all positions immediately
- `GET /api/strategy/journal?limit=50&offset=0` → trade feed (JSONL)

### Live Data
- `GET /api/chain?asset=BTC&expiry=LATEST&strikes=20` → current option chain (REST fallback)
- `GET /api/spot?symbol=BTCUSD` → spot price (REST fallback)

### Account
- `GET /api/account` → testnet demo balances + positions (signed Delta API call)

### Analytics
- `GET /api/performance` → closed trades, equity curve, stats
- `GET /api/backtest?strategy=...` → replay scores
- `GET /api/research/families` → walk-forward results
- `GET /api/volatility/surface?expiry=...` → live IV heatmap
- `GET /api/volatility/vrp` → VRP time series

### Health
- `GET /api/health` → auth status, engine state, data freshness, errors

---

## Data Flows

### Live Price Update Path (50ms roundtrip target)
```
Delta WebSocket v2/ticker
  ↓
PriceBus.update(ticker)  [thread-safe in-memory]
  ↓
Engine cycle: PriceBus.bid(symbol) [zero-latency]
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

### Strategy Signal-to-Order Path (5s poll + gate logic)
```
Engine.poll():
  1. Manage exits (TP/SL/time/expiry)
  2. Bar clock: skip if no new candle
  3. Fetch option chain (lazy, cached ~10s)
  4. For each enabled strategy:
       - Evaluate(candles, chain, spot)
       - If Signal → Executor.execute(signal)
  5. Executor:
     - Paper: simulated fill (bid/ask), fee calc, P&L
     - Live demo: POST /v2/orders (real testnet order)
  6. Journal → SQLite + JSONL
  7. Emit state → EventBus → WS clients
```

### Backtest/Research Path (Minutes)
```
Research harness:
  1. Define train/test/holdout split (60/20/20)
  2. For each strategy:
     - Load historical OHLCV (cached .npz from Binance)
     - Replay backtest.evaluate() per bar (pure)
     - Score: directional accuracy, R/trade, Sharpe
  3. Keep only strategies with both train AND test positive
  4. Store results → JSON
```

---

## Configuration (`.env`)

```ini
# Delta Exchange
DELTA_API_BASE = https://api.india.delta.exchange
DELTA_API_KEY = [signed request key — testnet only]
DELTA_API_SECRET = [HMAC secret]

# Engine
EXECUTION_MODE = paper              # or live_demo
ENGINE_ASSET = BTC                  # or ETH
ENGINE_CONTRACTS = 1                # size per trade
ENGINE_POLL_SECONDS = 15            # evaluation interval (5s floor)
ENGINE_AUTOSTART = false            # start on boot?
ENGINE_MAX_OPEN = 8                 # position cap
ENGINE_MAX_HOLD_BARS = 60           # time exit
ENGINE_COOLDOWN_BARS = 5            # strategy cooldown after close
ENGINE_ENABLED_STRATEGIES = []      # [] = all; ["ema-cross", ...] = subset
```

---

## Key Technical Decisions

1. **PriceBus** (in-memory store, not Redis)
   - Engine runs in the same process; no network latency
   - Populated by upstream Delta WS; reads by engine (zero I/O)
   - Trade-off: single-process; scaling requires separate worker

2. **Paper vs. Live Demo**
   - Paper (default): Fast iteration, repeatable fills, no fee surprises
   - Live demo: Real testnet order flow for operational validation (spread, fills, orphans)
   - One-line `.env` switch; no code branching

3. **Canvas DPR scaling** (bug fixed)
   - High-DPI screens (1.25–2.0× DPR) need pixel-buffer scaling for crisp rendering
   - **Incorrect:** Read `canvas.clientHeight`, write to `canvas.height` → feedback loop grows height every frame
   - **Correct:** Lock CSS height via `style.height` + `data-` attribute; derive pixel math from container, not canvas

4. **WebSocket frame rate**
   - Chain updates: 0.5s (8x per 4 seconds)
   - Engine updates: 1.0s (4x per 4 seconds)
   - Health: On-demand polling (5–10s interval)
   - Trade-off: 0.5s is fast enough for UI responsiveness; lower → bandwidth; higher → feels stale

5. **Strategy evaluation window**
   - Strategies never see partial (forming) candles
   - Fire at most once per closed candle
   - Cooldown prevents immediate re-entry after exit
   - Trade-off: Fewer false signals; slower to respond on real edge

---

## Known Issues / Caveats

1. **No validated edge:** All 8 strategies tested negative in backtest (−0.37 to −1.34 R/trade)
   - VRP test inconclusive (≈0)
   - IV surface normal (no obvious mispricing)
   - Walk-forward screen: 0 survivors across 30-combo grid
   - **Action:** Do not deploy capital. Use for plumbing/ops validation only.

2. **Testnet limits**
   - Liquidity thin on options; premium-based strategies (Booming Bulls) under-perform
   - Market orders pay ~15% spread; limit orders rarely fill
   - Paper fills assume your size doesn't move the book

3. **IP whitelist gotcha**
   - Delta testnet keys are bound to whitelisted IPs
   - If your ISP reassigns your IP, orders fail with `ip_not_whitelisted_for_api_key`
   - Journal logs the exact client IP for debugging

4. **Data freshness gaps**
   - Candles: 1s–2s lag (Delta publishes on close; we poll every 5s)
   - Chain: Varies per level (top-of-book live; deep book ~5s old)
   - Spot: Live (from PriceBus ticker)

---

## Running the Project

### 1. Setup
```bash
cd D:\TRADER\delta_trading_app
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2. Configure
```bash
# .env
DELTA_API_KEY=<your-testnet-key>
DELTA_API_SECRET=<your-testnet-secret>
EXECUTION_MODE=paper
ENGINE_AUTOSTART=false
```

### 3. Run
```bash
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 4. Browse
- Dashboard: `http://localhost:8000/`
- Option Chain: `http://localhost:8000/option-chain`
- Strategy Engine: `http://localhost:8000/strategy`
- Performance: `http://localhost:8000/performance`

---

## For Incoming AI (Next Steps)

1. **Read** `PHASE4_STRATEGY_ENGINE.md` for detailed strategy rules and hardening notes
2. **Understand** the 8 strategies in `strategy/zing_strategies.py` (all stateless; pure functions)
3. **Trace** one full engine cycle: `engine.py:run_engine()` → `executor.py:execute()` → `journal.py:log_trade()`
4. **Test locally:** Start the app, navigate to `/strategy`, enable one strategy, click "Start"
5. **Check** `/api/strategy/journal` for trade events
6. **Backtest** any changes via `/backtest` before deploying to `live_demo`

---

## Files Last Modified (2026-07-21)

- ✅ `static/js/option_chain.js` — Fixed canvas height growth bug (DPR scaling feedback loop)
- ✅ `templates/option_chain.html` — Switched to `data-h` attribute (immutable by rendering)
- ✅ `main.py` — Added `ChainStreamer`, `chain_publisher()`, `engine_publisher()`, AppHub, WebSocket routing
- ✅ `static/js/wsclient.js` — Singleton AppWS client with reconnect, heartbeat, topic subscriptions
- ✅ `strategy/pricebus.py` — Thread-safe in-memory price store (PriceBus)
- ✅ `strategy/eventbus.py` — Bounded queue bridging engine → asyncio WS layer

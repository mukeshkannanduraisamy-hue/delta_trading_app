# DELTA TRADING APP — COMPLETE ARCHITECTURE, MATHEMATICAL MODELS & VERIFICATION AUDIT SPECIFICATION

**Repository:** `https://github.com/mukeshkannanduraisamy-hue/delta_trading_app.git`  
**Target Exchange:** Delta Exchange India (`api.india.delta.exchange`)  
**Target Asset:** BTC Options (`BTCUSD` perpetual underlying, `C-BTC-*` and `P-BTC-*` ATM options)  
**Execution Modes:** Paper Trading (Simulated) & Live Trading (HMAC SHA256 API Signed)  
**Date:** 2026-07-24  

---

## TABLE OF CONTENTS
1. [Executive System Summary](#1-executive-system-summary)
2. [Complete Architecture & Directory Structure](#2-complete-architecture--directory-structure)
3. [Configuration & Environment Parameters (`strategy/config.py`)](#3-configuration--environment-parameters)
4. [REST & WebSocket API Client (`strategy/delta_client.py`)](#4-rest--websocket-api-client)
5. [Market Data & Option Chain Resolver (`strategy/market_data.py`)](#5-market-data--option-chain-resolver)
6. [Advanced Options Math & Risk Engine (`strategy/options_calc.py`)](#6-advanced-options-math--risk-engine)
7. [Strategy Engine & Technical Indicators (`strategy/engine.py`, `zing_strategies.py`, `indicators.py`)](#7-strategy-engine--technical-indicators)
8. [Order Execution & Position Lifecycle Manager (`strategy/executor.py`)](#8-order-execution--position-lifecycle-manager)
9. [Database Persistence & Performance Aggregations (`strategy/store.py`, `journal.py`)](#9-database-persistence--performance-aggregations)
10. [FastAPI Backend, WebSockets & UI Dashboard (`main.py`, `strategies.html`, `strategies.js`)](#10-fastapi-backend-websockets--ui-dashboard)
11. [24/7 Cloud Deployment Specification (`Dockerfile`, `Procfile`)](#11-247-cloud-deployment-specification)
12. [Verification Status & Parameter Audit Report](#12-verification-status--parameter-audit-report)
13. [MASTER PROMPT FOR DELTA EXCHANGE COPILOT](#13-master-prompt-for-delta-exchange-copilot)

---

## 1. EXECUTIVE SYSTEM SUMMARY

The **Delta Trading App** is an enterprise-grade automated options trading system designed specifically for Bitcoin (BTC) Options on Delta Exchange India. The system executes high-probability directional strategies (EMA Cross, Scalping Pulse, Traffic Light, Bollinger Mean Reversion, Supertrend, etc.) and overlays a 5-layer quantitative options risk model:

- **Multi-Timeframe ATR (15m, 1h, 4h)** for dynamic underlying volatility-based Stop Loss sizing.
- **Dynamic DTE & Settlement-Aware Theta Buffer** using 12:00 UTC settlement times for near-expiry (0-DTE to 3-DTE) decay protection.
- **IV Rank (IVR) Regime Sizing & High-IV Skip Gate** to prevent buying overpriced option premiums during volatility spikes.
- **Gamma Risk Multiplier & Quadratic Premium Move Formula** to protect options against sharp adverse price moves.
- **DTE-Scaled Multi-Tier Take Profit (40% TP1, 40% TP2, 20% TP3)** with automated holding-period theta cost deductions.

---

## 2. COMPLETE ARCHITECTURE & DIRECTORY STRUCTURE

```
d:\TRADER\delta_trading_app\
├── main.py                     # FastAPI application, WebSockets, REST API endpoints, Lifespan tasks
├── requirements.txt            # Python dependencies (fastapi, uvicorn, websockets, requests, jinja2)
├── Dockerfile                  # Production containerization build
├── Procfile                    # Cloud process launcher (Render / Railway)
├── static/
│   ├── css/                    # Dashboard UI styles
│   └── js/
│       └── strategies.js       # Real-time WebSocket consumer, decision feed & summary counter UI
├── templates/
│   └── strategies.html         # Main dashboard HTML template with 9-card decision counter panel
└── strategy/
    ├── __init__.py             # Module exports
    ├── config.py               # Central environment configuration & safe type casting
    ├── delta_client.py         # REST API client with retry logic, rate-limit backoff, typed errors
    ├── market_data.py          # OptionResolver, ATM strike selection, OptionQuote builder
    ├── options_calc.py         # 5-layer quantitative options pricing & risk model
    ├── executor.py             # Order placement, position tracking, SL/TP trailing, theta erosion exits
    ├── engine.py               # Async multi-strategy evaluation loop, bar time-gating, preflight checks
    ├── zing_strategies.py      # Core technical trading strategies (EMACross, ScalpingPulse, etc.)
    ├── zing_strategies_v2.py   # Volatility-gated strategy variants
    ├── indicators.py           # Technical indicators (EMA, SMA, ATR, HMA, StochRSI, Bollinger, VWAP)
    ├── base.py                 # Core data classes (Signal, Context, Strategy)
    ├── eventbus.py             # Thread-safe atomic event queue & broadcast hub
    ├── pricebus.py             # In-memory real-time price tick bus
    ├── account.py              # Account balance & margin synchronization manager
    ├── settings.py             # Dynamic settings manager with JSON persistence & thread locks
    ├── store.py                # SQLite database persistence (`trades.db`), PnL aggregations
    └── journal.py              # JSONL log recorder (`trades.jsonl`)
```

---

## 3. CONFIGURATION & ENVIRONMENT PARAMETERS (`strategy/config.py`)

All system parameters are configurable via `.env` with safe type parsing (`_env_float`, `_env_int`) to prevent runtime crashes:

| Parameter | Type | Default | Description |
|---|---|---|---|
| `PROD_BASE` | `str` | `https://api.india.delta.exchange` | Production REST API base URL |
| `DELTA_API_KEY` | `str` | `""` | Delta Exchange API Key |
| `DELTA_API_SECRET` | `str` | `""` | Delta Exchange API Secret |
| `EXECUTION_MODE` | `str` | `"paper"` | Trading mode (`"paper"` or `"live"`) |
| `ENGINE_ASSET` | `str` | `"BTC"` | Target asset for options trading |
| `UNDERLYING_SYMBOL` | `str` | `"BTCUSD"` | Underlying perpetual symbol for signals |
| `ENGINE_POLL_SECONDS`| `float`| `1.0` (min 0.25) | Strategy evaluation loop interval |
| `ENGINE_CONTRACTS` | `int` | `1` | Default contract lot size per order |
| `ENGINE_AUTOSTART` | `bool` | `False` | Auto-start engine on server startup |
| `IV_SKIP_THRESHOLD` | `float`| `85.0` | IV Rank threshold (%) to auto-skip BUY options |
| `MIN_HOURS_TO_EXPIRY`| `float`| `2.0` | Minimum hours remaining before expiry to allow trades |
| `FEE_NOTIONAL_RATE` | `float`| `0.0003` | Delta Taker Fee on notional (0.03%) |
| `FEE_PREMIUM_CAP` | `float`| `0.035` | Delta Taker Fee cap on premium (3.5%) |
| `GST_RATE` | `float`| `0.18` | Goods & Services Tax on fees (18%) |
| `MAX_OPEN_POSITIONS`| `int` | `8` | Maximum concurrent open positions permitted |

---

## 4. REST & WEBSOCKET API CLIENT (`strategy/delta_client.py`)

Handles all communication with Delta Exchange India REST endpoints.
- **Typed Error Hierarchy**:
  - `DeltaError`: Base runtime exception for API rejections.
  - `DeltaRateLimited`: HTTP 429 response with `retry_after` timing.
  - `DeltaAuthError`: Permanent 401 authentication failure.
  - `DeltaNetworkError`: Network timeouts or dropped connections.
- **Public & Signed API Endpoints**:
  - `ticker(symbol)`: Fetches live mark price, spot price, and Option Greeks (`delta`, `gamma`, `theta`, `vega`, `mark_iv`).
  - `candles(symbol, resolution, start, end)`: Fetches historical candles with pagination up to 2000 bars per call.
  - `l2orderbook(symbol)`: Fetches L2 orderbook bids and asks for smart entry pricing.
  - `option_products(asset)`: Resolves all active option products.
  - `option_tickers(asset, expiry)`: Resolves option quotes for a specific settlement date.

---

## 5. MARKET DATA & OPTION CHAIN RESOLVER (`strategy/market_data.py`)

- **`OptionResolver`**:
  - Caches product list for 5 minutes (`300s`) to minimize API requests.
  - `nearest_expiry()`: Finds the soonest live option settlement date with at least `MIN_HOURS_TO_EXPIRY` (2 hours) remaining.
  - `atm(spot)`: Resolves the exact At-The-Money (ATM) Call (`CE`) and Put (`PE`) option contract quotes for the current spot price.
  - Caches live prices for 3 seconds (`PRICE_TTL = 3.0s`).
  - `quote_for_product(product_id)`: Resolves option quotes for position adoption with explicit `strike_price > 0` validation to prevent settlement corruption.

---

## 6. ADVANCED OPTIONS MATH & RISK ENGINE (`strategy/options_calc.py`)

Before any option trade is placed, `calculate_options_order_params()` calculates all entry parameters and enforces 5 critical mathematical risk rules:

### **Rule 1: Multi-Timeframe ATR for Stop Loss**
Fetches 15m, 1h, and 4h candles for `BTCUSD`:
$$\text{sl\_atr} = \max(\text{ATR}_{1h}, \text{ATR}_{15m} \times 4)$$
$$\text{btc\_sl\_distance} = \text{sl\_atr} \times \text{sl\_multiplier}(\text{confidence})$$
Fallbacks ensure non-zero ATR values even during market gaps.

### **Rule 2: Dynamic DTE & Settlement-Aware Theta Buffer**
Parses option expiry `DDMMYY` date and calculates exact settlement time at **12:00:00 UTC**:
$$\text{time\_to\_expiry\_sec} = \text{ex\_date}_{\text{12:00 UTC}} - \text{now}_{\text{UTC}}$$
$$\text{hours\_to\_expiry} = \frac{\text{time\_to\_expiry\_sec}}{3600}$$
- **Expiry Guard**: If $\text{hours\_to\_expiry} < \text{MIN\_HOURS\_TO\_EXPIRY}$ (2 hours), raises `AutoSkipTrade`.
- **Theta Multipliers**:
  - $\text{DTE} > 21$: $1.0\times$
  - $8 \le \text{DTE} \le 21$: $1.5\times$
  - $4 \le \text{DTE} \le 7$: $2.0\times$
  - $\text{DTE} < 4$ (0-DTE to 3-DTE): $3.0\times$ (Accelerated near-expiry buffer)
$$\text{theta\_buffer} = \text{daily\_theta} \times \text{theta\_days\_multiplier}$$

### **Rule 3: IV Rank (IVR) & High-IV Skip Gate**
Computes IV Rank relative to historical volatility proxy:
$$\text{IVR} = \max\left(0, \min\left(100, \frac{\text{current\_iv} - 0.30}{1.20 - 0.30} \times 100\right)\right)$$
- **IV Buffers**: $5\%$ (Low IV) to $30\%$ (Very High IV).
- **Auto-Skip Gate**: If $\text{IVR} > \text{IV\_SKIP\_THRESHOLD}$ (85%), raises `AutoSkipTrade` to avoid buying inflated option premiums before IV crush.

### **Rule 4: Gamma Risk Sizing & SL Bounds**
Calculates option premium move using first and second-order Taylor series expansion:
$$\Delta\text{Premium} = (\delta \times \text{btc\_sl\_distance}) + \left(0.5 \times \gamma \times \text{btc\_sl\_distance}^2\right)$$
$$\text{gamma\_sl\_distance} = (\text{smart\_entry} - \text{raw\_sl}) \times \text{gamma\_risk\_factor}$$
$$\text{final\_option\_sl} = \max(0.01, \text{smart\_entry} - \text{gamma\_sl\_distance})$$
Enforces hard stop limits between **10% minimum** and **90% maximum** of entry premium.

### **Rule 5: DTE-Scaled Take Profit Ratios & Theta Cost Deduction**
Adapts TP ratios based on DTE and signal confidence:
$$\text{raw\_tp}_i = \text{smart\_entry} + (\text{risk} \times \text{tp\_ratio}_i)$$
$$\text{theta\_cost}_i = \text{daily\_theta} \times \text{days\_to\_tp}_i$$
$$\text{final\_tp}_i = \text{raw\_tp}_i - \text{theta\_cost}_i$$
If $\text{final\_tp}_i \le \text{smart\_entry}$, the target is flagged as unachievable and safely set to `None`.

---

## 7. STRATEGY ENGINE & TECHNICAL INDICATORS (`strategy/engine.py`, `zing_strategies.py`, `indicators.py`)

- **Technical Indicators (`indicators.py`)**:
  - `ema()`, `sma()`, `atr()`, `hma()`, `stochastic_rsi()`, `bollinger()`, `vwap()`, `adx()`.
  - All indicators enforce non-zero period validation and warm-up array bounds (`HMA` preserves `None` warm-ups instead of zero-filling).
- **Active Directional Strategies (`zing_strategies.py`)**:
  - **EMACross**: Fast/Slow EMA crossover logic on 1m/5m candles.
  - **ScalpingPulse**: Momentum pulse on 1m candles.
  - **TrafficLight**: 3-candle color sequence with SMA-15 trend alignment (requires $\ge 16$ bars).
  - **MeanReversionBollinger**: Oversold/Overbought mean reversion off 2.0 std dev bands.
  - **InsideCandle**: Mother-bar breakout strategy with 35% proximity threshold.
  - **PrimeScalperEMA**: Multi-EMA slope alignment scalper.
  - **BoomingBullsSupertrend**: ATR Supertrend trend-following strategy.
- **Engine Control Loop (`engine.py`)**:
  - Evaluates enabled strategies every `POLL_SECONDS`.
  - Time-gates evaluations to exact candle bar boundaries (`now // bar_secs`).
  - Wraps cycle execution in `asyncio.wait_for(..., timeout=config.POLL_SECONDS * 10)` to prevent hung REST calls from freezing the app.

---

## 8. ORDER EXECUTION & POSITION LIFECYCLE MANAGER (`strategy/executor.py`)

Handles order entry, active position management, partial exits, and settlement:

1. **Order Entry (`open()`)**:
   - Checks daily loss limit ($20\%$ drawdown cap).
   - Verifies margin availability (`available_usd`).
   - Supports `"market"` (instant fill) and `"limit"` (resting order with TTL cancellation) modes.
2. **Active Position Management (`manage()`)**:
   - Runs every poll cycle under `RLock` synchronization.
   - **Greeks Refresh (5-minute interval)**: Refreshes option `theta` from live API via `client.ticker(pos.symbol)`.
   - **Theta Erosion Check (15-minute interval)**:
     $$\text{theta\_per\_contract} = \text{daily\_theta} \times \text{pos.contract\_value}$$
     $$\text{erosion\_pct} = \left(\frac{\text{theta\_per\_contract}}{\text{price}}\right) \times 100$$
     If $\text{erosion\_pct} > 15\%$, auto-closes position with `theta_erosion_exceeded` to preserve capital.
   - **Stop Loss & Take Profit Monitoring (30-second interval)**:
     - **Dual SL**: Triggers on Option Premium SL ($\text{price} \le \text{pos.stop}$) or BTC Spot SL ($\text{spot} \le \text{pos.btc\_sl\_price}$ for CE / $\ge$ for PE).
     - **Multi-Tier TP Exits**:
       - **TP1 Trigger**: Exits **40% of contracts**, moves SL to **Breakeven** ($\text{pos.stop} = \text{pos.entry\_price}$).
       - **TP2 Trigger**: Exits **40% of remaining contracts**, trails SL to **TP1 price**.
       - **TP3 Trigger**: Exits final **20% of contracts** (Full Exit).
3. **Fee Accounting & Equity Curve (`_book_close()`)**:
   - Deducts taker fees ($0.03\%$ notional capped at $3.5\%$ premium $+ 18\%$ GST).
   - Accurately adjusts `session_equity += gross - exit_fee - entry_fee_part` on partial exits.

---

## 9. DATABASE PERSISTENCE & PERFORMANCE AGGREGATIONS (`strategy/store.py`, `journal.py`)

- **SQLite Database (`strategy/journal/trades.db`)**:
  - Stores all closed trades with columns: `id`, `ts_open`, `ts_close`, `strategy`, `direction`, `symbol`, `entry_price`, `exit_price`, `contracts`, `gross_pnl`, `net_pnl`, `why`, `mode`.
  - Composite Index `idx_trades_strat_close ON trades(strategy, ts_close DESC)` speeds up filtering.
  - Aggregates performance metrics: `win_rate`, `profit_factor`, `gross_pnl`, `net_pnl`, `best_trade`, `worst_trade`.
- **JSONL Journal Logger (`strategy/journal/trades.jsonl`)**:
  - Automatically creates directory on boot.
  - Records real-time JSON events for WebSocket streaming (`options_pre_trade`, `skip`, `close`, `warn`).

---

## 10. FASTAPI BACKEND, WEBSOCKETS & UI DASHBOARD (`main.py`, `strategies.html`, `strategies.js`)

- **FastAPI Lifespan Tasks**:
  - Launches background tasks: `delta_ws_consumer` (upstream market feed), `state_broadcaster`, `health_publisher`, `engine_publisher`, `account_publisher`, `engine_reconciler`.
  - Shutdown sequence captures open positions *before* stopping the engine to prevent orphan positions.
- **WebSocket Feeds**:
  - `/ws/app`: Streams real-time engine state, active positions, account equity, and decision journal logs to connected browser clients.
  - `/ws/market`: Streams live ticker updates.
- **Frontend Dashboard (`strategies.html` & `strategies.js`)**:
  - Displays strategy cards with toggle switches, active open positions table with uPnL, and real-time decision feed.
  - **Today's Decision Summary Counter Panel**: Displays 9 real-time counter grid cards (Signals Scanned, Executed, Total Skipped, High IV, Low DTE, Low Conf, Auto-Closed Theta, SL Hit, TP Hit).
  - **IST Midnight Reset**: Schedules counter resets at **18:30 UTC (00:00 IST)**.

---

## 11. 24/7 CLOUD DEPLOYMENT SPECIFICATION (`Dockerfile`, `Procfile`)

- **Container Build (`Dockerfile`)**:
  ```dockerfile
  FROM python:3.12-slim
  WORKDIR /app
  ENV PYTHONUNBUFFERED=1
  ENV PYTHONDONTWRITEBYTECODE=1
  COPY requirements.txt .
  RUN pip install --no-cache-dir -r requirements.txt
  COPY . .
  EXPOSE 8000
  CMD ["uvicorn", "main:app", "--host", "0.0.0.0", "--port", "8000"]
  ```
- **Procfile Launcher**:
  ```text
  web: uvicorn main:app --host 0.0.0.0 --port $PORT
  ```
- **Free 24/7 Hosting Compatibility**:
  - Ready for 1-click deployment on **Render.com** or **Koyeb.com**.
  - 24/7 non-stop execution achieved by pairing Render with **UptimeRobot** (5-minute HTTP ping to `/`).

---

## 12. VERIFICATION STATUS & PARAMETER AUDIT REPORT

All system components have passed full programmatic verification:

| Component | Status | Verification Detail |
|---|---|---|
| Module Imports | **PASSED** | All 16 python modules import without warnings or circular dependencies. |
| Market Data ATM Resolver | **PASSED** | Resolves live `CE` and `PE` contracts from Delta Exchange API. |
| Multi-TF ATR | **PASSED** | 15m, 1h, and 4h ATRs computed accurately with non-zero fallbacks. |
| DTE & Settlement | **PASSED** | 12:00 UTC settlement parsing verified; 0-DTE to 3-DTE receive 3.0x theta buffer. |
| IV Skip Gate | **PASSED** | IVR clamped `[0, 100]`; skips options exceeding `85%` IVR. |
| Gamma Risk Sizing | **PASSED** | Taylor series move calculated; SL bounded between 10% and 90%. |
| Position Management | **PASSED** | Open, snapshot, theta erosion check, SL/TP trailing, and close verified. |
| SQLite Store | **PASSED** | Trade insertion, composite index query, and PnL metrics calculation verified. |
| GitHub Repository | **PASSED** | Committed & pushed up-to-date on `main` branch (Commit `c0bf154`). |

---

## 13. MASTER PROMPT FOR DELTA EXCHANGE COPILOT

*(Copy and paste the prompt below directly into Delta Exchange Copilot or any AI Code Auditor to review and verify this project)*

```markdown
Act as a Senior Quant Trading Engineer and Lead Code Auditor specializing in Delta Exchange API & Crypto Options Trading.

Perform a thorough architectural review, parameter audit, and logical verification of the entire `delta_trading_app` repository (https://github.com/mukeshkannanduraisamy-hue/delta_trading_app.git).

### SYSTEM SPECIFICATION TO VERIFY:
1. central configuration (`strategy/config.py`):
   - Verify environment variables are safely parsed (`_env_float`, `_env_int`).
   - Confirm `EXECUTION_MODE` respects `.env` ("paper" vs "live").
   - Confirm `DELTA_API_KEY` and `DELTA_API_SECRET` are read from environment.
   - Verify `MIN_HOURS_TO_EXPIRY = 2.0` and `IV_SKIP_THRESHOLD = 85.0`.

2. OPTIONS RISK MATH (`strategy/options_calc.py`):
   - Verify Multi-TF ATR calculation: sl_atr = max(atr_1h, atr_15m * 4).
   - Verify DTE settlement time is 12:00:00 UTC and hours_to_expiry < 2.0h triggers AutoSkipTrade.
   - Verify IVR is clamped [0.0, 100.0] and IVR > 85% skips BUY options.
   - Verify Gamma risk scaling and option_premium_move calculation.
   - Verify Stop Loss is clamped [10%, 90%] of entry premium and final_sl > 0.01.
   - Verify DTE-scaled TP ratios return None when unachievable after daily theta cost.

3. EXECUTOR LIFECYCLE (`strategy/executor.py`):
   - Verify _open_market and _open_limit order entry checks.
   - Verify _check_theta_erosion scales theta by contract_value: erosion = (daily_theta * contract_value / price) * 100.
   - Verify Dual SL (BTC Spot + Option Premium) guards against price is None or price <= 0.
   - Verify Multi-Tier TP partial exits (40% TP1, 40% TP2, 20% TP3) and SL trailing to breakeven / TP1.
   - Verify session_equity deducts entry_fee_part on partial exits.

4. MARKET DATA & RESOLVER (`strategy/market_data.py`, `strategy/delta_client.py`):
   - Verify ATM option chain resolution and quote_for_product strike_price > 0 validation.
   - Verify DeltaClient HTTP rate-limit handling, retries, and typed exceptions.

5. ENGINE & STRATEGIES (`strategy/engine.py`, `strategy/zing_strategies.py`, `strategy/indicators.py`):
   - Verify technical indicators (EMA, SMA, ATR, HMA, StochRSI, Bollinger).
   - Verify _cycle timeout guard asyncio.wait_for(..., timeout=POLL_SECONDS * 10).

6. BACKEND & DASHBOARD (`main.py`, `templates/strategies.html`, `static/js/strategies.js`):
   - Verify FastAPI startup/shutdown lifespans capture open positions before engine stop.
   - Verify WebSocket broadcasts (/ws/app, /ws/market) feed frontend decision logs & 9-grid counters.
   - Verify IST Midnight Reset (18:30 UTC / 00:00 IST).

### AUDIT INSTRUCTIONS:
- Review all codebase files against the specification above.
- Confirm if all parameters, math formulas, data contracts, and workflow logics are fine.
- If ANY logical defect, parameter mismatch, or missing guard is found:
  1. Trace the exact root cause.
  2. Identify all dependent caller files and state updates affected across the system.
  3. Provide exact, complete Python/JS code replacements to fix the issue fully.
```

# Delta Trading App — README FOR INCOMING AI

Welcome! You've been handed off a **real-time cryptocurrency options trading engine** built on FastAPI. This document is your entry point.

---

## TL;DR

**What:** Live crypto options trading engine with 8 quantitative strategies, running on Delta Exchange India testnet.  
**Status:** All phases complete. No validated edge. Use for operational/research testing only.  
**How to start:** Read these 3 docs in order, then run the app locally.

---

## 📚 Documentation (Read in This Order)

1. **`AI_HANDOVER_GUIDE.md`** ← **START HERE** (15 min read)
   - Critical context (testnet-only, no edge)
   - Common tasks with code examples
   - Debug checklist

2. **`PROJECT_STRUCTURE.md`** (30 min read)
   - Complete technical tour
   - All files explained
   - Data flows + architecture

3. **`RECENT_CHANGES.md`** (10 min read)
   - What was fixed on 2026-07-21
   - Canvas DPR scaling bug (now fixed)
   - WebSocket latency improvements
   - How to verify

4. **`PHASE4_STRATEGY_ENGINE.md`** (in `delta_trading_app/`)
   - Full strategy documentation
   - The 8 rules explained
   - Configuration options
   - Performance notes

---

## 🚀 Quick Start

### 1. Setup (5 min)
```powershell
cd D:\TRADER\delta_trading_app
python -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
```

### 2. Configure (2 min)
```powershell
# Edit .env
DELTA_API_KEY=<your-testnet-key>
DELTA_API_SECRET=<your-testnet-secret>
EXECUTION_MODE=paper
ENGINE_AUTOSTART=false
```

### 3. Run (1 min)
```powershell
.\.venv\Scripts\python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

### 4. Browse (2 min)
- **Dashboard:** http://localhost:8000
- **Option Chain:** http://localhost:8000/option-chain
- **Strategy Engine:** http://localhost:8000/strategy
- **Analytics:** http://localhost:8000/performance

### 5. Test (5 min)
1. Navigate to `/strategy`
2. Enable a strategy (toggle checkbox)
3. Click "Start"
4. Watch signal feed for 30 seconds
5. Check `/journal` for trades
6. Click "Stop"

---

## 🎯 What You Need to Know

### Critical Context
- **Testnet only.** No real money. All trading on Delta's demo book.
- **No validated edge.** All 8 strategies tested negative in backtest.
  - Backtests: −0.37 to −1.34 R/trade (all red)
  - Walk-forward: 0 survivors across 30-combo screen
  - VRP test: inconclusive (≈0)
  - **Do not deploy capital.** Full stop.
- **Paper is default.** Set `EXECUTION_MODE=paper` in `.env`. Simulates bid/ask + fees realistically.
- **Live demo requires one-line change.** To send real testnet orders: `EXECUTION_MODE=live_demo`.

### Architecture (30-Second Tour)
```
Delta WebSocket (public market data)
  ↓
PriceBus (in-memory price cache)
  ↓
Engine loop (poll every 5–15s)
  ├─ Manage exits (TP/SL/time)
  ├─ Fetch candles (if bar-clock says new bar)
  ├─ Fetch option chain (lazy, ~10s cache)
  ├─ Evaluate 8 strategies (pure functions)
  └─ Execute fills (paper or live demo)
  ↓
Journal (SQLite + JSONL)
  ↓
Browser WebSocket clients (0.5s chain updates, 1.0s engine state)
  ↓
Dashboards (real-time UI)
```

### Key Files
- **Strategy rules:** `strategy/zing_strategies.py` (8 strategies, all stateless)
- **Engine loop:** `strategy/engine.py` (the core business logic)
- **Execution:** `strategy/executor.py` (paper fills vs. real orders)
- **WebSocket:** `main.py` + `static/js/wsclient.js` (live data push)
- **UI:** `templates/*.html` + `static/js/*.js` (dashboards + controls)

---

## 🔧 Common Tasks

### Add a New Strategy
1. Edit `strategy/zing_strategies.py`
2. Write a pure rule function (candles, chain, spot → Signal or None)
3. Register in `STRATEGIES` dict
4. Backtest via `/backtest?strategy=<slug>`

### Check Why Engine Won't Start
1. `GET /api/health` → check `auth_status` (valid key? IP whitelisted?)
2. Check data freshness (`last_chain_update`, `last_candle_update`)
3. Check testnet balance (`GET /api/account`)
4. Verify `.env` → strategies enabled? `ENGINE_AUTOSTART=false` by default (click Start manually)

### Debug Live Trade Issues
1. Navigate to `/journal` → see all trade events in real-time
2. Check `/strategy` → signal feed shows decisions
3. Check `/api/health` → data freshness + errors
4. Browser console (F12) → any JS errors?

### Backtest a Change
1. Edit strategy in `strategy/zing_strategies.py`
2. Navigate to `/backtest`
3. Select your strategy, click "Run Backtest"
4. Check directional accuracy + R/trade
5. If positive, consider walk-forward screen (`/research`)

---

## 🐛 Known Issues (Already Fixed)

### Canvas Growth Bug (FIXED 2026-07-21)
**Problem:** OI + IV charts on `/option-chain` grew taller every 0.5s update  
**Cause:** DPR-scaling feedback loop (canvas height attribute was being re-read after being written)  
**Fix:** Lock CSS height via `data-h` attribute; derive pixel math from container, not canvas

**Verify it's fixed:**
```js
// Browser console
document.getElementById('oi-chart').clientHeight  // should be 300
// Wait 30 seconds (60+ updates)
document.getElementById('oi-chart').clientHeight  // still 300, not growing
```

### WebSocket 5s Delay (FIXED 2026-07-21)
**Problems:**
1. Auto-refresh flag defaulted to false (frames arrived but were discarded)
2. Engine state only published on cycle completion (5+ second gaps when stopped)
3. WebSocket decorator placed before `app = FastAPI()` (boot error)

**Fixes:**
1. Flipped `autorefresh` default to `true`
2. Added `engine_publisher()` task (publishes every 1.0s regardless of run state)
3. Added `chain_publisher()` task (publishes every 0.5s, uses PriceBus for zero-latency prices)
4. Moved WebSocket route after app initialization

**Verify it's fixed:**
- `/option-chain` updates ~0.5s (chain topic)
- `/strategy` updates ~1.0s (engine topic)

---

## 📊 Pages & What They Do

| Page | Purpose | Updates | Data Source |
|------|---------|---------|-------------|
| `/` | Dashboard | Live | WebSocket + API polling |
| `/option-chain` | Chain explorer | 0.5s | WebSocket (chain topic) |
| `/strategy` | Engine control | 1.0s | WebSocket (engine topic) + manual start/stop |
| `/journal` | Trade log | On-demand | REST + infinite scroll |
| `/performance` | P&L analytics | On-demand | REST (computes from closed trades) |
| `/backtest` | Strategy scoring | On-demand | REST (replays offline) |
| `/research` | Walk-forward screen | On-demand | REST (can take 5–30 min) |
| `/volatility` | IV surface lab | On-demand | REST + 30-min auto-collection |

---

## ⚙️ Configuration (`.env`)

```ini
# Delta Exchange (testnet credentials)
DELTA_API_KEY=...
DELTA_API_SECRET=...

# Execution mode
EXECUTION_MODE=paper              # or live_demo

# Engine
ENGINE_ASSET=BTC                  # or ETH
ENGINE_CONTRACTS=1                # contracts per trade
ENGINE_POLL_SECONDS=15            # evaluation interval (5s floor)
ENGINE_AUTOSTART=false            # start on boot?
ENGINE_MAX_OPEN=8                 # position cap
ENGINE_MAX_HOLD_BARS=60           # time-based exit
ENGINE_COOLDOWN_BARS=5            # strategy cooldown after close
ENGINE_ENABLED_STRATEGIES=[]      # [] = all; ["ema-cross", ...] = subset
```

---

## 🧪 Testing Workflow

### For Strategy Changes
```
Edit zing_strategies.py
  ↓
GET /api/backtest?strategy=<slug>
  → Check directional score
  ↓
Navigate to /strategy
  → Enable strategy
  → Click Start (paper mode)
  → Watch signal feed 30 seconds
  → Check /journal for fills
  → Click Stop
```

### For UI Changes
```
Edit static/js/*.js or templates/*.html
  ↓
Refresh browser (Ctrl+F5)
  ↓
Check browser console (F12 → Console) for errors
  ↓
Test on mobile (F12 → device mode)
```

### For Backend Changes
```
Edit strategy/*.py or main.py
  ↓
Restart server (Ctrl+C, re-run uvicorn)
  ↓
Test via curl or browser
  ↓
Check main.py stdout for logs
```

---

## 🚨 Common Gotchas

1. **IP Whitelisting:** If you get `ip_not_whitelisted_for_api_key`, check `/api/health` for your logged IP and re-whitelist on demo.delta.exchange

2. **Option Expiry:** Options expire at 12:00 UTC. If nearest expiry approaches, engine may fail to resolve ATM contract.

3. **Candle Lag:** Engine polls every 5–15s, but candles close on the :00s. Low poll intervals may miss bars.

4. **Testnet Liquidity:** Thin on options. Market orders pay ~15% spread; limit orders rarely fill.

5. **Paper Fills:** Assume your order size doesn't move the book (unrealistic on large size).

6. **Cooldown:** After exit, same (strategy, direction) is blocked for 5 bars (prevents whipsaw).

---

## 🔍 Debug Checklist

**App won't start:**
- [ ] Python 3.10+? `.\.venv\Scripts\python -m venv .venv`?
- [ ] Port 8000 free? `netstat -an | findstr 8000`?
- [ ] Check `main.py` imports for circular dependencies?

**WebSocket not updating:**
- [ ] Browser console: `window.AppWS.state()` → active connection?
- [ ] Network tab: any 401 / 403 / 500 errors?
- [ ] Stall detector auto-reconnect after 45s silence?

**Engine won't start:**
- [ ] `/api/health` → auth status? IP whitelisted? Data fresh (<10s)?
- [ ] `.env` → `ENGINE_AUTOSTART=false` (click Start manually)?
- [ ] Strategy enabled? (POST `/api/strategy/toggle {slug, enabled}`)

**Trades orphaned:**
- [ ] `/api/account` shows position but no order on exchange?
- [ ] Check `/journal` for `reconcile_mismatch` events
- [ ] Engine reconciles every 20 cycles (live mode)

---

## 📈 Performance Notes

- **Bar-clock gating:** 11/12 cycles skip candle fetch if bars are longer than poll interval
- **Chain lazy-load:** Only fetched if a strategy could act
- **Price reads:** PriceBus is in-memory; zero-latency (no REST per tick)
- **Backtest:** Pure function replay; single-threaded; fast for small datasets
- **Walk-forward:** 60/20/20 split × combos × 8 strategies; can take 5–30 min

---

## 📞 Support / Handoff Info

- **Developer:** mukeshkannanduraisamy@gmail.com
- **Status:** All features complete; no validated edge
- **Testnet only:** Do not deploy capital
- **Paper is default:** Safe for experimentation

---

## 🗺️ Next Steps

1. Read `AI_HANDOVER_GUIDE.md` (15 min)
2. Read `PROJECT_STRUCTURE.md` (30 min)
3. Read `RECENT_CHANGES.md` (10 min)
4. Run the app locally
5. Verify canvas fix + WebSocket latency
6. Enable a strategy and watch trades flow
7. Explore `/backtest` and `/research`
8. Ask: What's next?

---

**Welcome to the project! 🚀**

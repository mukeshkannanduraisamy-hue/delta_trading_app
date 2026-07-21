# Delta Trading App — Complete Documentation Index

**Generated:** 2026-07-21  
**For:** Incoming AI / Next Developer  
**Status:** All phases complete; ready for handoff

---

## 📖 Start Here

> ## ⚠ EXECUTION MODE — READ THIS BEFORE ANYTHING ELSE
>
> **As of 2026-07-21 this engine is LIVE ONLY. Paper mode has been removed from
> the code.** Starting the engine places real orders on the Delta **testnet**
> demo book. There is no dry run and no simulated fallback.
>
> No real money is reachable — signed requests are hardcoded to the testnet base
> in `delta_client.py` — but the order flow is genuine.
>
> **Every statement about `paper` mode in the documents below is now obsolete.**
> They describe a code path that no longer exists. Also see
> [AUDIT_2026-07-21.md](AUDIT_2026-07-21.md) (36 issues, 13 fixed) and
> [RETUNE_STUDY_2026-07-21.md](RETUNE_STUDY_2026-07-21.md) (0 of 274 parameter
> combinations showed positive expectancy — running the engine is an execution
> test, not a profit-seeking activity).
>
> Verify the current state with `python verify_live_only.py`, never from prose.

### New to the project?
**Read in this order:**

1. **[README_FOR_AI.md](README_FOR_AI.md)** (10 min)
   - What the project does
   - Critical context (testnet-only, no edge)
   - Quick start guide
   - Common gotchas

2. **[AI_HANDOVER_GUIDE.md](AI_HANDOVER_GUIDE.md)** (15 min)
   - Common tasks with code examples
   - File locations for quick edits
   - Testing workflow
   - Debug checklist

3. **[PROJECT_STRUCTURE.md](PROJECT_STRUCTURE.md)** (30 min)
   - Complete technical tour
   - Every file explained
   - Data flow diagrams
   - API routes

4. **[RECENT_CHANGES.md](RECENT_CHANGES.md)** (10 min)
   - What was fixed on 2026-07-21
   - How to verify fixes
   - Lesson learned (DPR scaling, WebSocket latency)

---

## 📚 Reference Docs

### In This Directory (`D:\TRADER\`)

| File | Purpose | Read If |
|------|---------|---------|
| `README_FOR_AI.md` | Master overview + entry point | Starting fresh on project |
| `AI_HANDOVER_GUIDE.md` | Practical quick reference | Need to do a common task |
| `PROJECT_STRUCTURE.md` | Technical deep-dive + architecture | Understanding system design |
| `RECENT_CHANGES.md` | Bug fixes + improvements | Verifying recent work or debugging |
| `DOCUMENTATION_INDEX.md` | This file | Finding the right documentation |

### In Project Subdirectory (`D:\TRADER\delta_trading_app\`)

| File | Purpose | Read If |
|------|---------|---------|
| `PHASE4_STRATEGY_ENGINE.md` | Complete strategy documentation | Implementing new strategies or tweaking rules |
| `main.py` | FastAPI entry point | Understanding backend architecture |
| `.env` | Configuration | Changing engine behavior or credentials |
| `requirements.txt` | Python dependencies | Setting up environment |

---

## 🎯 Find Documentation By Task

### "I want to..."

#### Add a new strategy
→ `AI_HANDOVER_GUIDE.md` → Task 1: Add a New Strategy  
→ `PHASE4_STRATEGY_ENGINE.md` → The 8 Strategies (understand rule pattern)  
→ `strategy/zing_strategies.py` (code)

#### Debug why engine won't start
→ `AI_HANDOVER_GUIDE.md` → Task 2: Debug Why Engine Won't Start  
→ `RECENT_CHANGES.md` → "Related Infrastructure Improvements"  
→ `/api/health` (REST endpoint)

#### Understand the architecture
→ `PROJECT_STRUCTURE.md` → Core Systems  
→ `PROJECT_STRUCTURE.md` → Data Flows  
→ `PROJECT_STRUCTURE.md` → Key Technical Decisions

#### See what was fixed recently
→ `RECENT_CHANGES.md` → Fixed Issues  
→ `RECENT_CHANGES.md` → Related Infrastructure Improvements

#### Find a specific file
→ `PROJECT_STRUCTURE.md` → Directory Structure  
→ `AI_HANDOVER_GUIDE.md` → File Locations for Quick Edits

#### Set up the project locally
→ `README_FOR_AI.md` → Quick Start (5 steps)  
→ `AI_HANDOVER_GUIDE.md` → Testing Workflow

#### Verify a change works
→ `RECENT_CHANGES.md` → Testing Workflow (Post-Fix)  
→ `AI_HANDOVER_GUIDE.md` → Testing Workflow (How to Verify a Change)

#### Understand WebSocket real-time data
→ `PROJECT_STRUCTURE.md` → Live Price Update Path  
→ `RECENT_CHANGES.md` → PriceBus Integration  
→ `RECENT_CHANGES.md` → Client-Side WebSocket

#### Run walk-forward research or backtest
→ `AI_HANDOVER_GUIDE.md` → Task 4: Backtest a Strategy Change  
→ `AI_HANDOVER_GUIDE.md` → Task 5: Run Walk-Forward Research  
→ `PROJECT_STRUCTURE.md` → Backtest/Research Path

#### Check IV surface or VRP
→ `AI_HANDOVER_GUIDE.md` → Task 6: Check IV Surface / VRP  
→ `PHASE4_STRATEGY_ENGINE.md` → Volatility lab

#### Deploy to live testnet (real orders)
→ `AI_HANDOVER_GUIDE.md` → Task 7: Deploy Strategy to Live Demo  
→ `PHASE4_STRATEGY_ENGINE.md` → Execution modes

---

## 🔍 Documentation Map (By Topic)

### Getting Started
- `README_FOR_AI.md` ← Quick start, TL;DR, critical context
- `AI_HANDOVER_GUIDE.md` ← How to do common tasks

### System Architecture
- `PROJECT_STRUCTURE.md` ← Complete tour of all systems
- `PROJECT_STRUCTURE.md` → Data Flows section (price updates, signals, backtest)
- `PROJECT_STRUCTURE.md` → Configuration section (`.env` options)

### The Engine & Strategies
- `PHASE4_STRATEGY_ENGINE.md` ← Full strategy documentation
- `PHASE4_STRATEGY_ENGINE.md` → The 8 Strategies table (rules + basis)
- `strategy/zing_strategies.py` ← Code implementation
- `strategy/engine.py` ← Core loop (poll → evaluate → execute)

### API & WebSocket
- `PROJECT_STRUCTURE.md` → API Routes section (all endpoints)
- `RECENT_CHANGES.md` → AppHub + WebSocket Routing (real-time architecture)
- `static/js/wsclient.js` ← Browser-side WebSocket client

### Execution & Risk Management
- `strategy/executor.py` ← Paper fills vs. live orders
- `PHASE4_STRATEGY_ENGINE.md` → Hardening section (TP/SL/exits/reconciliation)
- `PROJECT_STRUCTURE.md` → Key Technical Decisions

### Backtesting & Research
- `strategy/backtest.py` ← Offline replay logic
- `strategy/research.py` ← Walk-forward validation
- `/backtest` page ← UI for running backtests
- `/research` page ← UI for walk-forward screen

### Analytics
- `strategy/store.py` ← SQLite trade persistence
- `strategy/journal.py` ← JSONL trade log
- `/performance` page ← P&L analytics + equity curve
- `/journal` page ← Trade table

### Volatility & IV Analysis
- `strategy/volatility.py` ← IV surface + VRP collector
- `/volatility` page ← IV heatmap + VRP trends

### Recent Fixes
- `RECENT_CHANGES.md` ← Canvas DPR scaling bug + WebSocket latency improvements
- `RECENT_CHANGES.md` → Testing Workflow (how to verify fixes)
- `README_FOR_AI.md` → Known Issues (Already Fixed)

### Troubleshooting
- `AI_HANDOVER_GUIDE.md` → Common Gotchas (5 operational issues)
- `AI_HANDOVER_GUIDE.md` → Debug Checklist (organized by symptom)
- `README_FOR_AI.md` → Debug Checklist (quick ref)

### Configuration & Deployment
- `.env` file ← All runtime settings
- `PHASE4_STRATEGY_ENGINE.md` → Configuration section
- `AI_HANDOVER_GUIDE.md` → Task 7: Deploy Strategy to Live Demo

---

## 📋 Quick Reference Tables

### All Pages in the App
| Route | Purpose | Updates | Status |
|-------|---------|---------|--------|
| `/` | Dashboard | Live | ✅ Working |
| `/option-chain` | Chain explorer | 0.5s WS | ✅ Fixed canvas bug |
| `/strategy` | Engine control | 1.0s WS | ✅ Fixed 5s delay |
| `/journal` | Trade log | On-demand | ✅ Working |
| `/performance` | P&L analytics | On-demand | ✅ Working |
| `/backtest` | Strategy scoring | On-demand | ✅ Working |
| `/research` | Walk-forward | On-demand | ✅ Working |
| `/volatility` | IV lab | On-demand | ✅ Working |

### Key Files by Function
| Function | File | Type |
|----------|------|------|
| Entry point | `main.py` | Python |
| Strategy rules | `strategy/zing_strategies.py` | Python |
| Core engine | `strategy/engine.py` | Python |
| Execution | `strategy/executor.py` | Python |
| Backtesting | `strategy/backtest.py` | Python |
| Research | `strategy/research.py` | Python |
| Trades DB | `strategy/store.py` | Python |
| Price cache | `strategy/pricebus.py` | Python |
| Live prices | `strategy/datafeed.py` | Python |
| Option chain | `strategy/market_data.py` | Python |
| REST client | `strategy/delta_client.py` | Python |
| WebSocket | `main.py` + `static/js/wsclient.js` | Python + JS |
| Option chain UI | `templates/option_chain.html` + `static/js/option_chain.js` | HTML + JS |
| Strategy UI | `templates/strategies.html` + `static/js/strategies.js` | HTML + JS |
| Performance UI | `templates/performance.html` + `static/js/performance.js` | HTML + JS |
| Journal UI | `templates/journal.html` + `static/js/journal.js` | HTML + JS |

### Configuration Options (`.env`)
| Option | Default | Purpose |
|--------|---------|---------|
| `EXECUTION_MODE` | `paper` | Simulation vs. real orders |
| `ENGINE_ASSET` | `BTC` | Which underlying to trade |
| `ENGINE_CONTRACTS` | `1` | Size per trade |
| `ENGINE_POLL_SECONDS` | `15` | Evaluation interval |
| `ENGINE_AUTOSTART` | `false` | Start on boot |
| `ENGINE_MAX_OPEN` | `8` | Position cap |
| `ENGINE_COOLDOWN_BARS` | `5` | Post-exit strategy cooldown |

---

## 🧠 Mental Models (Copy These)

### Data Flow: Live Price → Browser
```
Delta WebSocket (public market data)
  ↓ [every tick, <100ms]
PriceBus (thread-safe in-memory cache)
  ↓ [zero-latency reads]
Engine (strategy evaluation)
  ↓ [signals routed to executor]
EventBus (bounded queue)
  ↓ [state_broadcaster drains every 100ms]
AppWS (WebSocket broadcast)
  ↓ [topic subscriptions]
Browser (JavaScript updates DOM)
```

### Data Flow: Strategy Signal → Trade
```
Engine polls every 5–15s
  ↓ [bar-clock check: is there a new candle?]
Fetch candles + option chain (lazy, cached)
  ↓
Evaluate all enabled strategies (pure functions)
  ↓ [each returns Signal or None]
Executor receives signal
  ↓ [paper mode: simulated fill; live_demo: real order]
Position tracked + journal logged
  ↓
EventBus emits state
  ↓
WebSocket clients receive update
  ↓
Browser UI updates (P&L, positions, signal feed)
```

### Data Flow: Backtest/Research
```
Load historical OHLCV (cached from Binance)
  ↓
Replay same strategy.evaluate() per bar
  ↓ [pure function, no I/O]
Collect signals + scores (directional accuracy, R/trade)
  ↓
Train/test/holdout split (60/20/20)
  ↓
Report results
```

---

## 🚀 One-Minute Summaries

### What is this project?
A **FastAPI-based trading engine** that implements 8 quantitative option strategies, evaluates them live against Delta Exchange India testnet data, and executes orders either as paper (simulated) or live (real testnet).

### Why does it exist?
To test whether the 8 published Zing Trade blog strategies (adapted from Nifty index options to BTC/ETH crypto options) have any tradeable edge. **Verdict: They don't.** Backtests are all negative. Use for ops/research testing.

### How does it work?
1. Engine polls every 5–15s
2. Fetches latest candles + option chain
3. Evaluates all enabled strategies (pure functions)
4. Executes fills (paper or live_demo)
5. Publishes state to WebSocket clients
6. Browser shows real-time dashboards + analytics

### What can it do?
- ✅ Live options chain explorer (OI, IV, Greeks)
- ✅ Real-time strategy signal generation
- ✅ Paper + live_demo execution
- ✅ Trade journal (SQLite + JSONL)
- ✅ Performance analytics (equity curve, Sharpe, max DD)
- ✅ Offline backtesting (same logic as live)
- ✅ Walk-forward research (train/test validation)
- ✅ IV surface + VRP analysis lab

### What can't it do?
- ❌ Generate profitable signals (no validated edge)
- ❌ Work with real capital (testnet only)
- ❌ Auto-scale to multiple strategies (single-process)
- ❌ Predict market direction (it's not ML)

### How do I start?
1. Read `README_FOR_AI.md` (10 min)
2. Run `uvicorn main:app --host 127.0.0.1 --port 8000`
3. Navigate to `http://localhost:8000/strategy`
4. Enable a strategy, click Start
5. Watch trades flow

---

## 📞 When You Get Stuck

### "I don't understand how X works"
→ Find X in the **Documentation Map** above  
→ Open the recommended file  
→ Search for the section

### "I want to do X but don't know where to start"
→ Find X in the **Find Documentation By Task** section above  
→ Follow the recommended reading order

### "I found a bug"
→ Search `RECENT_CHANGES.md` for similar issues  
→ Check `/api/health` for root cause  
→ Debug via browser console (F12)  
→ Test fix against backtest (`/backtest`)

### "I want to add a feature"
→ Read `PROJECT_STRUCTURE.md` → Key Technical Decisions  
→ Identify impacted components  
→ Write tests/backtest first  
→ Update relevant docs (this file) when done

### "The project won't run"
→ Check `README_FOR_AI.md` → Quick Start  
→ Check `AI_HANDOVER_GUIDE.md` → Debug Checklist  
→ Run `GET /api/health` to diagnose

---

## ✅ Handoff Checklist

Before you take over, verify:

- [ ] You've read `README_FOR_AI.md`
- [ ] You've read `AI_HANDOVER_GUIDE.md`
- [ ] You've skimmed `PROJECT_STRUCTURE.md` (at least the directory structure)
- [ ] You've reviewed `RECENT_CHANGES.md` to understand recent fixes
- [ ] You can run the app locally: `uvicorn main:app --host 127.0.0.1 --port 8000`
- [ ] You can navigate to `/strategy` and enable a strategy
- [ ] You can verify canvas height fix: DevTools console, check `clientHeight` before/after 30s
- [ ] You can verify WebSocket latency: watch `/strategy` update ~1.0s (engine), `/option-chain` ~0.5s (chain)
- [ ] You understand the verdict: **no validated edge; testnet-only**
- [ ] You have questions? Ask before proceeding.

---

## 📜 Version History

| Date | Changes |
|------|---------|
| 2026-07-21 | Canvas DPR scaling bug fixed; WebSocket latency improved; full documentation generated |

---

## 🎓 Learning Resources (External)

- **Zing Trade blog:** https://zing.trade/blog/category/strategies/ (original strategies)
- **Delta Exchange API:** https://api.india.delta.exchange (public endpoints)
- **FastAPI docs:** https://fastapi.tiangolo.com (backend framework)
- **Lightweight Charts:** https://lightweight-charts.com (charting library)

---

**You're ready! Questions? Start with `README_FOR_AI.md`. 🚀**

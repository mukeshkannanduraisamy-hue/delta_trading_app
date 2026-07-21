# Delta Trading App — AI Handover Quick Reference

## What This Project Does

A **real-time cryptocurrency options trading engine** with 8 quantitative strategies that runs on Delta Exchange India testnet. Includes live dashboards, backtesting, walk-forward research, and P&L analytics. **Verdict:** No validated edge; use for operational/research testing only.

---

## Critical Context (Must Know)

### 1. It's Testnet-Only
- **No real money.** All trading is on Delta's testnet demo book
- `.env` controls execution: `EXECUTION_MODE=paper` (default) or `live_demo` (real testnet orders)
- Paper fills simulate bid/ask spread + Delta's 18% GST fee; still realistic cost

### 2. No Proven Edge
- Backtests: All 8 strategies negative (−0.37 to −1.34 R/trade)
- Walk-forward screen: 0 survivors across 30 parameter combos
- VRP test: inconclusive (≈0)
- **Do not think "this is broken, let me fix it."** The data says they don't work.

### 3. WebSocket Real-Time Data
- `Delta WS → PriceBus (in-memory) → Engine → EventBus → Browser`
- Chain updates: 0.5s latency
- Engine state: 1.0s latency
- Spot price: Live (via PriceBus ticks)

### 4. Canvas Rendering Bug (FIXED 2026-07-21)
- **Problem:** OI + IV charts grew taller every 0.5s data update
- **Root:** `canvas.height = clientHeight * dpr` fed back into next frame's clientHeight read
- **Fix:** Lock CSS height via `data-h` attribute; derive pixel math from container, never from canvas
- **Lesson:** Canvas intrinsic ≠ CSS size; DPR scaling needs bidirectional care

---

## Common Tasks

### Task 1: Add a New Strategy

**File:** `strategy/zing_strategies.py`

```python
# 1. Define Signal class instance (already imported from base.py)
# 2. Add rule function (pure: candles, chain, spot → Signal or None)

def my_strategy_rule(candles, chain, spot, context):
    """
    Pure function. Return Signal(direction, reason, strength) or None.
    - direction: 'BUY' (call) or 'SELL' (put)
    - reason: str (why fired)
    - strength: 0–1 (unused but structured)
    """
    # Calculate indicators (SMA, EMA, etc.) on candles
    # Check conditions
    # Return Signal(...) if triggered, else None

# 3. Register in STRATEGIES dict with slug
STRATEGIES = {
    "my-strategy": Strategy(
        slug="my-strategy",
        name="My Strategy",
        rule=my_strategy_rule,
        description="...",
    ),
    ...
}

# 4. Backtest: GET /api/backtest?strategy=my-strategy
#    (replays same rule() over historical data)
```

**Key:** Rules are **stateless**. No position tracking. Only inputs: current candles + chain + spot.

### Task 2: Debug Why Engine Won't Start

**Checklist:**
1. **Auth:** `GET /api/health` → check `auth_status.api_key_valid` and `ip_whitelisted`
   - If IP not whitelisted: log shows your IP; add it to demo.delta.exchange key settings
2. **Data:** `auth_status.last_chain_update`, `last_candle_update` should be recent (<10s)
   - If old: Delta WebSocket might be down or credentials wrong
3. **Funds:** `GET /api/account` → check testnet balance (must be > 0)
4. **Config:** `.env` → `ENGINE_AUTOSTART=false` by default (manually click Start)

### Task 3: View Live Trades

**URLs:**
- Real-time: `GET http://localhost:8000/strategy` → "Signal Feed" panel (WebSocket live)
- History: `GET /api/strategy/journal?limit=50` → JSON array of trade events
- Filtered table: `GET http://localhost:8000/journal`

**JSON structure (one trade event):**
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

### Task 4: Backtest a Strategy Change

**Steps:**
1. Edit `strategy/zing_strategies.py` → modify rule function
2. Navigate to `http://localhost:8000/backtest`
3. Select your strategy from dropdown
4. Click "Run Backtest" (runs offline replay against historical data)
5. Results: directional accuracy %, avg R/trade, signal count
6. **If negative, don't go to live_demo.** That's expected.

### Task 5: Run Walk-Forward Research

**Steps:**
1. Navigate to `http://localhost:8000/research`
2. Select symbol + timeframe + parameter range
3. Click "Run Screen" (can take 5–30 min; runs offline)
4. Results: train accuracy, test accuracy, holdout accuracy
   - Green = both train AND test positive → might be real edge (unlikely)
   - Red = train-only positive or negative test → overfitting (expected)

### Task 6: Check IV Surface / VRP

**Steps:**
1. Navigate to `http://localhost:8000/volatility`
2. Heatmap shows IV% by strike (left = far put, right = far call)
3. Line chart shows VRP trend (30-min snapshots collected automatically)
4. Green bar = implied vol > realized vol (unusual, maybe short vol); red = vice versa

### Task 7: Deploy Strategy to Live Demo

**⚠️ WARNING: This sends real orders to testnet.**

1. **Prep:** Backtest + walk-forward pass. (They won't.)
2. **Config:** `.env` → `EXECUTION_MODE=live_demo`
3. **Check:** `GET /api/account` → testnet balance > 0
4. **Run:** Restart server, navigate to `/strategy`
5. **Monitor:** Watch signal feed in real-time; check fills in `/journal`
6. **Exit:** Click "Stop" (closes all positions), or toggle strategy off

---

## File Locations for Quick Edits

| Change | File |
|--------|------|
| Strategy logic | `strategy/zing_strategies.py` |
| Execution (fills, TP/SL) | `strategy/executor.py` |
| Indicators (SMA, EMA, etc.) | `strategy/indicators.py` |
| WebSocket events | `main.py` (look for `@app.websocket` and `AppHub`) |
| UI (strategy panel) | `templates/strategies.html` + `static/js/strategies.js` |
| Option chain (table + charts) | `templates/option_chain.html` + `static/js/option_chain.js` |
| Dashboard | `templates/dashboard.html` + `static/js/health.js` |
| API endpoints | `main.py` (look for `@app.post` / `@app.get`) |

---

## Testing Workflow (How to Verify a Change)

### For Strategy Changes
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

### For UI Changes
```
1. Edit static/js/*.js or templates/*.html
2. Refresh browser (Ctrl+F5 to bypass cache)
3. Check browser console (F12 → Console tab) for JS errors
4. Verify responsive layout on mobile (F12 → toggle device mode)
```

### For Backend/API Changes
```
1. Edit strategy/*.py or main.py
2. Restart server (Ctrl+C, re-run uvicorn)
3. Test via curl:
   curl -X POST http://localhost:8000/api/strategy/start
4. Check main.py stdout for logs + exceptions
```

---

## Common Gotchas

### Gotcha 1: Candle Lag
- Engine polls every 5–15s (`.env` → `ENGINE_POLL_SECONDS`)
- Candles close on the :00s (:01–:60 = 1m bar)
- Engine might miss a candle if poll is too sparse (e.g., 1h bar closes only every 60min)
- **Fix:** Lower poll interval or add a bar-clock backfill

### Gotcha 2: Option Chain Expires
- Options expire 12:00 UTC
- If nearest expiry reaches < 5 min to close, engine fails to resolve ATM contract
- Executor tries to close position; if chain missing, it orphans
- **Fix:** Hardened to reconcile vs. exchange every 20 cycles (live mode)

### Gotcha 3: IP Whitelisting
- Delta testnet API keys are bound to your client IP
- If your ISP dynamic-assigns a new IP, orders fail
- Error: `ip_not_whitelisted_for_api_key`
- **Fix:** Check `/api/health` for logged IP; re-whitelist on demo.delta.exchange

### Gotcha 4: Paper Fills Assume Your Size Doesn't Move Book
- Testnet liquidity is thin
- Engine places market orders that may partially fill or move the book
- Paper fills assume full fill at bid/ask (unrealistic on size)
- **Reality:** Actual testnet fills may differ; verify empirically

### Gotcha 5: Cooldown Prevents Re-Entry Too Fast
- After exit, same (strategy, direction) is blocked for 5 bars
- Prevents whipsaw; but can miss real second signal
- **Override:** Edit `ENGINE_COOLDOWN_BARS` in `.env`

---

## Debug Checklist

**App won't start:**
- Python version: 3.10+?
- Venv activated: `.\.venv\Scripts\python.exe -m uvicorn main:app`?
- Port 8000 free: `netstat -an | findstr 8000`?
- Check `main.py` imports; any circular dependencies?

**WebSocket not updating:**
- Browser console: `window.AppWS.state()` → should show active connection
- Network tab: any 401 / 403 / 500 errors?
- Check `wsclient.js` for stall detector (auto-reconnect after 45s silence)

**Engine won't start:**
- `/api/health` → auth status? IP whitelisted? Data freshness?
- `.env` → `ENGINE_AUTOSTART=false` means you must click "Start" manually
- Strategy enabled? (Toggle in UI, POST `/api/strategy/toggle`)

**Trades orphaned (position open, no order on exchange):**
- Network glitch during order placement?
- Check `/api/account` (signed call reads testnet positions)
- Engine reconciles every 20 cycles; watch `/journal` for `reconcile_mismatch` events
- If reconcile fails, journal + close manually

**Charts not rendering:**
- Canvas element missing? Check `/option-chain` source → `<canvas id="oi-chart">`?
- JavaScript error? Browser F12 → Console tab
- No data? Check WS connection + `/api/chain` response

---

## Performance Notes

- **Bar-clock gating:** If poll is 5s and bars are 1m, 11/12 cycles skip candle fetch (no new bar)
- **Chain lazy-load:** Only fetched if strategy can act (time-of-day, last data freshness check)
- **Price reads:** PriceBus is in-memory; engine reads are zero-latency (no REST per tick)
- **Backtest:** Replays pure `evaluate()` function; single-threaded; can be slow for large datasets
- **Walk-forward:** 60/20/20 split × 5 parameter combos × 8 strategies ≈ 5–30 min on modern hardware

---

## Next Steps for Incoming AI

1. **Read** `PROJECT_STRUCTURE.md` (full tour)
2. **Run** the app locally: `uvicorn main:app --host 127.0.0.1 --port 8000`
3. **Navigate** to `/strategy` and enable one strategy (e.g., EMA Cross)
4. **Click** "Start" and watch signal feed for 1 minute
5. **Check** `/journal` for trade events
6. **Click** "Stop" to exit all positions
7. **Review** `/api/health` to understand engine state
8. **Trace** one full cycle: `engine.py:run_engine()` → strategy rule → executor → journal
9. **Backtest** the strategy: `/backtest?strategy=ema-cross`
10. **Make a small change** (e.g., adjust EMA periods) and re-backtest to verify loop works

---

## Contact / Handoff Notes

- **Developer email:** mukeshkannanduraisamy@gmail.com
- **Delta API docs:** https://api.india.delta.exchange (public endpoints only)
- **Zing strategies reference:** https://zing.trade/blog/category/strategies/
- **Project verdict:** No validated edge. All strategies tested negative. Use for ops/research testing only. Do not deploy capital.

---

**Generated:** 2026-07-21  
**Last updated:** 2026-07-21 (Canvas DPR fix, WebSocket refactor, PriceBus + EventBus architecture)

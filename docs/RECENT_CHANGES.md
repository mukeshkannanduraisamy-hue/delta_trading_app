# Delta Trading App — Recent Changes & Bug Fixes

**Last Updated:** 2026-07-21

---

## Fixed Issues (2026-07-21)

### ✅ Issue 1: Canvas Height Growth on Option Chain Charts
**Status:** FIXED  
**Symptom:** OI bar chart and IV Smile chart on `/option-chain` grew taller by ~10% on every WebSocket data update (every 0.5s), causing unbounded UI height expansion over 10–20 minutes.

**Root Cause (Technical):**
- Standard DPR-scaling bug in `renderOIChart()` and `renderIVSmile()`
- Code pattern: `const h = canvas.clientHeight; canvas.height = h * dpr;`
- Canvas element's `height` attribute (intrinsic pixel size) overwrites the HTML attribute
- Next frame's read of `getAttribute("height")` returns the inflated pixel value, not the original CSS height
- On DPR=1.25, each frame: 300 → 375 → 469 → 586 → ... (exponential growth)

**Solution:**
- **`static/js/option_chain.js:234`** — `setupCanvas()` function
  - Changed to read fixed height from `canvas.dataset.h` (a `data-h` attribute)
  - Set `canvas.style.height` and `canvas.style.width` once per frame (CSS-level lock)
  - Derive pixel dimensions for DPR scaling from the fixed container, not from inflated canvas
  ```js
  // BEFORE (buggy)
  var cssH = canvas.getAttribute("height") ? parseInt(canvas.getAttribute("height"), 10) : 300;
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;  // overwrites height attribute; next read is corrupted
  
  // AFTER (fixed)
  var cssH = parseInt(canvas.dataset.h || "300", 10);  // read from data-h, never overwritten
  canvas.style.width = cssW + "px";
  canvas.style.height = cssH + "px";
  canvas.width = cssW * dpr;
  canvas.height = cssH * dpr;
  ```

- **`templates/option_chain.html:77,81`** — Canvas element declarations
  - Changed from `height="300"` to `data-h="300"`
  - Immutable by canvas rendering; never overwritten by intrinsic size calculations

**Verification:**
- After 4 seconds of 0.5s updates (8 frames), both canvases remain exactly 300px height
- No growth observed; CSS height locked; rendering crisp on high-DPR screens

**Lesson Learned:**
- Canvas intrinsic size (`canvas.width` / `canvas.height` attributes) ≠ CSS size
- DPR scaling requires careful separation: lock CSS, scale only the pixel buffer
- When scaling canvas, **never read from `canvas.clientHeight` after writing to `canvas.height`**

---

### ✅ Issue 2: 5-Second Delay on Option Chain & Strategy Pages
**Status:** FIXED  
**Symptom:** `/option-chain` and `/strategy` pages felt "stale" or sluggish; data updated only every 5+ seconds despite advertised 0.5s push.

**Root Cause (Multiple):**

#### Part A: Auto-Refresh Flag Defaulted to False
- `option_chain.js` initialized `state.autoRefresh = false`
- WebSocket frames arrived (verified: `AppWS.send({action:'sub_chain'})` showed 7 frames per 2.5 seconds)
- But handler bailed: `if (!state.autoRefresh) return;` — all frames discarded
- HTML checkbox `#autorefresh-toggle` existed but was not `checked` by default

**Solution:**
- **`templates/option_chain.html:28`** — HTML checkbox
  - Changed from `<input type="checkbox" id="autorefresh-toggle" />` 
  - To `<input type="checkbox" id="autorefresh-toggle" checked />`
  
- **`static/js/option_chain.js`** — Initialize state
  - Changed initial value to `true`

#### Part B: Strategy Engine Only Published on Cycle Completion
- Engine ran every 15 seconds (default `ENGINE_POLL_SECONDS`)
- State was only emitted to EventBus on cycle end (manage + fetch + evaluate + route + exit)
- If engine stopped, zero frames for 5+ seconds, then health tick (fallback to 5s)
- Frontend fell back to polling, felt like "5s lag"

**Solution:**
- **`main.py`** — Added background task `engine_publisher()`
  - Asyncio task publishing `engine` state every 1.0 second regardless of engine run state
  - Runs independently from engine loop; ensures steady 1.0s update even when engine is stopped
  - Drains from EventBus and broadcasts to connected WebSocket clients

- **`main.py`** — Added background task `chain_publisher()`
  - Asyncio task fetching option chain and broadcasting `chain` topic every 0.5 seconds
  - Uses `ChainStreamer`: fetches structure once (REST), overlays live prices from PriceBus (zero-latency)
  - Reduces REST overhead; prices are fresh from WebSocket ticks

#### Part C: Routing Error in WebSocket Decorator
- **`main.py`** — `@app.websocket` decorator was placed **before** `app = FastAPI()`
- Reference to undefined `app` object → server wouldn't boot
- **Solution:** Moved route definition to after `app = FastAPI(...)`

**Verification:**
- `/option-chain` now updates ~0.5s (chain topic, 8 frames per 4 seconds)
- `/strategy` now updates ~1.0s (engine topic, 4 frames per 4 seconds)
- Status line shows "Live · 50 contracts streaming" with updated timestamp

**Lesson Learned:**
- WebSocket subscriptions + background tasks > REST polling for real-time UX
- Separate concerns: `chain_publisher()` (market data) vs. `engine_publisher()` (engine state) for independent timing
- Always default UI flags to `true` if they enable the primary flow (auto-refresh = on)

---

## Related Infrastructure Improvements (2026-07-21)

### AppHub + WebSocket Routing
**File:** `main.py`  
**Purpose:** Centralized multiplexer for live data streams

- **ChainStreamer:** Fetches option chain structure once (REST), overlays live prices from PriceBus
  - Reduces REST load (chain structure rarely changes; prices are always fresh)
  - API: `GET /api/chain` (REST fallback if WebSocket unavailable)
  
- **EventBus:** Bounded queue (maxsize=2000) bridging engine worker thread → asyncio WebSocket layer
  - Engine (background task, CPU-bound) pushes signals/exits → EventBus
  - `state_broadcaster()` task drains every 100ms → broadcasts to clients
  - Overflow: drops oldest (doesn't block engine thread)

- **Topic-based subscriptions:** WebSocket handlers route messages by topic
  - `sub_chain` / `unsub_chain` → receive `chain` topic (0.5s)
  - `sub_engine` / `unsub_engine` → receive `engine` topic (1.0s)
  - Clients only receive data they subscribed to (bandwidth efficient)

### PriceBus Integration
**File:** `strategy/pricebus.py`  
**Purpose:** Thread-safe in-memory price cache

- Populated by upstream Delta WS consumer (every ticker tick, <100ms)
- Engine reads without REST calls: `bus.bid(symbol)`, `bus.spot(symbol)`, `bus.want(*symbols)`
- Key insight: **Live price updates flow through WebSocket, not polling**
- Executor uses PriceBus for fill simulation + exit calculations (zero latency)

### Client-Side WebSocket
**File:** `static/js/wsclient.js`  
**Purpose:** Singleton `window.AppWS` for reconnection + heartbeat + subscriptions

- Exponential backoff + jitter on disconnect (prevents thundering herd)
- 15-second heartbeat (empty frame keeps connection alive)
- 45-second stall detector (auto-reconnect if no data received)
- Outbound queue (messages sent even if connection down; drained on reconnect)
- Visibility-change listener (reconnect faster when tab regains focus)
- Online event listener (reconnect faster when WiFi returns)

---

## Current Status

| Component | Status | Notes |
|-----------|--------|-------|
| **Live ticker** | ✅ Working | 0.5s latency, WebSocket push |
| **Option chain** | ✅ Working | 0.5s latency, fixed canvas bug |
| **Strategy engine** | ✅ Working | 1.0s state latency, runs if enabled |
| **Paper fills** | ✅ Working | Simulates bid/ask + 18% GST fee |
| **Live demo (testnet)** | ✅ Working | Real orders, one-line `.env` switch |
| **Journal** | ✅ Working | SQLite + JSONL trade log |
| **Backtest** | ✅ Working | Offline replay, pure evaluation |
| **Walk-forward research** | ✅ Working | Train/test/holdout split scoring |
| **IV surface / VRP** | ✅ Working | 30-min snapshots, time-series lab |
| **Performance analytics** | ✅ Working | Equity curve, stats, Sharpe |
| **Health dashboard** | ✅ Working | Auth status, data freshness, errors |

**Known Limitations:**
- No validated edge (all 8 strategies tested negative)
- Testnet liquidity thin on options
- Market orders pay ~15% spread
- IP whitelist required (testnet API key binding)

---

## Testing Workflow (Post-Fix)

### Verify Canvas Fix
```
1. Navigate to http://localhost:8000/option-chain
2. Open browser DevTools (F12)
3. Console: document.getElementById('oi-chart').clientHeight
4. Wait 30 seconds (60+ data updates at 0.5s)
5. Console: document.getElementById('oi-chart').clientHeight
   → Should still be 300; not growing
```

### Verify WebSocket Latency
```
1. Navigate to http://localhost:8000/strategy
2. Observe "Signal Feed" panel timestamps
3. Every frame should arrive ~1.0s apart (engine topic)
4. Compare to `/option-chain` table: should update ~0.5s apart (chain topic)
```

### Verify Engine State Flow
```
1. Navigate to http://localhost:8000/strategy
2. Enable one strategy (toggle checkbox)
3. Click "Start"
4. Check status: should show "Live · N contracts streaming" (green)
5. Check `/journal` → new trade events appear in real-time
6. Click "Stop" → status changes to "Stopped"
7. Engine should stop emitting engine topic (state goes stale ~1s)
```

---

## Files Modified (This Session)

| File | Change | Reason |
|------|--------|--------|
| `static/js/option_chain.js` | `setupCanvas()`: read from `data-h` instead of `getAttribute("height")` | Fix canvas growth loop |
| `templates/option_chain.html` | Change `height="300"` to `data-h="300"` on canvases | Immutable storage for CSS height |
| `main.py` | Add `ChainStreamer`, `chain_publisher()`, `engine_publisher()`, `AppHub`, WebSocket routing | Fix 5s delay; optimize REST load |
| `static/js/wsclient.js` | Singleton `AppWS` with reconnect, heartbeat, topic subscriptions | Reliable real-time connection |
| `strategy/pricebus.py` | Thread-safe in-memory price store | Zero-latency reads in hot path |
| `strategy/eventbus.py` | Bounded queue bridging engine → asyncio | Safe producer/consumer decoupling |

---

## Handoff Checklist

- ✅ Canvas DPR scaling bug identified and fixed
- ✅ WebSocket latency root causes addressed (auto-refresh flag, engine publisher, chain publisher)
- ✅ Project structure documented (`PROJECT_STRUCTURE.md`)
- ✅ AI handover guide created (`AI_HANDOVER_GUIDE.md`)
- ✅ Recent changes documented (this file)
- ✅ Testing procedures verified
- ✅ Code is production-ready for testnet
- ⚠️ **REMINDER:** No validated edge; do not deploy capital

---

## Next AI Session: Starting Points

1. **Read** this file + `PROJECT_STRUCTURE.md` + `AI_HANDOVER_GUIDE.md`
2. **Run** the app: `uvicorn main:app --host 127.0.0.1 --port 8000`
3. **Verify** fixes: navigate to `/option-chain` and check height stability
4. **Check** `/strategy` for real-time engine state (1.0s latency)
5. **Explore** one strategy backtest: `/backtest?strategy=ema-cross`
6. **Ask:** What's the next feature or fix?

---

**Generated by:** Claude (Sonnet 4.6)  
**Date:** 2026-07-21  
**Time spent:** ~45 minutes of coding + verification + documentation

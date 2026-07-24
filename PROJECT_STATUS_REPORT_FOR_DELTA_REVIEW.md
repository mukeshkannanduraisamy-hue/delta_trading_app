# Delta Trading App — Full Project Report (for Delta Exchange review)

Generated: 2026-07-24. Purpose: hand to Delta Exchange support/copilot to confirm
every Delta-specific assumption baked into this code is correct, and to get a
fix-prompt back for anything that is wrong.

No secrets are included in this file (API keys, account IDs excluded).

---

## 1. What this project is

A FastAPI web app (`D:\TRADER\delta_trading_app`) that:
- Streams live BTC/ETH/SOL spot + option data from Delta Exchange India over
  WebSocket (`wss://socket.india.delta.exchange`) and REST
  (`https://api.india.delta.exchange`).
- Runs 8 rule-based technical strategies + 1 LLM-driven strategy that generate
  BUY-CALL / BUY-PUT signals on BTC options.
- **Currently operates in PAPER/SIMULATION mode only.** No real or testnet
  orders are placed. All entries, exits, fees, and P&L are computed internally
  against live market prices. Every authenticated (order-placing) API method is
  hard-disabled (`raise NotImplementedError`) at the client layer.
- Persists every simulated trade to SQLite and serves a dashboard, option
  chain, journal, performance, backtest, and volatility-research UI.

**Stack:** Python 3 / FastAPI / `websockets` / `requests` / SQLite (stdlib
`sqlite3`) / vanilla JS + Jinja2 templates (no frontend framework, no build
step for the served UI — there is a separate unused `frontend/` Vite scaffold).

---

## 2. Repository layout

```
delta_trading_app/
├── main.py                  # FastAPI app, all HTTP+WS routes, upstream WS consumer
├── strategy/
│   ├── config.py             # central config, loaded from .env
│   ├── settings.py           # user-editable risk settings (settings.json)
│   ├── delta_client.py        # signed/public REST client for Delta Exchange
│   ├── market_data.py         # option chain resolver (ATM strike/expiry selection)
│   ├── pricebus.py            # in-memory live price cache (fed by the WS consumer)
│   ├── eventbus.py            # thread-safe queue: engine thread -> asyncio UI push
│   ├── engine.py              # polls market data, evaluates strategies, manages exits
│   ├── executor.py            # order lifecycle: open/close/partial-TP/SL/adopt/flatten
│   ├── options_calc.py        # smart entry, SL/TP, theta/IV/gamma risk maths
│   ├── indicators.py          # EMA/SMA/RSI/MACD/ATR/Supertrend/HMA/ADX/etc (pure Python)
│   ├── zing_strategies.py     # 8 rule-based strategies
│   ├── zing_strategies_v2.py  # (secondary/experimental strategy variants)
│   ├── ai_strategy.py         # LLM-driven strategy (NVIDIA-hosted models)
│   ├── base.py                # Signal / Context / Strategy base classes
│   ├── account.py             # simulated wallet (paper mode)
│   ├── journal.py             # in-memory + JSONL append-only event log
│   ├── store.py                # SQLite: trades, iv_snapshots, performance aggregates
│   ├── volatility.py           # IV/RV/skew snapshot + variance-risk-premium tracking
│   ├── research.py / backtest.py / optimizer.py  # offline research tools
│   └── diagnostics.py
├── static/js/                 # dashboard, strategies, journal, performance, chart JS
├── templates/                  # Jinja2 pages
├── research/                    # standalone edge-research scripts (separate from strategy/)
├── settings.json                # user-editable risk parameters (see §4)
├── .env                          # secrets + engine config (not included here)
├── verify_audit_fixes.py         # offline regression suite (13 checks, no network)
└── docs/                         # prior audit reports, handover notes
```

---

## 3. Live data flow

1. `delta_ws_consumer()` (main.py) holds ONE upstream WS connection to Delta,
   subscribed to `v2/ticker` for BTCUSD/ETHUSD/SOLUSD plus every candlestick
   resolution for the chart symbol, plus `all_trades`.
2. Every `v2/ticker` frame (underlying **and** option contracts once the engine
   starts tracking them) is written into `pricebus` — an in-memory dict keyed
   by symbol — before anything else happens, so the trading engine never waits
   on a browser or a REST round-trip.
3. `engine.py` polls `pricebus` for spot, evaluates strategies against
   REST-fetched historical candles (only when a new bar has closed), resolves
   the ATM call/put via `market_data.OptionResolver`, and hands signals to
   `executor.py`.
4. `executor.py` simulates the fill, books simulated fees, and manages the
   position's stop-loss / multi-tier take-profit / time exit / settlement exit
   every cycle.
5. All UI pages are pushed live over `/ws/app` (app/engine/journal/health/
   account state) and `/ws/market` (ticker/candle/trade tape) — no polling.

---

## 4. Configuration parameters currently in effect

### 4.1 `.env` (non-secret values)
| Key | Value | Meaning |
|---|---|---|
| `EXECUTION_MODE` | `live_demo` (invalid value → **silently resolves to `paper`**, see §7 finding F1) | intended execution mode |
| `ENGINE_ASSET` | `BTC` | underlying asset traded |
| `ENGINE_CONTRACTS` | `1` | (currently unused directly — position sizing comes from confidence tiers, see §4.3) |
| `ENGINE_AUTOSTART` | `false` | engine does not start automatically on boot |
| `ENTRY_ORDER_TYPE` | `limit` | resting limit entry (vs. market) |
| `AI_MODEL` | `z-ai/glm-5.2` | primary LLM for the AI strategy |
| `AI_FALLBACK_MODELS` | `abacusai/dracarys-llama-3.1-70b-instruct` | fallback if primary times out |
| `AI_TEMPERATURE` | `0.2` | low, for near-deterministic trading calls |
| `AI_REFRESH_SECONDS` | `300` | min seconds between LLM calls |
| `AI_MIN_CONFIDENCE` | `0.65` | ignore low-confidence LLM calls |
| `AI_TIMEOUT_SECONDS` | `25` | per-model call timeout |

### 4.2 `settings.json` (user-editable via the UI Settings modal)
```json
{
  "max_lot_size": 100000,
  "max_leverage_cap": 200,
  "leverage_very_low": 2,  "leverage_low": 5,  "leverage_medium": 10,
  "leverage_high": 15,     "leverage_very_high": 20,
  "lot_pct_very_low": 10,  "lot_pct_low": 25,  "lot_pct_medium": 50,
  "lot_pct_high": 75,      "lot_pct_very_high": 100,
  "starting_virtual_balance": 100000,
  "daily_loss_limit_pct": 50,
  "max_open_positions": 50
}
```
Confidence-tiered sizing (`_confidence_params` in executor.py):
| Confidence | Lot % of max_lot_size | Leverage |
|---|---|---|
| ≤20 | 10% | 2x |
| ≤40 | 25% | 5x |
| ≤60 | 50% | 10x |
| ≤80 | 75% | 15x |
| >80 | 100% | 20x |
(leverage is additionally capped at `max_leverage_cap`)

### 4.3 Hardcoded fee model (`config.py`)
```
FEE_NOTIONAL_RATE = 0.0003   # 0.03% of notional
FEE_PREMIUM_CAP   = 0.035    # 3.5% of premium (whichever is LOWER wins)
GST_RATE          = 0.18     # 18% GST on top of the fee
```
Applied as: `fee = min(0.03% * spot * contract_value * contracts, 3.5% * premium * contract_value * contracts) * 1.18`

**⚠️ This is the #1 thing to verify with Delta Exchange** — see §8, Q1.

### 4.4 Options risk-engine constants (`options_calc.py`)
- **DTE / theta buffer multiplier:**
  | DTE range | Theta days multiplier |
  |---|---|
  | > 21d | 1.0x |
  | 8–21d | 1.5x |
  | 4–7d | 2.0x (weekly warning) |
  | < 4d | 3.0x (near-expiry warning) |
  | Hard floor: `MIN_HOURS_TO_EXPIRY = 2.0` hours — trade blocked below this |
- **IV regime (IVR proxy, computed as `(current_iv - 0.30) / (1.20 - 0.30) * 100`, clamped [0,100]):**
  | IVR | Buffer % | Regime |
  |---|---|---|
  | ≤20 | 5% | LOW IV |
  | ≤40 | 8% | BELOW AVERAGE |
  | ≤60 | 12% | AVERAGE |
  | ≤80 | 20% | HIGH IV (warning) |
  | >80 | 30% | VERY HIGH IV (warning) |
  | `IV_SKIP_THRESHOLD = 85` — trade auto-skipped above this IVR |
- **Gamma risk scaling (widens SL):**
  | DTE | Gamma factor |
  |---|---|
  | >14d | 1.0x |
  | 7–14d | 1.2x |
  | 4–6d | 1.5x |
  | <4d | 2.0x |
- **Stop-loss hard bounds:** clamped to [10%, 90%] of entry premium; never ≤0.
- **Take-profit:** 3-tier (TP1/TP2/TP3), ratios scaled by DTE band × confidence
  tier (e.g. DTE>21 & conf>80 → 1.5x/2.5x/4.0x risk); each TP level is reduced
  by its estimated theta cost in days-to-target; a TP that nets below entry
  after theta cost is dropped (`None`) rather than offered.
- **Partial-exit ladder on hit:** 40% at TP1, 40% at TP2, remaining 20% at TP3
  (SL trails to breakeven at TP1, to TP1 at TP2).
- **Settlement:** options settle at **12:00 UTC** on expiry date (hardcoded
  assumption — verify in §8, Q2).
- **Symbol → expiry parsing:** expects a 6-digit `DDMMYY` token in the option
  symbol (e.g. `...-241225-...`); verify in §8, Q3.

### 4.5 Engine safety parameters (`config.py`)
| Parameter | Value | Purpose |
|---|---|---|
| `MIN_HOURS_TO_EXPIRY` | 2.0h | refuse to enter an expiry closer than this |
| `MAX_OPEN_POSITIONS` | 8 (config default; UI setting `max_open_positions=50` overrides in practice) | cap on concurrent positions |
| `ORPHAN_POLICY` | `adopt` | untracked exchange positions get brought under management automatically |
| `ADOPT_SL_PCT` / `ADOPT_RR` | 0.30 / 1.5 | bracket applied to an adopted (orphaned) position |
| `ACCOUNT_SYNC_SECONDS` | 15s | account snapshot refresh cadence |
| `FLATTEN_ON_SHUTDOWN` | true | close all positions on app shutdown |
| `DEFAULT_MAX_HOLD_BARS` | 60 | time-based exit if nothing else triggers |
| `COOLDOWN_BARS` | 5 | bars to wait before re-entering the same (strategy, direction) pair |

---

## 5. Strategies implemented (8 rule-based + 1 AI)

| Slug | Timeframe | Basis | Rule summary | RR |
|---|---|---|---|---|
| `ema_cross` | 1m | underlying | 9/21 EMA crossover, fires on cross candle only | 1.2 |
| `scalping_pulse` | 1m | underlying | 9/21 EMA trend + pullback to fast EMA + breakout confirm | 1.0 |
| `traffic_light` | 1m | underlying | 2-candle colour-flip breakout vs 15-SMA trend filter | 1.2 |
| `inside_candle` | 5m | underlying | Mother/baby inside-bar breakout, ≤35% beyond range | 2.0 |
| `mean_reversion_bollinger` | 1m | underlying | Fade Bollinger(20,2) band crosses | 1.5 |
| `prime_scalper_ema` | 1m | underlying | ATR-normalized EMA(21) slope momentum cross | 1.2 |
| `swingking_sniper` | 5m | underlying | 20/50 EMA trend + pullback, longer hold (48 bars) | 2.0 |
| `booming_bulls_supertrend` | 1m | **premium** (reads option price directly) | SMA(20)+2 Supertrends alignment on the option premium itself | 1.5 |
| `ai_strategy` | — | LLM | NVIDIA-hosted model call every ≥300s, min confidence 0.65 | — |

All strategies express risk as `sl_pct` of the option premium; the engine
converts that + the options-risk-engine output into actual price levels at
fill time.

---

## 6. What has been independently verified correct (code-level audit, 2026-07-24)

Full-codebase audit against the "no symptom patches, trace root causes"
standard. These were checked line-by-line and confirmed sound — no changes
needed:

- Multi-timeframe ATR (15m/1h/4h) with sane fallbacks when a candle window is
  empty.
- DTE settlement-time and `MIN_HOURS_TO_EXPIRY` gating.
- IVR bounds clamped [0,100]; `IV_SKIP_THRESHOLD` enforced.
- Stop-loss bounds [10%,90%] and `final_sl > 0` guaranteed.
- Theta erosion check correctly scales `daily_theta * contract_value / price`.
- Dual-condition SL (option premium SL **and** BTC spot SL, whichever fires
  first).
- Option-chain resolver refuses a quote with missing/zero `contract_value` or
  `strike` rather than silently mis-sizing every downstream P&L calc.
- Live exit price is always the **best bid** (never mark price) — prevents
  phantom spread profit in the simulation.
- Candle pagination correctly pages backward past Delta's ~2000-bar response
  cap.
- Rate-limit header parsing handles Delta's milliseconds-vs-seconds
  ambiguity.
- Engine's worker-thread isolation: all blocking REST calls run in
  `asyncio.to_thread`, so the event loop (and every WebSocket) never freezes.
- Bar-clock gating: a strategy evaluates at most once per closed candle, and
  a fetch failure does NOT falsely consume the gate (would silently skip a
  whole bar of signals otherwise).
- SQLite connections always close (no file-handle leak); schema migrations
  are idempotent.
- Supertrend, ADX, StochRSI, HMA and every other indicator are correctly
  warm-up-guarded (return `None`/empty rather than crashing on short input).

---

## 7. Bugs found and FIXED in this audit (all verified, 13/13 regression checks pass)

| # | Severity | File | Root cause | Fix |
|---|---|---|---|---|
| 1 | HIGH | `delta_client.py` | `_public_get` raised `NameError` (undefined variable `url`) instead of the typed `DeltaError` whenever Delta responded `{"success": false}` — defeated the entire typed-exception error-handling contract | Corrected to reference the actual `base`/`path` params |
| 2 | MEDIUM | `executor.py` | Session equity line double-counted the entry fee (subtracted once at open, again at every close) — silently drifted the simulated P&L low | Entry fee is now charged exactly once, at open |
| 3 | MEDIUM | `executor.py` | Take-profit ladder was actually 40/30/30, not the documented 40/40/20, because TP2's exit size was a fraction of the *already-reduced* position instead of the *original* size | Added `initial_contracts` tracking + a pure `_tp_exit_size()` helper; ladder is now a true 40/40/20 |
| 4 | LOW-MED | `executor.py` | `flatten_shorts()` called the now-disabled `client.positions()`, which raises `NotImplementedError` — not caught by its `except DeltaError`, so the endpoint 500'd | Paper-mode guard returns a graceful no-op (there are no real exchange shorts in simulation) |
| 5 | LOW | `static/js/strategies.js` | Decision-counter UI miscategorized near-expiry auto-skips as "low confidence" because the skip message ("TRADE BLOCKED: Option expires in…") didn't match the JS's keyword filter | Filter now also matches `expire`/`blocked` |
| **D1** | **MEDIUM (safety-critical)** | `account.py` + `executor.py` + `store.py` | **Three different modules read three different "starting balance" values.** The simulated wallet and the equity curve started at 10,000 (a fixed `.env` constant) while the **daily loss-limit safety check** measured against 100,000 (the UI-editable setting). A 50%-of-100k threshold can never trip on a 10k-origin account — the loss-limit safety net was silently non-functional. | Introduced `config.starting_balance()` as one authoritative source (reads the UI setting), routed every consumer (wallet, session equity, both loss-limit checks, equity curve, manual-trade preview, config summary) through it. Verified the loss limit now correctly trips. |
| 6 | test debt | `verify_audit_fixes.py` | The regression suite itself was stale — it exercised `client.place_order()` expecting live order flow, which the paper-trading refactor had already disabled, so the suite crashed before finishing | Rewritten to test the actual current contract; extended to 13 checks covering all fixes above |

All fixes are applied to the working tree (not yet committed to git — awaiting
your go-ahead).

---

## 8. Questions for Delta Exchange support/copilot — please verify each

**Q1 — Fee schedule.** Is the options taker fee still `min(0.03% of notional,
3.5% of premium)` plus `18% GST` on top, for India options as of today? Any
recent change to these rates, or maker-fee rebates this simulation should
account for?

**Q2 — Settlement time.** Do BTC/ETH options on Delta India settle at exactly
**12:00 UTC** on the expiry date, every day (daily options included), with no
exceptions (e.g. holidays, different settlement windows for weekly vs.
monthly contracts)?

**Q3 — Symbol format.** Is the expiry date always encoded as a `DDMMYY`
6-digit token inside the option symbol (e.g. `C-BTC-100000-241225`)? Has this
format changed recently, or does it differ for weekly vs. monthly expiries?

**Q4 — Ticker/greeks field names.** The code reads `ticker["greeks"]["delta"
/"gamma"/"theta"]`, `ticker["mark_iv"]` or `ticker["quotes"]["mark_iv"]`,
`ticker["spot_price"]`, `ticker["mark_price"]` from `GET /v2/tickers/{symbol}`
and the `v2/ticker` WebSocket channel. Are these still the correct field
names/paths on the current API version? Is `theta` returned as a per-day
value scaled to 1.0 underlying unit (this code multiplies it by
`contract_value` to get per-contract theta — is that the right scaling)?

**Q5 — Contract value / tick size source.** The code takes `contract_value`
and `tick_size` from `GET /v2/products` per product and refuses to trade a
contract if either is missing/zero. Is `/v2/products` still the correct and
only source for these fields for options contracts?

**Q6 — Rate limits.** Current rate-limit handling reads `X-RATE-LIMIT-RESET`
(assumed to be milliseconds when the raw value is >1000, else seconds) with a
fallback to the standard `Retry-After` header. Is this still accurate for the
India REST API?

**Q7 — WebSocket liveness.** Is `wss://socket.india.delta.exchange` still the
correct endpoint, and is a `v2/ticker` subscription still the right channel to
receive live option-contract quotes (bid/ask/mark/greeks) alongside
underlying spot?

**Q8 — Order endpoint contract (dormant code, disabled today).** If/when this
project re-enables live order placement, is the following still accurate for
`POST /v2/orders`: `size` as an integer number of contracts, `reduce_only` as
a **string** `"true"`/`"false"` (not boolean), `client_order_id` capped at 32
characters, `limit_price` sent as a **string**?

---

## 9. What to send back if something above is wrong

If Delta's copilot flags any of Q1–Q8 as outdated, reply with the corrected
value/behavior for each flagged item and I will locate the exact file/line
and apply a root-cause fix (not a patch) — the same way the fixes in §7 were
done: trace every downstream consumer of the changed value, update all of
them, and add a regression check to `verify_audit_fixes.py` so it can never
silently regress again.

---

## 10. Current operational status

- **Mode:** paper/simulation only. No real money, no testnet orders. Verified
  by `verify_audit_fixes.py` (`mode=paper`, all authenticated client methods
  assert-disabled).
- **Engine:** stopped by default (`ENGINE_AUTOSTART=false`).
- **Regression suite:** 13/13 checks pass as of this report.
- **Known non-blocking items** (documented, not fixed — need a product
  decision, not a code fix):
  - DTE float bands in `options_calc.py` have two 1-hour gaps (e.g. DTE in
    `(6,7)` falls to the most conservative tier) — behavior is safe but not
    contiguous; a tuning choice.
  - `MAX_OPEN_POSITIONS` in `config.py` (8) is effectively overridden by the
    UI setting `max_open_positions` (50) everywhere it's checked — the config
    constant is currently dead code, not a bug, but worth pruning for clarity.

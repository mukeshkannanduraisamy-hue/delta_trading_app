# Phase 4 — Zing Trade Strategy Engine

An automated options strategy engine that implements the eight strategies
published on the [Zing Trade blog](https://zing.trade/blog/category/strategies/),
generates buy/sell signals from live Delta Exchange India market data, and
executes them — as **paper trades** (default) or as **real orders on the Delta
testnet demo book**.

> **Reality note:** Zing's published strategies are **Nifty index-option**
> intraday strategies, not crypto. This engine adapts their indicator/price-action
> rules to Delta **BTC/ETH options**: signals are computed on the `BTCUSD` perp
> candles (or the option premium for Booming Bulls) and executed by buying the
> ATM call (CE) or put (PE).

## Run

```powershell
cd D:\TRADER\delta_trading_app
.\.venv\Scripts\python.exe -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000/strategy>.

Use the toggles to enable strategies, then **Start**. The panel shows live spot,
paper balance, open positions with unrealized P&L, and a signal/trade feed. The
**Demo Wallet** panel reads the real testnet balance via signed API calls.

## Execution modes (`.env` → `EXECUTION_MODE`)

| Mode | What it does |
|------|--------------|
| `paper` *(default)* | Simulates fills: **buy at the ask, exit at the bid** (spread is a real cost), applies Delta's option taker fee + 18% GST, tracks virtual P&L. **No orders are sent.** |
| `live_demo` | Places **real market orders** on the Delta **testnet** demo book via `POST /v2/orders`. No real money, but real order flow. |

Going live-demo is a deliberate one-line change in `.env` — the engine never
auto-arms real orders.

## The 8 strategies

| Strategy | TF | Basis | Rule (as published, adapted) |
|----------|----|----|------|
| EMA Cross | 1m | underlying | 9/21 EMA crossover; fires only on the crossover candle |
| Scalping Pulse | 1m | underlying | Trend (9/21 EMA) + pullback to fast EMA + confirmation candle |
| Traffic Light | 1m | underlying | Two-candle colour-flip; break of pair high→CE, low→PE (OCO) |
| Inside Candle | 5m | underlying | Inside-bar breakout, ≤35% proximity filter; SL at opposite end, 2:1 |
| Mean Reversion Bollinger | 1m | underlying | Fade band extremes: above upper→PE, below lower→CE |
| Prime Scalper EMA | 1m | underlying | ATR-normalized EMA slope vs threshold + confirm candle |
| SwingKing Sniper | 5m | underlying | High-conviction 20/50 EMA trend + pullback, longer hold |
| Booming Bulls Supertrend | 1m | **premium** | SMA + fast/slow Supertrend on the option premium; TP +7.5% / SL −5% |

Risk is managed uniformly as a **% of the option premium** (Nifty point targets
don't translate to crypto), preserving each strategy's published reward:risk
ratio. Booming Bulls keeps its literal 7.5%/5%.

## Configuration (`.env`)

| Key | Default | Meaning |
|-----|---------|---------|
| `EXECUTION_MODE` | `paper` | `paper` or `live_demo` |
| `ENGINE_ASSET` | `BTC` | underlying (BTC / ETH) |
| `ENGINE_CONTRACTS` | `1` | option contracts per trade (BTC contract = 0.001 BTC, so raise this for meaningful demo P&L) |
| `ENGINE_POLL_SECONDS` | `15` | evaluate / manage interval |
| `ENGINE_AUTOSTART` | `false` | start the engine on app boot |
| `ENGINE_MAX_OPEN` | `8` | hard cap on concurrent open positions |
| `ENGINE_MAX_HOLD_BARS` | `60` | time-based exit (in candles) |

## Package layout (`strategy/`)

```
config.py          env + endpoints + fee model
delta_client.py    signed REST client (HMAC): candles, chain, orders, balances
market_data.py     nearest-expiry + ATM CE/PE resolution, live bid pricing
indicators.py      SMA, EMA, ATR, Bollinger, Supertrend (no numpy)
base.py            Signal + Strategy contract
zing_strategies.py the 8 strategies + registry
executor.py        paper + live_demo fills, position mgmt, TP/SL/time exits, fees
engine.py          poll → evaluate → route signals → manage; background task
journal.py         append-only JSONL trade log (strategy/journal/trades.jsonl)
```

## API

- `GET /strategy` — control panel
- `GET /api/strategy/status` — engine + positions + stats + strategy list
- `POST /api/strategy/start` · `POST /api/strategy/stop?flatten=true`
- `POST /api/strategy/toggle` `{slug, enabled}`
- `POST /api/strategy/flatten` — close all open positions
- `GET /api/strategy/journal?limit=` — trade feed
- `GET /api/account` — live testnet demo balances + positions (signed)

## Caveats / honest limitations

- **Not investment advice, and no proven edge.** These are educational
  strategies; the earlier autotrader backtests found no cost-surviving edge on
  BTC. Treat paper results as a plumbing test, not a green light to risk capital.
- Paper fills use live bid/ask + Delta's fee model, but ignore slippage on
  fast moves and assume your order size doesn't move the book.
- Testnet option liquidity/candles can be thin; premium-based Booming Bulls is
  most affected in `live_demo`.
- Demo API keys work **only** against the testnet base; prod keys only against prod.

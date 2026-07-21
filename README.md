# Delta Options Trading Engine

A FastAPI application that runs eight quantitative option strategies against
**Delta Exchange India's testnet demo book**, with live WebSocket market data, a
research harness, and a full audit trail.

> ### ⚠ Read before running
>
> **This engine is LIVE ONLY.** Paper mode was removed. Starting the engine
> places **real orders** on the Delta **testnet** demo book. There is no dry run.
>
> No real money is reachable — signed requests are hardcoded to the testnet base
> in `strategy/delta_client.py` — but the order flow is genuine.
>
> **None of these strategies are profitable.** A 90-day study over 129,600 1m
> bars tested 274 parameter combinations and found **zero** with positive gross
> expectancy. Seven of eight are classified CONCEPT_FLAWED. See
> [`docs/RETUNE_STUDY_2026-07-21.md`](docs/RETUNE_STUDY_2026-07-21.md).
>
> This is a **systems and research project** — an execution engine, a data
> pipeline, and an honest negative result. It is not a money-making tool, and
> nothing here is investment advice.

---

## Quick start

```bash
python -m venv .venv
.venv/Scripts/pip install -r requirements.txt     # Windows
cp .env.example .env                              # then fill in your testnet keys
.venv/Scripts/python -m uvicorn main:app --host 127.0.0.1 --port 8000
```

Open <http://localhost:8000>. The engine does **not** auto-start
(`ENGINE_AUTOSTART=false`); arming it requires an explicit click and a
confirmation dialog.

Get testnet API keys at <https://demo.delta.exchange>. Demo keys authenticate
only against the testnet base.

## Pages

| Route | What it does |
|---|---|
| `/` | Dashboard — live tickers, chart, health strip |
| `/option-chain` | Live option chain: OI, IV, greeks, PCR, max pain (0.5 s push) |
| `/strategy` | Engine control, account sync, positions, signal feed (1 s push) |
| `/journal` | Filterable trade history |
| `/performance` | Equity curve, win rate, profit factor, drawdown |
| `/backtest` | Replay a strategy's real `evaluate()` over history |
| `/research` | Walk-forward screen (train / test split) |
| `/volatility` | IV surface, 25-delta skew, variance risk premium |

## Architecture

```
Delta WebSocket ──> PriceBus (in-memory, lock-guarded)
                        │
                        ├──> Engine loop ──> strategies ──> Executor ──> Delta REST
                        │                                       │
                        │                                       └──> journal (JSONL + SQLite)
                        │
                        └──> EventBus ──> asyncio broadcaster ──> browser WebSocket
```

- **`strategy/pricebus.py`** — every tick lands in memory; the engine reads spot
  and premia with no REST call in the hot path.
- **`strategy/account.py`** — polls balances/positions/orders every 15 s
  *independently of the engine*, so a stopped engine still shows a truthful
  account. The exchange is the source of truth; the engine's book is a model.
- **`strategy/eventbus.py`** — bounded queue bridging the engine's worker thread
  to the event loop. Drops oldest on overflow so a slow UI can never stall
  trading.
- **`research/`** — 90-day backtest harness with numpy strategy replicas that
  are **proven equivalent** to the production `evaluate()` (`equivalence.py`,
  100% agreement) before any grid search is trusted.

## Safety properties

Earned the hard way — a full audit on 2026-07-21 found 36 issues, 13 fixed.
See [`docs/AUDIT_2026-07-21.md`](docs/AUDIT_2026-07-21.md).

- Every exit order carries `reduce_only=true`, so a duplicate send can never
  flip the account short.
- Every order carries a `client_order_id`; a timed-out submission is resolved
  against the exchange rather than dropped.
- Real-time `/v2/positions?product_id=` drives order decisions — never the
  10 s-stale bulk endpoint.
- No option is bought within `ENGINE_MIN_HOURS_TO_EXPIRY` of settlement.
- Async code never acquires the executor lock (a blocking lock on the event loop
  froze the entire app for the length of a REST timeout).
- The engine refuses to start without working credentials — with no simulated
  fallback, a silent degrade would look like trading while filling nothing.
- Exits keep running while the engine is stopped: *stopped* means "opens nothing
  new", not "abandons open positions".

## Verification

```bash
python verify_audit_fixes.py     # audit invariants (offline)
python verify_live_only.py       # no simulated-fill path remains (sends no orders)
python verify_account_sync.py    # adopt / ghost-clear / drift (sends no orders)
```

Each stubs both the API client **and** the trade store, so running them cannot
touch the exchange or write synthetic rows into the trade history.

## Research results

| | |
|---|---|
| Data | BTCUSD 1m (129,600 bars) + 5m (25,920), 90 days, 100% complete |
| Combinations tested | 274 |
| With positive gross expectancy | **0** |
| Verdict | 7 of 8 CONCEPT_FLAWED; nothing retuned, because nothing earned a retune |

Delta's fee model alone costs ~2.2 R per trade at 1m, where a 1×ATR stop is
smaller than ordinary intrabar noise. That is an arithmetic problem, not a
tuning problem — worth checking the fee/ATR ratio *before* any backtest.

## Docs

- [`docs/DOCUMENTATION_INDEX.md`](docs/DOCUMENTATION_INDEX.md) — start here
- [`docs/AUDIT_2026-07-21.md`](docs/AUDIT_2026-07-21.md) — 36 issues, 13 fixed
- [`docs/RETUNE_STUDY_2026-07-21.md`](docs/RETUNE_STUDY_2026-07-21.md) — the negative result
- [`docs/LIVE_ONLY_2026-07-21.md`](docs/LIVE_ONLY_2026-07-21.md) — paper-mode removal
- [`PHASE4_STRATEGY_ENGINE.md`](PHASE4_STRATEGY_ENGINE.md) — the eight strategies

Documents dated before 2026-07-21 describe a `paper` execution mode that no
longer exists; the index carries a correction.

## License

MIT — see [`LICENSE`](LICENSE).

## Disclaimer

Educational and research software. Not investment advice. The author is not a
licensed financial advisor. Trading derivatives carries substantial risk of
loss. The strategies here are documented as unprofitable — do not deploy capital
against them.

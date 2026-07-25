# Options Edge Research & Risk Management — Design

Date: 2026-07-25
Status: approved (design), implementation in progress
Scope: `D:\TRADER\delta_trading_app`

---

## 1. Problem

The live app runs 8 rule-based strategies on BTC options. A prior 90-day study
found all 8 have approximately zero gross directional edge, and that the 1m
underlying cost model consumes ~2.9 R per trade. No strategy has ever cleared a
credible acceptance bar on this project.

The goal is to search for a strategy that survives realistic option costs,
validate it against historical data under a bar fixed in advance, and rebuild
position sizing so it derives from measured edge rather than guesswork.

## 2. Findings that constrain the design

Established by direct probing of the Delta India API on 2026-07-25:

| Finding | Evidence | Consequence |
|---|---|---|
| Option symbols encode expiry as `DDMMYY` | `C-BTC-63000-270726` | Confirms the app's existing parser (report Q3) |
| Options settle at 12:00 UTC | Last mark bars on an expired contract are 11:58, 11:59 | Confirms the app's assumption (report Q2) |
| Traded option candles are single-print and flat | `C-BTC-63000-270726` returns OHLC `1197/1197/1197/1197`, vol 5 | Traded option candles are unusable for fills — this is the "thin-liquidity fill artifact" that inflated the earlier options study |
| `MARK:` option candles are continuous | `MARK:C-BTC-...` returns varying OHLC, no volume | Mark series is the usable premium path |
| **Option candle history is retained ~2 days past expiry** | 46 expired BTC expiries mapped: 1-day-old → 1440 bars; 2-day → 1441; 3-day → 1–2; June expiries → **0** | **A multi-month backtest on real option data is impossible.** Real option data exists only in a ~3-day rolling window |
| Underlying 1m history goes back ≥540 days | Full 120/120 bars at 90/180/270/365/540d probes | Signal research can use 365 days of real data, not 90 |

The decisive constraint is option-data retention. It forces the validation
split described in §4.

## 3. Decisions

| Decision | Choice | Rationale |
|---|---|---|
| Instrument | BTC options; app architecture unchanged | User's call; keeps existing execution layer |
| Validation | Signal on real spot, options as calibrated overlay | Only honest option available given ~3-day option retention |
| Acceptance bar | Strict: OOS + walk-forward + shuffle test | Fixed before the search so the search cannot select the bar |
| If nothing passes | Report plainly, list closest near-misses and the criterion each failed; ship nothing | Prior studies cleared nothing at weaker bars; this is a likely and valid outcome |
| Risk focus | Fix dangerous defaults; size from measured edge | Current defaults permit 100% lot at 20× on one signal |
| Search scope | Existing 8 + 6 new families across 1m–4h | Existing 8 were only ever tested at one hardcoded timeframe |
| Build approach | Generalize the proven research core; add new modules | `control.py` / `equivalence.py` / `significance.py` are what make results credible; rebuilding them re-earns trust already held |
| Git | **No commits, no pushes.** All changes local for review | Explicit user instruction |

## 4. Architecture

```
research/
  # KEPT — proven, self-validating
  sim.py            no-look-ahead fill model        → cost model made injectable
  vec.py            vectorized indicators + existing 8 replicas
  control.py        random-entry / direction-shuffle nulls
  equivalence.py    replica-vs-production signal proof
  significance.py   t-test + multiple-comparison correction

  # GENERALIZED
  data.py           any resolution; 1m fetch + resample to higher TFs

  # NEW
  optdata.py        live chain + rolling real-option window (traded & MARK)
  calibrate.py      fit option overlay to real option data, with holdout
  costs.py          CostModel protocol: PerpCost | OptionCost(calibrated)
  families.py       new signal families as vec.py-style replicas
  gauntlet.py       the Strict bar as a composable criteria pipeline
  sizing.py         position sizing from measured edge
  search.py         driver: enumerate (family × timeframe × params) → gauntlet
  report2.py        final report including near-misses
```

Two modularity requirements, per review feedback:
- `costs.py` exposes a `CostModel` protocol; new cost structures are added as a
  class, not by editing existing ones.
- `gauntlet.py` holds a composable list of criterion objects, not a hardcoded
  if-chain, so criteria can be added or reordered without touching the driver.

### 4.1 Data layer

- Fetch **365 days of BTCUSD 1m** once (~526k bars, ~263 paginated calls),
  cached as `.npz`.
- Derive 5m/15m/1h/4h by **resampling the single 1m series**, not by separate
  fetches. Separate fetches can disagree at bar boundaries and silently
  introduce look-ahead. Resampled 5m is spot-checked against Delta's native 5m
  and any divergence is reported.
- `data.validate()` gates every series (bars, gaps, completeness, OHLC sanity).
  A series that fails does not enter the study.
- Option data: live chain plus the ~2–3 day expired window, both traded and
  `MARK:`, cached to disk. Each run extends the cache, so the calibration window
  grows over time at no extra cost. This is caching, not a recorder daemon.

## 5. Option cost overlay

Everything downstream depends on a faithful spot-path → premium-path mapping.

1. **Pricing core.** Proper Black-Scholes, replacing the
   `spot × IV × √T × 0.4` approximation in the current `premium.py`, which
   yields no usable greeks. `r ≈ 0` for crypto.
2. **IV model.** The current `premium.py` sets IV equal to trailing realized
   vol. That is wrong in an optimistic direction: real ATM crypto IV trades
   persistently above RV (variance risk premium), so bought options cost more
   and decay faster than that model implies. Fit instead from real option data:
   level (`IV ≈ a + b·RV` against real `mark_iv`), term structure, and skew.
3. **Spread model.** The current model omits bid/ask entirely; on Delta's option
   book the spread is the dominant cost. Measured from live `/v2/tickers`
   best_bid/best_ask as % of premium, bucketed by moneyness × DTE.
4. **Fee model.** Delta's real options fee,
   `min(0.03% × notional, 3.5% × premium) × 1.18`, read per-product from
   `/v2/products` rather than hardcoded.
5. **Fill model.** Entries pay the ask, exits receive the bid. Never mid. This
   matches what the live executor already does.

### 5.1 Calibration parsimony (review requirement)

Given only ~3 days of real option history, model complexity is gated on
demonstrated holdout improvement. Default to the simplest shape (flat term,
flat skew); add structure only where the holdout says it helps; break ties
toward the more pessimistic fit. Do not fit a surface the data cannot support.

### 5.2 Credibility gate

The overlay is validated against **held-out real option prices**: given the real
spot path plus a contract's K/T for a period it was not fitted on, its output is
compared to that contract's real `MARK:` series. Reported as MAE/RMSE in % of
premium, and R² on premium *changes* (changes are what P&L is made of).

**If the overlay cannot track real option prices within a stated band, the study
reports that and stops**, rather than emitting strategy results built on a price
model known to be broken.

The measured tracking error is then **propagated**: the gauntlet re-runs with the
overlay perturbed across its error band, and a candidate must pass across the
band, not merely at the point estimate. Model uncertainty becomes a robustness
requirement instead of a hidden number.

This overlay is strictly more pessimistic than the current `premium.py` on every
axis. Candidates that looked positive under the old model may not survive it.

## 6. Signal families

The existing 8 have ~0 gross edge, meaning their entries are coin flips. The
plausible remedy is not another coin flip but taking fewer flips in unfavourable
conditions. Roughly half the new work is therefore **gates** on existing
signals rather than new standalone entries: a filter that cuts trade count 70%
while leaving gross edge flat removes most of the cost bleed, a larger effect
than any realistic entry improvement.

Existing 8 are re-tested across all timeframes (they were only ever run at one).

| Family | Type | Rationale |
|---|---|---|
| Volatility regime | gate | ATR/RV percentile. Costs fixed, edge regime-dependent; the 8 fire indiscriminately |
| Session / time-of-day | gate + entry | Crypto intraday seasonality (Asia/London/US opens), currently ignored |
| Momentum persistence | entry | Trend-following amortizes fees at longer horizons; 7 of 8 existing are 1m |
| Breakout + volume | entry | Donchian break with volume z-score; existing breakouts ignore volume |
| Variance risk premium | gate | IV vs RV. The one genuinely options-native edge source; `volatility.py` already measures it |
| HTF trend alignment | gate | 1h/4h filter on lower-TF entries; classic chop defence |

Timeframes: 1m / 5m / 15m / 1h / 4h.

**Search budget is declared before running**, and the multiple-comparison
correction is applied over that full declared count **including combos tried and
discarded**. Searching thousands of combos and correcting as if a handful were
searched is the easiest way to manufacture a false winner.

## 7. The gauntlet

**Split of 365 days:**
- **Final holdout: last 73 days (20%), touched exactly once**, only by
  candidates that survived everything else.
- **Development: first 292 days**, divided into 4 anchored walk-forward windows
  (~73d each). For window *i*, parameters are selected on data strictly before
  *i*, then tested on *i*. This is what makes it out-of-sample rather than
  merely split.

**Criteria — all must pass, ordered cheapest-first:**

1. **≥200 trades** in development. A validity precondition, not an extra hurdle:
   below this the t-test has no power. Candidates failing *only* here are
   reported separately.
2. **Net expectancy > 0** after full option costs.
3. **Positive in ≥3 of 4** walk-forward windows.
4. **Beats the direction-shuffle null** at p<0.05 (`control.py`).
5. **Multiple-comparison-corrected significance** over the declared budget
   (`significance.py`).
6. **Survives the overlay error band** (§5.2) — passes 2–5 across the band.
7. **Final holdout net-positive.** Touched once.

Every failure records which criterion killed it, which is what makes the
near-miss report useful.

## 8. Risk management

### 8.1 Fix dangerous defaults — unconditional

Current `settings.json` permits, on a single high-confidence signal, 100% of
`max_lot_size` at 20× leverage, alongside 50 concurrent positions and a 50%
daily loss limit. These compose badly: 50 positions that are effectively one
BTC-direction bet, each at 20×, against a limit that only acts after half the
account is gone.

Replacement caps are **derived from the measured drawdown distribution**: size
so the 95th-percentile historical drawdown consumes no more than a stated
fraction of equity. Applied regardless of gauntlet outcome — the defaults are
wrong on their own terms.

### 8.2 Sizing from measured edge

Replace the confidence-tier lookup with fractional Kelly from the strategy's own
validated OOS trade distribution: `f* = edge / variance`, applied at
**quarter-Kelly**, hard-capped by §8.1. Kelly on an *estimated* edge is fragile;
the quarter discount is the standard defence against estimation error.

Structurally: **sizing becomes a function of validated edge.** A strategy that
did not clear the gauntlet gets size zero by construction. If nothing passes,
the engine ships with sizing disabled rather than tuned numbers implying
confidence the evidence does not support.

Stated limit: confidence tiers are a guess at edge; quarter-Kelly on a measured
edge is better but inherits all the uncertainty of the measurement. It is not a
safety guarantee.

## 9. Testing & error handling

**Correctness gates — each blocks the study:**
- `equivalence.py` extended: every new family's numpy replica must reproduce a
  plain scalar reference implementation on randomly sampled bars. Divergence
  invalidates loudly rather than silently.
- **Harness null re-run:** random entries at zero cost must return ~0.00 gross
  expectancy. If the harness's own null has drifted, every downstream number is
  suspect. This check is what caught the mis-specified slippage previously.
- **Overlay holdout** (§5.2).
- **Data integrity** via `data.validate()`.
- `verify_audit_fixes.py` stays green (17/17): research work must not regress
  the live app.

**Error handling:**
- Reuse `data.py`'s existing 429 / `X-RATE-LIMIT-RESET` and 5xx backoff. All
  fetches cached, so reruns are free and a mid-fetch failure resumes rather than
  restarts.
- A single strategy erroring is recorded as a failure for that combo and does
  not abort the sweep — but is reported, never swallowed.

**Reproducibility:**
- Fixed RNG seeds for every shuffle/permutation test, written into the results
  file alongside the declared search budget, so any published number can be
  regenerated exactly.

## 10. Out of scope

- Live order placement. The app stays paper-only; all authenticated client
  methods remain disabled.
- A forward option-data recorder daemon (considered, declined). The incremental
  cache in §4.1 covers calibration needs without a new subsystem.
- Rebuilding the app's execution layer for perpetuals.

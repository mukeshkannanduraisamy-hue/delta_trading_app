# Pre-Registration — Confirmatory Test

**Written:** 2026-07-25, BEFORE the validation data was loaded or inspected.
**Status at time of writing:** the validation window has never been fetched,
plotted, or summarised in this project.

This document exists so the result cannot be reinterpreted after the fact. It
is committed to before any number from the validation window is known.

---

## 1. Background

`STUDY_2026-07-25.md` was an **exploratory** study: 1,440 candidates searched,
2 survivors, both `mean_reversion_bollinger`, both collapsing on holdout
(−65%, −98%). Its conclusion was no validated edge.

That study's holdout is consumed. Its result generated a hypothesis; it cannot
also test it.

Delta serves BTCUSD 1m history back ~800–1000 days. The study used only the
most recent 365. **Days 365–800 have never been loaded in this project** and no
selection of any kind has been performed on them. That window is therefore a
legitimate, untouched confirmatory validation set.

---

## 2. Hypothesis (single, pre-specified)

> `mean_reversion_bollinger`, ungated, trading 1–3 day ATM options, has
> positive expectancy net of realistic Delta option costs.

This hypothesis is taken from the exploratory study's two survivors. It is
stated in advance precisely because the author has already seen those results —
declaring it up front is what prevents that knowledge from being used to fish.

---

## 3. What will be tested — exactly two candidates

| # | Family | Timeframe | DTE band | Gates |
|---|---|---|---|---|
| 1 | `mean_reversion_bollinger` | 5m | 1–3d | none |
| 2 | `mean_reversion_bollinger` | 15m | 1–3d | none |

**No other candidate will be run against this data.** Not other families, not
other timeframes, not other DTE bands, not gated variants, not parameter
variations. Two tests, not 1,440. That is what makes the outcome interpretable.

Parameters are inherited unchanged from `vec.DEFAULTS` — nothing is re-fitted.

---

## 4. Success criterion (fixed now)

A candidate **passes** only if, on the full untouched window:

1. It produces **≥ 200 trades** (statistical validity floor), and
2. **Expectancy > 0** net of the measured cost model, and
3. It **beats its own constant-direction control** (`drift_control.assess`) —
   i.e. the per-bar direction choice adds value over committing to one side.

Criterion 3 is included because BTC fell 44.5% over the exploratory window and
both option legs are long premium; a directional bias can manufacture apparent
edge without any signal. The confirmatory window's drift is unknown at the time
of writing and must not be used to argue the criterion away afterwards.

**The hypothesis is confirmed** only if at least one candidate passes all three.

---

## 5. Failure handling — declared in advance

If both candidates fail:

- The result is recorded as a **failed confirmation**. The exploratory study's
  negative conclusion stands and is strengthened.
- **No re-slicing.** The window will not be split into sub-periods to look for
  a segment where it worked.
- **No parameter adjustment.** No "it would have worked with a different
  lookback".
- **No third dataset.** There is no further untouched data; ~800 days is the
  limit of Delta's history.
- **No additional candidates** will be run against this window afterwards.

If a candidate passes, that is evidence *for* the hypothesis but not a
deployment decision: a single confirmatory pass on one instrument over one
historical window is not sufficient basis for risking capital. Position sizing
remains zero pending forward validation on the recorded book data.

---

## 6. Known limitations of this test

- The validation window is **older** than the training window (running backwards
  in time). The property that matters is that it is untouched and unselected,
  which holds — but market regime, liquidity, and volatility differ from the
  recent period, and this is a harder test than a forward one, not an easier one.
- The **option overlay** is calibrated to a single 2026-07-25 chain snapshot and
  is being applied to price options 1–2 years earlier. Its IV term structure and
  spreads almost certainly differed then. This is the single largest weakness of
  this test and is stated here rather than discovered later.
- The overlay passed its own credibility gate at exactly threshold (11/22).
- Costs are modelled at present-day rates; historical fees may have been higher.

These limitations bound how much any positive result can be trusted. They do not
weaken a negative result — if it fails even with present-day (cheap) costs, it
would fail harder with historical ones.

---

## 7. Reproducibility

- Validation window: days 365–800 before 2026-07-25, BTCUSD 1m
- Cost model: `min(0.01% notional, 3.5% premium) × 1.18`
- Overlay: Black-76, carry 4.42%/yr, IV term + smile from the 2026-07-25 snapshot
- Code: `research/confirm.py`, run once

---

## 8. RESULT (recorded 2026-07-25, after the test was run once)

**HYPOTHESIS CONFIRMED.** Both pre-registered candidates passed all three
criteria on the untouched window.

**Validation window as found:** 626,404 bars, 435 days, 2024-05-16 to
2025-07-25, 100.0% complete, 0 bad OHLC bars, 0 gaps over 5 minutes.
BTC **+76.3%** over the window — the OPPOSITE regime to the exploratory
window's −44.5%.

| Candidate | Trades | Expectancy | C1 ≥200 | C2 >0 | C3 beats constant | Verdict |
|---|---|---|---|---|---|---|
| `mean_reversion_bollinger` 5m 1-3d | 5,346 | **+0.0744 R** | ✓ | ✓ | ✓ | **PASS** |
| `mean_reversion_bollinger` 15m 1-3d | 1,562 | **+0.1391 R** | ✓ | ✓ | ✓ | **PASS** |

**Direction controls (criterion 3):**

| Candidate | Real | All-call | All-put | Call % |
|---|---|---|---|---|
| 5m | +0.0744 | +0.0215 | +0.0241 | 51% |
| 15m | +0.1391 | +0.1020 | +0.0662 | 51% |

Balanced call/put mix in a **rising** market, beating both constant-direction
controls. In the exploratory window the same test was passed in a **falling**
market. The edge is therefore not directional exposure in either regime.

**Descriptive statistics (NOT pre-registered criteria, reported for context):**

| Candidate | t | p | Total R |
|---|---|---|---|
| 5m | 4.49 | 6.99e-06 | +397.9 |
| 15m | 4.79 | 1.67e-06 | +217.3 |

**Comparison to the exploratory window:**

| Candidate | Exploratory dev | Exploratory holdout | **Confirmatory (untouched)** |
|---|---|---|---|
| 5m | +0.1118 | +0.0025 | **+0.0744** |
| 15m | +0.1568 | +0.0534 | **+0.1391** |

The confirmatory result sits *between* the dev and holdout figures and is far
above the holdout that triggered the original negative conclusion. The
exploratory holdout (the most recent 20%, BTC −21%) now looks like an unusually
hard sub-period rather than proof of no edge.

**This does not change the deployment recommendation.** Per §5, a single
confirmatory pass on one instrument over one historical window is not
sufficient basis for risking capital. Size remains zero pending forward
validation on the recorded book data. The limitations in §6 — above all that a
2026-07-25 option-chain calibration is being applied to 2024–2025 options —
bound how much this result can be trusted.

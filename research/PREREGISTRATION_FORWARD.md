# Pre-Registration — Forward Validation on Recorded Book Data

**Written:** 2026-07-27
**Book data in existence at time of writing:** 82 snapshots, **0.14 days of
effective coverage** (0.5% of the 30-day requirement below), verified by
`research/check_recorder.py`.

This document is written now, while there is essentially no data, precisely so
the protocol cannot be shaped by looking at results. Nothing in the criteria
below could have been chosen to fit an outcome, because no outcome exists yet.

---

## 1. What this test is for

`STUDY_2026-07-25.md` §14 recorded a confirmed hypothesis with one unresolved
caveat, stated in advance and asymmetric **against** the result:

> The option overlay is calibrated to a 2026-07-25 chain and applied to 2024–25
> options. These strategies **buy** premium. If options were dearer then than
> the snapshot implies, the overlay underprices what the strategy would have
> paid and expectancy is overstated.

That caveat cannot be settled with historical data — Delta purges option chains
~2 days after expiry, so there is no 2024 chain left to calibrate against.

**This test removes the overlay entirely.** Options are priced from the actual
recorded bid/ask book. If the edge survives without a modelled premium, the
caveat is answered. If it does not, the caveat was the edge.

This is the deployment decision point. Nothing before it is.

---

## 2. Hypothesis (unchanged, single, pre-specified)

> `mean_reversion_bollinger`, ungated, trading 1–3 day ATM options, has positive
> expectancy net of realistic Delta option costs, when premiums are taken from
> the observed book rather than a model.

Identical to the confirmed hypothesis. It is **not** re-derived, re-fitted, or
re-selected from the historical results.

---

## 3. What will be tested — exactly two candidates

| # | Family | Timeframe | DTE band | Gates | Params |
|---|---|---|---|---|---|
| 1 | `mean_reversion_bollinger` | 5m | 1–3d | none | `vec.DEFAULTS`, unchanged |
| 2 | `mean_reversion_bollinger` | 15m | 1–3d | none | `vec.DEFAULTS`, unchanged |

**No other candidate will be run against this data.** Not other families,
timeframes, DTE bands, gates, or parameter values. If a variant is run against
this dataset, the dataset is spent and this pre-registration is void.

---

## 4. Data requirements — the test may not run until ALL are met

Checked with `research/check_recorder.py`, which reports **effective coverage**
(sum of intervals under 30 minutes), not wall-clock span:

| Requirement | Threshold | Why |
|---|---|---|
| Effective coverage | **≥ 30.0 days** | Below this the trade count cannot support a verdict |
| Largest single outage | **≤ 24 hours** | A longer hole means an unrepresentative sample of regimes |
| Total outage time | **≤ 15% of span** | Guards against a Swiss-cheese dataset that *sums* to 30 days |
| Snapshots | **≥ 6,000** | ~300s cadence over 30 days, allowing for loss |

**Wall-clock span is explicitly NOT a criterion.** A recorder that ran one hour,
died for two days and restarted shows a 2-day "span" and ~1 hour of coverage.
This project has already been misled once by exactly that distinction.

If the requirements are not met, the test does not run. Collection continues.
There is no partial-credit version of this test.

---

## 5. Success criteria (fixed now)

A candidate **passes** only if, over the full collected window:

1. **≥ 200 trades**, and
2. **Expectancy > 0** net of the measured cost model, priced from the recorded
   book (bid to enter, bid to exit — the side that actually fills), and
3. It **beats its own constant-direction control** (`drift_control.assess`).

**The hypothesis is confirmed forward** only if at least one candidate passes
all three.

Criterion 2 uses the **conservative** fill side throughout. Entering at the ask
and exiting at the bid is what a taker actually pays; anything friendlier is a
modelling favour and is not permitted here.

---

## 6. Failure handling — declared in advance

If both candidates fail:

- Recorded as a **failed forward validation**.
- The historical confirmation in §14 is then attributed to the overlay caveat
  in §1 — i.e. the edge was an artifact of underpriced modelled premium.
- **Position size remains zero permanently** for this hypothesis. It will have
  failed the only test that could have settled it.
- **No re-slicing** into favourable sub-periods.
- **No parameter adjustment.**
- **No "collect more and retry"** unless a *specific, pre-stated* defect in the
  collection is identified (e.g. a recorder bug corrupting quotes) — not merely
  because the result was disappointing.

---

## 7. If it passes — sizing is still not automatic

A forward pass is strong evidence, not a mandate. Recommended action on a pass:

1. Size at **no more than one quarter of quarter-Kelly** initially — the edge
   estimate carries the uncertainty of a single forward window.
2. Paper-trade live for a further 30 days with the engine's own execution path,
   which tests slippage, latency and partial fills that no backtest models.
3. Only then consider capital, and only at the drawdown-capped size from
   `sizing.recommend`.

The gap between "positive expectancy in a replayed book" and "positive
expectancy through a real order router" is where most backtested edges die.

---

## 8. Known limitations of this test

- **Single instrument** (BTC) and a **single forward window**.
- The recorded book is a **snapshot every ~300s**, not a full order-book feed.
  Intra-interval moves are invisible; fills are assumed at the nearest snapshot,
  which is optimistic on fast moves.
- Quote sizes are not recorded, so **fill feasibility for larger size is
  untested**. Results describe a small taker.
- ~30 days spans few volatility regimes. A pass is not evidence of robustness
  across regimes; the historical tests carry that weight.

---

## 9. Reproducibility

- Data: `research/book/book_BTC_*.jsonl.gz`, collected from 2026-07-27 onward
- Health gate: `research/check_recorder.py` (exit 0 required before running)
- Cost model: `min(0.01% notional, 3.5% premium) × 1.18`
- **No overlay. No Black-76. No IV model.** Premiums come from recorded quotes.
- Code: to be written as `research/forward.py`, run **once**

---

## 10. RESULT

*Not yet run. Blocked on §4 data requirements — currently 0.14 of 30.0 days.*

# Backtest → Loss Analysis → Retune Study

**Date:** 2026-07-21
**Data:** BTCUSD 1m (129,600 bars) + 5m (25,920 bars), 90 days, Delta Exchange India
**Split:** in-sample days 0–60 (Phases 2–3) · out-of-sample days 60–90 (Phase 4 only)

---

## Verdict

**No strategy is deployable. Seven of eight are CONCEPT_FLAWED. Nothing was retuned, because nothing earned a retune.**

274 parameter combinations were tested. **Zero** produced positive gross expectancy in-sample. The two strategies that showed a positive gross edge under a corrected fill model fail a raw t-test (p = 0.079 and p = 0.903) and are annihilated by multiple-comparison correction (Bonferroni p = 1.000 for both).

Separately, the study surfaced a **methodological defect in the prescribed fill model** that had to be corrected before any conclusion about the signals was possible. Both results are reported.

---

## Data integrity

| | 1m | 5m |
|---|---|---|
| bars | 129,600 | 25,920 |
| completeness | **100.00%** | **100.00%** |
| NaN / duplicate ts / gaps > 5 min | 0 / 0 / 0 | 0 / 0 / 0 |
| bad OHLC bars | 0 | 0 |
| span | 2026-04-22 → 2026-07-21 | same |

Fetched via paginated `/v2/history/candles` (65 + 13 calls, 0.1 s spacing, seconds-based timestamps, 2000-bar pages).

### Harness validation

The grid search cannot call the production `evaluate()` 274 times over 129k bars, so numpy replicas were built — then **proved equivalent** before being trusted:

| strategy | bars sampled | agreement |
|---|---:|---:|
| all 7 directional strategies | 1,200 each | **100.00%** |

The first run caught two genuine replica bugs (a cluster-indexing error in `traffic_light`, and `swingking_sniper` diverging because production feeds a 50-period EMA only a 120-bar window — too short to converge). Both were fixed; without this check the entire study would have been invalid.

---

## PHASE 1 — Initial backtest (prescribed fill model, 90 days)

| Strategy | Trades | Win% | Loss% | Gross R | Net R | PF | Status |
|---|---:|---:|---:|---:|---:|---:|:--|
| ema_cross | 5,755 | 3.3 | 96.7 | −0.813 | −3.739 | 0.003 | FAIL |
| scalping_pulse | 23,813 | 2.3 | 97.7 | −0.828 | −3.749 | 0.001 | FAIL |
| traffic_light | 18,058 | 3.0 | 97.0 | −0.822 | −3.747 | 0.003 | FAIL |
| inside_candle | 802 | 19.7 | 80.3 | −0.404 | −1.407 | 0.135 | FAIL |
| mean_reversion_bollinger | 7,380 | 4.7 | 95.3 | −0.796 | −3.686 | 0.006 | FAIL |
| prime_scalper_ema | 7,658 | 3.2 | 96.8 | −0.816 | −3.763 | 0.003 | FAIL |
| swingking_sniper | 1,663 | 17.7 | 82.3 | −0.453 | −1.500 | 0.117 | FAIL |
| booming_bulls_supertrend¹ | 5,511 | 34.4 | 65.6 | −0.135 | −0.244 | 0.667 | MARGINAL |

**8 of 8 flagged for Phase 2.**

¹ Trades a *simulated* premium series — see Assumptions.

**The 2–5% win rates were treated as a red flag, not a result.** A 1 : 1.2 bracket on a random walk resolves to the stop ~55% of the time, i.e. a ~45% win rate. Observing 3% means something other than the market is deciding the outcome.

---

## PHASE 2 — Deep loss analysis (in-sample only)

### D — Parameter grids: the headline number

| strategy | combos | positive **net** | positive **gross** |
|---|---:|---:|---:|
| ema_cross | 25 | 0 | **0** |
| scalping_pulse | 9 | 0 | **0** |
| traffic_light | 3 | 0 | **0** |
| inside_candle | 20 | 0 | **0** |
| mean_reversion_bollinger | 16 | 0 | **0** |
| prime_scalper_ema | 48 | 0 | **0** |
| swingking_sniper | 9 | 0 | **0** |
| booming_bulls_supertrend | 144 | 0 | **0** |
| **total** | **274** | **0** | **0** |

### A — Loss clustering by regime

The **prescribed absolute thresholds are degenerate at these timeframes.** `ATR/spot > 0.015` (high vol) and `> 0.008` (filter A) select **zero** 1m bars — BTC 1m ATR is ~0.0005 of spot, 16–30× smaller than the cutoffs. Every bar tags as "low vol" and "ranging", carrying no information. Self-calibrating percentile equivalents were added alongside, and those do discriminate:

`mean_reversion_bollinger`, in-sample:

| regime | trades | loss% | gross R |
|---|---:|---:|---:|
| bottom-quintile vol | 1,020 | **100.0** | **−1.000** |
| top-quintile vol | 864 | 73.3 | −0.331 |

**1,020 consecutive trades, 100% losses, gross exactly −1.000R** — every single one stopped out. A stop that is *never* survived is not a signal problem; it is a stop placed inside the noise.

### E — MFE/MAE

Across the 1m strategies, **79–81% of losing trades never moved even 0.1 R in the intended direction.** Losers averaged MFE 0.18–0.20 R against MAE 1.88–2.10 R. The trades were not going right and then reversing — they went wrong immediately.

### F — Regime filters

The self-calibrating volatility gate was the only lever with real effect:

| strategy | baseline gross | top-quintile-vol gross | improvement |
|---|---:|---:|---:|
| mean_reversion_bollinger | −0.798 | −0.331 | **+0.467** |
| traffic_light | −0.830 | −0.454 | **+0.376** |

Large, reproducible — and still negative. The gate works by *arithmetic*, not prediction: cost in R is `cost / (stop_atr × ATR)`, so raising ATR shrinks cost in R. It improves the trade's arithmetic, not the signal's accuracy.

### B / C — Time of day, signal quality

**Zero hours** of the 24 showed positive gross R for any strategy. Best discriminator found anywhere was candle-body size at the 90th percentile, which lifted gross from −0.82 to −0.54 — still deeply negative.

---

## The control experiments (not requested, but required)

The 3% win rate could not be explained by any signal defect, so four controls were run.

**Control 1 — scale check**

| tf | median ATR(14) | prescribed 0.05% slippage | slip / ATR |
|---|---:|---:|---:|
| **1m** | 35.76 USD | 38.33 USD | **1.07** |
| 5m | 96.42 USD | 38.33 USD | 0.40 |

**The prescribed entry slippage exceeds one full ATR at 1m.** The fill is displaced by a whole stop-width before the trade starts, placing the stop at roughly the prevailing market price, where ordinary intrabar noise takes it out. BTCUSD ticks at 0.5 with a 0.5–2.0 spread — 0.05% is **65.6 ticks**, roughly 50× a realistic market-order cost.

**Control 2 — random entries under the same model:** 1m random gives win 3.5%, gross −0.813. The real strategies give win 2–5%, gross −0.78 to −0.84. **Statistically indistinguishable.**

**Control 4 — the harness is not broken:**

| slippage | stop | random-entry gross R |
|---|---|---:|
| 0.050% | 1 ATR | −0.794 |
| 0.010% | 1 ATR | −0.209 |
| **0.000%** | 1 ATR | **−0.013** |
| **0.000%** | 3 ATR | **+0.009** |

With slippage removed, random entries return **~0.00 gross**, exactly as random-walk theory requires. The simulator is correctly specified; the prescribed slippage parameter was the dominant term.

**Control 3 — direction shuffle (matched comparison, same fill model both sides):**

| strategy | real gross | direction-shuffled gross | delta |
|---|---:|---:|---:|
| ema_cross | −0.8182 | −0.8198 | +0.002 |
| traffic_light | −0.8296 | −0.8305 | +0.001 |
| mean_reversion_bollinger | −0.7978 | −0.8206 | +0.023 |
| inside_candle | −0.4000 | −0.4384 | +0.038 |
| swingking_sniper | −0.4576 | −0.4466 | −0.011 |

**Randomising the direction on the same bars changes almost nothing.** This is the single most damaging result in the study, and it is immune to the fill-model objection because both sides pay identical costs.

---

## Re-run under a corrected fill model (1 tick / fill)

| strategy | gross (prescribed) | gross (corrected) | change | WF windows +ve |
|---|---:|---:|---:|:--:|
| ema_cross | −0.8131 | −0.0076 | +0.806 | 1/3 |
| scalping_pulse | −0.8276 | −0.0414 | +0.786 | 0/3 |
| traffic_light | −0.8215 | −0.0228 | +0.799 | 0/3 |
| inside_candle | −0.4043 | −0.0333 | +0.371 | 1/3 |
| **mean_reversion_bollinger** | −0.7957 | **+0.0267** | +0.823 | **3/3** |
| **prime_scalper_ema** | −0.8164 | **+0.0016** | +0.818 | 2/3 |
| swingking_sniper | −0.4526 | −0.0230 | +0.430 | 1/3 |
| booming_bulls_supertrend | −0.1349 | −0.1084 | +0.026 | 0/3 |

Every strategy collapses to ≈ 0 gross — the random-walk null. **The signals are neither predictive nor anti-predictive. They are noise.**

### Are the two positives real? No.

| strategy | trades | mean R | std err | t | raw p | Bonferroni p (282 tests) |
|---|---:|---:|---:|---:|---:|:--|
| mean_reversion_bollinger | 6,546 | +0.0267 | 0.0152 | 1.76 | 0.079 | **1.000 — does not survive** |
| prime_scalper_ema | 7,360 | +0.0016 | 0.0128 | 0.12 | 0.903 | **1.000 — does not survive** |

Neither reaches p < 0.05 even *before* correcting for 282 searched configurations.

---

## PHASE 3 — Retuning decisions

**No strategy was retuned.** Section 8 Rule 8 applies to seven of eight (Phase-1 net < −1.0 R **and** zero grid combos with positive expectancy); `booming_bulls_supertrend` returns NO_EDGE_FOUND (0/144 combos positive).

Retuning a signal that shows no measurable edge under any of 274 parameterizations is curve-fitting by definition — the only thing further search can find is noise that happens to fit these particular 60 days.

**What was delivered instead:** [`strategy/zing_strategies_v2.py`](delta_trading_app/strategy/zing_strategies_v2.py) implements the one validated lever — a self-calibrating volatility gate — as an opt-in mixin over the unmodified originals. Verified as a **strict subset** of v1 (3,000 sampled bars: 543 v1 signals → 132 v2 signals, 411 suppressed, **0 rule mismatches, 0 direction flips**). It is *not* wired into `STRATEGY_CLASSES`. It makes losing strategies lose less; it does not make them profitable.

---

## PHASE 4 — Out-of-sample validation (days 60–90, untouched until now)

| Strategy | Orig 90d net | IS net | **OOS net** | OOS gross | WF +ve | Verdict |
|---|---:|---:|---:|---:|:--:|:--|
| ema_cross | −3.739 | −3.742 | −3.658 | −0.807 | 0/3 | CONCEPT_FLAWED |
| scalping_pulse | −3.749 | −3.825 | −3.596 | −0.810 | 0/3 | CONCEPT_FLAWED |
| traffic_light | −3.747 | −3.811 | −3.615 | −0.805 | 0/3 | CONCEPT_FLAWED |
| inside_candle | −1.407 | −1.381 | −1.410 | −0.372 | 0/3 | CONCEPT_FLAWED |
| mean_reversion_bollinger | −3.686 | −3.622 | −3.403 | −0.776 | 0/3 | CONCEPT_FLAWED |
| prime_scalper_ema | −3.763 | −3.750 | −3.614 | −0.791 | 0/3 | CONCEPT_FLAWED |
| swingking_sniper | −1.500 | −1.487 | −1.426 | −0.415 | 0/3 | CONCEPT_FLAWED |
| booming_bulls_supertrend | −0.244 | −0.116 | −0.133 | −0.013 | 1/3 | MARGINAL |

**Overfitting warnings: none.** In-sample and out-of-sample agree to within ~0.1 R for every strategy. There was no in-sample gain to overfit *to* — which is itself the finding.

---

## The arithmetic that settles it

Independent of any signal question:

```
round-trip taker fee alone   = 0.118% of notional = 77.36 USD
median ATR(14), 1m           = 34.80 USD   ->  fee = 2.22 R per trade
median ATR(14), 5m           = 93.29 USD   ->  fee = 0.83 R per trade

To net +0.1 R at 1m with a 1-ATR stop, gross edge must exceed  2.32 R/trade.
Best gross edge measured anywhere in this study:               0.027 R/trade.
Shortfall:                                                     86x
```

For fees to cost ≤ 0.10 R, the stop must be ≥ 774 USD — **22× the 1m ATR, 8.3× the 5m ATR**. That is a swing-trading stop, not a scalping stop.

**One-minute mean-reversion scalping is arithmetically impossible on Delta's fee schedule, regardless of signal quality.** This holds before slippage, before the option bid/ask spread, and before theta.

---

## Assumptions (stated per Rule 5)

1. **Option premiums are simulated, not measured.** Delta serves no historical option ticks. `booming_bulls_supertrend` runs on a synthetic series: `intrinsic + spot × IV × √T × 0.4`, IV = trailing realized vol, strike re-struck daily on the 200-wide grid. It omits the bid/ask spread (~15% on testnet), smile/skew, IV crush, and the variance risk premium. **Every omission biases optimistic.**
2. The premium test applies the *futures* fee (0.05%). Delta's option fee is `min(0.03% notional, 3.5% premium) + GST`, which on an ATM contract is ≈ **8% of premium round-trip** — far worse. `booming_bulls`'s −0.24 R is therefore an optimistic bound.
3. The 7 directional strategies are tested on the underlying with ATR-sized stops. This measures **signal edge**, a necessary condition for the option trade. It does not model theta or premium spread.
4. Regime thresholds as prescribed are degenerate at 1m; percentile equivalents were substituted and both are reported.
5. One position at a time per strategy; live allows one per (strategy, direction) up to 8 concurrent.

---

## Recommendations

**Deploy to paper trading:** none.
**Do not deploy:** all 8.
**Abandon as concept-flawed:** ema_cross, scalping_pulse, traffic_light, inside_candle, mean_reversion_bollinger, prime_scalper_ema, swingking_sniper.
**booming_bulls_supertrend:** MARGINAL on a simulated series with an optimistic fee model. Not evidence of anything.

### If the goal is to find a real edge, the next step is not another retune

1. **Change timeframe, not parameters.** Fees cost 2.22 R at 1m and 0.83 R at 5m. The first question worth answering is what they cost at 1h and 4h. No 1m variant can clear an 86× shortfall.
2. **Stop testing directional signals on the underlying.** Control 3 showed direction is a coin flip across five independent rule families. That is a property of 1m BTC, not of these particular rules.
3. **If options remain the instrument, the edge must come from volatility, not direction** — the existing `/volatility` VRP lab is the right tool, and it already reports VRP ≈ 0.
4. **Keep the harness.** `research/` is reusable and validated: 100% replica equivalence, and a random-entry null that correctly returns 0.00. Any future strategy should be required to clear the null before anything else.

---

## Artifacts

| path | contents |
|---|---|
| `research/data.py` | paginated Delta fetcher + integrity validation |
| `research/vec.py` | vectorized indicators + 8 signal generators |
| `research/equivalence.py` | **replica-vs-production proof (must pass before trusting results)** |
| `research/sim.py` | fill model, cost model, Section 2.5 metrics |
| `research/premium.py` | synthetic ATM option premium series |
| `research/phase1.py` · `phase2.py` · `phase34.py` | the four phases |
| `research/control.py` | random-entry null, direction shuffle, slippage sweep |
| `research/corrected.py` · `significance.py` | corrected-model run, t-tests, Bonferroni |
| `strategy/zing_strategies_v2.py` | volatility-gated variants (opt-in, originals untouched) |
| `research/*.json`, `research/phase2_log.txt` | full numeric output |

Originals in `strategy/zing_strategies.py` were **not modified**.

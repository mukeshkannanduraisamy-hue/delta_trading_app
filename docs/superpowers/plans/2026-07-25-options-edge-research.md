# Options Edge Research & Risk Management Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Search for a BTC options strategy that survives realistic costs under an acceptance bar fixed in advance, and rebuild position sizing so it derives from measured edge instead of a guessed confidence table.

**Architecture:** Keep the proven research core (`sim.py`, `vec.py`, `control.py`, `equivalence.py`, `significance.py`) and generalize only what is hardcoded. Add an option cost overlay calibrated against the ~3-day window of real option data Delta retains, gated on holdout tracking accuracy. Run every candidate through a composable pass/fail gauntlet whose criteria are fixed before the search begins.

**Tech Stack:** Python 3, numpy 2.5.1, stdlib `math`/`sqlite3`, `requests`. pytest for tests (dev-only). No scipy — `significance.py` already uses a stdlib normal approximation; keep it that way.

## Global Constraints

- **NO GIT COMMITS. NO PUSHES.** All changes stay local in the working tree for user review. Every task ends with verification, not a commit.
- Spec: `docs/superpowers/specs/2026-07-25-options-edge-research-design.md`. Read it before starting.
- The live app must not regress: `.venv/Scripts/python.exe verify_audit_fixes.py` must print `ALL CHECKS PASSED` (17 checks) after every task.
- Paper-only. Do not enable, re-implement, or call any authenticated Delta endpoint. All of `delta_client.py`'s authenticated methods stay `raise NotImplementedError`.
- Run Python via `.venv/Scripts/python.exe` from `D:\TRADER\delta_trading_app`. There is no global Python.
- Option premium paths use `MARK:` candles only. Traded option candles (`C-BTC-...`) are single-print and flat — never use them for fills.
- Delta options fee: `min(0.03% × notional, 3.5% × premium) × 1.18`. Read per-product from `/v2/products` where available.
- Entries pay the ask, exits receive the bid. Never mid.
- Every RNG use takes an explicit seed, recorded in the results file.
- Cache all network fetches to `research/cache/`. A rerun must cost zero network calls.

---

### Task 1: Test infrastructure + timeframe resampling

**Files:**
- Create: `requirements-dev.txt`
- Create: `tests/__init__.py`
- Create: `tests/test_resample.py`
- Modify: `research/data.py` (append `resample()`)

**Interfaces:**
- Consumes: `research.data.fetch()`, `research.data.res_seconds()` (existing)
- Produces: `research.data.resample(d: dict, from_res: str, to_res: str) -> dict` returning the same `{time,open,high,low,close,volume}` ndarray dict shape as `fetch()`

- [ ] **Step 1: Install pytest**

Create `requirements-dev.txt`:

```
pytest==8.3.4
```

Run: `.venv/Scripts/python.exe -m pip install -r requirements-dev.txt`
Expected: `Successfully installed pytest-8.3.4` (plus iniconfig/pluggy)

- [ ] **Step 2: Write the failing test**

Create `tests/__init__.py` (empty file).

Create `tests/test_resample.py`:

```python
import numpy as np
import pytest

from research import data


def _series(n, start=0, step=60):
    """n bars of synthetic 1m OHLCV with a known shape."""
    t = np.arange(start, start + n * step, step, dtype="int64")
    close = np.arange(1, n + 1, dtype="float64") * 10.0
    return {
        "time": t,
        "open": close - 2.0,
        "high": close + 5.0,
        "low": close - 5.0,
        "close": close,
        "volume": np.ones(n, dtype="float64"),
    }


def test_resample_aggregates_ohlcv_correctly():
    d = _series(10)
    out = data.resample(d, "1m", "5m")
    assert len(out["time"]) == 2
    # bucket 0 = bars 0..4, bucket 1 = bars 5..9
    assert out["open"][0] == d["open"][0]
    assert out["close"][0] == d["close"][4]
    assert out["high"][0] == d["high"][:5].max()
    assert out["low"][0] == d["low"][:5].min()
    assert out["volume"][0] == 5.0
    assert out["time"][0] == d["time"][0]


def test_resample_drops_incomplete_trailing_bucket():
    d = _series(7)  # 5 complete + 2 leftover
    out = data.resample(d, "1m", "5m")
    assert len(out["time"]) == 1, "partial trailing bucket must be dropped"


def test_resample_buckets_align_to_epoch_boundaries():
    # start at 03:02 UTC so the first bar is mid-bucket
    d = _series(20, start=3 * 3600 + 2 * 60)
    out = data.resample(d, "1m", "5m")
    assert np.all(out["time"] % 300 == 0), "bucket starts must align to the resolution grid"


def test_resample_rejects_downsample_to_finer():
    d = _series(10)
    with pytest.raises(ValueError):
        data.resample(d, "5m", "1m")
```

- [ ] **Step 3: Run test to verify it fails**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resample.py -v`
Expected: FAIL — `AttributeError: module 'research.data' has no attribute 'resample'`

- [ ] **Step 4: Implement resample()**

Append to `research/data.py`:

```python
def resample(d: dict, from_res: str, to_res: str) -> dict:
    """Aggregate a finer series into a coarser one.

    Higher timeframes are derived from ONE 1m fetch rather than fetched
    separately: independent fetches can disagree at bar boundaries, which
    silently introduces look-ahead when a strategy reads two timeframes.

    Buckets are aligned to the epoch grid (floor(t / target) * target) so a
    "1h" bar always starts on the hour, matching what the exchange serves.
    An incomplete trailing bucket is dropped — a partial bar has not closed,
    and the live engine only ever evaluates closed bars.
    """
    src = res_seconds(from_res)
    dst = res_seconds(to_res)
    if dst < src:
        raise ValueError(f"cannot resample {from_res} -> {to_res}: target is finer")
    if dst % src:
        raise ValueError(f"cannot resample {from_res} -> {to_res}: not an integer multiple")
    if dst == src:
        return {k: v.copy() for k, v in d.items()}

    t = d["time"]
    if len(t) == 0:
        return {k: v.copy() for k, v in d.items()}

    bucket = (t // dst) * dst
    # index of the first row of each bucket, in order
    starts = np.flatnonzero(np.r_[True, bucket[1:] != bucket[:-1]])
    ends = np.r_[starts[1:], len(t)]          # exclusive

    expected = dst // src
    complete = (ends - starts) == expected
    starts, ends = starts[complete], ends[complete]

    out = {
        "time": bucket[starts],
        "open": d["open"][starts],
        "close": d["close"][ends - 1],
        "high": np.array([d["high"][a:b].max() for a, b in zip(starts, ends)], dtype="float64"),
        "low": np.array([d["low"][a:b].min() for a, b in zip(starts, ends)], dtype="float64"),
        "volume": np.array([d["volume"][a:b].sum() for a, b in zip(starts, ends)], dtype="float64"),
    }
    return out
```

- [ ] **Step 5: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_resample.py -v`
Expected: 4 passed

- [ ] **Step 6: Verify resampled 5m matches Delta's native 5m**

Create `tests/test_resample_vs_native.py`:

```python
"""Resampled 5m must track Delta's own 5m. A divergence means the bucket
alignment is wrong and every multi-timeframe result would be suspect."""
import numpy as np
import pytest

from research import data


def test_resampled_5m_matches_native_5m():
    d1 = data.fetch("BTCUSD", "1m", 90)
    d5 = data.fetch("BTCUSD", "5m", 90)
    got = data.resample(d1, "1m", "5m")

    common, i_got, i_nat = np.intersect1d(got["time"], d5["time"], return_indices=True)
    assert len(common) > 1000, f"too few overlapping bars to compare: {len(common)}"

    for field in ("open", "high", "low", "close"):
        a, b = got[field][i_got], d5[field][i_nat]
        mismatch = np.abs(a - b) > 0.51          # BTCUSD ticks at 0.5
        rate = mismatch.mean()
        assert rate < 0.02, f"{field}: {rate:.2%} of resampled bars diverge from native"
```

Run: `.venv/Scripts/python.exe -m pytest tests/test_resample_vs_native.py -v -s`
Expected: PASS. If it fails, the bucket alignment is wrong — fix before continuing; every later result depends on it.

- [ ] **Step 7: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.** Leave changes in the working tree.

---

### Task 2: Cost models — protocol, Delta option fees, Black-Scholes core

**Files:**
- Create: `research/costs.py`
- Create: `tests/test_costs.py`

**Interfaces:**
- Consumes: nothing from earlier tasks
- Produces:
  - `research.costs.CostModel` — protocol with `round_trip_cost(entry: float, exit_: float, **kw) -> float`
  - `research.costs.PerpCost(fee_pct=0.0005, gst=0.18, slip_pct=...)`
  - `research.costs.OptionCost(notional_rate=0.0003, premium_cap=0.035, gst=0.18)` with
    `fee(premium: float, spot: float, contract_value: float, contracts: int) -> float`
  - `research.costs.bs_price(S, K, T, sigma, is_call, r=0.0) -> float`
  - `research.costs.bs_greeks(S, K, T, sigma, is_call, r=0.0) -> dict` with keys `delta gamma theta vega`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_costs.py`:

```python
import math

import pytest

from research import costs


# ---------- Delta options fee ----------

def test_option_fee_takes_the_cheaper_of_notional_and_premium_cap():
    c = costs.OptionCost()
    # cheap option: 3.5% of premium is far less than 0.03% of notional
    fee = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=1)
    notional_leg = 0.0003 * 64000.0 * 0.001 * 1     # 0.0192
    premium_leg = 0.035 * 10.0 * 0.001 * 1          # 0.00035
    assert fee == pytest.approx(min(notional_leg, premium_leg) * 1.18)


def test_option_fee_uses_notional_leg_when_premium_is_large():
    c = costs.OptionCost()
    fee = c.fee(premium=100000.0, spot=64000.0, contract_value=0.001, contracts=1)
    notional_leg = 0.0003 * 64000.0 * 0.001 * 1
    assert fee == pytest.approx(notional_leg * 1.18)


def test_option_fee_scales_with_contracts():
    c = costs.OptionCost()
    one = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=1)
    ten = c.fee(premium=10.0, spot=64000.0, contract_value=0.001, contracts=10)
    assert ten == pytest.approx(one * 10)


def test_option_fee_matches_live_app_config():
    """The study's fee must equal what strategy/config.py charges, or the
    backtest is not measuring the app that will trade."""
    from strategy import config
    c = costs.OptionCost(
        notional_rate=config.FEE_NOTIONAL_RATE,
        premium_cap=config.FEE_PREMIUM_CAP,
        gst=config.GST_RATE,
    )
    fee = c.fee(premium=250.0, spot=64000.0, contract_value=0.001, contracts=3)
    expected = min(
        config.FEE_NOTIONAL_RATE * 64000.0 * 0.001 * 3,
        config.FEE_PREMIUM_CAP * 250.0 * 0.001 * 3,
    ) * (1 + config.GST_RATE)
    assert fee == pytest.approx(expected)


# ---------- Black-Scholes ----------

def test_bs_atm_call_known_value():
    # S=K=100, T=1, sigma=20%, r=0  ->  ~7.966
    p = costs.bs_price(100.0, 100.0, 1.0, 0.20, is_call=True)
    assert p == pytest.approx(7.9656, abs=1e-3)


def test_bs_put_call_parity():
    S, K, T, sig = 64000.0, 63000.0, 0.05, 0.55
    call = costs.bs_price(S, K, T, sig, is_call=True)
    put = costs.bs_price(S, K, T, sig, is_call=False)
    assert call - put == pytest.approx(S - K, abs=1e-6)


def test_bs_intrinsic_at_expiry():
    assert costs.bs_price(64000.0, 63000.0, 0.0, 0.5, is_call=True) == pytest.approx(1000.0)
    assert costs.bs_price(62000.0, 63000.0, 0.0, 0.5, is_call=True) == pytest.approx(0.0)
    assert costs.bs_price(62000.0, 63000.0, 0.0, 0.5, is_call=False) == pytest.approx(1000.0)


def test_bs_delta_matches_finite_difference():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    g = costs.bs_greeks(S, K, T, sig, is_call=True)
    h = 1.0
    fd = (costs.bs_price(S + h, K, T, sig, True) - costs.bs_price(S - h, K, T, sig, True)) / (2 * h)
    assert g["delta"] == pytest.approx(fd, abs=1e-4)


def test_bs_theta_is_negative_for_long_options():
    g = costs.bs_greeks(64000.0, 64000.0, 0.05, 0.55, is_call=True)
    assert g["theta"] < 0, "a long option must lose value as time passes"


def test_bs_vega_matches_finite_difference():
    S, K, T, sig = 64000.0, 64000.0, 0.05, 0.55
    g = costs.bs_greeks(S, K, T, sig, is_call=True)
    h = 1e-4
    fd = (costs.bs_price(S, K, T, sig + h, True) - costs.bs_price(S, K, T, sig - h, True)) / (2 * h)
    assert g["vega"] == pytest.approx(fd / 100.0, rel=1e-3)  # vega per 1 vol point
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_costs.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.costs'`

- [ ] **Step 3: Implement costs.py**

Create `research/costs.py`:

```python
"""Cost models and the Black-Scholes pricing core.

WHY A PROTOCOL
--------------
The study has to price two different instruments with different fee
structures (perp taker vs. Delta options min-of-two-legs). Making the cost
model injectable means `sim.py` stays one fill model instead of growing an
`if instrument == ...` branch, and a new structure (maker rebates, a
different venue) is added as a class rather than by editing existing code.

BLACK-SCHOLES
-------------
Replaces the `spot * IV * sqrt(T) * 0.4` approximation in premium.py, which
produces no usable greeks. r = 0 for crypto: there is no risk-free leg in a
BTC-margined option, and Delta's options are inverse-settled.
"""

from __future__ import annotations

import math
from typing import Protocol


# --------------------------------------------------------------------------
# Black-Scholes
# --------------------------------------------------------------------------

def _norm_cdf(x: float) -> float:
    return 0.5 * math.erfc(-x / math.sqrt(2.0))


def _norm_pdf(x: float) -> float:
    return math.exp(-0.5 * x * x) / math.sqrt(2.0 * math.pi)


def _d1_d2(S: float, K: float, T: float, sigma: float, r: float):
    v = sigma * math.sqrt(T)
    d1 = (math.log(S / K) + (r + 0.5 * sigma * sigma) * T) / v
    return d1, d1 - v


def bs_price(S: float, K: float, T: float, sigma: float,
             is_call: bool, r: float = 0.0) -> float:
    """European option price. T in YEARS. Returns intrinsic at T<=0."""
    if S <= 0 or K <= 0:
        return 0.0
    if T <= 0 or sigma <= 0:
        return max(S - K, 0.0) if is_call else max(K - S, 0.0)
    d1, d2 = _d1_d2(S, K, T, sigma, r)
    disc = math.exp(-r * T)
    if is_call:
        return S * _norm_cdf(d1) - K * disc * _norm_cdf(d2)
    return K * disc * _norm_cdf(-d2) - S * _norm_cdf(-d1)


def bs_greeks(S: float, K: float, T: float, sigma: float,
              is_call: bool, r: float = 0.0) -> dict:
    """delta (per 1 unit spot), gamma, theta (per DAY), vega (per 1 vol POINT)."""
    if T <= 0 or sigma <= 0 or S <= 0 or K <= 0:
        intrinsic_delta = (1.0 if S > K else 0.0) if is_call else (-1.0 if S < K else 0.0)
        return {"delta": intrinsic_delta, "gamma": 0.0, "theta": 0.0, "vega": 0.0}

    d1, d2 = _d1_d2(S, K, T, sigma, r)
    disc = math.exp(-r * T)
    pdf = _norm_pdf(d1)
    sqrtT = math.sqrt(T)

    delta = _norm_cdf(d1) if is_call else _norm_cdf(d1) - 1.0
    gamma = pdf / (S * sigma * sqrtT)
    vega = S * pdf * sqrtT / 100.0                      # per 1 vol point
    term = -(S * pdf * sigma) / (2.0 * sqrtT)
    if is_call:
        theta = (term - r * K * disc * _norm_cdf(d2)) / 365.0
    else:
        theta = (term + r * K * disc * _norm_cdf(-d2)) / 365.0
    return {"delta": delta, "gamma": gamma, "theta": theta, "vega": vega}


# --------------------------------------------------------------------------
# Cost models
# --------------------------------------------------------------------------

class CostModel(Protocol):
    """Per-unit round-trip cost of one trade, in price units of the traded
    instrument. Slippage is baked into fill prices by the caller; this covers
    the explicit fee legs."""

    def round_trip_cost(self, entry: float, exit_: float, **kw) -> float:
        ...


class PerpCost:
    """BTCUSD perpetual: flat taker fee + GST on each side."""

    def __init__(self, fee_pct: float = 0.0005, gst: float = 0.18):
        self.fee_pct = fee_pct
        self.gst = gst

    def round_trip_cost(self, entry: float, exit_: float, **kw) -> float:
        k = self.fee_pct * (1.0 + self.gst)
        return entry * k + exit_ * k


class OptionCost:
    """Delta India options: min(notional leg, premium leg), plus GST.

    fee = min(notional_rate * spot * cv * n, premium_cap * premium * cv * n) * (1 + gst)

    Both legs scale by contract_value and contract count, so the min() is
    taken on the ALREADY-SCALED legs — taking it on per-unit rates and
    scaling afterwards gives the same answer only when cv and n are equal on
    both legs, which is fragile. Keep it explicit.
    """

    def __init__(self, notional_rate: float = 0.0003,
                 premium_cap: float = 0.035, gst: float = 0.18):
        self.notional_rate = notional_rate
        self.premium_cap = premium_cap
        self.gst = gst

    def fee(self, premium: float, spot: float,
            contract_value: float, contracts: int) -> float:
        notional_leg = self.notional_rate * spot * contract_value * contracts
        premium_leg = self.premium_cap * premium * contract_value * contracts
        return min(notional_leg, premium_leg) * (1.0 + self.gst)

    def round_trip_cost(self, entry: float, exit_: float, *,
                        spot_in: float, spot_out: float,
                        contract_value: float = 1.0, contracts: int = 1,
                        **kw) -> float:
        return (self.fee(entry, spot_in, contract_value, contracts)
                + self.fee(exit_, spot_out, contract_value, contracts))
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_costs.py -v`
Expected: 10 passed

- [ ] **Step 5: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 3: Option data fetch and cache (`optdata.py`)

**Files:**
- Create: `research/optdata.py`
- Create: `tests/test_optdata.py`

**Interfaces:**
- Consumes: `research.data._get` pattern (rate-limit handling), `research.data.CACHE`
- Produces:
  - `research.optdata.parse_symbol(sym: str) -> dict` with keys `kind` (`C`/`P`), `asset`, `strike` (float), `expiry` (unix seconds at 12:00 UTC)
  - `research.optdata.live_chain(asset='BTC') -> list[dict]`
  - `research.optdata.mark_candles(symbol, resolution, days) -> dict` (same ndarray shape as `data.fetch`)
  - `research.optdata.snapshot_spreads(asset='BTC') -> list[dict]` with keys `symbol moneyness dte_hours mid spread_pct`
  - `research.optdata.recent_real_contracts(asset='BTC', min_bars=500) -> list[str]`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_optdata.py`:

```python
import calendar

import pytest

from research import optdata


def test_parse_symbol_extracts_all_fields():
    got = optdata.parse_symbol("C-BTC-63000-270726")
    assert got["kind"] == "C"
    assert got["asset"] == "BTC"
    assert got["strike"] == 63000.0
    # DDMMYY 270726 -> 27 July 2026, settling 12:00 UTC
    assert got["expiry"] == calendar.timegm((2026, 7, 27, 12, 0, 0, 0, 0, 0))


def test_parse_symbol_handles_puts():
    assert optdata.parse_symbol("P-BTC-58000-010126")["kind"] == "P"


def test_parse_symbol_rejects_malformed():
    for bad in ("BTCUSD", "C-BTC-63000", "C-BTC-abc-270726", "C-BTC-63000-2707261"):
        with pytest.raises(ValueError):
            optdata.parse_symbol(bad)


def test_parse_symbol_rejects_impossible_date():
    with pytest.raises(ValueError):
        optdata.parse_symbol("C-BTC-63000-321326")   # day 32, month 13


def test_expiry_is_noon_utc():
    """Settlement at 12:00 UTC was confirmed empirically: the final mark
    candles on an expired contract are 11:58 and 11:59."""
    e = optdata.parse_symbol("C-BTC-63000-270726")["expiry"]
    import time
    assert time.gmtime(e).tm_hour == 12
    assert time.gmtime(e).tm_min == 0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_optdata.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.optdata'`

- [ ] **Step 3: Implement optdata.py**

Create `research/optdata.py`:

```python
"""Option chain and option candle access.

RETENTION CONSTRAINT (measured 2026-07-25)
------------------------------------------
Delta keeps option candle history for roughly TWO DAYS past expiry. Mapping
all 46 expired BTC expiries: 1-day-old -> 1440 bars, 2-day -> 1441,
3-day -> 1-2, June expiries -> 0. A multi-month backtest on real option data
is therefore impossible; the real window is only ever ~3 days wide. That is
what forces the calibrated-overlay design in calibrate.py.

TRADED VS MARK
--------------
Traded candles (`C-BTC-...`) are single-print and flat: a real response is
OHLC 1197/1197/1197/1197 with volume 5. Using them for fills manufactures
phantom edge — this is the artifact that inflated the earlier options study.
Only `MARK:` candles are used for premium paths.
"""

from __future__ import annotations

import calendar
import re
import time
from pathlib import Path

import numpy as np
import requests

from research.data import CACHE, PROD, _get, res_seconds

SETTLEMENT_HOUR_UTC = 12
_SYM = re.compile(r"^([CP])-([A-Z]+)-(\d+(?:\.\d+)?)-(\d{6})$")


def parse_symbol(sym: str) -> dict:
    """`C-BTC-63000-270726` -> kind/asset/strike/expiry(unix, 12:00 UTC).

    The DDMMYY token is validated as a real calendar date rather than
    slice-and-trust: a silently wrong expiry would corrupt every DTE, theta
    and settlement calculation downstream.
    """
    m = _SYM.match(sym or "")
    if not m:
        raise ValueError(f"not an option symbol: {sym!r}")
    kind, asset, strike, ddmmyy = m.groups()
    dd, mm, yy = int(ddmmyy[0:2]), int(ddmmyy[2:4]), int(ddmmyy[4:6])
    if not (1 <= mm <= 12 and 1 <= dd <= 31):
        raise ValueError(f"impossible expiry date in {sym!r}: {ddmmyy}")
    year = 2000 + yy
    try:
        expiry = calendar.timegm((year, mm, dd, SETTLEMENT_HOUR_UTC, 0, 0, 0, 0, 0))
        # timegm normalizes overflow (e.g. 31 Feb); reject it
        got = time.gmtime(expiry)
        if (got.tm_mday, got.tm_mon) != (dd, mm):
            raise ValueError
    except (ValueError, OverflowError):
        raise ValueError(f"impossible expiry date in {sym!r}: {ddmmyy}")
    return {"kind": kind, "asset": asset, "strike": float(strike), "expiry": expiry}


def live_chain(asset: str = "BTC") -> list[dict]:
    """Every live option product for `asset`, with strike/cv/tick attached."""
    out = []
    for ct in ("call_options", "put_options"):
        r = requests.get(f"{PROD}/v2/products",
                         params={"contract_types": ct, "states": "live",
                                 "page_size": 500},
                         timeout=(3.05, 30))
        r.raise_for_status()
        for p in (r.json().get("result") or []):
            if (p.get("underlying_asset") or {}).get("symbol") != asset:
                continue
            try:
                meta = parse_symbol(p["symbol"])
            except ValueError:
                continue
            cv = float(p.get("contract_value") or 0)
            if cv <= 0:
                continue          # refuse to size against a missing contract_value
            out.append({**meta, "symbol": p["symbol"], "contract_value": cv,
                        "tick_size": float(p.get("tick_size") or 0)})
        time.sleep(0.1)
    return out


def mark_candles(symbol: str, resolution: str, days: float,
                 refresh: bool = False) -> dict:
    """MARK candles for one option contract, cached like data.fetch()."""
    safe = symbol.replace(":", "_")
    cache = CACHE / f"MARK_{safe}_{resolution}_{days:g}d.npz"
    if cache.exists() and not refresh:
        z = np.load(cache)
        return {k: z[k] for k in z.files}

    now = int(time.time())
    rows = _get({"symbol": f"MARK:{symbol}", "resolution": resolution,
                 "start": now - int(days * 86400), "end": now})
    rows = sorted((c for c in rows if c.get("time") is not None),
                  key=lambda c: int(c["time"]))
    d = {
        "time": np.array([int(c["time"]) for c in rows], dtype="int64"),
        "open": np.array([float(c["open"]) for c in rows], dtype="float64"),
        "high": np.array([float(c["high"]) for c in rows], dtype="float64"),
        "low": np.array([float(c["low"]) for c in rows], dtype="float64"),
        "close": np.array([float(c["close"]) for c in rows], dtype="float64"),
        "volume": np.zeros(len(rows), dtype="float64"),   # MARK carries no volume
    }
    np.savez_compressed(cache, **d)
    return d


def snapshot_spreads(asset: str = "BTC") -> list[dict]:
    """One live bid/ask sample of the whole chain.

    The spread is the dominant cost in options and the old premium.py model
    omitted it entirely. Sampling the live book is the only way to measure it
    — it is not recoverable from candles.
    """
    r = requests.get(f"{PROD}/v2/tickers",
                     params={"contract_types": "call_options,put_options"},
                     timeout=(3.05, 30))
    r.raise_for_status()
    out = []
    for t in (r.json().get("result") or []):
        sym = t.get("symbol") or ""
        try:
            meta = parse_symbol(sym)
        except ValueError:
            continue
        if meta["asset"] != asset:
            continue
        try:
            bid = float(t.get("quotes", {}).get("best_bid") or 0)
            ask = float(t.get("quotes", {}).get("best_ask") or 0)
            spot = float(t.get("spot_price") or 0)
        except (TypeError, ValueError):
            continue
        if bid <= 0 or ask <= 0 or ask < bid or spot <= 0:
            continue
        mid = 0.5 * (bid + ask)
        out.append({
            "symbol": sym,
            "moneyness": meta["strike"] / spot,
            "dte_hours": max(0.0, (meta["expiry"] - time.time()) / 3600.0),
            "mid": mid,
            "spread_pct": (ask - bid) / mid,
        })
    return out


def recent_real_contracts(asset: str = "BTC", min_bars: int = 500,
                          resolution: str = "1m") -> list[str]:
    """Live + recently-expired contracts that still carry usable MARK history.

    This is the calibration window. It is small by construction (see the
    retention note above) and callers must treat it as such.
    """
    syms = [c["symbol"] for c in live_chain(asset)]
    for ct in ("call_options", "put_options"):
        r = requests.get(f"{PROD}/v2/products",
                         params={"contract_types": ct, "states": "expired",
                                 "page_size": 200},
                         timeout=(3.05, 30))
        r.raise_for_status()
        for p in (r.json().get("result") or []):
            if (p.get("underlying_asset") or {}).get("symbol") == asset:
                syms.append(p["symbol"])
        time.sleep(0.1)

    keep = []
    for s in dict.fromkeys(syms):
        try:
            d = mark_candles(s, resolution, 5)
        except Exception:
            continue
        if len(d["time"]) >= min_bars:
            keep.append(s)
        time.sleep(0.05)
    return keep
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_optdata.py -v`
Expected: 5 passed

- [ ] **Step 5: Populate the option cache (network)**

Run:

```bash
.venv/Scripts/python.exe -c "from research import optdata; import json; s=optdata.recent_real_contracts(); print(len(s),'contracts with usable MARK history'); print(json.dumps(s[:10],indent=1))"
```

Expected: a non-empty list. Record the count — it is the width of the calibration window and gets reported in the final study.

- [ ] **Step 6: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 4: Calibration and the option overlay, with the credibility gate

**Files:**
- Create: `research/calibrate.py`
- Create: `tests/test_calibrate.py`

**Interfaces:**
- Consumes: `research.costs.bs_price`, `research.costs.bs_greeks`, `research.optdata.*`, `research.premium.realized_vol`
- Produces:
  - `research.calibrate.IVModel(a: float, b: float, floor: float, cap: float)` with `iv(rv: float, dte_hours: float, moneyness: float) -> float`
  - `research.calibrate.SpreadModel(buckets: dict)` with `spread_pct(moneyness: float, dte_hours: float) -> float`
  - `research.calibrate.fit_iv(samples) -> tuple[IVModel, dict]` (model, fit report)
  - `research.calibrate.fit_spread(snapshots) -> SpreadModel`
  - `research.calibrate.Overlay(iv_model, spread_model, cost_model)` with
    `premium_path(spot, K, expiry_ts, times, is_call) -> np.ndarray`
  - `research.calibrate.validate_overlay(overlay, symbol) -> dict` with keys
    `mae_pct rmse_pct r2_changes n_bars passed`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_calibrate.py`:

```python
import numpy as np
import pytest

from research import calibrate, costs


def test_fit_iv_recovers_known_linear_relationship():
    """If IV really is a + b*RV, the fit must find a and b."""
    rng = np.random.default_rng(7)
    rv = rng.uniform(0.3, 1.2, 400)
    iv = 0.15 + 0.85 * rv + rng.normal(0, 0.005, 400)
    samples = [{"rv": r, "iv": i, "dte_hours": 24.0, "moneyness": 1.0}
               for r, i in zip(rv, iv)]
    model, report = calibrate.fit_iv(samples)
    assert model.a == pytest.approx(0.15, abs=0.02)
    assert model.b == pytest.approx(0.85, abs=0.05)
    assert report["r2"] > 0.95


def test_iv_model_never_returns_below_floor():
    m = calibrate.IVModel(a=-1.0, b=0.0, floor=0.15, cap=3.0)
    assert m.iv(rv=0.5, dte_hours=24.0, moneyness=1.0) == 0.15


def test_iv_model_never_returns_above_cap():
    m = calibrate.IVModel(a=99.0, b=0.0, floor=0.15, cap=3.0)
    assert m.iv(rv=0.5, dte_hours=24.0, moneyness=1.0) == 3.0


def test_fit_iv_refuses_insufficient_data():
    """Parsimony: do not fit a model the data cannot support."""
    with pytest.raises(ValueError):
        calibrate.fit_iv([{"rv": 0.5, "iv": 0.6, "dte_hours": 24.0, "moneyness": 1.0}])


def test_spread_model_falls_back_to_global_median_for_unseen_bucket():
    snaps = [{"moneyness": 1.0, "dte_hours": 24.0, "spread_pct": 0.10},
             {"moneyness": 1.0, "dte_hours": 24.0, "spread_pct": 0.20}]
    m = calibrate.fit_spread(snaps)
    # a bucket never observed must still return a usable, non-zero spread
    got = m.spread_pct(moneyness=0.5, dte_hours=999.0)
    assert got > 0


def test_spread_model_is_pessimistic_on_thin_evidence():
    """With few samples in a bucket the model must not report a tighter
    spread than the global median - thin evidence should not look cheap."""
    snaps = ([{"moneyness": 1.0, "dte_hours": 24.0, "spread_pct": 0.30}] * 50
             + [{"moneyness": 2.0, "dte_hours": 24.0, "spread_pct": 0.01}])
    m = calibrate.fit_spread(snaps)
    assert m.spread_pct(2.0, 24.0) >= 0.30 * 0.5


def test_overlay_reproduces_black_scholes_when_iv_is_known():
    iv = calibrate.IVModel(a=0.55, b=0.0, floor=0.01, cap=3.0)
    sp = calibrate.SpreadModel(buckets={}, fallback=0.0)
    ov = calibrate.Overlay(iv, sp, costs.OptionCost())

    expiry = 1_800_000_000
    times = np.array([expiry - 86400], dtype="int64")
    spot = np.array([64000.0])
    got = ov.premium_path(spot, 64000.0, expiry, times, is_call=True)
    want = costs.bs_price(64000.0, 64000.0, 1.0 / 365.0, 0.55, True)
    assert got[0] == pytest.approx(want, rel=1e-6)


def test_overlay_premium_decays_to_intrinsic_at_expiry():
    iv = calibrate.IVModel(a=0.55, b=0.0, floor=0.01, cap=3.0)
    ov = calibrate.Overlay(iv, calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    expiry = 1_800_000_000
    times = np.array([expiry], dtype="int64")
    got = ov.premium_path(np.array([65000.0]), 64000.0, expiry, times, True)
    assert got[0] == pytest.approx(1000.0, abs=1e-6)


def test_validate_overlay_reports_failure_rather_than_raising():
    """The gate must return a verdict the caller can act on, not explode."""
    iv = calibrate.IVModel(a=0.01, b=0.0, floor=0.01, cap=3.0)   # deliberately wrong
    ov = calibrate.Overlay(iv, calibrate.SpreadModel({}, 0.0), costs.OptionCost())
    report = calibrate.validate_overlay(ov, symbol=None, _fake=_bad_fixture())
    assert report["passed"] is False
    assert "mae_pct" in report


def _bad_fixture():
    """(spot, mark, times, K, expiry, is_call) that no sane IV can fit."""
    n = 600
    expiry = 1_800_000_000
    times = np.arange(expiry - n * 60, expiry, 60, dtype="int64")
    spot = np.full(n, 64000.0)
    mark = np.full(n, 5000.0)         # absurd premium for a 1-day ATM option
    return spot, mark, times, 64000.0, expiry, True
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calibrate.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.calibrate'`

- [ ] **Step 3: Implement calibrate.py**

Create `research/calibrate.py`. Key design points to honour:

- `fit_iv` requires >= 30 samples, else `raise ValueError` (parsimony gate).
- `IVModel.iv()` clamps to `[floor, cap]` and, per the parsimony requirement,
  applies term/skew adjustments **only if** `fit_iv` found they improved
  holdout error; otherwise both are flat.
- `fit_spread` buckets by (moneyness band, DTE band), uses the **median** per
  bucket, and for any bucket with < 10 samples returns
  `max(bucket_median, 0.5 * global_median)` so thin evidence never looks cheap.
- `Overlay.premium_path` computes, per bar: `T = (expiry - t) / (365*86400)`,
  `rv` from `premium.realized_vol`, `sigma = iv_model.iv(rv, dte, moneyness)`,
  then `costs.bs_price(...)`. Vectorized over the time axis.
- `validate_overlay` accepts an optional `_fake` tuple so the failure path is
  testable without network. Computes MAE%, RMSE% on levels and R² on
  first-differences; `passed = (mae_pct <= 0.25) and (r2_changes >= 0.50)`.
  Those thresholds are the stated band from the spec — record them in the
  report dict so the final study prints them.

Full implementation:

```python
"""Fit the option overlay to real option data, and prove it tracks.

WHY THIS EXISTS
---------------
Delta retains option candles ~2 days past expiry (see optdata.py), so a
365-day option backtest cannot use real option prices. The overlay maps a
real spot path to a premium path instead. That is only legitimate if the
mapping is shown to reproduce real option prices on data it was not fitted
on - which is what validate_overlay() is for.

PARSIMONY
---------
The calibration window is ~3 days wide. Model complexity is gated on
demonstrated holdout improvement: the default IV shape is FLAT in both term
and skew, and structure is added only where it measurably helps. Ties break
toward the more pessimistic fit.
"""

from __future__ import annotations

import math

import numpy as np

from research import costs, premium

MIN_IV_SAMPLES = 30
MIN_BUCKET_SAMPLES = 10
MAE_PCT_LIMIT = 0.25        # overlay must track real marks within 25%
R2_CHANGES_LIMIT = 0.50     # and explain half the variance of premium CHANGES


class IVModel:
    """IV as a function of realized vol, optionally of DTE and moneyness."""

    def __init__(self, a: float, b: float, floor: float = 0.15,
                 cap: float = 3.0, term: dict | None = None,
                 skew: dict | None = None):
        self.a, self.b = a, b
        self.floor, self.cap = floor, cap
        self.term = term or {}
        self.skew = skew or {}

    def iv(self, rv: float, dte_hours: float, moneyness: float) -> float:
        v = self.a + self.b * rv
        if self.term:
            v *= self.term.get(_dte_band(dte_hours), 1.0)
        if self.skew:
            v *= self.skew.get(_money_band(moneyness), 1.0)
        return float(min(max(v, self.floor), self.cap))

    def iv_array(self, rv: np.ndarray, dte_hours: np.ndarray,
                 moneyness: np.ndarray) -> np.ndarray:
        v = self.a + self.b * rv
        if self.term:
            v = v * np.array([self.term.get(_dte_band(h), 1.0) for h in dte_hours])
        if self.skew:
            v = v * np.array([self.skew.get(_money_band(m), 1.0) for m in moneyness])
        return np.clip(v, self.floor, self.cap)


def _dte_band(h: float) -> str:
    if h <= 6:
        return "0-6h"
    if h <= 24:
        return "6-24h"
    if h <= 72:
        return "1-3d"
    return "3d+"


def _money_band(m: float) -> str:
    if m < 0.97:
        return "itm"
    if m <= 1.03:
        return "atm"
    return "otm"


def fit_iv(samples: list[dict]) -> tuple[IVModel, dict]:
    """OLS of iv on rv. Refuses to fit on too little data."""
    if len(samples) < MIN_IV_SAMPLES:
        raise ValueError(
            f"need >= {MIN_IV_SAMPLES} IV samples to fit, got {len(samples)}. "
            "Fitting fewer would produce a model the data cannot support.")
    rv = np.array([s["rv"] for s in samples], dtype="float64")
    iv = np.array([s["iv"] for s in samples], dtype="float64")
    ok = np.isfinite(rv) & np.isfinite(iv) & (rv > 0) & (iv > 0)
    rv, iv = rv[ok], iv[ok]
    if len(rv) < MIN_IV_SAMPLES:
        raise ValueError(f"only {len(rv)} finite IV samples after cleaning")

    b, a = np.polyfit(rv, iv, 1)
    pred = a + b * rv
    ss_res = float(np.sum((iv - pred) ** 2))
    ss_tot = float(np.sum((iv - iv.mean()) ** 2))
    r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    report = {"a": float(a), "b": float(b), "r2": float(r2), "n": int(len(rv)),
              "iv_mean": float(iv.mean()), "rv_mean": float(rv.mean()),
              "term_fitted": False, "skew_fitted": False}
    return IVModel(a=float(a), b=float(b)), report


class SpreadModel:
    def __init__(self, buckets: dict, fallback: float):
        self.buckets = buckets
        self.fallback = fallback

    def spread_pct(self, moneyness: float, dte_hours: float) -> float:
        return float(self.buckets.get((_money_band(moneyness), _dte_band(dte_hours)),
                                      self.fallback))


def fit_spread(snapshots: list[dict]) -> SpreadModel:
    """Median spread per (moneyness, DTE) bucket.

    A bucket with thin evidence must not look CHEAP, so any bucket under
    MIN_BUCKET_SAMPLES is floored at half the global median.
    """
    if not snapshots:
        return SpreadModel({}, 0.15)
    allv = np.array([s["spread_pct"] for s in snapshots], dtype="float64")
    allv = allv[np.isfinite(allv) & (allv >= 0)]
    global_med = float(np.median(allv)) if len(allv) else 0.15

    grouped: dict = {}
    for s in snapshots:
        key = (_money_band(s["moneyness"]), _dte_band(s["dte_hours"]))
        grouped.setdefault(key, []).append(s["spread_pct"])

    buckets = {}
    for key, vals in grouped.items():
        med = float(np.median(vals))
        buckets[key] = med if len(vals) >= MIN_BUCKET_SAMPLES else max(med, 0.5 * global_med)
    return SpreadModel(buckets, global_med)


class Overlay:
    """Maps a real spot path to a premium path."""

    def __init__(self, iv_model: IVModel, spread_model: SpreadModel,
                 cost_model: costs.OptionCost, rv_window: int = 1440,
                 bar_seconds: int = 60):
        self.iv_model = iv_model
        self.spread_model = spread_model
        self.cost_model = cost_model
        self.rv_window = rv_window
        self.bar_seconds = bar_seconds

    def premium_path(self, spot: np.ndarray, K: float, expiry_ts: int,
                     times: np.ndarray, is_call: bool,
                     rv: np.ndarray | None = None) -> np.ndarray:
        spot = np.asarray(spot, dtype="float64")
        times = np.asarray(times, dtype="int64")
        if rv is None:
            bars_per_year = 365.0 * 86400.0 / self.bar_seconds
            rv = premium.realized_vol(spot, min(self.rv_window, max(2, len(spot) - 1)),
                                      bars_per_year)
        rv = np.nan_to_num(np.asarray(rv, dtype="float64"), nan=0.5)

        secs_left = np.maximum(expiry_ts - times, 0).astype("float64")
        T = secs_left / (365.0 * 86400.0)
        dte_hours = secs_left / 3600.0
        moneyness = K / np.maximum(spot, 1e-9)
        sigma = self.iv_model.iv_array(rv, dte_hours, moneyness)

        out = np.empty(len(spot), dtype="float64")
        for i in range(len(spot)):
            out[i] = costs.bs_price(spot[i], K, T[i], sigma[i], is_call)
        return out

    def bid_ask(self, premium_mid: np.ndarray, K: float, expiry_ts: int,
                times: np.ndarray, spot: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
        """Entries pay the ask, exits receive the bid. Never mid."""
        secs_left = np.maximum(expiry_ts - np.asarray(times), 0).astype("float64")
        half = np.array([
            0.5 * self.spread_model.spread_pct(K / max(s, 1e-9), h / 3600.0)
            for s, h in zip(np.asarray(spot), secs_left)
        ])
        return premium_mid * (1.0 - half), premium_mid * (1.0 + half)


def validate_overlay(overlay: Overlay, symbol: str | None,
                     _fake: tuple | None = None) -> dict:
    """Compare the overlay against REAL mark prices it was not fitted on.

    Returns a verdict dict rather than raising, so the caller can report a
    failed gate and stop cleanly instead of crashing mid-study.
    """
    if _fake is not None:
        spot, mark, times, K, expiry, is_call = _fake
    else:
        from research import data, optdata
        meta = optdata.parse_symbol(symbol)
        m = optdata.mark_candles(symbol, "1m", 5)
        if len(m["time"]) < 100:
            return {"passed": False, "reason": "insufficient real mark history",
                    "n_bars": int(len(m["time"])), "mae_pct": float("nan"),
                    "rmse_pct": float("nan"), "r2_changes": float("nan"),
                    "symbol": symbol}
        spot_series = data.fetch("BTCUSD", "1m", 365)
        idx = np.searchsorted(spot_series["time"], m["time"])
        idx = np.clip(idx, 0, len(spot_series["time"]) - 1)
        ok = spot_series["time"][idx] == m["time"]
        if ok.sum() < 100:
            return {"passed": False, "reason": "spot/mark timestamps do not align",
                    "n_bars": int(ok.sum()), "mae_pct": float("nan"),
                    "rmse_pct": float("nan"), "r2_changes": float("nan"),
                    "symbol": symbol}
        times = m["time"][ok]
        mark = m["close"][ok]
        spot = spot_series["close"][idx[ok]]
        K, expiry, is_call = meta["strike"], meta["expiry"], meta["kind"] == "C"

    pred = overlay.premium_path(spot, K, expiry, times, is_call)
    denom = np.maximum(np.abs(mark), 1e-9)
    mae_pct = float(np.mean(np.abs(pred - mark) / denom))
    rmse_pct = float(math.sqrt(np.mean(((pred - mark) / denom) ** 2)))

    dm, dp = np.diff(mark), np.diff(pred)
    if len(dm) > 2 and np.std(dm) > 0:
        ss_res = float(np.sum((dm - dp) ** 2))
        ss_tot = float(np.sum((dm - dm.mean()) ** 2))
        r2 = 1.0 - ss_res / ss_tot if ss_tot > 0 else 0.0
    else:
        r2 = float("nan")

    passed = bool(mae_pct <= MAE_PCT_LIMIT and
                  (r2 >= R2_CHANGES_LIMIT if np.isfinite(r2) else False))
    return {"symbol": symbol, "n_bars": int(len(mark)), "mae_pct": mae_pct,
            "rmse_pct": rmse_pct, "r2_changes": float(r2), "passed": passed,
            "mae_limit": MAE_PCT_LIMIT, "r2_limit": R2_CHANGES_LIMIT}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_calibrate.py -v`
Expected: 9 passed

- [ ] **Step 5: Run the real calibration and the credibility gate**

Create `research/run_calibration.py`:

```python
"""Fit the overlay to real option data and report whether it tracks."""
import json
import time

import numpy as np

from research import calibrate, costs, data, optdata, premium


def main():
    print("collecting live spread snapshots ...")
    snaps = optdata.snapshot_spreads("BTC")
    print(f"  {len(snaps)} quotes")
    spread_model = calibrate.fit_spread(snaps)

    print("collecting IV samples from live tickers ...")
    import requests
    r = requests.get(f"{optdata.PROD}/v2/tickers",
                     params={"contract_types": "call_options,put_options"},
                     timeout=(3.05, 30))
    d1 = data.fetch("BTCUSD", "1m", 365)
    rv_series = premium.realized_vol(d1["close"], 1440, 365 * 24 * 60)
    rv_now = float(np.nan_to_num(rv_series[-1], nan=0.5))

    samples = []
    for t in (r.json().get("result") or []):
        try:
            meta = optdata.parse_symbol(t.get("symbol") or "")
        except ValueError:
            continue
        if meta["asset"] != "BTC":
            continue
        iv = t.get("mark_iv") or (t.get("quotes") or {}).get("mark_iv")
        try:
            iv = float(iv)
        except (TypeError, ValueError):
            continue
        if not (0 < iv < 5):
            continue
        spot = float(t.get("spot_price") or 0)
        if spot <= 0:
            continue
        samples.append({"rv": rv_now, "iv": iv,
                        "dte_hours": max(0.0, (meta["expiry"] - time.time()) / 3600),
                        "moneyness": meta["strike"] / spot})
    print(f"  {len(samples)} IV samples")

    iv_model, iv_report = calibrate.fit_iv(samples)
    print(json.dumps(iv_report, indent=2))

    overlay = calibrate.Overlay(iv_model, spread_model, costs.OptionCost())

    print("\nvalidating overlay against REAL mark prices ...")
    contracts = optdata.recent_real_contracts("BTC", min_bars=500)
    reports = [calibrate.validate_overlay(overlay, s) for s in contracts[:20]]
    ok = [r for r in reports if r["passed"]]
    for rep in reports:
        print(f"  {rep['symbol']:26s} n={rep['n_bars']:5d} "
              f"mae={rep['mae_pct']:.3f} r2d={rep['r2_changes']:.3f} "
              f"{'PASS' if rep['passed'] else 'FAIL'}")
    print(f"\n  {len(ok)}/{len(reports)} contracts pass the tracking gate")

    out = {"iv_report": iv_report, "n_spread_quotes": len(snaps),
           "spread_buckets": {str(k): v for k, v in spread_model.buckets.items()},
           "overlay_validation": reports,
           "gate_passed": len(ok) >= max(1, len(reports) // 2)}
    with open("research/calibration_results.json", "w", encoding="utf-8") as fh:
        json.dump(out, fh, indent=2, default=str)
    print("\nwrote research/calibration_results.json")
    print("GATE:", "PASSED" if out["gate_passed"] else "FAILED - do not proceed to the search")


if __name__ == "__main__":
    main()
```

Run: `.venv/Scripts/python.exe -m research.run_calibration`

Expected: prints the IV fit, per-contract tracking, and a final `GATE: PASSED` or `GATE: FAILED`.

**If the gate FAILS, stop and report to the user.** Do not continue to Task 5+ with an overlay that cannot reproduce real option prices — that is the failure mode the spec explicitly designs against.

- [ ] **Step 6: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 5: Make `sim.py` cost model injectable

**Files:**
- Modify: `research/sim.py`
- Create: `tests/test_sim_costs.py`

**Interfaces:**
- Consumes: `research.costs.CostModel`
- Produces: `sim.simulate(..., cost_model: CostModel | None = None)`. When
  `cost_model is None`, behaviour is byte-identical to today (module-level
  `SLIP_PCT`/`FEE_PCT`/`GST_RATE`), so every existing result stays reproducible.

- [ ] **Step 1: Write the failing test**

Create `tests/test_sim_costs.py`:

```python
import numpy as np
import pytest

from research import costs, sim


def _trending(n=400):
    t = np.arange(0, n * 60, 60, dtype="int64")
    c = 64000.0 + np.arange(n, dtype="float64") * 2.0
    return {"time": t, "open": c - 1, "high": c + 8, "low": c - 8,
            "close": c, "volume": np.ones(n)}


def test_default_cost_path_is_unchanged():
    """Passing no cost model must reproduce the legacy numbers exactly."""
    d = _trending()
    sig = np.zeros(len(d["close"]), dtype="int8")
    sig[50:300:25] = 1
    a = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30)
    b = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30, cost_model=None)
    assert [t["net_r"] for t in a["trades"]] == [t["net_r"] for t in b["trades"]]


def test_injected_zero_cost_model_beats_default():
    d = _trending()
    sig = np.zeros(len(d["close"]), dtype="int8")
    sig[50:300:25] = 1

    class Free:
        def round_trip_cost(self, entry, exit_, **kw):
            return 0.0

    default = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30)
    free = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30, cost_model=Free())
    dm = np.mean([t["net_r"] for t in default["trades"]])
    fm = np.mean([t["net_r"] for t in free["trades"]])
    assert fm > dm, "removing fees must improve net expectancy"


def test_injected_perp_cost_matches_legacy_constants():
    d = _trending()
    sig = np.zeros(len(d["close"]), dtype="int8")
    sig[50:300:25] = 1
    legacy = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30)
    injected = sim.simulate(d, sig, stop_atr=1.0, rr=1.5, max_hold=30,
                            cost_model=costs.PerpCost(fee_pct=sim.FEE_PCT,
                                                      gst=sim.GST_RATE))
    for x, y in zip(legacy["trades"], injected["trades"]):
        assert x["net_r"] == pytest.approx(y["net_r"], rel=1e-12)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sim_costs.py -v`
Expected: FAIL — `TypeError: simulate() got an unexpected keyword argument 'cost_model'`

- [ ] **Step 3: Modify sim.py**

In `research/sim.py`, add `cost_model` to the `simulate()` signature and route
the fee computation through it. Replace the direct `_round_trip_cost(...)` call
inside the trade loop with:

```python
        cost = (_round_trip_cost(entry, exit_px) if cost_model is None
                else cost_model.round_trip_cost(entry, exit_px))
```

and add to the signature:

```python
def simulate(d: dict, signals: np.ndarray, *,
             stop_atr: float = 1.0, rr: float = 1.5,
             atr_period: int = 14, max_hold: int = 30,
             long_only: bool = False,
             atr_override: np.ndarray | None = None,
             cost_model=None) -> dict:
```

Add to the module docstring, under COSTS:

```
  * cost_model (optional) overrides the module-level fee constants. Passing
    None preserves the legacy perp model exactly, so every result produced
    before this parameter existed remains reproducible.
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sim_costs.py -v`
Expected: 3 passed

- [ ] **Step 5: Re-run the harness null — the critical check**

The harness self-check must still hold after touching the fill model.

Run: `.venv/Scripts/python.exe -m research.control`
Expected: random entries at zero slippage return gross expectancy ~0.00.
If this has drifted, the change to `sim.py` broke the fill model — fix before continuing.

- [ ] **Step 6: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 6: New signal families

**Files:**
- Create: `research/families.py`
- Create: `tests/test_families.py`

**Interfaces:**
- Consumes: `research.vec` (existing indicator helpers: `atr`, `ema`, `sma`)
- Produces, each returning an `int8` array of `-1/0/+1` the same length as input:
  - `families.momentum_persistence(d, lookback=12, persist=3) -> np.ndarray`
  - `families.breakout_volume(d, channel=20, vol_z=1.5) -> np.ndarray`
  - Gates, each returning a `bool` mask:
  - `families.vol_regime_gate(d, atr_period=14, lo_pct=30, hi_pct=70) -> np.ndarray`
  - `families.session_gate(d, hours=(13,14,15,16)) -> np.ndarray`
  - `families.htf_trend_gate(d, htf_close, fast=20, slow=50) -> np.ndarray`
  - `families.vrp_gate(iv, rv, max_ratio=1.5) -> np.ndarray`
  - `families.apply_gates(signals, *gates) -> np.ndarray`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_families.py`:

```python
import numpy as np
import pytest

from research import families


def _d(close, volume=None, start=0):
    close = np.asarray(close, dtype="float64")
    n = len(close)
    return {"time": np.arange(start, start + n * 60, 60, dtype="int64"),
            "open": close, "high": close + 1, "low": close - 1,
            "close": close, "volume": (np.ones(n) if volume is None
                                       else np.asarray(volume, dtype="float64"))}


def test_momentum_persistence_fires_long_on_sustained_uptrend():
    d = _d(np.arange(100, dtype="float64") + 1000.0)
    sig = families.momentum_persistence(d, lookback=12, persist=3)
    assert sig[-1] == 1


def test_momentum_persistence_fires_short_on_sustained_downtrend():
    d = _d(2000.0 - np.arange(100, dtype="float64"))
    sig = families.momentum_persistence(d, lookback=12, persist=3)
    assert sig[-1] == -1


def test_momentum_persistence_silent_on_flat():
    d = _d(np.full(100, 1000.0))
    sig = families.momentum_persistence(d, lookback=12, persist=3)
    assert np.all(sig == 0)


def test_momentum_persistence_has_no_lookahead():
    """Changing a FUTURE bar must not change a PAST signal."""
    c = np.r_[np.arange(60, dtype="float64"), np.arange(60, 120)[::-1].astype("float64")]
    base = families.momentum_persistence(_d(c), 12, 3)
    c2 = c.copy()
    c2[80:] += 500.0
    mod = families.momentum_persistence(_d(c2), 12, 3)
    assert np.array_equal(base[:80], mod[:80])


def test_breakout_volume_requires_volume_confirmation():
    c = np.r_[np.full(40, 1000.0), np.full(10, 1050.0)]
    quiet = families.breakout_volume(_d(c, volume=np.ones(50)), channel=20, vol_z=1.5)
    loud_vol = np.ones(50)
    loud_vol[40:] = 50.0
    loud = families.breakout_volume(_d(c, volume=loud_vol), channel=20, vol_z=1.5)
    assert loud.sum() > quiet.sum(), "volume spike must enable a breakout the quiet case suppresses"


def test_vol_regime_gate_excludes_extremes():
    rng = np.random.default_rng(3)
    c = 1000.0 + np.cumsum(rng.normal(0, 1, 500))
    gate = families.vol_regime_gate(_d(c), atr_period=14, lo_pct=30, hi_pct=70)
    frac = gate[50:].mean()
    assert 0.2 < frac < 0.7, f"mid-regime gate should admit a middling fraction, got {frac:.2f}"


def test_session_gate_matches_utc_hours():
    # start at 00:00 UTC, 1 bar per minute for 24h
    d = _d(np.full(1440, 1000.0), start=0)
    gate = families.session_gate(d, hours=(13, 14))
    hrs = (d["time"] // 3600) % 24
    assert np.array_equal(gate, np.isin(hrs, (13, 14)))


def test_vrp_gate_blocks_when_iv_far_above_rv():
    iv = np.array([1.0, 1.0, 1.0])
    rv = np.array([0.9, 0.5, 0.2])
    gate = families.vrp_gate(iv, rv, max_ratio=1.5)
    assert gate[0] and not gate[2], "buying premium at IV/RV=5 must be blocked"


def test_apply_gates_is_an_and_of_all_masks():
    sig = np.array([1, -1, 1, -1], dtype="int8")
    g1 = np.array([True, True, False, False])
    g2 = np.array([True, False, True, False])
    out = families.apply_gates(sig, g1, g2)
    assert np.array_equal(out, np.array([1, 0, 0, 0], dtype="int8"))


def test_apply_gates_never_creates_a_signal():
    sig = np.zeros(4, dtype="int8")
    out = families.apply_gates(sig, np.ones(4, dtype=bool))
    assert np.all(out == 0)
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_families.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.families'`

- [ ] **Step 3: Implement families.py**

Create `research/families.py`. Requirements every function must satisfy:

- **No look-ahead.** Every value at index `i` uses only data at `<= i`. Use
  `np.r_[np.full(k, fill), arr[:-k]]`-style shifts, never a centred window.
- Warm-up region returns `0` (signals) or `False` (gates), never a guess.
- Gates return `bool` arrays; entries return `int8` in `{-1,0,1}`.

```python
"""Additional signal families and gates.

WHY MOSTLY GATES
----------------
The existing 8 strategies have ~0 gross edge, which means their entries are
coin flips. Another coin flip does not help. What does help is taking fewer
flips in unfavourable conditions: a gate that cuts trade count 70% while
leaving gross edge flat removes most of the cost bleed, which is a larger
effect than any realistic entry improvement.

NO LOOK-AHEAD
-------------
Every value at index i is computed from data at index <= i. Warm-up regions
emit 0/False rather than a guess.
"""

from __future__ import annotations

import numpy as np

from research.vec import atr as _atr


def _shift(a: np.ndarray, k: int, fill=np.nan) -> np.ndarray:
    out = np.full(len(a), fill, dtype="float64")
    if k < len(a):
        out[k:] = a[:len(a) - k]
    return out


def momentum_persistence(d: dict, lookback: int = 12,
                         persist: int = 3) -> np.ndarray:
    """Enter with N-bar momentum once its sign has held `persist` bars.

    Trend-following amortizes a fixed per-trade cost over a larger move,
    which is the structural reason to look above 1m.
    """
    c = d["close"]
    n = len(c)
    sig = np.zeros(n, dtype="int8")
    if n <= lookback + persist:
        return sig
    mom = c - _shift(c, lookback)
    s = np.sign(np.nan_to_num(mom, nan=0.0))
    held = np.ones(n, dtype=bool)
    for k in range(persist):
        held &= (np.r_[np.zeros(k, dtype="int8"), s[:n - k]] == s) if k else True
    valid = np.zeros(n, dtype=bool)
    valid[lookback + persist:] = True
    sig[valid & held & (s > 0)] = 1
    sig[valid & held & (s < 0)] = -1
    return sig


def breakout_volume(d: dict, channel: int = 20,
                    vol_z: float = 1.5) -> np.ndarray:
    """Donchian break confirmed by a volume z-score.

    The existing breakout strategies (traffic_light, inside_candle) ignore
    volume entirely; volume is the standard false-breakout filter.
    """
    c, v = d["close"], d["volume"]
    n = len(c)
    sig = np.zeros(n, dtype="int8")
    if n <= channel + 2:
        return sig

    hi = np.full(n, np.nan)
    lo = np.full(n, np.nan)
    for i in range(channel, n):
        w = c[i - channel:i]                 # STRICTLY prior bars
        hi[i], lo[i] = w.max(), w.min()

    vm = np.full(n, np.nan)
    vs = np.full(n, np.nan)
    for i in range(channel, n):
        w = v[i - channel:i]
        vm[i], vs[i] = w.mean(), w.std()
    z = np.where(vs > 0, (v - vm) / np.where(vs > 0, vs, 1.0), 0.0)

    ok = np.isfinite(hi) & np.isfinite(lo) & (z >= vol_z)
    sig[ok & (c > hi)] = 1
    sig[ok & (c < lo)] = -1
    return sig


def vol_regime_gate(d: dict, atr_period: int = 14,
                    lo_pct: float = 30, hi_pct: float = 70) -> np.ndarray:
    """Admit only bars whose trailing ATR percentile sits in a middle band.

    Percentile rank is computed against PRIOR bars only.
    """
    a = _atr(d["high"], d["low"], d["close"], atr_period)
    n = len(a)
    gate = np.zeros(n, dtype=bool)
    warm = atr_period * 4
    for i in range(warm, n):
        w = a[:i]
        w = w[np.isfinite(w)]
        if len(w) < warm // 2 or not np.isfinite(a[i]):
            continue
        pr = 100.0 * (w < a[i]).mean()
        gate[i] = lo_pct <= pr <= hi_pct
    return gate


def session_gate(d: dict, hours: tuple = (13, 14, 15, 16)) -> np.ndarray:
    """Admit only bars whose UTC hour is in `hours`."""
    hrs = (d["time"] // 3600) % 24
    return np.isin(hrs, np.asarray(hours))


def htf_trend_gate(d: dict, htf_close: np.ndarray,
                   fast: int = 20, slow: int = 50) -> np.ndarray:
    """Admit bars where the higher-timeframe trend agrees.

    `htf_close` must already be forward-filled onto d's bar grid by the
    caller using only CLOSED higher-timeframe bars.
    """
    from research.vec import ema
    n = len(d["close"])
    if len(htf_close) != n:
        raise ValueError("htf_close must be aligned to d's bar grid")
    f, s = ema(htf_close, fast), ema(htf_close, slow)
    return np.isfinite(f) & np.isfinite(s) & (f > s)


def vrp_gate(iv: np.ndarray, rv: np.ndarray,
             max_ratio: float = 1.5) -> np.ndarray:
    """Block long-premium entries when implied vol is far above realized.

    This is the one genuinely options-native gate: when IV/RV is high you are
    structurally overpaying for the option regardless of direction.
    """
    iv = np.asarray(iv, dtype="float64")
    rv = np.asarray(rv, dtype="float64")
    with np.errstate(divide="ignore", invalid="ignore"):
        ratio = np.where(rv > 0, iv / rv, np.inf)
    return np.isfinite(ratio) & (ratio <= max_ratio)


def apply_gates(signals: np.ndarray, *gates: np.ndarray) -> np.ndarray:
    """AND every gate onto the signal. Gates can only REMOVE signals."""
    out = np.array(signals, dtype="int8", copy=True)
    for g in gates:
        g = np.asarray(g, dtype=bool)
        if len(g) != len(out):
            raise ValueError("gate length does not match signal length")
        out[~g] = 0
    return out
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_families.py -v`
Expected: 10 passed

- [ ] **Step 5: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 7: The gauntlet

**Files:**
- Create: `research/gauntlet.py`
- Create: `tests/test_gauntlet.py`

**Interfaces:**
- Consumes: `research.control.random_entries`, `research.sim.simulate`
- Produces:
  - `gauntlet.Criterion` protocol: `name: str`, `check(ctx: dict) -> tuple[bool, dict]`
  - Concrete: `MinTrades(200)`, `PositiveExpectancy()`, `WalkForward(min_positive=3, windows=4)`, `ShuffleControl(p=0.05, seed=...)`, `CorrectedSignificance(p=0.05)`, `OverlayBand()`, `FinalHoldout()`
  - `gauntlet.DEFAULT_CRITERIA` — the ordered list from the spec
  - `gauntlet.run(ctx: dict, criteria=None) -> dict` with keys `passed`, `failed_at`, `details`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_gauntlet.py`:

```python
import numpy as np
import pytest

from research import gauntlet


def _ctx(**kw):
    base = {
        "trades": [{"net_r": 0.1} for _ in range(500)],
        "walk_forward": [0.05, 0.03, -0.01, 0.04],
        "shuffle_p": 0.01,
        "corrected_p": 0.02,
        "band_results": [0.02, 0.01, 0.03],
        "holdout_expectancy": 0.04,
        "search_budget": 3000,
    }
    base.update(kw)
    return base


def test_min_trades_fails_below_threshold():
    ok, info = gauntlet.MinTrades(200).check(_ctx(trades=[{"net_r": 0.1}] * 199))
    assert ok is False
    assert info["n"] == 199


def test_min_trades_passes_at_threshold():
    ok, _ = gauntlet.MinTrades(200).check(_ctx(trades=[{"net_r": 0.1}] * 200))
    assert ok is True


def test_positive_expectancy_fails_on_negative():
    ok, _ = gauntlet.PositiveExpectancy().check(_ctx(trades=[{"net_r": -0.5}] * 300))
    assert ok is False


def test_walk_forward_requires_three_of_four():
    assert gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, 0.1, -0.1]))[0] is True
    assert gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, -0.1, -0.1]))[0] is False


def test_walk_forward_treats_none_window_as_failure():
    ok, _ = gauntlet.WalkForward().check(_ctx(walk_forward=[0.1, 0.1, None, None]))
    assert ok is False


def test_shuffle_control_rejects_high_p():
    assert gauntlet.ShuffleControl().check(_ctx(shuffle_p=0.40))[0] is False


def test_corrected_significance_rejects_high_p():
    assert gauntlet.CorrectedSignificance().check(_ctx(corrected_p=0.20))[0] is False


def test_overlay_band_requires_every_perturbation_positive():
    assert gauntlet.OverlayBand().check(_ctx(band_results=[0.02, -0.01, 0.03]))[0] is False
    assert gauntlet.OverlayBand().check(_ctx(band_results=[0.02, 0.01, 0.03]))[0] is True


def test_final_holdout_requires_positive():
    assert gauntlet.FinalHoldout().check(_ctx(holdout_expectancy=-0.01))[0] is False


def test_run_reports_the_first_failing_criterion():
    res = gauntlet.run(_ctx(walk_forward=[0.1, -0.1, -0.1, -0.1]))
    assert res["passed"] is False
    assert res["failed_at"] == "walk_forward"


def test_run_stops_at_first_failure_and_does_not_evaluate_later_criteria():
    res = gauntlet.run(_ctx(trades=[{"net_r": 0.1}] * 10))
    assert res["failed_at"] == "min_trades"
    assert "final_holdout" not in res["details"]


def test_run_passes_a_fully_qualifying_candidate():
    res = gauntlet.run(_ctx())
    assert res["passed"] is True
    assert res["failed_at"] is None
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gauntlet.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.gauntlet'`

- [ ] **Step 3: Implement gauntlet.py**

```python
"""The acceptance bar, fixed BEFORE the search.

Criteria are objects in an ordered list rather than an if-chain so a new
criterion is added by appending a class, and the order (cheapest-first) can
change without touching the driver.

Evaluation SHORT-CIRCUITS at the first failure. That is both a compute
saving and the thing that makes the near-miss report meaningful: every
candidate records exactly which criterion killed it.
"""

from __future__ import annotations

import numpy as np


class MinTrades:
    """Statistical-validity precondition, not an extra hurdle: below this the
    t-test has no power, so a 'pass' would be noise whatever it said."""
    name = "min_trades"

    def __init__(self, n: int = 200):
        self.n = n

    def check(self, ctx: dict):
        got = len(ctx.get("trades") or [])
        return got >= self.n, {"n": got, "required": self.n}


class PositiveExpectancy:
    name = "positive_expectancy"

    def check(self, ctx: dict):
        trades = ctx.get("trades") or []
        if not trades:
            return False, {"expectancy": None}
        e = float(np.mean([t["net_r"] for t in trades]))
        return e > 0, {"expectancy": e}


class WalkForward:
    name = "walk_forward"

    def __init__(self, min_positive: int = 3, windows: int = 4):
        self.min_positive = min_positive
        self.windows = windows

    def check(self, ctx: dict):
        wf = ctx.get("walk_forward") or []
        pos = sum(1 for x in wf if x is not None and x > 0)
        return pos >= self.min_positive, {"positive": pos, "windows": len(wf),
                                          "required": self.min_positive,
                                          "values": wf}


class ShuffleControl:
    name = "shuffle_control"

    def __init__(self, p: float = 0.05):
        self.p = p

    def check(self, ctx: dict):
        got = ctx.get("shuffle_p")
        if got is None:
            return False, {"shuffle_p": None}
        return got < self.p, {"shuffle_p": got, "threshold": self.p}


class CorrectedSignificance:
    name = "corrected_significance"

    def __init__(self, p: float = 0.05):
        self.p = p

    def check(self, ctx: dict):
        got = ctx.get("corrected_p")
        if got is None:
            return False, {"corrected_p": None}
        return got < self.p, {"corrected_p": got, "threshold": self.p,
                              "search_budget": ctx.get("search_budget")}


class OverlayBand:
    """Must stay positive across the overlay's measured error band, not just
    at the point estimate."""
    name = "overlay_band"

    def check(self, ctx: dict):
        vals = ctx.get("band_results") or []
        if not vals:
            return False, {"band_results": []}
        return all(v > 0 for v in vals), {"band_results": vals,
                                          "min": float(min(vals))}


class FinalHoldout:
    name = "final_holdout"

    def check(self, ctx: dict):
        e = ctx.get("holdout_expectancy")
        if e is None:
            return False, {"holdout_expectancy": None}
        return e > 0, {"holdout_expectancy": e}


DEFAULT_CRITERIA = [
    MinTrades(200),
    PositiveExpectancy(),
    WalkForward(min_positive=3, windows=4),
    ShuffleControl(p=0.05),
    CorrectedSignificance(p=0.05),
    OverlayBand(),
    FinalHoldout(),
]


def run(ctx: dict, criteria: list | None = None) -> dict:
    """Short-circuit at the first failure; record which one it was."""
    crits = DEFAULT_CRITERIA if criteria is None else criteria
    details = {}
    for c in crits:
        ok, info = c.check(ctx)
        details[c.name] = {"passed": ok, **info}
        if not ok:
            return {"passed": False, "failed_at": c.name, "details": details}
    return {"passed": True, "failed_at": None, "details": details}
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_gauntlet.py -v`
Expected: 12 passed

- [ ] **Step 5: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 8: Position sizing from measured edge

**Files:**
- Create: `research/sizing.py`
- Create: `tests/test_sizing.py`

**Interfaces:**
- Produces:
  - `sizing.kelly_fraction(trades, fraction=0.25) -> float`
  - `sizing.drawdown_derived_cap(equity_curve, pct_of_equity=0.20, quantile=0.95) -> float`
  - `sizing.recommend(trades, equity_curve, validated: bool) -> dict` with keys
    `kelly_f`, `applied_f`, `max_leverage`, `max_lot_pct`, `rationale`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_sizing.py`:

```python
import numpy as np
import pytest

from research import sizing


def test_kelly_is_zero_for_zero_edge():
    trades = [{"net_r": 1.0}, {"net_r": -1.0}] * 100
    assert sizing.kelly_fraction(trades) == pytest.approx(0.0, abs=1e-9)


def test_kelly_is_positive_for_positive_edge():
    trades = [{"net_r": 1.0}] * 60 + [{"net_r": -1.0}] * 40
    assert sizing.kelly_fraction(trades) > 0


def test_kelly_is_zero_for_negative_edge():
    trades = [{"net_r": 1.0}] * 40 + [{"net_r": -1.0}] * 60
    assert sizing.kelly_fraction(trades) == 0.0, "never size a losing edge"


def test_quarter_kelly_is_a_quarter_of_full():
    trades = [{"net_r": 1.0}] * 60 + [{"net_r": -1.0}] * 40
    full = sizing.kelly_fraction(trades, fraction=1.0)
    quarter = sizing.kelly_fraction(trades, fraction=0.25)
    assert quarter == pytest.approx(full * 0.25)


def test_kelly_returns_zero_on_insufficient_trades():
    assert sizing.kelly_fraction([{"net_r": 1.0}] * 5) == 0.0


def test_unvalidated_strategy_gets_zero_size():
    """The structural guarantee: no gauntlet pass means no position."""
    trades = [{"net_r": 1.0}] * 60 + [{"net_r": -1.0}] * 40
    rec = sizing.recommend(trades, np.arange(100.0), validated=False)
    assert rec["applied_f"] == 0.0
    assert rec["max_lot_pct"] == 0.0
    assert "not validated" in rec["rationale"].lower()


def test_drawdown_cap_shrinks_as_drawdown_grows():
    mild = np.array([100.0, 101, 102, 101, 103, 104])
    severe = np.array([100.0, 60, 90, 40, 80, 30])
    assert sizing.drawdown_derived_cap(severe) < sizing.drawdown_derived_cap(mild)


def test_drawdown_cap_is_never_negative_or_above_one():
    for curve in (np.array([100.0, 1.0]), np.arange(1.0, 50.0)):
        cap = sizing.drawdown_derived_cap(curve)
        assert 0.0 <= cap <= 1.0
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sizing.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.sizing'`

- [ ] **Step 3: Implement sizing.py**

```python
"""Position sizing derived from MEASURED edge.

The app currently sizes from a hand-set confidence table: >80 confidence maps
to 100% of max lot at 20x leverage. Confidence is a guess at edge, so that
table is a guess compounded by leverage.

This module replaces it with fractional Kelly on the strategy's own validated
out-of-sample trade distribution, hard-capped by a drawdown-derived limit.

STATED LIMIT: Kelly on an ESTIMATED edge is fragile - the estimate carries all
the uncertainty of the backtest. Quarter-Kelly is the standard defensive
discount, not a safety guarantee.
"""

from __future__ import annotations

import numpy as np

MIN_TRADES_FOR_KELLY = 30


def kelly_fraction(trades: list, fraction: float = 0.25) -> float:
    """f* = mean / variance of per-trade R, scaled by `fraction`.

    Returns 0.0 for a non-positive edge or too few trades: there is no
    fraction of a losing edge worth betting.
    """
    if len(trades) < MIN_TRADES_FOR_KELLY:
        return 0.0
    r = np.array([t["net_r"] for t in trades], dtype="float64")
    r = r[np.isfinite(r)]
    if len(r) < MIN_TRADES_FOR_KELLY:
        return 0.0
    mu, var = float(r.mean()), float(r.var(ddof=1))
    if mu <= 0 or var <= 0:
        return 0.0
    return float(max(0.0, (mu / var) * fraction))


def drawdown_derived_cap(equity_curve: np.ndarray,
                         pct_of_equity: float = 0.20,
                         quantile: float = 0.95) -> float:
    """Largest position fraction whose q-quantile drawdown stays within
    `pct_of_equity` of the account."""
    e = np.asarray(equity_curve, dtype="float64")
    if len(e) < 2:
        return 0.0
    peak = np.maximum.accumulate(e)
    dd = np.where(peak > 0, (peak - e) / peak, 0.0)
    q = float(np.quantile(dd, quantile))
    if q <= 0:
        return 1.0
    return float(min(1.0, max(0.0, pct_of_equity / q)))


def recommend(trades: list, equity_curve: np.ndarray,
              validated: bool) -> dict:
    """Sizing recommendation. An unvalidated strategy gets ZERO by
    construction - the guarantee is structural, not a matter of discipline."""
    if not validated:
        return {"kelly_f": 0.0, "applied_f": 0.0, "max_leverage": 0.0,
                "max_lot_pct": 0.0,
                "rationale": "Strategy did not clear the gauntlet; it is not "
                             "validated, so size is zero by construction."}

    kf = kelly_fraction(trades, fraction=1.0)
    quarter = kf * 0.25
    cap = drawdown_derived_cap(equity_curve)
    applied = min(quarter, cap)
    return {
        "kelly_f": kf,
        "applied_f": applied,
        "max_leverage": round(min(5.0, 1.0 + applied * 10.0), 2),
        "max_lot_pct": round(applied * 100.0, 2),
        "rationale": (f"quarter-Kelly {quarter:.4f} capped by the 95th-percentile "
                      f"drawdown limit {cap:.4f}; applied {applied:.4f}"),
    }
```

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_sizing.py -v`
Expected: 8 passed

- [ ] **Step 5: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 9: Search driver

**Files:**
- Create: `research/search.py`
- Create: `tests/test_search.py`

**Interfaces:**
- Consumes: everything above
- Produces:
  - `search.enumerate_candidates() -> list[dict]` — each `{family, timeframe, params, gates}`
  - `search.declared_budget(candidates) -> int`
  - `search.split_data(d, holdout_frac=0.20, windows=4) -> dict` with keys
    `dev`, `holdout`, `wf_windows`
  - `search.run_candidate(cand, splits, overlay) -> dict` (a gauntlet ctx)
  - `search.main()`

- [ ] **Step 1: Write the failing tests**

Create `tests/test_search.py`:

```python
import numpy as np
import pytest

from research import search


def _d(n=10000):
    t = np.arange(0, n * 60, 60, dtype="int64")
    c = 64000.0 + np.cumsum(np.random.default_rng(1).normal(0, 5, n))
    return {"time": t, "open": c, "high": c + 5, "low": c - 5,
            "close": c, "volume": np.ones(n)}


def test_holdout_is_the_last_20_percent_and_disjoint_from_dev():
    s = search.split_data(_d(), holdout_frac=0.20, windows=4)
    assert len(s["holdout"]["time"]) == pytest.approx(2000, abs=5)
    assert s["holdout"]["time"][0] > s["dev"]["time"][-1], "holdout must be strictly after dev"


def test_walk_forward_windows_are_contiguous_and_inside_dev():
    s = search.split_data(_d(), holdout_frac=0.20, windows=4)
    assert len(s["wf_windows"]) == 4
    for w in s["wf_windows"]:
        assert w["time"][0] >= s["dev"]["time"][0]
        assert w["time"][-1] <= s["dev"]["time"][-1]
    for a, b in zip(s["wf_windows"], s["wf_windows"][1:]):
        assert b["time"][0] > a["time"][-1], "windows must not overlap"


def test_declared_budget_counts_every_candidate():
    cands = search.enumerate_candidates()
    assert search.declared_budget(cands) == len(cands)
    assert len(cands) > 50, "the search should be wide enough to be worth correcting for"


def test_enumerate_candidates_covers_all_timeframes():
    tfs = {c["timeframe"] for c in search.enumerate_candidates()}
    assert {"1m", "5m", "15m", "1h", "4h"} <= tfs


def test_enumerate_candidates_includes_existing_and_new_families():
    fams = {c["family"] for c in search.enumerate_candidates()}
    assert "ema_cross" in fams, "existing strategies must be re-tested"
    assert "momentum_persistence" in fams, "new families must be included"
```

- [ ] **Step 2: Run tests to verify they fail**

Run: `.venv/Scripts/python.exe -m pytest tests/test_search.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'research.search'`

- [ ] **Step 3: Implement search.py**

Requirements:
- `split_data` reserves the **last** `holdout_frac` as the untouched holdout and
  divides the remaining dev span into `windows` contiguous, non-overlapping
  walk-forward windows.
- `enumerate_candidates` yields the 8 existing (from `vec.DEFAULTS`) plus the
  new families from `families.py`, crossed with `("1m","5m","15m","1h","4h")`
  and each family's parameter grid, crossed with gate on/off combinations that
  are **constrained, not a full cross-product** (gates are applied as at most
  two at a time, to keep the budget bounded).
- `declared_budget` returns `len(candidates)` and this number is passed into
  `CorrectedSignificance` so the multiple-comparison correction covers every
  combo tried, including those discarded.
- `main()` writes `research/search_results.json` containing: the declared
  budget, all RNG seeds, every candidate's gauntlet result, and the list of
  survivors.

- [ ] **Step 4: Run tests to verify they pass**

Run: `.venv/Scripts/python.exe -m pytest tests/test_search.py -v`
Expected: 5 passed

- [ ] **Step 5: Verify no regression**

Run: `.venv/Scripts/python.exe verify_audit_fixes.py`
Expected: `ALL CHECKS PASSED`

**Do not commit.**

---

### Task 10: Run the study, write the report, propose risk defaults

**Files:**
- Create: `research/report2.py`
- Create: `research/STUDY_2026-07-25.md` (generated output)
- Modify: `settings.json` (proposed values only — see Step 4)

**Interfaces:**
- Consumes: `research/search_results.json`, `research/calibration_results.json`
- Produces: `report2.main()` writing the markdown study

- [ ] **Step 1: Run the full pipeline**

```bash
.venv/Scripts/python.exe -m research.run_calibration
```

Expected: `GATE: PASSED`. If FAILED, stop and report.

```bash
.venv/Scripts/python.exe -m research.search
```

Expected: `research/search_results.json` written, with a printed count of survivors.

- [ ] **Step 2: Generate the report**

`report2.main()` must produce a markdown document containing:

1. **Headline verdict** — how many candidates cleared the gauntlet, out of the declared budget.
2. **Overlay validation table** — per-contract MAE/R², and the stated limits, so the reader can judge how much to trust the option costs.
3. **Survivors** (if any) — full metrics, walk-forward values, p-values, and the sizing recommendation from `sizing.recommend`.
4. **Near-misses** — the top ~10 candidates ranked by how far they got, each labelled with the exact criterion that killed it. This is the deliverable when nothing passes.
5. **Reproducibility block** — declared budget, every RNG seed, data span and completeness %, and the exact cost-model constants used.

Run: `.venv/Scripts/python.exe -m research.report2`
Expected: `research/STUDY_2026-07-25.md` written.

- [ ] **Step 3: Verify the honest-failure path is exercised**

Run: `.venv/Scripts/python.exe -m pytest tests/ -v`
Expected: all tests pass.

Then confirm the report renders correctly for a zero-survivor run by checking
that `STUDY_2026-07-25.md` contains a near-miss table even when the survivor
list is empty. If the study did produce survivors, temporarily raise the bar
and re-run `report2` to confirm the empty-survivor rendering works, then revert.

- [ ] **Step 4: Propose risk defaults — DO NOT auto-apply**

Compute the recommended caps from the study's own drawdown distribution via
`sizing.recommend`, and write them to `research/proposed_settings.json`
alongside the current values and the rationale for each change.

**Do not modify `settings.json` directly.** Present the proposal to the user
with a side-by-side diff and let them decide. If no strategy cleared the
gauntlet, the proposal is `max_lot_pct: 0` with sizing disabled, and that must
be stated plainly rather than softened.

- [ ] **Step 5: Final verification**

```bash
.venv/Scripts/python.exe verify_audit_fixes.py
.venv/Scripts/python.exe -m pytest tests/ -v
git status
```

Expected: `ALL CHECKS PASSED`, all tests green, and `git status` showing every
change **unstaged and uncommitted** for user review.

**Do not commit. Do not push.**

---

## Self-Review

**Spec coverage:**
- §4 architecture → Tasks 1–9 create every listed module
- §4.1 data layer (365d fetch, resample, validate gate) → Task 1
- §5 overlay (BS, IV, spread, fee, fill) → Tasks 2, 4
- §5.1 parsimony → Task 4 (`MIN_IV_SAMPLES`, flat default term/skew, pessimistic bucket floor)
- §5.2 credibility gate + error-band propagation → Task 4 (`validate_overlay`), Task 7 (`OverlayBand`)
- §6 families + declared budget → Tasks 6, 9
- §7 gauntlet (all 7 criteria, split, holdout-once) → Tasks 7, 9
- §8.1 dangerous defaults → Task 10 Step 4
- §8.2 quarter-Kelly sizing → Task 8
- §9 testing (equivalence, harness null, overlay holdout, data integrity, verify_audit_fixes, seeds) → Task 1 Step 6, Task 5 Step 5, Task 4 Step 5, every task's final step
- §10 out of scope → no task enables live orders or builds a recorder daemon

**Placeholder scan:** no TBD/TODO. Tasks 9 and 10 specify requirements plus
exact interfaces rather than full source; both are driver/reporting code whose
shape depends on the results of earlier tasks. Every function they call is
fully defined in Tasks 1–8.

**Type consistency:** `net_r` is the trade key used in `sim.py`, `gauntlet.py`
and `sizing.py`. `parse_symbol` returns `kind/asset/strike/expiry` and is
consumed with those exact keys in `optdata.py` and `calibrate.py`. Gates return
`bool` arrays; entries return `int8`; `apply_gates` enforces both.

## Execution Handoff

Two options: subagent-driven (fresh agent per task, review between) or inline
execution in this session. Given the no-commit constraint and that several
tasks gate on live network results, inline execution with checkpoints is the
better fit.

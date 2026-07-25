"""Strategy search driver.

WHAT IT SEARCHES
----------------
The 8 existing strategies (vec.GENERATORS) plus the new entry families in
families.py, crossed with timeframes and a constrained set of gate
combinations. Every candidate goes through the fixed bar in gauntlet.py.

HOW AN OPTION TRADE IS MODELLED
-------------------------------
The live engine signals on the UNDERLYING then buys an ATM option: a call for
a long signal, a put for a short one. Both are LONG premium, which is why both
legs are simulated as long trades on their own premium series rather than as a
short. `booming_bulls_supertrend` is the exception -- it reads the option
premium directly, so it is fed the premium series (matching phase1.py).

Premium series are built once per (timeframe, DTE band) and reused across
every candidate on that timeframe; rebuilding per candidate would dominate
runtime.

SCOPE: DTE >= 1 DAY
-------------------
Set by the overlay's own validation against executable trade prints, not by
preference: it passed 5/5 above 7d, 4/9 at 1-3d, and only 2/8 below a day.
Sub-daily is run but flagged EXPLORATORY and excluded from the primary
acceptance set.
"""

from __future__ import annotations

import json
import math
import time

import numpy as np

from research import (calibrate, costs, data, families, gauntlet, optdata,
                      premium, sim, sizing, vec)

TIMEFRAMES = ("5m", "15m", "1h", "4h")
PRIMARY_DTE = {"1-3d": 48.0, "3-7d": 120.0, "7d+": 240.0}
EXPLORATORY_DTE = {"<1d": 8.0}

HOLDOUT_FRAC = 0.20
WF_WINDOWS = 4
SEED = 20260725
CONTRACT_VALUE = 0.001
CONTRACTS = 1
MAX_HOLD = 48
N_SHUFFLE = 100
IV_BAND = (0.85, 1.15)      # overlay passed its gate at threshold; test +-15%

PREMIUM_BASED = {"booming_bulls_supertrend"}


# ------------------------------------------------------------------ splits

def split_indices(n: int, holdout_frac=HOLDOUT_FRAC, windows=WF_WINDOWS):
    cut = int(n * (1.0 - holdout_frac))
    step = max(1, cut // windows)
    wf = [(i * step, (i + 1) * step if i < windows - 1 else cut)
          for i in range(windows)]
    return {"dev": (0, cut), "holdout": (cut, n), "wf": wf}


def split_data(d: dict, holdout_frac=HOLDOUT_FRAC, windows=WF_WINDOWS) -> dict:
    idx = split_indices(len(d["time"]), holdout_frac, windows)
    sl = lambda ab: {k: v[ab[0]:ab[1]] for k, v in d.items()}
    return {"dev": sl(idx["dev"]), "holdout": sl(idx["holdout"]),
            "wf_windows": [sl(w) for w in idx["wf"]]}


# ------------------------------------------------------- candidate space

def _param_grid(name: str) -> list[dict]:
    if name == "momentum_persistence":
        return [{"lookback": lb, "persist": p} for lb in (6, 12, 24) for p in (2, 3)]
    if name == "breakout_volume":
        return [{"channel": ch, "vol_z": z} for ch in (20, 50) for z in (1.0, 1.5)]
    return [{}]


GATE_SETS = [(), ("vol_regime",), ("session",), ("htf_trend",),
             ("vol_regime", "htf_trend")]


def enumerate_candidates(dte_bands=None) -> list[dict]:
    """Every (family, timeframe, DTE band, params, gates) combination.

    Gate combinations are capped at two: the full cross-product would multiply
    the budget without adding information, and every extra combo widens the
    multiple-comparison correction applied to whatever survives.
    """
    bands = dte_bands if dte_bands is not None else PRIMARY_DTE
    out = []
    for fam in list(vec.GENERATORS) + list(families.ENTRY_FAMILIES):
        for tf in TIMEFRAMES:
            for band in bands:
                for params in _param_grid(fam):
                    for gates in GATE_SETS:
                        out.append({"family": fam, "timeframe": tf,
                                    "dte_band": band, "params": params,
                                    "gates": list(gates)})
    return out


def declared_budget(candidates) -> int:
    return len(candidates)


# ------------------------------------------------------- premium series

def build_premium_series(spot_d, overlay, dte_hours, is_call, bar_seconds,
                         iv_mult=1.0):
    """OHLC premium series for a rolling ATM contract.

    A fresh ATM contract is struck every dte/2 hours and expires dte_hours
    later. The strike is held FIXED between rolls: a perpetually-ATM strike
    would have no delta response and would make the premium blind to
    direction.
    """
    t = spot_d["time"]
    n = len(t)
    if n == 0:
        return {k: v.copy() for k, v in spot_d.items()}

    roll_s = max(int(dte_hours * 3600 // 2), bar_seconds)
    epoch = (t // roll_s) * roll_s
    starts = np.flatnonzero(np.r_[True, epoch[1:] != epoch[:-1]])
    ends = np.r_[starts[1:], n]

    bpy = 365.0 * 86400.0 / bar_seconds
    rv = premium.realized_vol(spot_d["close"], min(1440, max(2, n - 1)), bpy)
    fin = rv[np.isfinite(rv)]
    rv = np.nan_to_num(rv, nan=float(np.median(fin)) if len(fin) else 0.5)
    rv = rv * iv_mult

    fields = ("open", "high", "low", "close")
    out = {f: np.zeros(n, dtype="float64") for f in fields}
    for a, b in zip(starts, ends):
        K = round(float(spot_d["close"][a]) / 200.0) * 200.0     # 200-wide grid
        expiry = int(t[a]) + int(dte_hours * 3600)
        for f in fields:
            out[f][a:b] = overlay.premium_path(spot_d[f][a:b], K, expiry,
                                               t[a:b], is_call, rv=rv[a:b])

    hi = np.maximum.reduce([out[f] for f in fields])
    lo = np.minimum.reduce([out[f] for f in fields])
    return {"time": t, "open": out["open"], "close": out["close"],
            "high": hi, "low": lo, "volume": np.zeros(n),
            # Boundaries between DIFFERENT contracts. Callers MUST NOT let a
            # trade cross one -- see _trades().
            "roll_starts": starts.astype("int64")}


# ------------------------------------------------------------ evaluation

def build_signals(cand, d_tf, prem_ce, htf_close):
    fam = cand["family"]
    if fam in families.ENTRY_FAMILIES:
        sig = families.ENTRY_FAMILIES[fam](d_tf, **cand["params"])
    elif fam in PREMIUM_BASED:
        # reads the option premium directly, as phase1.py does
        sig = vec.GENERATORS[fam](prem_ce, **cand["params"])
    else:
        p = dict(vec.DEFAULTS.get(fam, {}).get("params", {}))
        p.update(cand["params"])
        sig = vec.GENERATORS[fam](d_tf, **p)

    gates = []
    for g in cand["gates"]:
        if g == "vol_regime":
            gates.append(families.vol_regime_gate(d_tf))
        elif g == "session":
            gates.append(families.session_gate(d_tf))
        elif g == "htf_trend" and htf_close is not None:
            gates.append(families.htf_trend_gate(d_tf, htf_close))
    return families.apply_gates(sig, *gates) if gates else sig


def _slice(d, a, b):
    """Slice a series. `roll_starts` holds INDICES, not per-bar values, so it
    is re-based to the slice rather than sliced positionally."""
    out = {k: v[a:b] for k, v in d.items() if k != "roll_starts"}
    if "roll_starts" in d:
        rs = np.asarray(d["roll_starts"], dtype="int64")
        rs = rs[(rs > a) & (rs < b)] - a
        out["roll_starts"] = np.concatenate([[0], rs]).astype("int64")
    return out


def _segment_bounds(series, n):
    """Contiguous [start, end) ranges, one per contract."""
    starts = series.get("roll_starts")
    if starts is None or len(starts) == 0:
        return [(0, n)]
    s = [int(x) for x in starts if 0 <= int(x) < n]
    if not s or s[0] != 0:
        s = [0] + s
    return list(zip(s, s[1:] + [n]))


def _trades(sig, ce, pe, spot_close, rr, cost_model):
    """Long CE on +1, long PE on -1 -- both long premium, as the engine does.

    Simulated ONE CONTRACT AT A TIME. A rolling premium series splices
    different contracts end to end, and the seam is not a price move anyone
    can trade: measured on the 1h/1-3d series the premium jumps a median 78.7%
    at a roll (p90 4609%, max 4.4e13% where an expiring contract worth ~0 is
    replaced by a fresh ATM one) against a median 16.8% for a normal bar.

    Letting a trade cross a seam manufactured the entire result of the first
    search run -- +1.07 R per trade at p=1e-83, which is not a market edge but
    an accounting fiction. Trades are therefore confined to a single contract
    and time-exit at its boundary, which is what holding a real option does.
    """
    out = []
    n = len(sig)
    for direction, series in ((1, ce), (-1, pe)):
        s = (sig == direction).astype("int8")
        if not s.any():
            continue
        for a, b in _segment_bounds(series, n):
            seg_sig = s[a:b]
            if not seg_sig.any():
                continue
            seg = {k: v[a:b] for k, v in series.items() if k != "roll_starts"}
            res = sim.simulate(seg, seg_sig, stop_atr=1.0, rr=rr,
                               max_hold=min(MAX_HOLD, b - a),
                               long_only=True, cost_model=cost_model,
                               spot=spot_close[a:b],
                               contract_value=CONTRACT_VALUE,
                               contracts=CONTRACTS)
            for tr in res["trades"]:          # restore whole-series indices
                tr["i"] += a
                tr["entry_i"] += a
                tr["exit_i"] += a
                # sim labels by long/short and BOTH legs are long premium, so
                # its "dir" field cannot tell a call from a put here. Record
                # which contract was actually bought.
                tr["opt"] = "CE" if direction == 1 else "PE"
                out.append(tr)
    return out


def _exp(trades):
    return float(np.mean([t["r"] for t in trades])) if trades else None


def evaluate(cand, bundle, sig_full, budget, rng) -> dict:
    """Build the gauntlet context for one candidate, LAZILY.

    Each statistic is computed only if the candidate is still alive at that
    point in the gauntlet's order. This is not just a speed trick -- the
    gauntlet short-circuits, so a statistic for a criterion that will never be
    reached has no bearing on the verdict.

    It matters a lot for runtime: the direction-shuffle null is N_SHUFFLE full
    re-simulations, and computing it for candidates that already died at
    positive_expectancy dominated the sweep (16.4s per 5m candidate, ~1.8h
    over the full budget). Most candidates die on the first two criteria, so
    evaluating in order makes the common case cheap.

    `_alive` re-runs the gauntlet against what has been computed so far, with
    later fields set to sentinel passes. That keeps gauntlet.py the single
    definition of the bar -- this function never re-encodes a threshold.
    """
    rr = float(vec.DEFAULTS.get(cand["family"], {}).get("rr", 1.5))
    idx = bundle["idx"]
    ce, pe, spot = bundle["ce"], bundle["pe"], bundle["spot"]
    cost = bundle["cost"]
    a, b = idx["dev"]

    # sentinels stand in for not-yet-computed criteria so the partial gauntlet
    # run stops at the first GENUINE failure rather than at a missing field
    PENDING = {"walk_forward": [1.0] * WF_WINDOWS, "shuffle_p": 0.0,
               "corrected_p": 0.0, "band_results": [1.0],
               "holdout_expectancy": 1.0}

    def _alive(ctx):
        return gauntlet.run({**PENDING, **ctx})["passed"]

    # --- 1/2: trade count and expectancy (always needed) ---
    dev = _trades(sig_full[a:b], _slice(ce, a, b), _slice(pe, a, b),
                  spot["close"][a:b], rr, cost)
    ctx = {"search_budget": budget, "trades": dev}
    if not _alive(ctx):
        return ctx
    obs = _exp(dev)

    # --- 3: walk-forward ---
    ctx["walk_forward"] = [
        _exp(_trades(sig_full[x:y], _slice(ce, x, y), _slice(pe, x, y),
                     spot["close"][x:y], rr, cost))
        for x, y in idx["wf"]]
    if not _alive(ctx):
        return ctx

    # --- 4: direction-shuffle null (expensive: N_SHUFFLE re-simulations) ---
    nz = np.flatnonzero(sig_full[a:b] != 0)
    nulls = []
    for _ in range(N_SHUFFLE if len(nz) else 0):
        sh = np.zeros(b - a, dtype="int8")
        sh[nz] = rng.choice(np.array([-1, 1], dtype="int8"), size=len(nz))
        e = _exp(_trades(sh, _slice(ce, a, b), _slice(pe, a, b),
                         spot["close"][a:b], rr, cost))
        if e is not None:
            nulls.append(e)
    ctx["shuffle_p"] = float(np.mean([x >= obs for x in nulls])) if nulls else None
    if not _alive(ctx):
        return ctx

    # --- 5: t-test, Bonferroni-corrected over the FULL declared budget ---
    r = np.array([t["r"] for t in dev], dtype="float64")
    if len(r) > 2 and r.std(ddof=1) > 0:
        tstat = r.mean() / (r.std(ddof=1) / math.sqrt(len(r)))
        ctx["corrected_p"] = min(1.0, math.erfc(abs(tstat) / math.sqrt(2)) * budget)
    else:
        ctx["corrected_p"] = None
    if not _alive(ctx):
        return ctx

    # --- 6: overlay error band, IV shifted +-15% ---
    ctx["band_results"] = [obs] + [
        _exp(_trades(sig_full[a:b], _slice(bundle[f"ce_{k}"], a, b),
                     _slice(bundle[f"pe_{k}"], a, b), spot["close"][a:b], rr, cost))
        for k in ("lo", "hi")]
    if not _alive(ctx):
        return ctx

    # --- 7: final holdout, touched exactly once and only here ---
    h0, h1 = idx["holdout"]
    ctx["holdout_expectancy"] = _exp(
        _trades(sig_full[h0:h1], _slice(ce, h0, h1), _slice(pe, h0, h1),
                spot["close"][h0:h1], rr, cost))
    return ctx

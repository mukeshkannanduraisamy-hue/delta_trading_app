"""Walk-forward parameter optimization for the crypto-adapted strategy.

The original Nifty concept (EMA trend + Supertrend/ATR risk, intraday session,
fixed point targets) is generalised into ONE parameterised crypto strategy, then
optimised by walk-forward analysis rather than a single in-sample grid search.

Why walk-forward and not plain grid search: with enough parameters, any grid
produces a "winner" on a random signal. Walk-forward refits on a training window
and scores ONLY on the untouched window that follows, then rolls forward. The
aggregate out-of-sample result is the honest number — in-sample results are
reported purely to show the size of the curve-fitting gap.

Modelling choices (all deliberately pessimistic):
  * Fees 0.05% per side (crypto taker) + 2bp slippage per side.
  * Stops/targets checked intrabar on high/low; when a bar touches both, the
    STOP is assumed first (conservative).
  * Entry on the bar AFTER the signal closes — no lookahead.
  * Risk-based sizing: each trade risks RISK_PCT of equity to the stop.
"""

from __future__ import annotations

import itertools
import math
from dataclasses import dataclass, field

import numpy as np

# --------------------------------------------------------------------------- #
# Vectorised indicators
# --------------------------------------------------------------------------- #
def ema(x: np.ndarray, n: int) -> np.ndarray:
    a = 2.0 / (n + 1.0)
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rma(x: np.ndarray, n: int) -> np.ndarray:
    a = 1.0 / n
    out = np.empty_like(x)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def atr(h: np.ndarray, l: np.ndarray, c: np.ndarray, n: int) -> np.ndarray:
    prev = np.concatenate(([c[0]], c[:-1]))
    tr = np.maximum(h - l, np.maximum(np.abs(h - prev), np.abs(l - prev)))
    return rma(tr, n)


def rsi(c: np.ndarray, n: int) -> np.ndarray:
    d = np.diff(c, prepend=c[0])
    up = rma(np.clip(d, 0, None), n)
    dn = rma(np.clip(-d, 0, None), n)
    rs = np.divide(up, dn, out=np.full_like(up, np.inf), where=dn > 0)
    return 100 - 100 / (1 + rs)


def adx(h, l, c, n: int) -> np.ndarray:
    up_move = np.diff(h, prepend=h[0])
    dn_move = -np.diff(l, prepend=l[0])
    plus_dm = np.where((up_move > dn_move) & (up_move > 0), up_move, 0.0)
    minus_dm = np.where((dn_move > up_move) & (dn_move > 0), dn_move, 0.0)
    a = atr(h, l, c, n)
    safe = np.where(a > 0, a, np.nan)
    pdi = 100 * rma(plus_dm, n) / safe
    mdi = 100 * rma(minus_dm, n) / safe
    dx = 100 * np.abs(pdi - mdi) / np.where((pdi + mdi) > 0, pdi + mdi, np.nan)
    return np.nan_to_num(rma(np.nan_to_num(dx), n))


def supertrend_dir(h, l, c, n: int, mult: float) -> np.ndarray:
    """Returns +1 uptrend / -1 downtrend."""
    a = atr(h, l, c, n)
    hl2 = (h + l) / 2
    upper, lower = hl2 + mult * a, hl2 - mult * a
    fu = upper.copy()
    fl = lower.copy()
    d = np.ones(len(c), dtype=np.int8)
    for i in range(1, len(c)):
        fu[i] = upper[i] if (upper[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
        fl[i] = lower[i] if (lower[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
        if d[i - 1] == 1:
            d[i] = -1 if c[i] < fl[i] else 1
        else:
            d[i] = 1 if c[i] > fu[i] else -1
    return d


# --------------------------------------------------------------------------- #
@dataclass
class Params:
    ema_fast: int = 9
    ema_slow: int = 21
    atr_len: int = 14
    atr_mult_sl: float = 1.5
    rr: float = 1.5              # target = rr x stop distance
    st_len: int = 10             # supertrend
    st_mult: float = 3.0
    use_supertrend: bool = True
    rsi_len: int = 14
    rsi_lo: float = 0.0          # 0 disables the filter
    rsi_hi: float = 100.0
    adx_len: int = 14
    adx_min: float = 0.0         # 0 disables
    vol_mult: float = 0.0        # require volume > vol_mult x 20-bar avg (0=off)
    htf_ema: int = 0             # higher-timeframe trend filter (0=off), in bars
    trail_atr: float = 0.0       # trailing stop in ATRs (0=off)
    breakeven_at: float = 0.0    # move stop to entry after this many R (0=off)
    partial_at: float = 0.0      # take half off at this many R (0=off)
    cooldown: int = 0            # bars to wait after a trade closes
    max_hold: int = 200

    def key(self) -> tuple:
        return tuple(sorted(self.__dict__.items()))


@dataclass
class Result:
    trades: int = 0
    wins: int = 0
    losses: int = 0
    net_profit: float = 0.0      # in R (risk multiples)
    gross_win: float = 0.0
    gross_loss: float = 0.0
    max_dd: float = 0.0
    equity: list = field(default_factory=list)
    returns: list = field(default_factory=list)

    def metrics(self, bars_per_year: float = 8760.0, avg_hold: float = 1.0) -> dict:
        n = self.trades
        if n == 0:
            return {"trades": 0, "win_rate": None, "profit_factor": None,
                    "net_profit_r": 0.0, "avg_trade_r": None, "max_drawdown_r": 0.0,
                    "sharpe": None, "sortino": None, "expectancy_r": None}
        r = np.array(self.returns)
        wr = self.wins / n * 100
        pf = (self.gross_win / self.gross_loss) if self.gross_loss > 0 else None
        exp = float(r.mean())
        sd = float(r.std(ddof=1)) if n > 1 else 0.0
        downside = r[r < 0]
        dsd = float(downside.std(ddof=1)) if len(downside) > 1 else 0.0
        # Annualise per-trade stats by the trade frequency actually observed.
        trades_per_year = bars_per_year / max(avg_hold, 1.0)
        ann = math.sqrt(max(trades_per_year, 1.0))
        return {
            "trades": n,
            "win_rate": round(wr, 2),
            "profit_factor": round(pf, 3) if pf else None,
            "net_profit_r": round(self.net_profit, 2),
            "avg_trade_r": round(exp, 4),
            "max_drawdown_r": round(self.max_dd, 2),
            "sharpe": round(exp / sd * ann, 3) if sd > 0 else None,
            "sortino": round(exp / dsd * ann, 3) if dsd > 0 else None,
            "expectancy_r": round(exp, 4),
        }


# --------------------------------------------------------------------------- #
FEE = 0.0005      # 0.05% per side
SLIP = 0.0002     # 2bp per side


def precompute(d: dict, p: Params) -> dict:
    h, l, c, v = d["high"], d["low"], d["close"], d["volume"]
    ind = {
        "ef": ema(c, p.ema_fast),
        "es": ema(c, p.ema_slow),
        "a": atr(h, l, c, p.atr_len),
    }
    ind["st"] = supertrend_dir(h, l, c, p.st_len, p.st_mult) if p.use_supertrend else None
    ind["rsi"] = rsi(c, p.rsi_len) if (p.rsi_lo > 0 or p.rsi_hi < 100) else None
    ind["adx"] = adx(h, l, c, p.adx_len) if p.adx_min > 0 else None
    if p.vol_mult > 0:
        k = np.ones(20) / 20
        ind["vavg"] = np.convolve(v, k, mode="same")
    else:
        ind["vavg"] = None
    ind["htf"] = ema(c, p.htf_ema) if p.htf_ema > 0 else None
    return ind


def backtest(d: dict, p: Params) -> Result:
    """Long/short directional backtest in R multiples (risk-normalised)."""
    h, l, c, v = d["high"], d["low"], d["close"], d["volume"]
    n = len(c)
    if n < 300:
        return Result()
    ind = precompute(d, p)
    ef, es, a = ind["ef"], ind["es"], ind["a"]

    res = Result()
    equity = 0.0
    peak = 0.0
    i = max(p.ema_slow, p.atr_len, p.st_len, p.htf_ema, 50) + 2
    cooldown_until = 0

    while i < n - 1:
        if i < cooldown_until or a[i] <= 0:
            i += 1
            continue

        long_sig = ef[i] > es[i] and ef[i - 1] <= es[i - 1]
        short_sig = ef[i] < es[i] and ef[i - 1] >= es[i - 1]
        if not (long_sig or short_sig):
            i += 1
            continue

        # ---- filters ----
        if ind["st"] is not None:
            if long_sig and ind["st"][i] != 1:
                i += 1; continue
            if short_sig and ind["st"][i] != -1:
                i += 1; continue
        if ind["rsi"] is not None:
            r_ = ind["rsi"][i]
            if long_sig and not (p.rsi_lo <= r_ <= p.rsi_hi):
                i += 1; continue
            if short_sig and not (100 - p.rsi_hi <= r_ <= 100 - p.rsi_lo):
                i += 1; continue
        if ind["adx"] is not None and ind["adx"][i] < p.adx_min:
            i += 1; continue
        if ind["vavg"] is not None and ind["vavg"][i] > 0 and v[i] < p.vol_mult * ind["vavg"][i]:
            i += 1; continue
        if ind["htf"] is not None:
            if long_sig and c[i] < ind["htf"][i]:
                i += 1; continue
            if short_sig and c[i] > ind["htf"][i]:
                i += 1; continue

        # ---- entry on the NEXT bar's open (no lookahead) ----
        entry_i = i + 1
        if entry_i >= n:
            break
        is_long = bool(long_sig)
        entry = d["open"][entry_i] * (1 + SLIP if is_long else 1 - SLIP)
        stop_d = p.atr_mult_sl * a[i]
        if stop_d <= 0:
            i += 1; continue
        stop = entry - stop_d if is_long else entry + stop_d
        target = entry + p.rr * stop_d if is_long else entry - p.rr * stop_d

        realised = 0.0          # R already banked from a partial
        remaining = 1.0         # fraction of position still open
        be_done = False
        part_done = False
        exit_i = min(n - 1, entry_i + p.max_hold)
        outcome = None

        for j in range(entry_i, exit_i + 1):
            move = (h[j] - entry) if is_long else (entry - l[j])
            adverse = (entry - l[j]) if is_long else (h[j] - entry)

            # stop first when a bar spans both (conservative)
            if adverse >= (entry - stop if is_long else stop - entry):
                outcome = realised + remaining * (-(abs(entry - stop)) / stop_d)
                exit_i = j
                break
            # partial profit
            if p.partial_at > 0 and not part_done and move >= p.partial_at * stop_d:
                realised += 0.5 * p.partial_at
                remaining = 0.5
                part_done = True
            # break-even
            if p.breakeven_at > 0 and not be_done and move >= p.breakeven_at * stop_d:
                stop = entry
                be_done = True
            # trailing stop
            if p.trail_atr > 0:
                if is_long:
                    stop = max(stop, h[j] - p.trail_atr * a[j])
                else:
                    stop = min(stop, l[j] + p.trail_atr * a[j])
            # target
            if move >= p.rr * stop_d:
                outcome = realised + remaining * p.rr
                exit_i = j
                break

        if outcome is None:  # time exit, mark to market
            px = c[exit_i]
            mv = (px - entry) if is_long else (entry - px)
            outcome = realised + remaining * (mv / stop_d)

        # costs: entry + exit, expressed in R
        cost_r = (2 * (FEE + SLIP) * entry) / stop_d
        outcome -= cost_r

        res.trades += 1
        res.returns.append(outcome)
        res.net_profit += outcome
        if outcome > 0:
            res.wins += 1
            res.gross_win += outcome
        else:
            res.losses += 1
            res.gross_loss += -outcome
        equity += outcome
        peak = max(peak, equity)
        res.max_dd = max(res.max_dd, peak - equity)
        res.equity.append(equity)

        cooldown_until = exit_i + 1 + p.cooldown
        i = exit_i + 1

    return res


# --------------------------------------------------------------------------- #
def walk_forward(d: dict, grid: list[Params], folds: int = 4,
                 train_frac: float = 0.6, bars_per_year: float = 8760.0) -> dict:
    """Refit on each training window, score on the untouched window that follows."""
    n = len(d["close"])
    if n < 2000:
        return {"error": "insufficient bars"}
    seg = n // folds
    oos_returns: list[float] = []
    picks = []

    for f in range(folds):
        lo = f * seg
        hi = min(n, lo + seg)
        cut = lo + int((hi - lo) * train_frac)
        if cut - lo < 500 or hi - cut < 300:
            continue
        train = {k: v[lo:cut] for k, v in d.items()}
        test = {k: v[cut:hi] for k, v in d.items()}

        best, best_score = None, -1e18
        for p in grid:
            r = backtest(train, p)
            if r.trades < 15:
                continue
            m = r.metrics(bars_per_year)
            # Rank by expectancy penalised by drawdown — favours smooth equity,
            # not a lucky win rate (task 13).
            score = m["expectancy_r"] - 0.05 * (r.max_dd / max(r.trades, 1))
            if score > best_score:
                best, best_score = p, score
        if best is None:
            continue
        rt = backtest(test, best)
        mt = rt.metrics(bars_per_year)
        picks.append({"fold": f, "params": dict(best.__dict__),
                      "train_score": round(best_score, 4), "oos": mt})
        oos_returns.extend(rt.returns)

    if not oos_returns:
        return {"folds": 0, "error": "no fold produced enough trades"}

    r = np.array(oos_returns)
    wins = r[r > 0]
    losses = r[r < 0]
    eq = np.cumsum(r)
    peak = np.maximum.accumulate(eq)
    dd = float(np.max(peak - eq)) if len(eq) else 0.0
    sd = float(r.std(ddof=1)) if len(r) > 1 else 0.0
    dsd = float(losses.std(ddof=1)) if len(losses) > 1 else 0.0
    ann = math.sqrt(max(len(r), 1))
    return {
        "folds": len(picks),
        "oos_trades": int(len(r)),
        "oos_win_rate": round(len(wins) / len(r) * 100, 2),
        "oos_expectancy_r": round(float(r.mean()), 4),
        "oos_net_r": round(float(r.sum()), 2),
        "oos_profit_factor": round(float(wins.sum() / -losses.sum()), 3) if len(losses) and losses.sum() < 0 else None,
        "oos_max_dd_r": round(dd, 2),
        "oos_sharpe": round(float(r.mean()) / sd * ann, 3) if sd > 0 else None,
        "oos_sortino": round(float(r.mean()) / dsd * ann, 3) if dsd > 0 else None,
        "fold_picks": picks,
    }


def build_grid(**axes) -> list[Params]:
    keys = list(axes)
    return [Params(**dict(zip(keys, combo))) for combo in itertools.product(*axes.values())]

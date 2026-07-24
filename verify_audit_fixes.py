"""Regression tests for the 2026-07-21 system-audit fixes.

Each assertion here guards an invariant that was actually violated in
production code. Run after any change to the execution or data path:

    cd D:\\TRADER\\delta_trading_app
    .\\.venv\\Scripts\\python.exe verify_audit_fixes.py

Offline except for the module imports — no network, no orders, no side effects.
"""
import main  # noqa: F401  — proves the whole app graph imports
from strategy import config, indicators as ind
from strategy.executor import _fees, _settlement_value, _limit_entry_price
from strategy.market_data import OptionQuote
from strategy.base import Signal
from strategy.pricebus import bus

print("import OK  mode={} contracts={} min_hrs={}".format(
    config.EXECUTION_MODE, config.CONTRACTS, config.MIN_HOURS_TO_EXPIRY))

# --- #7 settlement valuation ---
assert _settlement_value("CE", 100000, 105000) == 5000.0
assert _settlement_value("CE", 100000, 95000) == 0.0
assert _settlement_value("PE", 100000, 95000) == 5000.0
assert _settlement_value("PE", 100000, 105000) == 0.0
assert _settlement_value("CE", 100000, None) is None
print("settlement value OK")

# --- #14/#15 fees require real spot, no silent cap substitution ---
assert _fees(100.0, 0.001, 1, spot=0) is None
f = _fees(100.0, 0.001, 1, spot=100000.0)
assert f is not None and f > 0
print("fees OK ->", round(f, 6))

# --- #16 strategy-declared limit offset is honoured ---
q = OptionQuote("BTC-C", 1, 100000, "CE", 100.0, 110.0, 105.0, 0.001, 0.5, "01-01-2027")
base_px = _limit_entry_price(q, None)
sig = Signal("booming", "CE", "t", limit_offset_pct=0.005)
off_px = _limit_entry_price(q, sig)
assert off_px > base_px, (base_px, off_px)
print("limit offset OK  mid={}  +0.5%={}".format(base_px, off_px))

# --- #13 pricebus must never substitute mark for bid ---
bus.update("TEST-OPT", best_bid=None, best_ask=9.0, mark_price=7.0)
assert bus.bid("TEST-OPT") is None, bus.bid("TEST-OPT")
assert bus.ask("TEST-OPT") == 9.0
assert bus.mark("TEST-OPT") == 7.0
bus.update("TEST-OPT", best_bid=6.5)
assert bus.bid("TEST-OPT") == 6.5
print("pricebus bid/ask/mark OK")

# --- #19 supertrend no longer depends on negative-index luck ---
cs = [{"time": i * 60, "open": 100 + i, "high": 101 + i, "low": 99 + i,
       "close": 100.5 + i, "volume": 1} for i in range(40)]
line, direction = ind.supertrend(cs, 10, 2.0)
assert line[-1] is not None and direction[-1] in (1, -1)
assert ind.supertrend([], 10, 2.0) == ([], [])
print("supertrend OK  dir={}".format(direction[-1]))

# --- #18/#17 strategies self-guard against short input ---
from strategy.base import Context
from strategy.zing_strategies import STRATEGY_CLASSES
for cls in STRATEGY_CLASSES:
    s = cls()
    for n in (0, 1, 2, 3, 5, 21, 25):
        bars = [{"time": i * 60, "open": 100.0, "high": 101.0, "low": 99.0,
                 "close": 100.0, "volume": 1.0} for i in range(n)]
        out = s.evaluate(Context(underlying=bars, spot=100.0, premium={"CE": bars, "PE": bars}))
        assert isinstance(out, list), (s.slug, n, out)
print("all {} strategies survive short input OK".format(len(STRATEGY_CLASSES)))

# --- paper-trading contract: authenticated endpoints are disabled ---
# The internal-simulation refactor gutted every signed call to raise
# NotImplementedError. A caller that expected a live order must fail loudly, not
# silently no-op, so guard that they still do.
from strategy.delta_client import client, DeltaError
for name, call in (
    ("place_order", lambda: client.place_order(1, 1, "buy", "market_order")),
    ("positions",   lambda: client.positions()),
    ("profile",     lambda: client.profile()),
    ("balances",    lambda: client.balances()),
):
    try:
        call()
        raise AssertionError(f"{name} must be disabled in paper mode")
    except NotImplementedError:
        pass
print("authenticated endpoints disabled OK")

# --- _public_get surfaces a typed DeltaError on success:false (was NameError) ---
# The error branch referenced an undefined `url`, so every {"success": false}
# response raised NameError instead of the DeltaError the whole client contract
# promises — and NameError slips past every `except DeltaError` handler.
class _FakeResp:
    status_code = 200
    headers: dict = {}
    def raise_for_status(self):  # noqa: D401
        return None
    def json(self):
        return {"success": False, "error": "simulated-rejection"}

_orig_get = client.session.get
client.session.get = lambda *a, **k: _FakeResp()
try:
    client._public_get(config.PROD_BASE, "/v2/thing", {})
    raise AssertionError("expected DeltaError on success:false")
except DeltaError as exc:
    assert "simulated-rejection" in str(exc), exc
finally:
    client.session.get = _orig_get
print("public error path raises DeltaError OK")

# --- take-profit ladder is a true 40/40/20 off the ORIGINAL size ---
# TP2 used to fraction the post-TP1 remainder (int(contracts*0.66)), turning a
# 10-lot into 4/3/3 instead of the intended 4/4/2.
from strategy.executor import _tp_exit_size
assert _tp_exit_size(10, 10, 1) == 4          # TP1: 40% of 10
assert _tp_exit_size(10, 6, 2) == 4           # TP2: another 40% of 10, not of 6
assert _tp_exit_size(10, 2, 3) == 2           # TP3: whatever remains
assert _tp_exit_size(1, 1, 1) == 1            # never zero
assert _tp_exit_size(5, 3, 2) == 2            # never more than what's open
print("tp 40/40/20 ladder OK")

# --- session equity line = base + realized P&L (entry fee charged exactly once) ---
# _book_close subtracted the entry fee again although _register_position had
# already charged it, dragging the session equity line low by every trade's
# entry fee.
from strategy import store as strat_store
from strategy.executor import Executor
from strategy.market_data import OptionResolver
ex = Executor(OptionResolver(config.ASSET))
base_eq = ex.session_equity
vq = OptionQuote("__VERIFY__", 999999, 100000.0, "CE", 5.0, 6.0, 5.5, 0.001, 0.5, "01-01-2027")
vsig = Signal("__verify__", "CE", "equity check", sl_pct=0.05, rr=1.5, confidence=50)
ex._register_position("__verify__", "CE", vq, vsig, entry=5.0, contracts=4,
                      bar_seconds=60, leverage=1, order_id=None, adv_params={})
vpos = next(p for p in ex.positions.values() if p.strategy == "__verify__")
assert vpos.initial_contracts == 4, vpos.initial_contracts
ex._book_close(vpos, 7.0, vpos.contracts, "verify_full")
assert abs(ex.session_equity - (base_eq + ex.realized_pnl)) < 1e-6, \
    (ex.session_equity, base_eq, ex.realized_pnl)
strat_store.purge_trades(strategies=("__verify__",))
print("session equity single-fee OK")

# --- flatten_shorts stays graceful in paper mode (positions() is disabled) ---
res = ex.flatten_shorts()
assert res.get("ok") is True and res.get("closed") == 0, res
print("flatten_shorts paper-safe OK")

# --- D1: one authoritative starting balance for wallet / equity / loss-limit ---
# Before this fix account.py's wallet and store.performance()'s equity curve
# both started from config.SESSION_EQUITY_BASE (10,000) while the daily-loss-
# limit check in executor.py measured against settings.starting_virtual_balance
# (100,000) — a 50%-of-100k threshold could never trip against a 10k wallet, so
# the safety net was silently dead. All three must now agree.
from strategy.settings import manager as settings_manager
from strategy.account import AccountSync
expected = config.starting_balance()
assert expected == float(settings_manager.get("starting_virtual_balance")), expected
assert AccountSync().virtual_balance == expected, AccountSync().virtual_balance
fresh_ex = Executor(OptionResolver(config.ASSET))
assert fresh_ex.session_equity == expected, fresh_ex.session_equity
assert config.summary()["session_equity_base"] == expected
print("D1 starting-balance single-source-of-truth OK  ({})".format(expected))

# ============================================================================
# Delta-copilot fix-prompt (2026-07-24) — each item VALIDATED against the live
# API before implementing (see the probe scripts / PROJECT report). Copilot
# claims that the live API refuted are intentionally NOT implemented:
#   - FIX 4 (remove quotes.mark_iv): REJECTED — quotes.mark_iv exists live.
#   - FIX 3 formula (drop min/notional, GST off): CORRECTED — real fee keeps
#     min(taker*notional, premium_rate*premium) + GST.
# ============================================================================

# --- FIX 1: compact public-socket ticker/candle/trade normalizers ---
ws_item = {"s": "C-BTC-65000-070826", "m": "1845.8",
           "g": ["0.5", "0.0001", "0.36", "-50.04", "8.5"],   # delta,gamma,rho,theta,vega
           "q": ["1854", "5408", "1840", "8439", None],        # ask,ask_sz,bid,bid_sz
           "qiv": ["0.361", "0.358", "0.359"],                 # ask_iv,bid_iv,mark_iv
           "ohlc": [310.0, 335.0, 305.0, 318.0], "oi": ["1200", "45000"], "m24hc": "3.2"}
_n = main.normalize_ws_ticker_item(ws_item, spot_price="65000.5")
assert _n["best_ask"] == 1854.0 and _n["best_bid"] == 1840.0, _n   # NOT transposed
assert _n["theta"] == -50.04 and _n["mark_iv"] == 0.359, _n        # g[3], qiv[2]
assert _n["spot_price"] == 65000.5, _n
_cd = main.normalize_candle({"type": "candlestick_1m", "cst": 1700000000000000,
                             "o": 1, "h": 2, "l": 0.5, "c": 1.5, "v": 9, "res": "1m", "sy": "BTCUSD"})
assert _cd["time"] == 1700000000 and _cd["close"] == 1.5 and _cd["symbol"] == "BTCUSD", _cd
_tb = main.normalize_trade({"type": "trades", "sy": "BTCUSD", "p": "65000", "s": 3, "r": "t", "t": 1})
_ts = main.normalize_trade({"type": "trades", "sy": "BTCUSD", "p": "65000", "s": 3, "r": "m", "t": 1})
assert _tb["side"] == "buy" and _ts["side"] == "sell", (_tb, _ts)  # r=t buyer-taker, r=m seller-taker
print("FIX1 compact WS normalizers OK")

# --- FIX 2: settlement instant read from the product, not hardcoded 12:00 ---
from strategy.market_data import _parse_settlement
from strategy.executor import _expiry_settled
from datetime import datetime as _dt, timezone as _tz, timedelta as _td
assert _parse_settlement({"settlement_time": "2026-07-24T12:00:00Z"}) == _dt(2026, 7, 24, 12, 0, tzinfo=_tz.utc)
assert _expiry_settled("01-01-2099", _dt.now(_tz.utc) - _td(hours=1)) is True    # authoritative dt wins
assert _expiry_settled("01-01-2099", _dt.now(_tz.utc) + _td(hours=1)) is False
print("FIX2 settlement_time authoritative OK")

# --- FIX 3: per-product fee rates, keep min(notional, premium) + GST ---
assert config.FEE_NOTIONAL_RATE == 0.0001, config.FEE_NOTIONAL_RATE  # live rate; was 0.0003 (3x)
assert abs(_fees(100.0, 0.001, 1, spot=100000.0) - 0.00413) < 1e-5   # premium-cap-bound
assert abs(_fees(500.0, 0.001, 1, spot=100000.0) - 0.0118) < 1e-4    # notional-bound (0.01*1.18)
assert _fees(100.0, 0.001, 1, spot=100000.0, taker_rate=0.0002, premium_rate=0.05) \
       > _fees(100.0, 0.001, 1, spot=100000.0)                       # per-product override flows
print("FIX3 per-product fees + min + GST OK")

# --- FIX 5: X-RATE-LIMIT-RESET is milliseconds; Retry-After is seconds ---
from strategy.delta_client import _retry_after
_mk = lambda h: type("R", (), {"headers": h})()
assert _retry_after(_mk({"X-RATE-LIMIT-RESET": "500"})) == 0.5       # 500ms -> 0.5s, not 500s
assert _retry_after(_mk({"X-RATE-LIMIT-RESET": "2000"})) == 2.0
assert _retry_after(_mk({"Retry-After": "60"})) == 60.0             # header already in seconds
assert _retry_after(_mk({})) == 5.0
print("FIX5 rate-limit unit handling OK")

# --- FIX 6: theta erosion uses theta/price (per-unit), not theta*contract_value/price ---
ex6 = Executor(OptionResolver(config.ASSET))
q6 = OptionQuote("__THETA__", 999997, 100000.0, "CE", 100.0, 101.0, 100.5, 0.001, 0.5, "01-01-2099")
ex6._register_position("__theta__", "CE",
                       q6, Signal("__theta__", "CE", "t", confidence=50),
                       entry=100.0, contracts=1, bar_seconds=60, leverage=1,
                       order_id=None, adv_params={})
p6 = next(p for p in ex6.positions.values() if p.strategy == "__theta__")
p6.meta["daily_theta"] = 20.0                    # 20/100 = 20%/day  > 15% -> must auto-close
ex6.resolver.live_price = lambda sym: 100.0      # stub the exit quote
ex6._check_theta_erosion()
assert p6.id not in ex6.positions, "20%/day theta must auto-close (the old cv-bug computed 0.02%)"
strat_store.purge_trades(strategies=("__theta__",))
print("FIX6 theta erosion units OK")

print("\nALL CHECKS PASSED")

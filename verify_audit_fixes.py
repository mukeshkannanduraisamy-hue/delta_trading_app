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

# --- #1 order payload field types ---
import inspect
from strategy.delta_client import client, DeltaError
sent = {}


def _fake_signed(method, path, params=None, payload=None, _retried_sig=False):
    sent.update({"method": method, "path": path, "payload": payload})
    return {"id": 1, "state": "closed", "unfilled_size": 0}


client._signed = _fake_signed
client.place_order(123, 5, "sell", "market_order", reduce_only=True,
                   client_order_id="x" * 40)
p = sent["payload"]
assert p["reduce_only"] == "true" and isinstance(p["reduce_only"], str), p
assert p["size"] == 5 and isinstance(p["size"], int), p
assert len(p["client_order_id"]) == 32, p
client.place_order(123, 5, "buy", "limit_order", limit_price=12.5)
p = sent["payload"]
assert p["limit_price"] == "12.5" and isinstance(p["limit_price"], str), p
assert p["reduce_only"] == "false", p
for bad in (dict(size=0), dict(size=-1)):
    try:
        client.place_order(123, side="buy", **bad)
        raise AssertionError("expected rejection for " + str(bad))
    except DeltaError:
        pass
print("order payload field types OK")

print("\nALL CHECKS PASSED")

"""Verify the engine is LIVE ONLY — no simulated-fill path remains.

Run from delta_trading_app/:
    .\\.venv\\Scripts\\python.exe verify_live_only.py

Places NO orders: the one order-path test uses a stubbed client so it can prove
an order WOULD be sent without actually sending one.
"""
import sys
import types

from strategy import config
from strategy.base import Signal
from strategy.market_data import OptionQuote

print("=" * 72)
print("LIVE-ONLY VERIFICATION")
print("=" * 72)

# --- 1. Config is pinned to live, regardless of .env -----------------------
assert config.LIVE_DEMO is True, config.LIVE_DEMO
assert config.EXECUTION_MODE == "live_demo", config.EXECUTION_MODE
assert config.EXEC_BASE == config.TESTNET_BASE, config.EXEC_BASE
assert config.summary()["paper_mode_available"] is False
print(f"config      : mode={config.EXECUTION_MODE} live_only=True "
      f"exec_base={config.EXEC_BASE}")
print(f"              contracts={config.CONTRACTS} entry={config.ENTRY_ORDER_TYPE}")

# --- 2. Signed calls can only reach testnet -------------------------------
from strategy.delta_client import client, DeltaError, DeltaAuthError  # noqa: E402
assert client.testnet == config.TESTNET_BASE
assert "testnet" in client.testnet
print(f"signed base : {client.testnet}  (real-money book unreachable)")

# --- 3. No LIVE_DEMO conditionals remain in the execution path ------------
import inspect  # noqa: E402
from strategy import executor as ex  # noqa: E402

src = inspect.getsource(ex)
for marker in ("if config.LIVE_DEMO", "if not config.LIVE_DEMO"):
    assert marker not in src, f"paper branch still present: {marker!r}"
print("executor    : no LIVE_DEMO conditionals remain in the execution path")

# --- 4. Entry ACTUALLY places an order (stubbed, nothing is sent) ----------
sent = []


class _StubClient:
    def place_order(self, product_id, size, side, order_type="market_order",
                    limit_price=None, reduce_only=False, client_order_id=None,
                    time_in_force="gtc"):
        sent.append({"product_id": product_id, "size": size, "side": side,
                     "order_type": order_type, "reduce_only": reduce_only,
                     "client_order_id": client_order_id})
        return {"id": 999, "state": "closed", "unfilled_size": 0,
                "average_fill_price": "125.0"}

    def positions(self, product_id=None):
        return []


# Stub the STORE as well: _book_close() persists every close to SQLite, so an
# unguarded run injects synthetic fills into the real trade record.
_recorded = []
real_record = ex.store.record_trade
ex.store.record_trade = lambda row: _recorded.append(row)

real_client = ex.client
ex.client = _StubClient()
try:
    resolver = types.SimpleNamespace(live_price=lambda s: 120.0)
    e = ex.Executor(resolver)
    q = OptionQuote("C-BTC-66000-210726", 4242, 66000.0, "CE",
                    120.0, 125.0, 122.0, 0.001, 0.5, "31-12-2027")
    sig = Signal("verify", "CE", "live-only check", sl_pct=0.05, rr=1.5)
    pos = e.open(sig, q, 60)
    assert pos is not None, "entry returned None — no position opened"
    assert len(sent) == 1, f"expected exactly 1 order, got {len(sent)}"
    o = sent[0]
    assert o["side"] == "buy" and o["size"] == config.CONTRACTS
    assert o["client_order_id"], "entry order carries no idempotency key"
    print(f"entry       : ORDER SENT  {o['side']} {o['size']} @ {o['order_type']} "
          f"coid={o['client_order_id'][:12]}...")

    # --- 5. Exit sends a reduce_only sell --------------------------------
    p = list(e.positions.values())[0]
    e._close(p, 130.0, "verify_exit")
    assert len(sent) == 2, f"expected an exit order, got {len(sent)} total"
    x = sent[1]
    assert x["side"] == "sell", x
    assert x["reduce_only"] is True, "exit must be reduce_only"
    print(f"exit        : ORDER SENT  {x['side']} {x['size']} reduce_only="
          f"{x['reduce_only']}")
    print(f"store       : {len(_recorded)} write(s) intercepted (0 persisted)")
finally:
    ex.client = real_client
    ex.store.record_trade = real_record

# --- 6. Preflight refuses to start without credentials --------------------
from strategy.engine import Engine  # noqa: E402

eng = Engine()
saved_key, saved_secret = config.API_KEY, config.API_SECRET
config.API_KEY, config.API_SECRET = "", ""
try:
    blocker = eng.preflight()
    assert blocker and "not set" in blocker, blocker
    assert eng.start() is False, "engine started without credentials!"
    print(f"preflight   : REFUSED with no credentials -> {blocker[:58]}...")
finally:
    config.API_KEY, config.API_SECRET = saved_key, saved_secret

# --- 7. Session equity is not presented as a wallet -----------------------
st = ex.Executor(types.SimpleNamespace(live_price=lambda s: None)).stats()
assert "session_equity" in st and st["live_only"] is True
print(f"stats       : session_equity={st['session_equity']} (not the wallet); "
      f"live_only={st['live_only']}")

print("\n" + "=" * 72)
print("LIVE ONLY CONFIRMED — no simulated-fill path remains.")
print("Starting the engine WILL place real orders on the Delta testnet book.")
print("=" * 72)

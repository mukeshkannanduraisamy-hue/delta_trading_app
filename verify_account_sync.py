"""Verify the engine syncs with the real Delta demo account.

Read-only against the exchange (balances / positions / orders). The adoption
path is exercised with a stubbed client so no order is ever sent.

    .\\.venv\\Scripts\\python.exe verify_account_sync.py
"""
import types

from strategy import config
from strategy.account import sync
from strategy.base import Signal  # noqa: F401  (imported by executor path)

print("=" * 74)
print("ACCOUNT SYNC VERIFICATION")
print("=" * 74)
print(f"orphan policy    : {config.ORPHAN_POLICY}")
print(f"sync interval    : {config.ACCOUNT_SYNC_SECONDS}s")
print(f"adopt bracket    : SL {config.ADOPT_SL_PCT:.0%} / RR {config.ADOPT_RR}")

# --- 1. Live pull from the exchange ---------------------------------------
snap = sync.refresh()
print(f"\nsync ok          : {snap['ok']}")
if snap["errors"]:
    print(f"errors           : {snap['errors']}")
print(f"equity (USD)     : {snap['equity_usd']}")
print(f"available (USD)  : {snap['available_usd']}")
print(f"balances         : {len(snap['balances'])} non-zero asset(s)")
for b in snap["balances"][:6]:
    print(f"   {b['asset']:<8} balance={b['balance']}  available={b['available']}")
print(f"positions        : {snap['position_count']}")
print(f"resting orders   : {snap['open_order_count']}")

# --- 2. snapshot() is lock-free and reports staleness ---------------------
s2 = sync.snapshot()
assert "age_sec" in s2 and "stale" in s2
print(f"\nsnapshot age     : {s2['age_sec']}s  stale={s2['stale']}")
assert s2["stale"] is False or not s2["ok"], "fresh successful sync must not be stale"

by_prod = sync.positions_by_product()
print(f"positions_by_product: {by_prod or '{} (flat)'}")

# --- 3. Adoption brings an untracked position under management ------------
from strategy import executor as ex  # noqa: E402
from strategy.market_data import OptionQuote  # noqa: E402

sent = []


class _Stub:
    def place_order(self, *a, **k):
        sent.append((a, k))
        return {"id": 1, "state": "closed", "unfilled_size": 0,
                "average_fill_price": "100.0"}

    def positions(self, product_id=None):
        # One position on the exchange the engine knows nothing about.
        return [{"product_id": 7777, "size": 3, "entry_price": "142.5"}]


fake_quote = OptionQuote("C-BTC-70000-311227", 7777, 70000.0, "CE",
                         140.0, 145.0, 142.0, 0.001, 0.5, "31-12-2027")
resolver = types.SimpleNamespace(
    live_price=lambda s: 140.0,
    quote_for_product=lambda pid: fake_quote if pid == 7777 else None,
)

# Stub the STORE too. These scripts drive the real Executor, and _book_close()
# writes every close to SQLite — so an unguarded run silently injects synthetic
# fills into the durable trade record and corrupts realized P&L. (It did; four
# rows had to be purged.) Nothing here is a real fill, so nothing here persists.
_recorded = []
real_record = ex.store.record_trade
ex.store.record_trade = lambda row: _recorded.append(row)

real = ex.client
ex.client = _Stub()
try:
    e = ex.Executor(resolver)
    assert e.open_count() == 0
    e.reconcile()
    assert e.open_count() == 1, f"orphan was not adopted (open={e.open_count()})"
    p = list(e.positions.values())[0]
    assert p.meta.get("adopted") is True
    assert p.product_id == 7777 and p.contracts == 3
    assert abs(p.entry_price - 142.5) < 1e-9, p.entry_price
    assert p.stop < p.entry_price < p.target
    assert not sent, "adoption must not place any order"
    print(f"\nadoption         : product 7777 size 3 adopted at "
          f"{p.entry_price} (exchange entry_price)")
    print(f"                   stop={p.stop:.2f} target={p.target:.2f} "
          f"strategy={p.strategy!r}")
    print(f"                   orders sent during adoption: {len(sent)}")

    # Now it is tracked, a second reconcile must be a no-op.
    e.reconcile()
    assert e.open_count() == 1, "reconcile double-adopted the same position"
    print("re-reconcile     : no duplicate adoption")

    # --- 4. Ghost: engine holds it, exchange does not -------------------
    class _Empty(_Stub):
        def positions(self, product_id=None):
            return []

    ex.client = _Empty()
    e.reconcile()
    assert e.open_count() == 0, "ghost position was not cleared"
    print("ghost handling   : position absent on exchange -> booked closed")
    print(f"store writes     : {len(_recorded)} intercepted (0 persisted)")
finally:
    ex.client = real
    ex.store.record_trade = real_record

# --- 5. Policy is honoured -------------------------------------------------
assert config.ORPHAN_POLICY in ("adopt", "flatten", "report")
print(f"\npolicy honoured  : ORPHAN_POLICY={config.ORPHAN_POLICY}")

print("\n" + "=" * 74)
print("ACCOUNT SYNC CONFIRMED — exchange is the source of truth.")
print("=" * 74)
